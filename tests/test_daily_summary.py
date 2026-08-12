#!/usr/bin/env python3
"""Hermetic tests for project-scoped daily-summary discovery."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from claude_obsidian.daily_summary import (
    DailySummaryError,
    collect_daily_summary,
    render_daily_summary,
)


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
        assert {item["title"] for item in result["follow_ups"]} == {"Ship invoice export"}
        assert {item["title"] for item in result["changes"]} == {"Updated: Billing Plan"}
        assert all(item["source"].startswith("acme/billing/wiki/") for item in result["follow_ups"])
        assert result["changes"][0]["source_link"] == "[[acme/billing/wiki/plan]]"


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
        item_id = previous["follow_ups"][0]["id"]
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


def test_changes_are_not_follow_ups_and_work_sources_extract_action_items() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        make_vault(root)
        wiki = root / "acme/billing/wiki"
        wiki.mkdir(parents=True)
        (wiki / "trace-matrices.md").write_text(
            "---\ntitle: Cross-Platform Trace Matrix\nupdated: 2026-08-11\n---\n"
            "Ma tran truy vet xuyen nen tang.\n",
            encoding="utf-8",
        )
        (wiki / "jira-backlog.md").write_text(
            "---\ntype: jira\ntitle: Backlog\n---\n"
            "TODO: Confirm API contract | assignee: linh\n"
            "ACTION: Review acceptance criteria | assignee: another-user\n"
            "[[trace-matrices]]\n",
            encoding="utf-8",
        )

        result = collect_daily_summary(
            root,
            project="acme/billing",
            summary_date="2026-08-11",
            owner="linh",
        )

        assert [item["title"] for item in result["changes"]] == [
            "Updated: Cross-Platform Trace Matrix"
        ]
        assert [item["title"] for item in result["follow_ups"]] == [
            "Confirm API contract | assignee: linh"
        ]
        assert result["follow_ups"][0]["source_kind"] == "jira"
        assert result["follow_ups"][0]["source_link"] == "[[acme/billing/wiki/jira-backlog]]"
        assert result["follow_ups"][0]["related_links"] == ["[[trace-matrices]]"]


def test_open_work_sections_yield_follow_ups_and_dedupe_across_pages() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        make_vault(root)
        wiki = root / "acme/billing/wiki"
        wiki.mkdir(parents=True)
        (wiki / "register.md").write_text(
            "---\ntype: concept\ntitle: Change Register\n---\n"
            "## Open / unresolved\n\n"
            "- **Confirm the API contract** (ev-001).\n"
            "- **Review OT status** (ev-002).\n",
            encoding="utf-8",
        )
        (wiki / "plan.md").write_text(
            "---\ntype: concept\ntitle: Plan\n---\n"
            "## TODO / action items\n\n"
            "- Confirm the API contract.\n"
            "- Provision a vibeflow account.\n",
            encoding="utf-8",
        )

        result = collect_daily_summary(root, project="acme/billing", summary_date="2026-08-11")

        titles = [item["title"] for item in result["follow_ups"]]
        # "Confirm the API contract" appears in both register (## Open / unresolved)
        # and plan (## TODO / action items); it must collapse to one.
        norm = [t.rstrip(".").casefold() for t in titles]
        assert norm.count("confirm the api contract") == 1
        assert any("review ot status" in t for t in norm)
        assert any("provision a vibeflow account" in t for t in norm)
        assert all(item["status"] in {"todo", "in-progress", "blocked"} for item in result["follow_ups"])


def test_descriptive_log_bullets_are_not_follow_ups() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        make_vault(root)
        wiki = root / "acme/billing/wiki"
        wiki.mkdir(parents=True)
        (wiki / "log.md").write_text(
            "---\ntype: meta\ntitle: Log\n---\n"
            "## 2026-08-11 — WBS & Backlog status export ingest\n\n"
            "- WBS: 603 tasks, 533 Done (88%).\n"
            "- Backlog: 1,437 issues, 1,155 closed.\n",
            encoding="utf-8",
        )

        result = collect_daily_summary(root, project="acme/billing", summary_date="2026-08-11")

        # Descriptive ingest prose under a dated log heading must not become tasks.
        assert result["follow_ups"] == []


def test_change_history_is_not_carried_forward() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        make_vault(root)
        wiki = root / "acme/billing/wiki"
        (wiki / "daily").mkdir(parents=True)
        (wiki / "daily/2026-08-10.md").write_text(
            "## Changes\n\n"
            "### Change change-old: Updated matrix\n\n"
            "- type: change\n- evidence: [[acme/billing/wiki/matrix]]\n\n"
            "## Follow-ups\n",
            encoding="utf-8",
        )

        result = collect_daily_summary(root, project="acme/billing", summary_date="2026-08-11")

        assert result["carry_forward"] == []


def test_daily_summary_uses_wiki_query_retrieval_before_draft() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        make_vault(root)
        wiki = root / "acme/billing/wiki"
        wiki.mkdir(parents=True)
        (wiki / "plan.md").write_text(
            "---\ntitle: Billing Plan\n---\n- [ ] Ship export\n",
            encoding="utf-8",
        )
        retrieval = {
            "query": "billing delivery risks",
            "strategy": "bm25-only",
            "candidates": [
                {
                    "page_path": "acme/billing/wiki/plan.md",
                    "snippet": "Ship export before the release review.",
                    "bm25_score": 4.2,
                }
            ],
            "explain": {"bm25_candidate_count": 1},
        }
        completed = subprocess.CompletedProcess([], 0, json.dumps(retrieval), "")

        with patch("claude_obsidian.daily_summary.subprocess.run", return_value=completed) as run:
            result = collect_daily_summary(
                root,
                project="acme/billing",
                summary_date="2026-08-11",
                search_query="billing delivery risks",
            )

        assert result["search_context"]["strategy"] == "bm25-only"
        assert result["search_context"]["candidates"] == [
            {
                "source": "acme/billing/wiki/plan.md",
                "source_link": "[[acme/billing/wiki/plan]]",
                "snippet": "Ship export before the release review.",
                "score": 4.2,
            }
        ]
        assert result["warnings"] == []
        command = run.call_args.args[0]
        assert "scripts/retrieve.py" in command[1]
        assert "--no-rerank" in command
        assert "--explain" in command
        assert command[-1] == "billing delivery risks"


def test_daily_summary_reports_retrieval_fallback_without_failing() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        make_vault(root)
        wiki = root / "acme/billing/wiki"
        wiki.mkdir(parents=True)
        (wiki / "plan.md").write_text("# Plan\n- [ ] Ship export\n", encoding="utf-8")
        completed = subprocess.CompletedProcess([], 10, "", "index unavailable")

        with patch("claude_obsidian.daily_summary.subprocess.run", return_value=completed):
            result = collect_daily_summary(
                root,
                project="acme/billing",
                summary_date="2026-08-11",
            )

        assert result["search_context"]["strategy"] == "fallback-project-scan"
        assert result["warnings"] == [
            "wiki-retrieve unavailable: index unavailable; used bounded project scan"
        ]


def test_renderer_produces_decision_ready_vietnamese_format() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        make_vault(root)
        wiki = root / "acme/billing/wiki"
        wiki.mkdir(parents=True)
        (wiki / "trace-matrix.md").write_text(
            "---\ntitle: Cross-PF Trace Matrix\nupdated: 2026-08-11\n---\n"
            "Purpose: Scope analysis for the new business flow.\n",
            encoding="utf-8",
        )
        (wiki / "open-items.md").write_text(
            "---\ntitle: Project Change Register\n---\n"
            "## Open / unresolved\n- What is the functional scope?\n",
            encoding="utf-8",
        )

        result = collect_daily_summary(root, project="acme/billing", summary_date="2026-08-11")
        rendered = render_daily_summary(result)

        assert "## TL;DR" in rendered
        assert "## What Happened" in rendered
        assert "## Follow-ups" in rendered
        assert "### Change change-" not in rendered
        assert "- What:" in rendered
        assert "- Why:" in rendered
        assert "- Impact:" in rendered
        assert "- Background:" in rendered
        assert "- Owner: N/A" in rendered
        assert "- Due: N/A" in rendered
        assert "- Next action: Confirm answer" in rendered
        assert "- Memo:" in rendered
        assert "related" not in rendered.casefold()


def test_renderer_groups_system_changes_without_action_fields() -> None:
    summary = {
        "date": "2026-08-11",
        "project": "acme/billing",
        "lang": "vi",
        "changes": [
            {
                "title": "Updated: Wiki Log",
                "category": "system",
                "signal": "🟢",
                "source_link": "[[acme/billing/wiki/log]]",
            },
            {
                "title": "Updated: Wiki Index",
                "category": "system",
                "signal": "🟢",
                "source_link": "[[acme/billing/wiki/index]]",
            },
        ],
        "follow_ups": [],
        "carry_forward": [],
    }

    rendered = render_daily_summary(summary)

    assert rendered.count("Cập nhật hệ thống / tự động") == 1
    assert "Không cần hành động" in rendered
    assert "- What:" not in rendered


def test_rendered_follow_up_keeps_carry_forward_identity() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        make_vault(root)
        wiki = root / "acme/billing/wiki"
        wiki.mkdir(parents=True, exist_ok=True)
        (wiki / "plan.md").write_text(
            "---\ntitle: Plan\n---\n## Open / unresolved\n- What is the API scope?\n",
            encoding="utf-8",
        )
        first = collect_daily_summary(root, project="acme/billing", summary_date="2026-08-11")
        daily = wiki / "daily"
        daily.mkdir()
        rendered = render_daily_summary(first)
        rendered = rendered.replace("- Owner: N/A", "- Owner: Linh")
        rendered = rendered.replace("- Due: N/A", "- Due: 2026-08-14")
        rendered = rendered.replace("- Memo: ", "- Memo: Đã hỏi khách")
        (daily / "2026-08-11.md").write_text(rendered, encoding="utf-8")

        second = collect_daily_summary(root, project="acme/billing", summary_date="2026-08-12")

        assert second["carry_forward"]
        assert second["carry_forward"][0]["id"] == first["follow_ups"][0]["id"]
        assert second["carry_forward"][0]["owner"] == "Linh"
        assert second["carry_forward"][0]["due"] == "2026-08-14"
        assert second["carry_forward"][0]["memo"] == "Đã hỏi khách"


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
            "--owner",
            "linh",
            "--query",
            "billing delivery risks",
            "--date",
            "2026-08-11",
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stderr
        document = json.loads(result.stdout)
        assert document["schema"] == "claude-obsidian.daily-summary.v2"
        assert document["read_only"] is True
        assert document["project"] == "acme/billing"
        assert document["search_context"]["query"] == "billing delivery risks"

        markdown_command = [*command, "--format", "markdown"]
        markdown = subprocess.run(
            markdown_command, capture_output=True, text=True, check=False
        )
        assert markdown.returncode == 0, markdown.stderr
        assert "## What Happened" in markdown.stdout
        assert "## Follow-ups" in markdown.stdout
        assert "## TL;DR" in markdown.stdout

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
    test_changes_are_not_follow_ups_and_work_sources_extract_action_items()
    test_change_history_is_not_carried_forward()
    test_open_work_sections_yield_follow_ups_and_dedupe_across_pages()
    test_descriptive_log_bullets_are_not_follow_ups()
    test_daily_summary_uses_wiki_query_retrieval_before_draft()
    test_daily_summary_reports_retrieval_fallback_without_failing()
    test_renderer_produces_decision_ready_vietnamese_format()
    test_renderer_groups_system_changes_without_action_fields()
    test_requires_explicit_registered_project()
    test_cli_wrapper_returns_read_only_json()
    print("All daily-summary tests passed.")