# Archive Naming Contract

Use this file when archiving raw meeting inputs or exporting confirmed meeting notes. The naming contract is deterministic so local runs and later manual review use the same paths.

This contract applies only when the user has allowed archive/export writes. If the user asks for read-only analysis, says not to archive, says not to modify files, or marks the run as a no-archive test, do not run archive/export writes without explicit confirmation.

## Raw Input Archive

Archive root:

```text
$INVESTMENT_MINUTES_WORKSPACE/00 Inbox/会议原始记录
```

If `INVESTMENT_MINUTES_WORKSPACE` is not set, local scripts default to `Path.home() / "Documents/会议纪要整理"`. Use `--archive-root` for one-off overrides.

Folder pattern:

```text
YYYY-MM-DD/YYYY-MM-DD - 会议标题/
```

Raw file pattern:

```text
YYYY-MM-DD - 会议标题 - 原始NN-材料类型.扩展名
```

Allowed material labels:

- `文稿`
- `录音`
- `录像`
- `附件`

Examples:

```text
2032-06-17/2032-06-17 - AI数据中心液冷产业链交流/
2032-06-17 - AI数据中心液冷产业链交流 - 原始01-文稿.docx
2032-06-17 - AI数据中心液冷产业链交流 - 原始02-录音.mp3
2032-06-17 - AI数据中心液冷产业链交流 - 原始03-附件.pdf
```

Rules:

- Copy raw files; do not move or delete user originals.
- Use the meeting date when known; otherwise use the current date.
- Use `YYYY-MM-DD` only. Reject malformed dates.
- Infer a title from the first non-empty DOCX paragraph or TXT/MD line only when no title is passed.
- Do not keep generic export names such as `export_*.mp3` when a title is known or inferable.
- Replace invalid filename characters `\ / : * ? " < > |` with `-`.
- Collapse repeated whitespace.
- Truncate very long titles before building filenames.
- Preserve all collisions by adding a timestamp or numeric suffix; never overwrite.
- Use `scripts/archive_raw_inputs.py --dry-run --json ...` before irreversible archive writes in automation.

## Final Note Archive

Default final-note root:

```text
$INVESTMENT_MINUTES_WORKSPACE/01 Projects/会议纪要
```

Use `--export-dir` for one-off overrides.

Final-note folder:

```text
YYYY-MM-DD/
```

Final-note filename by meeting type:

```text
多人复盘会：YYYY-MM-DD - 会议系列.md
公司交流：YYYY-MM-DD - 公司名 - 上市公司交流.md
专家交流：YYYY-MM-DD - 主题 - 专家交流.md
```

Use `--meeting-date` when an explicit export date is needed; otherwise use the Markdown `会议日期` field, then the current date as the last fallback.

For `多人复盘会`, fill `会议系列` before export. Deployers may provide a
pipe-separated `MEETING_REVIEW_SERIES` environment variable for filename-based
inference. Use an inferred value only when exactly one configured series
matches; otherwise require explicit user confirmation. The public repository
does not ship an organization-specific series list.

For `公司交流`, derive the confirmed company name from `会议标题` such as `XX公司交流会议`; remove only the exchange suffix and preserve the company name. For `专家交流`, derive the topic from `会议标题` such as `XX主题专家交流`; remove the expert-exchange suffix and preserve the topic. If the company name or topic remains unclear, ask the user before export.

Do not use literal filename placeholders. The `会议标题` field controls the company name or topic only for the matching non-review meeting type. If the filename collides, append a timestamp suffix to the Markdown file.

When non-person business doubts require audit records, a same-stem `.verification.json` or `.verification.jsonl` may be kept as an internal sidecar. It is not a formal deliverable.

Do not expose raw archive paths or technical archive status in the human-readable note body.
