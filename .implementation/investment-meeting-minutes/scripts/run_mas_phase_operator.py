#!/usr/bin/env python3
"""Run one repeatable MAS operator loop without dispatching subagents."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from build_mas_task_bundle import build_bundle_from_request, read_json, validate_bundle, write_dispatch_files
from collect_mas_artifacts import PHASE_ORDER, collect_mas_snapshot_unlocked
from create_mas_source_manifest import create_source_manifest, source_manifest_artifact
from ingest_mas_artifact import ingest_mas_artifact_file
from mas_task_lock import mas_task_lock
from plan_mas_next_action import plan_from_summary


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def default_path(task_dir: Path, explicit: str | None, filename: str) -> Path:
    return Path(explicit) if explicit else task_dir / filename


def prepare_dispatch(task_dir: Path, request_path: Path | None, overwrite_dispatch: bool) -> dict[str, Any]:
    task_dir.mkdir(parents=True, exist_ok=True)
    bundle_path = task_dir / "mas_task_bundle.json"
    if request_path is None:
        if not bundle_path.exists():
            return {
                "created": False,
                "errors": [f"missing MAS task bundle: {bundle_path}"],
                "warnings": [],
            }
        return {"created": False, "errors": [], "warnings": [], "bundle_file": str(bundle_path)}

    if bundle_path.exists() and not overwrite_dispatch:
        return {
            "created": False,
            "errors": [
                f"task_dir already has mas_task_bundle.json; omit --request-json or pass --overwrite-dispatch: {task_dir}"
            ],
            "warnings": [],
            "bundle_file": str(bundle_path),
        }

    try:
        request = read_json(request_path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {
            "created": False,
            "errors": [f"request-json cannot be read or parsed: {request_path}: {exc}"],
            "warnings": [],
        }
    if not isinstance(request, dict):
        return {"created": False, "errors": [f"request-json must be a JSON object: {request_path}"], "warnings": []}
    bundle = build_bundle_from_request(request)
    errors = validate_bundle(bundle)
    if errors:
        return {"created": False, "errors": errors, "warnings": []}
    try:
        dispatch_files = write_dispatch_files(bundle, task_dir, overwrite_prompts=overwrite_dispatch)
    except (OSError, ValueError) as exc:
        return {"created": False, "errors": [f"dispatch files cannot be written: {exc}"], "warnings": []}
    return {"created": True, "errors": [], "warnings": [], **dispatch_files}


def _auto_write_source_manifest_unlocked(task_dir: Path, request_path: Path | None) -> dict[str, Any]:
    artifact_path = task_dir / "artifacts" / "source_manifest.json"
    if artifact_path.exists():
        return {
            "enabled": True,
            "status": "already_exists",
            "artifact_file": str(artifact_path),
            "errors": [],
            "warnings": [],
        }
    # Always use the bound dispatch bundle. A raw request has no run_id and is
    # not the authoritative post-dispatch context.
    context_path = task_dir / "mas_task_bundle.json"
    context = read_json(context_path)
    if not isinstance(context, dict):
        return {
            "enabled": True,
            "status": "failed",
            "artifact_file": "",
            "errors": [f"source_manifest context must be a JSON object: {context_path}"],
            "warnings": [],
        }
    manifest, warnings = create_source_manifest(context)
    run_id = str(context.get("run_id") or "")
    if not run_id:
        raise ValueError("source_manifest context missing dispatch run_id")
    write_json(artifact_path, source_manifest_artifact(manifest, run_id))
    return {
        "enabled": True,
        "status": "written",
        "artifact_file": str(artifact_path),
        "errors": [],
        "warnings": warnings,
    }


def auto_write_source_manifest(task_dir: Path, request_path: Path | None) -> dict[str, Any]:
    with mas_task_lock(task_dir, exclusive=True):
        return _auto_write_source_manifest_unlocked(task_dir, request_path)


def operator_status(plan: dict[str, Any], ingest_results: list[dict[str, Any]]) -> str:
    if any(not bool(result.get("ok")) for result in ingest_results):
        return "repair_return_artifacts"
    if not bool(plan.get("ok")):
        return "inspect_plan"
    plan_status = str(plan.get("plan_status") or "")
    if plan_status == "repair_before_continue":
        return "repair_before_continue"
    if plan_status == "dispatch_or_collect_phase":
        has_dispatch = bool(plan.get("dispatch_tasks"))
        has_main_owned = bool(plan.get("main_owned_missing_artifacts"))
        if has_dispatch and has_main_owned:
            return "prepare_main_owned_and_dispatch_subagents"
        if has_main_owned:
            return "create_main_owned_artifacts"
        if has_dispatch:
            return "dispatch_subagent_tasks"
        return "collect_phase_artifacts"
    if plan_status == "ask_user":
        return "ask_user"
    if plan_status == "apply_main_actions":
        return "apply_main_actions"
    if plan_status == "continue":
        return "continue_main_workflow"
    return "inspect_summary"


def stop_reason_for(status: str) -> str:
    reasons = {
        "repair_return_artifacts": "invalid_or_duplicate_return_artifacts_need_repair",
        "inspect_plan": "next_action_plan_failed",
        "repair_before_continue": "collector_requires_artifact_repair_before_continue",
        "prepare_main_owned_and_dispatch_subagents": "waiting_for_main_owned_artifacts_and_subagent_returns",
        "create_main_owned_artifacts": "waiting_for_main_owned_artifacts",
        "dispatch_subagent_tasks": "waiting_for_subagent_returns",
        "collect_phase_artifacts": "waiting_for_phase_artifact_collection",
        "ask_user": "user_confirmation_required",
        "apply_main_actions": "main_workflow_must_apply_actions",
        "continue_main_workflow": "continue_without_user_intervention",
    }
    return reasons.get(status, "inspect_operator_state")


def run_mas_phase_operator(
    task_dir: Path,
    request_path: Path | None = None,
    return_paths: list[Path] | None = None,
    through_phase: str | None = None,
    summary_out: Path | None = None,
    combined_out: Path | None = None,
    plan_out: Path | None = None,
    state_out: Path | None = None,
    overwrite_dispatch: bool = False,
    auto_source_manifest: bool = False,
    replace_existing: bool = False,
) -> dict[str, Any]:
    task_dir = task_dir.expanduser()
    return_paths = return_paths or []
    errors: list[str] = []
    warnings: list[str] = []

    dispatch = prepare_dispatch(task_dir, request_path, overwrite_dispatch)
    errors.extend(str(error) for error in dispatch.get("errors", []))
    warnings.extend(str(warning) for warning in dispatch.get("warnings", []))

    source_manifest_result = {"enabled": False, "status": "not_requested", "errors": [], "warnings": []}
    if auto_source_manifest and not errors:
        try:
            source_manifest_result = auto_write_source_manifest(task_dir, request_path)
        except Exception as exc:
            source_manifest_result = {
                "enabled": True,
                "status": "failed",
                "artifact_file": "",
                "errors": [f"auto source_manifest failed: {exc.__class__.__name__}: {exc}"],
                "warnings": [],
            }
        errors.extend(str(error) for error in source_manifest_result.get("errors", []))
        warnings.extend(str(warning) for warning in source_manifest_result.get("warnings", []))

    ingest_results: list[dict[str, Any]] = []
    if not errors:
        for return_path in return_paths:
            result = ingest_mas_artifact_file(
                return_path,
                task_dir,
                through_phase=through_phase,
                replace_existing=replace_existing,
            )
            ingest_results.append(result)
            warnings.extend(str(warning) for warning in result.get("warnings", []))

    summary: dict[str, Any] = {}
    plan: dict[str, Any] = {}
    combined_errors: list[str] = []
    summary_path = default_path(task_dir, str(summary_out) if summary_out else None, "mas_run_summary.json")
    combined_path = default_path(
        task_dir,
        str(combined_out) if combined_out else None,
        "mas_artifacts_collected.json",
    )
    plan_path = default_path(task_dir, str(plan_out) if plan_out else None, "mas_next_action_plan.json")
    state_path = default_path(task_dir, str(state_out) if state_out else None, "mas_operator_state.json")

    with mas_task_lock(task_dir, exclusive=True):
        if not errors:
            summary, combined_payload, combined_errors = collect_mas_snapshot_unlocked(
                task_dir,
                through_phase=through_phase,
            )
            write_json(summary_path, summary)
            write_json(combined_path, combined_payload)
            plan = plan_from_summary(summary)
            write_json(plan_path, plan)
            warnings.extend(combined_errors)

        status = operator_status(plan, ingest_results) if plan else "inspect_operator_state"
        result_errors = errors + [
            str(error)
            for item in ingest_results
            for error in item.get("errors", [])
            if not item.get("ok")
        ]
        command_ok = not result_errors and bool(plan.get("ok", False))
        gate_ok = bool(summary.get("ok")) if summary else False
        complete = gate_ok and status == "continue_main_workflow" and str(plan.get("phase") or "") == "complete"
        result = {
            "schema_version": "1.0",
            "ok": command_ok,
            "command_ok": command_ok,
            "gate_ok": gate_ok,
            "complete": complete,
            "execution_mode": "operator_harness_no_subagent_dispatch_no_final_markdown",
            "task_dir": str(task_dir),
            "through_phase": through_phase or "complete",
            "dispatch": dispatch,
            "auto_source_manifest": source_manifest_result,
            "ingested_return_count": len(return_paths),
            "ingest_results": ingest_results,
            "collector_ok": bool(summary.get("ok")) if summary else False,
            "collector_summary_file": str(summary_path),
            "combined_artifacts_file": str(combined_path),
            "next_action_plan_file": str(plan_path),
            "operator_state_file": str(state_path),
            "operator_status": status,
            "stop_reason": stop_reason_for(status),
            "plan_status": plan.get("plan_status") if plan else "",
            "next_action_type": plan.get("next_action_type") if plan else "",
            "phase": plan.get("phase") if plan else "",
            "dispatch_tasks": plan.get("dispatch_tasks", []) if plan else [],
            "main_owned_missing_artifacts": plan.get("main_owned_missing_artifacts", []) if plan else [],
            "repair_errors": plan.get("repair_errors", []) if plan else [],
            "main_actions": plan.get("main_actions", []) if plan else [],
            "main_action_checklist": plan.get("main_action_checklist", []) if plan else [],
            "summary": summary,
            "plan": plan,
            "errors": result_errors,
            "warnings": warnings,
        }
        write_json(state_path, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one MAS operator loop over a dispatch directory")
    parser.add_argument("--task-dir", required=True, help="MAS dispatch directory")
    parser.add_argument("--request-json", help="Initialize the dispatch directory from a MAS request JSON")
    parser.add_argument("--return-json", action="append", default=[], help="Returned artifact JSON; may repeat")
    parser.add_argument("--through-phase", choices=sorted(PHASE_ORDER), help="Collector phase gate to evaluate")
    parser.add_argument("--summary-out", help="Write collector summary JSON")
    parser.add_argument("--combined-out", help="Write combined artifacts JSON")
    parser.add_argument("--plan-out", help="Write next-action plan JSON")
    parser.add_argument("--state-out", help="Write operator state JSON")
    parser.add_argument("--overwrite-dispatch", action="store_true", help="Allow replacing an existing dispatch bundle")
    parser.add_argument("--replace-existing", action="store_true", help="Archive and replace same-task returned artifacts")
    parser.add_argument("--auto-source-manifest", action="store_true", help="Create source_manifest if missing")
    parser.add_argument("--json", action="store_true", help="Print JSON; default is also JSON")
    args = parser.parse_args()

    try:
        result = run_mas_phase_operator(
            task_dir=Path(args.task_dir),
            request_path=Path(args.request_json) if args.request_json else None,
            return_paths=[Path(path) for path in args.return_json],
            through_phase=args.through_phase,
            summary_out=Path(args.summary_out) if args.summary_out else None,
            combined_out=Path(args.combined_out) if args.combined_out else None,
            plan_out=Path(args.plan_out) if args.plan_out else None,
            state_out=Path(args.state_out) if args.state_out else None,
            overwrite_dispatch=bool(args.overwrite_dispatch),
            auto_source_manifest=bool(args.auto_source_manifest),
            replace_existing=bool(args.replace_existing),
        )
    except Exception as exc:
        result = {
            "schema_version": "1.0",
            "ok": False,
            "command_ok": False,
            "gate_ok": False,
            "complete": False,
            "execution_mode": "operator_harness_no_subagent_dispatch_no_final_markdown",
            "errors": [f"MAS phase operator failed: {exc.__class__.__name__}: {exc}"],
            "warnings": [],
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
