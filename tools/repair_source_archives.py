#!/usr/bin/env python3
"""Backup-first repair for source archive/version-retention gates.

The operator restores an archived file from the exact Base-recorded approved
source version, or retries a previously failed archive when no archive link
exists.  It never deletes or overwrites an old archive artifact.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import time
from typing import Any, Callable


WORKSPACE = Path(__file__).resolve().parents[1]


def load_module(path: Path):
    spec = importlib.util.spec_from_file_location("source_archive_repair_runtime", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load runtime: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def read_json(path: Path, default: Any):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def retry(call: Callable[[], Any], attempts: int = 4):
    delay = 1.5
    for attempt in range(1, attempts + 1):
        try:
            return call()
        except Exception:
            if attempt == attempts:
                raise
            time.sleep(delay)
            delay *= 2


def selected_state(runtime, cfg, record_id: str) -> dict[str, Any]:
    record = retry(lambda: runtime.get_bitable_record(cfg, record_id))
    fields = record.get("fields") or {}
    return {
        "record_id": record_id,
        "source_name": runtime.plain_field_value(fields.get(cfg.archive_file_name_field)),
        "archive_status": runtime.plain_field_value(fields.get(cfg.archive_status_field)),
        "version_status": runtime.plain_field_value(fields.get(runtime.FIELD_VERSION_STATUS)),
        "version_error": runtime.plain_field_value(fields.get(runtime.FIELD_VERSION_ERROR)),
        "source_url": runtime.url_from_field_value(fields.get(cfg.archive_file_link_field)),
        "archive_url": runtime.url_from_field_value(fields.get(cfg.archive_link_field)),
        "archive_time_ms": runtime.ms_from_record_time(fields.get(cfg.archive_time_field)),
        "original_time_ms": runtime.ms_from_record_time(fields.get(cfg.archive_original_time_field)),
        "baseline_url": runtime.url_from_field_value(fields.get(cfg.version_baseline_link_field)),
        "baseline_version": runtime.plain_field_value(fields.get(runtime.FIELD_BASELINE_VERSION)),
        "baseline_sha256": runtime.plain_field_value(fields.get(runtime.FIELD_BASELINE_SHA256)).lower(),
        "approved_version": runtime.plain_field_value(fields.get(runtime.FIELD_APPROVED_VERSION)),
        "approved_sha256": runtime.plain_field_value(fields.get(runtime.FIELD_APPROVED_SHA256)).lower(),
    }


def verify_state(runtime, cfg, record_id: str) -> dict[str, Any]:
    state = selected_state(runtime, cfg, record_id)
    reasons = []
    if state["archive_status"] != "已归档":
        reasons.append("archive_status")
    if state["version_status"] != runtime.VERSION_STATUS_COMPLETE:
        reasons.append("version_status")
    if not state["archive_url"]:
        reasons.append("archive_url")
        archive_hash = ""
    else:
        archive_token, archive_type = runtime.parse_drive_url(state["archive_url"])
        if archive_type != "file":
            reasons.append("archive_type")
            archive_hash = ""
        else:
            archive_hash = sha256(retry(lambda: runtime.download_drive_file_version(cfg, archive_token)))
    if not state["approved_sha256"] or archive_hash != state["approved_sha256"]:
        reasons.append("archive_approved_hash")
    if not state["baseline_url"] or not state["baseline_version"] or not state["baseline_sha256"]:
        reasons.append("baseline_fields")
    state.update({"archive_sha256": archive_hash, "ok": not reasons, "reasons": reasons})
    return state


def restore_recorded_approved(runtime, cfg, record_id: str, run_tag: str) -> dict[str, Any]:
    before = selected_state(runtime, cfg, record_id)
    if not before["source_url"] or not before["archive_url"]:
        raise RuntimeError("restore-approved requires source and archive links")
    if not before["approved_version"] or not before["approved_sha256"]:
        raise RuntimeError("restore-approved requires recorded approved version and hash")
    source_token, source_type = runtime.parse_drive_url(before["source_url"])
    old_archive_token, old_archive_type = runtime.parse_drive_url(before["archive_url"])
    if source_type != "file" or old_archive_type != "file":
        raise RuntimeError("restore-approved supports Drive files only")

    approved_content = retry(
        lambda: runtime.download_drive_file_version(cfg, source_token, before["approved_version"])
    )
    approved_hash = sha256(approved_content)
    if approved_hash != before["approved_sha256"]:
        raise RuntimeError("recorded approved version content does not match approved SHA field")
    old_archive_content = retry(
        lambda: runtime.download_drive_file_version(cfg, old_archive_token)
    )
    old_archive_hash = sha256(old_archive_content)
    if old_archive_hash == approved_hash:
        return {"status": "already_repaired", "before": before, "verified": verify_state(runtime, cfg, record_id)}

    original_ms = before["original_time_ms"]
    if original_ms is None:
        raise RuntimeError("invalid original time")
    month = runtime.month_from_ms(original_ms, cfg.archive_timezone_offset_hours)
    source_meta = retry(lambda: runtime.get_file_meta(cfg, source_token, source_type))
    source_name = str(source_meta.get("title") or before["source_name"] or source_token)

    backup_folder = retry(lambda: runtime.ensure_version_baseline_folder(cfg, month))
    old_backup_name = (
        f"{Path(source_name).stem} - 归档修复前备份 - {old_archive_token[-8:]} - "
        f"{run_tag}-{old_archive_hash[:8]}.md"
    )
    _old_backup_token, old_backup_url = retry(
        lambda: runtime.upload_version_artifact(
            cfg, backup_folder, old_backup_name, old_archive_content
        )
    )

    archive_folder = retry(
        lambda: runtime.ensure_child_folder(cfg, cfg.archive_root_folder_token, month, dry_run=False)
    )
    repaired_name = (
        f"{Path(source_name).stem} - 归档修复 - {record_id[-8:]} - "
        f"{run_tag}-{approved_hash[:8]}.md"
    )
    repaired_token, repaired_url = retry(
        lambda: runtime.upload_version_artifact(
            cfg, archive_folder, repaired_name, approved_content
        )
    )
    repaired_remote = retry(
        lambda: runtime.download_drive_file_version(cfg, repaired_token)
    )
    if sha256(repaired_remote) != approved_hash:
        raise RuntimeError("repaired archive upload hash mismatch")

    baseline_hash = before["baseline_sha256"]
    version_diff = (
        runtime.VERSION_DIFF_SAME
        if baseline_hash and baseline_hash == approved_hash
        else runtime.VERSION_DIFF_CHANGED
    )
    retry(
        lambda: runtime.update_bitable_record(
            cfg,
            record_id,
            {
                cfg.archive_status_field: "已归档",
                cfg.archive_link_field: {"text": repaired_name, "link": repaired_url},
                cfg.archive_time_field: int(time.time() * 1000),
                runtime.FIELD_APPROVED_VERSION: before["approved_version"],
                runtime.FIELD_APPROVED_SHA256: approved_hash,
                runtime.FIELD_VERSION_DIFF: version_diff,
                runtime.FIELD_VERSION_STATUS: runtime.VERSION_STATUS_COMPLETE,
                runtime.FIELD_VERSION_ERROR: "",
            },
        )
    )
    verified = verify_state(runtime, cfg, record_id)
    if not verified["ok"]:
        raise RuntimeError("post-repair verification failed: " + ",".join(verified["reasons"]))
    return {
        "status": "restored_recorded_approved_version",
        "record_id": record_id,
        "approved_version": before["approved_version"],
        "approved_sha256": approved_hash,
        "old_archive_url": before["archive_url"],
        "old_archive_sha256": old_archive_hash,
        "old_archive_backup_url": old_backup_url,
        "repaired_archive_url": repaired_url,
        "repaired_archive_name": repaired_name,
        "verified": verified,
    }


def retry_failed_archive(runtime, cfg, record_id: str) -> dict[str, Any]:
    before = selected_state(runtime, cfg, record_id)
    if before["archive_url"]:
        return {"status": "archive_link_already_exists", "verified": verify_state(runtime, cfg, record_id)}
    if not before["baseline_url"] or not before["baseline_version"] or not before["baseline_sha256"]:
        raise RuntimeError("failed archive has no complete review baseline")
    retry(
        lambda: runtime.update_bitable_record(
            cfg,
            record_id,
            {
                cfg.archive_status_field: "待归档",
                runtime.FIELD_VERSION_STATUS: runtime.VERSION_STATUS_BASELINE,
                runtime.FIELD_VERSION_DIFF: runtime.VERSION_DIFF_PENDING,
                runtime.FIELD_VERSION_ERROR: "",
            },
        )
    )
    result = retry(lambda: runtime.archive_record_with_failure_status(cfg, record_id), attempts=3)
    verified = verify_state(runtime, cfg, record_id)
    if not verified["ok"]:
        raise RuntimeError("post-archive verification failed: " + ",".join(verified["reasons"]))
    return {"status": "failed_archive_retried", "archive_result": result, "verified": verified}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("apply", "verify"))
    parser.add_argument("--archive-dir", type=Path, required=True)
    parser.add_argument("--env-file", default=".env.meeting-minutes")
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--run-tag", default="source-archive-repair")
    parser.add_argument("--restore-approved", action="append", default=[])
    parser.add_argument("--retry-archive", action="append", default=[])
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    run_root = args.run_root.resolve()
    try:
        run_root.relative_to(WORKSPACE)
    except ValueError as exc:
        raise SystemExit("run root must stay inside workspace") from exc
    if args.command == "apply" and not args.apply:
        raise SystemExit("command=apply requires --apply")
    runtime = load_module(args.archive_dir / "feishu_drive_to_bitable.py")
    cfg = runtime.read_config_from_env_file(args.archive_dir / args.env_file)
    record_modes = {
        **{record_id: "restore-approved" for record_id in args.restore_approved},
        **{record_id: "retry-archive" for record_id in args.retry_archive},
    }
    if not record_modes:
        raise SystemExit("at least one record id is required")
    result_path = run_root / "repair_results.json"
    existing = read_json(result_path, [])
    by_id = {item["record_id"]: item for item in existing if item.get("record_id")}
    if args.command == "verify":
        checks = [verify_state(runtime, cfg, record_id) for record_id in record_modes]
        summary = {"total": len(checks), "passed": sum(item["ok"] for item in checks), "checks": checks}
        write_json(run_root / "verify_results.json", summary)
        print(json.dumps(summary, ensure_ascii=False))
        return 0 if summary["passed"] == summary["total"] else 1

    for index, (record_id, mode) in enumerate(record_modes.items(), start=1):
        previous = by_id.get(record_id)
        if previous and previous.get("committed") is True:
            print(f"repaired {index}/{len(record_modes)} resume {record_id}", flush=True)
            continue
        before = selected_state(runtime, cfg, record_id)
        write_json(run_root / "before" / f"{record_id}.json", before)
        if mode == "restore-approved":
            result = restore_recorded_approved(runtime, cfg, record_id, args.run_tag)
        else:
            result = retry_failed_archive(runtime, cfg, record_id)
        item = {
            "record_id": record_id,
            "mode": mode,
            "committed": True,
            "completed_at": datetime.now().astimezone().replace(microsecond=0).isoformat(),
            **result,
        }
        by_id[record_id] = item
        write_json(result_path, [by_id[key] for key in record_modes if key in by_id])
        print(f"repaired {index}/{len(record_modes)} {record_id} mode={mode}", flush=True)
        time.sleep(0.8)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
