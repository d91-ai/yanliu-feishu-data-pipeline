#!/usr/bin/env python3
"""Regenerate every eligible reviewed meeting note with structured schema v6.

The operator is deliberately split into prepare/generate/apply/verify phases.
Only ``apply --apply`` mutates Feishu.  Every apply is resumable per source
record and overwrites the currently linked Markdown file when one exists.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Callable, TypeVar


WORKSPACE = Path(__file__).resolve().parents[1]
GENERATION_SCHEMA_VERSION = 6
DEFAULT_RUN_ROOT = WORKSPACE / "outputs/structured-regeneration-schema-v6"
T = TypeVar("T")

svc: Any = None
worker: Any = None
arch: Any = None
RUN_ROOT = DEFAULT_RUN_ROOT
MANIFEST_PATH = RUN_ROOT / "manifest.json"
APPLY_RESULTS_PATH = RUN_ROOT / "apply_results.json"
VERIFY_RESULTS_PATH = RUN_ROOT / "verify_results.json"
RUN_TAG = "schema-v6-rerun"
ARCHIVE_DIR = Path()
SELECTED_RECORD_IDS: set[str] = set()


def load_runtime_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"runtime module is unavailable: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def safe_name(value: str) -> str:
    return "".join(character if character.isalnum() or character in "_.-" else "-" for character in value).strip("-") or "record"


def retry_read(operation: Callable[[], T], *, attempts: int = 6) -> T:
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except Exception as exc:
            detail = str(exc)
            retryable = any(
                marker in detail
                for marker in (
                    "99991400",
                    "request trigger frequency limit",
                    "timed out",
                    "temporarily unavailable",
                    "HTTP 429",
                    "HTTP 502",
                    "HTTP 503",
                )
            )
            if not retryable or attempt == attempts:
                raise
            time.sleep(min(30, 2 ** attempt))
    raise RuntimeError("read retry exhausted")


def first_file_token(value: Any) -> tuple[str, str]:
    url = svc.first_url(value)
    if not url:
        return "", ""
    token, file_type = svc.parse_drive_url(url)
    if file_type != "file":
        raise RuntimeError(f"expected Drive file URL, got {file_type}: {url}")
    return token, url


def shanghai_midnight_ms(date_text: str) -> int:
    tz = timezone(timedelta(hours=8))
    return int(datetime.strptime(date_text, "%Y-%m-%d").replace(tzinfo=tz).timestamp() * 1000)


def resolve_meeting_series_for_batch(fields: dict[str, Any], source_text: str, source_name: str) -> tuple[str, str]:
    try:
        return svc.resolve_meeting_series(fields, source_text), "source_field_or_markdown"
    except Exception as exc:
        if getattr(exc, "error_code", "") != "meeting_series_missing":
            raise
    stem = source_name[:-3].strip() if source_name.lower().endswith(".md") else source_name.strip()
    parts = [part.strip() for part in stem.split(" - ") if part.strip()]
    if len(parts) >= 2 and svc.normalize_date(parts[0]):
        fallback = svc.clean_file_name_part(parts[1])
        fallback = re.sub(r"[（(]已核对[）)](?:[（(]\d+[）)])?$", "", fallback).strip()
        if fallback:
            return fallback, "source_file_name"
    raise RuntimeError("meeting_series_missing_in_record_markdown_and_file_name")


def get_configs() -> tuple[Any, Any, Any]:
    return (
        svc.read_config(),
        worker.read_config(),
        arch.read_config_from_env_file(ARCHIVE_DIR / ".env.structured"),
    )


def structured_indexes(records: list[dict[str, Any]]) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    by_uid: dict[str, list[dict[str, Any]]] = {}
    by_token: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        fields = record.get("fields") or {}
        meeting_uid = svc.plain_field_value(fields.get(svc.FIELD_MEETING_UID))
        if meeting_uid:
            by_uid.setdefault(meeting_uid, []).append(record)
        try:
            token, _url = first_file_token(fields.get(svc.FIELD_STRUCTURED_MD_LINK))
        except RuntimeError:
            token = ""
        if token:
            by_token.setdefault(token, []).append(record)
    return by_uid, by_token


def official_records_by_structured_record(
    records: list[dict[str, Any]], structured_record_ids: set[str]
) -> dict[str, list[dict[str, Any]]]:
    by_record: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        fields = record.get("fields") or {}
        value = fields.get(svc.FIELD_OFFICIAL_SOURCE_MD_RECORD)
        for record_id in structured_record_ids:
            if svc.source_record_link_matches(value, record_id):
                by_record.setdefault(record_id, []).append(record)
    return by_record


def prepare() -> int:
    cfg, _worker_cfg, _archive_cfg = get_configs()
    source_records = retry_read(
        lambda: svc.list_bitable_records(cfg, cfg.source_base_token, cfg.source_table_id, page_size=500)
    )
    structured_records = retry_read(
        lambda: svc.list_bitable_records(cfg, cfg.structured_base_token, cfg.structured_table_id, page_size=500)
    )
    official_table_id = svc.resolve_bitable_table_id(
        cfg, cfg.structured_base_token, cfg.official_json_table_id
    )
    official_records = retry_read(
        lambda: svc.list_bitable_records(cfg, cfg.structured_base_token, official_table_id, page_size=500)
    )
    by_uid, by_token = structured_indexes(structured_records)
    structured_ids = {str(record.get("record_id") or "") for record in structured_records}
    official_by_structured = official_records_by_structured_record(official_records, structured_ids)
    all_reviewed = [record for record in source_records if svc.record_review_ok(cfg, record.get("fields") or {})]
    reviewed = [
        record
        for record in all_reviewed
        if not SELECTED_RECORD_IDS or str(record.get("record_id") or "") in SELECTED_RECORD_IDS
    ]
    if SELECTED_RECORD_IDS:
        found_ids = {str(record.get("record_id") or "") for record in reviewed}
        missing_ids = sorted(SELECTED_RECORD_IDS - found_ids)
        if missing_ids:
            raise RuntimeError("selected reviewed records not found: " + ", ".join(missing_ids))
    entries: list[dict[str, Any]] = []
    blocked: list[dict[str, str]] = []

    for record in sorted(
        reviewed,
        key=lambda item: (
            svc.get_record_meeting_date(item.get("fields") or {}),
            svc.plain_field_value((item.get("fields") or {}).get(svc.FIELD_FILE_NAME)),
        ),
    ):
        record_id = str(record.get("record_id") or "")
        fields = record.get("fields") or {}
        source_name = svc.plain_field_value(fields.get(svc.FIELD_FILE_NAME))
        archive_status = svc.plain_field_value(fields.get(svc.FIELD_ARCHIVE_STATUS))
        version_status = svc.plain_field_value(fields.get(svc.FIELD_VERSION_STATUS))
        archive_url = svc.first_url(fields.get(svc.FIELD_SOURCE_ARCHIVE_LINK))
        approved_hash = svc.plain_field_value(fields.get(svc.FIELD_APPROVED_SHA256)).lower()
        if archive_status != "已归档" or version_status != "已完成" or not archive_url or not approved_hash:
            blocked.append(
                {
                    "record_id": record_id,
                    "source_name": source_name,
                    "reason": f"source_gate_not_ready archive={archive_status or '-'} version={version_status or '-'}",
                }
            )
            continue
        try:
            archive_token, archive_type = svc.parse_drive_url(archive_url)
            if archive_type != "file":
                raise RuntimeError(f"unsupported archive type: {archive_type}")
            source_content = retry_read(lambda: svc.download_drive_file(cfg, archive_token))
            actual_hash = sha256_bytes(source_content)
            if actual_hash != approved_hash:
                raise RuntimeError("approved_archive_hash_mismatch")
            source_text = source_content.decode("utf-8")
            meeting_date = svc.resolve_meeting_date(fields, source_text, source_name)
            meeting_series, meeting_series_source = resolve_meeting_series_for_batch(
                fields, source_text, source_name
            )
            meeting_uid = svc.meeting_uid_value(fields.get(svc.FIELD_MEETING_UID))
            output_name = svc.output_file_name_from_fields(meeting_date, meeting_series)
            structured_token, structured_url = first_file_token(fields.get(svc.FIELD_TABLE_LINK))
            uid_matches = by_uid.get(meeting_uid) or []
            token_matches = (by_token.get(structured_token) or []) if structured_token else []
            combined = {
                str(item.get("record_id") or ""): item for item in [*uid_matches, *token_matches]
            }
            if len(uid_matches) > 1 or len(token_matches) > 1 or len(combined) > 1:
                raise RuntimeError(
                    "structured_record_mapping_conflict "
                    f"uid={len(uid_matches)} token={len(token_matches)} combined={len(combined)}"
                )
            structured_record = next(iter(combined.values()), {})
            structured_fields = structured_record.get("fields") or {}
            structured_record_id = str(structured_record.get("record_id") or "")
            official_matches = official_by_structured.get(structured_record_id) or []
            if len(official_matches) > 1:
                raise RuntimeError(f"official_record_mapping_count={len(official_matches)}")
            official_record = official_matches[0] if official_matches else {}
            official_fields = official_record.get("fields") or {}
            if not structured_token:
                structured_token, structured_url = first_file_token(
                    structured_fields.get(svc.FIELD_STRUCTURED_MD_LINK)
                )
            old_content = retry_read(lambda: svc.download_drive_file(cfg, structured_token)) if structured_token else b""

            job_dir = RUN_ROOT / "jobs" / safe_name(record_id)
            job_dir.mkdir(parents=True, exist_ok=True)
            source_path = job_dir / "source.md"
            existing_path = job_dir / "existing_structured.md"
            source_path.write_bytes(source_content)
            if old_content:
                existing_path.write_bytes(old_content)
            entry = {
                "source_record_id": record_id,
                "source_name": source_name,
                "source_archive_url": archive_url,
                "source_archive_sha256": approved_hash,
                "meeting_uid": meeting_uid,
                "meeting_date": meeting_date,
                "meeting_series": meeting_series,
                "meeting_series_source": meeting_series_source,
                "meeting_type": svc.plain_field_value(fields.get("会议类型")),
                "output_name": output_name,
                "structured_record_id": structured_record_id,
                "structured_file_token": structured_token,
                "structured_file_url": structured_url,
                "old_cloud_sha256": sha256_bytes(old_content) if old_content else "",
                "old_structured_state": {
                    "approved": svc.checkbox_is_checked(structured_fields.get(svc.FIELD_STRUCTURED_APPROVED)),
                    "archive_status": svc.plain_field_value(structured_fields.get("归档状态")),
                    "json_status": svc.plain_field_value(structured_fields.get(svc.FIELD_STRUCTURED_JSON_STATUS)),
                    "json_link": svc.first_url(structured_fields.get(svc.FIELD_STRUCTURED_JSON_LINK)),
                    "json_row_count": svc.number_field_value(structured_fields.get(svc.FIELD_STRUCTURED_JSON_ROW_COUNT)),
                },
                "official_json_record_id": str(official_record.get("record_id") or ""),
                "old_official_json_state": {
                    "status": svc.plain_field_value(official_fields.get(svc.FIELD_OFFICIAL_STATUS)),
                    "json_link": svc.first_url(official_fields.get(svc.FIELD_OFFICIAL_JSON_LINK)),
                    "json_row_count": svc.number_field_value(official_fields.get(svc.FIELD_OFFICIAL_JSON_ROW_COUNT)),
                    "source_md_hash": svc.plain_field_value(official_fields.get(svc.FIELD_OFFICIAL_SOURCE_MD_HASH)),
                },
                "apply_mode": "update" if structured_record else "create",
                "job_dir": str(job_dir),
                "source_path": str(source_path),
                "existing_structured_path": str(existing_path),
                "output_path": str(job_dir / "structured.md"),
            }
            write_json(job_dir / "entry.json", entry)
            entries.append(entry)
            print(f"prepared {len(entries)}/{len(reviewed)} {source_name}", flush=True)
            time.sleep(0.15)
        except Exception as exc:
            blocked.append(
                {
                    "record_id": record_id,
                    "source_name": source_name,
                    "reason": " ".join(str(exc).split())[:800] or exc.__class__.__name__,
                }
            )

    manifest = {
        "schema_version": GENERATION_SCHEMA_VERSION,
        "prepared_at": svc.now_shanghai_iso(),
        "reviewed_total_count": len(all_reviewed),
        "reviewed_count": len(reviewed),
        "eligible_count": len(entries),
        "blocked": blocked,
        "entries": entries,
    }
    write_json(MANIFEST_PATH, manifest)
    print(json.dumps({"reviewed": len(reviewed), "eligible": len(entries), "blocked": blocked}, ensure_ascii=False))
    return 0 if entries else 1


def generation_entries(entries: list[dict[str, Any]], shard_count: int, shard_index: int) -> list[tuple[int, dict[str, Any]]]:
    if shard_count < 1 or shard_index < 0 or shard_index >= shard_count:
        raise RuntimeError("invalid shard selection")
    return [(index, entry) for index, entry in enumerate(entries) if index % shard_count == shard_index]


def generate(*, shard_count: int, shard_index: int) -> int:
    cfg, worker_cfg, _archive_cfg = get_configs()
    generator = load_runtime_module("structured_v6_generator", worker_cfg.skill_script)
    exporter_cfg = cfg.__class__(**{**cfg.__dict__, "skill_script": worker_cfg.skill_script})
    manifest = load_json(MANIFEST_PATH)
    entries = manifest.get("entries") or []
    selected = generation_entries(entries, shard_count, shard_index)
    failures: list[dict[str, str]] = []
    completed = 0

    for zero_index, entry in selected:
        display_index = zero_index + 1
        job_dir = Path(entry["job_dir"])
        output_path = Path(entry["output_path"])
        result_path = job_dir / "generation_result.json"
        if result_path.exists() and output_path.exists():
            existing = load_json(result_path)
            if int(existing.get("schema_version") or 0) == GENERATION_SCHEMA_VERSION:
                print(f"generated {display_index}/{len(entries)} resume {entry['source_name']}", flush=True)
                completed += 1
                continue
        print(f"generating {display_index}/{len(entries)} {entry['source_name']}", flush=True)
        try:
            worker.generate_source_fragments(worker_cfg, job_dir)
            worker.run_claim_unit_stage(worker_cfg, job_dir)
            claim_units_path = job_dir / "claim_units.json"
            metadata = {
                "model_version": worker_cfg.model_version,
                "schema_version": GENERATION_SCHEMA_VERSION,
                "skill_script_sha256": worker.sha256_file(worker_cfg.skill_script),
                "worker": "codex-local-agent",
                "completed_at": svc.now_shanghai_iso(),
            }
            write_json(job_dir / "model_metadata.json", metadata)
            row_count = svc.run_skill(
                exporter_cfg,
                source_markdown_path=Path(entry["source_path"]),
                claim_units_path=claim_units_path,
                output_path=output_path,
                source_record_id=entry["source_record_id"],
                meeting_uid=entry["meeting_uid"],
                source_archive_url=entry["source_archive_url"],
                source_file_name=entry["source_name"] + ("" if entry["source_name"].endswith(".md") else ".md"),
                meeting_date=entry["meeting_date"],
                model_version=worker_cfg.model_version,
            )
            output_text = output_path.read_text(encoding="utf-8")
            frontmatter = generator.parse_frontmatter(output_text)
            parsed_rows = generator.normalize_approved_rows(
                generator.parse_approved_markdown_rows(output_text, label=entry["source_name"]),
                meeting_date=entry["meeting_date"],
                meeting_uid=entry["meeting_uid"],
            )
            if int(frontmatter.get("schema_version") or 0) != GENERATION_SCHEMA_VERSION:
                raise RuntimeError("generated artifact is not schema v6")
            if frontmatter.get("meeting_uid") != entry["meeting_uid"]:
                raise RuntimeError("generated artifact meeting_uid mismatch")
            if len(parsed_rows) != row_count:
                raise RuntimeError(f"parsed row count mismatch expected={row_count} actual={len(parsed_rows)}")
            result = {
                "source_record_id": entry["source_record_id"],
                "row_count": row_count,
                "output_sha256": sha256_bytes(output_path.read_bytes()),
                "output_size": output_path.stat().st_size,
                "schema_version": GENERATION_SCHEMA_VERSION,
                "condition_count": sum(len(row.get("conditions") or []) for row in parsed_rows),
                **metadata,
            }
            write_json(result_path, result)
            (job_dir / "generation_error.txt").unlink(missing_ok=True)
            completed += 1
            print(f"generated {display_index}/{len(entries)} rows={row_count} {entry['source_name']}", flush=True)
        except BaseException as exc:
            if isinstance(exc, KeyboardInterrupt):
                raise
            detail = " ".join(str(exc).split())[:1600] or exc.__class__.__name__
            (job_dir / "generation_error.txt").write_text(detail + "\n", encoding="utf-8")
            failures.append({"record_id": entry["source_record_id"], "source_name": entry["source_name"], "error": detail})
            print(f"failed {display_index}/{len(entries)} {entry['source_name']}: {detail}", flush=True)

    summary = {
        "schema_version": GENERATION_SCHEMA_VERSION,
        "shard_index": shard_index,
        "shard_count": shard_count,
        "selected": len(selected),
        "generated": completed,
        "failed": failures,
    }
    write_json(RUN_ROOT / f"generation_summary.shard-{shard_index}-of-{shard_count}.json", summary)
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if not failures else 1


def run_lark_overwrite(file_token: str, file_path: Path, output_name: str) -> dict[str, Any]:
    relative_path = file_path.resolve().relative_to(WORKSPACE)
    cmd = [
        "lark-cli",
        "markdown",
        "+overwrite",
        "--as",
        "bot",
        "--file-token",
        file_token,
        "--file",
        str(relative_path),
        "--name",
        output_name,
        "--json",
    ]
    result = subprocess.run(cmd, cwd=WORKSPACE, text=True, capture_output=True, timeout=240, check=False)
    if result.returncode != 0:
        detail = " ".join((result.stderr or result.stdout or "overwrite failed").split())[:1200]
        raise RuntimeError(detail)
    payload = json.loads(result.stdout)
    data = payload.get("data") or payload
    if not data.get("version"):
        raise RuntimeError("lark-cli overwrite returned no version")
    return data


def apply() -> int:
    cfg, worker_cfg, archive_cfg = get_configs()
    generator = load_runtime_module("structured_v6_apply_validator", worker_cfg.skill_script)
    manifest = load_json(MANIFEST_PATH)
    entries = manifest.get("entries") or []
    missing = [entry["source_name"] for entry in entries if not (Path(entry["job_dir"]) / "generation_result.json").exists()]
    if missing:
        raise RuntimeError("refuse partial apply; missing generation results: " + ", ".join(missing))
    source_fields = svc.fields_by_name(svc.list_bitable_fields(cfg, cfg.source_base_token, cfg.source_table_id))
    structured_fields = svc.fields_by_name(svc.list_bitable_fields(cfg, cfg.structured_base_token, cfg.structured_table_id))
    official_table_id = svc.resolve_bitable_table_id(
        cfg, cfg.structured_base_token, cfg.official_json_table_id
    )
    baseline_link_field = archive_cfg.version_baseline_link_field
    existing_results = load_json(APPLY_RESULTS_PATH) if APPLY_RESULTS_PATH.exists() else []
    results_by_id = {item["source_record_id"]: item for item in existing_results}

    for index, entry in enumerate(entries, start=1):
        previous = results_by_id.get(entry["source_record_id"])
        if previous and previous.get("committed") is True:
            print(f"applied {index}/{len(entries)} resume {entry['source_name']}", flush=True)
            continue
        output_path = Path(entry["output_path"])
        generation = load_json(Path(entry["job_dir"]) / "generation_result.json")
        expected_hash = generation["output_sha256"]
        if not output_path.exists():
            raise RuntimeError(f"missing generated output: {entry['source_name']}")
        output_bytes = output_path.read_bytes()
        if sha256_bytes(output_bytes) != expected_hash:
            raise RuntimeError(f"generated output hash mismatch: {entry['source_name']}")
        output_text = output_bytes.decode("utf-8")
        frontmatter = generator.parse_frontmatter(output_text)
        parsed_rows = generator.normalize_approved_rows(
            generator.parse_approved_markdown_rows(output_text, label=entry["source_name"]),
            meeting_date=entry["meeting_date"],
            meeting_uid=entry["meeting_uid"],
        )
        expected_source_name = entry["source_name"] + (
            "" if entry["source_name"].endswith(".md") else ".md"
        )
        expected_frontmatter = {
            "schema_version": GENERATION_SCHEMA_VERSION,
            "meeting_uid": entry["meeting_uid"],
            "source_record_id": entry["source_record_id"],
            "source_archive_url": entry["source_archive_url"],
            "source_file_name": expected_source_name,
        }
        for field_name, expected_value in expected_frontmatter.items():
            actual_value = frontmatter.get(field_name)
            if field_name == "schema_version":
                actual_value = int(actual_value or 0)
            if actual_value != expected_value:
                raise RuntimeError(
                    f"generated output {field_name} mismatch: {entry['source_name']}"
                )
        if len(parsed_rows) != int(generation.get("row_count") or -1):
            raise RuntimeError(f"generated output row count mismatch: {entry['source_name']}")
        actual_condition_count = sum(len(row.get("conditions") or []) for row in parsed_rows)
        if actual_condition_count != int(generation.get("condition_count") or 0):
            raise RuntimeError(f"generated output condition count mismatch: {entry['source_name']}")
        token = entry["structured_file_token"]
        file_url = entry["structured_file_url"]
        upload_name = entry["output_name"]
        structured_record_id = entry["structured_record_id"]
        intent_path = Path(entry["job_dir"]) / "apply_intent.json"
        intent = load_json(intent_path) if intent_path.exists() else {}
        resumed_created_file = False
        if not token and intent.get("created_file_token"):
            token = str(intent["created_file_token"])
            file_url = str(intent.get("created_file_url") or "")
            upload_name = str(intent.get("created_file_name") or upload_name)
            resumed_created_file = True
        old_backup_url = str(intent.get("old_backup_url") or "")
        if token and entry.get("old_cloud_sha256") and not old_backup_url:
            old_content_path = Path(entry["existing_structured_path"])
            if not old_content_path.exists():
                raise RuntimeError(f"missing pre-rerun backup content: {entry['source_name']}")
            old_content = old_content_path.read_bytes()
            if sha256_bytes(old_content) != entry["old_cloud_sha256"]:
                raise RuntimeError(f"pre-rerun backup hash mismatch: {entry['source_name']}")
            month = entry["meeting_date"][:7]
            backup_folder = arch.ensure_version_baseline_folder(archive_cfg, month)
            backup_name = (
                f"{Path(upload_name).stem} - 重跑前备份 - {token[-8:]} - "
                f"{RUN_TAG}-{entry['old_cloud_sha256'][:8]}.md"
            )
            _old_backup_token, old_backup_url = arch.upload_version_artifact(
                archive_cfg, backup_folder, backup_name, old_content
            )
            intent = {
                "source_record_id": entry["source_record_id"],
                "old_cloud_sha256": entry["old_cloud_sha256"],
                "old_backup_url": old_backup_url,
                "old_backup_name": backup_name,
                "recorded_at": svc.now_shanghai_iso(),
            }
            write_json(intent_path, intent)
        if token:
            current = retry_read(lambda: svc.download_drive_file(cfg, token))
            if sha256_bytes(current) == expected_hash:
                overwrite_version = "initial" if resumed_created_file else "unchanged"
                action = "created_resumed" if resumed_created_file else "already_current"
            else:
                overwrite = run_lark_overwrite(token, output_path, upload_name)
                overwrite_version = str(overwrite["version"])
                action = "overwritten"
        else:
            month = entry["meeting_date"][:7]
            target_folder = str(svc.ensure_month_folders(cfg, month)["source_folder_token"])
            existing_names = {
                str(item.get("name"))
                for item in retry_read(lambda: svc.list_drive_folder_items(cfg, target_folder))
                if item.get("name")
            }
            upload_name = svc.unique_upload_name(upload_name, existing_names)
            token = svc.upload_markdown_file(cfg, target_folder, upload_name, output_path.read_bytes())
            file_url = svc.resolve_uploaded_file_url(cfg, target_folder, token, upload_name)
            intent.update(
                {
                    "source_record_id": entry["source_record_id"],
                    "created_file_token": token,
                    "created_file_url": file_url,
                    "created_file_name": upload_name,
                    "recorded_at": svc.now_shanghai_iso(),
                }
            )
            write_json(intent_path, intent)
            overwrite_version = "initial"
            action = "created"
        remote_content = retry_read(lambda: svc.download_drive_file(cfg, token))
        remote_hash = sha256_bytes(remote_content)
        if remote_hash != expected_hash:
            raise RuntimeError(f"remote hash mismatch after write: {entry['source_name']}")

        month = entry["meeting_date"][:7]
        baseline_folder = arch.ensure_version_baseline_folder(archive_cfg, month)
        baseline_name = (
            f"{Path(upload_name).stem} - 审核前 - {token[-8:]} - "
            f"{RUN_TAG}-{expected_hash[:8]}.md"
        )
        baseline_url = str(intent.get("baseline_url") or "")
        if not baseline_url:
            _baseline_token, baseline_url = arch.upload_version_artifact(
                archive_cfg, baseline_folder, baseline_name, remote_content
            )
            intent.update(
                {
                    "source_record_id": entry["source_record_id"],
                    "baseline_url": baseline_url,
                    "baseline_name": baseline_name,
                    "baseline_sha256": expected_hash,
                    "recorded_at": svc.now_shanghai_iso(),
                }
            )
            write_json(intent_path, intent)
        now_ms = int(time.time() * 1000)
        structured_payload: dict[str, Any] = {
            svc.FIELD_STRUCTURED_TABLE_NAME: Path(upload_name).stem,
            svc.FIELD_MEETING_UID: entry["meeting_uid"],
            svc.FIELD_MEETING_DATE: shanghai_midnight_ms(entry["meeting_date"]),
            svc.FIELD_MEETING_SERIES: entry["meeting_series"],
            svc.FIELD_GENERATED_AT: now_ms,
            "文档来源": "会议纪要",
            "源纪要记录": svc.record_link_cell_value(structured_fields, "源纪要记录", entry["source_record_id"]),
            "源纪要链接": svc.url_cell_value(structured_fields, "源纪要链接", entry["source_archive_url"], entry["source_name"]),
            svc.FIELD_STRUCTURED_MD_LINK: svc.url_cell_value(structured_fields, svc.FIELD_STRUCTURED_MD_LINK, file_url, upload_name),
            svc.FIELD_STRUCTURED_VIEWPOINT_COUNT: int(generation["row_count"]),
            svc.FIELD_STRUCTURED_APPROVED: False,
            "归档状态": "待归档",
            svc.FIELD_STRUCTURED_ARCHIVE_LINK: None,
            "归档时间": None,
            svc.FIELD_STRUCTURED_NEEDS_JSON_REGEN: True,
            svc.FIELD_STRUCTURED_CURRENT_MD_HASH: "",
            svc.FIELD_STRUCTURED_JSON_STATUS: "待审核（schema v6 重跑）",
            svc.FIELD_STRUCTURED_ERROR: "",
            baseline_link_field: svc.url_cell_value(structured_fields, baseline_link_field, baseline_url, baseline_name),
            arch.FIELD_BASELINE_VERSION: overwrite_version,
            arch.FIELD_BASELINE_SHA256: expected_hash,
            arch.FIELD_APPROVED_VERSION: "",
            arch.FIELD_APPROVED_SHA256: "",
            arch.FIELD_VERSION_DIFF: arch.VERSION_DIFF_PENDING,
            arch.FIELD_VERSION_STATUS: arch.VERSION_STATUS_BASELINE,
            arch.FIELD_VERSION_ERROR: "",
            # Existing official JSON files/records are retained as historical artifacts,
            # but the structured record must not expose them as current after regeneration.
            svc.FIELD_STRUCTURED_JSON_LINK: None,
            svc.FIELD_STRUCTURED_JSON_ROW_COUNT: None,
            svc.FIELD_STRUCTURED_JSON_GENERATED_AT: None,
            svc.FIELD_STRUCTURED_JSON_SOURCE_MD_HASH: "",
        }
        if entry.get("meeting_type"):
            structured_payload["会议类型"] = entry["meeting_type"]
        if structured_record_id:
            svc.update_bitable_record_in(
                cfg, cfg.structured_base_token, cfg.structured_table_id, structured_record_id, structured_payload
            )
        else:
            created = svc.create_bitable_record_in(
                cfg, cfg.structured_base_token, cfg.structured_table_id, structured_payload
            )
            created_record = (created.get("data") or {}).get("record") or created.get("record") or {}
            structured_record_id = str(
                created_record.get("record_id")
                or (created.get("data") or {}).get("record_id")
                or created.get("record_id")
                or ""
            )
            if not structured_record_id:
                raise RuntimeError(f"structured Base create returned no record_id: {entry['source_name']}")
        official_record_id = str(entry.get("official_json_record_id") or "")
        if official_record_id:
            svc.update_bitable_record_in(
                cfg,
                cfg.structured_base_token,
                official_table_id,
                official_record_id,
                {
                    svc.FIELD_OFFICIAL_STATUS: "待生成",
                    svc.FIELD_OFFICIAL_JSON_LINK: None,
                    svc.FIELD_OFFICIAL_JSON_ROW_COUNT: None,
                    svc.FIELD_OFFICIAL_GENERATED_AT: None,
                    svc.FIELD_OFFICIAL_SOURCE_MD_HASH: "",
                    svc.FIELD_OFFICIAL_ERROR: "",
                },
            )
        source_status = svc.STATUS_GENERATED if int(generation["row_count"]) else svc.STATUS_NO_ROWS
        svc.update_bitable_record(
            cfg,
            entry["source_record_id"],
            {
                svc.FIELD_TABLE_STATUS: source_status,
                svc.FIELD_TABLE_LINK: svc.source_link_value(cfg, source_fields, file_url, upload_name),
                svc.FIELD_GENERATED_AT: now_ms,
                svc.FIELD_TABLE_ROWS: int(generation["row_count"]),
                svc.FIELD_TABLE_ERROR: "",
            },
        )
        result = {
            "source_record_id": entry["source_record_id"],
            "source_name": entry["source_name"],
            "structured_record_id": structured_record_id,
            "file_token": token,
            "file_url": file_url,
            "file_name": upload_name,
            "action": action,
            "overwrite_version": overwrite_version,
            "old_cloud_sha256": entry["old_cloud_sha256"],
            "new_cloud_sha256": remote_hash,
            "row_count": int(generation["row_count"]),
            "baseline_url": baseline_url,
            "baseline_sha256": expected_hash,
            "old_backup_url": old_backup_url,
            "old_backup_sha256": entry.get("old_cloud_sha256") or "",
            "official_json_record_id": official_record_id,
            "committed": True,
        }
        results_by_id[entry["source_record_id"]] = result
        write_json(
            APPLY_RESULTS_PATH,
            [results_by_id[item["source_record_id"]] for item in entries if item["source_record_id"] in results_by_id],
        )
        print(f"applied {index}/{len(entries)} rows={generation['row_count']} {entry['source_name']}", flush=True)
        time.sleep(0.4)
    return 0


def verify() -> int:
    cfg, _worker_cfg, archive_cfg = get_configs()
    manifest = load_json(MANIFEST_PATH)
    entries = manifest.get("entries") or []
    applied_by_id = {
        item["source_record_id"]: item
        for item in (load_json(APPLY_RESULTS_PATH) if APPLY_RESULTS_PATH.exists() else [])
    }
    failures: list[dict[str, str]] = []
    results: list[dict[str, Any]] = []
    baseline_link_field = archive_cfg.version_baseline_link_field
    official_table_id = svc.resolve_bitable_table_id(
        cfg, cfg.structured_base_token, cfg.official_json_table_id
    )

    for index, entry in enumerate(entries, start=1):
        generation = load_json(Path(entry["job_dir"]) / "generation_result.json")
        applied = applied_by_id.get(entry["source_record_id"]) or {}
        file_token = str(applied.get("file_token") or "")
        structured_record_id = str(applied.get("structured_record_id") or "")
        official_record_id = str(applied.get("official_json_record_id") or "")
        reasons: list[str] = []
        if not file_token or not structured_record_id:
            reasons.append("apply_identity_missing")
            remote_hash = ""
            source = {}
            structured = {}
            official = {}
        else:
            remote_hash = sha256_bytes(retry_read(lambda: svc.download_drive_file(cfg, file_token)))
            source = retry_read(lambda: svc.get_bitable_record(cfg, entry["source_record_id"]).get("fields") or {})
            structured = retry_read(
                lambda: svc.get_bitable_record_from(
                    cfg, cfg.structured_base_token, cfg.structured_table_id, structured_record_id
                ).get("fields") or {}
            )
            official = (
                retry_read(
                    lambda: svc.get_bitable_record_from(
                        cfg,
                        cfg.structured_base_token,
                        official_table_id,
                        official_record_id,
                    ).get("fields")
                    or {}
                )
                if official_record_id
                else {}
            )
        expected_hash = generation["output_sha256"]
        expected_status = svc.STATUS_GENERATED if int(generation["row_count"]) else svc.STATUS_NO_ROWS
        archive_token, archive_type = svc.parse_drive_url(entry["source_archive_url"])
        if archive_type != "file" or sha256_bytes(
            retry_read(lambda: svc.download_drive_file(cfg, archive_token))
        ) != entry["source_archive_sha256"]:
            reasons.append("source_archive_hash")
        if remote_hash != expected_hash:
            reasons.append("cloud_hash")
        source_token, _source_url = first_file_token(source.get(svc.FIELD_TABLE_LINK)) if source else ("", "")
        if source_token != file_token:
            reasons.append("source_link")
        if svc.plain_field_value(source.get(svc.FIELD_TABLE_STATUS)) != expected_status:
            reasons.append("source_status")
        if int(svc.number_field_value(source.get(svc.FIELD_TABLE_ROWS)) or 0) != int(generation["row_count"]):
            reasons.append("source_rows")
        structured_token, _structured_url = first_file_token(structured.get(svc.FIELD_STRUCTURED_MD_LINK)) if structured else ("", "")
        if structured_token != file_token:
            reasons.append("structured_link")
        if svc.plain_field_value(structured.get(svc.FIELD_MEETING_UID)) != entry["meeting_uid"]:
            reasons.append("meeting_uid")
        if int(svc.number_field_value(structured.get(svc.FIELD_STRUCTURED_VIEWPOINT_COUNT)) or 0) != int(generation["row_count"]):
            reasons.append("structured_rows")
        if svc.checkbox_is_checked(structured.get(svc.FIELD_STRUCTURED_APPROVED)):
            reasons.append("structured_approval_not_reset")
        if svc.plain_field_value(structured.get("归档状态")) != "待归档":
            reasons.append("structured_archive_status")
        if svc.plain_field_value(structured.get(arch.FIELD_VERSION_STATUS)) != arch.VERSION_STATUS_BASELINE:
            reasons.append("structured_version_status")
        if svc.plain_field_value(structured.get(arch.FIELD_BASELINE_SHA256)) != expected_hash:
            reasons.append("baseline_hash_field")
        if not svc.checkbox_is_checked(structured.get(svc.FIELD_STRUCTURED_NEEDS_JSON_REGEN)):
            reasons.append("json_regen_flag")
        if svc.plain_field_value(structured.get(svc.FIELD_STRUCTURED_JSON_STATUS)) != "待审核（schema v6 重跑）":
            reasons.append("json_status")
        if svc.first_url(structured.get(svc.FIELD_STRUCTURED_JSON_LINK)):
            reasons.append("stale_json_link")
        if official_record_id:
            if svc.plain_field_value(official.get(svc.FIELD_OFFICIAL_STATUS)) != "待生成":
                reasons.append("official_json_status")
            if svc.first_url(official.get(svc.FIELD_OFFICIAL_JSON_LINK)):
                reasons.append("official_json_stale_link")
            if svc.plain_field_value(official.get(svc.FIELD_OFFICIAL_SOURCE_MD_HASH)):
                reasons.append("official_json_stale_hash")
        old_backup_url = str(applied.get("old_backup_url") or "")
        if entry.get("old_cloud_sha256"):
            if not old_backup_url:
                reasons.append("old_backup_link")
            else:
                old_backup_token, old_backup_type = svc.parse_drive_url(old_backup_url)
                if old_backup_type != "file" or sha256_bytes(
                    retry_read(lambda: svc.download_drive_file(cfg, old_backup_token))
                ) != entry["old_cloud_sha256"]:
                    reasons.append("old_backup_hash")
        baseline_url = svc.first_url(structured.get(baseline_link_field))
        if not baseline_url:
            reasons.append("baseline_link")
        else:
            baseline_token, baseline_type = svc.parse_drive_url(baseline_url)
            if baseline_type != "file" or sha256_bytes(retry_read(lambda: svc.download_drive_file(cfg, baseline_token))) != expected_hash:
                reasons.append("baseline_artifact_hash")
        if int(generation.get("schema_version") or 0) != GENERATION_SCHEMA_VERSION:
            reasons.append("schema_version")
        result = {
            "source_record_id": entry["source_record_id"],
            "source_name": entry["source_name"],
            "ok": not reasons,
            "reasons": reasons,
            "row_count": int(generation["row_count"]),
            "cloud_sha256": remote_hash,
            "structured_record_id": structured_record_id,
        }
        results.append(result)
        if reasons:
            failures.append(
                {"record_id": entry["source_record_id"], "source_name": entry["source_name"], "reason": ",".join(reasons)}
            )
        print(f"verified {index}/{len(entries)} ok={not reasons} {entry['source_name']}", flush=True)
        time.sleep(0.15)
    summary = {
        "schema_version": GENERATION_SCHEMA_VERSION,
        "verified_at": svc.now_shanghai_iso(),
        "total": len(results),
        "passed": len(results) - len(failures),
        "blocked_source_records": manifest.get("blocked") or [],
        "failed": failures,
        "results": results,
    }
    write_json(VERIFY_RESULTS_PATH, summary)
    print(json.dumps({"total": len(results), "passed": summary["passed"], "failed": failures}, ensure_ascii=False))
    return 0 if not failures else 1


def main() -> int:
    global svc, worker, arch, RUN_ROOT, MANIFEST_PATH, APPLY_RESULTS_PATH, VERIFY_RESULTS_PATH, RUN_TAG, ARCHIVE_DIR, SELECTED_RECORD_IDS
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("prepare", "generate", "apply", "verify"))
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--archive-dir", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    parser.add_argument("--run-tag", default="schema-v6-rerun")
    parser.add_argument("--online-read-only", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--record-id", action="append", default=[])
    args = parser.parse_args()
    if args.command in {"prepare", "verify"} and not args.online_read_only:
        raise SystemExit(f"command={args.command} requires --online-read-only")
    if args.command == "apply" and not args.apply:
        raise SystemExit("command=apply requires --apply")
    if args.apply and args.command != "apply":
        raise SystemExit("--apply is only valid with command=apply")
    RUN_ROOT = args.run_root.expanduser().resolve()
    try:
        RUN_ROOT.relative_to(WORKSPACE)
    except ValueError as exc:
        raise SystemExit("run root must stay inside the workspace") from exc
    MANIFEST_PATH = RUN_ROOT / "manifest.json"
    APPLY_RESULTS_PATH = RUN_ROOT / "apply_results.json"
    VERIFY_RESULTS_PATH = RUN_ROOT / "verify_results.json"
    RUN_TAG = str(args.run_tag).strip() or "schema-v6-rerun"
    ARCHIVE_DIR = args.archive_dir.expanduser().resolve()
    SELECTED_RECORD_IDS = {str(record_id).strip() for record_id in args.record_id if str(record_id).strip()}
    runtime_dir = args.runtime_dir.expanduser().resolve()
    worker = load_runtime_module("v6_batch_semantic_worker", runtime_dir / "semantic_worker.py")
    svc = load_runtime_module("v6_batch_structured_service", runtime_dir / "structured_generate_service.py")
    arch = load_runtime_module("v6_batch_archive_runtime", ARCHIVE_DIR / "feishu_drive_to_bitable.py")
    if int(getattr(svc, "GENERATION_SCHEMA_VERSION", 0)) != GENERATION_SCHEMA_VERSION:
        raise SystemExit("runtime service is not schema v6")
    if int(getattr(worker, "GENERATION_SCHEMA_VERSION", 0)) != GENERATION_SCHEMA_VERSION:
        raise SystemExit("runtime worker is not schema v6")
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    if args.command == "prepare":
        return prepare()
    if args.command == "generate":
        return generate(shard_count=args.shard_count, shard_index=args.shard_index)
    if args.command == "apply":
        return apply()
    return verify()


if __name__ == "__main__":
    raise SystemExit(main())
