#!/usr/bin/env python3
"""Build deterministic MAS specialist task bundles for meeting minutes."""

from __future__ import annotations

import argparse
import copy
import json
import uuid
from pathlib import Path
from typing import Any

from create_mas_source_manifest import material_coverage_errors
from mas_task_lock import mas_task_lock
from validate_mas_artifacts import (
    BOOLEAN_FIELD_RULES,
    DOUBTFUL_REQUIRED_FIELDS,
    FORBIDDEN_FINAL_FIELDS,
    LIST_FIELD_RULES,
    REQUIRED_FIELDS,
    STRING_FIELD_RULES,
)

RUN_PROFILES = {"fast_document", "standard", "strict_audio"}
SOURCE_MODES = {"document_only", "audio_only", "audio_plus_document"}
MEETING_TYPES = {"多人复盘会", "公司交流", "专家交流"}
SOURCE_SELECTION_STATUSES = {"not_applicable", "not_compared", "compared_clear", "conflict", "uncertain"}
PRIMARY_SOURCE_ALIASES_BY_MODE = {
    "audio_only": {"aligned_transcript", "audio_transcript", "transcript"},
    "document_only": {"document", "provided_document", "provided_transcript", "transcript"},
    "audio_plus_document": {
        "aligned_transcript",
        "audio_transcript",
        "document",
        "provided_document",
        "provided_transcript",
        "transcript",
    },
}
PRIMARY_SOURCE_EXAMPLE_BY_MODE = {
    "audio_only": "aligned_transcript",
    "document_only": "provided_document",
    "audio_plus_document": "aligned_transcript",
}

AUDIO_RISKS = {
    "audio_input",
    "long_audio",
    "noisy_audio",
    "unclear_speaker_boundaries",
    "timestamp_alignment",
    "strict_audio",
}
SOURCE_RECONCILIATION_RISKS = {
    "audio_plus_document",
    "source_conflict",
    "primary_source_uncertain",
}
ENTITY_RISKS = {
    "entity_verification",
    "high_risk_facts",
    "many_doubtful_items",
    "company_codes",
    "customers_suppliers",
    "numbers_dates",
}
TARGET_RISKS = {
    "target_attribution",
    "multi_target",
    "mixed_targets",
    "positive_negative_views",
}
FIDELITY_RISKS = {
    "fidelity_review",
    "omission_risk",
    "summary_compression",
    "third_person_rewrite",
    "prior_user_feedback",
}
KNOWN_RISK_FLAGS = (
    AUDIO_RISKS
    | SOURCE_RECONCILIATION_RISKS
    | ENTITY_RISKS
    | TARGET_RISKS
    | FIDELITY_RISKS
)

ROLE_SPECS: dict[str, dict[str, Any]] = {
    "transcript_audit": {
        "role": "Transcript Auditor",
        "dispatch_phase": "pre_draft",
        "objective": "Audit ASR quality, speaker boundaries, timestamp anchors, and ASR conflicts.",
        "inputs": [
            "raw audio metadata",
            "SenseVoice transcript",
            "Paraformer auxiliary differences",
            "timestamp_index",
        ],
        "checks": [
            "ASR noise",
            "long segment anomalies",
            "speaker-boundary ambiguity",
            "SenseVoice/Paraformer conflict",
            "timestamp anchor reliability",
        ],
    },
    "source_reconciliation": {
        "role": "Source Reconciler",
        "dispatch_phase": "pre_draft",
        "objective": "Select and justify the primary body source from same-session materials.",
        "inputs": [
            "audio-derived aligned_transcript",
            "provided document or transcript",
            "same-session user corrections",
            "source quality notes",
        ],
        "checks": [
            "coverage",
            "speaker order",
            "verbatimness",
            "timestamp evidence",
            "ASR noise versus human-correction traces",
            "omissions",
            "source conflicts",
        ],
    },
    "entity_verification_report": {
        "role": "Entity Verifier",
        "dispatch_phase": "pre_draft",
        "objective": "Verify non-person business entities and update doubtful_items proposals.",
        "inputs": [
            "current-session source context",
            "entity candidates",
            "local code candidates",
            "external evidence paths",
        ],
        "checks": [
            "company names",
            "stock codes",
            "customers and suppliers",
            "numbers and dates",
            "industry terms",
            "public high-risk facts",
        ],
        "secondary_artifacts": ["doubtful_items"],
    },
    "target_attribution_review": {
        "role": "Target Attribution Reviewer",
        "dispatch_phase": "draft_review",
        "objective": "Review target headings, sector grouping, and positive/negative attribution.",
        "inputs": [
            "review-meeting draft body",
            "source spans",
            "entity verification status",
        ],
        "checks": [
            "wrong grouping",
            "missing positive targets",
            "incidental targets in heading",
            "negative targets in target line",
            "non-source companies",
        ],
    },
    "fidelity_review": {
        "role": "Fidelity Reviewer",
        "dispatch_phase": "draft_review",
        "objective": "Review whether draft prose preserves source order, pronouns, and substance.",
        "inputs": [
            "draft Markdown",
            "source spans",
            "source_reconciliation",
        ],
        "checks": [
            "summary compression",
            "third-person rewrite",
            "omitted reasons or numbers",
            "merged speaker turns",
            "speaker-order drift",
        ],
    },
    "export_manifest": {
        "role": "Contract Verifier",
        "dispatch_phase": "final_verification",
        "objective": "Verify encoding, Markdown contract, sidecar consistency, export status, and regressions.",
        "inputs": [
            "final Markdown",
            "verification sidecar",
            "timestamp_index",
            "export logs",
            "validator outputs",
        ],
        "checks": [
            "UTF-8",
            "Markdown contract",
            "doubtful table",
            "timestamp_index",
            "verification sidecar",
            "regression result",
        ],
    },
}

DISPATCH_PHASES: dict[str, dict[str, str]] = {
    "pre_draft": {
        "when": "After current-session source materials are prepared and before final-note drafting.",
        "materials": "Audio/transcript/document excerpts, timestamp indexes, entity candidates, and source-quality notes relevant to the assigned role.",
    },
    "draft_review": {
        "when": "After the main workflow has a draft and before final validation.",
        "materials": "Draft Markdown excerpts plus source spans and validated process artifacts required by the assigned role.",
    },
    "final_verification": {
        "when": "After final Markdown, sidecars, export logs, and validator outputs exist.",
        "materials": "Final Markdown path, verification sidecar path, timestamp index, export logs, and validator/regression results.",
    },
}

PHASE_ORDER = {phase: index for index, phase in enumerate(DISPATCH_PHASES)}


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def normalized_flags(flags: Any) -> list[str]:
    if flags is None:
        return []
    if not isinstance(flags, list):
        raise ValueError("risk_flags 必须是 JSON array")
    normalized = sorted({str(flag).strip() for flag in flags if str(flag).strip()})
    unknown = sorted(set(normalized) - KNOWN_RISK_FLAGS)
    if unknown:
        raise ValueError("未知 risk_flags: " + ", ".join(unknown))
    return normalized


def validate_choice(name: str, value: str, allowed: set[str]) -> None:
    if value not in allowed:
        allowed_text = ", ".join(sorted(allowed))
        raise ValueError(f"{name} 必须是以下之一: {allowed_text}")


def bundle_configuration_errors(
    run_profile: str,
    source_mode: str,
    meeting_type: str,
    source_selection_status: str,
) -> list[str]:
    errors: list[str] = []
    if run_profile not in RUN_PROFILES:
        errors.append("run_profile 必须是固定枚举值")
    if source_mode not in SOURCE_MODES:
        errors.append("source_mode 必须是固定枚举值")
    if meeting_type not in MEETING_TYPES:
        errors.append("meeting_type 必须是固定枚举值")
    if source_selection_status not in SOURCE_SELECTION_STATUSES:
        errors.append("source_selection_status 必须是固定枚举值")
    if source_mode == "audio_only" and run_profile != "strict_audio":
        errors.append("audio_only 必须使用 strict_audio run_profile")
    if source_mode == "audio_plus_document" and source_selection_status == "not_applicable":
        errors.append("audio_plus_document 的 source_selection_status 不得为 not_applicable")
    if source_mode in {"audio_only", "document_only"} and source_selection_status != "not_applicable":
        errors.append("非混合来源的 source_selection_status 必须为 not_applicable")
    return errors


def normalized_source_selection_status(source_mode: str, value: Any) -> str:
    status = str(value or "").strip()
    if not status:
        return "not_compared" if source_mode == "audio_plus_document" else "not_applicable"
    validate_choice("source_selection_status", status, SOURCE_SELECTION_STATUSES)
    if source_mode == "audio_plus_document" and status == "not_applicable":
        return "not_compared"
    if source_mode != "audio_plus_document" and status != "not_applicable":
        raise ValueError("source_selection_status 仅适用于 audio_plus_document；其他 source_mode 请使用 not_applicable")
    return status


def infer_risk_flags(
    run_profile: str,
    source_mode: str,
    risk_flags: list[str],
    source_selection_status: str = "not_applicable",
) -> list[str]:
    risks = set(risk_flags)
    if source_mode == "audio_only":
        risks.update({"audio_input", "timestamp_alignment"})
    if source_mode == "audio_plus_document":
        if source_selection_status in {"not_compared", "uncertain"}:
            risks.add("primary_source_uncertain")
        elif source_selection_status == "conflict":
            risks.update({"primary_source_uncertain", "source_conflict"})
    if run_profile == "strict_audio":
        risks.update({"audio_input", "strict_audio", "long_audio", "timestamp_alignment", "omission_risk"})
    return sorted(risks)


def should_use_mas(
    run_profile: str,
    source_mode: str,
    risk_flags: list[str],
    source_selection_status: str = "not_applicable",
) -> bool:
    risks = set(infer_risk_flags(run_profile, source_mode, risk_flags, source_selection_status))
    if run_profile == "fast_document" and not risks:
        return False
    return bool(risks)


def select_expected_artifacts(
    run_profile: str,
    source_mode: str,
    meeting_type: str,
    risk_flags: list[str],
    source_selection_status: str = "not_applicable",
) -> list[str]:
    risks = set(infer_risk_flags(run_profile, source_mode, risk_flags, source_selection_status))
    if not should_use_mas(run_profile, source_mode, risk_flags, source_selection_status):
        return []

    artifacts = {"source_manifest", "export_manifest"}
    if risks & AUDIO_RISKS or source_mode == "audio_plus_document":
        artifacts.add("transcript_audit")
    if source_mode == "audio_plus_document" or risks & SOURCE_RECONCILIATION_RISKS:
        artifacts.add("source_reconciliation")
    if risks & TARGET_RISKS or (meeting_type == "多人复盘会" and run_profile == "strict_audio"):
        artifacts.add("target_attribution_review")
    if risks & ENTITY_RISKS:
        artifacts.add("entity_verification_report")
        artifacts.add("doubtful_items")
    if risks & (FIDELITY_RISKS | SOURCE_RECONCILIATION_RISKS):
        artifacts.add("fidelity_review")
    return sorted(artifacts)


def output_shape_for(
    artifact_type: str,
    secondary_artifacts: list[str],
    identity: dict[str, str] | None = None,
    source_mode: str = "audio_plus_document",
) -> dict[str, Any]:
    def placeholder(field: str) -> Any:
        artifact_examples: dict[str, dict[str, Any]] = {
            "transcript_audit": {
                "asr_primary": "SenseVoiceSmall",
                "asr_auxiliary": "",
                "timestamp_index_status": "unavailable",
                "recommended_action": "continue",
            },
            "source_reconciliation": {
                "primary_body_source": PRIMARY_SOURCE_EXAMPLE_BY_MODE.get(source_mode, "transcript"),
                "primary_source_reason": "replace with current-session evidence",
                "cross_check_source": "provided_document" if source_mode == "audio_plus_document" else "",
                "manual_review_required": False,
            },
            "target_attribution_review": {"segments_reviewed": 1},
            "fidelity_review": {"paragraphs_reviewed": 1},
            "export_manifest": {
                "markdown_path": "NOTE.md",
                "markdown_sha256": "0" * 64,
                "verification_sidecar_path": "",
                "validators_run": [
                    {"name": "validate_utf8_text.py", "ok": False},
                    {"name": "validate_meeting_minutes_contract.py", "ok": False},
                ],
                "regression_result": {
                    "name": "run_meeting_minutes_regression.py",
                    "case_count": 1,
                    "ok": False,
                },
                "export_status": "blocked",
                "main_actions_verified": False,
            },
        }
        if field in artifact_examples.get(artifact_type, {}):
            return copy.deepcopy(artifact_examples[artifact_type][field])
        if field in BOOLEAN_FIELD_RULES.get(artifact_type, []):
            return False
        if field in LIST_FIELD_RULES.get(artifact_type, []):
            return []
        if field in STRING_FIELD_RULES.get(artifact_type, []):
            return ""
        if field in {"segments_reviewed", "paragraphs_reviewed"}:
            return 1
        if field in {"confirmed_item_evidence_paths", "regression_result"}:
            return {}
        return None

    identity = identity or {}
    if secondary_artifacts:
        shape: dict[str, Any] = {
            **identity,
            "artifacts": {
                artifact_type: {field: placeholder(field) for field in REQUIRED_FIELDS[artifact_type]},
            }
        }
        for secondary in secondary_artifacts:
            if secondary == "doubtful_items":
                shape["artifacts"][secondary] = []
        return shape
    return {
        **identity,
        "artifact_type": artifact_type,
        "artifact": {field: placeholder(field) for field in REQUIRED_FIELDS[artifact_type]},
    }


def prompt_for_task(artifact_type: str, spec: dict[str, Any], run_profile: str, source_mode: str) -> str:
    required_fields = REQUIRED_FIELDS[artifact_type]
    secondary = [str(item) for item in spec.get("secondary_artifacts", [])]
    lines = [
        "Use $investment-meeting-minutes for this process-only specialist task.",
        f"Role: {spec['role']}.",
        f"Run profile: {run_profile}; source mode: {source_mode}.",
        f"Objective: {spec['objective']}",
        "Do not write, modify, assemble, or export final Markdown.",
        "Do not modify repository files or meeting-note files; return the requested process artifact only.",
        "Use only current-session meeting materials as meeting-content evidence.",
        "External sources may verify names, codes, terms, and public facts only.",
        "Do not upload private meeting materials, transcripts, recordings, or local paths to external services.",
        "Return only JSON. Do not include prose outside JSON.",
        f"Primary artifact: {artifact_type}.",
        "Role inputs: " + "; ".join(str(item) for item in spec.get("inputs", [])) + ".",
        "Required checks: " + "; ".join(str(item) for item in spec.get("checks", [])) + ".",
        f"Required fields: {', '.join(required_fields)}.",
    ]
    if "doubtful_items" in secondary:
        lines.append(f"Also return doubtful_items with fields: {', '.join(DOUBTFUL_REQUIRED_FIELDS)}.")
    if artifact_type == "transcript_audit":
        lines.append("Set recommended_action to exactly one of: continue, repair_transcript, request_user.")
        lines.append("Do not use continue when quality_flags, speaker_boundary_findings, or conflicts are non-empty.")
    elif artifact_type == "source_reconciliation":
        lines.append("When manual_review_required=false, primary_body_source and primary_source_reason must be non-empty.")
        aliases = ", ".join(sorted(PRIMARY_SOURCE_ALIASES_BY_MODE.get(source_mode, {"transcript"})))
        lines.append(
            "primary_body_source must name a current-session material or use an alias allowed for this source_mode: "
            + aliases
            + "."
        )
        if source_mode == "audio_plus_document":
            lines.append(
                "When manual_review_required=false, cross_check_source must be non-empty, bound to current-session "
                "audio/document evidence, and come from the evidence side not used by primary_body_source."
            )
        else:
            lines.append("If cross_check_source is non-empty, it must be bound to an eligible current-session body source.")
    elif artifact_type == "entity_verification_report":
        lines.append("Every items entry must appear in exactly one of confirmed_items or unresolved_items.")
        lines.append("Do not copy local_candidate_paths into external_evidence_paths.")
        lines.append("If entity evidence is insufficient, put the exact item in unresolved_items and doubtful_items; do not guess.")
    elif artifact_type == "target_attribution_review":
        lines.append("segments_reviewed must be a positive integer for the actual reviewed scope.")
        lines.append("If target attribution is unsupported, add the exact finding to recommended_revisions; do not invent a target.")
    elif artifact_type == "fidelity_review":
        lines.append("paragraphs_reviewed must be a positive integer for the actual reviewed scope.")
        lines.append("If source mapping is insufficient, add the exact paragraph to source_mapping_failures; do not infer missing speech.")
    elif artifact_type == "export_manifest":
        lines.append("Return markdown_sha256 for markdown_path and set main_actions_verified as a boolean.")
        lines.append("validators_run must contain exactly validate_utf8_text.py and validate_meeting_minutes_contract.py with boolean ok.")
        lines.append("regression_result must contain name=run_meeting_minutes_regression.py, a positive integer case_count, and boolean ok.")
        lines.append("Set export_status to exactly one of: passed, failed, blocked.")
    lines.append(f"Forbidden final-output fields: {', '.join(sorted(FORBIDDEN_FINAL_FIELDS))}.")
    if artifact_type not in {"entity_verification_report", "target_attribution_review", "fidelity_review"}:
        lines.append("If evidence is insufficient or conflicting, use this artifact's conflict or failure fields instead of guessing.")
    return "\n".join(lines)


def task_file_name(index: int, task: dict[str, Any]) -> str:
    artifact_type = str(task["artifact_type"])
    return f"{index:02d}-{artifact_type}.prompt.md"


def prompt_markdown(bundle: dict[str, Any], task: dict[str, Any]) -> str:
    artifact_type = str(task["artifact_type"])
    dispatch_phase = str(task["dispatch_phase"])
    phase = DISPATCH_PHASES[dispatch_phase]
    output_shape = json.dumps(task["expected_output_shape"], ensure_ascii=False, indent=2)
    return "\n".join(
        [
            f"# MAS Specialist Task: {task['role']}",
            "",
            "Use this as the exact prompt for one Codex subagent when subagents are available.",
            "The main workflow remains the only final-note writer and final decision owner.",
            "",
            "## Run Context",
            "",
            f"- run_profile: `{bundle['run_profile']}`",
            f"- source_mode: `{bundle['source_mode']}`",
            f"- meeting_type: `{bundle['meeting_type']}`",
            f"- artifact_type: `{artifact_type}`",
            f"- dispatch_phase: `{dispatch_phase}`",
            f"- run_id: `{bundle.get('run_id', '')}`",
            f"- task_id: `{task.get('task_id', '')}`",
            f"- artifact_owner: `{task.get('role', '')}`",
            f"- phase_timing: {phase['when']}",
            "",
            "## Material Handoff",
            "",
            "The main workflow must attach only the role-relevant current-session materials for this subagent.",
            f"Expected material class: {phase['materials']}",
            "Do not use this prompt alone as meeting-content evidence.",
            "Do not request or inspect unrelated repository files.",
            "",
            "## Prompt",
            "",
            "```text",
            str(task["prompt"]),
            "```",
            "",
            "## Expected JSON Shape",
            "",
            "```json",
            output_shape,
            "```",
            "",
        ]
    )


def build_task(artifact_type: str, run_profile: str, source_mode: str) -> dict[str, Any]:
    spec = ROLE_SPECS[artifact_type]
    inputs = [str(item) for item in spec.get("inputs", [])]
    if artifact_type == "fidelity_review" and source_mode != "audio_plus_document":
        inputs = [
            "selected primary body source and source-selection rationale"
            if item == "source_reconciliation"
            else item
            for item in inputs
        ]
    prompt_spec = {**spec, "inputs": inputs}
    secondary_artifacts = [str(item) for item in spec.get("secondary_artifacts", [])]
    dispatch_phase = str(spec["dispatch_phase"])
    task = {
        "role": spec["role"],
        "artifact_type": artifact_type,
        "dispatch_phase": dispatch_phase,
        "secondary_artifacts": secondary_artifacts,
        "objective": spec["objective"],
        "inputs": inputs,
        "checks": spec["checks"],
        "required_fields": REQUIRED_FIELDS[artifact_type],
        "forbidden_final_fields": sorted(FORBIDDEN_FINAL_FIELDS),
        "expected_output_shape": output_shape_for(
            artifact_type,
            secondary_artifacts,
            source_mode=source_mode,
        ),
        "prompt": prompt_for_task(artifact_type, prompt_spec, run_profile, source_mode),
        "material_handoff": DISPATCH_PHASES[dispatch_phase]["materials"],
    }
    if "doubtful_items" in secondary_artifacts:
        task["secondary_required_fields"] = {"doubtful_items": DOUBTFUL_REQUIRED_FIELDS}
    return task


def bind_dispatch_identity(bundle: dict[str, Any]) -> dict[str, Any]:
    bound = copy.deepcopy(bundle)
    run_id = str(bound.get("run_id") or uuid.uuid4().hex)
    bound["run_id"] = run_id
    for index, task in enumerate(bound.get("tasks", []), start=1):
        if not isinstance(task, dict):
            continue
        artifact_type = str(task.get("artifact_type") or "")
        task_id = str(task.get("task_id") or f"{run_id}:{index:02d}:{artifact_type}")
        dispatch_phase = str(task.get("dispatch_phase") or "")
        owner = str(task.get("role") or "")
        task.update(
            {
                "run_id": run_id,
                "task_id": task_id,
                "artifact_owner": owner,
                "expected_output_shape": output_shape_for(
                    artifact_type,
                    [str(item) for item in task.get("secondary_artifacts", [])],
                    identity={
                        "run_id": run_id,
                        "task_id": task_id,
                        "dispatch_phase": dispatch_phase,
                        "artifact_owner": owner,
                    },
                    source_mode=str(bound.get("source_mode") or "audio_plus_document"),
                ),
            }
        )
    return bound


def dispatch_protocol() -> dict[str, Any]:
    return {
        "runtime": "codex_subagent_optional",
        "dispatch": "Spawn one read-only/process-only subagent per generated task file only when that task's dispatch_phase is ready; otherwise use the prompts as a manual checklist.",
        "parallelism": "Tasks in the same dispatch_phase may run in parallel after the main workflow has prepared role-relevant current-session materials.",
        "phases": DISPATCH_PHASES,
        "return_contract": "Each subagent returns only the requested JSON artifact. The main workflow validates and consumes artifacts.",
        "main_workflow_after_return": [
            "run_mas_phase_operator.py",
            "create_mas_source_manifest.py",
            "ingest_mas_artifact.py",
            "collect_mas_artifacts.py",
            "plan_mas_next_action.py",
            "validate_mas_artifacts.py",
            "summarize_mas_decisions.py",
            "revise or mark doubtful only through the main workflow",
            "run final Markdown validators",
        ],
    }


def artifact_owners(expected_artifacts: list[str]) -> dict[str, str]:
    owners: dict[str, str] = {}
    for artifact in expected_artifacts:
        if artifact == "source_manifest":
            owners[artifact] = "Main Orchestrator"
        elif artifact == "doubtful_items":
            owners[artifact] = "Entity Verifier proposes; Main Orchestrator decides"
        elif artifact in ROLE_SPECS:
            owners[artifact] = str(ROLE_SPECS[artifact]["role"])
        else:
            owners[artifact] = "Main Orchestrator"
    return owners


def build_bundle_from_request(request: dict[str, Any]) -> dict[str, Any]:
    run_profile = str(request.get("run_profile") or "standard")
    source_mode = str(request.get("source_mode") or "document_only")
    meeting_type = str(request.get("meeting_type") or "多人复盘会")
    validate_choice("run_profile", run_profile, RUN_PROFILES)
    validate_choice("source_mode", source_mode, SOURCE_MODES)
    validate_choice("meeting_type", meeting_type, MEETING_TYPES)

    risk_flags = normalized_flags(request.get("risk_flags", request.get("risks", [])))
    materials = request.get("materials", [])
    if not isinstance(materials, list):
        raise ValueError("materials 必须是 JSON array")
    source_selection_status = normalized_source_selection_status(source_mode, request.get("source_selection_status"))
    configuration_errors = bundle_configuration_errors(
        run_profile,
        source_mode,
        meeting_type,
        source_selection_status,
    )
    if configuration_errors:
        raise ValueError("; ".join(configuration_errors))
    inferred_risks = infer_risk_flags(run_profile, source_mode, risk_flags, source_selection_status)
    expected_artifacts = select_expected_artifacts(
        run_profile,
        source_mode,
        meeting_type,
        risk_flags,
        source_selection_status,
    )
    task_artifacts = [
        artifact
        for artifact in expected_artifacts
        if artifact not in {"source_manifest", "doubtful_items"}
    ]
    tasks = sorted(
        (build_task(artifact, run_profile, source_mode) for artifact in task_artifacts),
        key=lambda task: (PHASE_ORDER[str(task["dispatch_phase"])], str(task["artifact_type"])),
    )

    return {
        "schema_version": "1.0",
        "run_profile": run_profile,
        "source_mode": source_mode,
        "source_selection_status": source_selection_status,
        "meeting_type": meeting_type,
        "mas_required": should_use_mas(run_profile, source_mode, risk_flags, source_selection_status),
        "risk_flags": inferred_risks,
        "materials": copy.deepcopy(materials),
        "main_orchestrator": {
            "final_writer_only": True,
            "must_not_delegate": [
                "final Markdown writing",
                "archive/export side effects",
                "final user-facing delivery wording",
                "conflict decisions that require user confirmation",
            ],
            "decision_outputs": ["automatic_pass", "automatic_doubtful", "repair_required", "request_user"],
        },
        "expected_artifacts": expected_artifacts,
        "artifact_owners": artifact_owners(expected_artifacts),
        "dispatch_protocol": dispatch_protocol(),
        "tasks": tasks,
        "validation": {
            "artifact_validator": "scripts/validate_mas_artifacts.py",
            "required_artifacts": expected_artifacts,
        },
    }


def validate_bundle(
    bundle: dict[str, Any],
    *,
    require_material_coverage: bool = True,
) -> list[str]:
    errors: list[str] = []
    expected_artifacts = bundle.get("expected_artifacts")
    tasks = bundle.get("tasks")
    if bundle.get("schema_version") != "1.0":
        errors.append("MAS task bundle schema_version 必须是 1.0")
    run_profile = str(bundle.get("run_profile") or "")
    source_mode = str(bundle.get("source_mode") or "")
    meeting_type = str(bundle.get("meeting_type") or "")
    source_selection_status = str(bundle.get("source_selection_status") or "")
    configuration_errors = bundle_configuration_errors(
        run_profile,
        source_mode,
        meeting_type,
        source_selection_status,
    )
    errors.extend(configuration_errors)
    if not isinstance(expected_artifacts, list):
        errors.append("MAS task bundle expected_artifacts 必须是 JSON array")
        expected_artifacts = []
    if not isinstance(tasks, list):
        errors.append("MAS task bundle tasks 必须是 JSON array")
        tasks = []
    if not isinstance(bundle.get("materials"), list):
        errors.append("MAS task bundle materials 必须是 JSON array")
    elif require_material_coverage and bundle.get("mas_required") and not bundle.get("materials"):
        errors.append("MAS task bundle 启用 MAS 时 materials 不得为空")
    elif require_material_coverage:
        errors.extend(
            material_coverage_errors(
                str(bundle.get("source_mode") or ""),
                list(bundle.get("materials") or []),
            )
        )
    dispatch = bundle.get("dispatch_protocol")
    if bundle.get("mas_required") and not isinstance(dispatch, dict):
        errors.append("MAS task bundle dispatch_protocol 必须是 JSON object")
    owners = bundle.get("artifact_owners")
    if expected_artifacts and not isinstance(owners, dict):
        errors.append("MAS task bundle artifact_owners 必须是 JSON object")
        owners = {}
    if bundle.get("mas_required") and not tasks:
        errors.append("MAS task bundle 启用 MAS 时必须包含 specialist tasks")
    for artifact in expected_artifacts:
        if str(artifact) not in owners:
            errors.append(f"MAS task bundle artifact_owners 缺少 owner: {artifact}")

    expected_list = [str(item) for item in expected_artifacts]
    expected_set = set(expected_list)
    if len(expected_list) != len(expected_set):
        errors.append("MAS task bundle expected_artifacts 不得重复")
    risk_flags: list[str] = []
    try:
        risk_flags = normalized_flags(bundle.get("risk_flags"))
    except ValueError as exc:
        errors.append(str(exc))
    else:
        if risk_flags != bundle.get("risk_flags"):
            errors.append("MAS task bundle risk_flags 必须去重并排序")
    if not isinstance(bundle.get("mas_required"), bool):
        errors.append("MAS task bundle mas_required 必须是 boolean")
    if not configuration_errors and isinstance(bundle.get("mas_required"), bool):
        canonical_mas_required = should_use_mas(
            run_profile,
            source_mode,
            risk_flags,
            source_selection_status,
        )
        if bundle.get("mas_required") != canonical_mas_required:
            errors.append("MAS task bundle mas_required 与 risk matrix 不一致")
        canonical_expected = set(
            select_expected_artifacts(
                run_profile,
                source_mode,
                meeting_type,
                risk_flags,
                source_selection_status,
            )
        )
        if expected_set != canonical_expected:
            errors.append(
                "MAS task bundle expected_artifacts 与 risk matrix 不一致: "
                f"expected={sorted(canonical_expected)} actual={sorted(expected_set)}"
            )
    if (
        bundle.get("source_mode") == "audio_plus_document"
        and "fidelity_review" in expected_set
        and "source_reconciliation" not in expected_set
    ):
        errors.append("audio_plus_document fidelity_review 缺少 source_reconciliation 依赖")
    produced_artifacts = {"source_manifest"} if bundle.get("mas_required") else set()
    producer_counts: dict[str, int] = {"source_manifest": 1} if bundle.get("mas_required") else {}
    for task in tasks:
        if not isinstance(task, dict):
            errors.append("MAS task bundle task 必须是 JSON object")
            continue
        artifact_type = str(task.get("artifact_type") or "")
        if artifact_type not in ROLE_SPECS:
            errors.append(f"未知 MAS task artifact_type: {artifact_type}")
            continue
        expected_task = build_task(artifact_type, run_profile, source_mode)
        for field in (
            "role",
            "dispatch_phase",
            "secondary_artifacts",
            "objective",
            "inputs",
            "checks",
            "required_fields",
            "forbidden_final_fields",
            "prompt",
            "material_handoff",
        ):
            if task.get(field) != expected_task.get(field):
                errors.append(f"{artifact_type} task {field} 与角色契约不一致")
        if task.get("secondary_required_fields") != expected_task.get("secondary_required_fields"):
            errors.append(f"{artifact_type} task secondary_required_fields 与角色契约不一致")
        produced_artifacts.add(artifact_type)
        producer_counts[artifact_type] = producer_counts.get(artifact_type, 0) + 1
        actual_secondary = task.get("secondary_artifacts")
        if isinstance(actual_secondary, list):
            for item in actual_secondary:
                secondary = str(item)
                produced_artifacts.add(secondary)
                producer_counts[secondary] = producer_counts.get(secondary, 0) + 1
        if artifact_type not in expected_set:
            errors.append(f"task artifact_type 不在 expected_artifacts 中: {artifact_type}")
        required_fields = task.get("required_fields")
        if artifact_type in REQUIRED_FIELDS and required_fields != REQUIRED_FIELDS[artifact_type]:
            errors.append(f"{artifact_type} task required_fields 与 artifact schema 不一致")
        dispatch_phase = str(task.get("dispatch_phase") or "")
        if dispatch_phase not in DISPATCH_PHASES:
            errors.append(f"{artifact_type} task dispatch_phase 不合法: {dispatch_phase}")
        if not task.get("material_handoff"):
            errors.append(f"{artifact_type} task 缺少 material_handoff")
        prompt = str(task.get("prompt") or "")
        if "Do not write, modify, assemble, or export final Markdown." not in prompt:
            errors.append(f"{artifact_type} task prompt 缺少终稿写作边界")
        if "Return only JSON" not in prompt:
            errors.append(f"{artifact_type} task prompt 缺少 JSON-only 输出要求")
        if "Do not modify repository files or meeting-note files" not in prompt:
            errors.append(f"{artifact_type} task prompt 缺少文件写入边界")
    if produced_artifacts != expected_set:
        missing_producers = sorted(expected_set - produced_artifacts)
        unexpected_producers = sorted(produced_artifacts - expected_set)
        if missing_producers:
            errors.append("MAS task bundle 缺少 artifact 生产者: " + ", ".join(missing_producers))
        if unexpected_producers:
            errors.append("MAS task bundle 包含未声明 artifact 生产者: " + ", ".join(unexpected_producers))
    duplicate_producers = sorted(
        artifact_type
        for artifact_type, count in producer_counts.items()
        if count != 1
    )
    if duplicate_producers:
        errors.append("MAS task bundle artifact 必须恰好一个生产者: " + ", ".join(duplicate_producers))
    return errors


def _write_dispatch_files_unlocked(
    bundle: dict[str, Any],
    task_dir: Path,
    *,
    overwrite_prompts: bool = False,
) -> dict[str, Any]:
    task_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir = task_dir / "artifacts"
    existing_dispatch_files = [
        path
        for path in [
            task_dir / "mas_task_bundle.json",
            task_dir / "dispatch_manifest.json",
            *task_dir.glob("[0-9][0-9]-*.prompt.md"),
        ]
        if path.exists()
    ]
    if existing_dispatch_files and not overwrite_prompts:
        raise ValueError(
            "task_dir already contains dispatch files; pass the explicit overwrite option: "
            + ", ".join(path.name for path in existing_dispatch_files)
        )
    if artifact_dir.exists() and any(artifact_dir.glob("*.json")):
        raise ValueError(
            "task_dir already contains artifact JSON files; use a fresh dispatch directory "
            "or finish/repair the existing MAS run before generating a new bundle"
        )
    if overwrite_prompts:
        for path in task_dir.glob("[0-9][0-9]-*.prompt.md"):
            path.unlink()
    bundle = bind_dispatch_identity(bundle)
    bundle_path = task_dir / "mas_task_bundle.json"
    write_json(bundle_path, bundle)

    task_files: list[dict[str, str]] = []
    for index, task in enumerate(bundle.get("tasks", []), start=1):
        if not isinstance(task, dict):
            continue
        task_path = task_dir / task_file_name(index, task)
        write_text(task_path, prompt_markdown(bundle, task))
        task_files.append(
            {
                "role": str(task.get("role") or ""),
                "run_id": str(bundle.get("run_id") or ""),
                "task_id": str(task.get("task_id") or ""),
                "artifact_owner": str(task.get("artifact_owner") or task.get("role") or ""),
                "artifact_type": str(task.get("artifact_type") or ""),
                "dispatch_phase": str(task.get("dispatch_phase") or ""),
                "secondary_artifacts": [str(item) for item in task.get("secondary_artifacts", [])],
                "path": task_path.name,
            }
        )

    manifest = {
        "schema_version": "1.0",
        "run_id": str(bundle.get("run_id") or ""),
        "bundle_file": bundle_path.name,
        "mas_required": bool(bundle.get("mas_required")),
        "task_count": len(task_files),
        "task_files": task_files,
        "dispatch_phases": DISPATCH_PHASES,
        "artifact_collection": {
            "artifact_dir": "artifacts",
            "collector": "scripts/collect_mas_artifacts.py",
            "summary_file": "mas_run_summary.json",
            "combined_artifacts_file": "mas_artifacts_collected.json",
        },
        "artifact_owners": bundle.get("artifact_owners", {}),
        "dispatch_protocol": bundle.get("dispatch_protocol", {}),
        "validation": bundle.get("validation", {}),
    }
    manifest_path = task_dir / "dispatch_manifest.json"
    write_json(manifest_path, manifest)
    return {
        "task_dir": str(task_dir),
        "bundle_file": str(bundle_path),
        "manifest_file": str(manifest_path),
        "task_files": [str(task_dir / item["path"]) for item in task_files],
    }


def write_dispatch_files(
    bundle: dict[str, Any],
    task_dir: Path,
    *,
    overwrite_prompts: bool = False,
) -> dict[str, Any]:
    errors = validate_bundle(bundle, require_material_coverage=True)
    if errors:
        raise ValueError("; ".join(errors))
    with mas_task_lock(task_dir, exclusive=True):
        return _write_dispatch_files_unlocked(
            bundle,
            task_dir,
            overwrite_prompts=overwrite_prompts,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 MAS specialist task bundle")
    parser.add_argument("--request-json", help="包含 run_profile/source_mode/risk_flags 的 JSON 请求")
    parser.add_argument("--run-profile", choices=sorted(RUN_PROFILES), default=None)
    parser.add_argument("--source-mode", choices=sorted(SOURCE_MODES), default=None)
    parser.add_argument("--meeting-type", choices=sorted(MEETING_TYPES), default=None)
    parser.add_argument("--risk", action="append", default=[], help="风险标记，可重复")
    parser.add_argument("--material", action="append", default=[], help="当前会议材料路径，可重复")
    parser.add_argument("--out", help="写入 JSON 文件；默认输出到 stdout")
    parser.add_argument("--task-dir", help="写入 Codex-ready subagent prompt 文件和 dispatch manifest")
    parser.add_argument("--overwrite-dispatch", action="store_true", help="显式覆盖无 artifact 的已有 dispatch 文件")
    args = parser.parse_args()

    try:
        request: dict[str, Any] = {}
        if args.request_json:
            payload = read_json(Path(args.request_json))
            if not isinstance(payload, dict):
                raise ValueError("request-json 顶层必须是 JSON object")
            request.update(payload)
        if args.run_profile:
            request["run_profile"] = args.run_profile
        if args.source_mode:
            request["source_mode"] = args.source_mode
        if args.meeting_type:
            request["meeting_type"] = args.meeting_type
        if args.risk:
            request["risk_flags"] = normalized_flags(request.get("risk_flags", [])) + normalized_flags(args.risk)
        if args.material:
            existing_materials = request.get("materials", [])
            if not isinstance(existing_materials, list):
                raise ValueError("materials 必须是 JSON array")
            request["materials"] = [*existing_materials, *args.material]

        bundle = build_bundle_from_request(request)
        errors = validate_bundle(
            bundle,
            require_material_coverage=bool(args.request_json or args.material or args.task_dir),
        )
        if errors:
            print(json.dumps({"ok": False, "errors": errors}, ensure_ascii=False, indent=2))
            return 1
        dispatch_files: dict[str, Any] | None = None
        if args.task_dir:
            dispatch_files = write_dispatch_files(
                bundle,
                Path(args.task_dir),
                overwrite_prompts=bool(args.overwrite_dispatch),
            )
            bound_bundle = read_json(Path(dispatch_files["bundle_file"]))
            if not isinstance(bound_bundle, dict):
                raise ValueError("写入后的 MAS task bundle 顶层必须是 JSON object")
            bundle = bound_bundle
        output_payload = dict(bundle)
        if dispatch_files:
            output_payload["dispatch_files"] = dispatch_files
        if args.out:
            write_json(Path(args.out), output_payload)
        if not args.out:
            print(json.dumps(output_payload, ensure_ascii=False, indent=2))
        return 0
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        print(json.dumps({"ok": False, "errors": [f"MAS task bundle 生成失败: {exc}"]}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
