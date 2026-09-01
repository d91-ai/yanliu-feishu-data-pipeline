#!/usr/bin/env python3
"""Add the human-readable meeting name and simplify the four review views."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

import apply_unified_base as base_ops
from migrate_unified_base import meeting_name, plain


FIELD_DEFINITION = {
    "type": "text",
    "name": "会议名",
    "description": "仅供人工识别，不作为去重或版本主键",
}


class RefinementError(RuntimeError):
    pass


def load_records(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RefinementError("legacy source backup invalid") from exc
    records = payload.get("records") if isinstance(payload, dict) else None
    if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
        raise RefinementError("legacy source backup records invalid")
    return records


def build_name_plan(records: list[dict[str, Any]], expected_count: int) -> dict[str, str]:
    if len(records) != expected_count:
        raise RefinementError("legacy source backup count drift")
    result: dict[str, str] = {}
    for item in records:
        record_id = str(item.get("record_id") or "").strip()
        fields = item.get("fields")
        if not record_id or record_id in result or not isinstance(fields, dict):
            raise RefinementError("legacy source backup identity invalid")
        series = plain(fields.get("会议系列")).strip()
        date = base_ops.date_text(fields.get("会议日期"))
        if not date:
            raise RefinementError("legacy meeting date invalid")
        result[record_id] = meeting_name(fields.get("文件名"), series, date)
    if any(not value for value in result.values()):
        raise RefinementError("derived meeting name missing")
    return result


def desired_views(schema_path: Path) -> list[dict[str, Any]]:
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RefinementError("unified Base schema invalid") from exc
    views = schema.get("views") if isinstance(schema, dict) else None
    if not isinstance(views, list) or len(views) != 4:
        raise RefinementError("unified Base view contract invalid")
    normalized = []
    for item in views:
        if not isinstance(item, dict) or not isinstance(item.get("fields"), list):
            raise RefinementError("unified Base view contract invalid")
        normalized.append(
            {"name": str(item.get("name") or ""), "type": "grid", "visible_fields": list(item["fields"])}
        )
    if any(not item["name"] or "会议名" not in item["visible_fields"] for item in normalized):
        raise RefinementError("unified Base views must expose meeting name")
    return normalized


def name_plan_hash(plan: Mapping[str, str]) -> str:
    encoded = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def find_name_field(fields: list[dict[str, Any]]) -> dict[str, Any] | None:
    matches = [item for item in fields if base_ops.field_name(item) == "会议名"]
    if len(matches) > 1:
        raise RefinementError("meeting name field ambiguous")
    if matches and str(matches[0].get("type") or "") != "text":
        raise RefinementError("meeting name field type conflict")
    return matches[0] if matches else None


def execute(
    *,
    client: base_ops.LarkBaseClient,
    base_token: str,
    table_id: str,
    table_name: str,
    names: Mapping[str, str],
    views: list[dict[str, Any]],
    apply: bool,
) -> dict[str, Any]:
    table = client.table(base_token, table_id)
    if str(table.get("name") or "") != table_name:
        raise RefinementError("target table identity drift")
    fields = client.fields(base_token, table_id)
    field = find_name_field(fields)
    if field is None and apply:
        try:
            client.field_create(base_token, table_id, FIELD_DEFINITION)
        except base_ops.CliError:
            pass
        field = find_name_field(client.fields(base_token, table_id))
        if field is None:
            raise RefinementError("meeting name field create unconfirmed")

    changed = len(names) if field is None else 0
    if field is not None:
        name_field_id = base_ops.field_id(field)
        rows = client.records(base_token, table_id, list(names), [name_field_id])
        if set(rows) != set(names):
            raise RefinementError("target record identity drift")
        for record_id, desired in names.items():
            current = plain(rows[record_id]["by_id"].get(name_field_id)).strip()
            if current == desired:
                continue
            # The only accepted non-empty transition is the one-time upgrade
            # from the previously deployed bare label to ``date - label``.
            if current and not desired.endswith(f" - {current}"):
                raise RefinementError(f"existing meeting name differs:{record_id}")
            changed += 1
            if apply:
                try:
                    client.record_update(base_token, table_id, record_id, {name_field_id: desired})
                except base_ops.CliError:
                    check = client.records(base_token, table_id, [record_id], [name_field_id]).get(record_id)
                    if check is None or plain(check["by_id"].get(name_field_id)).strip() != desired:
                        raise

    if apply:
        if field is None:
            raise RefinementError("meeting name field unavailable")
        base_ops.ensure_views(
            client,
            {"base_token": base_token, "source_table_id": table_id, "views": views},
        )
        name_field_id = base_ops.field_id(find_name_field(client.fields(base_token, table_id)) or {})
        rows = client.records(base_token, table_id, list(names), [name_field_id])
        for record_id, desired in names.items():
            if plain(rows[record_id]["by_id"].get(name_field_id)).strip() != desired:
                raise RefinementError(f"meeting name verification failed:{record_id}")
        live_views = client.views(base_token, table_id)
        live_ids = {
            str(item.get("name") or item.get("view_name") or ""): str(item.get("id") or item.get("view_id") or "")
            for item in live_views
        }
        for view in views:
            view_id = live_ids.get(view["name"], "")
            if not view_id or client.view_fields(base_token, table_id, view_id) != view["visible_fields"]:
                raise RefinementError(f"view verification failed:{view['name']}")

    return {
        "status": "applied_verified" if apply else "planned",
        "record_count": len(names),
        "meeting_name_field": "ready" if field is not None else "create",
        "record_updates": changed,
        "view_count": len(views),
        "name_plan_sha256": name_plan_hash(names),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-token", required=True)
    parser.add_argument("--table-id", required=True)
    parser.add_argument("--table-name", default="会议数据库")
    parser.add_argument("--legacy-source-backup", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--expected-record-count", type=int, default=73)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    names = build_name_plan(load_records(args.legacy_source_backup), args.expected_record_count)
    result = execute(
        client=base_ops.LarkBaseClient(),
        base_token=args.base_token,
        table_id=args.table_id,
        table_name=args.table_name,
        names=names,
        views=desired_views(args.schema),
        apply=args.apply,
    )
    if args.receipt and args.apply:
        base_ops.write_private_json(args.receipt, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
