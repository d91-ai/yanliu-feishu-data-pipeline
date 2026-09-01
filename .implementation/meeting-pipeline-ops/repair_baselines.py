#!/usr/bin/env python3
"""Preflight and idempotently upload approved review baselines from a manifest."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
from typing import Any, Mapping


UID_PATTERN = re.compile(r"mtg_[0-9a-f]{32}")
SHA_PATTERN = re.compile(r"[0-9a-f]{64}")
ARTIFACT_CONFIG = {
    "meeting_minutes": {"category": "会议纪要", "source_field": "文档链接"},
    "structured_viewpoints": {"category": "结构化表格", "source_field": "表格链接"},
}


class RepairError(ValueError):
    pass


def canonical_hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def load_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RepairError(f"invalid {label}") from exc
    if not isinstance(value, dict):
        raise RepairError(f"{label} must be a JSON object")
    return value


def validate_manifest(value: Mapping[str, Any]) -> list[dict[str, Any]]:
    if value.get("schema_version") != 1 or value.get("mode") != "production-cutover-preflight":
        raise RepairError("unsupported baseline manifest")
    targets = value.get("targets")
    if not isinstance(targets, list) or not targets:
        raise RepairError("baseline manifest targets missing")
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for raw in targets:
        if not isinstance(raw, dict):
            raise RepairError("baseline target must be an object")
        target = dict(raw)
        artifact_type = str(target.get("artifact_type") or "")
        if artifact_type not in ARTIFACT_CONFIG:
            raise RepairError("baseline artifact type invalid")
        record_id = str(target.get("record_id") or "")
        meeting_uid = str(target.get("meeting_uid") or "").lower()
        meeting_date = str(target.get("meeting_date") or "")
        meeting_series = str(target.get("meeting_series") or "")
        source_token = str(target.get("source_file_token") or "")
        source_version = str(target.get("source_version") or "")
        source_sha256 = str(target.get("source_sha256") or "").lower()
        source_size_bytes = target.get("source_size_bytes")
        target_folder_token = str(target.get("target_folder_token") or "")
        target_name = str(target.get("target_name") or "")
        if not record_id or not UID_PATTERN.fullmatch(meeting_uid):
            raise RepairError("baseline record identity invalid")
        if not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", meeting_date) or not meeting_series:
            raise RepairError("baseline meeting metadata invalid")
        if not source_token or not source_version or not SHA_PATTERN.fullmatch(source_sha256):
            raise RepairError("baseline source identity invalid")
        if (
            isinstance(source_size_bytes, bool)
            or not isinstance(source_size_bytes, int)
            or source_size_bytes <= 0
        ):
            raise RepairError("baseline source size invalid")
        if (
            not target_folder_token
            or Path(target_name).name != target_name
            or not target_name.endswith(".md")
        ):
            raise RepairError("baseline target identity invalid")
        if not isinstance(target.get("expected_reviewed"), bool):
            raise RepairError("baseline expected_reviewed invalid")
        key = (record_id, artifact_type)
        if key in seen:
            raise RepairError("duplicate baseline target identity")
        seen.add(key)
        normalized.append(target)
    return normalized


def load_router(path: Path):
    spec = importlib.util.spec_from_file_location("baseline_repair_router", path)
    if spec is None or spec.loader is None:
        raise RepairError("cannot load Router module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_config(router, route_env_path: Path, shared_env_path: Path, version_config: Path):
    route = router.parse_dotenv_file(route_env_path)
    shared = router.parse_dotenv_file(shared_env_path)
    for canonical, alias in (("FEISHU_APP_ID", "LARK_APP_ID"), ("FEISHU_APP_SECRET", "LARK_APP_SECRET")):
        if not router.first_value(route, canonical, alias):
            value = router.first_value(shared, canonical, alias)
            if value:
                route[canonical] = value
    route["FEISHU_VERSION_CONFIG_PATH"] = str(version_config.resolve())
    return router.config_from_env(route)


def _exact_child_folder(router, cfg, parent_token: str, name: str) -> str:
    matches = [
        item
        for item in router.list_drive_folder_items(cfg, parent_token)
        if item.get("type") == "folder" and item.get("name") == name
    ]
    if len(matches) != 1:
        raise RepairError(f"baseline folder ambiguous:{name}")
    token = router.drive_item_token(matches[0])
    if not token:
        raise RepairError(f"baseline folder token missing:{name}")
    return token


def resolve_existing_baseline_folder(router, cfg, meeting_date: str) -> str:
    category = _exact_child_folder(router, cfg, cfg.version_root_folder_token, cfg.version_category)
    month = _exact_child_folder(router, cfg, category, meeting_date[:7])
    return _exact_child_folder(router, cfg, month, "审核前")


def _exact_named_files(router, cfg, folder_token: str, name: str) -> list[dict[str, Any]]:
    return [
        item
        for item in router.list_drive_folder_items(cfg, folder_token)
        if item.get("type") == "file" and item.get("name") == name
    ]


def _plain(router, value: Any) -> str:
    return router.plain_field_value(value).strip()


def preflight_target(router, source_cfg, artifact_cfg, target: Mapping[str, Any]) -> dict[str, Any]:
    config = ARTIFACT_CONFIG[str(target["artifact_type"])]
    if artifact_cfg.version_category != config["category"]:
        raise RepairError("baseline category mismatch")
    record = router.get_bitable_record(source_cfg, str(target["record_id"]))
    fields = record.get("fields", {})
    if not isinstance(fields, dict):
        raise RepairError("baseline source record fields invalid")
    if _plain(router, fields.get("会议UID")).lower() != target["meeting_uid"]:
        raise RepairError("baseline meeting_uid mismatch")
    if _plain(router, fields.get("会议系列")) != target["meeting_series"]:
        raise RepairError("baseline meeting_series mismatch")
    if router.form_meeting_date_from_field(source_cfg, fields.get("会议日期")) != target["meeting_date"]:
        raise RepairError("baseline meeting_date mismatch")
    if router.checkbox_is_checked(fields.get("审核状态")) is not target["expected_reviewed"]:
        raise RepairError("baseline review state mismatch")
    source_url = router.url_from_field_value(fields.get(config["source_field"]))
    if not source_url:
        raise RepairError("baseline source link missing")
    source_token, source_type = router.parse_drive_url(source_url)
    if source_type != "file" or source_token != target["source_file_token"]:
        raise RepairError("baseline source token mismatch")

    version_info, content = router.first_valid_file_version(artifact_cfg, source_token)
    version = str(version_info.get("version") or "")
    content_hash = router.sha256_hex(content)
    if version != target["source_version"] or content_hash != target["source_sha256"]:
        raise RepairError("baseline source version or hash mismatch")
    if len(content) != int(target["source_size_bytes"]):
        raise RepairError("baseline source size mismatch")
    meta = router.get_file_meta(artifact_cfg, source_token, "file")
    computed_name = router.baseline_artifact_name(str(meta.get("title") or source_token), source_token, version_info)
    if computed_name != target["target_name"]:
        raise RepairError("baseline target name mismatch")
    folder_token = resolve_existing_baseline_folder(router, artifact_cfg, str(target["meeting_date"]))
    if folder_token != target["target_folder_token"]:
        raise RepairError("baseline target folder mismatch")
    matches = _exact_named_files(router, artifact_cfg, folder_token, computed_name)
    if len(matches) > 1:
        raise RepairError("baseline exact target ambiguous")
    existing_token = router.drive_item_token(matches[0]) if matches else ""
    existing_url = ""
    if existing_token:
        existing_content = router.download_drive_file_version(artifact_cfg, existing_token)
        if router.sha256_hex(existing_content) != content_hash:
            raise RepairError("baseline existing target hash mismatch")
        existing_url = str(matches[0].get("url") or router.resolve_drive_file_url(artifact_cfg, existing_token, folder_token))
    return {
        "record_id": target["record_id"],
        "meeting_uid": target["meeting_uid"],
        "artifact_type": target["artifact_type"],
        "source_file_token": source_token,
        "source_version": version,
        "source_sha256": content_hash,
        "source_size_bytes": len(content),
        "target_folder_token": folder_token,
        "target_name": computed_name,
        "target_file_token": existing_token,
        "target_url": existing_url,
        "status": "existing_verified" if existing_token else "ready_to_upload",
        "_content": content,
    }


def _receipt_path(receipt_dir: Path, result: Mapping[str, Any]) -> Path:
    key = canonical_hash(
        {
            name: result[name]
            for name in ("record_id", "artifact_type", "source_file_token", "source_version", "source_sha256")
        }
    )
    return receipt_dir / f"{key}.json"


def apply_target(router, artifact_cfg, result: dict[str, Any], receipt_dir: Path) -> dict[str, Any]:
    receipt_path = _receipt_path(receipt_dir, result)
    expected_receipt_identity = {
        name: result[name]
        for name in (
            "record_id", "meeting_uid", "artifact_type", "source_file_token", "source_version",
            "source_sha256", "source_size_bytes", "target_folder_token", "target_name"
        )
    }
    if receipt_path.exists():
        receipt = load_json_object(receipt_path, "baseline receipt")
        if any(receipt.get(name) != value for name, value in expected_receipt_identity.items()):
            raise RepairError("baseline receipt conflict")
    token = str(result.get("target_file_token") or "")
    url = str(result.get("target_url") or "")
    if not token:
        token, url = router.upload_version_artifact(
            artifact_cfg,
            str(result["target_folder_token"]),
            str(result["target_name"]),
            result["_content"],
        )
    matches = _exact_named_files(
        router, artifact_cfg, str(result["target_folder_token"]), str(result["target_name"])
    )
    if len(matches) != 1 or router.drive_item_token(matches[0]) != token:
        raise RepairError("baseline upload identity unconfirmed")
    downloaded = router.download_drive_file_version(artifact_cfg, token)
    if router.sha256_hex(downloaded) != result["source_sha256"]:
        raise RepairError("baseline upload hash unconfirmed")
    if not url:
        url = str(matches[0].get("url") or router.resolve_drive_file_url(
            artifact_cfg, token, str(result["target_folder_token"])
        ))
    receipt = {
        "schema_version": 1,
        "status": "uploaded_verified",
        **expected_receipt_identity,
        "target_file_token": token,
        "target_url": url,
        "target_sha256": result["source_sha256"],
    }
    router.write_private_json(receipt_path, receipt)
    return receipt


def sanitize_result(result: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in result.items() if key != "_content"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Repair exact historical review baselines")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--router", required=True)
    parser.add_argument("--source-env", required=True)
    parser.add_argument("--structured-env", required=True)
    parser.add_argument("--shared-env", required=True)
    parser.add_argument("--version-config", required=True)
    parser.add_argument("--receipt-dir", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    manifest = load_json_object(Path(args.manifest), "baseline manifest")
    targets = validate_manifest(manifest)
    router = load_router(Path(args.router))
    shared_env = Path(args.shared_env)
    version_config = Path(args.version_config)
    source_cfg = load_config(router, Path(args.source_env), shared_env, version_config)
    structured_cfg = load_config(router, Path(args.structured_env), shared_env, version_config)
    configs = {"meeting_minutes": source_cfg, "structured_viewpoints": structured_cfg}

    results: list[dict[str, Any]] = []
    for target in targets:
        artifact_cfg = configs[str(target["artifact_type"])]
        preflight = preflight_target(router, source_cfg, artifact_cfg, target)
        if args.apply:
            results.append(apply_target(router, artifact_cfg, preflight, Path(args.receipt_dir)))
        else:
            results.append(sanitize_result(preflight))
    print(json.dumps({
        "status": "applied" if args.apply else "dry_run",
        "manifest_sha256": canonical_hash(manifest),
        "target_count": len(results),
        "results": results,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RepairError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from None
