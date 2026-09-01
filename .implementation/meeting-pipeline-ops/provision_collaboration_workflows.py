#!/usr/bin/env python3
"""Plan or create the five notification-only Base workflows.

The command is read-only unless ``--apply`` is present. It never prints the
reviewer open_id or any application secret.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import tempfile
from typing import Any, Mapping


WORKFLOW_TITLES = (
    "研流-上传登记提醒",
    "研流-源纪要待审核",
    "研流-行业市场观点待审核",
    "研流-标的观点待审核",
    "研流-正式结果完成",
)

REQUIRED_FIELDS = {
    "会议日期", "会议系列", "会议类型", "会议纪要上传附件", "会议纪要MD",
    "源纪要审核", "行业与市场观点MD", "行业与市场观点审核", "标的观点MD",
    "标的观点审核", "行业与市场观点JSON", "标的观点JSON",
}


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


def not_empty(field_name: str) -> dict[str, Any]:
    return {"field_name": field_name, "operator": "isNotEmpty", "value": []}


def option_condition(field_name: str) -> dict[str, Any]:
    return {
        "field_name": field_name,
        "operator": "containsAny",
        "value": [
            {"value_type": "option", "value": {"name": "未审核"}},
            {"value_type": "option", "value": {"name": "需重审"}},
        ],
    }


def notification(
    action_id: str,
    title: str,
    content: str,
    trigger_id: str,
    reviewer_open_id: str,
    reviewer_name: str,
) -> dict[str, Any]:
    return {
        "id": action_id,
        "type": "LarkMessageAction",
        "title": title,
        "next": None,
        "children": {"links": []},
        "data": {
            "receiver": [{"value_type": "user", "value": {
                "id": reviewer_open_id, "name": reviewer_name,
            }}],
            "send_to_everyone": False,
            "title": [{"value_type": "text", "value": title}],
            "content": [{"value_type": "text", "value": content}],
            "btn_list": [{
                "text": "打开记录",
                "btn_action": "openLink",
                "link": [{"value_type": "ref", "value": f"$.{trigger_id}.recordLink"}],
            }],
        },
    }


def workflow(
    *, token: str, title: str, trigger_id: str, trigger_type: str,
    trigger_title: str, conditions: list[dict[str, Any]], action_id: str,
    message: str, table_name: str, reviewer_open_id: str, reviewer_name: str,
    condition_groups: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "client_token": token,
        "title": title,
        "steps": [
            {
                "id": trigger_id,
                "type": trigger_type,
                "title": trigger_title,
                "next": action_id,
                "children": {"links": []},
                "data": {
                    "table_name": table_name,
                    "trigger_control_list": [
                        "pasteUpdate", "automationBatchUpdate", "appendImport",
                        "openAPIBatchUpdate",
                    ],
                    "condition_list": condition_groups or [
                        {"conjunction": "and", "conditions": conditions}
                    ],
                },
            },
            notification(action_id, title, message, trigger_id,
                         reviewer_open_id, reviewer_name),
        ],
    }


def build_workflows(
    table_name: str, reviewer_open_id: str, reviewer_name: str
) -> list[dict[str, Any]]:
    return [
        workflow(
            token="yanliu-public-upload-registered-v1", title=WORKFLOW_TITLES[0],
            trigger_id="trigger_upload", trigger_type="AddRecordTrigger",
            trigger_title="新增含纪要附件的记录",
            conditions=[not_empty("会议纪要上传附件"), not_empty("会议日期"),
                        not_empty("会议系列"), not_empty("会议类型")],
            action_id="notify_upload", message="新的会议纪要已登记，系统将开始自动处理。",
            table_name=table_name, reviewer_open_id=reviewer_open_id,
            reviewer_name=reviewer_name,
        ),
        workflow(
            token="yanliu-public-source-review-v1", title=WORKFLOW_TITLES[1],
            trigger_id="trigger_source", trigger_type="ChangeRecordTrigger",
            trigger_title="源纪要生成且待审核",
            conditions=[not_empty("会议纪要MD"), option_condition("源纪要审核")],
            action_id="notify_source",
            message="会议纪要 Markdown 已生成，请人工校对后确认审核状态。",
            table_name=table_name, reviewer_open_id=reviewer_open_id,
            reviewer_name=reviewer_name,
        ),
        workflow(
            token="yanliu-public-industry-review-v1", title=WORKFLOW_TITLES[2],
            trigger_id="trigger_industry", trigger_type="ChangeRecordTrigger",
            trigger_title="行业与市场观点生成且待审核",
            conditions=[not_empty("行业与市场观点MD"),
                        option_condition("行业与市场观点审核")],
            action_id="notify_industry",
            message="行业与市场观点 Markdown 已生成，请人工校对后确认审核状态。",
            table_name=table_name, reviewer_open_id=reviewer_open_id,
            reviewer_name=reviewer_name,
        ),
        workflow(
            token="yanliu-public-target-review-v1", title=WORKFLOW_TITLES[3],
            trigger_id="trigger_target", trigger_type="ChangeRecordTrigger",
            trigger_title="标的观点生成且待审核",
            conditions=[not_empty("标的观点MD"), option_condition("标的观点审核")],
            action_id="notify_target",
            message="标的观点 Markdown 已生成，请人工校对后确认审核状态。",
            table_name=table_name, reviewer_open_id=reviewer_open_id,
            reviewer_name=reviewer_name,
        ),
        workflow(
            token="yanliu-public-result-ready-v1", title=WORKFLOW_TITLES[4],
            trigger_id="trigger_result", trigger_type="ChangeRecordTrigger",
            trigger_title="任一路正式 JSON 生成", conditions=[],
            condition_groups=[
                {"conjunction": "and", "conditions": [not_empty("行业与市场观点JSON")]},
                {"conjunction": "and", "conditions": [not_empty("标的观点JSON")]},
            ],
            action_id="notify_result", message="至少一路审核后 JSON 已生成，可打开记录查看结果。",
            table_name=table_name, reviewer_open_id=reviewer_open_id,
            reviewer_name=reviewer_name,
        ),
    ]


def field_names(payload: Mapping[str, Any]) -> set[str]:
    data = payload.get("data")
    items = data.get("fields") if isinstance(data, Mapping) else None
    if not isinstance(items, list):
        return set()
    return {str(item.get("name") or "") for item in items if isinstance(item, Mapping)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Provision five notification-only workflows")
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--table-name", default="会议数据库")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    env = parse_env(args.env_file)
    base_token = env.get("FEISHU_SOURCE_BITABLE_APP_TOKEN") or env.get(
        "FEISHU_BITABLE_APP_TOKEN", ""
    )
    table_id = env.get("FEISHU_SOURCE_TABLE_ID") or env.get(
        "FEISHU_BITABLE_TABLE_ID", ""
    )
    reviewer_open_id = env.get("FEISHU_WORKFLOW_REVIEWER_OPEN_ID", "")
    reviewer_name = env.get("FEISHU_WORKFLOW_REVIEWER_NAME", "审核人")
    if not base_token or not table_id or not reviewer_open_id:
        raise ValueError("Base token, table ID, or reviewer open_id is missing")

    planned = build_workflows(args.table_name, reviewer_open_id, reviewer_name)
    if not args.apply:
        print(json.dumps({
            "ok": True, "status": "planned", "workflow_count": len(planned),
            "titles": [item["title"] for item in planned],
            "reviewer_configured": True, "external_writes": False,
        }, ensure_ascii=False))
        return 0

    common = ["--as", "user", "--format", "json"]
    fields = run_lark([
        "lark-cli", "base", "+field-list", "--base-token", base_token,
        "--table-id", table_id, *common,
    ])
    missing = sorted(REQUIRED_FIELDS - field_names(fields))
    if missing:
        raise ValueError("required Workflow fields are missing: " + ", ".join(missing))

    existing_payload = run_lark([
        "lark-cli", "base", "+workflow-list", "--base-token", base_token, *common,
    ])
    existing_items = ((existing_payload.get("data") or {}).get("items") or [])
    existing_by_title: dict[str, list[Mapping[str, Any]]] = {}
    for item in existing_items:
        if isinstance(item, Mapping):
            existing_by_title.setdefault(str(item.get("title") or ""), []).append(item)

    results: list[dict[str, str]] = []
    for item in planned:
        title = str(item["title"])
        matches = existing_by_title.get(title, [])
        if len(matches) > 1:
            raise ValueError(f"duplicate existing Workflow title: {title}")
        if matches:
            workflow_id = str(matches[0].get("workflow_id") or "")
            if not workflow_id:
                raise ValueError(f"existing Workflow has no ID: {title}")
            if str(matches[0].get("status") or "") != "enabled":
                run_lark([
                    "lark-cli", "base", "+workflow-enable", "--base-token", base_token,
                    "--workflow-id", workflow_id, *common,
                ])
                results.append({"title": title, "status": "enabled_existing"})
            else:
                results.append({"title": title, "status": "already_enabled"})
            continue

        with tempfile.TemporaryDirectory(prefix="yanliu-workflow-") as temp_dir:
            payload_path = Path(temp_dir) / "workflow.json"
            payload_path.write_text(json.dumps(item, ensure_ascii=False,
                                                 separators=(",", ":")), encoding="utf-8")
            created = run_lark([
                "lark-cli", "base", "+workflow-create", "--base-token", base_token,
                "--json", "@workflow.json", *common,
            ], cwd=temp_dir)
        workflow_id = str((created.get("data") or {}).get("workflow_id") or "")
        if not workflow_id:
            raise ValueError(f"Workflow creation returned no ID: {title}")
        run_lark([
            "lark-cli", "base", "+workflow-enable", "--base-token", base_token,
            "--workflow-id", workflow_id, *common,
        ])
        results.append({"title": title, "status": "created_enabled"})

    print(json.dumps({"ok": True, "results": results}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
