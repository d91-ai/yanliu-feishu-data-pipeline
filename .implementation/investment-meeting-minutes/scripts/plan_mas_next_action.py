#!/usr/bin/env python3
"""Turn a MAS collector next_action into an executable main-workflow plan."""

from __future__ import annotations

import argparse
import json
import shlex
from pathlib import Path
from typing import Any

from collect_mas_artifacts import PHASE_ORDER, collect_mas_run

MAIN_ACTION_SPECS: dict[str, dict[str, Any]] = {
    "ask_user_to_confirm_primary_body_source": {
        "automation_level": "user_confirmation_required",
        "inputs": ["source_reconciliation.primary_body_source", "source_reconciliation.conflicts"],
        "main_workflow_action": "Ask a narrow question to confirm the primary body source before final writing.",
        "output": "confirmed primary_body_source decision or unresolved source conflict note",
    },
    "resolve_or_record_source_conflicts": {
        "automation_level": "main_review_required",
        "inputs": ["source_reconciliation.conflicts", "source_reconciliation.coverage_findings"],
        "main_workflow_action": "Resolve conflicts from current-session evidence or record them as unresolved.",
        "output": "source conflict decision recorded in process notes or doubtful handling",
    },
    "mark_unresolved_business_items_as_doubtful": {
        "automation_level": "main_workflow_apply",
        "inputs": ["entity_verification_report.unresolved_items", "doubtful_items"],
        "main_workflow_action": "Mark unresolved non-person business items as doubtful in final handling.",
        "output": "final doubtful table rows and matching verification sidecar candidates",
    },
    "keep_conflicting_entities_doubtful": {
        "automation_level": "main_workflow_apply",
        "inputs": ["entity_verification_report.conflicts", "doubtful_items"],
        "main_workflow_action": "Keep conflicting entity facts doubtful instead of choosing an unsupported candidate.",
        "output": "doubtful table entries for conflicting entity evidence",
    },
    "derive_final_doubtful_table_from_doubtful_items": {
        "automation_level": "main_workflow_apply",
        "inputs": ["doubtful_items"],
        "main_workflow_action": "Derive the final ambiguity table only from validated doubtful_items.",
        "output": "final Markdown doubtful table and optional verification sidecar records",
    },
    "ask_user_only_for_flagged_doubtful_items": {
        "automation_level": "user_confirmation_required",
        "inputs": ["doubtful_items"],
        "main_workflow_action": "Ask only about doubtful_items explicitly marked for user confirmation.",
        "output": "narrow user answer or unresolved item retained as doubtful",
    },
    "revise_draft_before_final_validation": {
        "automation_level": "main_workflow_apply",
        "inputs": ["target_attribution_review", "fidelity_review", "current draft", "source spans"],
        "main_workflow_action": "Revise the draft from source-backed review findings before final validation.",
        "output": "main-workflow-owned revised Markdown draft",
    },
    "keep_unverified_parts_doubtful_or_unconfirmed": {
        "automation_level": "main_workflow_apply",
        "inputs": ["export_manifest.known_unverified_parts", "doubtful_items"],
        "main_workflow_action": "Keep known unverified parts doubtful or explicitly unconfirmed.",
        "output": "final doubtful or unconfirmed handling in Markdown and sidecar",
    },
    "repair_export_or_validator_failure": {
        "automation_level": "repair_required",
        "inputs": ["export_manifest.export_status", "export_manifest.validators_run"],
        "main_workflow_action": "Repair export or validator failures before final delivery.",
        "output": "passing validator/export evidence or explicit blocked state",
    },
    "repair_transcript_before_draft": {
        "automation_level": "repair_required",
        "inputs": ["transcript_audit"],
        "main_workflow_action": "Repair or rerun transcription before drafting.",
        "output": "new transcript evidence and a replacement transcript_audit artifact",
    },
    "ask_user_about_transcript_quality": {
        "automation_level": "user_confirmation_required",
        "inputs": ["transcript_audit"],
        "main_workflow_action": "Ask a narrow question about the transcript-quality blocker.",
        "output": "user confirmation or transcript repair decision",
    },
    "continue_without_user_intervention": {
        "automation_level": "automatic_continue",
        "inputs": ["validated MAS artifacts"],
        "main_workflow_action": "Continue the main workflow without asking the user.",
        "output": "next main-workflow step",
    },
}


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def quote_command(parts: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in parts)


def collector_command(task_dir: Path, phase: str | None = None) -> str:
    script_path = Path(__file__).with_name("collect_mas_artifacts.py")
    parts = ["python3", str(script_path), str(task_dir)]
    if phase:
        parts.extend(["--through-phase", phase])
    parts.append("--json")
    return quote_command(parts)


def receipt_command(task_dir: Path) -> str:
    script_path = Path(__file__).with_name("record_mas_main_actions.py")
    return quote_command(
        [
            "python3",
            str(script_path),
            "--task-dir",
            str(task_dir),
            "--markdown-path",
            "<main-owned-markdown-after-actions>",
            "--json",
        ]
    )


def ingest_command(task_dir: Path, artifact_type: str, phase: str | None = None) -> str:
    script_path = Path(__file__).with_name("ingest_mas_artifact.py")
    returned_name = f"<returned-json-for-{artifact_type}>"
    parts = ["python3", str(script_path), returned_name, "--task-dir", str(task_dir)]
    if phase:
        parts.extend(["--through-phase", phase])
    parts.append("--json")
    return quote_command(parts)


def prompt_path(task_dir: Path, relative_path: str) -> str:
    if not relative_path:
        return ""
    path = Path(relative_path)
    if path.is_absolute():
        return str(path)
    return str(task_dir / path)


def plan_status_for(action_type: str) -> str:
    if action_type == "collect_or_dispatch_phase_artifacts":
        return "dispatch_or_collect_phase"
    if action_type in {
        "repair_missing_artifacts",
        "repair_invalid_or_duplicate_artifacts",
        "repair_before_continue",
        "repair_before_final_delivery",
    }:
        return "repair_before_continue"
    if action_type == "ask_user_for_narrow_confirmation":
        return "ask_user"
    if action_type in {"apply_main_actions_before_final_delivery", "apply_main_actions_before_final_verification"}:
        return "apply_main_actions"
    if action_type == "continue_without_user_intervention":
        return "continue"
    return "inspect_summary"


def main_action_checklist(main_actions: list[str]) -> list[dict[str, Any]]:
    checklist: list[dict[str, Any]] = []
    for action in main_actions:
        spec = MAIN_ACTION_SPECS.get(action)
        if spec is None:
            spec = {
                "automation_level": "main_review_required",
                "inputs": ["MAS artifacts"],
                "main_workflow_action": "Review and apply the named main action without delegating final Markdown writing.",
                "output": "main-workflow-owned decision",
            }
        checklist.append(
            {
                "main_action": action,
                "owner": "Main Orchestrator",
                "final_markdown_owner": "Main Orchestrator",
                **spec,
            }
        )
    return checklist


def plan_from_summary(summary: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    if summary.get("schema_version") != "1.0":
        errors.append(f"MAS run summary schema_version 不符合预期: {summary.get('schema_version')}")
    next_action = summary.get("next_action")
    if not isinstance(next_action, dict):
        next_action = {}
        errors.append("MAS run summary 缺少 next_action object")

    task_dir = Path(str(summary.get("task_dir") or "."))
    action_type = str(next_action.get("type") or "")
    phase = str(next_action.get("phase") or "")
    phase_for_commands = phase if phase in PHASE_ORDER else None
    task_files = next_action.get("task_files")
    if not isinstance(task_files, list):
        task_files = []

    dispatch_tasks: list[dict[str, str]] = []
    for item in task_files:
        if not isinstance(item, dict):
            errors.append("MAS next_action task_files item 必须是 JSON object")
            continue
        artifact_type = str(item.get("artifact_type") or "")
        relative_prompt = str(item.get("path") or "")
        dispatch_tasks.append(
            {
                "artifact_type": artifact_type,
                "role": str(item.get("role") or ""),
                "dispatch_phase": str(item.get("dispatch_phase") or ""),
                "prompt_file": relative_prompt,
                "prompt_path": prompt_path(task_dir, relative_prompt),
                "ingest_command": ingest_command(task_dir, artifact_type, phase_for_commands),
            }
        )

    main_owned_missing = [
        str(item)
        for item in next_action.get("main_owned_missing_artifacts", [])
        if str(item)
    ]
    missing_artifacts = [str(item) for item in next_action.get("missing_artifacts", []) if str(item)]
    main_actions = [str(item) for item in next_action.get("main_actions", []) if str(item)]
    checklist = main_action_checklist(main_actions)
    repair_errors = [str(item) for item in next_action.get("errors", []) if str(item)]

    recommended_steps: list[dict[str, Any]] = []
    if action_type == "collect_or_dispatch_phase_artifacts":
        for task in dispatch_tasks:
            recommended_steps.append(
                {
                    "action": "dispatch_subagent_prompt",
                    "artifact_type": task["artifact_type"],
                    "role": task["role"],
                    "prompt_path": task["prompt_path"],
                }
            )
            recommended_steps.append(
                {
                    "action": "ingest_returned_artifact",
                    "artifact_type": task["artifact_type"],
                    "command": task["ingest_command"],
                }
            )
        for artifact in main_owned_missing:
            recommended_steps.append(
                {
                    "action": "create_main_owned_artifact",
                    "artifact_type": artifact,
                    "destination": str(task_dir / "artifacts" / f"{artifact}.json"),
                }
            )
        recommended_steps.append(
            {
                "action": "run_collector_after_phase_inputs",
                "command": collector_command(task_dir, phase_for_commands),
            }
        )
    elif action_type in {
        "repair_missing_artifacts",
        "repair_invalid_or_duplicate_artifacts",
        "repair_before_continue",
        "repair_before_final_delivery",
    }:
        recommended_steps.append(
            {
                "action": action_type,
                "errors": repair_errors or [str(item) for item in summary.get("errors", [])],
                "missing_artifacts": missing_artifacts,
                "main_actions": main_actions,
                "main_action_checklist": checklist,
            }
        )
    elif action_type == "ask_user_for_narrow_confirmation":
        recommended_steps.append(
            {
                "action": "ask_user_for_narrow_confirmation",
                "main_actions": main_actions,
                "main_action_checklist": checklist,
            }
        )
    elif action_type == "apply_main_actions_before_final_verification":
        recommended_steps.append(
            {
                "action": "apply_main_actions_before_final_verification",
                "main_actions": main_actions,
                "main_action_checklist": checklist,
            }
        )
        recommended_steps.append(
            {
                "action": "record_main_action_receipt",
                "command": receipt_command(task_dir),
            }
        )
        recommended_steps.append(
            {
                "action": "rerun_collector_before_final_verification",
                "command": collector_command(task_dir, "draft_review"),
            }
        )
    elif action_type in {"apply_main_actions_before_final_delivery", "continue_without_user_intervention"}:
        recommended_steps.append(
            {
                "action": action_type,
                "main_actions": main_actions,
                "main_action_checklist": checklist,
            }
        )
    else:
        recommended_steps.append({"action": "inspect_mas_run_summary", "next_action": next_action})

    return {
        "schema_version": "1.0",
        "ok": not errors,
        "execution_mode": "plan_only_no_side_effects",
        "summary_ok": bool(summary.get("ok")),
        "task_dir": str(task_dir),
        "next_action_type": action_type,
        "phase": phase,
        "plan_status": plan_status_for(action_type),
        "dispatch_tasks": dispatch_tasks,
        "main_owned_missing_artifacts": main_owned_missing,
        "missing_artifacts": missing_artifacts,
        "repair_errors": repair_errors,
        "main_actions": main_actions,
        "main_action_checklist": checklist,
        "collector_command": collector_command(task_dir, phase_for_commands),
        "recommended_steps": recommended_steps,
        "errors": errors,
        "warnings": [str(item) for item in summary.get("warnings", [])],
    }


def load_summary(args: argparse.Namespace) -> dict[str, Any]:
    if args.summary_json:
        payload = read_json(Path(args.summary_json))
        if not isinstance(payload, dict):
            raise ValueError(f"MAS run summary 必须是 JSON object: {args.summary_json}")
        return payload
    if not args.task_dir:
        raise ValueError("必须提供 task_dir 或 --summary-json")
    artifact_dir = Path(args.artifact_dir) if args.artifact_dir else None
    return collect_mas_run(Path(args.task_dir), artifact_dir=artifact_dir, through_phase=args.through_phase)


def main() -> int:
    parser = argparse.ArgumentParser(description="将 MAS collector next_action 转成主流程下一步执行清单")
    parser.add_argument("task_dir", nargs="?", help="包含 MAS dispatch files 和 artifacts 的任务目录")
    parser.add_argument("--summary-json", help="直接读取 collect_mas_artifacts.py 输出的 run summary")
    parser.add_argument("--artifact-dir", help="artifact JSON 目录；仅在从 task_dir 现场收集时使用")
    parser.add_argument("--through-phase", choices=sorted(PHASE_ORDER), help="现场收集时只校验截至指定 phase")
    parser.add_argument("--out", help="写入 next-action plan JSON")
    parser.add_argument("--json", action="store_true", help="输出 JSON；默认也是 JSON")
    args = parser.parse_args()

    try:
        summary = load_summary(args)
        result = plan_from_summary(summary)
    except Exception as exc:
        result = {
            "schema_version": "1.0",
            "ok": False,
            "execution_mode": "plan_only_no_side_effects",
            "errors": [f"MAS next-action plan 生成失败: {exc.__class__.__name__}: {exc}"],
            "warnings": [],
        }
    if args.out:
        write_json(Path(args.out), result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
