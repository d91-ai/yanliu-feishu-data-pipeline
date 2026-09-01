#!/usr/bin/env python3
"""Read-only audit of source/archive/version retention gates for selected records."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import time


def load_module(path: Path):
    spec = importlib.util.spec_from_file_location("source_archive_audit_runtime", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load runtime: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive-dir", type=Path, required=True)
    parser.add_argument("--env-file", default=".env.meeting-minutes")
    parser.add_argument("--record-id", action="append", required=True)
    args = parser.parse_args()
    runtime = load_module(args.archive_dir / "feishu_drive_to_bitable.py")
    cfg = runtime.read_config_from_env_file(args.archive_dir / args.env_file)
    results = []
    for record_id in args.record_id:
        record = runtime.get_bitable_record(cfg, record_id)
        fields = record.get("fields") or {}
        source_url = runtime.url_from_field_value(fields.get(cfg.archive_file_link_field))
        archive_url = runtime.url_from_field_value(fields.get(cfg.archive_link_field))
        item = {
            "record_id": record_id,
            "source_name": runtime.plain_field_value(fields.get(cfg.archive_file_name_field)),
            "archive_status": runtime.plain_field_value(fields.get(cfg.archive_status_field)),
            "version_status": runtime.plain_field_value(fields.get(runtime.FIELD_VERSION_STATUS)),
            "version_error": runtime.plain_field_value(fields.get(runtime.FIELD_VERSION_ERROR)),
            "source_url": source_url,
            "archive_url": archive_url,
            "baseline_url": runtime.url_from_field_value(fields.get(cfg.version_baseline_link_field)),
            "baseline_version": runtime.plain_field_value(fields.get(runtime.FIELD_BASELINE_VERSION)),
            "baseline_sha256": runtime.plain_field_value(fields.get(runtime.FIELD_BASELINE_SHA256)).lower(),
            "approved_version": runtime.plain_field_value(fields.get(runtime.FIELD_APPROVED_VERSION)),
            "approved_sha256_field": runtime.plain_field_value(fields.get(runtime.FIELD_APPROVED_SHA256)).lower(),
            "archive_time_ms": runtime.ms_from_record_time(fields.get(cfg.archive_time_field)),
        }
        source_token = ""
        if source_url:
            source_token, source_type = runtime.parse_drive_url(source_url)
            item["source_type"] = source_type
            versions = sorted(runtime.list_drive_file_versions(cfg, source_token), key=runtime.version_sort_key)
            item["source_version_count"] = len(versions)
            if versions:
                latest_info = versions[-1]
                latest_version = str(latest_info.get("version") or "")
                latest_content = runtime.download_drive_file_version(cfg, source_token, latest_version)
                item["source_latest_version"] = latest_version
                item["source_latest_sha256"] = sha256(latest_content)
            time.sleep(0.4)
        if archive_url:
            archive_token, archive_type = runtime.parse_drive_url(archive_url)
            item["archive_type"] = archive_type
            archive_content = runtime.download_drive_file_version(cfg, archive_token)
            archive_hash = sha256(archive_content)
            item["archive_sha256"] = archive_hash
            matches = []
            if source_token:
                archive_ms = item["archive_time_ms"]
                for version_info in reversed(versions):
                    edited_at, version_sort = runtime.version_sort_key(version_info)
                    if archive_ms is not None and edited_at and edited_at > archive_ms:
                        continue
                    version = str(version_info.get("version") or version_sort or "")
                    if not version:
                        continue
                    content = runtime.download_drive_file_version(cfg, source_token, version)
                    if sha256(content) == archive_hash:
                        matches.append({"version": version, "edited_at": edited_at})
                        break
                    time.sleep(0.25)
            item["archive_matching_source_versions"] = matches
        results.append(item)
        time.sleep(0.5)
    print(json.dumps({"records": results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
