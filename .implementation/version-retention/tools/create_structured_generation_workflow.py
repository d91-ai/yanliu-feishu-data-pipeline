#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import uuid
from typing import Any


WORKFLOW_TITLE = "审核后结构化生成"
TRIGGER_ID = "trigStructuredSemantic"
ACTION_ID = "actStructuredSemantic"


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def parse_cli_json(output: str) -> dict[str, Any]:
    start = output.find("{")
    if start < 0:
        raise ValueError("lark-cli response did not contain JSON")
    payload, _end = json.JSONDecoder().raw_decode(output[start:])
    if not isinstance(payload, dict):
        raise ValueError("lark-cli response was not a JSON object")
    return payload


def run_lark(args: list[str], *, cwd: str | None = None) -> dict[str, Any]:
    result = subprocess.run(args, check=False, capture_output=True, text=True, cwd=cwd)
    if result.returncode != 0:
        raise RuntimeError(f"lark-cli failed with exit code {result.returncode}")
    return parse_cli_json(result.stdout)


def build_workflow(field_map: dict[str, dict[str, Any]], http_token: str) -> dict[str, Any]:
    # The workflow API resolves select values by name. This is the same form used
    # by the existing version-retention gate updater and avoids persisting mutable
    # option IDs in this deployment helper.
    archived = {"name": "已归档"}
    retained = {"name": "已完成"}
    conditions = [
        {
            "field_name": "审核状态",
            "operator": "is",
            "value": [{"value_type": "boolean", "value": True}],
        },
        {
            "field_name": "归档状态",
            "operator": "is",
            "value": [{"value_type": "option", "value": archived}],
        },
        {
            "field_name": "版本留存状态",
            "operator": "is",
            "value": [{"value_type": "option", "value": retained}],
        },
        {"field_name": "归档链接", "operator": "isNotEmpty", "value": []},
        {"field_name": "表格生成状态", "operator": "isEmpty", "value": []},
    ]
    return {
        "client_token": "structured-semantic-" + uuid.uuid4().hex,
        "title": WORKFLOW_TITLE,
        "steps": [
            {
                "id": TRIGGER_ID,
                "type": "ChangeRecordTrigger",
                "title": "已审核会议纪要完成归档和版本留存时触发",
                "next": ACTION_ID,
                "children": {"links": []},
                "data": {
                    "table_name": "非结构化数据库",
                    "trigger_control_list": [
                        "pasteUpdate",
                        "automationBatchUpdate",
                        "appendImport",
                        "openAPIBatchUpdate",
                    ],
                    "condition_list": [{"conjunction": "and", "conditions": conditions}],
                },
            },
            {
                "id": ACTION_ID,
                "type": "HTTPClientAction",
                "title": "排队生成待审核结构化 Markdown",
                "children": {"links": []},
                "data": {
                    "method": "POST",
                    "url": [
                        {
                            "value_type": "text",
                            "value": "https://dify.example.invalid/feishu-structured/generate",
                        }
                    ],
                    "headers": [
                        {
                            "key": "Content-Type",
                            "value": [{"value_type": "text", "value": "application/json"}],
                        },
                        {
                            "key": "X-Structured-Token",
                            "value": [{"value_type": "text", "value": http_token}],
                        },
                    ],
                    "body_type": "raw",
                    "raw_body": [
                        {"value_type": "text", "value": '{"record_id":"'},
                        {"value_type": "ref", "value": f"$.{TRIGGER_ID}.recordId"},
                        {"value_type": "text", "value": '"}'},
                    ],
                    "response_type": "json",
                    "response_value": (
                        '{"ok":true,"status":"queued","record_id":"recxxx",'
                        '"job_id":"recxxx-attempt-001"}'
                    ),
                },
            },
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Create the post-review structured generation workflow")
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    env = parse_env(args.env_file)
    base_token = env.get("FEISHU_SOURCE_BITABLE_APP_TOKEN", "")
    table_id = env.get("FEISHU_SOURCE_TABLE_ID", "")
    http_token = env.get("FEISHU_STRUCTURED_HTTP_TOKEN", "")
    if not base_token or not table_id or not http_token:
        raise ValueError("required Base/table/HTTP token configuration is missing")

    common = ["--as", "user", "--format", "json"]
    existing = run_lark(
        ["lark-cli", "base", "+workflow-list", "--base-token", base_token, *common]
    )
    items = ((existing.get("data") or {}).get("items") or [])
    for item in items:
        if item.get("title") == WORKFLOW_TITLE:
            print(
                json.dumps(
                    {
                        "ok": True,
                        "status": "existing",
                        "workflow_id": item.get("workflow_id"),
                        "enabled": item.get("status") == "enabled",
                    },
                    ensure_ascii=False,
                )
            )
            return 0

    fields_envelope = run_lark(
        [
            "lark-cli",
            "base",
            "+field-list",
            "--base-token",
            base_token,
            "--table-id",
            table_id,
            *common,
        ]
    )
    fields = ((fields_envelope.get("data") or {}).get("fields") or [])
    field_map = {str(item.get("name")): item for item in fields}
    for required in ("审核状态", "归档状态", "版本留存状态", "归档链接", "表格生成状态"):
        if required not in field_map:
            raise ValueError(f"required trigger field is missing: {required}")

    workflow = build_workflow(field_map, http_token)
    if not args.apply:
        print(
            json.dumps(
                {
                    "ok": True,
                    "status": "validated",
                    "title": workflow["title"],
                    "table": "非结构化数据库",
                    "condition_fields": [
                        item["field_name"]
                        for item in workflow["steps"][0]["data"]["condition_list"][0]["conditions"]
                    ],
                    "endpoint": workflow["steps"][1]["data"]["url"][0]["value"],
                    "secret_in_output": False,
                },
                ensure_ascii=False,
            )
        )
        return 0

    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix="structured-generation-workflow-",
            suffix=".json",
            dir="/private/tmp",
            delete=False,
        ) as handle:
            temp_path = handle.name
            os.chmod(temp_path, 0o600)
            json.dump(workflow, handle, ensure_ascii=False, separators=(",", ":"))
        created = run_lark(
            [
                "lark-cli",
                "base",
                "+workflow-create",
                "--base-token",
                base_token,
                "--json",
                "@" + Path(temp_path).name,
                *common,
            ],
            cwd=str(Path(temp_path).parent),
        )
    finally:
        if temp_path:
            Path(temp_path).unlink(missing_ok=True)

    workflow_id = str((created.get("data") or {}).get("workflow_id") or "")
    if not workflow_id:
        raise ValueError("workflow creation returned no workflow_id")
    run_lark(
        [
            "lark-cli",
            "base",
            "+workflow-enable",
            "--base-token",
            base_token,
            "--workflow-id",
            workflow_id,
            *common,
        ]
    )
    print(
        json.dumps(
            {
                "ok": True,
                "status": "created_enabled",
                "workflow_id": workflow_id,
                "title": WORKFLOW_TITLE,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
