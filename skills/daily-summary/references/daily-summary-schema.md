# Daily summary schema

Daily summaries are project-local history pages, not shared vault hot context.
The canonical path is `<customer>/<project>/wiki/daily/YYYY-MM-DD.md`.

Required frontmatter is flat YAML with `type`, `title`, `status`, `created`,
`updated`, `date`, `project`, and `tags`. Dates use `YYYY-MM-DD`.

Each item uses a stable heading:

```markdown
## Item <stable-id>: <title>

- type: task | change | carry-forward
- status: todo | in-progress | blocked | done | deferred
- evidence: <vault-relative path or path#Lnumber>
- memo: <user-owned progress note>
- next_action: <user-owned next step>
```

The collector may propose task and change items. It may carry forward only
open statuses. It must preserve user-authored `status`, `memo`, and
`next_action` when merging an existing note. It must not infer completion,
rewrite source tasks, or use daily pages as source evidence for a new item.

`daily/index.md` is the active catalog for daily notes. The project `log.md`
records the completed daily-summary operation, while project `hot.md` contains
only a bounded pointer to unresolved items and the latest daily note.