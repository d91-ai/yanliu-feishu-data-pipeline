#!/usr/bin/env python3
"""Dry-run-first, resumable direct cutover of the unified Feishu Base table."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any, Mapping


UID_PATTERN = re.compile(r"mtg_[0-9a-f]{32}")
REVIEW_STATUSES = {"未审核", "已审核", "需重审"}
EXPORT_LABELS = ("source", "structured", "sanitized", "official")
UNCHANGED_REUSED_FIELDS = {"会议日期", "会议系列", "会议类型"}
SINGLE_SELECT_BUSINESS_FIELDS = {
    "会议系列",
    "会议类型",
    "源纪要审核",
    "行业与市场观点审核",
    "标的观点审核",
}


class CutoverError(RuntimeError):
    pass


class CliError(CutoverError):
    pass


def load_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CutoverError(f"invalid {label}") from exc
    if not isinstance(value, dict):
        raise CutoverError(f"{label} must be a JSON object")
    return value


def canonical_hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def write_private_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def read_export(path: Path, label: str) -> dict[str, Any]:
    value = load_json_object(path, f"{label} export")
    records = value.get("records")
    if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
        raise CutoverError(f"{label} export records invalid")
    return value


def validate_field_definition(value: Any, *, attachment: bool = False) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CutoverError("field definition must be an object")
    definition = dict(value)
    name = str(definition.get("name") or "").strip()
    field_type = str(definition.get("type") or "").strip()
    allowed = {"text", "number", "select", "datetime", "attachment"}
    if not name or field_type not in allowed:
        raise CutoverError("field definition identity invalid")
    if attachment != (field_type == "attachment"):
        raise CutoverError("attachment field definition mismatch")
    if field_type == "select":
        options = definition.get("options")
        if definition.get("multiple") is not False or not isinstance(options, list) or not options:
            raise CutoverError("select field definition invalid")
        names = [str(item.get("name") or "") for item in options if isinstance(item, dict)]
        if len(names) != len(options) or any(not item for item in names) or len(set(names)) != len(names):
            raise CutoverError("select field options invalid")
    return definition


def validate_config(value: Mapping[str, Any]) -> dict[str, Any]:
    config = dict(value)
    if config.get("schema_version") != 1:
        raise CutoverError("unsupported cutover config")
    if not str(config.get("base_token") or "") or not str(config.get("source_table_id") or "").startswith("tbl"):
        raise CutoverError("cutover Base identity invalid")
    if not str(config.get("current_table_name") or "") or not str(config.get("target_table_name") or ""):
        raise CutoverError("cutover table name invalid")
    snapshot_tables = config.get("snapshot_tables")
    if not isinstance(snapshot_tables, dict) or set(snapshot_tables) != set(EXPORT_LABELS):
        raise CutoverError("cutover snapshot tables invalid")
    for label, item in snapshot_tables.items():
        if (
            not isinstance(item, dict)
            or not str(item.get("table_id") or "").startswith("tbl")
            or isinstance(item.get("record_count"), bool)
            or not isinstance(item.get("record_count"), int)
            or item["record_count"] < 0
        ):
            raise CutoverError(f"cutover snapshot table invalid:{label}")
    reused = config.get("reused_fields")
    new_fields = config.get("new_fields")
    if not isinstance(reused, list) or len(reused) != 8:
        raise CutoverError("cutover reused fields invalid")
    if not isinstance(new_fields, list) or len(new_fields) != 12:
        raise CutoverError("cutover new fields invalid")
    targets: list[str] = []
    field_ids: set[str] = set()
    for item in reused:
        if not isinstance(item, dict):
            raise CutoverError("cutover reused field invalid")
        definition = validate_field_definition(item.get("definition"))
        target_name = str(item.get("target_name") or "")
        field_id = str(item.get("field_id") or "")
        current_name = str(item.get("current_name") or "")
        if definition["name"] != target_name or not field_id.startswith("fld") or not current_name:
            raise CutoverError("cutover reused field identity invalid")
        if field_id in field_ids:
            raise CutoverError("cutover reused field duplicate")
        field_ids.add(field_id)
        targets.append(target_name)
    normalized_new = [validate_field_definition(item) for item in new_fields]
    targets.extend(str(item["name"]) for item in normalized_new)
    if len(targets) != 20 or len(set(targets)) != 20:
        raise CutoverError("cutover business field names invalid")
    validate_field_definition(config.get("attachment_field"), attachment=True)
    views = config.get("views")
    if not isinstance(views, list) or len(views) != 4:
        raise CutoverError("cutover views invalid")
    for view in views:
        if (
            not isinstance(view, dict)
            or not str(view.get("name") or "")
            or view.get("type") != "grid"
            or not isinstance(view.get("visible_fields"), list)
            or any(name not in targets for name in view["visible_fields"])
        ):
            raise CutoverError("cutover view invalid")
    workflows = config.get("old_workflows")
    if (
        not isinstance(workflows, dict)
        or len(workflows) != 9
        or any(status not in {"enabled", "disabled"} for status in workflows.values())
    ):
        raise CutoverError("cutover workflows invalid")
    return config


def validate_schema(value: Mapping[str, Any], config: Mapping[str, Any]) -> list[str]:
    fields = value.get("fields")
    if value.get("schema_version") != 1 or not isinstance(fields, list) or len(fields) != 20:
        raise CutoverError("unified Base schema invalid")
    names = [str(item.get("name") or "") for item in fields if isinstance(item, dict)]
    config_names = [str(item["target_name"]) for item in config["reused_fields"]] + [
        str(item["name"]) for item in config["new_fields"]
    ]
    if len(config_names) != 20 or set(names) != set(config_names):
        raise CutoverError("unified Base schema and cutover config differ")
    return names


def validate_migration(value: Mapping[str, Any], business_names: list[str], expected_count: int) -> dict[str, Any]:
    migration = dict(value)
    plan_hash = str(migration.get("plan_sha256") or "")
    unsigned = {key: item for key, item in migration.items() if key != "plan_sha256"}
    if not re.fullmatch(r"[0-9a-f]{64}", plan_hash) or canonical_hash(unsigned) != plan_hash:
        raise CutoverError("migration plan hash invalid")
    if (
        migration.get("schema_version") != 1
        or migration.get("mode") != "offline-direct-cutover-plan"
        or migration.get("issue_count") != 0
        or migration.get("planned_count") != expected_count
    ):
        raise CutoverError("migration plan is not applyable")
    records = migration.get("records")
    if not isinstance(records, list) or len(records) != expected_count:
        raise CutoverError("migration record count invalid")
    record_ids: set[str] = set()
    meeting_ids: set[str] = set()
    for item in records:
        if not isinstance(item, dict) or item.get("meeting_uid_generated") is not False:
            raise CutoverError("migration historical meeting identity invalid")
        record_id = str(item.get("source_record_id") or "")
        fields = item.get("fields")
        if not record_id or record_id in record_ids or not isinstance(fields, dict) or list(fields) != business_names:
            raise CutoverError("migration record identity or fields invalid")
        record_ids.add(record_id)
        uid = str(fields.get("会议ID") or "")
        if not UID_PATTERN.fullmatch(uid) or uid in meeting_ids:
            raise CutoverError("migration meeting ID invalid or duplicate")
        meeting_ids.add(uid)
        if fields.get("数据版本") != 1:
            raise CutoverError("migration data version invalid")
        for name in ("源纪要审核", "行业与市场观点审核", "标的观点审核"):
            if fields.get(name) not in REVIEW_STATUSES:
                raise CutoverError("migration review status invalid")
        for name, cell in fields.items():
            if (name.endswith("MD") or name.endswith("JSON")) and cell not in (None, ""):
                if "https://" not in str(cell):
                    raise CutoverError("migration link field invalid")
    return migration


def normalize_cell(value: Any) -> Any:
    if value in (None, ""):
        return ""
    if isinstance(value, list):
        return [normalize_cell(item) for item in value]
    if isinstance(value, dict):
        return {key: normalize_cell(item) for key, item in sorted(value.items())}
    return value


def date_text(value: Any) -> str:
    text = str(value or "").strip()
    match = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", text)
    if match:
        return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
    try:
        number = float(text)
    except (TypeError, ValueError):
        return ""
    if number < 10_000_000_000:
        number *= 1000
    return datetime.fromtimestamp(number / 1000, timezone(timedelta(hours=8))).date().isoformat()


def business_link_url(value: Any) -> Any:
    if not isinstance(value, str):
        return normalize_cell(value)
    text = value.strip()
    if text.startswith("https://"):
        return text
    marker = text.rfind("](")
    if text.startswith("[") and marker > 0 and text.endswith(")"):
        url = text[marker + 2 : -1].strip()
        if url.startswith("https://"):
            return url
    return normalize_cell(value)


def normalized_business_value(name: str, value: Any) -> Any:
    if name == "会议日期":
        return date_text(value)
    if name.endswith("MD") or name.endswith("JSON"):
        return business_link_url(value)
    if name in SINGLE_SELECT_BUSINESS_FIELDS and isinstance(value, list):
        if not value:
            return ""
        if len(value) == 1:
            return normalize_cell(value[0])
    if name == "数据版本" and value not in (None, ""):
        try:
            return int(value)
        except (TypeError, ValueError):
            return value
    return normalize_cell(value)


class LarkBaseClient:
    def __init__(self, cli: str = "lark-cli"):
        self.cli = cli

    def call(self, command: str, arguments: list[str]) -> dict[str, Any]:
        process = subprocess.run(
            [self.cli, "base", command, *arguments, "--format", "json", "--as", "user"],
            capture_output=True,
            text=True,
            check=False,
        )
        if process.returncode != 0:
            detail = process.stderr.strip() or process.stdout.strip()
            raise CliError(f"lark-cli {command} failed:{detail[:500]}")
        try:
            value = json.loads(process.stdout)
        except json.JSONDecodeError as exc:
            raise CliError(f"lark-cli {command} returned invalid JSON") from exc
        if not isinstance(value, dict) or value.get("ok") is not True:
            raise CliError(f"lark-cli {command} did not confirm success")
        return value

    def fields(self, base_token: str, table_id: str) -> list[dict[str, Any]]:
        value = self.call("+field-list", ["--base-token", base_token, "--table-id", table_id, "--limit", "200"])
        fields = value.get("data", {}).get("fields")
        if not isinstance(fields, list):
            raise CliError("field-list response invalid")
        return [item for item in fields if isinstance(item, dict)]

    def table(self, base_token: str, table_id: str) -> dict[str, Any]:
        value = self.call("+table-get", ["--base-token", base_token, "--table-id", table_id])
        table = value.get("data", {}).get("table")
        if not isinstance(table, dict):
            raise CliError("table-get response invalid")
        return table

    def views(self, base_token: str, table_id: str) -> list[dict[str, Any]]:
        value = self.call("+view-list", ["--base-token", base_token, "--table-id", table_id])
        views = value.get("data", {}).get("views")
        if not isinstance(views, list):
            raise CliError("view-list response invalid")
        return [item for item in views if isinstance(item, dict)]

    def workflows(self, base_token: str) -> dict[str, str]:
        value = self.call("+workflow-list", ["--base-token", base_token])
        items = value.get("data", {}).get("items")
        if not isinstance(items, list):
            raise CliError("workflow-list response invalid")
        return {
            str(item.get("workflow_id") or item.get("id") or ""): str(item.get("status") or "")
            for item in items
            if isinstance(item, dict)
        }

    def records(
        self,
        base_token: str,
        table_id: str,
        record_ids: list[str],
        field_ids: list[str],
    ) -> dict[str, dict[str, Any]]:
        arguments = ["--base-token", base_token, "--table-id", table_id]
        arguments.extend(["--json", json.dumps({"record_id_list": record_ids}, separators=(",", ":"))])
        for field_id in field_ids:
            arguments.extend(["--field-id", field_id])
        value = self.call("+record-get", arguments)
        data = value.get("data", {})
        rows = data.get("data")
        ids = data.get("record_id_list")
        fields = data.get("fields")
        returned_field_ids = data.get("field_id_list")
        if not all(isinstance(item, list) for item in (rows, ids, fields, returned_field_ids)):
            raise CliError("record-get response invalid")
        if len(rows) != len(ids) or any(not isinstance(row, list) or len(row) != len(fields) for row in rows):
            raise CliError("record-get row shape invalid")
        result: dict[str, dict[str, Any]] = {}
        for record_id, row in zip(ids, rows):
            result[str(record_id)] = {
                "by_name": dict(zip(fields, row)),
                "by_id": dict(zip(returned_field_ids, row)),
            }
        return result

    def field_create(self, base_token: str, table_id: str, definition: Mapping[str, Any]) -> None:
        self.call(
            "+field-create",
            ["--base-token", base_token, "--table-id", table_id, "--json", json.dumps(definition, ensure_ascii=False, separators=(",", ":"))],
        )

    def field_update(self, base_token: str, table_id: str, field_id: str, definition: Mapping[str, Any]) -> None:
        self.call(
            "+field-update",
            ["--base-token", base_token, "--table-id", table_id, "--field-id", field_id, "--json", json.dumps(definition, ensure_ascii=False, separators=(",", ":")), "--yes"],
        )

    def record_update(self, base_token: str, table_id: str, record_id: str, patch: Mapping[str, Any]) -> None:
        self.call(
            "+record-upsert",
            ["--base-token", base_token, "--table-id", table_id, "--record-id", record_id, "--json", json.dumps(patch, ensure_ascii=False, separators=(",", ":"))],
        )

    def table_rename(self, base_token: str, table_id: str, name: str) -> None:
        self.call("+table-update", ["--base-token", base_token, "--table-id", table_id, "--name", name])

    def view_create(self, base_token: str, table_id: str, name: str, view_type: str) -> None:
        self.call(
            "+view-create",
            ["--base-token", base_token, "--table-id", table_id, "--json", json.dumps({"name": name, "type": view_type}, ensure_ascii=False, separators=(",", ":"))],
        )

    def view_rename(self, base_token: str, table_id: str, view_id: str, name: str) -> None:
        self.call(
            "+view-rename",
            ["--base-token", base_token, "--table-id", table_id, "--view-id", view_id, "--name", name],
        )

    def view_set_fields(self, base_token: str, table_id: str, view_id: str, fields: list[str]) -> None:
        self.call(
            "+view-set-visible-fields",
            ["--base-token", base_token, "--table-id", table_id, "--view-id", view_id, "--json", json.dumps({"visible_fields": fields}, ensure_ascii=False, separators=(",", ":"))],
        )

    def view_fields(self, base_token: str, table_id: str, view_id: str) -> list[str]:
        value = self.call(
            "+view-get-visible-fields",
            ["--base-token", base_token, "--table-id", table_id, "--view-id", view_id],
        )
        fields = value.get("data", {}).get("visible_fields")
        if not isinstance(fields, list):
            raise CliError("view visible fields response invalid")
        return [str(item) for item in fields]


def field_id(field: Mapping[str, Any]) -> str:
    return str(field.get("id") or field.get("field_id") or "")


def field_name(field: Mapping[str, Any]) -> str:
    return str(field.get("name") or field.get("field_name") or "")


def definition_matches(field: Mapping[str, Any], definition: Mapping[str, Any], *, name: str | None = None) -> bool:
    if field_name(field) != (name if name is not None else definition.get("name")):
        return False
    if str(field.get("type") or "") != str(definition.get("type") or ""):
        return False
    if "multiple" in definition and field.get("multiple") is not definition.get("multiple"):
        return False
    expected_style = definition.get("style")
    actual_style = field.get("style")
    if isinstance(expected_style, dict):
        if not isinstance(actual_style, dict) or any(actual_style.get(key) != value for key, value in expected_style.items()):
            return False
    expected_options = definition.get("options")
    if isinstance(expected_options, list):
        actual_options = field.get("options") or []
        if [item.get("name") for item in actual_options if isinstance(item, dict)] != [
            item.get("name") for item in expected_options if isinstance(item, dict)
        ]:
            return False
    return True


def index_fields(fields: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    by_id = {field_id(item): item for item in fields if field_id(item)}
    by_name: dict[str, list[dict[str, Any]]] = {}
    for item in fields:
        by_name.setdefault(field_name(item), []).append(item)
    return by_id, by_name


def assert_field_state(config: Mapping[str, Any], fields: list[dict[str, Any]]) -> dict[str, str]:
    by_id, by_name = index_fields(fields)
    resolved: dict[str, str] = {}
    for item in config["reused_fields"]:
        existing = by_id.get(str(item["field_id"]))
        if existing is None:
            raise CutoverError(f"reused field missing:{item['target_name']}")
        actual_name = field_name(existing)
        if actual_name not in {item["current_name"], item["target_name"]}:
            raise CutoverError(f"reused field name drift:{item['target_name']}")
        if not definition_matches(existing, item["definition"], name=actual_name):
            raise CutoverError(f"reused field definition drift:{item['target_name']}")
        target_matches = by_name.get(str(item["target_name"]), [])
        if target_matches and any(field_id(match) != item["field_id"] for match in target_matches):
            raise CutoverError(f"target field name collision:{item['target_name']}")
        resolved[str(item["target_name"])] = str(item["field_id"])
    for definition in [*config["new_fields"], config["attachment_field"]]:
        matches = by_name.get(str(definition["name"]), [])
        if len(matches) > 1:
            raise CutoverError(f"new field ambiguous:{definition['name']}")
        if matches:
            if not definition_matches(matches[0], definition):
                raise CutoverError(f"new field definition drift:{definition['name']}")
            resolved[str(definition["name"])] = field_id(matches[0])
    return resolved


def export_rows(export: Mapping[str, Any]) -> tuple[list[str], list[str], dict[str, dict[str, Any]]]:
    records = export.get("records") or []
    record_ids = [str(item.get("record_id") or "") for item in records]
    field_names = list((records[0].get("fields") or {}).keys()) if records else []
    expected: dict[str, dict[str, Any]] = {}
    for item in records:
        fields = item.get("fields") or {}
        if list(fields) != field_names:
            raise CutoverError("export field projection is inconsistent")
        record_id = str(item.get("record_id") or "")
        if not record_id or record_id in expected:
            raise CutoverError("export record identity invalid")
        expected[record_id] = {name: normalize_cell(fields.get(name)) for name in field_names}
    return record_ids, field_names, expected


def verify_export(client: LarkBaseClient, base_token: str, table_id: str, export: Mapping[str, Any]) -> None:
    record_ids, field_names, expected = export_rows(export)
    actual = client.records(base_token, table_id, record_ids, field_names)
    if set(actual) != set(expected):
        raise CutoverError("live export record set drift")
    for record_id in record_ids:
        live = {name: normalize_cell(actual[record_id]["by_name"].get(name)) for name in field_names}
        if live != expected[record_id]:
            raise CutoverError(f"live export value drift:{record_id}")


def workflow_state_matches(actual: Mapping[str, str], expected: Mapping[str, str]) -> bool:
    return all(actual.get(workflow_id) == status for workflow_id, status in expected.items())


def preflight(
    client: LarkBaseClient,
    config: Mapping[str, Any],
    migration: Mapping[str, Any],
    exports: Mapping[str, Mapping[str, Any]],
    *,
    maintenance: bool = False,
) -> dict[str, Any]:
    base_token = str(config["base_token"])
    table_id = str(config["source_table_id"])
    table = client.table(base_token, table_id)
    if str(table.get("id") or table.get("table_id") or "") != table_id:
        raise CutoverError("source table identity drift")
    allowed_table_names = {str(config["current_table_name"]), str(config["target_table_name"])}
    if str(table.get("name") or "") not in allowed_table_names:
        raise CutoverError("source table name drift")
    fields = client.fields(base_token, table_id)
    resolved = assert_field_state(config, fields)
    views = client.views(base_token, table_id)
    target_view_names = {str(item["name"]) for item in config["views"]}
    for name in target_view_names:
        matches = [item for item in views if str(item.get("name") or item.get("view_name") or "") == name]
        if len(matches) > 1 or (matches and str(matches[0].get("type") or matches[0].get("view_type") or "") != "grid"):
            raise CutoverError(f"target view conflict:{name}")
    workflows = client.workflows(base_token)
    expected_workflows = (
        {workflow_id: "disabled" for workflow_id in config["old_workflows"]}
        if maintenance
        else config["old_workflows"]
    )
    if not workflow_state_matches(workflows, expected_workflows):
        raise CutoverError("old workflow state drift")
    legacy_state = (
        str(table.get("name") or "") == config["current_table_name"]
        and all(
            field_name(next(item for item in fields if field_id(item) == reused["field_id"]))
            == reused["current_name"]
            for reused in config["reused_fields"]
        )
        and not any(name in resolved for name in [item["name"] for item in config["new_fields"]])
    )
    if legacy_state:
        for label in EXPORT_LABELS:
            spec = config["snapshot_tables"][label]
            if len(exports[label]["records"]) != spec["record_count"]:
                raise CutoverError(f"snapshot count drift:{label}")
            verify_export(client, base_token, str(spec["table_id"]), exports[label])
    migration_ids = {str(item["source_record_id"]) for item in migration["records"]}
    source_ids = {str(item["record_id"]) for item in exports["source"]["records"]}
    if migration_ids != source_ids:
        raise CutoverError("migration and source export identities differ")
    return {
        "table": table,
        "fields": fields,
        "views": views,
        "workflows": workflows,
        "resolved_fields": resolved,
        "legacy_state": legacy_state,
        "missing_new_fields": [
            item["name"] for item in [*config["new_fields"], config["attachment_field"]]
            if item["name"] not in resolved
        ],
        "missing_views": [
            name for name in target_view_names
            if not any(str(item.get("name") or item.get("view_name") or "") == name for item in views)
        ],
    }


def validate_maintenance_proof(value: Mapping[str, Any], config: Mapping[str, Any], plan_hash: str) -> None:
    if (
        value.get("schema_version") != 1
        or value.get("base_token") != config["base_token"]
        or value.get("source_table_id") != config["source_table_id"]
        or value.get("migration_plan_sha256") != plan_hash
        or value.get("old_services_paused") is not True
    ):
        raise CutoverError("maintenance proof identity invalid")
    statuses = value.get("workflow_statuses")
    if not isinstance(statuses, dict) or any(statuses.get(item) != "disabled" for item in config["old_workflows"]):
        raise CutoverError("maintenance proof workflow state invalid")


def save_backup(
    backup_dir: Path,
    config_path: Path,
    schema_path: Path,
    migration_path: Path,
    export_paths: Mapping[str, Path],
    state: Mapping[str, Any],
    config: Mapping[str, Any],
    exports: Mapping[str, Mapping[str, Any]],
) -> None:
    backup_dir.mkdir(parents=True, exist_ok=True)
    for source in [config_path, schema_path, migration_path, *export_paths.values()]:
        target = backup_dir / source.name
        if target.exists() and target.read_bytes() != source.read_bytes():
            raise CutoverError("backup file conflict")
        if not target.exists():
            shutil.copy2(source, target)
            os.chmod(target, 0o600)
    rollback_records = []
    source_records = {str(item["record_id"]): item.get("fields") or {} for item in exports["source"]["records"]}
    for record_id, fields in source_records.items():
        patch = {
            str(item["field_id"]): fields.get(str(item["current_name"]))
            for item in config["reused_fields"]
            if str(item["current_name"]) in fields
        }
        rollback_records.append({"record_id": record_id, "patch_by_field_id": patch})
    rollback = {
        "schema_version": 1,
        "destructive_actions": 0,
        "table_name": config["current_table_name"],
        "field_names": {str(item["field_id"]): str(item["current_name"]) for item in config["reused_fields"]},
        "record_patches": rollback_records,
        "workflow_statuses": config["old_workflows"],
        "note": "Disable unified runtime first; restore values and names by stable IDs; do not delete new fields or files.",
    }
    write_private_json(backup_dir / "rollback-plan.json", rollback)
    write_private_json(
        backup_dir / "live-resource-state.json",
        {
            "schema_version": 1,
            "table": state["table"],
            "fields": state["fields"],
            "views": state["views"],
            "workflows": state["workflows"],
        },
    )


def _field_is_ready(client: LarkBaseClient, config: Mapping[str, Any], definition: Mapping[str, Any]) -> bool:
    fields = client.fields(str(config["base_token"]), str(config["source_table_id"]))
    matches = [item for item in fields if field_name(item) == definition["name"]]
    return len(matches) == 1 and definition_matches(matches[0], definition)


def create_missing_fields(client: LarkBaseClient, config: Mapping[str, Any]) -> dict[str, str]:
    base_token = str(config["base_token"])
    table_id = str(config["source_table_id"])
    for definition in [*config["new_fields"], config["attachment_field"]]:
        if _field_is_ready(client, config, definition):
            continue
        try:
            client.field_create(base_token, table_id, definition)
        except CliError:
            if not _field_is_ready(client, config, definition):
                raise
        if not _field_is_ready(client, config, definition):
            raise CutoverError(f"field create unconfirmed:{definition['name']}")
    fields = client.fields(base_token, table_id)
    return assert_field_state(config, fields)


def record_matches(
    row: Mapping[str, Any], desired: Mapping[str, Any], target_field_ids: Mapping[str, str]
) -> bool:
    by_id = row.get("by_id") or {}
    return all(
        normalized_business_value(name, by_id.get(target_field_ids[name]))
        == normalized_business_value(name, value)
        for name, value in desired.items()
    )


def update_records(
    client: LarkBaseClient,
    config: Mapping[str, Any],
    migration: Mapping[str, Any],
    target_field_ids: Mapping[str, str],
    journal_path: Path,
    journal: dict[str, Any],
) -> None:
    base_token = str(config["base_token"])
    table_id = str(config["source_table_id"])
    field_ids = [target_field_ids[name] for name in target_field_ids]
    completed = set(journal.get("records_completed") or [])
    reused_names = {str(item["target_name"]) for item in config["reused_fields"]}
    for item in migration["records"]:
        record_id = str(item["source_record_id"])
        desired = item["fields"]
        rows = client.records(base_token, table_id, [record_id], field_ids)
        row = rows.get(record_id)
        if row is None:
            raise CutoverError(f"migration record missing:{record_id}")
        if record_matches(row, desired, target_field_ids):
            completed.add(record_id)
        else:
            patch: dict[str, Any] = {}
            for name, value in desired.items():
                field = target_field_ids[name]
                current = normalized_business_value(name, row["by_id"].get(field))
                expected = normalized_business_value(name, value)
                if current == expected:
                    continue
                if name in UNCHANGED_REUSED_FIELDS:
                    raise CutoverError(f"unchanged source metadata drift:{record_id}:{name}")
                if name in reused_names or value not in (None, ""):
                    patch[field] = value if value not in (None, "") else None
            if patch:
                try:
                    client.record_update(base_token, table_id, record_id, patch)
                except CliError:
                    check = client.records(base_token, table_id, [record_id], field_ids).get(record_id)
                    if check is None or not record_matches(check, desired, target_field_ids):
                        raise
            check = client.records(base_token, table_id, [record_id], field_ids).get(record_id)
            if check is None or not record_matches(check, desired, target_field_ids):
                raise CutoverError(f"record update unconfirmed:{record_id}")
            completed.add(record_id)
        journal["records_completed"] = sorted(completed)
        journal["stage"] = "records"
        write_private_json(journal_path, journal)


def rename_reused_fields(client: LarkBaseClient, config: Mapping[str, Any]) -> None:
    base_token = str(config["base_token"])
    table_id = str(config["source_table_id"])
    for item in config["reused_fields"]:
        if item["current_name"] == item["target_name"]:
            continue
        fields = client.fields(base_token, table_id)
        by_id, _ = index_fields(fields)
        existing = by_id.get(str(item["field_id"]))
        if existing is None:
            raise CutoverError(f"rename field missing:{item['target_name']}")
        if definition_matches(existing, item["definition"]):
            continue
        if not definition_matches(existing, item["definition"], name=str(item["current_name"])):
            raise CutoverError(f"rename field drift:{item['target_name']}")
        try:
            client.field_update(base_token, table_id, str(item["field_id"]), item["definition"])
        except CliError:
            pass
        fields = client.fields(base_token, table_id)
        by_id, _ = index_fields(fields)
        if not definition_matches(by_id.get(str(item["field_id"])) or {}, item["definition"]):
            raise CutoverError(f"field rename unconfirmed:{item['target_name']}")


def rename_table(client: LarkBaseClient, config: Mapping[str, Any]) -> None:
    base_token = str(config["base_token"])
    table_id = str(config["source_table_id"])
    table = client.table(base_token, table_id)
    if table.get("name") == config["target_table_name"]:
        return
    if table.get("name") != config["current_table_name"]:
        raise CutoverError("table rename source drift")
    try:
        client.table_rename(base_token, table_id, str(config["target_table_name"]))
    except CliError:
        pass
    if client.table(base_token, table_id).get("name") != config["target_table_name"]:
        raise CutoverError("table rename unconfirmed")


def ensure_views(client: LarkBaseClient, config: Mapping[str, Any]) -> None:
    base_token = str(config["base_token"])
    table_id = str(config["source_table_id"])
    fields = client.fields(base_token, table_id)
    _by_id, by_name = index_fields(fields)
    for desired in config["views"]:
        target_name = str(desired["name"])
        temporary_name = f"{target_name}（配置中）"
        visible_field_ids: list[str] = []
        for name in desired["visible_fields"]:
            matches = by_name.get(str(name)) or []
            if len(matches) != 1:
                raise CutoverError(f"view field unresolved:{target_name}:{name}")
            visible_field_ids.append(field_id(matches[0]))
        views = client.views(base_token, table_id)
        matches = [
            item
            for item in views
            if str(item.get("name") or item.get("view_name") or "")
            in {target_name, temporary_name}
        ]
        if len(matches) > 1:
            raise CutoverError(f"view ambiguous:{target_name}")
        if not matches:
            try:
                client.view_create(base_token, table_id, target_name, "grid")
            except CliError:
                pass
            views = client.views(base_token, table_id)
            matches = [
                item
                for item in views
                if str(item.get("name") or item.get("view_name") or "") == target_name
            ]
        if len(matches) != 1:
            raise CutoverError(f"view create unconfirmed:{target_name}")
        view = matches[0]
        if str(view.get("type") or view.get("view_type") or "") != "grid":
            raise CutoverError(f"view type conflict:{target_name}")
        view_id = str(view.get("id") or view.get("view_id") or "")
        desired_fields = list(desired["visible_fields"])
        current_name = str(view.get("name") or view.get("view_name") or "")
        if client.view_fields(base_token, table_id, view_id) != desired_fields:
            if target_name in desired_fields and current_name == target_name:
                client.view_rename(base_token, table_id, view_id, temporary_name)
                current_name = temporary_name
            phases = [(desired_fields, visible_field_ids)]
            if target_name in desired_fields:
                without_status_fields = [name for name in desired_fields if name != target_name]
                without_status_ids = [
                    identifier
                    for name, identifier in zip(desired_fields, visible_field_ids)
                    if name != target_name
                ]
                stable_fields = [name for name in desired_fields if "审核" not in name]
                stable_ids = [
                    identifier
                    for name, identifier in zip(desired_fields, visible_field_ids)
                    if "审核" not in name
                ]
                phases = [
                    (stable_fields, stable_ids),
                    (without_status_fields, without_status_ids),
                    (desired_fields, visible_field_ids),
                ]
            for phase_fields, phase_ids in phases:
                if client.view_fields(base_token, table_id, view_id) == phase_fields:
                    continue
                update_error: CliError | None = None
                try:
                    client.view_set_fields(base_token, table_id, view_id, phase_ids)
                except CliError as exc:
                    update_error = exc
                if client.view_fields(base_token, table_id, view_id) != phase_fields:
                    if update_error is not None:
                        raise update_error
                    raise CutoverError(f"view fields unconfirmed:{target_name}")
        if current_name == temporary_name:
            try:
                client.view_rename(base_token, table_id, view_id, target_name)
            except CliError:
                pass
            views = client.views(base_token, table_id)
            restored = [
                item
                for item in views
                if str(item.get("name") or item.get("view_name") or "") == target_name
            ]
            if len(restored) != 1 or str(restored[0].get("id") or restored[0].get("view_id") or "") != view_id:
                raise CutoverError(f"view rename unconfirmed:{target_name}")


def verify_final_records(
    client: LarkBaseClient,
    config: Mapping[str, Any],
    migration: Mapping[str, Any],
    target_field_ids: Mapping[str, str],
) -> None:
    base_token = str(config["base_token"])
    table_id = str(config["source_table_id"])
    record_ids = [str(item["source_record_id"]) for item in migration["records"]]
    rows = client.records(base_token, table_id, record_ids, list(target_field_ids.values()))
    if set(rows) != set(record_ids):
        raise CutoverError("final record set mismatch")
    for item in migration["records"]:
        record_id = str(item["source_record_id"])
        if not record_matches(rows[record_id], item["fields"], target_field_ids):
            raise CutoverError(f"final record mismatch:{record_id}")


def apply_cutover(
    client: LarkBaseClient,
    config: Mapping[str, Any],
    migration: Mapping[str, Any],
    state: Mapping[str, Any],
    *,
    journal_path: Path,
) -> dict[str, Any]:
    plan_hash = str(migration["plan_sha256"])
    if journal_path.exists():
        journal = load_json_object(journal_path, "cutover journal")
        if (
            journal.get("schema_version") != 1
            or journal.get("migration_plan_sha256") != plan_hash
            or journal.get("base_token") != config["base_token"]
            or journal.get("source_table_id") != config["source_table_id"]
        ):
            raise CutoverError("cutover journal conflict")
    else:
        if not state["legacy_state"]:
            raise CutoverError("cutover is partial without a matching journal")
        journal = {
            "schema_version": 1,
            "migration_plan_sha256": plan_hash,
            "base_token": config["base_token"],
            "source_table_id": config["source_table_id"],
            "stage": "started",
            "records_completed": [],
        }
        write_private_json(journal_path, journal)
    target_field_ids = create_missing_fields(client, config)
    journal["stage"] = "fields_created"
    journal["target_field_ids"] = target_field_ids
    write_private_json(journal_path, journal)
    update_records(client, config, migration, target_field_ids, journal_path, journal)
    rename_reused_fields(client, config)
    rename_table(client, config)
    ensure_views(client, config)
    final_fields = client.fields(str(config["base_token"]), str(config["source_table_id"]))
    target_field_ids = assert_field_state(config, final_fields)
    verify_final_records(client, config, migration, target_field_ids)
    journal["stage"] = "complete"
    journal["records_completed"] = sorted(
        str(item["source_record_id"]) for item in migration["records"]
    )
    write_private_json(journal_path, journal)
    return {
        "status": "applied_verified",
        "record_count": len(migration["records"]),
        "business_field_count": 20,
        "attachment_field": config["attachment_field"]["name"],
        "view_count": len(config["views"]),
        "migration_plan_sha256": plan_hash,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply the reviewed unified Base cutover")
    parser.add_argument("--config", required=True)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--migration-snapshot", required=True)
    for label in EXPORT_LABELS:
        parser.add_argument(f"--{label}-export", required=True)
    parser.add_argument("--backup-dir", required=True)
    parser.add_argument("--journal", required=True)
    parser.add_argument("--maintenance-proof")
    parser.add_argument("--cli", default="lark-cli")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    config_path = Path(args.config)
    schema_path = Path(args.schema)
    migration_path = Path(args.migration_snapshot)
    export_paths = {label: Path(getattr(args, f"{label}_export")) for label in EXPORT_LABELS}
    config = validate_config(load_json_object(config_path, "cutover config"))
    business_names = validate_schema(load_json_object(schema_path, "unified Base schema"), config)
    migration = validate_migration(
        load_json_object(migration_path, "migration snapshot"),
        business_names,
        int(config["snapshot_tables"]["source"]["record_count"]),
    )
    exports = {label: read_export(path, label) for label, path in export_paths.items()}
    client = LarkBaseClient(args.cli)
    if args.apply:
        if not args.maintenance_proof:
            raise CutoverError("--maintenance-proof is required with --apply")
        proof = load_json_object(Path(args.maintenance_proof), "maintenance proof")
        validate_maintenance_proof(proof, config, str(migration["plan_sha256"]))
        state = preflight(client, config, migration, exports, maintenance=True)
        save_backup(
            Path(args.backup_dir), config_path, schema_path, migration_path,
            export_paths, state, config, exports,
        )
        result = apply_cutover(
            client, config, migration, state, journal_path=Path(args.journal)
        )
    else:
        state = preflight(client, config, migration, exports, maintenance=False)
        result = {
            "status": "dry_run_ready",
            "record_count": len(migration["records"]),
            "business_field_count": len(business_names),
            "new_field_create_count": len(state["missing_new_fields"]),
            "new_view_create_count": len(state["missing_views"]),
            "reused_field_count": len(config["reused_fields"]),
            "migration_plan_sha256": migration["plan_sha256"],
            "external_writes": 0,
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CutoverError as exc:
        print(str(exc), file=os.sys.stderr)
        raise SystemExit(2) from None
