#!/usr/bin/env python3
"""Collect MAS specialist artifacts from a dispatch directory."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from create_mas_source_manifest import material_coverage_errors, normalize_material
from build_mas_task_bundle import PRIMARY_SOURCE_ALIASES_BY_MODE
from mas_task_lock import mas_task_lock
from summarize_mas_decisions import summarize_payload
from validate_meeting_minutes_contract import (
    read_verification_records,
    validate_contract,
    validate_verification_sidecar,
)
from validate_mas_artifacts import (
    artifact_mapping,
    artifact_set_digest,
    file_sha256,
    forbidden_field_errors,
    has_items,
    read_json,
    validate_dispatch_context,
    validate_dispatch_identity,
    validate_payload,
)

PHASE_ORDER = {"pre_draft": 0, "draft_review": 1, "final_verification": 2}
BODY_MATERIAL_KINDS_BY_MODE = {
    "audio_only": {"audio"},
    "document_only": {"document"},
    "audio_plus_document": {"audio", "document"},
}
SOURCE_ALIAS_KINDS = {
    "aligned_transcript": {"audio"},
    "audio_transcript": {"audio"},
    "document": {"document"},
    "provided_document": {"document"},
    "provided_transcript": {"document"},
    "transcript": {"audio", "document"},
}


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def required_artifacts_for_phase(bundle: dict[str, Any], through_phase: str | None = None) -> list[str]:
    expected = [str(item) for item in bundle.get("expected_artifacts", [])]
    if not through_phase:
        return expected
    if through_phase not in PHASE_ORDER:
        raise ValueError(f"through_phase 必须是以下之一: {', '.join(PHASE_ORDER)}")
    phase_index = PHASE_ORDER[through_phase]
    required: set[str] = set()
    if "source_manifest" in expected:
        required.add("source_manifest")
    for task in bundle.get("tasks", []):
        if not isinstance(task, dict):
            continue
        task_phase = str(task.get("dispatch_phase") or "")
        if task_phase not in PHASE_ORDER or PHASE_ORDER[task_phase] > phase_index:
            continue
        artifact_type = str(task.get("artifact_type") or "")
        if artifact_type:
            required.add(artifact_type)
        for secondary in task.get("secondary_artifacts", []):
            required.add(str(secondary))
    return sorted(required & set(expected))


def collect_artifact_files(artifact_dir: Path) -> list[Path]:
    if not artifact_dir.exists():
        return []
    return sorted(path for path in artifact_dir.glob("*.json") if path.is_file())


def merge_artifact_files(paths: list[Path]) -> tuple[dict[str, Any], list[dict[str, str]], list[str], list[dict[str, str]]]:
    artifacts: dict[str, Any] = {}
    artifact_sources: list[dict[str, str]] = []
    source_by_artifact: dict[str, str] = {}
    errors: list[str] = []
    duplicates: list[dict[str, str]] = []
    for path in paths:
        try:
            payload = read_json(path)
        except Exception as exc:
            errors.append(f"无法读取 MAS artifact JSON: {path}: {exc}")
            continue
        mapping, mapping_errors = artifact_mapping(payload)
        for error in mapping_errors:
            errors.append(f"{path.name}: {error}")
        for error in forbidden_field_errors(payload, path.name):
            errors.append(error)
        for artifact_type, artifact in mapping.items():
            if artifact_type in artifacts:
                errors.append(f"重复 MAS artifact: {artifact_type}")
                duplicates.append(
                    {
                        "artifact_type": artifact_type,
                        "first_path": source_by_artifact.get(artifact_type, ""),
                        "duplicate_path": str(path),
                    }
                )
                continue
            artifacts[artifact_type] = artifact
            source_by_artifact[artifact_type] = str(path)
            artifact_sources.append({"artifact_type": artifact_type, "path": str(path)})
    return artifacts, artifact_sources, errors, duplicates


def task_records(bundle: dict[str, Any], manifest: dict[str, Any]) -> list[dict[str, Any]]:
    path_by_artifact: dict[str, str] = {}
    for item in manifest.get("task_files", []):
        if isinstance(item, dict):
            path_by_artifact[str(item.get("artifact_type") or "")] = str(item.get("path") or "")
    records: list[dict[str, Any]] = []
    for task in bundle.get("tasks", []):
        if not isinstance(task, dict):
            continue
        artifact_type = str(task.get("artifact_type") or "")
        records.append(
            {
                "artifact_type": artifact_type,
                "secondary_artifacts": [str(item) for item in task.get("secondary_artifacts", [])],
                "dispatch_phase": str(task.get("dispatch_phase") or ""),
                "role": str(task.get("role") or ""),
                "task_id": str(task.get("task_id") or ""),
                "path": path_by_artifact.get(artifact_type, ""),
            }
        )
    return records


def task_files_for_artifacts(records: list[dict[str, Any]], artifact_types: list[str]) -> list[dict[str, str]]:
    wanted = set(artifact_types)
    files: list[dict[str, str]] = []
    for record in records:
        produced = {str(record.get("artifact_type") or "")}
        produced.update(str(item) for item in record.get("secondary_artifacts", []))
        if not (produced & wanted):
            continue
        files.append(
            {
                "artifact_type": str(record.get("artifact_type") or ""),
                "dispatch_phase": str(record.get("dispatch_phase") or ""),
                "role": str(record.get("role") or ""),
                "path": str(record.get("path") or ""),
            }
        )
    return files


def phase_gates(bundle: dict[str, Any], artifacts: dict[str, Any], manifest: dict[str, Any]) -> list[dict[str, Any]]:
    artifact_types = set(artifacts)
    records = task_records(bundle, manifest)
    gates: list[dict[str, Any]] = []
    previous_required: set[str] = set()
    previous_missing: list[str] = []
    for phase in PHASE_ORDER:
        cumulative_required = required_artifacts_for_phase(bundle, phase)
        current_required = sorted(set(cumulative_required) - previous_required)
        current_missing = [artifact for artifact in current_required if artifact not in artifact_types]
        cumulative_missing = [artifact for artifact in cumulative_required if artifact not in artifact_types]
        if previous_missing:
            status = "blocked_by_previous_phase"
        elif current_missing:
            status = "ready_for_dispatch_or_collection"
        else:
            status = "complete"
        missing_task_files = task_files_for_artifacts(records, current_missing)
        task_file_artifacts = {
            artifact
            for task_file in missing_task_files
            for artifact in [task_file["artifact_type"]]
            if artifact
        }
        main_owned_missing = [
            artifact
            for artifact in current_missing
            if artifact not in task_file_artifacts
            and not any(artifact in record.get("secondary_artifacts", []) for record in records)
        ]
        gates.append(
            {
                "phase": phase,
                "status": status,
                "required_artifacts": cumulative_required,
                "current_phase_required_artifacts": current_required,
                "missing_artifacts": cumulative_missing,
                "current_phase_missing_artifacts": current_missing,
                "missing_task_files": missing_task_files,
                "main_owned_missing_artifacts": main_owned_missing,
            }
        )
        previous_required = set(cumulative_required)
        previous_missing = cumulative_missing
    return gates


def action_names(decision: dict[str, Any]) -> list[str]:
    excluded = {
        "continue_without_user_intervention",
        "repair_export_or_validator_failure",
        "ask_user_to_confirm_primary_body_source",
        "ask_user_only_for_flagged_doubtful_items",
    }
    return sorted(
        {
            str(item).strip()
            for item in decision.get("main_actions", [])
            if str(item).strip() and str(item).strip() not in excluded
        }
    )


def resolve_runtime_path(task_dir: Path, value: Any) -> Path:
    path = Path(str(value or "")).expanduser()
    return path if path.is_absolute() else task_dir / path


def main_action_receipt_state(
    artifacts: dict[str, Any],
    bundle: dict[str, Any],
    task_dir: Path,
    required_actions: list[str],
) -> dict[str, Any]:
    if not required_actions:
        return {"required": False, "valid": True, "errors": [], "recorded_actions": []}
    receipt = artifacts.get("main_action_receipt")
    if not isinstance(receipt, dict):
        return {
            "required": True,
            "valid": False,
            "errors": ["缺少 main_action_receipt，主流程动作尚未绑定到当前 Markdown"],
            "recorded_actions": [],
        }
    errors: list[str] = []
    run_id = str(bundle.get("run_id") or "")
    if str(receipt.get("run_id") or "") != run_id:
        errors.append("main_action_receipt.run_id 与当前 dispatch 不匹配")
    recorded_actions = sorted({str(item).strip() for item in receipt.get("actions", []) if str(item).strip()})
    missing_actions = sorted(set(required_actions) - set(recorded_actions))
    if missing_actions:
        errors.append("main_action_receipt 未覆盖当前动作: " + ", ".join(missing_actions))
    expected_digest = artifact_set_digest(artifacts)
    if str(receipt.get("source_artifact_digest") or "") != expected_digest:
        errors.append("main_action_receipt.source_artifact_digest 已过期")
    markdown_path = resolve_runtime_path(task_dir, receipt.get("markdown_path"))
    if not markdown_path.is_file():
        errors.append(f"main_action_receipt Markdown 不存在: {markdown_path}")
    elif file_sha256(markdown_path) != str(receipt.get("markdown_sha256") or ""):
        errors.append("main_action_receipt Markdown 哈希与当前文件不一致")
    return {
        "required": True,
        "valid": not errors,
        "errors": errors,
        "recorded_actions": recorded_actions,
        "markdown_path": str(markdown_path),
        "markdown_sha256": str(receipt.get("markdown_sha256") or ""),
    }


def export_binding_errors(
    artifacts: dict[str, Any],
    bundle: dict[str, Any],
    task_dir: Path,
    receipt_state: dict[str, Any],
) -> list[str]:
    export_manifest = artifacts.get("export_manifest")
    if not isinstance(export_manifest, dict):
        return []
    errors: list[str] = []
    markdown_path = resolve_runtime_path(task_dir, export_manifest.get("markdown_path"))
    if not markdown_path.is_file():
        errors.append(f"export_manifest Markdown 不存在: {markdown_path}")
        return errors
    actual_hash = file_sha256(markdown_path)
    if actual_hash != str(export_manifest.get("markdown_sha256") or ""):
        errors.append("export_manifest.markdown_sha256 与当前 Markdown 不一致")
    if receipt_state.get("required"):
        if not receipt_state.get("valid"):
            errors.append("export_manifest 不得建立在无效 main_action_receipt 上")
        elif actual_hash != str(receipt_state.get("markdown_sha256") or ""):
            errors.append("export_manifest 验证的 Markdown 与 main_action_receipt 不是同一版本")
        if export_manifest.get("main_actions_verified") is not True:
            errors.append("export_manifest.main_actions_verified 必须确认主流程动作已反映在终稿")
    try:
        markdown = markdown_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        errors.append(f"export_manifest Markdown 无法按 UTF-8 读取: {exc}")
    else:
        if "\ufffd" in markdown:
            errors.append("export_manifest Markdown 包含 Unicode 替换字符 U+FFFD")
        contract_result = validate_contract(
            markdown,
            source_mode=str(bundle.get("source_mode") or "auto"),
            timestamp_mode="auto",
        )
        errors.extend(
            f"export_manifest Markdown contract: {error}"
            for error in contract_result.get("errors", [])
        )
    known_unverified = export_manifest.get("known_unverified_parts")
    if has_items(known_unverified):
        sidecar_path_value = str(export_manifest.get("verification_sidecar_path") or "").strip()
        if not sidecar_path_value:
            errors.append("export_manifest 存在 known_unverified_parts 时必须提供 verification_sidecar_path")
        else:
            sidecar_path = resolve_runtime_path(task_dir, sidecar_path_value)
            sidecar_result = validate_verification_sidecar(sidecar_path, require_verification=True)
            for error in sidecar_result.get("errors", []):
                errors.append(f"export_manifest verification sidecar: {error}")
    return errors


def artifact_context_errors(
    artifacts: dict[str, Any],
    bundle: dict[str, Any],
    task_dir: Path,
) -> list[str]:
    """Bind specialist claims to the current dispatch and its sidecar files."""
    errors: list[str] = []
    source_manifest = artifacts.get("source_manifest")
    if isinstance(source_manifest, dict):
        if source_manifest.get("source_mode") != bundle.get("source_mode"):
            errors.append("source_manifest.source_mode 与当前 MAS task bundle 不一致")
        expected_materials = [normalize_material(item) for item in bundle.get("materials", [])]
        actual_materials = source_manifest.get("materials")
        expected_keys = {
            (str(item.get("kind") or ""), str(item.get("name") or ""))
            for item in expected_materials
        }
        actual_keys = {
            (str(item.get("kind") or ""), str(item.get("name") or ""))
            for item in actual_materials
            if isinstance(actual_materials, list) and isinstance(item, dict)
        } if isinstance(actual_materials, list) else set()
        if expected_keys != actual_keys:
            errors.append("source_manifest.materials 与当前 MAS task bundle 不一致")
        errors.extend(
            f"source_manifest {error}"
            for error in material_coverage_errors(
                str(bundle.get("source_mode") or ""),
                actual_materials if isinstance(actual_materials, list) else [],
            )
        )

    reconciliation = artifacts.get("source_reconciliation")
    if isinstance(reconciliation, dict):
        primary = str(reconciliation.get("primary_body_source") or "").strip()
        cross_check = str(reconciliation.get("cross_check_source") or "").strip()
        source_mode = str(bundle.get("source_mode") or "")
        eligible_kinds = BODY_MATERIAL_KINDS_BY_MODE.get(source_mode, set())
        material_kinds_by_reference: dict[str, set[str]] = {}
        for material in bundle.get("materials", []):
            normalized = normalize_material(material)
            kind = str(normalized.get("kind") or "")
            name = str(normalized.get("name") or "")
            if kind not in eligible_kinds or not name:
                continue
            for reference in {name, Path(name).stem}:
                material_kinds_by_reference.setdefault(reference, set()).add(kind)
        allowed = PRIMARY_SOURCE_ALIASES_BY_MODE.get(source_mode, set()) | set(material_kinds_by_reference)

        def source_kinds(value: str) -> set[str]:
            return SOURCE_ALIAS_KINDS.get(value, set()) | material_kinds_by_reference.get(value, set())

        def validate_bound_source(field: str, value: str) -> bool:
            if value.startswith(("http://", "https://", "file://")) or Path(value).is_absolute():
                if field == "primary_body_source":
                    errors.append("source_reconciliation.primary_body_source 必须引用当前会话材料，不得使用外部 URL 或绝对路径")
                else:
                    errors.append("source_reconciliation.cross_check_source 必须引用当前会话正文材料，不得使用外部 URL 或绝对路径")
                return False
            if value not in allowed:
                if field == "primary_body_source":
                    errors.append("source_reconciliation.primary_body_source 未绑定到当前会话材料或允许的来源别名")
                else:
                    errors.append("source_reconciliation.cross_check_source 未绑定到当前会话可用正文材料或允许的来源别名")
                return False
            return True

        primary_bound = bool(primary) and validate_bound_source("primary_body_source", primary)
        cross_bound = bool(cross_check) and validate_bound_source("cross_check_source", cross_check)
        if source_mode == "audio_plus_document" and reconciliation.get("manual_review_required") is False:
            if not cross_check:
                errors.append("source_reconciliation audio_plus_document 自动继续时 cross_check_source 不得为空")
            elif primary_bound and cross_bound:
                primary_kinds = source_kinds(primary)
                cross_kinds = source_kinds(cross_check)
                if len(primary_kinds) != 1 or len(cross_kinds) != 1 or primary_kinds == cross_kinds:
                    errors.append("source_reconciliation audio_plus_document 自动继续时主源与交叉源必须来自不同且明确的证据侧")

    export_manifest = artifacts.get("export_manifest")
    doubtful_items = artifacts.get("doubtful_items")
    if isinstance(export_manifest, dict) and isinstance(doubtful_items, list):
        expected_sidecar_items = {
            str(item.get("原始表述") or "").strip()
            for item in doubtful_items
            if isinstance(item, dict) and item.get("是否需要 sidecar") is True
        }
        known_unverified = {
            str(item).strip()
            for item in export_manifest.get("known_unverified_parts", [])
            if str(item).strip()
        } if isinstance(export_manifest.get("known_unverified_parts"), list) else set()
        if expected_sidecar_items != known_unverified:
            errors.append("export_manifest.known_unverified_parts 必须与需要 sidecar 的 doubtful_items 完全一致")
        if expected_sidecar_items:
            sidecar_path = resolve_runtime_path(task_dir, export_manifest.get("verification_sidecar_path"))
            try:
                sidecar_records = read_verification_records(sidecar_path)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
                sidecar_records = []
            if any(
                not str(item.get("原始表述") or "").strip()
                for item in sidecar_records
                if isinstance(item, dict)
            ):
                errors.append("verification sidecar 原始表述不得为空")
            sidecar_items = {
                str(item.get("原始表述") or "").strip()
                for item in sidecar_records
                if isinstance(item, dict) and str(item.get("原始表述") or "").strip()
            }
            if sidecar_items != expected_sidecar_items:
                errors.append("verification sidecar 原始表述必须与需要 sidecar 的 doubtful_items 完全一致")
            selected_doubtful_records = [
                item
                for item in doubtful_items
                if isinstance(item, dict) and item.get("是否需要 sidecar") is True
            ]
            selected_sidecar_records = [
                item
                for item in sidecar_records
                if isinstance(item, dict) and str(item.get("原始表述") or "").strip()
            ]
            doubtful_by_raw = {
                str(item.get("原始表述") or "").strip(): item
                for item in selected_doubtful_records
            }
            sidecar_by_raw = {
                str(item.get("原始表述") or "").strip(): item
                for item in selected_sidecar_records
            }
            if len(selected_doubtful_records) != len(doubtful_by_raw) or len(selected_sidecar_records) != len(sidecar_by_raw):
                errors.append("doubtful_items 与 verification sidecar 的原始表述必须各自唯一")
            for raw in sorted(expected_sidecar_items & sidecar_items):
                for field in (
                    "存疑类型",
                    "当前判断",
                    "候选项",
                    "是否需要 sidecar",
                    "上下文依据",
                    "检索/证据路径",
                    "最终处理",
                ):
                    if doubtful_by_raw[raw].get(field) != sidecar_by_raw[raw].get(field):
                        errors.append(f"verification sidecar 与 doubtful_items 字段不一致: {raw} -> {field}")
    return errors


def artifact_identity_errors(
    paths: list[Path],
    bundle: dict[str, Any],
    manifest: dict[str, Any],
    through_phase: str | None,
) -> list[str]:
    errors: list[str] = []
    for path in paths:
        try:
            payload = read_json(path)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            errors.append(f"无法校验 MAS artifact identity: {path}: {exc}")
            continue
        for error in validate_dispatch_identity(
            payload,
            bundle,
            manifest,
            through_phase=through_phase,
            phase_order=PHASE_ORDER,
            allow_internal_split=True,
        ):
            errors.append(f"{path.name}: {error}")
    return errors


def next_action(
    gates: list[dict[str, Any]],
    decision: dict[str, Any],
    errors: list[str],
    missing_artifacts: list[str],
    receipt_state: dict[str, Any],
) -> dict[str, Any]:
    missing_errors = {f"缺少必需 artifact: {artifact}" for artifact in missing_artifacts}
    non_missing_errors = [error for error in errors if error not in missing_errors]
    if non_missing_errors:
        return {
            "type": "repair_invalid_or_duplicate_artifacts",
            "phase": "",
            "missing_artifacts": missing_artifacts,
            "errors": non_missing_errors,
        }
    decision_type = str(decision.get("decision") or "")
    if decision.get("ok") and decision_type == "repair_required":
        main_actions = [str(item) for item in decision.get("main_actions", [])]
        if "repair_transcript_before_draft" in main_actions:
            return {
                "type": "repair_before_continue",
                "phase": "pre_draft",
                "main_actions": main_actions,
            }
        return {
            "type": "repair_before_final_delivery",
            "phase": "final_verification",
            "main_actions": main_actions,
        }
    if decision.get("ok") and decision_type == "request_user":
        return {
            "type": "ask_user_for_narrow_confirmation",
            "phase": "",
            "main_actions": decision.get("main_actions", []),
        }
    required_actions = action_names(decision) if decision.get("ok") else []
    for gate in gates:
        if gate["status"] in {"ready_for_dispatch_or_collection", "blocked_by_previous_phase"}:
            if gate["phase"] == "final_verification" and required_actions and not receipt_state.get("valid"):
                return {
                    "type": "apply_main_actions_before_final_verification",
                    "phase": "draft_review",
                    "main_actions": required_actions,
                    "receipt_errors": receipt_state.get("errors", []),
                    "requires_final_verification_rerun": "export_manifest" in gate.get("required_artifacts", []),
                }
            return {
                "type": "collect_or_dispatch_phase_artifacts",
                "phase": gate["phase"],
                "missing_artifacts": gate["missing_artifacts"],
                "current_phase_missing_artifacts": gate["current_phase_missing_artifacts"],
                "task_files": gate["missing_task_files"],
                "main_owned_missing_artifacts": gate["main_owned_missing_artifacts"],
            }
    if errors:
        action_type = "repair_missing_artifacts" if missing_artifacts else "repair_invalid_or_duplicate_artifacts"
        return {
            "type": action_type,
            "phase": "",
            "missing_artifacts": missing_artifacts,
            "errors": errors,
        }
    if required_actions and not receipt_state.get("valid"):
        return {
            "type": "apply_main_actions_before_final_verification",
            "phase": "final_verification",
            "main_actions": required_actions,
            "receipt_errors": receipt_state.get("errors", []),
            "requires_final_verification_rerun": True,
        }
    if decision_type == "automatic_pass":
        return {
            "type": "continue_without_user_intervention",
            "phase": "complete",
            "main_actions": decision.get("main_actions", []),
        }
    if decision_type == "automatic_doubtful":
        return {
            "type": "continue_without_user_intervention",
            "phase": "complete",
            "main_actions": [],
            "applied_main_actions": receipt_state.get("recorded_actions", []),
        }
    return {
        "type": "inspect_mas_run_summary",
        "phase": "complete",
        "main_actions": decision.get("main_actions", []),
    }


def _collect_mas_run_unlocked(
    task_dir: Path,
    artifact_dir: Path | None = None,
    through_phase: str | None = None,
) -> dict[str, Any]:
    task_dir = task_dir.expanduser()
    artifact_dir = artifact_dir.expanduser() if artifact_dir else task_dir / "artifacts"
    bundle_path = task_dir / "mas_task_bundle.json"
    manifest_path = task_dir / "dispatch_manifest.json"
    errors: list[str] = []
    warnings: list[str] = []
    pending_transactions = sorted(artifact_dir.glob(".mas-ingest-txn-*")) if artifact_dir.exists() else []
    if pending_transactions:
        errors.append(
            "存在未完成 MAS artifact 事务，必须先通过 ingest 恢复: "
            + ", ".join(path.name for path in pending_transactions)
        )

    if not bundle_path.exists():
        errors.append(f"缺少 MAS task bundle: {bundle_path}")
        bundle: dict[str, Any] = {}
    else:
        try:
            bundle_payload = read_json(bundle_path)
            if not isinstance(bundle_payload, dict):
                errors.append(f"MAS task bundle 顶层必须是 JSON object: {bundle_path}")
                bundle = {}
            else:
                bundle = bundle_payload
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            errors.append(f"无法读取 MAS task bundle: {bundle_path}: {exc}")
            bundle = {}

    manifest: dict[str, Any] = {}
    if manifest_path.exists():
        try:
            manifest_payload = read_json(manifest_path)
            if isinstance(manifest_payload, dict):
                manifest = manifest_payload
            else:
                errors.append(f"MAS dispatch manifest 顶层必须是 JSON object: {manifest_path}")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            errors.append(f"无法读取 MAS dispatch manifest: {manifest_path}: {exc}")
    else:
        warnings.append(f"缺少 MAS dispatch manifest: {manifest_path}")

    artifact_paths = collect_artifact_files(artifact_dir)
    if not artifact_paths:
        warnings.append(f"MAS artifact 目录没有 JSON 文件: {artifact_dir}")
    artifacts, artifact_sources, merge_errors, duplicate_artifacts = merge_artifact_files(artifact_paths)
    errors.extend(merge_errors)

    if not bundle:
        validation = {
            "ok": False,
            "errors": list(errors),
            "warnings": [],
            "artifact_count": len(artifacts),
            "artifact_types": sorted(artifacts),
        }
        decision = {
            "ok": False,
            "decision": "request_user",
            "reasons": ["collector_initialization_failed"],
            "main_actions": ["repair_or_regenerate_invalid_artifacts"],
            "errors": list(errors),
            "warnings": [],
            "artifact_types": sorted(artifacts),
        }
        action = next_action([], decision, errors, [], {"required": False, "valid": False, "errors": []})
        return {
            "schema_version": "1.0",
            "ok": False,
            "task_dir": str(task_dir),
            "artifact_dir": str(artifact_dir),
            "through_phase": through_phase or "complete",
            "required_artifacts": [],
            "missing_artifacts": [],
            "duplicate_artifacts": duplicate_artifacts,
            "phase_gates": [],
            "next_action": action,
            "artifact_count": len(artifacts),
            "artifact_types": sorted(artifacts),
            "artifact_sources": artifact_sources,
            "manifest_task_count": manifest.get("task_count"),
            "decision": decision,
            "validation": validation,
            "errors": errors,
            "warnings": warnings,
        }

    errors.extend(validate_dispatch_context(bundle, manifest))

    required_artifacts = required_artifacts_for_phase(bundle, through_phase=through_phase)
    errors.extend(artifact_identity_errors(artifact_paths, bundle, manifest, through_phase))
    combined_payload = {"artifacts": artifacts}
    validation = validate_payload(combined_payload, required_artifacts=required_artifacts)
    decision = summarize_payload(combined_payload, required_artifacts=required_artifacts)
    for error in validation.get("errors", []):
        if error not in errors:
            errors.append(str(error))
    for error in decision.get("errors", []):
        if error not in errors:
            errors.append(str(error))
    warnings.extend(str(warning) for warning in validation.get("warnings", []))
    warnings.extend(str(warning) for warning in decision.get("warnings", []))
    errors.extend(artifact_context_errors(artifacts, bundle, task_dir))

    missing_artifacts = [artifact for artifact in required_artifacts if artifact not in artifacts]
    gates = phase_gates(bundle, artifacts, manifest)
    receipt_state = main_action_receipt_state(
        artifacts,
        bundle,
        task_dir,
        action_names(decision) if decision.get("ok") else [],
    )
    if "main_action_receipt" in artifacts and not receipt_state.get("valid"):
        errors.extend(str(error) for error in receipt_state.get("errors", []))
    errors.extend(export_binding_errors(artifacts, bundle, task_dir, receipt_state))
    action = next_action(gates, decision, errors, missing_artifacts, receipt_state)
    return {
        "schema_version": "1.0",
        "ok": not errors and bool(validation.get("ok")) and bool(decision.get("ok")),
        "task_dir": str(task_dir),
        "artifact_dir": str(artifact_dir),
        "through_phase": through_phase or "complete",
        "required_artifacts": required_artifacts,
        "missing_artifacts": missing_artifacts,
        "duplicate_artifacts": duplicate_artifacts,
        "phase_gates": gates,
        "next_action": action,
        "artifact_count": len(artifacts),
        "artifact_types": sorted(artifacts),
        "artifact_sources": artifact_sources,
        "manifest_task_count": manifest.get("task_count"),
        "decision": decision,
        "main_action_receipt": receipt_state,
        "validation": validation,
        "errors": errors,
        "warnings": warnings,
    }


def collect_mas_run(
    task_dir: Path,
    artifact_dir: Path | None = None,
    through_phase: str | None = None,
) -> dict[str, Any]:
    task_dir = task_dir.expanduser()
    with mas_task_lock(task_dir, exclusive=False):
        return _collect_mas_run_unlocked(
            task_dir,
            artifact_dir=artifact_dir,
            through_phase=through_phase,
        )


def combined_payload_from_summary(summary: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    source_paths = [
        Path(str(item.get("path") or ""))
        for item in summary.get("artifact_sources", [])
        if isinstance(item, dict) and item.get("path")
    ]
    artifacts, _, errors, _ = merge_artifact_files(source_paths)
    payload: dict[str, Any] = {"artifacts": artifacts}
    if not summary.get("ok"):
        payload.update(
            {
                "ok": False,
                "errors": summary.get("errors", []),
                "missing_artifacts": summary.get("missing_artifacts", []),
                "duplicate_artifacts": summary.get("duplicate_artifacts", []),
                "source_summary": {
                    "task_dir": summary.get("task_dir"),
                    "through_phase": summary.get("through_phase"),
                },
            }
        )
    return payload, errors


def collect_mas_snapshot_unlocked(
    task_dir: Path,
    artifact_dir: Path | None = None,
    through_phase: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    summary = _collect_mas_run_unlocked(
        task_dir.expanduser(),
        artifact_dir=artifact_dir,
        through_phase=through_phase,
    )
    payload, errors = combined_payload_from_summary(summary)
    return summary, payload, errors


def main() -> int:
    parser = argparse.ArgumentParser(description="收集 MAS subagent artifacts 并生成主流程决策摘要")
    parser.add_argument("task_dir", help="包含 mas_task_bundle.json 和 dispatch_manifest.json 的派发目录")
    parser.add_argument("--artifact-dir", help="artifact JSON 目录；默认 task_dir/artifacts")
    parser.add_argument("--through-phase", choices=sorted(PHASE_ORDER), help="只校验截至指定 phase 的必需 artifact")
    parser.add_argument("--out", help="写入 run summary JSON；默认输出到 stdout")
    parser.add_argument("--combined-out", help="写入合并后的 artifacts JSON")
    parser.add_argument("--json", action="store_true", help="输出 JSON；默认也是 JSON")
    args = parser.parse_args()

    task_dir = Path(args.task_dir).expanduser()
    artifact_dir = Path(args.artifact_dir).expanduser() if args.artifact_dir else None
    with mas_task_lock(task_dir, exclusive=True):
        result, combined_payload, combined_errors = collect_mas_snapshot_unlocked(
            task_dir,
            artifact_dir=artifact_dir,
            through_phase=args.through_phase,
        )
        result["warnings"].extend(str(error) for error in combined_errors)
        if args.combined_out:
            write_json(Path(args.combined_out), combined_payload)
        if args.out:
            write_json(Path(args.out), result)
    if args.json or not args.out:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
