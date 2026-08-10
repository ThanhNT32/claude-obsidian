#!/usr/bin/env python3
"""Focused regression tests for project-aware wiki-mode routing."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WIKI_MODE = ROOT / "scripts/wiki-mode.py"


def make_vault(registry: dict) -> Path:
    root = Path(tempfile.mkdtemp())
    (root / ".vault-meta").mkdir()
    (root / "wiki").mkdir()
    (root / ".raw").mkdir()
    (root / ".vault-meta/projects.json").write_text(
        json.dumps(registry), encoding="utf-8"
    )
    return root


def run_route(root: Path, *extra: str, environ: dict[str, str] | None = None):
    env = os.environ.copy()
    if environ:
        env.update(environ)
    return subprocess.run(
        [sys.executable, str(WIKI_MODE), "--vault", str(root), "route", "entity", "Invoice", *extra],
        cwd=ROOT,
        env=env,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )


def test_project_route_precedence_and_validation() -> None:
    root = make_vault(
        {
            "schema": "claude-obsidian.projects.v1",
            "default_project": "acme/billing",
            "projects": [
                {"customer_code": "acme", "project_code": "billing", "name": "Billing", "aliases": ["bill"]},
                {"customer_code": "acme", "project_code": "portal", "name": "Portal", "aliases": ["portal-app"]},
            ],
        }
    )
    try:
        explicit = run_route(root, "--project", "acme/portal")
        assert explicit.returncode == 0, explicit.stderr
        assert explicit.stdout.strip() == "acme/portal/wiki/entities/Invoice.md"

        env_selected = run_route(root, environ={"CLAUDE_OBSIDIAN_PROJECT": "bill"})
        assert env_selected.returncode == 0, env_selected.stderr
        assert env_selected.stdout.strip() == "acme/billing/wiki/entities/Invoice.md"

        workspace = root / "nested"
        workspace.mkdir()
        (workspace / ".claude-obsidian.json").write_text(
            json.dumps({"schema": "claude-obsidian.workspace.v1", "project": "portal-app"}),
            encoding="utf-8",
        )
        workspace_result = subprocess.run(
            [sys.executable, str(WIKI_MODE), "--vault", str(root), "route", "entity", "Invoice"],
            cwd=workspace,
            env={key: value for key, value in os.environ.items() if key != "CLAUDE_OBSIDIAN_PROJECT"},
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
        assert workspace_result.returncode == 0, workspace_result.stderr
        assert workspace_result.stdout.strip() == "acme/portal/wiki/entities/Invoice.md"

        unknown = run_route(root, "--project", "acme/missing")
        assert unknown.returncode == 2
        assert "PROJECT_NOT_FOUND" in unknown.stderr

        conflict = run_route(root, "--project", "bill", environ={"CLAUDE_OBSIDIAN_PROJECT": "acme/portal"})
        assert conflict.returncode == 0, conflict.stderr
        assert conflict.stdout.strip() == "acme/billing/wiki/entities/Invoice.md"
    finally:
        shutil.rmtree(root)


def test_project_alias_collision_is_rejected() -> None:
    root = make_vault(
        {
            "schema": "claude-obsidian.projects.v1",
            "projects": [
                {"customer_code": "acme", "project_code": "billing", "name": "Billing", "aliases": ["shared"]},
                {"customer_code": "acme", "project_code": "portal", "name": "Portal", "aliases": ["shared"]},
            ],
        }
    )
    try:
        result = run_route(root, "--project", "shared")
        assert result.returncode == 2
        assert "PROJECT_TERM_COLLISION" in result.stderr
    finally:
        shutil.rmtree(root)


if __name__ == "__main__":
    test_project_route_precedence_and_validation()
    test_project_alias_collision_is_rejected()
    print("All project route tests passed.")