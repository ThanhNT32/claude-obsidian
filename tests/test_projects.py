#!/usr/bin/env python3
"""Hermetic tests for multi-project registry selection."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from claude_obsidian.projects import (
    PROJECTS_PATH,
    PROJECTS_SCHEMA,
    ProjectRegistry,
    ProjectSelectionError,
    ProjectValidationError,
    resolve_project,
)


def registry(*projects: dict[str, object], default_project: str | None = None) -> ProjectRegistry:
    return ProjectRegistry.from_document(
        {
            "schema": PROJECTS_SCHEMA,
            "default_project": default_project,
            "projects": list(projects),
        }
    )


def project(customer: str, code: str, *, aliases: list[str] | None = None, keywords: list[str] | None = None) -> dict[str, object]:
    return {
        "customer_code": customer,
        "project_code": code,
        "name": f"{customer} {code}",
        "aliases": aliases or [],
        "keywords": keywords or [],
    }


def make_vault(root: Path) -> Path:
    (root / ".vault-meta").mkdir(parents=True)
    return root


def test_registry_validates_codes_and_collisions() -> None:
    current = registry(
        project("acme", "billing-api", aliases=["billing"], keywords=["invoice"]),
        project("globex", "portal", aliases=["customer-portal"]),
        default_project="acme/billing-api",
    )
    assert current.default_project == "acme/billing-api"
    assert current.projects[0].wiki_prefix == "acme/billing-api/wiki/"
    assert current.projects[0].metadata_prefix == ".vault-meta/projects/acme/billing-api/"

    for invalid in ("../escape", "Acme", "customer/project", "CON", "bad\\name"):
        try:
            registry(project(invalid, "app"))
        except ProjectValidationError as exc:
            assert exc.code == "PROJECT_CODE_INVALID"
        else:
            raise AssertionError(f"invalid code accepted: {invalid}")

    try:
        registry(
            project("acme", "one", aliases=["shared"]),
            project("globex", "two", aliases=["shared"]),
        )
    except ProjectValidationError as exc:
        assert exc.code == "PROJECT_TERM_COLLISION"
    else:
        raise AssertionError("duplicate aliases must be rejected")


def test_resolution_precedence_and_workspace_default() -> None:
    current = registry(
        project("acme", "one", aliases=["first"]),
        project("globex", "two", aliases=["second"]),
        default_project="acme/one",
    )
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        workspace = root / "workspace"
        workspace.mkdir()
        (workspace / ".claude-obsidian.json").write_text(
            json.dumps(
                {
                    "schema": "claude-obsidian.workspace.v1",
                    "vault": "../vault",
                    "project": "globex/two",
                }
            ),
            encoding="utf-8",
        )
        nested = workspace / "src"
        nested.mkdir()
        assert resolve_project(current, start=nested, environ={}).identifier == "globex/two"
        assert (
            resolve_project(
                current,
                explicit="acme/one",
                start=nested,
                environ={"CLAUDE_OBSIDIAN_PROJECT": "globex/two"},
            ).identifier
            == "acme/one"
        )
        assert (
            resolve_project(
                current,
                start=root / "elsewhere",
                environ={"CLAUDE_OBSIDIAN_PROJECT": "globex/two"},
            ).identifier
            == "globex/two"
        )
        assert resolve_project(current, start=root / "elsewhere", environ={}).identifier == "acme/one"


def test_classification_is_hint_only_and_ambiguous() -> None:
    current = registry(
        project("acme", "one", aliases=["first"], keywords=["billing"]),
        project("globex", "two", aliases=["second"], keywords=["billing"]),
    )
    assert [item.identifier for item in current.matches("FIRST")] == ["acme/one"]
    assert [item.identifier for item in current.classify_text("new billing invoice")] == [
        "acme/one",
        "globex/two",
    ]
    assert current.classify_text("billingness") == ()
    try:
        resolve_project(current, explicit="billing")
    except ProjectSelectionError as exc:
        assert exc.code == "PROJECT_NOT_FOUND"
    else:
        raise AssertionError("keywords must not silently select a project")


def test_load_missing_registry_preserves_legacy_scope() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = make_vault(Path(directory))
        loaded = ProjectRegistry.load(root)
        assert loaded.projects == ()
        assert resolve_project(loaded) is None
        registry_path = root / PROJECTS_PATH
        assert not registry_path.exists()


def test_cli_project_add_is_reviewed_and_transactional() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = make_vault(Path(directory))
        (root / ".obsidian").mkdir()
        (root / "wiki").mkdir()
        (root / ".raw").mkdir()
        common = [
            sys.executable,
            str(ROOT / "scripts/claude-obsidian.py"),
            "project",
            "add",
            "--vault",
            str(root),
            "--customer-code",
            "acme",
            "--project-code",
            "billing-api",
            "--name",
            "Billing API",
            "--alias",
            "billing",
            "--keyword",
            "invoice",
            "--default",
            "--generated-at",
            "2026-08-10T00:00:00Z",
            "--operation-id",
            "project-test",
        ]
        preview = subprocess.run(
            common,
            cwd=ROOT,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
        assert preview.returncode == 0, preview.stderr
        plan = json.loads(preview.stdout)
        assert plan["status"] == "dry-run"
        assert [write["path"] for write in plan["operation"]["writes"]] == [PROJECTS_PATH]
        assert not (root / PROJECTS_PATH).exists()
        if os.name == "nt":
            return
        applied = subprocess.run(
            [*common, "--approved-plan-sha256", plan["approved_plan_sha256"], "--apply"],
            cwd=ROOT,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
        assert applied.returncode == 0, applied.stderr
        document = json.loads((root / PROJECTS_PATH).read_text(encoding="utf-8"))
        assert document["default_project"] == "acme/billing-api"
        assert document["projects"][0]["aliases"] == ["billing"]

        listed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/claude-obsidian.py"),
                "project",
                "list",
                "--vault",
                str(root),
            ],
            cwd=ROOT,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
        assert listed.returncode == 0, listed.stderr
        assert json.loads(listed.stdout)["projects"][0]["identifier"] == "acme/billing-api"


if __name__ == "__main__":
    test_registry_validates_codes_and_collisions()
    test_resolution_precedence_and_workspace_default()
    test_classification_is_hint_only_and_ambiguous()
    test_load_missing_registry_preserves_legacy_scope()
    test_cli_project_add_is_reviewed_and_transactional()
    print("All project tests passed.")
