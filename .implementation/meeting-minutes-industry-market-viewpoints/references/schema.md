# Industry and Market Viewpoint Schema v1

The source meeting note is authoritative during draft generation. Human-reviewed
Markdown is authoritative at reviewed export.

## Review Markdown

One document contains `市场观点` and `行业观点` sections. Every `### 观点`
card contains a two-column table with exactly five editable business fields,
in the following fixed order:

| Field | Meaning |
| --- | --- |
| `日期` | Meeting date in `YYYY-MM-DD`, copied from the meeting context. |
| `主题` | The smallest complete, stable, and reusable market or industry subject that matches the viewpoint object. |
| `发言人` | Original speaker label. |
| `观点类型` | `看多`, `看空`, or `中性`, classified from the expected outcome rather than modal strength. |
| `观点` | One or two concise, source-faithful, independently readable sentences suitable for dashboard display. |

Use `看多` for an explicit positive investment direction, `看空` for an
explicit negative direction, and `中性` for balanced, expectation-matching, or
directionally unresolved judgments. Exclude a pure event watch with no market
or industry judgment. Probability and uncertainty normally affect wording, not
direction classification. Split a source passage when it contains independently
reviewable directions or materially different time scopes. Keep `观点` faithful
to the selected source fragments, but remove verbal filler, secondary
background, and non-material qualification. Do not change the substantive
object, direction, fact status, time scope, or conclusion.

Identify the substantive object before naming `主题`. Prefer a conventional,
stable, reusable market concept, industry, sector, or industrial-chain label.
Remove event, time, direction, conclusion, catalyst, and trading-action
language. Reuse the same subject for the same substantive concept, and create a
new subject only when existing labels would cause material semantic loss. Do
not force distinct objects into one broad label. Prefer a concise label,
normally 2-10 Chinese characters when practical.

Write every `观点` as a complete standalone statement. Resolve references that
depend on omitted context, remove context-dependent transitions, lead with the
core judgment, and retain only the most useful reason, time boundary, or
limitation needed for understanding. Merge adjacent judgments from the same
speaker when subject and direction match and they form one rationale; keep
materially different objects, directions, time scopes, speakers, or reasoning
separate. Read `quality_rules.md` before drafting or reviewing cards.

The Markdown never contains UID, hashes, schema version, viewpoint ID, record
ID, task ID, or model metadata. Card numbers are display-only.

## JSON item

| Field | Rule |
| --- | --- |
| `viewpoint_id` | Deterministic opaque ID derived from meeting UID and approved card identity. |
| `meeting_date` | Card date copied from the meeting context. |
| `view_scope` | `market` or `industry`. |
| `subject` | Stable, reusable theme that matches the substantive viewpoint object. |
| `presenter` | Original speaker text. |
| `view_type` | `看多`, `看空`, or `中性`, based on expected direction. |
| `viewpoint_text` | Concise, source-faithful, independently readable text approved in the review Markdown. |

`view_scope` and `viewpoint_id` remain machine fields. `meeting_series` remains
artifact metadata for dashboard filtering. Stable `source_refs` are required
only in draft claim units and are not exported to the review Markdown or final
artifact items.

JSON metadata follows meeting pipeline artifact metadata schema v1. Draft JSON
uses `quality_status=unreviewed`; reviewed export requires
`quality_status=reviewed` and `artifact_review_status=已审核`.
