#!/usr/bin/env python3
"""Offline audit for the unified Base index and Drive publication manifest."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
from typing import Any
import urllib.parse


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = ROOT / ".implementation" / "meeting-pipeline-contract" / "meeting_pipeline_contract.py"
JSON_FIELDS = {
    "industry_market_viewpoints": "行业与市场观点JSON",
    "structured_viewpoints": "标的观点JSON",
}


class AuditInputError(ValueError):
    pass


def load_contract(path: Path = CONTRACT_PATH):
    spec = importlib.util.spec_from_file_location("publish_audit_pipeline_contract", path)
    if spec is None or spec.loader is None:
        raise AuditInputError(f"cannot load pipeline contract: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def read_json(path: Path, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuditInputError(f"invalid {label}: {path}") from exc


def records_array(value: Any, label: str) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        value = value.get("records") if label == "Base export" else value.get("files")
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise AuditInputError(f"{label} must contain an object array")
    return value


def plain_value(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, dict):
        for name in ("text", "link", "url", "value", "name"):
            if value.get(name) not in (None, ""):
                return str(value[name])
        return ""
    if isinstance(value, list):
        return ",".join(filter(None, (plain_value(item) for item in value)))
    return str(value)


def url_value(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("link") or value.get("url") or value.get("text") or "")
    if isinstance(value, list):
        for item in value:
            result = url_value(item)
            if result:
                return result
    return str(value or "")


def token_from_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    parts = [urllib.parse.unquote(item) for item in parsed.path.split("/") if item]
    for index, part in enumerate(parts[:-1]):
        if part in {"file", "files"}:
            return parts[index + 1]
    query = urllib.parse.parse_qs(parsed.query)
    for key in ("file_token", "token"):
        values = query.get(key) or []
        if len(values) == 1:
            return str(values[0])
    return ""


def load_artifact(file: dict[str, Any], manifest_root: Path) -> tuple[dict[str, Any], bytes]:
    if isinstance(file.get("artifact"), dict):
        artifact = file["artifact"]
        encoded = (
            json.dumps(artifact, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        return artifact, encoded
    local_path = str(file.get("local_path") or "")
    if not local_path:
        raise AuditInputError("Drive manifest file is missing artifact/local_path")
    path = Path(local_path)
    if not path.is_absolute():
        path = manifest_root / path
    try:
        encoded = path.read_bytes()
        value = json.loads(encoded.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuditInputError(f"invalid artifact JSON: {path}") from exc
    if not isinstance(value, dict):
        raise AuditInputError("artifact JSON must contain an object")
    return value, encoded


def content_hash(file: dict[str, Any], encoded: bytes) -> str:
    supplied = str(file.get("sha256") or "").strip().lower()
    calculated = hashlib.sha256(encoded).hexdigest()
    if supplied and not re.fullmatch(r"[0-9a-f]{64}", supplied):
        raise AuditInputError("Drive manifest sha256 is invalid")
    if supplied and supplied != calculated:
        raise AuditInputError("Drive manifest sha256 does not match artifact bytes")
    return supplied or calculated


def audit(base_records: list[dict[str, Any]], drive_files: list[dict[str, Any]], *, manifest_root: Path) -> dict[str, Any]:
    contract = load_contract()
    issues: list[dict[str, Any]] = []
    referenced_tokens: set[str] = set()
    by_token: dict[str, list[dict[str, Any]]] = {}
    identities: dict[tuple[str, str, int], set[str]] = {}
    parsed_files: dict[str, tuple[dict[str, Any], str]] = {}

    for file in drive_files:
        token = str(file.get("file_token") or file.get("token") or "").strip()
        if not token:
            issues.append({"code": "drive_file_token_missing", "scope": "drive"})
            continue
        by_token.setdefault(token, []).append(file)
        try:
            artifact, encoded = load_artifact(file, manifest_root)
            metadata = contract.validate_artifact_metadata(artifact.get("metadata"))
            digest = content_hash(file, encoded)
        except Exception as exc:
            issues.append(
                {"code": "artifact_invalid", "file_token": token, "detail": str(exc)[:300]}
            )
            continue
        parsed_files[token] = (artifact, digest)
        identity = (metadata["meeting_uid"], metadata["artifact_type"], metadata["data_version"])
        identities.setdefault(identity, set()).add(digest)

    for token, matches in by_token.items():
        if len(matches) > 1:
            issues.append({"code": "drive_token_ambiguous", "file_token": token})
    for identity, hashes in identities.items():
        if len(hashes) > 1:
            issues.append(
                {
                    "code": "same_identity_hash_conflict",
                    "meeting_uid": identity[0],
                    "artifact_type": identity[1],
                    "data_version": identity[2],
                    "hashes": sorted(hashes),
                }
            )

    seen_uids: dict[str, str] = {}
    for record in base_records:
        record_id = str(record.get("record_id") or "").strip()
        fields = record.get("fields") or {}
        if not record_id or not isinstance(fields, dict):
            issues.append({"code": "base_record_invalid", "scope": "base"})
            continue
        uid = plain_value(fields.get("会议ID")).strip().lower()
        try:
            uid = contract.validate_meeting_uid(uid)
            version = contract.validate_data_version(int(plain_value(fields.get("数据版本"))))
        except Exception as exc:
            issues.append({"code": "base_identity_invalid", "record_id": record_id, "detail": str(exc)})
            continue
        if uid in seen_uids and seen_uids[uid] != record_id:
            issues.append(
                {"code": "base_uid_duplicate", "meeting_uid": uid, "record_ids": [seen_uids[uid], record_id]}
            )
        seen_uids[uid] = record_id
        for artifact_type, field_name in JSON_FIELDS.items():
            url = url_value(fields.get(field_name))
            if not url:
                continue
            token = token_from_url(url)
            if not token:
                issues.append(
                    {"code": "base_json_url_invalid", "record_id": record_id, "field": field_name}
                )
                continue
            referenced_tokens.add(token)
            if token not in parsed_files:
                issues.append(
                    {
                        "code": "base_current_file_missing_or_invalid",
                        "record_id": record_id,
                        "field": field_name,
                        "file_token": token,
                    }
                )
                continue
            artifact, _digest = parsed_files[token]
            metadata = artifact["metadata"]
            if (
                metadata.get("meeting_uid") != uid
                or metadata.get("artifact_type") != artifact_type
                or metadata.get("data_version") > version
            ):
                issues.append(
                    {
                        "code": "base_artifact_identity_mismatch",
                        "record_id": record_id,
                        "field": field_name,
                        "file_token": token,
                    }
                )

    for token in sorted(set(parsed_files) - referenced_tokens):
        artifact, _digest = parsed_files[token]
        issues.append(
            {
                "code": "orphan_json",
                "file_token": token,
                "meeting_uid": artifact["metadata"].get("meeting_uid"),
                "artifact_type": artifact["metadata"].get("artifact_type"),
                "recommended_action": "quarantine_after_manual_confirmation",
            }
        )
    return {
        "ok": not issues,
        "base_record_count": len(base_records),
        "drive_file_count": len(drive_files),
        "referenced_json_count": len(referenced_tokens),
        "issue_count": len(issues),
        "issues": issues,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit a unified meeting publication snapshot")
    parser.add_argument("--base-export", required=True)
    parser.add_argument("--drive-manifest", required=True)
    parser.add_argument("--json-output")
    args = parser.parse_args(argv)
    drive_path = Path(args.drive_manifest)
    result = audit(
        records_array(read_json(Path(args.base_export), "Base export"), "Base export"),
        records_array(read_json(drive_path, "Drive manifest"), "Drive manifest"),
        manifest_root=drive_path.parent,
    )
    output = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.json_output:
        Path(args.json_output).write_text(output, encoding="utf-8")
    print(output, end="")
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AuditInputError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from None
