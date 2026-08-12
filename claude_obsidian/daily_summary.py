"""Read-only project daily-summary discovery and history parsing."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from .paths import canonical
from .projects import ProjectError, ProjectScope, resolve_project_scope
from .transaction import read_vault_regular


DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MISSING = "N/A"
OPEN_STATUSES = frozenset({"todo", "in-progress", "blocked"})
VALID_STATUSES = frozenset({"todo", "in-progress", "blocked", "done", "deferred"})
TASK_RE = re.compile(r"^(?P<indent>\s*)[-*]\s+\[(?P<mark>[ xX])\]\s+(?P<text>.+?)\s*$")
ACTION_RE = re.compile(
    r"^(?:[-*]\s+)?(?:TODO|ACTION|FOLLOW[- ]?UP|NEXT ACTION)\s*:\s*(?P<text>.+?)\s*$",
    re.I,
)
OWNER_RE = re.compile(
    r"(?:assignee|assigned_to|owner|responsible|for)\s*:\s*(?P<owner>[^,;|]+)",
    re.I,
)
HEADING_RE = re.compile(r"^(?P<marks>#{1,6})\s+(?P<title>.+?)\s*$")
ITEM_HEADING_RE = re.compile(
    r"^##\s+Item\s+(?P<item_id>[a-z0-9_-]+):\s+(?P<title>.+?)\s*$"
)
FOLLOW_UP_HEADING_RE = re.compile(
    r"^#{2,3}\s+(?:Follow[- ]up|Item)\s+(?P<item_id>[a-z0-9_-]+):\s+(?P<title>.+?)\s*$",
    re.I,
)
CHANGE_HEADING_RE = re.compile(
    r"^##\s+Change\s+(?P<item_id>[a-z0-9_-]+):\s+(?P<title>.+?)\s*$",
    re.I,
)
FOLLOW_UP_ID_COMMENT_RE = re.compile(r"^<!--\s*follow-up-id:\s*(?P<item_id>[a-z0-9_-]+)\s*-->\s*$", re.I)
FIELD_RE = re.compile(
    r"^[-*]\s+(?P<key>status|memo|next[_ ]action|evidence|owner|due|background|impact):\s*(?P<value>.*)\s*$",
    re.I,
)
STATUS_TOKEN_RE = re.compile(r"\[(?P<status>todo|in-progress|blocked|done|deferred)\]", re.I)
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
BULLET_RE = re.compile(r"^(?P<indent>\s*)[-*]\s+(?P<text>.+?)\s*$")
MARKDOWN_EMPHASIS_RE = re.compile(r"\*\*(?P<inner>[^*]+)\*\*")

# Headings that explicitly denote open work. Bullets under these are treated as
# follow-up candidates even when they lack `- [ ]` checkboxes or `TODO:` labels.
# Tokens are deliberately conservative: "backlog" is excluded because it appears
# in descriptive ingest titles (e.g. "WBS & Backlog status export ingest") whose
# bullets are factual prose, not tasks.
OPEN_WORK_HEADING_TOKENS = (
    "todo",
    "action item",
    "unresolved",
    "follow-up",
    "followup",
    "next step",
    "next action",
    "pending",
)


def _is_open_work_heading(title: str) -> bool:
    lowered = title.casefold()
    if lowered.strip() == "open" or lowered.startswith("open ") or " open " in f" {lowered} ":
        return True
    return any(token in lowered for token in OPEN_WORK_HEADING_TOKENS)


def _strip_markdown(text: str) -> str:
    return MARKDOWN_EMPHASIS_RE.sub(lambda m: m.group("inner"), text).strip()


def _normalize_title(text: str) -> str:
    """Normalize a task title for deduplication across pages.

    Lowercases, strips trailing punctuation and trailing parenthetical citations
    (e.g. "(ev-001)"), and collapses whitespace so the same open item phrased
    slightly differently in two pages collapses to one.
    """
    lowered = text.casefold().strip()
    lowered = re.sub(r"\s*\([^)]*\)\s*[.!?]*$", "", lowered)
    lowered = re.sub(r"[.!?]+$", "", lowered)
    return re.sub(r"\s+", " ", lowered)


def _status_from_bullet(text: str) -> str:
    lowered = text.casefold()
    if "[blocked]" in lowered or "blocked" in lowered or "blocking" in lowered:
        return "blocked"
    if "in progress" in lowered or "in-progress" in lowered:
        return "in-progress"
    return "todo"

WORK_SOURCE_TERMS = frozenset(
    {"wbs", "backlog", "jira", "chat", "mail", "email", "teams", "meeting", "action-items"}
)
WORK_SOURCE_TYPES = frozenset(
    {"wbs", "backlog", "jira", "chat", "mail", "email", "teams", "meeting"}
)


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
    max_search_results: int = 8
    search_timeout_seconds: int = 10


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


def _source_link(relative: str) -> str:
    return f"[[{relative.removesuffix('.md')}]]"


def _related_links(text: str, source_link: str) -> list[str]:
    links: list[str] = []
    for target in WIKILINK_RE.findall(text):
        link = f"[[{target.strip()}]]"
        if link != source_link and link not in links:
            links.append(link)
    return links[:3]


def _source_kind(relative: str, metadata: dict[str, str]) -> str | None:
    for key in ("source_type", "source", "type"):
        value = metadata.get(key, "").casefold().replace("_", "-").strip()
        if value in WORK_SOURCE_TYPES:
            return value
    values = [relative, *metadata.values()]
    lowered = " ".join(values).casefold().replace("_", "-")
    for source_type in (
        "jira",
        "wbs",
        "backlog",
        "chat",
        "mail",
        "email",
        "teams",
        "meeting",
    ):
        if re.search(rf"(?<![a-z]){re.escape(source_type)}(?![a-z])", lowered):
            return source_type
    for term in ("action-items",):
        if re.search(rf"(?<![a-z]){re.escape(term)}(?![a-z])", lowered):
            return term
    return "project"


def _owner_matches(text: str, owner: str | None) -> bool:
    if owner is None:
        return True
    match = OWNER_RE.search(text)
    return match is not None and owner.casefold() in match.group("owner").casefold()


def _label_value(text: str, labels: tuple[str, ...]) -> str:
    pattern = "|".join(re.escape(label) for label in labels)
    match = re.search(rf"(?im)^\s*(?:[-*]\s*)?(?:{pattern})\s*:\s*(.+?)\s*$", text)
    return match.group(1).strip() if match else ""


def _metadata_value(metadata: dict[str, str], keys: tuple[str, ...]) -> str:
    lowered = {key.casefold().replace("-", "_"): value for key, value in metadata.items()}
    for key in keys:
        if lowered.get(key):
            return lowered[key]
    return ""


def _due_value(text: str, metadata: dict[str, str]) -> str:
    value = _metadata_value(metadata, ("due", "deadline", "due_date"))
    if value:
        return value
    match = re.search(
        r"(?i)\b(?:due|deadline|by)\s*[:：]?\s*(\d{4}-\d{2}-\d{2}|\d{2}[/-]\d{2})\b",
        text,
    )
    return match.group(1) if match else MISSING


def _change_fields(text: str, metadata: dict[str, str], title: str) -> dict[str, str]:
    what = _metadata_value(metadata, ("what", "change", "summary", "description"))
    what = what or _label_value(text, ("what", "changed", "change", "summary"))
    why = _metadata_value(metadata, ("why", "background", "purpose", "reason"))
    why = why or _label_value(text, ("why", "background", "purpose", "reason"))
    impact = _metadata_value(metadata, ("impact", "risk", "deadline"))
    impact = impact or _label_value(text, ("impact", "risk", "deadline"))
    if not what:
        what = _change_summary(text, metadata, title)
    return {
        "what": what,
        "why": why or MISSING,
        "impact": impact or MISSING,
    }


def _change_signal(relative: str, metadata: dict[str, str], text: str) -> tuple[str, str]:
    lowered = f"{relative} {metadata} {text}".casefold()
    name = Path(relative).name.casefold()
    if name in {"index.md", "log.md", "hot.md"} or any(
        token in lowered for token in ("generated", "catalog", "rebuild index", "automatic")
    ):
        return "🟢", "system"
    if any(token in lowered for token in ("risk", "blocker", "scope", "deadline", "requirement", "issue")):
        return "🔴", "business"
    return "🟡", "business"


def _follow_up_context(
    task_text: str,
    heading: str,
    metadata: dict[str, str],
    source_text: str,
) -> dict[str, str]:
    title = metadata.get("title", "")
    background = _metadata_value(metadata, ("background", "context", "description", "purpose"))
    if not background:
        background = _label_value(source_text, ("background", "context", "purpose"))
    if not background and heading and not _is_open_work_heading(heading):
        background = heading
    background = background or MISSING
    impact = _metadata_value(metadata, ("impact", "risk", "deadline"))
    impact = impact or _label_value(source_text, ("impact", "risk", "deadline"))
    impact = impact or MISSING
    owner = _metadata_value(metadata, ("owner", "assignee", "assigned_to", "responsible"))
    if not owner:
        owner_match = OWNER_RE.search(task_text)
        owner = owner_match.group("owner").strip() if owner_match else MISSING
    next_action = _metadata_value(metadata, ("next_action", "next action", "action"))
    next_action = next_action or _label_value(source_text, ("next_action", "next action"))
    action_source = "explicit" if next_action else "inferred"
    if not next_action:
        if task_text.rstrip().endswith(("?", "？")):
            next_action = "Confirm answer"
        else:
            next_action = "Define next step"
    return {
        "background": background,
        "impact": impact,
        "owner": owner,
        "due": _due_value(task_text, metadata),
        "next_action": next_action,
        "next_action_source": action_source,
    }


def _make_follow_up(
    *,
    relative: str,
    heading: str,
    task_text: str,
    status: str,
    line_number: int,
    source_kind: str | None,
    source_text: str,
    metadata: dict[str, str],
) -> dict[str, Any]:
    fingerprint = _fingerprint(relative, heading, task_text)
    source_link = _source_link(relative)
    context = _follow_up_context(task_text, heading, metadata, source_text)
    return {
        "id": f"task-{fingerprint}",
        "type": "task",
        "title": task_text,
        "status": status,
        "evidence": f"{relative}#L{line_number}",
        "source": relative,
        "source_link": source_link,
        "related_links": _related_links(source_text, source_link),
        "source_kind": source_kind,
        **context,
        "memo": "",
        "fingerprint": fingerprint,
    }


def _task_candidates(
    relative: str,
    text: str,
    metadata: dict[str, str],
    *,
    owner: str | None,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    heading = ""
    in_open_work = False
    source_kind = _source_kind(relative, metadata)
    for line_number, line in enumerate(text.splitlines(), start=1):
        heading_match = HEADING_RE.match(line)
        if heading_match:
            heading = heading_match.group("title")
            in_open_work = _is_open_work_heading(heading)
            continue
        match = TASK_RE.match(line)
        if not match:
            continue
        task_text = match.group("text").strip()
        status = _status_from_task(match.group("mark"), task_text)
        if status == "done":
            continue
        if source_kind not in {None, "project"} and owner is not None and not _owner_matches(task_text, owner):
            continue
        candidates.append(
            _make_follow_up(
                relative=relative,
                heading=heading,
                task_text=task_text,
                status=status,
                line_number=line_number,
                source_kind=source_kind,
                source_text=text,
                metadata=metadata,
            )
        )
    if source_kind is not None:
        for line_number, line in enumerate(text.splitlines(), start=1):
            match = ACTION_RE.match(line.strip())
            if match is None:
                continue
            task_text = match.group("text").strip()
            if owner is not None and not _owner_matches(task_text, owner):
                continue
            candidates.append(
                _make_follow_up(
                    relative=relative,
                    heading=heading,
                    task_text=task_text,
                    status=_status_from_task(" ", task_text),
                    line_number=line_number,
                    source_kind=source_kind,
                    source_text=text,
                    metadata=metadata,
                )
            )
    # Plain bullets under an explicit open-work heading are follow-up candidates
    # even without `- [ ]` checkboxes or `TODO:` labels. Track the nearest
    # heading so a bullet is attributed to the section it belongs to.
    current_heading = ""
    in_open_work = False
    for line_number, line in enumerate(text.splitlines(), start=1):
        heading_match = HEADING_RE.match(line)
        if heading_match:
            current_heading = heading_match.group("title")
            in_open_work = _is_open_work_heading(current_heading)
            continue
        if not in_open_work:
            continue
        bullet = BULLET_RE.match(line)
        if bullet is None:
            continue
        task_text = _strip_markdown(bullet.group("text"))
        if not task_text or task_text.startswith("["):
            continue
        status = _status_from_bullet(task_text)
        if status == "done":
            continue
        if source_kind not in {None, "project"} and owner is not None and not _owner_matches(task_text, owner):
            continue
        candidates.append(
            _make_follow_up(
                relative=relative,
                heading=current_heading,
                task_text=task_text,
                status=status,
                line_number=line_number,
                source_kind=source_kind,
                source_text=text,
                metadata=metadata,
            )
        )
    return candidates


def _change_summary(text: str, metadata: dict[str, str], title: str) -> str:
    for key in ("summary", "description", "abstract"):
        if metadata.get(key):
            return metadata[key]
    marker = text.find("\n---\n", 4) if text.startswith("---\n") else -1
    body = text[marker + 5 :] if marker >= 0 else text
    for line in body.splitlines():
        candidate = line.strip()
        if candidate and not candidate.startswith("#") and not TASK_RE.match(candidate):
            return candidate[:240]
    return title


def _updated_candidate(
    relative: str,
    frontmatter: dict[str, str],
    text: str,
    summary_date: str,
) -> dict[str, Any] | None:
    if frontmatter.get("updated") != summary_date or relative in {"index.md", "hot.md"}:
        return None
    title = frontmatter.get("title") or relative.removesuffix(".md")
    fingerprint = _fingerprint(relative, summary_date, title)
    source_link = _source_link(relative)
    signal, category = _change_signal(relative, frontmatter, text)
    fields = _change_fields(text, frontmatter, title)
    return {
        "id": f"change-{fingerprint}",
        "type": "change",
        "title": f"Updated: {title}",
        "evidence": relative,
        "source": relative,
        "source_link": source_link,
        "related_links": _related_links(text, source_link),
        "source_kind": _source_kind(relative, frontmatter),
        "summary": fields["what"],
        **fields,
        "signal": signal,
        "category": category,
        "fingerprint": fingerprint,
    }


def _default_search_query(scope: ProjectScope, summary_date: str, owner: str | None) -> str:
    owner_term = f" assigned to {owner}" if owner else ""
    return (
        f"{scope.identifier} changes and outstanding follow-up tasks on {summary_date}; "
        f"WBS backlog Jira chat mail Teams meeting action items{owner_term}"
    )


def _wiki_query_search(
    vault_root: Path,
    scope: ProjectScope,
    query: str,
    budget: CollectionBudget,
) -> tuple[dict[str, Any], list[str]]:
    """Run wiki-query's read-only retrieval stage before daily synthesis."""

    retrieve_script = Path(__file__).resolve().parents[1] / "scripts" / "retrieve.py"
    if not retrieve_script.is_file():
        return {"query": query, "strategy": "fallback-project-scan", "candidates": []}, [
            "wiki-retrieve unavailable: retrieve.py is missing; used bounded project scan"
        ]
    command = [
        sys.executable,
        str(retrieve_script),
        "--vault",
        str(vault_root),
        "--project",
        scope.identifier,
        "--top",
        str(budget.max_search_results),
        "--no-rerank",
        "--explain",
        query,
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=budget.search_timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"query": query, "strategy": "fallback-project-scan", "candidates": []}, [
            "wiki-retrieve unavailable: read-only retrieval failed; used bounded project scan"
        ]
    if completed.returncode != 0:
        reason = "index unavailable" if completed.returncode == 10 else "query failed"
        return {"query": query, "strategy": "fallback-project-scan", "candidates": []}, [
            f"wiki-retrieve unavailable: {reason}; used bounded project scan"
        ]
    try:
        result = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError):
        return {"query": query, "strategy": "fallback-project-scan", "candidates": []}, [
            "wiki-retrieve unavailable: invalid read-only result; used bounded project scan"
        ]
    if not isinstance(result, dict) or not isinstance(result.get("candidates"), list):
        return {"query": query, "strategy": "fallback-project-scan", "candidates": []}, [
            "wiki-retrieve unavailable: malformed read-only result; used bounded project scan"
        ]
    safe_candidates: list[dict[str, Any]] = []
    project_root = scope.wiki_root.resolve()
    for candidate in result["candidates"][: budget.max_search_results]:
        if not isinstance(candidate, dict):
            continue
        page_path = candidate.get("page_path")
        if not isinstance(page_path, str) or not page_path:
            continue
        absolute = (vault_root / page_path).resolve()
        if not absolute.is_file() or not absolute.is_relative_to(project_root):
            continue
        relative = _vault_relative(vault_root, absolute)
        safe_candidates.append(
            {
                "source": relative,
                "source_link": _source_link(relative),
                "snippet": str(candidate.get("snippet", ""))[:500],
                "score": candidate.get("rerank_score", candidate.get("bm25_score", 0)),
            }
        )
    return {
        "query": query,
        "strategy": result.get("strategy", "bm25-only"),
        "candidates": safe_candidates,
        "explain": result.get("explain", {}),
    }, []


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
        pending_id: str | None = None
        for line in text.splitlines():
            heading = FOLLOW_UP_HEADING_RE.match(line)
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
            id_comment = FOLLOW_UP_ID_COMMENT_RE.match(line)
            if id_comment:
                pending_id = id_comment.group("item_id")
                continue
            if pending_id and line.startswith("### "):
                if current is not None:
                    latest[current["id"]] = current
                current = {
                    "id": pending_id,
                    "title": line[4:].strip(),
                    "date": note_date,
                    "status": "todo",
                    "memo": "",
                    "next_action": "",
                    "evidence": "",
                }
                pending_id = None
                continue
            if current is None:
                continue
            field = FIELD_RE.match(line)
            if field:
                key = field.group("key").casefold().replace(" ", "_")
                current[key] = field.group("value")
                continue
            links = WIKILINK_RE.findall(line)
            if links and not current.get("evidence"):
                current["evidence"] = f"[[{links[0]}]]"
        if current is not None:
            latest[current["id"]] = current
    return latest, previous_notes


def collect_daily_summary(
    vault_root: Path | str,
    *,
    project: str | None,
    summary_date: str | date,
    owner: str | None = None,
    search_query: str | None = None,
    language: str = "vi",
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
    query = search_query or _default_search_query(scope, selected_date, owner)
    search_context, warnings = _wiki_query_search(root, scope, query, limits)
    files = _safe_markdown_files(scope.wiki_root)
    if len(files) > limits.max_files:
        raise DailySummaryError("FILE_BUDGET_EXCEEDED", "project wiki file budget exceeded")
    total_bytes = 0
    changes: list[dict[str, Any]] = []
    follow_ups: list[dict[str, Any]] = []
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
        follow_ups.extend(_task_candidates(relative, text, metadata, owner=owner))
        changed = _updated_candidate(relative, metadata, text, selected_date)
        if changed is not None:
            changes.append(changed)
    history, previous_notes = _history_items(root, scope, selected_date, limits)
    carry_forward: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in follow_ups:
        previous = history.get(item["id"])
        if previous is not None:
            item = {
                **item,
                **{
                    key: previous[key]
                    for key in ("status", "memo", "owner", "due", "background", "impact", "next_action")
                    if previous.get(key)
                },
            }
        if item["id"] in seen:
            continue
        seen.add(item["id"])
        if item.get("status") in OPEN_STATUSES and item["id"] in history:
            carry_forward.append(item)
    for item_id, previous in history.items():
        if item_id not in seen and previous.get("status") in OPEN_STATUSES:
            evidence = previous.get("evidence", "")
            source = evidence.removeprefix("[[").removesuffix("]]" )
            carry_forward.append({
                "id": item_id,
                "type": "carry-forward",
                "title": previous.get("title", item_id),
                "status": previous.get("status", "todo"),
                "memo": previous.get("memo", ""),
                "background": previous.get("background", MISSING),
                "impact": previous.get("impact", MISSING),
                "owner": previous.get("owner", MISSING),
                "due": previous.get("due", MISSING),
                "next_action": previous.get("next_action", ""),
                "evidence": evidence,
                "source": source,
                "source_link": evidence if evidence.startswith("[[") else _source_link(source) if source else "",
                "related_links": [],
                "fingerprint": item_id.removeprefix("task-").removeprefix("change-"),
            })
    unique_changes: dict[str, dict[str, Any]] = {item["id"]: item for item in changes}
    # Collapse near-duplicate follow-ups (same normalized title) that arise when
    # the same open item appears in more than one page. Keep the first source's
    # evidence; the fingerprint/id of the first occurrence is retained.
    unique_follow_ups: dict[str, dict[str, Any]] = {}
    for item in follow_ups:
        norm_key = _normalize_title(item["title"])
        if norm_key in unique_follow_ups:
            continue
        unique_follow_ups[norm_key] = item
    return {
        "schema": "claude-obsidian.daily-summary.v2",
        "project": scope.identifier,
        "date": selected_date,
        "owner": owner,
        "lang": language,
        "read_only": True,
        "changes": list(unique_changes.values()),
        "follow_ups": list(unique_follow_ups.values()),
        "carry_forward": carry_forward,
        "previous_notes": previous_notes,
        "search_context": search_context,
        "warnings": warnings,
    }


def _display_title(title: str) -> str:
    return re.sub(r"\s*[|｜]\s*(?:assignee|assigned_to|owner)\s*:\s*.+$", "", title, flags=re.I).strip()


def render_daily_summary(summary: dict[str, Any], *, language: str | None = None) -> str:
    """Render a decision-ready daily note from a collected summary."""

    lang = language or str(summary.get("lang", "vi"))
    if lang != "vi":
        raise DailySummaryError("LANGUAGE_UNSUPPORTED", "only Vietnamese rendering is supported")
    summary_date = str(summary.get("date", ""))
    project = str(summary.get("project", ""))
    changes = [item for item in summary.get("changes", []) if isinstance(item, dict)]
    follow_ups = [item for item in summary.get("follow_ups", []) if isinstance(item, dict)]
    carry_forward = [item for item in summary.get("carry_forward", []) if isinstance(item, dict)]
    all_follow_ups: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in [*carry_forward, *follow_ups]:
        item_id = str(item.get("id", ""))
        if item_id and item_id not in seen:
            seen.add(item_id)
            all_follow_ups.append(item)

    business_changes = [item for item in changes if item.get("category") != "system"]
    system_changes = [item for item in changes if item.get("category") == "system"]
    lines = [
        "---",
        "type: daily-summary",
        f"title: Daily Summary {summary_date}",
        f"date: {summary_date}",
        f"project: {project}",
        "lang: vi",
        "tags: [daily-summary]",
        "---",
        "",
        f"# Daily Summary — {summary_date} ({project})",
        "",
        "## TL;DR",
    ]
    if business_changes:
        for item in business_changes[:2]:
            lines.append(f"- {item.get('signal', '🟡')} {_display_title(str(item.get('title', '')))}: {item.get('what', '')}")
    if all_follow_ups:
        item = all_follow_ups[0]
        lines.append(f"- Follow-up: {_display_title(str(item.get('title', '')))} — {item.get('next_action', '')}")
    if not business_changes and not all_follow_ups:
        lines.append(f"- {MISSING}")
    lines.extend(
        [
            "",
            "## What Happened",
            "_Việc đã xảy ra hôm nay. Section này do hệ thống sinh, không sửa tay._",
            "",
        ]
    )
    if business_changes:
        for item in business_changes:
            lines.extend(
                [
                    f"### {item.get('signal', '🟡')} {_display_title(str(item.get('title', '')))}",
                    f"- What: {item.get('what', '')}",
                    f"- Why: {item.get('why', '')}",
                    f"- Impact: {item.get('impact', '')}",
                    f"- 🔗 {item.get('source_link', '')}",
                    "",
                ]
            )
    if system_changes:
        links = [str(item.get("source_link", "")) for item in system_changes if item.get("source_link")]
        sources = ", ".join(links[:3])
        lines.extend(
            [
                "### 🟢 Cập nhật hệ thống / tự động",
                f"- {len(system_changes)} system pages updated. Không cần hành động. {sources}".rstrip(),
                "",
            ]
        )
    if not business_changes and not system_changes:
        lines.extend([f"- {MISSING}", ""])
    lines.extend(
        [
            "## Follow-ups",
            "_Việc cần theo dõi tiếp. User edit trực tiếp: thêm/sửa các field bên dưới._",
            "",
        ]
    )
    if all_follow_ups:
        for item in all_follow_ups:
            lines.extend(
                [
                    f"<!-- follow-up-id: {item.get('id', '')} -->",
                    f"### {_display_title(str(item.get('title', '')))}",
                    f"- Background: {item.get('background', MISSING)}",
                    f"- Impact: {item.get('impact', MISSING)}",
                    f"- Owner: {item.get('owner', MISSING)}",
                    f"- Due: {item.get('due', MISSING)}",
                    f"- Next action: {item.get('next_action', MISSING)}",
                    f"- Memo: {item.get('memo', '')}",
                    f"- Status: {item.get('status', 'todo')}",
                    f"- 🔗 {item.get('source_link', '')}",
                    "",
                ]
            )
    else:
        lines.extend([f"- {MISSING}", ""])
    return "\n".join(lines).rstrip() + "\n"