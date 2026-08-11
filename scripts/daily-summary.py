#!/usr/bin/env python3
"""Collect a project-scoped daily-summary draft without mutating the vault."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
sys.dont_write_bytecode = True


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from claude_obsidian.daily_summary import (  # noqa: E402
    CollectionBudget,
    DailySummaryError,
    collect_daily_summary,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect a project daily summary.")
    parser.add_argument("--vault", required=True, help="Explicit user vault root")
    parser.add_argument("--project", required=True, help="Registered customer/project identifier")
    parser.add_argument("--date", default=date.today().isoformat(), help="Summary date in YYYY-MM-DD form")
    parser.add_argument("--max-files", type=int, default=500)
    parser.add_argument("--max-bytes", type=int, default=8 * 1024 * 1024)
    parser.add_argument("--max-history-days", type=int, default=31)
    args = parser.parse_args(argv)
    try:
        result = collect_daily_summary(
            args.vault,
            project=args.project,
            summary_date=args.date,
            budget=CollectionBudget(
                max_files=args.max_files,
                max_bytes=args.max_bytes,
                max_history_days=args.max_history_days,
            ),
        )
    except DailySummaryError as exc:
        print(
            json.dumps(
                {
                    "schema": "claude-obsidian.daily-summary-error.v1",
                    "code": exc.code,
                    "message": str(exc),
                }
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
