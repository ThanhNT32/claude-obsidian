"""Read-only project daily-summary discovery and history parsing."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from .paths import canonical
from .projects import ProjectError, ProjectScope, resolve_project_scope
from .transaction import read_vault_regular


DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
OPEN_STATUSES = frozenset({"todo", "in-progress", "blocked"})
VALID_STATUSES = frozenset({"todo", "in-progress", "blocked", "done", "deferred"})
TASK_RE = re.compile(r"^(?P<indent>\s*)[-*]\s+\[(?P<mark>[ xX])\]\s+(?P<text>.+?)\s*$")
HEADING_RE = re.compile(r"^(?P<marks>#{1,6})\s+(?P<title>.+?)\s*$")
ITEM_HEADING_RE = re.compile(
    r"^##\s+Item\s+(?P<item_id>[a-z0-9_-]+):\s+(?P<title>.+?)\s*$"
)
FIELD_RE = re.compile(r"^[-*]\s+(?P<key>status|memo|next_action|evidence):\s*(?P<value>.*)\s*$")
STATUS_TOKEN_RE = re.compile(r"\[(?P<status>todo|in-progress|blocked|done|deferred)\]", re.I)


class DailySummaryError(RuntimeError):
    """Stable error raised when a summary cannot be collected safely."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CollectionBudget:
    max_files: int = 500
    max_bytes: int = 8 * 1024 * 1024
    max_history_days: int = 31


def _validate_summary_date(value: str | date) -> str:
    result = value.isoformat() if isinstance(value, date) else value
    if not isinstance(result, str) or DATE_RE.fullmatch(result) is None:
        raise DailySummaryError("DATE_INVALID", "summary date must use YYYY-MM-DD")
    try:
        date.fromisoformat(result)
    except ValueError as exc:
        raise DailySummaryError("DATE_INVALID", "summary date is not calendar-valid") from exc
    return result


def _read_text(vault_root: Path, relative: str, *, limit: int) -> str:
    raw = read_vault_regular(vault_root, relative, limit=limit)
    if raw is None:
        raise DailySummaryError("SOURCE_MISSING", f"source file is missing: {relative}")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DailySummaryError("SOURCE_NOT_UTF8", f"source file is not UTF-8: {relative}") from exc


def _project_relative(scope: ProjectScope, path: Path) -> str:
    try:
        return path.relative_to(scope.wiki_root).as_posix()
    except ValueError as exc:
        raise DailySummaryError("PATH_ESCAPE", f"path is outside project wiki: {path}") from exc


def _vault_relative(vault_root: Path, path: Path) -> str:
    try:
        return path.relative_to(vault_root).as_posix()
    except ValueError as exc:
        raise DailySummaryError("PATH_ESCAPE", f"path is outside vault: {path}") from exc


def _safe_markdown_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    if root.is_symlink() or not root.is_dir():
        raise DailySummaryError("PROJECT_WIKI_INVALID", "project wiki root must be a directory")
    files: list[Path] = []
    for directory, directories, names in __import__("os").walk(root, followlinks=False):
        current = Path(directory)
        directories[:] = sorted(
            name for name in directories if not (current / name).is_symlink() and name != "daily"
        )
        for name in sorted(names):
            path = current / name
            if path.suffix.lower() == ".md" and not path.is_symlink() and path.is_file():
                files.append(path)
    return files


def _frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---\n"):
        return {}
    marker = text.find("\n---\n", 4)
    if marker < 0:
        return {}
    values: dict[str, str] = {}
    for line in text[4:marker].splitlines():
        if ":" not in line or line[:1].isspace():
            continue
        key, value = line.split(":", 1)
        values[key.strip()] = value.strip().strip('"\'')
    return values


def _fingerprint(*parts: str) -> str:
    payload = "\n".join(part.strip() for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _status_from_task(mark: str, text: str) -> str:
    if mark.lower() == "x":
        return "done"
    match = STATUS_TOKEN_RE.search(text)
    return match.group("status").casefold() if match else "todo"


def _task_candidates(relative: str, text: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    heading = ""
    for line_number, line in enumerate(text.splitlines(), start=1):
        heading_match = HEADING_RE.match(line)
        if heading_match:
            heading = heading_match.group("title")
            continue
        match = TASK_RE.match(line)
        if not match:
            continue
        task_text = match.group("text").strip()
        status = _status_from_task(match.group("mark"), task_text)
        if status == "done":
            continue
        item_id = f"task-{_fingerprint(relative, heading, task_text)}"
        candidates.append(
            {
                "id": item_id,
                "type": "task",
                "title": task_text,
                "status": status,
                "evidence": f"{relative}#L{line_number}",
                "source": relative,
                "fingerprint": _fingerprint(relative, heading, task_text),
            }
        )
    return candidates


def _updated_candidate(relative: str, frontmatter: dict[str, str], summary_date: str) -> dict[str, Any] | None:
    if frontmatter.get("updated") != summary_date or relative in {"index.md", "hot.md"}:
        return None
    title = frontmatter.get("title") or relative.removesuffix(".md")
    fingerprint = _fingerprint(relative, summary_date, title)
    return {
        "id": f"change-{fingerprint}",
        "type": "change",
        "title": f"Updated: {title}",
        "status": "todo",
        "evidence": relative,
        "source": relative,
        "fingerprint": fingerprint,
    }


def _history_items(vault_root: Path, scope: ProjectScope, summary_date: str, budget: CollectionBudget) -> tuple[dict[str, dict[str, Any]], list[str]]:
    daily_root = scope.wiki_root / "daily"
    if not daily_root.exists():
        return {}, []
    if daily_root.is_symlink() or not daily_root.is_dir():
        raise DailySummaryError("DAILY_ROOT_INVALID", "project daily root must be a directory")
    dates: list[tuple[str, Path]] = []
    for path in sorted(daily_root.iterdir()):
        if path.is_symlink() or not path.is_file() or path.suffix.lower() != ".md":
            continue
        candidate_date = path.stem
        if DATE_RE.fullmatch(candidate_date) and candidate_date < summary_date:
            dates.append((candidate_date, path))
    dates = dates[-budget.max_history_days :]
    latest: dict[str, dict[str, Any]] = {}
    previous_notes: list[str] = []
    for note_date, path in dates:
        relative = _vault_relative(vault_root, path)
        text = _read_text(vault_root, relative, limit=budget.max_bytes)
        previous_notes.append(relative)
        current: dict[str, Any] | None = None
        for line in text.splitlines():
            heading = ITEM_HEADING_RE.match(line)
            if heading:
                if current is not None:
                    latest[current["id"]] = current
                current = {
                    "id": heading.group("item_id"),
                    "title": heading.group("title"),
                    "date": note_date,
                    "status": "todo",
                    "memo": "",
                    "next_action": "",
                    "evidence": "",
                }
                continue
            if current is None:
                continue
            field = FIELD_RE.match(line)
            if field:
                current[field.group("key")] = field.group("value")
        if current is not None:
            latest[current["id"]] = current
    return latest, previous_notes


def collect_daily_summary(
    vault_root: Path | str,
    *,
    project: str | None,
    summary_date: str | date,
    budget: CollectionBudget | None = None,
) -> dict[str, Any]:
    if not project:
        raise DailySummaryError("PROJECT_REQUIRED", "daily summary requires an explicit project")
    selected_date = _validate_summary_date(summary_date)
    limits = budget or CollectionBudget()
    root = canonical(vault_root)
    try:
        scope = resolve_project_scope(root, explicit=project, shared=False)
    except ProjectError as exc:
        raise DailySummaryError(exc.code, str(exc)) from exc
    files = _safe_markdown_files(scope.wiki_root)
    if len(files) > limits.max_files:
        raise DailySummaryError("FILE_BUDGET_EXCEEDED", "project wiki file budget exceeded")
    total_bytes = 0
    candidates: list[dict[str, Any]] = []
    for path in files:
        relative = _vault_relative(root, path)
        raw = read_vault_regular(root, relative, limit=limits.max_bytes)
        if raw is None:
            raise DailySummaryError("SOURCE_MISSING", f"source file disappeared: {relative}")
        total_bytes += len(raw)
        if total_bytes > limits.max_bytes:
            raise DailySummaryError("BYTE_BUDGET_EXCEEDED", "project wiki byte budget exceeded")
        text = raw.decode("utf-8")
        metadata = _frontmatter(text)
        candidates.extend(_task_candidates(relative, text))
        changed = _updated_candidate(relative, metadata, selected_date)
        if changed is not None:
            candidates.append(changed)
    history, previous_notes = _history_items(root, scope, selected_date, limits)
    carry_forward: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in candidates:
        previous = history.get(item["id"])
        if previous is not None:
            item = {**item, **{key: previous[key] for key in ("status", "memo", "next_action") if previous.get(key)}}
        if item["id"] in seen:
            continue
        seen.add(item["id"])
        if item.get("status") in OPEN_STATUSES and item["id"] in history:
            carry_forward.append(item)
    for item_id, previous in history.items():
        if item_id not in seen and previous.get("status") in OPEN_STATUSES:
            carry_forward.append({
                "id": item_id,
                "type": "carry-forward",
                "title": previous.get("title", item_id),
                "status": previous.get("status", "todo"),
                "memo": previous.get("memo", ""),
                "next_action": previous.get("next_action", ""),
                "evidence": previous.get("evidence", ""),
                "source": previous.get("evidence", ""),
                "fingerprint": item_id.removeprefix("task-").removeprefix("change-"),
            })
    return {
        "schema": "claude-obsidian.daily-summary.v1",
        "project": scope.identifier,
        "date": selected_date,
        "read_only": True,
        "candidates": candidates,
        "carry_forward": carry_forward,
        "previous_notes": previous_notes,
        "warnings": [],
    }