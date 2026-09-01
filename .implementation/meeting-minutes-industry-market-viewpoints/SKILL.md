---
name: meeting-minutes-industry-market-viewpoints
description: Extract source-grounded market and industry viewpoints from a reviewed or unreviewed Chinese investment meeting note, render one human-editable review Markdown, and export draft or human-reviewed JSON. Use when the pipeline needs market-level, sector-level, or industry-level opinions that must remain separate from security-level structured viewpoints.
---

# Meeting Minutes Industry and Market Viewpoints

Treat the supplied meeting note as the only semantic source. Extract market and
industry viewpoints into one review document. Do not emit individual-security
opinions here; those belong to `meeting-minutes-structured-table`.

`contract/manifest.json` is the only machine contract for this Skill. The
review Markdown is human content only: never write meeting UID, hashes, schema
versions, task IDs, record IDs, or model metadata into it.

## Scope

Include a statement only when it expresses a judgment, expectation, action, or
risk about:

- the overall market, index environment, style, liquidity, valuation, risk
  appetite, or market-wide trading conditions; or
- an industry, sector, board, industrial chain, or cross-company theme.

Exclude pure facts without a judgment, meeting logistics, speaker biography,
and opinions whose only subject is one named security. Do not convert several
security opinions into an invented industry conclusion.

Keep different speakers and incompatible judgments separate. Split a single
statement only when it contains independently reviewable subjects or outcomes.
Do not merge distant source passages merely because they mention the same
industry.

Classify every claim with `view_type` (`看多`, `看空`, or `中性`). Exclude a
pure event watch with no directional market or industry judgment. Split
passages that contain different directions. Normalize the subject to a short,
reusable market or industry label, and compress the viewpoint to one or two
source-faithful sentences suitable for dashboard display. Judge the result on
accuracy and readability: preserve the substantive object, direction, time
scope, and conclusion while making every card independently understandable.
Read `references/schema.md` for the fixed card order and
`references/quality_rules.md` for the semantic generation rules.

## Workflow

1. Generate stable source fragments:

   ```bash
   python3 scripts/generate_viewpoints.py source-fragments \
     --meeting-markdown "YYYY-MM-DD - 会议.md" \
     --output source_fragments.json
   ```

2. Read the whole note, `contract/semantic_prompt.md`, and
   `references/quality_rules.md`. Work in two internal passes. First inventory
   every eligible judgment and preserve its primary object, direction, time
   scope, material conditions, limitations, and source support. Then normalize
   subjects, merge or split related judgments, and render independently
   readable viewpoints. Every draft `source_refs` must support every retained
   claim. Compression may remove verbal filler, secondary background, and
   non-material qualification, but must not materially change the source.

3. Review the complete claim array for omissions, single-object consistency,
   scope alignment, material limitations, event-only statements, unsupported
   additions, unresolved references, context-dependent wording, and
   unnecessary fragmentation. Revise failed claims before rendering artifacts.

4. Generate the human review Markdown and unreviewed JSON together:

   ```bash
   python3 scripts/generate_viewpoints.py generate \
     --meeting-markdown "YYYY-MM-DD - 会议.md" \
     --claim-units claim_units.json \
     --context context.json \
     --review-output review.md \
     --json-output draft.json
   ```

5. After human review, treat the final Markdown as semantic authority. A
   reviewer may correct, shorten, add, or delete viewpoints.
   Export exactly those cards without re-running model interpretation:

   ```bash
   python3 scripts/generate_viewpoints.py export-reviewed \
     --review-markdown review.md \
     --context reviewed-context.json \
     --json-output reviewed.json
   ```

6. Validate an artifact when receiving it across a process boundary:

   ```bash
   python3 scripts/generate_viewpoints.py validate --artifact-json artifact.json
   ```

Read `references/schema.md` for the card and JSON field contract. The service,
not this Skill, owns Feishu credentials, Base fields, Drive folders, task
queues, retries, and publication decisions.
