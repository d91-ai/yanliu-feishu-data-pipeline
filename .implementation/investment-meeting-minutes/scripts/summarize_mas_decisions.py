#!/usr/bin/env python3
"""Summarize MAS artifact bundles into main-orchestrator decisions."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from validate_mas_artifacts import artifact_mapping, read_json, validate_payload

REQUEST_USER_PREFIXES = ("请求人工确认", "请求用户确认", "需人工确认", "需用户确认")


def as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    return [value]


def has_items(value: Any) -> bool:
    return bool(as_list(value))


def doubtful_items_request_user(value: Any) -> bool:
    if not isinstance(value, list):
        return False
    for item in value:
        if not isinstance(item, dict):
            continue
        for field in ("当前判断", "最终处理"):
            text = str(item.get(field) or "").strip()
            if text.startswith(REQUEST_USER_PREFIXES):
                return True
    return False


def add_action(actions: list[str], action: str) -> None:
    if action not in actions:
        actions.append(action)


def status_indicates_failure(value: Any) -> bool:
    if isinstance(value, bool):
        return value is False
    if isinstance(value, dict):
        if not value:
            return True
        for key in ("ok", "success", "passed"):
            if value.get(key) is False:
                return True
        for key in ("errors", "failures"):
            if has_items(value.get(key)):
                return True
        for key in ("status", "result", "state", "conclusion"):
            if key in value and status_indicates_failure(value.get(key)):
                return True
        return False
    if isinstance(value, list):
        return not value or any(status_indicates_failure(item) for item in value)
    text = str(value or "").strip().lower()
    if not text:
        return True
    return text in {
        "false",
        "failed",
        "failure",
        "blocked",
        "not_run",
        "not run",
        "not-run",
        "skipped",
        "not_started",
        "未运行",
        "失败",
        "错误",
        "阻塞",
    }


def summarize_payload(payload: Any, required_artifacts: list[str] | None = None) -> dict[str, Any]:
    validation = validate_payload(payload, required_artifacts=required_artifacts)
    artifacts, mapping_errors = artifact_mapping(payload)
    validation_errors = list(validation.get("errors", [])) + mapping_errors
    if validation_errors:
        return {
            "ok": False,
            "decision": "request_user",
            "reasons": ["artifact_validation_failed"],
            "main_actions": ["repair_or_regenerate_invalid_artifacts"],
            "errors": validation_errors,
            "warnings": validation.get("warnings", []),
            "artifact_types": sorted(artifacts),
        }

    reasons: list[str] = []
    actions: list[str] = []
    request_user = False
    repair_required = False

    transcript_audit = artifacts.get("transcript_audit", {})
    if isinstance(transcript_audit, dict):
        transcript_action = str(transcript_audit.get("recommended_action") or "")
        if transcript_action == "repair_transcript":
            reasons.append("transcript_repair_required")
            add_action(actions, "repair_transcript_before_draft")
            repair_required = True
        elif transcript_action == "request_user":
            reasons.append("transcript_requires_user_confirmation")
            add_action(actions, "ask_user_about_transcript_quality")
            request_user = True

    source_reconciliation = artifacts.get("source_reconciliation", {})
    if isinstance(source_reconciliation, dict):
        if source_reconciliation.get("manual_review_required") is True:
            request_user = True
            reasons.append("primary_source_requires_manual_review")
            add_action(actions, "ask_user_to_confirm_primary_body_source")
        if has_items(source_reconciliation.get("conflicts")):
            reasons.append("source_conflicts_present")
            add_action(actions, "resolve_or_record_source_conflicts")

    entity_report = artifacts.get("entity_verification_report", {})
    if isinstance(entity_report, dict):
        if has_items(entity_report.get("confirmed_items")) and not has_items(entity_report.get("external_evidence_paths")):
            reasons.append("confirmed_items_without_external_evidence")
            add_action(actions, "keep_unverified_parts_doubtful_or_unconfirmed")
        if has_items(entity_report.get("unresolved_items")):
            reasons.append("unresolved_business_items_present")
            add_action(actions, "mark_unresolved_business_items_as_doubtful")
        if has_items(entity_report.get("conflicts")):
            reasons.append("entity_evidence_conflicts_present")
            add_action(actions, "keep_conflicting_entities_doubtful")

    doubtful_items = artifacts.get("doubtful_items", [])
    if has_items(doubtful_items):
        reasons.append("doubtful_items_present")
        add_action(actions, "derive_final_doubtful_table_from_doubtful_items")
        if doubtful_items_request_user(doubtful_items):
            request_user = True
            reasons.append("doubtful_items_request_user_confirmation")
            add_action(actions, "ask_user_only_for_flagged_doubtful_items")

    for artifact_type, field_names in {
        "target_attribution_review": [
            "wrong_grouping",
            "missing_positive_targets",
            "incidental_targets_in_heading",
            "negative_targets_in_heading",
            "non_source_companies",
            "recommended_revisions",
        ],
        "fidelity_review": [
            "source_mapping_failures",
            "summary_compression_findings",
            "pronoun_rewrite_findings",
            "omission_findings",
            "recommended_revisions",
        ],
    }.items():
        artifact = artifacts.get(artifact_type, {})
        if not isinstance(artifact, dict):
            continue
        for field_name in field_names:
            if has_items(artifact.get(field_name)):
                reasons.append(f"{artifact_type}.{field_name}_present")
                add_action(actions, "revise_draft_before_final_validation")
                break

    export_manifest = artifacts.get("export_manifest", {})
    if "export_manifest" in artifacts and isinstance(export_manifest, dict):
        if export_manifest.get("main_actions_verified") is not True:
            reasons.append("main_actions_not_verified")
            add_action(actions, "repair_export_or_validator_failure")
            repair_required = True
        if not has_items(export_manifest.get("validators_run")):
            reasons.append("validators_not_run")
            add_action(actions, "repair_export_or_validator_failure")
            repair_required = True
        elif status_indicates_failure(export_manifest.get("validators_run")):
            reasons.append("validator_failure_present")
            add_action(actions, "repair_export_or_validator_failure")
            repair_required = True
        if status_indicates_failure(export_manifest.get("regression_result")):
            reasons.append("regression_not_passed")
            add_action(actions, "repair_export_or_validator_failure")
            repair_required = True
        if has_items(export_manifest.get("known_unverified_parts")):
            if export_manifest.get("main_actions_verified") is True:
                reasons.append("known_unverified_parts_already_handled")
            else:
                reasons.append("known_unverified_parts_present")
                add_action(actions, "keep_unverified_parts_doubtful_or_unconfirmed")
        if status_indicates_failure(export_manifest.get("export_status")):
            reasons.append("export_status_failed")
            add_action(actions, "repair_export_or_validator_failure")
            repair_required = True

    if repair_required:
        decision = "repair_required"
    elif request_user:
        decision = "request_user"
    elif reasons:
        decision = "automatic_doubtful"
    else:
        decision = "automatic_pass"
        add_action(actions, "continue_without_user_intervention")

    return {
        "ok": True,
        "decision": decision,
        "reasons": reasons,
        "main_actions": actions,
        "errors": [],
        "warnings": validation.get("warnings", []),
        "artifact_types": sorted(artifacts),
    }


def summarize_file(path: Path, required_artifacts: list[str] | None = None) -> dict[str, Any]:
    return summarize_payload(read_json(path), required_artifacts=required_artifacts)


def main() -> int:
    parser = argparse.ArgumentParser(description="汇总 MAS artifacts 并给出主流程决策")
    parser.add_argument("artifact_file", help="MAS artifact JSON 文件")
    parser.add_argument("--require-artifact", action="append", default=[], help="要求存在的 artifact 类型，可重复")
    parser.add_argument("--json", action="store_true", help="输出 JSON；默认也是 JSON")
    args = parser.parse_args()

    try:
        result = summarize_file(Path(args.artifact_file), required_artifacts=[str(item) for item in args.require_artifact])
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        result = {
            "ok": False,
            "decision": "request_user",
            "reasons": ["artifact_file_unreadable"],
            "main_actions": ["repair_or_regenerate_invalid_artifacts"],
            "errors": [f"MAS artifact 文件无法读取或解析: {exc}"],
            "warnings": [],
            "artifact_types": [],
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
