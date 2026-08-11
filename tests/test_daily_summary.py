#!/usr/bin/env python3
"""Hermetic tests for project-scoped daily-summary discovery."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from claude_obsidian.daily_summary import DailySummaryError, collect_daily_summary


def make_vault(root: Path) -> None:
    (root / ".vault-meta").mkdir(parents=True)
    (root / "wiki").mkdir()
    (root / ".vault-meta/projects.json").write_text(
        json.dumps(
            {
                "schema": "claude-obsidian.projects.v1",
                "projects": [
                    {"customer_code": "acme", "project_code": "billing", "name": "Billing"},
                    {"customer_code": "acme", "project_code": "portal", "name": "Portal"},
                ],
            }
        ),
        encoding="utf-8",
    )


def test_collects_tasks_and_updated_pages_only_for_selected_project() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        make_vault(root)
        billing = root / "acme/billing/wiki"
        portal = root / "acme/portal/wiki"
        billing.mkdir(parents=True)
        portal.mkdir(parents=True)
        (billing / "plan.md").write_text(
            "---\ntitle: Billing Plan\nupdated: 2026-08-11\n---\n"
            "# Plan\n- [ ] Ship invoice export\n- [x] Create schema\n",
            encoding="utf-8",
        )
        (portal / "plan.md").write_text(
            "---\ntitle: Portal Plan\nupdated: 2026-08-11\n---\n- [ ] Portal task\n",
            encoding="utf-8",
        )

        result = collect_daily_summary(root, project="acme/billing", summary_date="2026-08-11")

        assert result["project"] == "acme/billing"
        assert result["read_only"] is True
        assert {item["title"] for item in result["candidates"]} == {
            "Ship invoice export",
            "Updated: Billing Plan",
        }
        assert all(item["source"].startswith("acme/billing/wiki/") for item in result["candidates"])


def test_carries_forward_open_items_and_preserves_memo() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        make_vault(root)
        wiki = root / "acme/billing/wiki"
        (wiki / "daily").mkdir(parents=True)
        (wiki / "plan.md").write_text(
            "# Plan\n- [ ] Ship export\n",
            encoding="utf-8",
        )
        previous = collect_daily_summary(
            root, project="acme/billing", summary_date="2026-08-10"
        )
        item_id = previous["candidates"][0]["id"]
        (wiki / "daily/2026-08-10.md").write_text(
            "---\ntype: daily-summary\n---\n"
            f"## Item {item_id}: Ship export\n"
            "- status: blocked\n- memo: Waiting for API access\n- next_action: Ask platform\n\n"
            "## Item task-done: Create schema\n"
            "- status: done\n- memo: Merged\n",
            encoding="utf-8",
        )

        result = collect_daily_summary(root, project="acme/billing", summary_date="2026-08-11")

        assert len(result["carry_forward"]) == 1
        item = result["carry_forward"][0]
        assert item["id"] == item_id
        assert item["status"] == "blocked"
        assert item["memo"] == "Waiting for API access"
        assert item["next_action"] == "Ask platform"
        assert all(entry["status"] != "done" for entry in result["carry_forward"])


def test_requires_explicit_registered_project() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        make_vault(root)
        for project in (None, "acme/missing"):
            try:
                collect_daily_summary(root, project=project, summary_date="2026-08-11")
            except DailySummaryError as exc:
                assert exc.code in {"PROJECT_REQUIRED", "PROJECT_NOT_FOUND"}
            else:
                raise AssertionError("invalid project selection was accepted")


def test_cli_wrapper_returns_read_only_json() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        make_vault(root)
        wiki = root / "acme/billing/wiki"
        wiki.mkdir(parents=True)
        (wiki / "plan.md").write_text("# Plan\n- [ ] Ship export\n", encoding="utf-8")
        command = [
            sys.executable,
            str(ROOT / "scripts/daily-summary.py"),
            "--vault",
            str(root),
            "--project",
            "acme/billing",
            "--date",
            "2026-08-11",
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stderr
        document = json.loads(result.stdout)
        assert document["schema"] == "claude-obsidian.daily-summary.v1"
        assert document["read_only"] is True
        assert document["project"] == "acme/billing"

        invalid_command = list(command)
        invalid_command[invalid_command.index("--project") + 1] = "missing/project"
        invalid = subprocess.run(
            invalid_command,
            capture_output=True,
            text=True,
            check=False,
        )
        assert invalid.returncode == 2
        assert json.loads(invalid.stderr)["code"] == "PROJECT_NOT_FOUND"


if __name__ == "__main__":
    test_collects_tasks_and_updated_pages_only_for_selected_project()
    test_carries_forward_open_items_and_preserves_memo()
    test_requires_explicit_registered_project()
    test_cli_wrapper_returns_read_only_json()
    print("All daily-summary tests passed.")