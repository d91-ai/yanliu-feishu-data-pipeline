#!/usr/bin/env python3
"""Build an offline direct-cutover plan for the unified meeting table."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
from typing import Any
import unicodedata
import uuid


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = ROOT / ".implementation" / "meeting-pipeline-contract" / "meeting_pipeline_contract.py"
MIGRATION_NAMESPACE = uuid.UUID("7ae5d248-bdcc-4daa-8bc7-912417288bb4")


class MigrationError(ValueError):
    pass


def load_contract():
    spec = importlib.util.spec_from_file_location("migration_pipeline_contract", CONTRACT_PATH)
    if spec is None or spec.loader is None:
        raise MigrationError("cannot load pipeline contract")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def read_records(path: str | None, label: str) -> list[dict[str, Any]]:
    if not path:
        return []
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MigrationError(f"invalid {label} export") from exc
    if isinstance(value, dict):
        value = value.get("records")
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise MigrationError(f"{label} export must contain records array")
    return value


def read_baseline_repairs(path: str | None) -> dict[tuple[str, str], dict[str, Any]]:
    if not path:
        return {}
    root = Path(path)
    paths = sorted(root.glob("*.json")) if root.is_dir() else [root]
    repairs: dict[tuple[str, str], dict[str, Any]] = {}
    for receipt_path in paths:
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MigrationError("invalid baseline repair receipt") from exc
        if not isinstance(receipt, dict) or receipt.get("schema_version") != 1:
            raise MigrationError("invalid baseline repair receipt")
        if receipt.get("status") != "uploaded_verified":
            continue
        record_id = str(receipt.get("record_id") or "")
        artifact_type = str(receipt.get("artifact_type") or "")
        source_hash = str(receipt.get("source_sha256") or "")
        target_hash = str(receipt.get("target_sha256") or "")
        target_url = str(receipt.get("target_url") or "")
        if (
            not record_id
            or artifact_type not in {"meeting_minutes", "structured_viewpoints"}
            or not re.fullmatch(r"[0-9a-f]{64}", source_hash)
            or source_hash != target_hash
            or not target_url.startswith("https://")
        ):
            raise MigrationError("baseline repair receipt identity invalid")
        key = (record_id, artifact_type)
        if key in repairs and repairs[key] != receipt:
            raise MigrationError("conflicting baseline repair receipts")
        repairs[key] = receipt
    return repairs


def plain(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("text", "link", "url", "value", "name"):
            if value.get(key) not in (None, ""):
                return str(value[key])
        return ""
    if isinstance(value, list):
        return ",".join(filter(None, (plain(item) for item in value)))
    return str(value)


def meeting_name(value: Any, fallback_series: str, meeting_date: str = "") -> str:
    """Derive the human label from a legacy file name, never from the UID."""
    name = plain(value).strip()
    name = re.sub(r"\.md$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"^20\d{2}-\d{2}-\d{2}\s*[-–—]\s*", "", name)
    name = re.sub(r"\s*[-–—]\s*v\d+\s*$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\s*[-–—]\s*会议纪要\s*$", "", name)
    name = " ".join(unicodedata.normalize("NFKC", name).split())
    if not name:
        name = " ".join(unicodedata.normalize("NFKC", fallback_series).split())
    if not name or len(name) > 80 or any(ord(char) < 32 or ord(char) == 127 for char in name):
        raise MigrationError("meeting name invalid")
    if meeting_date:
        if not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", meeting_date):
            raise MigrationError("meeting name date invalid")
        name = f"{meeting_date} - {name}"
    return name


def first(fields: dict[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        value = fields.get(name)
        if plain(value).strip():
            return value
    return ""


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return plain(value).strip().lower() in {"1", "true", "yes", "是", "已审核", "checked"}


def source_reference(value: Any) -> str:
    if isinstance(value, list) and value:
        first_value = value[0]
        if isinstance(first_value, dict):
            return str(first_value.get("record_id") or first_value.get("id") or "")
        return str(first_value)
    if isinstance(value, dict):
        return str(value.get("record_id") or value.get("id") or plain(value))
    return plain(value).strip()


def deterministic_uid(record_id: str) -> str:
    return "mtg_" + uuid.uuid5(MIGRATION_NAMESPACE, record_id).hex


def _date_text(value: Any) -> str:
    text = plain(value).strip()
    match = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", text)
    if match:
        return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
    if text.isdigit() and len(text) >= 10:
        from datetime import datetime, timezone, timedelta

        return datetime.fromtimestamp(int(text) / 1000, timezone(timedelta(hours=8))).date().isoformat()
    return ""


def build_plan(
    source_records: list[dict[str, Any]],
    structured_records: list[dict[str, Any]],
    official_records: list[dict[str, Any]],
    sanitized_records: list[dict[str, Any]],
    baseline_repairs: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    contract = load_contract()
    issues: list[dict[str, Any]] = []
    repairs = baseline_repairs or {}
    consumed_repairs: set[tuple[str, str]] = set()
    structured_by_source: dict[str, list[dict[str, Any]]] = {}
    structured_ids: dict[str, dict[str, Any]] = {}
    for item in structured_records:
        record_id = str(item.get("record_id") or "")
        fields = item.get("fields") or {}
        source_id = source_reference(
            first(fields, ("源纪要记录", "source_record_id", "源记录ID"))
        )
        if source_id:
            structured_by_source.setdefault(source_id, []).append(item)
        if record_id:
            structured_ids[record_id] = item
    official_by_structured: dict[str, list[dict[str, Any]]] = {}
    for item in official_records:
        fields = item.get("fields") or {}
        source_id = source_reference(
            first(fields, ("来源结构化MD记录", "source_md_record_id", "源MD记录"))
        )
        if source_id:
            official_by_structured.setdefault(source_id, []).append(item)
    sanitized_by_uid: dict[str, list[dict[str, Any]]] = {}
    for item in sanitized_records:
        fields = item.get("fields") or {}
        uid = plain(first(fields, ("会议ID", "会议UID"))).strip().lower()
        if uid:
            sanitized_by_uid.setdefault(uid, []).append(item)

    planned: list[dict[str, Any]] = []
    seen_uids: dict[str, str] = {}
    for source in source_records:
        record_id = str(source.get("record_id") or "").strip()
        fields = source.get("fields") or {}
        if not record_id or not isinstance(fields, dict):
            issues.append({"code": "source_record_invalid"})
            continue
        supplied_uid = plain(first(fields, ("会议ID", "会议UID"))).strip().lower()
        uid = supplied_uid or deterministic_uid(record_id)
        try:
            contract.validate_meeting_uid(uid)
        except Exception:
            issues.append({"code": "meeting_uid_invalid", "record_id": record_id, "value": supplied_uid})
            continue
        if uid in seen_uids and seen_uids[uid] != record_id:
            issues.append(
                {"code": "meeting_uid_ambiguous", "meeting_uid": uid, "record_ids": [seen_uids[uid], record_id]}
            )
            continue
        seen_uids[uid] = record_id
        date = _date_text(first(fields, ("会议日期", "日期", "会议时间")))
        series = plain(first(fields, ("会议系列", "系列"))).strip()
        meeting_type = plain(first(fields, ("会议类型", "类型"))).strip()
        missing = [name for name, value in (("会议日期", date), ("会议系列", series), ("会议类型", meeting_type)) if not value]
        if missing:
            issues.append({"code": "required_metadata_missing", "record_id": record_id, "fields": missing})
            continue
        structured_matches = structured_by_source.get(record_id, [])
        if len(structured_matches) > 1:
            issues.append({"code": "structured_record_ambiguous", "record_id": record_id})
            continue
        structured = structured_matches[0] if structured_matches else None
        structured_fields = (structured or {}).get("fields") or {}
        structured_id = str((structured or {}).get("record_id") or "")
        official_matches = official_by_structured.get(structured_id, []) if structured_id else []
        if len(official_matches) > 1:
            issues.append({"code": "official_json_ambiguous", "record_id": record_id})
            continue
        official_fields = (official_matches[0] if official_matches else {}).get("fields") or {}
        sanitized_matches = sanitized_by_uid.get(uid, [])
        if len(sanitized_matches) > 1:
            issues.append({"code": "sanitized_record_ambiguous", "record_id": record_id})
            continue
        sanitized_fields = (sanitized_matches[0] if sanitized_matches else {}).get("fields") or {}
        source_reviewed = truthy(first(fields, ("审核状态", "已审核", "源纪要审核")))
        structured_reviewed = truthy(
            first(structured_fields, ("已审核", "标的观点审核", "结构化观点审核"))
        )
        source_draft = first(fields, ("文档链接", "源纪要链接", "会议纪要MD"))
        source_baseline = first(fields, ("审核前版本链接", "会议纪要审核前MD"))
        source_approved = first(fields, ("归档链接", "会议纪要审核后MD"))
        source_repair_key = (record_id, "meeting_minutes")
        source_repair = repairs.get(source_repair_key)
        if source_repair:
            if plain(source_repair.get("meeting_uid")).strip().lower() != uid:
                issues.append({"code": "baseline_repair_uid_mismatch", "record_id": record_id})
            elif not plain(source_baseline).strip():
                source_baseline = source_repair["target_url"]
                consumed_repairs.add(source_repair_key)
        if not plain(source_baseline).strip():
            issues.append({"code": "source_baseline_required", "record_id": record_id})
        if source_reviewed and not plain(source_approved).strip():
            issues.append({"code": "source_approved_link_missing", "record_id": record_id})

        structured_draft = first(
            structured_fields,
            ("待审核MD链接", "表格链接", "标的观点MD", "结构化观点MD"),
        )
        structured_baseline = first(
            structured_fields,
            (
                "审核前基线MD链接",
                "审核前版本链接",
                "标的观点审核前MD",
                "结构化审核前MD",
            ),
        )
        structured_approved = first(
            structured_fields,
            ("审核后归档MD链接", "标的观点审核后MD", "结构化审核后MD"),
        )
        structured_repair_key = (record_id, "structured_viewpoints")
        structured_repair = repairs.get(structured_repair_key)
        if structured_repair and plain(structured_repair.get("meeting_uid")).strip().lower() != uid:
            issues.append({"code": "baseline_repair_uid_mismatch", "record_id": record_id})
            structured_repair = None
        if structured is None:
            legacy_structured = first(fields, ("表格链接",))
            if plain(legacy_structured).strip():
                structured_draft = legacy_structured
                if structured_repair:
                    structured_baseline = structured_repair["target_url"]
                    consumed_repairs.add(structured_repair_key)
                else:
                    issues.append(
                        {"code": "structured_baseline_required", "record_id": record_id}
                    )
        elif not plain(structured_baseline).strip():
            if structured_repair:
                structured_baseline = structured_repair["target_url"]
                consumed_repairs.add(structured_repair_key)
            else:
                issues.append({"code": "structured_baseline_required", "record_id": record_id})
        if structured_reviewed and not plain(structured_approved).strip():
            issues.append({"code": "structured_approved_link_missing", "record_id": record_id})

        structured_json = first(
            structured_fields, ("正式JSON链接", "标的观点JSON", "结构化观点JSON")
        )
        official_json = first(
            official_fields,
            ("JSON链接", "正式JSON链接", "标的观点JSON", "结构化观点JSON"),
        )
        if (
            plain(structured_json).strip()
            and plain(official_json).strip()
            and plain(structured_json).strip() != plain(official_json).strip()
        ):
            issues.append({"code": "official_json_link_conflict", "record_id": record_id})
        current_structured_json = official_json or structured_json
        if plain(current_structured_json).strip() and not structured_reviewed:
            issues.append({"code": "official_json_without_structured_review", "record_id": record_id})

        target_fields = {
            "会议ID": uid,
            "会议名": meeting_name(
                first(fields, ("会议名", "文件名")), series, date
            ),
            "会议日期": date,
            "会议系列": series,
            "会议类型": meeting_type,
            "数据版本": 1,
            "会议纪要MD": source_approved if source_reviewed and plain(source_approved).strip() else source_draft,
            "会议纪要审核前MD": source_baseline,
            "会议纪要审核后MD": source_approved,
            "源纪要审核": "已审核" if source_reviewed else "未审核",
            "行业与市场观点MD": "",
            "行业与市场观点审核前MD": "",
            "行业与市场观点审核后MD": "",
            "行业与市场观点JSON": "",
            "行业与市场观点审核": "未审核",
            "标的观点MD": (
                structured_approved
                if structured_reviewed and plain(structured_approved).strip()
                else structured_draft
            ),
            "标的观点审核前MD": structured_baseline,
            "标的观点审核后MD": structured_approved,
            "标的观点JSON": current_structured_json,
            "标的观点审核": "已审核" if structured_reviewed else "未审核",
            "脱敏会议纪要MD": first(
                sanitized_fields,
                ("脱敏MD链接", "脱敏会议纪要MD", "文件链接", "文档链接"),
            ),
        }
        if len(target_fields) != 21 or set(target_fields) != {
            field["name"] for field in contract.CONTRACT.business_fields
        }:
            raise MigrationError("migration output does not match 21-field contract")
        planned.append(
            {
                "source_record_id": record_id,
                "meeting_uid_generated": not bool(supplied_uid),
                "fields": target_fields,
            }
        )
    for record_id, artifact_type in sorted(set(repairs) - consumed_repairs):
        issues.append(
            {
                "code": "baseline_repair_unmatched",
                "record_id": record_id,
                "artifact_type": artifact_type,
            }
        )
    return {
        "schema_version": 1,
        "mode": "offline-direct-cutover-plan",
        "source_count": len(source_records),
        "planned_count": len(planned),
        "issue_count": len(issues),
        "records": planned,
        "issues": issues,
    }


def canonical_hash(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def write_local_apply(path: Path, plan: dict[str, Any]) -> str:
    if plan["issue_count"]:
        raise MigrationError("migration plan has unresolved issues")
    payload = {"plan_sha256": canonical_hash(plan), **plan}
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing == payload:
            return "skipped_idempotent"
        raise MigrationError("target file already exists with different content")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return "written_local_snapshot"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a unified Base migration plan")
    parser.add_argument("--source-export", required=True)
    parser.add_argument("--structured-export")
    parser.add_argument("--official-json-export")
    parser.add_argument("--sanitized-export")
    parser.add_argument("--baseline-receipts")
    parser.add_argument("--plan-output")
    parser.add_argument("--apply-local-output")
    args = parser.parse_args(argv)
    plan = build_plan(
        read_records(args.source_export, "source"),
        read_records(args.structured_export, "structured"),
        read_records(args.official_json_export, "official JSON"),
        read_records(args.sanitized_export, "sanitized"),
        read_baseline_repairs(args.baseline_receipts),
    )
    if args.plan_output:
        Path(args.plan_output).write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    status = "dry_run"
    if args.apply_local_output:
        status = write_local_apply(Path(args.apply_local_output), plan)
    print(json.dumps({"status": status, **plan}, ensure_ascii=False, indent=2))
    return 0 if not plan["issue_count"] else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except MigrationError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from None
