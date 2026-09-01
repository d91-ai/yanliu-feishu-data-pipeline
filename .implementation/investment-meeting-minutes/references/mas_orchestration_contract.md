# MAS 编排契约

本文件定义投资会议纪要 workflow 的 MAS 目标模式。MAS 的目的不是让多个 agent 拼接终稿，而是提高端到端自动化效率、减少人工常规参与、提升最终纪要质量和可复核性。

## 目标

- 用主流程统一调度会议纪要生产线。
- 用 specialist agents 自动处理高风险子任务和过程复核。
- 用结构化 artifacts 承载中间判断、证据路径、风险和处理结果。
- 让人工只介入证据冲突、主源不确定、高风险事实无法确认或用户业务偏好不明确的异常。
- 保持最终 Markdown 只由主流程生成或修改。

## 不变边界

- 主流程是最终 Markdown、归档输出和交付口径的唯一写作者。
- Specialist agents 不直接写、拼接、改写或导出最终纪要。
- 外部资料只能确认名称、代码、术语、公开事实或候选解释，不能补写会议材料没有出现的观点。
- `doubtful_items` 仍是终稿存疑表和 verification sidecar 的唯一事实源。
- Validator 保持结构、编码、样例和 artifact 字段检查，不新增看好/看空、主次标的等语义硬校验。
- 不引入 LangGraph、CrewAI、AutoGen 或其他重型 agent 框架。

## 角色

### Main Orchestrator

职责：
- 判断 run profile 和 MAS 触发条件。
- 分派 specialist agents。
- 汇总 artifacts、裁决冲突、执行低风险自动修正。
- 生成和修改最终 Markdown。
- 运行导出、validator、回归和最终交付检查。

禁止：
- 跳过转录、校对、识别、联网核验、编辑、排版或验证步骤而不记录原因。
- 把 specialist agent 的自由文本建议直接粘贴进终稿。

### Transcript Auditor

输入：
- 原始音频元信息、SenseVoice 转写、Paraformer 辅助差异、timestamp_index。

输出：
- `transcript_audit`

检查：
- ASR 噪声、长段落异常、说话人边界、SenseVoice/Paraformer 冲突、timestamp anchor 可靠性。

### Source Reconciler

输入：
- 原始音频转写、`aligned_transcript`、用户文稿、人工初审稿、同会话补充说明。

输出：
- `source_reconciliation`

检查：
- 覆盖完整度、发言顺序、逐字性、可靠时间戳、ASR 噪声、遗漏、人工校正痕迹和来源冲突。

### Entity Verifier

输入：
- 当前会议上下文、实体候选、本地代码候选、外部核验路径。

输出：
- `entity_verification_report`
- `doubtful_items` 更新建议

检查：
- 公司、股票代码、客户、供应商、竞争对手、数字、时间、政策事件、产品型号和行业术语。

### Target Attribution Reviewer

输入：
- 多人复盘会正文草案、来源片段、实体核验状态。

输出：
- `target_attribution_review`

检查：
- 板块行、标的行、看好/看空、客户/供应商/竞争对手/上下游误入标的行、顺带提及对象、多个标的是否共享同一逻辑链。

### Fidelity Reviewer

输入：
- 终稿草案和对应 source spans。
- `audio_plus_document` 额外要求已完成的 `source_reconciliation`；单一来源改用主流程已选定的正文源及选择理由，不伪造 reconciliation artifact。

输出：
- `fidelity_review`

检查：
- 总结化、第三人称改写、删减原因链/数字/时间/仓位动作/不确定表达、合并多轮发言、改变发言顺序。

### Contract Verifier

输入：
- 最终 Markdown、verification sidecar、timestamp_index、export_manifest。

输出：
- `export_manifest`

检查：
- UTF-8、Markdown 合约、存疑表、timestamp_index、verification sidecar、回归样例和导出结果。

## Artifact Schema

Every specialist return uses a dispatch-bound envelope with:
- `run_id`: current dispatch run only.
- `task_id`: exact generated specialist task.
- `dispatch_phase`: task phase from the dispatch manifest.
- `artifact_owner`: generated role name.
- `artifact_type` + `artifact`, or `artifacts` for the exact primary/secondary artifact set assigned to that task.

The ingest and collector layers must reject stale-run, cross-task, cross-phase, cross-owner, unexpected, or incomplete task returns. `task_artifact_set` and `ingested_split` are reserved fields and must never appear in a returned or collected artifact; the collector derives the allowed primary/secondary set from the bound dispatch manifest.

### source_manifest

Required fields:
- `source_mode`
- `materials`
- `archive_allowed`
- `archive_status`
- `skipped_reason`

`materials` must be a non-empty array for an active MAS run and must match the current bound task bundle by normalized material `kind` and basename. Known audio, document, PDF, JSON metadata, and timestamp-index filenames derive their canonical kind from the filename; an explicit mismatched `kind` cannot relabel a PDF, audio file, or metadata file as a body document. `source_mode` must match the bundle and its material-kind coverage (`audio_only`, `document_only`, or both for `audio_plus_document`). `archive_status` is one of `not_started`, `completed`, `skipped`, `skipped_for_fixture`, or `failed`; `archive_allowed=false` cannot be paired with `archive_status=completed`.

### transcript_audit

Required fields:
- `asr_primary`
- `asr_auxiliary`
- `quality_flags`
- `speaker_boundary_findings`
- `timestamp_index_status`
- `conflicts`
- `recommended_action`

### source_reconciliation

Required fields:
- `primary_body_source`
- `primary_source_reason`
- `cross_check_source`
- `coverage_findings`
- `speaker_order_findings`
- `omission_findings`
- `conflicts`
- `manual_review_required`

An automatically selected `primary_body_source` must be an eligible current-session body material name/stem or an allowed source alias such as `aligned_transcript`, `audio_transcript`, or `provided_document`; metadata, timestamp indexes, and `pdf_attachment` files are not body sources. An external URL, `file://` URI, or absolute local path is invalid. For `audio_plus_document`, automatic continuation also requires a non-empty `cross_check_source` bound to the other explicit evidence side; an empty, external, unbound, ambiguous, or same-side cross-check does not pass.

### entity_verification_report

Required fields:
- `items`
- `local_candidate_paths`
- `external_evidence_paths`
- `confirmed_item_evidence_paths`
- `confirmed_items`
- `unresolved_items`
- `conflicts`

`confirmed_item_evidence_paths` must be a per-confirmed-item mapping. A confirmed non-person business item is not sufficient merely because `external_evidence_paths` is non-empty; each string in `confirmed_items` must map to at least one external evidence path or source identifier. Each external reference must be a public `https://` URL or one of the supported public source IDs: `a_stock_data_live`, `cninfo`, `company_website`, `exchange_disclosure`, `professional_database`, or `regulatory_disclosure`. HTTP, localhost/private-network addresses, credential-bearing query parameters, local candidate file paths, and arbitrary opaque strings are invalid. This shape check does not claim that network retrieval occurred; live verification remains a main-workflow evidence requirement.

### doubtful_items

Use the fields and type enum in `verification_policy.md`. This list remains the only source for final ambiguity-table rows and verification sidecar records. Every entity `unresolved_items` entry and every export `known_unverified_parts` entry must have the same exact `原始表述` in `doubtful_items`. The sidecar record set must exactly match business doubtful items whose `是否需要 sidecar=true`.

### target_attribution_review

Required fields:
- `segments_reviewed`
- `wrong_grouping`
- `missing_positive_targets`
- `incidental_targets_in_heading`
- `negative_targets_in_heading`
- `non_source_companies`
- `recommended_revisions`

`segments_reviewed` must be a positive integer; a zero-scope review does not pass.

### fidelity_review

Required fields:
- `paragraphs_reviewed`
- `source_mapping_failures`
- `summary_compression_findings`
- `pronoun_rewrite_findings`
- `omission_findings`
- `recommended_revisions`

`paragraphs_reviewed` must be a positive integer; a zero-scope review does not pass.

### export_manifest

Required fields:
- `markdown_path`
- `markdown_sha256`
- `verification_sidecar_path`
- `validators_run`
- `regression_result`
- `export_status`
- `known_unverified_parts`
- `main_actions_verified`

`validators_run` must contain exactly the supported structural validators, `validate_utf8_text.py` and `validate_meeting_minutes_contract.py`, each with boolean `ok`. `regression_result` must contain `name=run_meeting_minutes_regression.py`, a positive integer `case_count`, and boolean `ok`; `export_status` must be `passed`, `failed`, or `blocked`. The collector resolves `markdown_path`, recomputes its SHA-256, and rejects a missing, stale, or mismatched final Markdown. When `known_unverified_parts` is non-empty, `verification_sidecar_path` must point to an existing, parseable, non-empty sidecar that passes the shared sidecar validator and matches `doubtful_items`.

### main_action_receipt

Main-owned optional process artifact, required whenever draft-review or doubtful handling changes the Markdown before final verification.

Required fields:
- `run_id`
- `actions`
- `status=applied`
- `markdown_path`
- `markdown_sha256`
- `source_artifact_digest`

The receipt is valid only for the same run, current pre-final source artifacts, listed main actions, and exact Markdown bytes. It is a main-workflow record, not independent proof that each listed edit was semantically applied; the final writer still owns content review. Any later source-artifact or Markdown change invalidates the receipt and any existing `export_manifest`.

## MAS Trigger Rules

Use MAS when any risk is present:
- Long audio, noisy audio, unclear speaker boundaries, or timestamp alignment risk.
- `audio_plus_document` with source conflict or unclear primary body source.
- Multiple targets, sectors, positive/negative views, customers, suppliers, competitors, or upstream/downstream entities mixed in one meeting.
- Numerous non-person business doubtful items.
- High-risk public facts such as company codes, customers, orders, capacity, revenue, profit, valuation, policies, events, dates, or models; or source-fidelity risk around speaker investment actions. Public facts require external evidence, while buy/sell/add/reduce/tracking actions are verified against the current-session source span unless the action embeds a public entity, ticker, or event.
- Prior user feedback indicates summary compression, third-person rewrite, omission, missed verification, or target-attribution drift.

Do not trigger MAS by default for short, clean `fast_document` work unless one of the above risks appears.

For mixed audio+document work, source selection is considered unresolved until the main workflow has compared the audio-derived transcript, `aligned_transcript`, and provided document. The task request should set `source_selection_status` to one of `not_compared`, `compared_clear`, `conflict`, or `uncertain`; omitted `audio_plus_document` status, or an accidental `not_applicable`, is treated as `not_compared`. If the primary body source is already clear and no other risk exists, set `source_selection_status=compared_clear` and keep the source-quality note in the main workflow instead of dispatching every MAS specialist. If MAS is used, dispatch only the phase that is ready rather than spawning all specialists at once.

## Task Bundle

Before dispatching specialist agents, use `scripts/build_mas_task_bundle.py` to generate a deterministic task bundle from `run_profile`, `source_mode`, `meeting_type`, risk flags, and current-session materials.

Accepted `risk_flags` are explicit and unknown tokens fail fast:
- Audio: `audio_input`, `long_audio`, `noisy_audio`, `unclear_speaker_boundaries`, `timestamp_alignment`, `strict_audio`.
- Source reconciliation: `audio_plus_document`, `source_conflict`, `primary_source_uncertain`.
- Entity/public fact: `entity_verification`, `high_risk_facts`, `many_doubtful_items`, `company_codes`, `customers_suppliers`, `numbers_dates`.
- Target attribution: `target_attribution`, `multi_target`, `mixed_targets`, `positive_negative_views`.
- Fidelity: `fidelity_review`, `omission_risk`, `summary_compression`, `third_person_rewrite`, `prior_user_feedback`.

Artifact selection is incremental rather than all-specialist by default. Every active MAS run keeps main-owned `source_manifest` plus final `export_manifest`; audio risks add `transcript_audit`, source-selection risks add `source_reconciliation` and `fidelity_review`, entity/public-fact risks add `entity_verification_report` plus `doubtful_items`, target risks add `target_attribution_review`, and fidelity risks add `fidelity_review`. `audio_only` must use `strict_audio`, which may reach the full set through its inferred risks. A flags-only CLI call may inspect a plan without materials; `--task-dir`, `--request-json`, or explicit `--material` activates source-coverage validation. Before any prompt files are written, the bundle builder must fail fast when `audio_only` lacks audio, `document_only` lacks a readable body document, or `audio_plus_document` lacks either evidence side; a PDF attachment alone is not a body document.

The task bundle must define:
- Whether MAS is required for the current run.
- Expected artifacts for the selected risk profile.
- Artifact owners. `source_manifest` is created by the Main Orchestrator. `doubtful_items` may be proposed by Entity Verifier, but final handling is decided by the Main Orchestrator.
- Specialist roles, inputs, checks, required fields, JSON-only prompt, and forbidden final-output fields.
- Main-orchestrator-only responsibilities: final Markdown writing, archive/export side effects, delivery wording, and user-facing conflict decisions.
- The artifact validator command and required artifacts for later `scripts/validate_mas_artifacts.py` checks.
- A fresh dispatch `run_id` plus one `task_id` per specialist task when prompt files are materialized.

Bundle validation must enforce profile/source/meeting enums, `audio_only => strict_audio`, source-selection status, exact role task contracts, and a closed artifact-producer set covering every expected primary or secondary artifact. A non-overwrite dispatch write must recheck the target directory under the task lock and refuse any existing bundle, manifest, or generated prompt.

The task bundle is a dispatch plan, not a runtime framework. It may be used with Codex subagents when available, or as a manual task checklist when subagent execution is not available. It must not create, modify, assemble, or export final Markdown.

## Codex Subagent Dispatch Protocol

When Codex subagents are available, the Main Orchestrator may run:

```bash
MAS_DISPATCH="$(mktemp -d /tmp/mas-dispatch.XXXXXX)"
python3 scripts/build_mas_task_bundle.py --request-json REQUEST.json --task-dir "$MAS_DISPATCH"
```

Use a fresh dispatch directory for each meeting or pilot run. Do not reuse a prior dispatch directory with old `artifacts/` unless the collector has explicitly told you to continue that same run. Use one generated `*.prompt.md` file per specialist subagent. Each subagent should receive only its assigned prompt plus the minimum current-session source materials needed for that role. Do not pass the expected answer, prior diagnosis, or final Markdown draft unless that draft is explicitly required by the role.

Generated task files include a `dispatch_phase`:
- `pre_draft`: run after current-session source materials are prepared and before final-note drafting. Typical tasks: transcript audit, source reconciliation, entity verification.
- `draft_review`: run only after the main workflow has a draft and role-relevant source spans. Typical tasks: target attribution and fidelity review.
- `final_verification`: run only after final Markdown, sidecars, export logs, and validators exist. Typical task: contract/export verification.

Subagent execution rules:
- Spawn one process-only specialist per generated prompt file only when that task's `dispatch_phase` is ready.
- Tasks in the same phase may run in parallel; tasks across phases must wait for their prerequisites.
- Keep subagents read-only toward repository files and meeting-note files unless a future task explicitly assigns a private artifact output path.
- Require each subagent to return only the JSON artifact shape requested in its prompt.
- Generated prompts must render the role-specific inputs and checks, use type-correct JSON examples, and state that private recordings, transcripts, meeting excerpts, and local paths must not be uploaded to external services.
- Save main-owned artifacts such as `source_manifest` under the dispatch directory's `artifacts/` folder. Use `scripts/create_mas_source_manifest.py` or `scripts/run_mas_phase_operator.py --auto-source-manifest` to create the initial `source_manifest` from the bound bundle without claiming archive completion. When `--task-dir` is present, its locked bundle and dispatch manifest are the only authority for `run_id`, source mode, and materials; request arguments cannot override them. `source_manifest` is always `pre_draft`; `main_action_receipt` is always `draft_review`.
- For each returned specialist JSON, run `scripts/ingest_mas_artifact.py RETURNED.json --task-dir "$MAS_DISPATCH" --through-phase PHASE --json`. Dispatch writes and ingest commits use an exclusive task-dir lock; collection uses a shared lock. The ingest script commits a task's primary/secondary artifacts and replacement-history records as one recoverable transaction, rolls back ordinary failures, and automatically recovers an interrupted uncommitted transaction before the next ingest. Invalid or duplicate returns go to `repair_history/`.
- Require returned `run_id`, `task_id`, `dispatch_phase`, `artifact_owner`, and artifact set to match the generated prompt and dispatch manifest. Do not ingest an artifact copied from another meeting or task even when its inner schema is valid.
- Do not manually overwrite an existing artifact file. If a specialist return is invalid or duplicate, repair or re-dispatch from the `repair_history/` record before continuing.
- When a corrected same-run/task return must replace an existing artifact, use `ingest_mas_artifact.py --replace-existing`; the old artifact must be preserved as `superseded` in `repair_history/` before replacement.
- Run `scripts/collect_mas_artifacts.py "$MAS_DISPATCH" --out "$MAS_DISPATCH/mas_run_summary.json" --combined-out "$MAS_DISPATCH/mas_artifacts_collected.json"` to merge artifacts, detect duplicates, check required artifacts from the bundle, validate field structure, produce the decision summary, and emit phase gates plus the next main-workflow action. A pending ingest transaction blocks collection until ingest recovery completes. Consume combined artifacts only when the collector summary has top-level `ok: true`; failed combined outputs are partial diagnostics.
- Run `scripts/plan_mas_next_action.py --summary-json "$MAS_DISPATCH/mas_run_summary.json" --json` to turn `next_action` into the next executable checklist: prompt files to dispatch, ingest commands to run after returns, main-owned artifact gaps, repair actions, narrow user-confirmation actions, or final `main_action_checklist`.
- Prefer `scripts/run_mas_phase_operator.py` when operating a live dispatch directory repeatedly. It initializes dispatch files from a request JSON when needed, ingests returned artifact JSON files, and publishes the collector summary, combined artifacts, next-action plan, and `mas_operator_state.json` as one locked artifact generation.
- For a partial phase gate, pass `--through-phase pre_draft`, `--through-phase draft_review`, or `--through-phase final_verification` so the collector only requires artifacts whose phase is ready.
- Gate on collector top-level `ok`. Treat the embedded `decision` as actionable only when collector output is `ok: true`; otherwise repair/regenerate invalid, duplicate, or missing artifacts before final delivery.
- Apply final writing, doubtful marking, export, and user-facing decisions only in the Main Orchestrator.
- If draft-review or doubtful actions can change Markdown, apply them before `final_verification`, run `record_mas_main_actions.py` against that Markdown, then rerun collector. Do not dispatch Contract Verifier until collector accepts the receipt.

## Codex Operator Harness

Use `scripts/run_mas_phase_operator.py` to reduce manual command stitching during staged MAS execution. It is an operator harness, not a subagent runtime and not a final-note writer.

Initialize or inspect a dispatch directory:

```bash
python3 scripts/run_mas_phase_operator.py \
  --request-json REQUEST.json \
  --task-dir "$MAS_DISPATCH" \
  --through-phase pre_draft \
  --auto-source-manifest \
  --json
```

After one or more specialist returns:

```bash
python3 scripts/run_mas_phase_operator.py \
  --task-dir "$MAS_DISPATCH" \
  --return-json RETURNED_ARTIFACT.json \
  --through-phase draft_review \
  --json
```

The harness stops with an explicit `operator_status`:
- `prepare_main_owned_and_dispatch_subagents`, `create_main_owned_artifacts`, or `dispatch_subagent_tasks`: provide the listed main-owned artifacts and/or dispatch the listed prompt files.
- `repair_return_artifacts` or `repair_before_continue`: repair invalid, duplicate, or missing artifacts before continuing.
- `ask_user`: ask only the narrow confirmation described by `main_actions`.
- `apply_main_actions` or `continue_main_workflow`: return to the Main Orchestrator for final drafting, doubtful handling, export, and validation.

Operator status fields have separate meanings: `command_ok` means the operator invocation completed without an internal error; `gate_ok` mirrors the collector gate for the requested phase; `complete` is true only after all required phases and final verification are complete. Do not interpret `ok=true` alone as delivery readiness.

When `main_action_checklist` appears, treat it as a Main Orchestrator runbook. It may identify source artifacts, action purpose, automation level, and output target, but it never transfers final Markdown writing or delivery wording to specialist agents.

## Codex Dry-Run Protocol

Use `scripts/run_mas_dry_run.py` to test the staged MAS handoff before relying on live subagent execution. The dry-run builds the dispatch bundle, writes generated prompt files, emits synthetic specialist artifacts phase by phase, runs the collector after each phase, and records a `mas_dry_run_trace.json` with `next_action` after `pre_draft`, `draft_review`, and `final_verification`.

Example:

```bash
MAS_DRY_RUN="$(mktemp -d /tmp/mas-dry-run.XXXXXX)"
python3 scripts/run_mas_dry_run.py \
  --request-json references/regression_samples/mas_task_request_audio_plus_document.json \
  --artifact-fixture references/regression_samples/mas_artifacts_valid.json \
  --task-dir "$MAS_DRY_RUN" \
  --out "$MAS_DRY_RUN/mas_dry_run_trace.json" \
  --json
```

The dry-run is deterministic and uses synthetic artifacts. In a live Codex subagent pilot, replace the fixture artifact writes with actual read-only subagent JSON returns:
- Dispatch only the task files listed by the current `next_action`.
- Give each subagent the generated prompt plus the minimum role-relevant current-session materials.
- Ingest each returned JSON object with `ingest_mas_artifact.py` rather than manually copying it into `artifacts/`.
- Run `collect_mas_artifacts.py` after each phase and follow the next `next_action`.
- Dispatch the next phase without user input only when collector `ok` is true and `next_action.type` is `collect_or_dispatch_phase_artifacts`.
- Apply final automatic actions only when collector `ok` is true and `next_action.type` is `continue_without_user_intervention` or `apply_main_actions_before_final_delivery`; otherwise repair artifacts or ask the narrow confirmation requested by `next_action`.

`--overwrite` may delete only an existing MAS dry-run directory under the system temporary root whose basename starts with `mas-` and which contains a dry-run marker or prior MAS control file. A marker outside the temporary root never authorizes recursive deletion.

## Live Codex Synthetic Pilot Findings

The portable synthetic trace in `references/regression_samples/mas_live_pilot_trace_synthetic.json` records a live Codex subagent pilot with five read-only specialist tasks across `pre_draft`, `draft_review`, and `final_verification`. It used synthetic audio+document materials only; no real meeting materials, active skill install sync, commit, push, or final Markdown ownership transfer is part of the trace.

Observed behavior:

- A live Source Reconciler returned schema-invalid JSON by making `manual_review_required` a string instead of a boolean.
- `collect_mas_artifacts.py --through-phase pre_draft` caught the invalid field and emitted `next_action.type=repair_invalid_or_duplicate_artifacts`.
- After repair, the collector allowed dispatch to `draft_review`, then `final_verification`.
- With all phase artifacts present and valid, the final collector still emitted `next_action.type=ask_user_for_narrow_confirmation` because unresolved source conflicts and known unverified parts remained.

Operational rule: run collector validation after every phase, repair invalid or duplicate specialist artifacts before dispatching later phases, and never treat complete artifacts as automatic delivery when the valid `next_action` requests narrow user confirmation.

## Decision Rules

### 自动通过

The main workflow may continue without asking the user when:
- Required artifacts exist for the selected risk profile.
- Source evidence is consistent or the primary source is clearly justified.
- Non-person business facts written as confirmed have reliable external evidence.
- `doubtful_items`, final table, and sidecar are derived from the same records.
- Fidelity review has no severe omission, perspective, or order findings.
- Contract verifier passes.

### 自动标存疑

The main workflow should keep the source wording and add or preserve `doubtful_items` when:
- Candidate entity is not unique.
- External evidence is unavailable, insufficient, stale, or conflicting.
- Timestamp anchor is unavailable or not reliable enough for inline timestamp.
- Audio/document conflict exists but does not decide the main source.
- Target attribution is plausible but not uniquely supported.

### 修复必需

The main workflow must repair and rerun verification before final delivery when:
- Transcript audit requires a rerun or repair; this emits `repair_before_continue` in `pre_draft`, before drafting.
- Required validators were not run.
- A validator, export step, or regression result is failed, blocked, or structurally reports `ok=false`.
- The contract verifier reports errors that cannot be resolved by merely marking content doubtful.

### 请求人工

Ask the user only when:
- Evidence conflict changes an investment fact, attribution, target heading, or source credibility.
- User correction conflicts with source evidence or reliable public facts.
- Primary body source cannot be selected safely.
- A high-risk fact must be written but cannot be confirmed or safely marked as doubtful.
- Specialist artifacts conflict and the main workflow cannot choose a lower-risk path.

After artifacts are emitted, use `scripts/summarize_mas_decisions.py` as a conservative helper for automatic pass, automatic doubtful handling, repair-required gates, and narrow user confirmation. The helper may only consume explicit artifact fields such as `manual_review_required`, `doubtful_items`, unresolved items, known unverified parts, review findings, and export status. It must not infer semantic investment direction or target priority from free text.

Deterministic transcript/export/validator repair takes precedence over a user question in the same run. A doubtful item requests the user only when its `当前判断` or `最终处理` begins with an explicit marker such as `请求人工确认` or `请求用户确认`; free-text mentions such as `无需用户确认` do not trigger a question.

For normal runs, prefer `scripts/collect_mas_artifacts.py` over calling the validator and summarizer separately. The collector reads the task bundle, derives the required artifact list, validates the merged artifact set, detects duplicate artifact types, embeds the summarizer result, reports `phase_gates`, and emits a machine-readable `next_action` in one run summary.

When duplicate artifacts exist, collector output includes `duplicate_artifacts` with the artifact type, first path, and duplicate path. When invalid or duplicate artifacts are present, repair them before dispatching later phases, even if later-phase artifacts are also missing.

`next_action.type` is the main workflow's next executable state:
- `collect_or_dispatch_phase_artifacts`: send or recollect the listed phase task files and main-owned artifacts.
- `repair_missing_artifacts`: regenerate missing artifacts before continuing.
- `repair_invalid_or_duplicate_artifacts`: remove duplicates or regenerate invalid specialist returns.
- `repair_before_continue`: repair or rerun the transcript in `pre_draft` before drafting.
- `repair_before_final_delivery`: repair export, validator, or regression failures before final delivery; rerun the relevant checks after repair.
- `apply_main_actions_before_final_verification`: apply the listed main-owned draft/doubtful actions, record `main_action_receipt`, then rerun collector before dispatching Contract Verifier.
- `apply_main_actions_before_final_delivery`: apply automatic doubtful, repair, or revision actions before user-facing delivery; if the action changes the final Markdown or sidecar, rerun export and validation before delivery.
- `continue_without_user_intervention`: continue the main workflow without asking the user.
- `ask_user_for_narrow_confirmation`: ask only the specific confirmation implied by valid artifacts.

## Implementation Order

1. Keep this contract as the stable reference.
2. Wire `SKILL.md`, README, and interface prompt to this contract.
3. Add synthetic regression anchors for the MAS contract and entry points.
4. Use `scripts/build_mas_task_bundle.py` to create deterministic specialist dispatch plans.
5. Use `scripts/validate_mas_artifacts.py` for lightweight artifact field validation once artifacts are emitted.
6. Use `scripts/summarize_mas_decisions.py` to turn valid artifacts into automatic pass, automatic doubtful handling, or user-confirmation decisions.
7. Use `scripts/collect_mas_artifacts.py` as the default handoff layer from subagent JSON files to a validated run summary.
8. Use `scripts/run_mas_dry_run.py` to verify staged phase handoff and trace `next_action` across synthetic runs.
9. Run a fresh Codex subagent synthetic blind-run through generated prompts, dispatch-bound returns, ingest, main-action receipt, final verification, and recovery before production use.
