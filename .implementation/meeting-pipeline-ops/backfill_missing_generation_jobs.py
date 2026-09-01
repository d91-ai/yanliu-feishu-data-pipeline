#!/usr/bin/env python3
"""Plan or enqueue only missing unified-pipeline generation branches."""

from __future__ import annotations

import argparse
import importlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Mapping


ARTIFACT_FIELDS = {
    "industry_market_viewpoints": (
        "行业与市场观点MD",
        "行业与市场观点审核前MD",
        "行业与市场观点JSON",
    ),
    "structured_viewpoints": (
        "标的观点MD",
        "标的观点审核前MD",
        "标的观点JSON",
    ),
}
QUEUE_STATES = ("pending", "processing", "done", "failed", "stale")
UID_PATTERN = re.compile(r"mtg_[0-9a-f]{32}")


class BackfillError(ValueError):
    pass


def _has_file(backend: Any, fields: Mapping[str, Any], field_name: str) -> bool:
    return bool(backend._file_token(fields.get(field_name)))


def missing_artifacts(backend: Any, fields: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(
        artifact_type
        for artifact_type, required_fields in ARTIFACT_FIELDS.items()
        if any(not _has_file(backend, fields, name) for name in required_fields)
    )


def build_job(record: Mapping[str, Any], artifact_type: str, created_at: str) -> dict[str, Any]:
    if artifact_type not in ARTIFACT_FIELDS:
        raise BackfillError("artifact_type_invalid")
    meeting_uid = str(record.get("meeting_uid") or "").lower()
    record_id = str(record.get("record_id") or "")
    if not record_id or not UID_PATTERN.fullmatch(meeting_uid):
        raise BackfillError("record_identity_invalid")
    version = record.get("data_version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise BackfillError("data_version_invalid")
    source_token = str(record.get("source_file_token") or "")
    source_hash = str(record.get("source_md_sha256") or "").lower()
    if not source_token or not re.fullmatch(r"[0-9a-f]{64}", source_hash):
        raise BackfillError("source_identity_invalid")
    meeting_date = str(record.get("meeting_date") or "")
    meeting_series = str(record.get("meeting_series") or "").strip()
    meeting_type = str(record.get("meeting_type") or "").strip()
    if not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", meeting_date):
        raise BackfillError("meeting_date_invalid")
    if not meeting_series or not meeting_type:
        raise BackfillError("meeting_metadata_missing")
    job_id = f"{meeting_uid}-v{version}-{artifact_type}"
    return {
        "job_version": 1,
        "job_id": job_id,
        "state": "pending",
        "meeting_uid": meeting_uid,
        "record_id": record_id,
        "artifact_type": artifact_type,
        "data_version": version,
        "input_file_token": source_token,
        "input_md_sha256": source_hash,
        "meeting_date": meeting_date,
        "meeting_series": meeting_series,
        "meeting_type": meeting_type,
        "source_review_status": str(record.get("source_review_status") or "未审核"),
        "created_at": created_at,
    }


def existing_job_states(root: Path, job_id: str) -> tuple[str, ...]:
    return tuple(state for state in QUEUE_STATES if (root / state / f"{job_id}.json").is_file())


def load_production(worker_root: Path, env_file: Path, *, apply: bool):
    if not worker_root.is_absolute() or not worker_root.is_dir() or worker_root.is_symlink():
        raise BackfillError("worker_root_invalid")
    if not env_file.is_absolute() or not env_file.is_file() or env_file.is_symlink():
        raise BackfillError("env_file_invalid")
    for name in ("unified_worker_service.py", "feishu_backend.py", "unified_pipeline_worker.py"):
        path = worker_root / name
        if not path.is_file() or path.is_symlink():
            raise BackfillError("worker_runtime_invalid")
    sys.path.insert(0, str(worker_root))
    worker_service = importlib.import_module("unified_worker_service")
    backend_module = importlib.import_module("feishu_backend")
    worker_service.load_dotenv(env_file)
    service_config = worker_service.WorkerServiceConfig.from_env()
    backend_config = backend_module.FeishuBackendConfig.from_env()
    service_config.validate_assets()
    structured_service, contract = backend_module.load_runtime_modules(
        service_config.structured_service_root,
        service_config.pipeline_contract_path,
    )
    backend = backend_module.FeishuPipelineBackend(
        backend_config,
        apply=apply,
        service=structured_service,
        contract=contract,
    )
    if apply:
        backend._require_apply()
    return worker_service, backend_module, backend


def execute(
    backend: Any,
    backend_module: Any,
    *,
    apply: bool,
    excluded_record_ids: set[str],
) -> dict[str, Any]:
    raw_records = backend.service.list_bitable_records(
        backend.api_cfg,
        backend.config.base_token,
        backend.config.table_id,
        page_size=100,
    )
    targets: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for raw in sorted(raw_records, key=lambda item: str(item.get("record_id") or "")):
        record_id = str(raw.get("record_id") or "")
        if not record_id or record_id in excluded_record_ids:
            continue
        fields = raw.get("fields") or {}
        if not isinstance(fields, dict):
            blocked.append({"record_id": record_id, "reason": "record_fields_invalid"})
            continue
        artifacts = missing_artifacts(backend, fields)
        if not artifacts:
            continue
        try:
            fresh = backend.get_record(record_id)
        except Exception as exc:
            blocked.append(
                {
                    "record_id": record_id,
                    "reason": getattr(exc, "code", exc.__class__.__name__),
                }
            )
            continue
        fresh_fields = fresh.get("raw_fields") or {}
        artifacts = missing_artifacts(backend, fresh_fields)
        for artifact_type in artifacts:
            try:
                job = build_job(fresh, artifact_type, backend_module.utc_now())
            except BackfillError as exc:
                blocked.append(
                    {"record_id": record_id, "artifact_type": artifact_type, "reason": str(exc)}
                )
                continue
            states = existing_job_states(backend.config.generation_job_root, job["job_id"])
            if states:
                blocked.append(
                    {
                        "record_id": record_id,
                        "artifact_type": artifact_type,
                        "reason": "generation_job_already_exists",
                        "states": list(states),
                    }
                )
                continue
            targets.append(job)
    if blocked:
        return {
            "ok": False,
            "apply": apply,
            "record_count": len({item["record_id"] for item in targets}),
            "task_count": len(targets),
            "blocked": blocked,
        }
    if apply:
        pending = backend.config.generation_job_root / "pending"
        backend_module.ensure_private_directory(pending)
        for job in targets:
            backend_module.atomic_private_json(pending / f"{job['job_id']}.json", job)
    counts = {artifact_type: 0 for artifact_type in ARTIFACT_FIELDS}
    for job in targets:
        counts[job["artifact_type"]] += 1
    return {
        "ok": True,
        "apply": apply,
        "record_count": len({job["record_id"] for job in targets}),
        "task_count": len(targets),
        "artifact_counts": counts,
        "job_ids": [job["job_id"] for job in targets],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-root", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--exclude-record-id", action="append", default=[])
    parser.add_argument("--apply", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _worker_service, backend_module, backend = load_production(
        args.worker_root.resolve(), args.env_file.resolve(), apply=args.apply
    )
    result = execute(
        backend,
        backend_module,
        apply=args.apply,
        excluded_record_ids=set(args.exclude_record_id),
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
