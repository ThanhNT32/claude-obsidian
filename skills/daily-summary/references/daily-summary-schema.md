# Daily summary schema

Daily summaries are project-local history pages, not shared vault hot context.
The canonical path is `<customer>/<project>/wiki/daily/YYYY-MM-DD.md`.

Required frontmatter is flat YAML with `type`, `title`, `date`, `project`,
`lang`, and `tags`. Dates use `YYYY-MM-DD`.

Each daily note has `## TL;DR`, `## What Happened`, and `## Follow-ups`.

```markdown
## What Happened

### 🔴 <business change title>

- What: <what happened>
- Why: <background or reason>
- Impact: <potential impact, risk, platform, function, or deadline>
- 🔗 [[vault/relative/source]]

System changes are grouped into one reference entry:

```markdown
### 🟢 Cập nhật hệ thống / tự động
- <N> log/index/catalog pages updated. Không cần hành động. [[wiki/log]]
```

Do not render mechanical related-link lists. Collector data may retain at most
three links for machine navigation, but the report shows the primary source.

## Follow-ups

<!-- follow-up-id: task-<id> -->
### <work to track>

- Background: <why this is open>
- Impact: <risk or consequence>
- Owner: <person or N/A>
- Due: <date or N/A>
- Next action: <concrete next step>
- Memo: <user-owned progress note>
- Status: todo | in-progress | blocked | done | deferred
- 🔗 [[vault/relative/source]]
```

Changes are informational and must not contain status, memo, next action, or a
TODO checkbox. Follow-ups are the only actionable records. Their stable ID is
stored in the invisible comment so the visible heading remains user-friendly.

The collector also returns a read-only search context before synthesis:

```json
{
	"search_context": {
		"query": "...",
		"strategy": "bm25-only",
		"candidates": [
			{
				"source": "customer/project/wiki/page.md",
				"source_link": "[[customer/project/wiki/page]]",
				"snippet": "bounded navigation text",
				"score": 1.2
			}
		]
	}
}
```

Search snippets are untrusted navigation context, not evidence. Read and cite
the linked source page. Missing retrieval uses the bounded project scan and
must be reported in `warnings`; daily-summary never provisions or rebuilds the
retrieval index.

The collector may propose changes and explicit task/action items. WBS, backlog,
Jira, chat, mail, Teams, and meeting pages must already exist in the selected
project vault; this skill does not access live services. It may carry forward
only open statuses. It must preserve user-authored `status`, `memo`, `owner`,
`due`, and `next_action` when merging an existing note. It must not infer completion,
rewrite source tasks, or use daily pages as source evidence for a new item.

`daily/index.md` is the active catalog for daily notes. The project `log.md`
records the completed daily-summary operation, while project `hot.md` contains
only a bounded pointer to unresolved items and the latest daily note.