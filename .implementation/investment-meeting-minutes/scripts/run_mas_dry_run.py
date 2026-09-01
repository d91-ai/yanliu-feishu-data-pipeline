#!/usr/bin/env python3
"""Create a deterministic staged MAS dry run from synthetic artifacts."""

from __future__ import annotations

import argparse
import copy
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from build_mas_task_bundle import build_bundle_from_request, validate_bundle, write_dispatch_files
from collect_mas_artifacts import collect_mas_run, merge_artifact_files, required_artifacts_for_phase
from record_mas_main_actions import record_main_actions
from validate_mas_artifacts import file_sha256

PHASES = ("pre_draft", "draft_review", "final_verification")
DRY_RUN_MARKER = ".mas-dry-run-marker"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def synthetic_final_markdown(artifacts: dict[str, Any]) -> str:
    doubtful_items = artifacts.get("doubtful_items")
    first = (
        doubtful_items[0]
        if isinstance(doubtful_items, list) and doubtful_items and isinstance(doubtful_items[0], dict)
        else None
    )
    lines = [
        "# 投资会议纪要｜合成 MAS dry-run",
        "",
        "**会议日期**：2032-07-11",
        "**整理时间**：2032-07-11",
        "**会议标题**：合成 MAS dry-run 会议",
        "**会议类型**：多人复盘会",
        "**会议系列**：合成回归",
        "",
        "---",
        "",
        "## 一、发言整理",
        "",
        "### 发言人1",
        "",
        "#### 【科技｜合成回归】",
        "",
    ]
    if first is None:
        lines.append("我按当前会话原文保留这段合成回归内容。")
    else:
        raw = str(first.get("原始表述") or "合成存疑词").replace("|", "\\|")
        current = str(first.get("当前判断") or "待人工确认").replace("|", "\\|")
        candidate = str(first.get("候选项") or "").replace("|", "\\|")
        lines.extend(
            [
                f"我在当前会话中提到 **{raw}**，需要保留原始存疑。",
                "",
                "## 二、存疑与待确认",
                "",
                "| 原始表述 | 当前判断 | 候选项 | 人工确认 |",
                "| --- | --- | --- | --- |",
                f"| {raw} | {current} | {candidate} | |",
            ]
        )
    return "\n".join(lines) + "\n"


def synthetic_verification_payload(artifacts: dict[str, Any]) -> dict[str, Any]:
    doubtful_items = artifacts.get("doubtful_items")
    records = [
        copy.deepcopy(item)
        for item in doubtful_items
        if isinstance(doubtful_items, list)
        and isinstance(item, dict)
        and item.get("是否需要 sidecar") is True
    ] if isinstance(doubtful_items, list) else []
    return {"records": records}


def can_overwrite_task_dir(task_dir: Path) -> bool:
    resolved = task_dir.expanduser().resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    if temp_root not in resolved.parents and resolved != temp_root:
        return False
    if not resolved.name.startswith("mas-"):
        return False
    return (
        (resolved / DRY_RUN_MARKER).exists()
        or (resolved / "mas_task_bundle.json").exists()
        or (resolved / "dispatch_manifest.json").exists()
    )


def load_fixture_artifacts(path: Path) -> dict[str, Any]:
    payload = read_json(path)
    if not isinstance(payload, dict) or not isinstance(payload.get("artifacts"), dict):
        raise ValueError(f"MAS dry-run fixture must contain an artifacts object: {path}")
    return dict(payload["artifacts"])


def artifact_identity(manifest: dict[str, Any], artifact_type: str) -> dict[str, Any]:
    run_id = str(manifest.get("run_id") or "")
    if artifact_type == "source_manifest":
        return {
            "run_id": run_id,
            "task_id": f"{run_id}:main:source_manifest",
            "dispatch_phase": "pre_draft",
            "artifact_owner": "Main Orchestrator",
        }
    for task in manifest.get("task_files", []):
        if not isinstance(task, dict):
            continue
        produced = {str(task.get("artifact_type") or "")}
        produced.update(str(item) for item in task.get("secondary_artifacts", []))
        if artifact_type in produced:
            return {
                "run_id": run_id,
                "task_id": str(task.get("task_id") or ""),
                "dispatch_phase": str(task.get("dispatch_phase") or ""),
                "artifact_owner": str(task.get("artifact_owner") or task.get("role") or ""),
            }
    raise ValueError(f"MAS dry-run cannot resolve task identity for artifact: {artifact_type}")


def write_artifact(
    artifact_dir: Path,
    manifest: dict[str, Any],
    artifact_type: str,
    artifact: Any,
) -> Path:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    path = artifact_dir / f"{artifact_type}.json"
    write_json(
        path,
        {
            **artifact_identity(manifest, artifact_type),
            "artifact_type": artifact_type,
            "artifact": artifact,
        },
    )
    return path


def task_files_by_phase(dispatch_manifest: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    grouped: dict[str, list[dict[str, str]]] = {phase: [] for phase in PHASES}
    for item in dispatch_manifest.get("task_files", []):
        if not isinstance(item, dict):
            continue
        phase = str(item.get("dispatch_phase") or "")
        if phase not in grouped:
            continue
        grouped[phase].append(
            {
                "role": str(item.get("role") or ""),
                "artifact_type": str(item.get("artifact_type") or ""),
                "path": str(item.get("path") or ""),
            }
        )
    return grouped


def run_mas_dry_run(request_path: Path, artifact_fixture_path: Path, task_dir: Path) -> dict[str, Any]:
    request = read_json(request_path)
    if not isinstance(request, dict):
        raise ValueError(f"MAS dry-run request must be a JSON object: {request_path}")
    fixture_artifacts = copy.deepcopy(load_fixture_artifacts(artifact_fixture_path))
    bundle = build_bundle_from_request(request)
    errors = validate_bundle(bundle)

    dispatch_result = write_dispatch_files(bundle, task_dir)
    manifest_path = Path(dispatch_result["manifest_file"])
    manifest = read_json(manifest_path)
    bound_bundle = read_json(Path(dispatch_result["bundle_file"]))
    if not isinstance(bound_bundle, dict) or not isinstance(manifest, dict):
        raise ValueError("MAS dry-run dispatch bundle and manifest must be JSON objects")
    bundle = bound_bundle
    artifact_dir = task_dir / "artifacts"
    synthetic_markdown = task_dir / "synthetic-final.md"
    synthetic_markdown.write_text(synthetic_final_markdown(fixture_artifacts), encoding="utf-8")
    write_json(
        task_dir / "synthetic.verification.json",
        synthetic_verification_payload(fixture_artifacts),
    )
    emitted_artifacts: set[str] = set()
    artifact_files: list[dict[str, str]] = []
    phase_results: list[dict[str, Any]] = []
    grouped_task_files = task_files_by_phase(manifest if isinstance(manifest, dict) else {})
    stop_reason = "completed"

    for phase_index, phase in enumerate(PHASES):
        required_now = required_artifacts_for_phase(bundle, phase)
        emitted_this_phase: list[str] = []
        for artifact_type in required_now:
            if artifact_type in emitted_artifacts:
                continue
            if artifact_type not in fixture_artifacts:
                errors.append(f"MAS dry-run fixture missing artifact: {artifact_type}")
                continue
            artifact = copy.deepcopy(fixture_artifacts[artifact_type])
            if artifact_type == "export_manifest" and isinstance(artifact, dict):
                artifact["markdown_path"] = str(synthetic_markdown)
                artifact["markdown_sha256"] = file_sha256(synthetic_markdown)
                artifact["main_actions_verified"] = True
            artifact_path = write_artifact(artifact_dir, manifest, artifact_type, artifact)
            emitted_artifacts.add(artifact_type)
            emitted_this_phase.append(artifact_type)
            artifact_files.append({"artifact_type": artifact_type, "path": str(artifact_path)})

        summary = collect_mas_run(task_dir, through_phase=phase)
        summary_path = task_dir / f"mas_run_summary.{phase}.json"
        write_json(summary_path, summary)
        next_action = summary.get("next_action", {})
        receipt_result: dict[str, Any] | None = None
        if next_action.get("type") == "apply_main_actions_before_final_verification":
            receipt_result = record_main_actions(
                task_dir,
                synthetic_markdown,
                summary_path=summary_path,
                replace=(artifact_dir / "main_action_receipt.json").exists(),
            )
            summary = collect_mas_run(task_dir, through_phase=phase)
            write_json(summary_path, summary)
            next_action = summary.get("next_action", {})
        phase_results.append(
            {
                "phase": phase,
                "task_files": grouped_task_files.get(phase, []),
                "emitted_artifacts": emitted_this_phase,
                "summary_file": str(summary_path),
                "collector_ok": bool(summary.get("ok")),
                "next_action": next_action,
                "main_action_receipt": receipt_result,
                "phase_gates": summary.get("phase_gates", []),
                "errors": summary.get("errors", []),
            }
        )
        if not summary.get("ok"):
            stop_reason = f"collector_not_ok:{phase}"
            break
        if phase_index < len(PHASES) - 1 and next_action.get("type") != "collect_or_dispatch_phase_artifacts":
            stop_reason = f"next_action_not_phase_dispatch:{next_action.get('type')}"
            break

    final_summary = collect_mas_run(task_dir)
    final_summary_path = task_dir / "mas_run_summary.json"
    write_json(final_summary_path, final_summary)
    combined_path = task_dir / "mas_artifacts_collected.json"

    collector_ok = all(bool(phase.get("collector_ok")) for phase in phase_results) and bool(final_summary.get("ok"))
    source_paths = [
        Path(str(item.get("path") or ""))
        for item in final_summary.get("artifact_sources", [])
        if isinstance(item, dict) and item.get("path")
    ]
    combined_artifacts, _, combined_errors, _ = merge_artifact_files(source_paths)
    errors.extend(combined_errors)
    combined_payload: dict[str, Any] = {"artifacts": combined_artifacts}
    if errors or not collector_ok:
        combined_payload.update(
            {
                "ok": False,
                "errors": errors + [str(error) for error in final_summary.get("errors", [])],
                "missing_artifacts": final_summary.get("missing_artifacts", []),
                "duplicate_artifacts": final_summary.get("duplicate_artifacts", []),
                "source_summary": {
                    "task_dir": str(task_dir),
                    "through_phase": final_summary.get("through_phase"),
                    "stop_reason": stop_reason,
                },
            }
        )
    write_json(combined_path, combined_payload)
    return {
        "schema_version": "1.0",
        "ok": not errors and collector_ok,
        "execution_mode": "deterministic_fixture_artifact_returns",
        "request_file": str(request_path),
        "artifact_fixture_file": str(artifact_fixture_path),
        "task_dir": str(task_dir),
        "bundle_file": dispatch_result["bundle_file"],
        "manifest_file": dispatch_result["manifest_file"],
        "artifact_dir": str(artifact_dir),
        "artifact_files": artifact_files,
        "phase_order": list(PHASES),
        "completed_phase_order": [str(phase["phase"]) for phase in phase_results],
        "stop_reason": stop_reason,
        "phases": phase_results,
        "final_summary_file": str(final_summary_path),
        "combined_artifacts_file": str(combined_path),
        "final_next_action": final_summary.get("next_action", {}),
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a staged MAS dry run from synthetic specialist artifacts")
    parser.add_argument("--request-json", required=True, help="MAS task request JSON")
    parser.add_argument("--artifact-fixture", required=True, help="Synthetic MAS artifacts JSON fixture")
    parser.add_argument("--task-dir", required=True, help="Output dispatch/dry-run directory")
    parser.add_argument("--out", help="Write dry-run trace JSON")
    parser.add_argument("--overwrite", action="store_true", help="Remove an existing task-dir before writing")
    parser.add_argument("--json", action="store_true", help="Print JSON")
    args = parser.parse_args()

    try:
        task_dir = Path(args.task_dir).expanduser()
        if task_dir.exists() and any(task_dir.iterdir()):
            if not args.overwrite:
                raise ValueError(f"task-dir is not empty; pass --overwrite to replace it: {task_dir}")
            if not can_overwrite_task_dir(task_dir):
                raise ValueError(
                    "refusing to overwrite task-dir without MAS dry-run marker or prior MAS temp outputs: "
                    f"{task_dir}"
                )
            shutil.rmtree(task_dir)
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / DRY_RUN_MARKER).write_text("mas dry-run workspace\n", encoding="utf-8")

        result = run_mas_dry_run(Path(args.request_json), Path(args.artifact_fixture), task_dir)
    except Exception as exc:
        result = {
            "schema_version": "1.0",
            "ok": False,
            "execution_mode": "deterministic_fixture_artifact_returns",
            "errors": [f"MAS dry-run failed: {exc.__class__.__name__}: {exc}"],
        }
    if args.out:
        write_json(Path(args.out), result)
    if args.json or not args.out:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
