#!/usr/bin/env python3
"""Regression coverage for project-isolated BM25 derived state."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BM25 = ROOT / "scripts/bm25-index.py"


def digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_page_and_chunk(root: Path, scope: str, address: str, name: str, text: str) -> None:
    page = root / scope / "wiki" / f"{name}.md" if scope else root / "wiki" / f"{name}.md"
    metadata = root / ".vault-meta" / "projects" / scope if scope else root / ".vault-meta"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(text, encoding="utf-8")
    chunk_dir = metadata / "chunks" / address
    chunk_dir.mkdir(parents=True, exist_ok=True)
    chunk = {
        "page_address": address,
        "page_path": page.relative_to(root).as_posix(),
        "chunk_index": 0,
        "raw_text": text,
        "contextualized_text": text,
        "body_hash": digest(text),
        "page_body_hash": digest(text),
    }
    (chunk_dir / "chunk-000.json").write_text(json.dumps(chunk), encoding="utf-8")


def test_project_bm25_index_isolated_from_shared_scope() -> None:
    root = Path(tempfile.mkdtemp())
    try:
        (root / ".vault-meta").mkdir()
        (root / "wiki").mkdir()
        (root / ".raw").mkdir()
        (root / ".vault-meta/projects.json").write_text(
            json.dumps(
                {
                    "schema": "claude-obsidian.projects.v1",
                    "projects": [
                        {"customer_code": "acme", "project_code": "one", "name": "One"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        write_page_and_chunk(root, "acme/one", "p-one", "project", "project-only retrieval token")
        write_page_and_chunk(root, "", "shared", "shared", "shared-only retrieval token")

        project_build = subprocess.run(
            [sys.executable, str(BM25), "--vault", str(root), "--project", "acme/one", "build"],
            cwd=ROOT,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
        assert project_build.returncode == 0, project_build.stderr
        project_index = json.loads(
            (root / ".vault-meta/projects/acme/one/bm25/index.json").read_text(encoding="utf-8")
        )
        assert set(project_index["docs"]) == {"p-one:0"}
        assert all(path.startswith(".vault-meta/projects/acme/one/") for path in (doc["path"] for doc in project_index["docs"].values()))
        assert not (root / ".vault-meta/bm25/index.json").exists()

        shared_build = subprocess.run(
            [sys.executable, str(BM25), "--vault", str(root), "--shared", "build"],
            cwd=ROOT,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
        assert shared_build.returncode == 0, shared_build.stderr
        shared_index = json.loads(
            (root / ".vault-meta/bm25/index.json").read_text(encoding="utf-8")
        )
        assert set(shared_index["docs"]) == {"shared:0"}
    finally:
        shutil.rmtree(root)


if __name__ == "__main__":
    test_project_bm25_index_isolated_from_shared_scope()
    print("All project derived-scope tests passed.")