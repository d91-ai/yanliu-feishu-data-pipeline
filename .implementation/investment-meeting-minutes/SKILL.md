---
name: investment-meeting-minutes
description: "Use when Codex needs to turn a Chinese investment meeting recording, transcript, document, DOCX/TXT/Markdown draft, or mixed audio+text input into a strict Markdown meeting note with speaker segmentation, company-name correction, stock-symbol validation, source-file archiving, source-fidelity checks, and Markdown export. Triggers include: 整理投资会议录音, 整理投研会议录音, 整理投资会议纪要, 整理投研会议纪要, 把这段投资会议转录整理成纪要, 输出 Obsidian 投资会议纪要, 在投资会议纪要中校对公司名称和股票代码, 导出投资会议纪要 md, 结合录音与文字整理投资会议纪要, 按发言人/版块分段整理投资会议纪要."
---

# Investment Meeting Minutes

## Overview

Produce a strict Chinese investment meeting note from the current meeting's audio, transcript, document, or mixed materials. The final body is a speaker-by-speaker cleaned transcript: preserve each speaker's original order, viewpoint, pronouns, logic, uncertainty, and meaningful wording; only remove pure filler words, obvious ASR noise, meaningless repetitions, and repeated false starts. Validate names and stock codes before writing confirmed entities, and export the human-confirmed Markdown note.

Use the fastest safe path for the source risk. The default path is a single main workflow with deterministic checks. Write final Markdown and archive outputs only through the main workflow. A same-stem `.verification.json` or `.verification.jsonl` may be kept as an internal audit sidecar, but it is not a formal deliverable.

For high-risk work, use `references/mas_orchestration_contract.md` as the process-automation contract. MAS is an execution and review layer for transcription audit, source reconciliation, entity verification, target attribution, fidelity review, and contract verification; it is not a second writer. Specialist agents produce structured artifacts for the main workflow to consume, while the main workflow remains responsible for decisions, final writing, export, and validation.

## Stable Contract

- Workflow after input archive: 转录 -> 校对 -> 识别 -> 联网核验 -> 编辑 -> 排版 -> 验证. Do not silently skip any step; when a step is not applicable, record `skipped_reason`, and when a step fails, record the failure reason and safest next action.
- MAS boundary: use MAS only when risk warrants it, following `references/mas_orchestration_contract.md`. Specialist agents may create `transcript_audit`, `source_reconciliation`, `entity_verification_report`, `target_attribution_review`, `fidelity_review`, and `export_manifest`, but they must not write or modify the final Markdown. Every specialist return must match the current dispatch `run_id`, `task_id`, phase, owner, and allowed artifact set. The main workflow must convert artifacts into automatic pass, transcript/export repair, automatic doubtful handling, or a narrow user-confirmation request.
- Source boundary: use only current-session materials as meeting-content sources. In `audio_plus_document`, transcribe the audio first, then compare the audio-derived `aligned_transcript` with the provided text/documents before choosing the body source. Use the higher-quality same-session source as primary, based on coverage, speaker order, verbatimness, timestamp evidence, ASR noise, omissions, and whether the text is visibly human-corrected. Use the other source as cross-check material for speaker labels, doubtful wording, omissions, and conflicts. External sources must verify non-person business entities, codes, terms, and high-risk public facts before they are written as confirmed; they must not add meeting content.
- User corrections: same-session user corrections or confirmations for entity names, stock codes, terms, candidates, or fact boundaries are high-priority source evidence for updating `doubtful_items` and final handling. If a user correction conflicts with original meeting materials or reliable public facts, record the conflict and keep the item doubtful instead of silently overwriting.
- User override boundary: if the user explicitly asks for read-only analysis, says not to modify files, says not to archive, says this is a test run without archive, or asks to analyze feasibility before execution, do not run archive/export or other write actions. Continue read-only when possible, or ask for confirmation before writing, and record `skipped_reason=user_requested_no_archive_or_write` for skipped archive/export steps.
- ASR: use local SenseVoiceSmall as the primary transcript model and Paraformer-Large as auxiliary proofreading plus timestamp evidence when available. Do not switch to Whisper or another ASR. If the local ASR/timestamp chain cannot run, first diagnose and repair model cache, dependencies, device compatibility, memory, or chunking; use a text-only path only when the runtime cannot be restored and the user accepts that audio review is incomplete.
- Final writer: the main workflow is the only writer and reviewer for final deliverables. It must perform transcript-quality, timestamp, speaker-boundary, source-fidelity, target-attribution, doubtful-item, and omission checks before export.
- Run profile: prefer `fast_document` for short, clean document-only sources; use `standard` for ordinary document-only or ordinary audio-plus-document meetings; use `strict_audio` for audio-only, long audio, audio/document conflicts, or high-risk facts. For audio-plus-document meetings, `standard` still starts from audio transcription so the main workflow can compare source quality before selecting the primary body source.
- Meeting type: default to `多人复盘会`. Use `公司交流` only for a single-company special meeting. Use `专家交流` only for expert Q&A. Do not create `其他`.
- Output format: follow `references/output_contract.md` for shared Markdown structure and ambiguity-table columns; follow the matching meeting-type reference for body structure: `references/meeting_types/review_meeting.md`, `references/meeting_types/listed_company.md`, or `references/meeting_types/expert_call.md`.
- Final filename: follow `references/archive_naming_contract.md`. Use `YYYY-MM-DD - 会议系列.md` for `多人复盘会`; resolve the series from the raw input filename against the maintained known-series list, or ask the user when no unique match exists. Use `YYYY-MM-DD - 公司名 - 上市公司交流.md` for `公司交流`, and `YYYY-MM-DD - 主题 - 专家交流.md` for `专家交流`.
- Review-meeting subsection headings: write the sector line first, then write the target line only when the segment has explicit positively viewed securities targets: `#### 【一级板块｜细分板块】` followed by optional `##### 【标的(代码)】`. If no explicit positively viewed securities target exists, omit the target line entirely; do not output empty brackets such as `##### 【】`.
- Speaker headings: identify speaker titles from current-session context when the source provides enough evidence, such as self-introduction, moderator address, agenda role, Q&A role, or stable transcript labels. Write the identified name or role as the `###` heading. If a speaker cannot be identified reliably, keep the fallback heading as `### 发言人1`, `### 发言人2`, `### 发言人3`, etc. in actual first-appearance order.
- Doubtful items: use one internal `doubtful_items` list as the source for verification, final table rows, and any same-stem verification sidecar. Keep final table columns in `references/output_contract.md`; keep process details in `references/verification_policy.md`.
- MAS evidence boundary: `external_evidence_paths` may contain only a public `https://` URL or a supported public source ID defined in `references/mas_orchestration_contract.md`, never HTTP, localhost/private-network URLs, credential-bearing query parameters, local candidate paths, or arbitrary opaque labels. When `export_manifest.known_unverified_parts` is non-empty, the declared verification sidecar must exist, pass the shared sidecar validator, and match the business `doubtful_items` selected for sidecar before collection can succeed.
- Fidelity: `## 一、发言整理` is a source-aligned cleaned transcript by speaker, not a content summary, abstract, rewrite, interpretation, or third-person retelling. Preserve source perspective and pronouns such as `我`、`我们`、`个人觉得`; do not rewrite them into `发言人认为`、`专家表示`、`管理层表示`、`公司表示` unless those words appear in the source. The only allowed cleanup is deleting pure filler words, obvious ASR noise, meaningless repetitions, and repeated false starts.
- Validators: keep final-note validation to encoding, Markdown structure, and regression samples. Process-artifact validators may enforce structural identity, source binding, review scope, and cross-artifact set consistency, but must not infer content direction or semantic target priority.
- Do not use this skill for standalone stock-symbol lookup, generic entity cleaning, ordinary Markdown export, non-investment/non-research meeting notes, pure ASR transcription without meeting-note output, or meeting-minutes anonymization; use the relevant narrower tool or `meeting-minutes-sanitizer` when the user asks for 脱敏 / 去发言人 / RAG 入库.

## Workflow

### Choose run profile

- `fast_document`: use for short, clean document-only material with clear speakers and few/no uncertain entities. Skip ASR readiness checks, but do not skip the mandatory live verification pass for any non-person business entity or high-risk fact written as confirmed. Run local formatting validators before export.
- `standard`: use for ordinary document-only or audio-plus-document work. For audio-plus-document work, transcribe audio first, build the SenseVoice-based `aligned_transcript`, then compare it with text/documents. Choose the higher-quality same-session source as the primary body source, and use the other source to cross-check speaker labels, missing clauses, doubtful terms, and conflicts. Batch local entity/code candidate lookup first, then run mandatory live verification before confirmed writing. Run main-workflow checks for source quality, attribution, doubtful items, and omissions before export.
- `strict_audio`: use for audio-only, long/noisy meetings, audio/document conflicts, or high-risk facts. Run the relevant readiness profile before the expensive step.

Before final writing, create process-only review notes when risk is non-trivial. When MAS is triggered, keep those notes as structured artifacts defined in `references/mas_orchestration_contract.md`; otherwise keep concise process notes. Do not write these notes into the final note body. Record transcript-quality, timestamp, speaker-boundary, audio/document conflict, target-attribution, high-risk fact, doubtful-item, and omission findings that affect the final note.

When draft-review artifacts require main-workflow changes, apply those changes before dispatching `final_verification`, then use `scripts/record_mas_main_actions.py` to bind the applied action list to the current Markdown SHA-256 and source-artifact digest. Do not reuse an older `export_manifest` after the Markdown or source artifacts change. Re-dispatch final verification and use explicit replacement with repair-history preservation when a same-run artifact must be superseded.

### 0. Prepare Inputs

Archive raw files before transcription or writing unless the user explicitly requested no archive/no file writes/read-only analysis. Use `scripts/archive_raw_inputs.py`; read `references/archive_naming_contract.md` before changing archive/export naming or archive bridges.

Handle source modes:
- `audio_only`: archive, then transcribe with SenseVoice.
- `document_only`: archive, then arrange speaker turns from the provided text/document without summarizing, rewriting, or changing viewpoint.
- `audio_plus_document`: archive both and transcribe audio first. Compare the audio-derived `aligned_transcript` with text/documents for coverage, speaker order, verbatimness, timestamp evidence, ASR noise, omissions, and human-correction signals. Write from the higher-quality same-session source; use the other source for speaker identity, term correction, omission detection, and conflict review. If sources disagree, keep the wording from the source with clearer same-session evidence; unresolved conflicts stay in process notes or `doubtful_items`.

Keep Chinese text files and generated Markdown/TXT/JSON/YAML as UTF-8 without BOM. In Python text I/O, pass `encoding="utf-8"`. If UTF-8 decoding fails or replacement characters appear, stop and report the affected file.

### 1. 转录

When audio is provided, use `scripts/transcribe_audio.py` for local SenseVoiceSmall primary transcription, Paraformer-Large auxiliary cross-checking, and timestamp-index preparation. Runtime failures are repair targets, not a reason to skip audio evidence by default.

Default audio pipeline:
1. Run full-audio fsmn-vad once to obtain global VAD segment boundaries, then transcribe each VAD segment with SenseVoiceSmall as the primary ASR transcript.
2. Run Paraformer-Large as an auxiliary ASR cross-check for finance terms, company names, stock codes, numbers, English abbreviations, and timestamp evidence.
3. Do not automatically replace the SenseVoiceSmall transcript with Paraformer-Large output. Use Paraformer differences as proofreading evidence, and surface unresolved conflicts in `transcript_audit` or `suspect_confirmation`.
4. Build a near-verbatim `aligned_transcript` from the SenseVoice primary transcript plus confirmed cross-check corrections. Do not use cleaned meeting-note prose for timestamp alignment.
5. Prefer sentence/phrase anchors when available. A short `source=sensevoice_vad_segment`, `precision=segment`, `duration_ms <= 10000` record is also reliable enough for doubtful-item replay. Other segment/chunk/minute-level ranges are not reliable final doubtful timestamps.
6. Use the selected timestamp index as the timestamp source for ambiguity rows, preserving `source` and `precision` fields.

Timestamp-index rules:
- Do not use Whisper for transcription, fallback transcription, cross-checking, or timestamp generation.
- Do not run VAD separately on pre-cut 20s/60s chunks for final timestamps; VAD boundaries must come from one full-audio VAD pass.
- `batch_size_s=60` is a runtime generation parameter, not a promise to preserve 60-second chunk artifacts.
- `timestamp_index.json` entries should include `start`, `end`, `start_ms`, `end_ms`, `duration_ms` when known, `chunk_index`, `text`, `source`, and `precision` when the engine exposes them.
- `source` should distinguish `paraformer`, `sensevoice`, `sensevoice_paraformer_checked`, `fa_zh_forced_alignment`, and fallback segment sources when applicable.
- `precision` should distinguish `sentence`, `phrase`, `segment`, `chunk`, and `unavailable`.
- For ambiguity rows, use the same internal `doubtful_items` list for verification, inline timestamps, and the final table. First match the doubtful term to `timestamp_index.text`; output `HH:MM:SS-HH:MM:SS` only from sentence/phrase anchors or short `sensevoice_vad_segment` records. If no reliable match exists, use the no-timestamp table shape and do not write a timestamp placeholder.
- Model downloads, dependency installation, and first-cache warmup are setup work, not formal transcription time.
- Before first use, machine changes, or production-like audio, read `references/runtime_readiness_guide.md` and run `scripts/check_investment_workflow_health.py --profile asr --strict`. Use `--runtime-smoke` only when a real short-audio service call is needed.

### 2. 校对

Use `scripts/process_transcript.py` when text is long, noisy, or missing clear speaker boundaries. Correct obvious ASR noise and delete only pure filler words, meaningless repetitions, and repeated false starts while preserving the speaker's viewpoint, pronouns, order, uncertainty, judgment strength, numbers, timing, actions, and meaningful wording. Treat cleaned text as evidence for final writing, not as permission to summarize, rewrite, polish into report style, or change perspective.

Build a process-only speaker map before final writing. Map raw labels such as `Speaker 1` or `发言人A` to an identified name or role only when current-session content supports it. Do not infer a personal name or role from topic expertise alone. When evidence is insufficient, keep numeric fallback labels in first-appearance order.

When audio is long, noise is heavy, multiple-speaker boundaries are unclear, audio and document evidence conflict, or timestamp alignment matters for doubtful-item review, the main workflow must explicitly check transcript quality, speaker boundaries, timestamp anchors, ASR conflicts, and audio/document conflicts before final writing.

Before final writing, run a source-restoration pass on the working transcript: compare each cleaned turn with its source span, restore omitted substantive clauses, and keep examples, reasons, hedge words, conditions, numbers, time points, actions, and speaker uncertainty unless they are clearly filler or ASR noise. If an intermediate draft is shorter or more polished than the source span, treat it as a checklist for omissions only and rewrite the paragraph from the source span.

### 3. Correct names and symbols

Use references only when they match the uncertainty:
- `references/verification_policy.md`: ASR cleanup, speaker naming, company names, stock-code lookup, evidence boundaries, stable doubtful-item prompt, target roles, investment actions, and heading coverage.

Rules:
- Start from meeting context before choosing a company, ticker, term, customer, supplier, number, date, or event.
- Confirm company names and stock codes before writing them as facts, following `references/verification_policy.md`. Local candidates and ASR output are clues, not proof.
- Run live/network verification as a non-skippable process for non-person business entities and high-risk facts before final writing. If the live source, network, or required professional source is unavailable, do not mark the item confirmed; keep source wording, record the failure path in process notes or sidecar, and place unresolved business items in `## 二、存疑与待确认`.
- External verification query privacy: send only the candidate entity, ticker, term, and the minimum public-fact keywords needed for lookup. Do not send raw long meeting excerpts, speaker identities, private links, unpublished customer/order context, or confidential source text to external search or professional data tools. If an item cannot be verified without exposing private context, keep it doubtful or ask the user for a narrow confirmation.
- Batch local candidate lookup before live verification when several names appear, for example `scripts/query_symbol_candidates.py --batch-file terms.txt --json`. Use `a-stock-data` live sources and reliable external sources required by `references/verification_policy.md`; use `scripts/query_symbol_candidates.py` only as a candidate generator.
- Use this process-only verification prompt before final writing: "For each non-person business entity, ticker, term, customer/supplier, number, date, or public event that will be written as confirmed, first match it to current-session source context, then verify it through at least one reliable live/network or professional external evidence path from `references/verification_policy.md`. For a speaker's buy, sell, add, reduce, or tracking action, verify the action, subject, polarity, and wording against the current-session source span; use external evidence only for a public entity, ticker, or event embedded in that action. If evidence is conflicting, insufficient, unavailable, or not unique, preserve the source wording, mark the doubtful fragment, and keep it in `doubtful_items`; do not add conclusions not present in the meeting source."
- Build and verify `doubtful_items` with the fields, type values, person/business split, and sidecar rules in `references/verification_policy.md`. If a non-person item cannot be confirmed, keep the source wording, mark the doubtful fragment, and keep it in the list for `## 二、存疑与待确认`.
- For audio/video or timestamped transcript sources, locate each doubtful fragment against `timestamp_index.json` before writing `## 二、存疑与待确认`. Use `HH:MM:SS-HH:MM:SS` only when the fragment matches a timestamped sentence/phrase or a short `source=sensevoice_vad_segment`, `duration_ms <= 10000` record. If the source is text/document-only or no reliable audio anchor exists, use the no-timestamp table shape and do not write a timestamp column.
- Do not estimate ambiguity timestamps from the relative position of cleaned notes, summaries, or edited paragraphs.
- Derive final rows and any internal `.verification.json` or `.verification.jsonl` sidecar only from `doubtful_items`; if they conflict, fix the shared list and regenerate both artifacts instead of adding validator hard rules. The sidecar supports audit and review, but the formal deliverable remains the Markdown note.
- Ignore pure person-name uncertainty unless it changes an investment fact or attribution.

When multiple targets are mixed, target attribution is complex, high-risk facts appear, non-person business doubtful items are numerous, or omission risk is high, the main workflow must explicitly check target attribution, high-risk claims, doubtful-item handling, heading coverage, and omissions before final writing.

For `多人复盘会`, target attribution and topic segmentation are semantic writing tasks that must be handled by the language model using current-session context, not by regexes, keyword lists, or deterministic content-direction validators:
- Each `#### 【...】` segment must be one independent theme, logic chain, or coherent comparison group. Unrelated themes must be split even when they appear in one continuous speaker turn.
- A `##### 【...】` target line may contain only securities targets with names and verified codes. It must not contain directions or sectors such as `科技｜算力`.
- Only explicit positively viewed targets belong in the target line. Do not promote negative, avoid/reduce, customer, supplier, competitor, comparable, upstream/downstream, background, or incidental mentions into the target line.
- If several positively viewed targets share the same sector, theme, and logic chain, they may share one target line. If their themes or logic chains differ, split them into separate segments.
- If one coherent theme contains both positive and negative targets, the segment may remain together, but the target line records only the positively viewed targets and the body preserves the negative or cautious view.
- Do not add a company to a target line unless it appears in current-session meeting materials. External evidence may confirm a name or ticker, but must not add new meeting content.
- Before export, run a model-based semantic review of topic segmentation and target attribution. If the review finds wrong grouping, missing primary positively viewed targets, incidental targets in headings, negative targets in target lines, or companies not present in source material, revise the Markdown body and headings before validation.

### 4. 编辑

Write one final speaker-ordered note. Use `references/output_contract.md` plus the matching meeting-type reference.

Preserve actual speech order, speaker perspective, original logic, uncertainty, and meaningful wording for every meeting type. The final body may only remove pure filler words, obvious ASR noise, meaningless repetitions, and repeated false starts; it must not summarize, rewrite, interpret, merge separate turns, polish into research-report prose, or change first-person wording into third-person attribution. Do not convert the note into a summary, compressed brief, research-report section, conclusion list, or target summary table. If a speaker appears multiple times, keep later turns in their real position. Do not include workflow debugging fields such as `输入来源`, `整理说明`, tool names, logs, paths, temporary workflow links, temporary identifiers, or draft-stage explanations.

Before export, do a source-fidelity pass against the current-session transcript or document:
- For each substantive paragraph, confirm it maps back to a source span from the same speaker turn.
- For `audio_plus_document`, map final body paragraphs back to the selected primary source first, then cross-check against the other same-session source for omissions, unclear words, speaker labels, and conflicts. If the document is selected as primary, still use audio timestamps where reliable for doubtful fragments and conflict review.
- Preserve first-person and speaker-perspective wording when the source uses it; do not recast it into third-person attribution.
- Keep long answers as lightly cleaned ordered prose. Split for readability only when the source naturally changes topic; do not replace them with `主要包括`、`核心观点`、`总结来看` style summaries, and do not add connective analysis that the speaker did not say.
- If intermediate notes are more compressed than the source, use them only as omission or risk findings and write final prose from the source span.
- Run a heading self-check against `output_contract.md` and the selected meeting-type reference. The final body must not contain contract-escape headings such as `发言片段`、`未归类`、`主题整理`、`内容摘要`、`观点汇总`; 多人复盘会 must not use a theme name as a fake speaker heading.

### 5. 排版

After final Markdown confirmation, validate and export locally. Do not skip transcription, proofreading, identification, editing, formatting, export, or validation silently; if one step is not applicable or cannot complete, record `skipped_reason` or the failure reason in the process notes before continuing or reporting the blocker.

```bash
python3 scripts/validate_utf8_text.py NOTE.md --require-cjk
python3 scripts/validate_meeting_minutes_contract.py NOTE.md --json
python3 scripts/export_to_obsidian.py NOTE.md
```

The exporter writes one Markdown file as the formal deliverable. Do not generate Word or PDF. If an internal verification sidecar is needed for non-person business doubts, keep it as an audit file and do not present it as the formal deliverable.

PDF input is not a baseline parsing capability. Archive PDF files only as attachments, or ask the user to provide readable text extracted outside this skill.

## Reference Routing

- Shared Markdown output structure and ambiguity tables: `references/output_contract.md`.
- Meeting-type references: `references/meeting_types/review_meeting.md`, `references/meeting_types/listed_company.md`, and `references/meeting_types/expert_call.md`.
- Archive/export naming: `references/archive_naming_contract.md`.
- Runtime readiness: `references/runtime_readiness_guide.md`.
- Name/code/entity proofreading, evidence boundaries, target attribution, and doubtful-item verification prompt: `references/verification_policy.md`.
- MAS process automation, specialist-agent boundaries, artifact schema, and automatic/manual decision rules: `references/mas_orchestration_contract.md`.

## Resources

Core scripts:
- `archive_raw_inputs.py`: copy current raw files into the workflow archive.
- `transcribe_audio.py`: local SenseVoiceSmall transcription plus Paraformer auxiliary proofreading and available timestamp-index preparation; no Whisper fallback.
- `process_transcript.py`: transcript cleanup aid.
- `query_symbol_candidates.py`: local symbol candidate lookup.
- `export_to_obsidian.py`: final Markdown export.
- `build_mas_task_bundle.py`: generate process-only MAS specialist task bundles and optional Codex-ready subagent prompt files before dispatch.
- `create_mas_source_manifest.py`: create the main-owned `source_manifest` artifact from the MAS request or task bundle without claiming archive completion.
- `ingest_mas_artifact.py`: receive one returned MAS subagent JSON artifact, validate it, write valid artifacts under `artifacts/`, and preserve invalid or duplicate returns under `repair_history/`.
- `collect_mas_artifacts.py`: collect returned MAS specialist JSON files from a dispatch directory, validate required artifacts, detect duplicates, report phase gates, and produce the main-orchestrator run summary with `next_action`.
- `plan_mas_next_action.py`: turn a collector `next_action` into the next executable checklist: prompt files to dispatch, ingest commands, main-owned artifact gaps, repair actions, narrow user confirmation, or final `main_action_checklist`.
- `record_mas_main_actions.py`: record main-owned pre-final actions against the exact Markdown SHA-256 and current source-artifact digest before final verification.
- `run_mas_phase_operator.py`: run one repeatable MAS operator loop over a dispatch directory by initializing dispatch files, ingesting returned artifact JSON, collecting artifacts, writing combined artifacts, and writing the next-action plan; it does not spawn subagents or write final Markdown.
- `run_mas_dry_run.py`: run a staged synthetic MAS handoff to verify prompt dispatch, artifact collection, phase gates, and `next_action` before relying on live Codex subagents.
- `summarize_mas_decisions.py`: summarize MAS artifacts into automatic pass, automatic doubtful handling, or user-confirmation decisions.
- `validate_utf8_text.py`, `validate_meeting_minutes_contract.py`, `validate_mas_artifacts.py`, `run_meeting_minutes_regression.py`: encoding, Markdown formatting, MAS artifact structure, and sample-regression checks.

Maintenance-only script:
- `organize_raw_archive_structure.py`: reorganize historical raw-input archives only after dry-run review; `--apply` and `--remove-empty-dirs` require explicit user approval and are not part of ordinary meeting processing.

## Output Contract

Every final note must follow `references/output_contract.md`, including metadata, speaker-order preservation, heading rules, meeting-type formatting, and ambiguity-table shape.

If the user asks for optimization later, preserve this simplified structure unless they explicitly request a breaking change.
