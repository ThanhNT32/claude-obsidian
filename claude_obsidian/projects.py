"""Project registry and deterministic project selection for one vault."""

from __future__ import annotations

import json
import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .json_utils import strict_json_loads
from .paths import MAX_WORKSPACE_CONFIG_BYTES, canonical, read_open_flags
from .transaction import read_vault_regular

PROJECTS_SCHEMA = "claude-obsidian.projects.v1"
PROJECTS_PATH = ".vault-meta/projects.json"
_PROJECT_CODE_RE = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$")
_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


class ProjectError(RuntimeError):
    """Base error with a stable machine-readable code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class ProjectValidationError(ProjectError):
    """The project registry or an identifier is invalid."""


class ProjectSelectionError(ProjectError):
    """A requested project cannot be selected unambiguously."""


def _normalized(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _validate_code(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ProjectValidationError(
            "PROJECT_CODE_INVALID", f"{field} must be a non-empty project code"
        )
    if (
        value != _normalized(value)
        or value != value.casefold()
        or "\\" in value
        or "/" in value
        or value in {".", ".."}
        or value.upper() in _RESERVED_NAMES
        or _PROJECT_CODE_RE.fullmatch(value) is None
    ):
        raise ProjectValidationError(
            "PROJECT_CODE_INVALID",
            f"{field} must use lowercase ASCII path-safe code syntax",
        )
    return value


def _validate_text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProjectValidationError(
            "PROJECT_TEXT_INVALID", f"{field} must be non-empty text"
        )
    if any(unicodedata.category(character).startswith("C") for character in value):
        raise ProjectValidationError(
            "PROJECT_TEXT_INVALID", f"{field} contains a control character"
        )
    return unicodedata.normalize("NFC", value).strip()


def _validate_terms(value: Any, *, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ProjectValidationError("PROJECT_TERMS_INVALID", f"{field} must be an array")
    terms: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        term = _validate_text(item, field=f"{field}[{index}]")
        key = _normalized(term)
        if key in seen:
            raise ProjectValidationError(
                "PROJECT_TERM_DUPLICATE", f"duplicate term in {field}: {term}"
            )
        seen.add(key)
        terms.append(term)
    return tuple(terms)


@dataclass(frozen=True)
class Project:
    customer_code: str
    project_code: str
    name: str
    aliases: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()

    @property
    def identifier(self) -> str:
        return f"{self.customer_code}/{self.project_code}"

    @property
    def wiki_prefix(self) -> str:
        return f"{self.identifier}/wiki/"

    @property
    def metadata_prefix(self) -> str:
        return f".vault-meta/projects/{self.identifier}/"

    def as_document(self) -> dict[str, Any]:
        return {
            "customer_code": self.customer_code,
            "project_code": self.project_code,
            "name": self.name,
            "aliases": list(self.aliases),
            "keywords": list(self.keywords),
        }

    @classmethod
    def from_document(cls, value: Mapping[str, Any], *, index: int) -> "Project":
        if not isinstance(value, Mapping):
            raise ProjectValidationError(
                "PROJECT_ENTRY_INVALID", f"projects[{index}] must be an object"
            )
        customer_code = _validate_code(
            value.get("customer_code"), field=f"projects[{index}].customer_code"
        )
        project_code = _validate_code(
            value.get("project_code"), field=f"projects[{index}].project_code"
        )
        name = _validate_text(value.get("name"), field=f"projects[{index}].name")
        aliases = _validate_terms(value.get("aliases", []), field=f"projects[{index}].aliases")
        keywords = _validate_terms(
            value.get("keywords", []), field=f"projects[{index}].keywords"
        )
        return cls(customer_code, project_code, name, aliases, keywords)


@dataclass(frozen=True)
class ProjectRegistry:
    projects: tuple[Project, ...]
    default_project: str | None = None

    @classmethod
    def from_document(cls, value: Any) -> "ProjectRegistry":
        if not isinstance(value, Mapping) or value.get("schema") != PROJECTS_SCHEMA:
            raise ProjectValidationError(
                "PROJECT_REGISTRY_INVALID",
                f"project registry must use {PROJECTS_SCHEMA}",
            )
        raw_projects = value.get("projects", [])
        if not isinstance(raw_projects, list):
            raise ProjectValidationError(
                "PROJECT_REGISTRY_INVALID", "projects must be an array"
            )
        projects = tuple(
            Project.from_document(item, index=index)
            for index, item in enumerate(raw_projects)
        )
        by_identifier: dict[str, Project] = {}
        terms: dict[str, str] = {}
        for project in projects:
            identifier_key = _normalized(project.identifier)
            if identifier_key in by_identifier:
                raise ProjectValidationError(
                    "PROJECT_DUPLICATE", f"duplicate project: {project.identifier}"
                )
            by_identifier[identifier_key] = project
            for term in (project.identifier, *project.aliases):
                term_key = _normalized(term)
                owner = terms.get(term_key)
                if owner is not None and owner != project.identifier:
                    raise ProjectValidationError(
                        "PROJECT_TERM_COLLISION",
                        f"project term {term!r} belongs to both {owner} and {project.identifier}",
                    )
                terms[term_key] = project.identifier
        default = value.get("default_project")
        if default is not None:
            if not isinstance(default, str) or _normalized(default) not in by_identifier:
                raise ProjectValidationError(
                    "PROJECT_DEFAULT_INVALID", "default_project must name a registered project"
                )
            default = by_identifier[_normalized(default)].identifier
        return cls(projects, default)

    @classmethod
    def load(cls, vault_root: Path | str) -> "ProjectRegistry":
        raw = read_vault_regular(canonical(vault_root), PROJECTS_PATH, limit=1024 * 1024)
        if raw is None:
            return cls(())
        try:
            value = strict_json_loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ProjectValidationError(
                "PROJECT_REGISTRY_INVALID", f"cannot read {PROJECTS_PATH}: {exc}"
            ) from exc
        return cls.from_document(value)

    def by_identifier(self, identifier: str) -> Project | None:
        key = _normalized(identifier)
        return next(
            (project for project in self.projects if _normalized(project.identifier) == key),
            None,
        )

    def matches(self, value: str) -> tuple[Project, ...]:
        normalized = _normalized(_validate_text(value, field="project selector"))
        matches: dict[str, Project] = {}
        for project in self.projects:
            flat_terms = (project.identifier, *project.aliases)
            if any(_normalized(term) == normalized for term in flat_terms):
                matches[project.identifier] = project
        return tuple(sorted(matches.values(), key=lambda project: project.identifier))

    def classify_text(self, value: str) -> tuple[Project, ...]:
        text = _normalized(_validate_text(value, field="classification text"))
        matches: dict[str, Project] = {}
        for project in self.projects:
            terms = (project.identifier, *project.aliases, *project.keywords)
            if any(_term_in_text(_normalized(term), text) for term in terms):
                matches[project.identifier] = project
        return tuple(sorted(matches.values(), key=lambda project: project.identifier))


def _term_in_text(term: str, text: str) -> bool:
    start = 0
    while True:
        index = text.find(term, start)
        if index < 0:
            return False
        before = text[index - 1] if index else " "
        after_index = index + len(term)
        after = text[after_index] if after_index < len(text) else " "
        if not (before.isalnum() or before in "_-" or after.isalnum() or after in "_-"):
            return True
        start = after_index


def _read_workspace_project(start: Path | str | None) -> str | None:
    if start is None:
        return None
    current = canonical(start)
    if current.is_file():
        current = current.parent
    for directory in (current, *current.parents):
        path = directory / ".claude-obsidian.json"
        try:
            before = path.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ProjectValidationError(
                "PROJECT_WORKSPACE_INVALID", f"cannot inspect {path}: {exc}"
            ) from exc
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_WORKSPACE_CONFIG_BYTES:
            raise ProjectValidationError(
                "PROJECT_WORKSPACE_INVALID", f"workspace config is not a regular bounded file: {path}"
            )
        descriptor = -1
        try:
            descriptor = os.open(path, read_open_flags())
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise ValueError("workspace config changed before it could be read")
            raw = os.read(descriptor, MAX_WORKSPACE_CONFIG_BYTES + 1)
        except (OSError, ValueError) as exc:
            raise ProjectValidationError(
                "PROJECT_WORKSPACE_INVALID", f"cannot read {path}: {exc}"
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        try:
            value = strict_json_loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ProjectValidationError(
                "PROJECT_WORKSPACE_INVALID", f"cannot parse {path}: {exc}"
            ) from exc
        if not isinstance(value, Mapping) or value.get("schema") != "claude-obsidian.workspace.v1":
            raise ProjectValidationError(
                "PROJECT_WORKSPACE_INVALID", f"{path} must declare the workspace schema"
            )
        project = value.get("project")
        if project is not None and (not isinstance(project, str) or not project.strip()):
            raise ProjectValidationError(
                "PROJECT_WORKSPACE_INVALID", "workspace project must be non-empty text"
            )
        return project
    return None


def resolve_project(
    registry: ProjectRegistry,
    *,
    explicit: str | None = None,
    start: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
) -> Project | None:
    env = os.environ if environ is None else environ
    requested = explicit
    source = "explicit"
    if requested is None:
        requested = env.get("CLAUDE_OBSIDIAN_PROJECT") or None
        source = "environment"
    if requested is None:
        requested = _read_workspace_project(start)
        source = "workspace-config"
    if requested is None:
        requested = registry.default_project
        source = "registry-default"
    if requested is None:
        return None
    if not isinstance(requested, str) or not requested.strip():
        raise ProjectSelectionError("PROJECT_REQUIRED", "project selector must be non-empty")
    project = registry.by_identifier(requested)
    if project is None:
        matches = registry.matches(requested)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ProjectSelectionError(
                "PROJECT_AMBIGUOUS", f"{source} project selector matches multiple projects"
            )
        raise ProjectSelectionError(
            "PROJECT_NOT_FOUND", f"project is not registered: {requested}"
        )
    return project


__all__ = [
    "PROJECTS_PATH",
    "PROJECTS_SCHEMA",
    "Project",
    "ProjectError",
    "ProjectRegistry",
    "ProjectSelectionError",
    "ProjectValidationError",
    "resolve_project",
]
