#!/usr/bin/env python3
"""Record main-owned MAS actions against the exact Markdown and source artifacts."""

from __future__ import annotations

import argparse
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from collect_mas_artifacts import collect_artifact_files, merge_artifact_files
from mas_task_lock import mas_task_lock
from validate_mas_artifacts import artifact_set_digest, file_sha256, read_json, validate_payload


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _record_main_actions_unlocked(
    task_dir: Path,
    markdown_path: Path,
    summary_path: Path | None = None,
    replace: bool = False,
) -> dict[str, Any]:
    task_dir = task_dir.expanduser()
    summary_path = summary_path or task_dir / "mas_run_summary.json"
    bundle = read_json(task_dir / "mas_task_bundle.json")
    summary = read_json(summary_path)
    if not isinstance(bundle, dict) or not isinstance(summary, dict):
        raise ValueError("MAS bundle and run summary must be JSON objects")
    run_id = str(bundle.get("run_id") or "")
    if not run_id:
        raise ValueError("MAS bundle missing run_id")

    next_action = summary.get("next_action")
    if not isinstance(next_action, dict):
        raise ValueError("MAS run summary missing next_action")
    actions = sorted({str(item).strip() for item in next_action.get("main_actions", []) if str(item).strip()})
    if not actions:
        raise ValueError("MAS next_action has no main_actions to record")

    markdown_path = markdown_path.expanduser().resolve()
    if not markdown_path.is_file():
        raise FileNotFoundError(f"main-action Markdown does not exist: {markdown_path}")

    artifact_dir = task_dir / "artifacts"
    artifacts, _, merge_errors, _ = merge_artifact_files(collect_artifact_files(artifact_dir))
    if merge_errors:
        raise ValueError("cannot record main actions with invalid artifacts: " + "; ".join(merge_errors))
    receipt = {
        "run_id": run_id,
        "actions": actions,
        "status": "applied",
        "markdown_path": str(markdown_path),
        "markdown_sha256": file_sha256(markdown_path),
        "source_artifact_digest": artifact_set_digest(artifacts),
    }
    payload = {
        "run_id": run_id,
        "task_id": f"{run_id}:main:main_action_receipt",
        "dispatch_phase": "draft_review",
        "artifact_owner": "Main Orchestrator",
        "artifact_type": "main_action_receipt",
        "artifact": receipt,
    }
    validation = validate_payload(payload, required_artifacts=["main_action_receipt"])
    if not validation.get("ok"):
        raise ValueError("main_action_receipt validation failed: " + "; ".join(validation.get("errors", [])))

    output_path = artifact_dir / "main_action_receipt.json"
    archived_path = ""
    if output_path.exists():
        if not replace:
            raise FileExistsError(f"main_action_receipt already exists; pass --replace to supersede it: {output_path}")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        archive_path = task_dir / "repair_history" / (
            f"{stamp}-{uuid.uuid4().hex[:12]}-main_action_receipt-superseded.json"
        )
        write_json(archive_path, read_json(output_path))
        archived_path = str(archive_path)
    write_json(output_path, payload)
    return {
        "schema_version": "1.0",
        "ok": True,
        "task_dir": str(task_dir),
        "run_id": run_id,
        "artifact_file": str(output_path),
        "archived_receipt_file": archived_path,
        "actions": actions,
        "markdown_path": str(markdown_path),
        "markdown_sha256": receipt["markdown_sha256"],
        "source_artifact_digest": receipt["source_artifact_digest"],
        "errors": [],
    }


def record_main_actions(
    task_dir: Path,
    markdown_path: Path,
    summary_path: Path | None = None,
    replace: bool = False,
) -> dict[str, Any]:
    task_dir = task_dir.expanduser()
    with mas_task_lock(task_dir, exclusive=True):
        return _record_main_actions_unlocked(
            task_dir,
            markdown_path,
            summary_path=summary_path,
            replace=replace,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Record applied MAS main actions against the current Markdown")
    parser.add_argument("--task-dir", required=True, help="MAS dispatch directory")
    parser.add_argument("--markdown-path", required=True, help="Main-owned Markdown after applying listed actions")
    parser.add_argument("--summary-json", help="MAS run summary; defaults to task-dir/mas_run_summary.json")
    parser.add_argument("--replace", action="store_true", help="Archive and replace an existing receipt")
    parser.add_argument("--json", action="store_true", help="Print JSON; default is also JSON")
    args = parser.parse_args()
    try:
        result = record_main_actions(
            Path(args.task_dir),
            Path(args.markdown_path),
            summary_path=Path(args.summary_json) if args.summary_json else None,
            replace=bool(args.replace),
        )
    except Exception as exc:
        result = {
            "schema_version": "1.0",
            "ok": False,
            "errors": [f"record MAS main actions failed: {exc.__class__.__name__}: {exc}"],
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
