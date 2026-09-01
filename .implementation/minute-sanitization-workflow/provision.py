#!/usr/bin/env python3
"""Provision and audit the Feishu resources for minute sanitization.

The default mode is a read-only dry run.  ``--apply`` is the only switch that
permits writes.  The script deliberately does not provision or call the
sanitization service itself.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo


SOURCE_TABLE = "非结构化数据库"
TARGET_TABLE = "脱敏数据库"

WORKFLOW_REVIEW = "审核后脱敏MD生成"
WORKFLOW_ARCHIVE = "审核归档工作流 - 脱敏数据库"

WORKFLOW_ENDPOINTS = {
    WORKFLOW_REVIEW: "/generate-review-md",
    WORKFLOW_ARCHIVE: "/archive-review-md",
}

TYPE_ALIASES = {
    1: "text",
    2: "number",
    3: "select",
    5: "datetime",
    7: "checkbox",
    15: "url",
}


class ProvisionError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Config:
    base_token: str
    root_folder_token: str
    service_base_url: str
    workflow_token: str
    source_cutoff: str
    month: str
    identity: str
    apply: bool


def select_field(name: str, options: Sequence[str]) -> dict[str, Any]:
    return {
        "name": name,
        "type": "select",
        "multiple": False,
        "options": [{"name": option} for option in options],
    }


def url_field(name: str) -> dict[str, Any]:
    return {"name": name, "type": "text", "style": {"type": "url"}}


def datetime_field(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "type": "datetime",
        "style": {"format": "yyyy-MM-dd HH:mm"},
    }


def source_field_specs() -> list[dict[str, Any]]:
    return [
        select_field("脱敏生成状态", ["待生成", "生成中", "已生成", "生成失败"]),
        url_field("脱敏MD链接"),
        datetime_field("脱敏生成时间"),
        {"name": "脱敏生成错误", "type": "text"},
    ]


def source_prerequisite_specs() -> list[dict[str, Any]]:
    return [
        {"name": "审核状态", "type": "checkbox"},
        select_field("归档状态", ["已归档"]),
        select_field("版本留存状态", ["已完成"]),
        url_field("归档链接"),
        {"name": "审核后内容SHA256", "type": "text"},
        datetime_field("归档时间"),
    ]


def target_field_specs() -> list[dict[str, Any]]:
    # The first field is the Base primary field.
    return [
        {"name": "脱敏纪要", "type": "text"},
        {"name": "来源记录ID", "type": "text"},
        url_field("来源归档链接"),
        {"name": "来源审核后SHA256", "type": "text"},
        datetime_field("会议日期"),
        {"name": "幂等键", "type": "text"},
        {"name": "脱敏规则版本", "type": "text"},
        select_field("MD生成状态", ["生成中", "已生成", "生成失败"]),
        url_field("脱敏MD链接"),
        datetime_field("MD生成时间"),
        select_field("质量检查状态", ["未检查", "已通过", "未通过"]),
        {"name": "审核状态", "type": "checkbox"},
        select_field("归档状态", ["待归档", "归档中", "已归档", "归档失败"]),
        url_field("归档链接"),
        datetime_field("归档时间"),
        url_field("审核前版本链接"),
        {"name": "审核前文件版本号", "type": "text"},
        {"name": "审核前内容SHA256", "type": "text"},
        {"name": "审核后文件版本号", "type": "text"},
        {"name": "审核后内容SHA256", "type": "text"},
        select_field("版本差异", ["未比较", "无修改", "有修改", "比较失败"]),
        select_field("版本留存状态", ["待留存", "基线已留存", "已完成", "留存失败"]),
        {"name": "版本留存错误", "type": "text"},
        {"name": "错误阶段", "type": "text"},
        {"name": "错误信息", "type": "text"},
    ]


def drive_paths(month: str) -> list[tuple[str, ...]]:
    return [
        ("脱敏会议纪要（待审核）", month),
        ("脱敏会议纪要（已审核）", month),
        ("审核版本留存", "脱敏会议纪要", month, "审核前"),
    ]


def option(name: str) -> list[dict[str, Any]]:
    return [{"value_type": "option", "value": {"name": name}}]


def boolean(value: bool) -> list[dict[str, Any]]:
    return [{"value_type": "boolean", "value": value}]


def date(value: str) -> list[dict[str, Any]]:
    return [{"value_type": "date", "value": value}]


def workflow_cutoff_date(source_cutoff: str) -> str:
    """Return a date-only guard that survives Feishu Workflow normalization.

    Workflow branch conditions discard the time component and normalize dates
    to ``YYYY/MM/DD``.  Using the previous calendar day with ``isGreater``
    admits records from the provisioning day onward; the service then applies
    the exact minute-level cutoff before any source-file download or artifact
    write.
    """

    cutoff = datetime.strptime(source_cutoff, "%Y-%m-%d %H:%M")
    return (cutoff - timedelta(days=1)).strftime("%Y/%m/%d")


def condition(field: str, operator: str, value: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"field_name": field, "operator": operator, "value": value or []}


def workflow_groups(source_cutoff: str) -> dict[str, list[dict[str, Any]]]:
    review_common = [
        condition("审核状态", "is", boolean(True)),
        condition("归档状态", "is", option("已归档")),
        condition("版本留存状态", "is", option("已完成")),
        condition("归档链接", "isNotEmpty"),
        condition("审核后内容SHA256", "isNotEmpty"),
        # Feishu rejects IsGreaterEqual in if/else branches and drops the time
        # component from date values.  Keep a coarse previous-day Workflow
        # guard, then enforce the exact minute cutoff again in the service.
        condition("归档时间", "isGreater", date(workflow_cutoff_date(source_cutoff))),
    ]
    return {
        WORKFLOW_REVIEW: [
            {
                "conjunction": "and",
                "conditions": [*review_common, condition("脱敏生成状态", "isEmpty")],
            },
            {
                "conjunction": "and",
                "conditions": [
                    *review_common,
                    condition("脱敏生成状态", "is", option("待生成")),
                ],
            },
        ],
        WORKFLOW_ARCHIVE: [
            {
                "conjunction": "and",
                "conditions": [
                    condition("MD生成状态", "is", option("已生成")),
                    condition("审核状态", "is", boolean(True)),
                    condition("归档状态", "is", option("待归档")),
                    condition("归档链接", "isEmpty"),
                    condition("脱敏MD链接", "isNotEmpty"),
                    condition("版本留存状态", "is", option("基线已留存")),
                    condition("审核前版本链接", "isNotEmpty"),
                    condition("审核前内容SHA256", "isNotEmpty"),
                ],
            }
        ],
    }


def validate_service_base_url(value: str) -> str:
    normalized = value.rstrip("/")
    parsed = urlsplit(normalized)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ProvisionError("invalid_service_url", "service base URL must be an HTTPS origin without credentials")
    if parsed.query or parsed.fragment:
        raise ProvisionError("invalid_service_url", "service base URL must not contain query or fragment data")
    return normalized


def build_workflows(service_base_url: str, workflow_token: str, source_cutoff: str) -> dict[str, dict[str, Any]]:
    origin = validate_service_base_url(service_base_url)
    if not workflow_token:
        raise ProvisionError("missing_workflow_token", "workflow bearer token is required")
    groups = workflow_groups(source_cutoff)
    table_by_title = {
        WORKFLOW_REVIEW: SOURCE_TABLE,
        WORKFLOW_ARCHIVE: TARGET_TABLE,
    }
    bodies: dict[str, dict[str, Any]] = {}
    for index, title in enumerate((WORKFLOW_REVIEW, WORKFLOW_ARCHIVE), start=1):
        trigger_id = f"trigger_minute_sanitize_{index}"
        action_id = f"action_minute_sanitize_{index}"
        client_key = hashlib.sha256(f"minute-sanitize\0{title}\0{source_cutoff}".encode()).hexdigest()[:24]
        bodies[title] = {
            "client_token": f"minute-sanitize-{client_key}",
            "title": title,
            "steps": [
                {
                    "id": trigger_id,
                    "type": "ChangeRecordTrigger",
                    "title": f"{table_by_title[title]}记录满足门禁时触发",
                    "next": action_id,
                    "data": {
                        "table_name": table_by_title[title],
                        "trigger_control_list": [
                            "pasteUpdate",
                            "automationBatchUpdate",
                            "syncUpdate",
                            "appendImport",
                            "openAPIBatchUpdate",
                        ],
                        "condition_list": groups[title],
                    },
                },
                {
                    "id": action_id,
                    "type": "HTTPClientAction",
                    "title": "调用独立脱敏编排服务",
                    "next": None,
                    "data": {
                        "method": "POST",
                        "url": [{"value_type": "text", "value": origin + WORKFLOW_ENDPOINTS[title]}],
                        "headers": [
                            {
                                "key": "Content-Type",
                                "value": [{"value_type": "text", "value": "application/json"}],
                            },
                            {
                                "key": "Authorization",
                                "value": [{"value_type": "text", "value": f"Bearer {workflow_token}"}],
                            },
                        ],
                        "body_type": "raw",
                        "raw_body": [
                            {"value_type": "text", "value": '{"record_id":"'},
                            {"value_type": "ref", "value": f"$.{trigger_id}.recordId"},
                            {"value_type": "text", "value": '"}'},
                        ],
                        "response_type": "json",
                        "response_value": '{"ok":true,"status":"accepted","record_id":"recxxx"}',
                    },
                },
            ],
        }
    return bodies


def parse_cli_json(output: str) -> dict[str, Any]:
    start = output.find("{")
    if start < 0:
        raise ProvisionError("invalid_cli_response", "lark-cli response did not contain JSON")
    try:
        payload, _end = json.JSONDecoder().raw_decode(output[start:])
    except json.JSONDecodeError as exc:
        raise ProvisionError("invalid_cli_response", "lark-cli returned malformed JSON") from exc
    if not isinstance(payload, dict):
        raise ProvisionError("invalid_cli_response", "lark-cli response was not a JSON object")
    return payload


TOKEN_PATTERN = re.compile(
    r"\b(?:bascn|fld|wkf|tbl|rec|dox|wik|sht|cli_)[A-Za-z0-9_-]{8,}\b",
    re.IGNORECASE,
)
URL_PATTERN = re.compile(r"https?://[^\s\"']+", re.IGNORECASE)
BEARER_PATTERN = re.compile(r"(?i)Bearer\s+[A-Za-z0-9._~+/-]+")


def redact_text(value: str, secrets: Iterable[str] = ()) -> str:
    result = value
    for secret in sorted({item for item in secrets if item}, key=len, reverse=True):
        result = result.replace(secret, "[REDACTED]")
    result = BEARER_PATTERN.sub("Bearer [REDACTED]", result)

    def redact_url(match: re.Match[str]) -> str:
        try:
            parsed = urlsplit(match.group(0))
            return f"{parsed.scheme}://{parsed.netloc}/[REDACTED]"
        except ValueError:
            return "[REDACTED_URL]"

    result = URL_PATTERN.sub(redact_url, result)
    return TOKEN_PATTERN.sub("[REDACTED_TOKEN]", result)


class LarkClient:
    def __init__(self, identity: str, secrets: Sequence[str]):
        self.identity = identity
        self.secrets = tuple(item for item in secrets if item)

    def run(self, args: Sequence[str], *, cwd: str | None = None) -> dict[str, Any]:
        env = dict(os.environ)
        env["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] = "1"
        env["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] = "1"
        completed = subprocess.run(
            ["lark-cli", *args, "--as", self.identity, "--format", "json"],
            check=False,
            capture_output=True,
            text=True,
            cwd=cwd,
            env=env,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "no diagnostic output"
            raise ProvisionError(
                "lark_cli_failed",
                f"lark-cli failed with exit code {completed.returncode}: "
                + redact_text(detail, self.secrets),
            )
        return parse_cli_json(completed.stdout)


def envelope_data(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    data = payload.get("data")
    return data if isinstance(data, Mapping) else payload


def extract_items(payload: Mapping[str, Any], keys: Sequence[str]) -> list[dict[str, Any]]:
    data = envelope_data(payload)
    for key in keys:
        items = data.get(key)
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
    if isinstance(data.get("item"), Mapping):
        return [dict(data["item"])]
    return []


def list_tables(client: LarkClient, base_token: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    offset = 0
    while True:
        payload = client.run(
            ["base", "+table-list", "--base-token", base_token, "--limit", "100", "--offset", str(offset)]
        )
        items = extract_items(payload, ("tables", "items"))
        added = 0
        for item in items:
            identity = str(item.get("table_id") or item.get("id") or item.get("name") or "")
            if identity and identity not in seen:
                seen.add(identity)
                results.append(item)
                added += 1
        data = envelope_data(payload)
        if not data.get("has_more") and len(items) < 100:
            return results
        if not items or added == 0:
            raise ProvisionError("pagination_stalled", "Base table pagination made no progress")
        offset += len(items)


def list_fields(client: LarkClient, base_token: str, table_id: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    offset = 0
    while True:
        payload = client.run(
            [
                "base",
                "+field-list",
                "--base-token",
                base_token,
                "--table-id",
                table_id,
                "--limit",
                "200",
                "--offset",
                str(offset),
            ]
        )
        items = extract_items(payload, ("fields", "items"))
        added = 0
        for item in items:
            identity = str(item.get("field_id") or item.get("id") or field_name(item))
            if identity and identity not in seen:
                seen.add(identity)
                results.append(item)
                added += 1
        data = envelope_data(payload)
        if not data.get("has_more") and len(items) < 200:
            return results
        if not items or added == 0:
            raise ProvisionError("pagination_stalled", "Base field pagination made no progress")
        offset += len(items)


def list_workflows(client: LarkClient, base_token: str) -> list[dict[str, Any]]:
    payload = client.run(["base", "+workflow-list", "--base-token", base_token, "--page-size", "100"])
    return extract_items(payload, ("items", "workflows"))


def list_drive_children(client: LarkClient, folder_token: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    seen_pages: set[str] = set()
    page_token = ""
    while True:
        page_key = page_token or "first"
        if page_key in seen_pages:
            raise ProvisionError("pagination_stalled", "Drive folder pagination repeated a page token")
        seen_pages.add(page_key)
        params: dict[str, Any] = {"folder_token": folder_token, "page_size": 200}
        if page_token:
            params["page_token"] = page_token
        payload = client.run(["drive", "files", "list", "--params", json.dumps(params, ensure_ascii=False)])
        data = envelope_data(payload)
        files = data.get("files")
        if not isinstance(files, list):
            files = []
        results.extend(item for item in files if isinstance(item, dict))
        if data.get("has_more") is not True:
            return results
        next_token = str(data.get("next_page_token") or data.get("page_token") or "")
        if not next_token:
            raise ProvisionError("pagination_stalled", "Drive folder has_more=true without a page token")
        page_token = next_token


def table_name(item: Mapping[str, Any]) -> str:
    return str(item.get("name") or item.get("table_name") or "")


def table_id(item: Mapping[str, Any]) -> str:
    return str(item.get("table_id") or item.get("id") or "")


def exact_named_item(items: Sequence[dict[str, Any]], name: str, *, kind: str) -> dict[str, Any] | None:
    matches = [item for item in items if str(item.get("name") or item.get("title") or "") == name]
    if len(matches) > 1:
        raise ProvisionError("duplicate_resource", f"multiple {kind} resources have the exact name: {name}")
    return matches[0] if matches else None


def exact_table(items: Sequence[dict[str, Any]], name: str) -> dict[str, Any] | None:
    matches = [item for item in items if table_name(item) == name]
    if len(matches) > 1:
        raise ProvisionError("duplicate_resource", f"multiple Base tables have the exact name: {name}")
    return matches[0] if matches else None


def field_name(item: Mapping[str, Any]) -> str:
    return str(item.get("name") or item.get("field_name") or "")


def normalized_field_type(item: Mapping[str, Any]) -> str:
    raw = item.get("type")
    if isinstance(raw, int):
        return TYPE_ALIASES.get(raw, f"numeric:{raw}")
    if isinstance(raw, str):
        stripped = raw.strip().lower()
        if stripped.isdigit():
            return TYPE_ALIASES.get(int(stripped), f"numeric:{stripped}")
        aliases = {"single_select": "select", "date": "datetime", "checkbox": "checkbox"}
        return aliases.get(stripped, stripped)
    return "unknown"


def expected_types(spec: Mapping[str, Any]) -> set[str]:
    if spec.get("type") == "text" and (spec.get("style") or {}).get("type") == "url":
        return {"text", "url"}
    return {str(spec.get("type") or "")}


def field_option_names(item: Mapping[str, Any]) -> set[str]:
    direct = item.get("options")
    property_value = item.get("property")
    nested = property_value.get("options") if isinstance(property_value, Mapping) else None
    options = direct if isinstance(direct, list) else nested if isinstance(nested, list) else []
    return {str(option.get("name")) for option in options if isinstance(option, Mapping) and option.get("name")}


def reconcile_fields(
    existing: Sequence[dict[str, Any]],
    specs: Sequence[dict[str, Any]],
    *,
    context: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    by_name: dict[str, list[dict[str, Any]]] = {}
    for item in existing:
        by_name.setdefault(field_name(item), []).append(item)
    missing: list[dict[str, Any]] = []
    errors: list[str] = []
    for spec in specs:
        name = str(spec["name"])
        matches = by_name.get(name, [])
        if not matches:
            missing.append(spec)
            continue
        if len(matches) > 1:
            errors.append(f"{context}.{name}: duplicate fields")
            continue
        item = matches[0]
        actual_type = normalized_field_type(item)
        if actual_type not in expected_types(spec):
            errors.append(f"{context}.{name}: expected {sorted(expected_types(spec))}, got {actual_type}")
            continue
        if spec.get("type") == "select":
            required = {str(option["name"]) for option in spec.get("options", [])}
            missing_options = sorted(required - field_option_names(item))
            if missing_options:
                errors.append(f"{context}.{name}: missing options {', '.join(missing_options)}")
    return missing, errors


def create_field(client: LarkClient, base_token: str, table_id_value: str, spec: Mapping[str, Any]) -> None:
    client.run(
        [
            "base",
            "+field-create",
            "--base-token",
            base_token,
            "--table-id",
            table_id_value,
            "--json",
            json.dumps(dict(spec), ensure_ascii=False, separators=(",", ":")),
        ]
    )


def create_target_table(client: LarkClient, base_token: str) -> None:
    client.run(
        [
            "base",
            "+table-create",
            "--base-token",
            base_token,
            "--name",
            TARGET_TABLE,
            "--fields",
            json.dumps(target_field_specs(), ensure_ascii=False, separators=(",", ":")),
        ]
    )


def find_folder(items: Sequence[dict[str, Any]], name: str) -> str | None:
    named = [item for item in items if str(item.get("name") or "") == name]
    folders = [item for item in named if str(item.get("type") or "").lower() == "folder"]
    non_folders = [item for item in named if str(item.get("type") or "").lower() != "folder"]
    if non_folders:
        raise ProvisionError("folder_name_conflict", f"a non-folder Drive item uses the required name: {name}")
    if len(folders) > 1:
        raise ProvisionError("duplicate_resource", f"multiple Drive folders have the exact name: {name}")
    if not folders:
        return None
    token = str(folders[0].get("token") or "")
    if not token:
        raise ProvisionError("invalid_drive_response", f"Drive folder has no token: {name}")
    return token


def ensure_drive_path(
    client: LarkClient,
    root_token: str,
    parts: Sequence[str],
    *,
    apply: bool,
    actions: list[dict[str, str]],
) -> None:
    current_token: str | None = root_token
    current_path: list[str] = []
    for index, part in enumerate(parts):
        current_path.append(part)
        display_path = "/".join(current_path)
        if current_token is None:
            actions.append({"operation": "create", "resource": "drive_folder", "target": display_path})
            continue
        items = list_drive_children(client, current_token)
        child_token = find_folder(items, part)
        if child_token:
            current_token = child_token
            continue
        actions.append({"operation": "create", "resource": "drive_folder", "target": display_path})
        if not apply:
            current_token = None
            continue
        client.run(
            ["drive", "+create-folder", "--folder-token", current_token, "--name", part]
        )
        # Re-read the parent rather than trusting the write response.
        child_token = find_folder(list_drive_children(client, current_token), part)
        if not child_token:
            raise ProvisionError("write_not_visible", f"created Drive folder was not visible on readback: {display_path}")
        current_token = child_token


def normalize_value(value: Any) -> Any:
    if isinstance(value, list):
        return [normalize_value(item) for item in value]
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key in sorted(value):
            if key == "id" and "name" in value:
                continue
            result[str(key)] = normalize_value(value[key])
        return result
    return value


def canonical_conditions(groups: Any) -> list[str]:
    if not isinstance(groups, list):
        return []
    normalized_groups: list[str] = []
    for group in groups:
        if not isinstance(group, Mapping):
            continue
        conditions = group.get("conditions")
        normalized_conditions = []
        if isinstance(conditions, list):
            for item in conditions:
                if isinstance(item, Mapping):
                    normalized_conditions.append(normalize_value(item))
        normalized_conditions.sort(key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True))
        normalized_groups.append(
            json.dumps(
                {"conjunction": group.get("conjunction"), "conditions": normalized_conditions},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    return sorted(normalized_groups)


def canonical_workflow(workflow: Mapping[str, Any]) -> dict[str, Any]:
    steps = workflow.get("steps")
    if not isinstance(steps, list):
        steps = []
    normalized_steps: list[dict[str, Any]] = []
    for step in steps:
        if not isinstance(step, Mapping):
            continue
        data = step.get("data") if isinstance(step.get("data"), Mapping) else {}
        step_type = step.get("type")
        normalized_data: dict[str, Any]
        if step_type == "ChangeRecordTrigger":
            normalized_data = {
                "table_name": data.get("table_name"),
                "trigger_control_list": sorted(data.get("trigger_control_list") or []),
                "condition_list": canonical_conditions(data.get("condition_list")),
            }
        elif step_type == "HTTPClientAction":
            response_value = data.get("response_value")
            try:
                response_value = json.loads(response_value) if isinstance(response_value, str) else response_value
            except json.JSONDecodeError:
                pass
            normalized_data = {
                "method": data.get("method"),
                "url": normalize_value(data.get("url") or []),
                "headers": sorted(
                    [normalize_value(item) for item in data.get("headers") or [] if isinstance(item, Mapping)],
                    key=lambda item: str(item.get("key") or ""),
                ),
                "body_type": data.get("body_type"),
                "raw_body": normalize_value(data.get("raw_body") or []),
                "response_type": data.get("response_type"),
                "response_value": normalize_value(response_value),
            }
        else:
            normalized_data = normalize_value(data)
        normalized_steps.append(
            {
                "id": step.get("id"),
                "type": step_type,
                "title": step.get("title"),
                "next": step.get("next"),
                "data": normalized_data,
            }
        )
    return {"title": workflow.get("title"), "steps": normalized_steps}


def workflow_id(item: Mapping[str, Any]) -> str:
    return str(item.get("workflow_id") or item.get("id") or "")


def workflow_status(item: Mapping[str, Any]) -> str:
    raw = item.get("status")
    if raw is False or raw == 0:
        return "disabled"
    value = str(raw or "").lower()
    if value in {"disabled", "off", "false", "0"}:
        return "disabled"
    if value in {"enabled", "on", "true", "1"}:
        return "enabled"
    return value or "unknown"


def workflow_from_get(payload: Mapping[str, Any]) -> dict[str, Any]:
    data = envelope_data(payload)
    nested = data.get("workflow")
    return dict(nested) if isinstance(nested, Mapping) else dict(data)


def get_workflow(client: LarkClient, base_token: str, workflow_id_value: str) -> dict[str, Any]:
    payload = client.run(
        ["base", "+workflow-get", "--base-token", base_token, "--workflow-id", workflow_id_value]
    )
    return workflow_from_get(payload)


def write_json_payload(payload: Mapping[str, Any]) -> tuple[str, str]:
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix="minute-sanitize-workflow-",
        suffix=".json",
        dir="/private/tmp",
        delete=False,
    )
    try:
        os.chmod(handle.name, 0o600)
        json.dump(dict(payload), handle, ensure_ascii=False, separators=(",", ":"))
        handle.close()
        return handle.name, os.path.basename(handle.name)
    except Exception:
        handle.close()
        try:
            os.unlink(handle.name)
        except FileNotFoundError:
            pass
        raise


def create_workflow(client: LarkClient, base_token: str, body: Mapping[str, Any]) -> None:
    path, basename = write_json_payload(body)
    try:
        client.run(
            ["base", "+workflow-create", "--base-token", base_token, "--json", "@" + basename],
            cwd=os.path.dirname(path),
        )
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def disable_workflow(client: LarkClient, base_token: str, workflow_id_value: str) -> None:
    client.run(
        ["base", "+workflow-disable", "--base-token", base_token, "--workflow-id", workflow_id_value]
    )


def validate_month(value: str) -> str:
    if not re.fullmatch(r"20\d{2}-(?:0[1-9]|1[0-2])", value):
        raise ProvisionError("invalid_month", "month must use YYYY-MM")
    return value


def validate_cutoff(value: str) -> str:
    try:
        datetime.strptime(value, "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise ProvisionError("invalid_cutoff", "source cutoff must use YYYY-MM-DD HH:MM") from exc
    return value


def resolve_config(args: argparse.Namespace) -> Config:
    base_token = args.base_token or os.environ.get("FEISHU_BASE_TOKEN", "")
    root_folder_token = args.root_folder_token or os.environ.get("FEISHU_KB_ROOT_FOLDER_TOKEN", "")
    service_base_url = args.service_base_url or os.environ.get("FEISHU_SANITIZE_SERVICE_BASE_URL", "")
    workflow_token = args.workflow_token or os.environ.get("FEISHU_SANITIZE_WORKFLOW_TOKEN", "")
    source_cutoff = args.source_cutoff or os.environ.get("FEISHU_SANITIZE_SOURCE_CUTOFF", "")
    missing = [
        name
        for name, value in (
            ("FEISHU_BASE_TOKEN", base_token),
            ("FEISHU_KB_ROOT_FOLDER_TOKEN", root_folder_token),
            ("FEISHU_SANITIZE_SERVICE_BASE_URL", service_base_url),
            ("FEISHU_SANITIZE_WORKFLOW_TOKEN", workflow_token),
            ("FEISHU_SANITIZE_SOURCE_CUTOFF", source_cutoff),
        )
        if not value
    ]
    if missing:
        raise ProvisionError("missing_configuration", "required configuration is missing: " + ", ".join(missing))
    return Config(
        base_token=base_token,
        root_folder_token=root_folder_token,
        service_base_url=validate_service_base_url(service_base_url),
        workflow_token=workflow_token,
        source_cutoff=validate_cutoff(source_cutoff),
        month=validate_month(args.month),
        identity=args.identity,
        apply=args.apply,
    )


def require_no_errors(errors: Sequence[str]) -> None:
    if errors:
        raise ProvisionError("schema_conflict", "; ".join(errors))


def provision(config: Config, client: LarkClient) -> dict[str, Any]:
    actions: list[dict[str, str]] = []
    checks: dict[str, Any] = {}

    tables = list_tables(client, config.base_token)
    source_table = exact_table(tables, SOURCE_TABLE)
    if not source_table or not table_id(source_table):
        raise ProvisionError("source_table_missing", f"required source table is not visible: {SOURCE_TABLE}")
    source_id = table_id(source_table)
    source_fields = list_fields(client, config.base_token, source_id)
    prerequisite_missing, prerequisite_errors = reconcile_fields(
        source_fields, source_prerequisite_specs(), context=SOURCE_TABLE
    )
    if prerequisite_missing:
        prerequisite_errors.extend(
            f"{SOURCE_TABLE}.{spec['name']}: required pre-existing field is missing"
            for spec in prerequisite_missing
        )
    require_no_errors(prerequisite_errors)
    checks["source_prerequisites"] = "verified"

    missing_source, source_errors = reconcile_fields(source_fields, source_field_specs(), context=SOURCE_TABLE)
    require_no_errors(source_errors)
    for spec in missing_source:
        actions.append(
            {"operation": "create", "resource": "base_field", "target": f"{SOURCE_TABLE}.{spec['name']}"}
        )
        if config.apply:
            create_field(client, config.base_token, source_id, spec)
    if config.apply and missing_source:
        source_fields = list_fields(client, config.base_token, source_id)
        remaining, errors = reconcile_fields(source_fields, source_field_specs(), context=SOURCE_TABLE)
        require_no_errors(errors + [f"field not visible after create: {item['name']}" for item in remaining])
    checks["source_fields"] = {
        "required": len(source_field_specs()),
        "missing_before": len(missing_source),
        "verified_after": bool(config.apply or not missing_source),
    }

    tables = list_tables(client, config.base_token) if config.apply and missing_source else tables
    target_table = exact_table(tables, TARGET_TABLE)
    target_id = table_id(target_table) if target_table else ""
    if not target_table:
        actions.append({"operation": "create", "resource": "base_table", "target": TARGET_TABLE})
        if config.apply:
            create_target_table(client, config.base_token)
            tables = list_tables(client, config.base_token)
            target_table = exact_table(tables, TARGET_TABLE)
            target_id = table_id(target_table) if target_table else ""
            if not target_id:
                raise ProvisionError("write_not_visible", f"created Base table was not visible on readback: {TARGET_TABLE}")

    missing_target: list[dict[str, Any]] = []
    if target_id:
        target_fields = list_fields(client, config.base_token, target_id)
        missing_target, target_errors = reconcile_fields(target_fields, target_field_specs(), context=TARGET_TABLE)
        require_no_errors(target_errors)
        for spec in missing_target:
            actions.append(
                {"operation": "create", "resource": "base_field", "target": f"{TARGET_TABLE}.{spec['name']}"}
            )
            if config.apply:
                create_field(client, config.base_token, target_id, spec)
        if config.apply and missing_target:
            refreshed = list_fields(client, config.base_token, target_id)
            remaining, errors = reconcile_fields(refreshed, target_field_specs(), context=TARGET_TABLE)
            require_no_errors(errors + [f"field not visible after create: {item['name']}" for item in remaining])
    checks["target_fields"] = {
        "required": len(target_field_specs()),
        "table_missing_before": target_table is None if not config.apply else any(
            item["resource"] == "base_table" for item in actions
        ),
        "missing_before": len(missing_target),
        "verified_after": bool(config.apply or (target_id and not missing_target)),
    }

    for path in drive_paths(config.month):
        ensure_drive_path(
            client,
            config.root_folder_token,
            path,
            apply=config.apply,
            actions=actions,
        )
    checks["drive_paths"] = {
        "required": ["/".join(path) for path in drive_paths(config.month)],
        "verified_after": bool(config.apply or not any(item["resource"] == "drive_folder" for item in actions)),
    }

    expected_workflows = build_workflows(
        config.service_base_url, config.workflow_token, config.source_cutoff
    )
    workflows = list_workflows(client, config.base_token)
    workflow_checks: dict[str, str] = {}
    for title, expected in expected_workflows.items():
        matches = [item for item in workflows if str(item.get("title") or item.get("name") or "") == title]
        if len(matches) > 1:
            raise ProvisionError("duplicate_resource", f"multiple workflows have the exact title: {title}")
        item = matches[0] if matches else None
        if not item:
            actions.append({"operation": "create_disabled", "resource": "workflow", "target": title})
            if not config.apply:
                workflow_checks[title] = "would_create_disabled"
                continue
            create_workflow(client, config.base_token, expected)
            workflows = list_workflows(client, config.base_token)
            created_matches = [
                candidate
                for candidate in workflows
                if str(candidate.get("title") or candidate.get("name") or "") == title
            ]
            if len(created_matches) != 1:
                raise ProvisionError("write_not_visible", f"created workflow was not uniquely visible on readback: {title}")
            item = created_matches[0]
        item_id = workflow_id(item)
        if not item_id:
            raise ProvisionError("invalid_workflow_response", f"workflow has no ID: {title}")
        actual = get_workflow(client, config.base_token, item_id)
        if canonical_workflow(actual) != canonical_workflow(expected):
            raise ProvisionError("workflow_drift", f"existing workflow definition differs from the approved contract: {title}")
        status = workflow_status(actual) if workflow_status(actual) != "unknown" else workflow_status(item)
        if status != "disabled":
            actions.append({"operation": "disable", "resource": "workflow", "target": title})
            if config.apply:
                disable_workflow(client, config.base_token, item_id)
                actual = get_workflow(client, config.base_token, item_id)
                status = workflow_status(actual)
        if config.apply and status != "disabled":
            raise ProvisionError("workflow_not_disabled", f"workflow is not disabled after readback: {title}")
        workflow_checks[title] = "verified_disabled" if status == "disabled" else "would_disable"
    checks["workflows"] = workflow_checks

    return {
        "ok": True,
        "mode": "apply" if config.apply else "dry-run",
        "writes_executed": config.apply,
        "month": config.month,
        "source_cutoff": config.source_cutoff,
        "actions": actions,
        "checks": checks,
        "scope": {
            "rag_or_dify": False,
            "skill_modified": False,
            "workflows_enabled": False,
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit or provision disabled Feishu resources for the minute-sanitization workflow"
    )
    parser.add_argument("--apply", action="store_true", help="perform idempotent writes; default is read-only")
    parser.add_argument("--identity", choices=("user", "bot"), default="user")
    parser.add_argument("--base-token", help="defaults to FEISHU_BASE_TOKEN")
    parser.add_argument("--root-folder-token", help="defaults to FEISHU_KB_ROOT_FOLDER_TOKEN")
    parser.add_argument("--service-base-url", help="defaults to FEISHU_SANITIZE_SERVICE_BASE_URL")
    parser.add_argument("--workflow-token", help="defaults to FEISHU_SANITIZE_WORKFLOW_TOKEN")
    parser.add_argument("--source-cutoff", help="YYYY-MM-DD HH:MM; defaults to FEISHU_SANITIZE_SOURCE_CUTOFF")
    parser.add_argument(
        "--month",
        default=datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m"),
        help="Drive month folder in YYYY-MM form",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        config = resolve_config(args)
        client = LarkClient(
            config.identity,
            (
                config.base_token,
                config.root_folder_token,
                config.workflow_token,
                config.service_base_url,
            ),
        )
        report = provision(config, client)
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except ProvisionError as exc:
        known_secrets = [
            os.environ.get("FEISHU_BASE_TOKEN", ""),
            os.environ.get("FEISHU_KB_ROOT_FOLDER_TOKEN", ""),
            os.environ.get("FEISHU_SANITIZE_WORKFLOW_TOKEN", ""),
            os.environ.get("FEISHU_SANITIZE_SERVICE_BASE_URL", ""),
        ]
        print(
            json.dumps(
                {
                    "ok": False,
                    "error_code": exc.code,
                    "message": redact_text(str(exc), known_secrets),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
