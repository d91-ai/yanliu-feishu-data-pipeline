#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any


REQUIRED_GATE_CONDITIONS = (
    {
        "field_name": "归档状态",
        "operator": "is",
        "value": [{"value_type": "option", "value": {"name": "已归档"}}],
    },
    {
        "field_name": "版本留存状态",
        "operator": "is",
        "value": [{"value_type": "option", "value": {"name": "已完成"}}],
    },
    {
        "field_name": "归档链接",
        "operator": "isNotEmpty",
        "value": [],
    },
)


def parse_cli_json(output: str) -> dict[str, Any]:
    start = output.find("{")
    if start < 0:
        raise ValueError("lark-cli response did not contain JSON")
    payload, _end = json.JSONDecoder().raw_decode(output[start:])
    if not isinstance(payload, dict):
        raise ValueError("lark-cli response was not a JSON object")
    return payload


def condition_identity(condition: dict[str, Any]) -> tuple[str, str]:
    return str(condition.get("field_name") or ""), str(condition.get("operator") or "")


def condition_matches_required(condition: dict[str, Any], required: dict[str, Any]) -> bool:
    if condition_identity(condition) != condition_identity(required):
        return False
    if required.get("operator") == "isNotEmpty":
        return not condition.get("value")
    required_values = required.get("value") or []
    actual_values = condition.get("value") or []
    if len(required_values) != 1 or len(actual_values) != 1:
        return False
    required_value = required_values[0].get("value", {})
    actual_value = actual_values[0].get("value", {})
    required_name = required_value.get("name") if isinstance(required_value, dict) else ""
    actual_name = actual_value.get("name") if isinstance(actual_value, dict) else ""
    return bool(required_name and actual_name == required_name)


def add_version_gate(workflow: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    steps = workflow.get("steps")
    if not isinstance(steps, list):
        raise ValueError("workflow steps are missing")
    triggers = [step for step in steps if isinstance(step, dict) and step.get("type") == "ChangeRecordTrigger"]
    if len(triggers) != 1:
        raise ValueError(f"expected exactly one ChangeRecordTrigger, got {len(triggers)}")
    trigger = triggers[0]
    data = trigger.get("data")
    if not isinstance(data, dict):
        raise ValueError("workflow trigger data are missing")
    groups = data.get("condition_list")
    if not isinstance(groups, list) or not groups:
        raise ValueError("workflow trigger condition_list is empty")

    changed = False
    for group in groups:
        if not isinstance(group, dict) or group.get("conjunction") != "and":
            raise ValueError("every workflow trigger group must use conjunction=and")
        conditions = group.get("conditions")
        if not isinstance(conditions, list):
            raise ValueError("workflow trigger group conditions are missing")
        existing = {
            condition_identity(item): item
            for item in conditions
            if isinstance(item, dict)
        }
        for required in REQUIRED_GATE_CONDITIONS:
            identity = condition_identity(required)
            if identity in existing:
                if not condition_matches_required(existing[identity], required):
                    raise ValueError(f"existing gate condition has unexpected value: {identity[0]}")
            else:
                conditions.append(json.loads(json.dumps(required, ensure_ascii=False)))
                changed = True
    return workflow, changed


def run_lark(args: list[str], *, cwd: str | None = None) -> str:
    result = subprocess.run(args, check=False, capture_output=True, text=True, cwd=cwd)
    if result.returncode != 0:
        raise RuntimeError(f"lark-cli failed with exit code {result.returncode}")
    return result.stdout


def main() -> int:
    parser = argparse.ArgumentParser(description="Add version-retention gates to an existing Base workflow")
    parser.add_argument("--base-token", required=True)
    parser.add_argument("--workflow-id", required=True)
    parser.add_argument("--apply", action="store_true", help="Submit the full workflow update; default is validation only")
    args = parser.parse_args()

    current_output = run_lark(
        [
            "lark-cli",
            "base",
            "+workflow-get",
            "--base-token",
            args.base_token,
            "--workflow-id",
            args.workflow_id,
            "--as",
            "user",
            "--format",
            "json",
        ]
    )
    envelope = parse_cli_json(current_output)
    workflow = envelope.get("data")
    if not isinstance(workflow, dict):
        raise ValueError("workflow data are missing")
    title = str(workflow.get("title") or "")
    updated, changed = add_version_gate(workflow)

    if args.apply and changed:
        body = {
            "title": updated.get("title"),
            "status": updated.get("status"),
            "steps": updated.get("steps"),
        }
        temp_path = ""
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                prefix="version-workflow-",
                suffix=".json",
                dir="/private/tmp",
                delete=False,
            ) as handle:
                temp_path = handle.name
                os.chmod(temp_path, 0o600)
                json.dump(body, handle, ensure_ascii=False, separators=(",", ":"))
            run_lark(
                [
                    "lark-cli",
                    "base",
                    "+workflow-update",
                    "--base-token",
                    args.base_token,
                    "--workflow-id",
                    args.workflow_id,
                    "--as",
                    "user",
                    "--json",
                    "@" + Path(temp_path).name,
                    "--format",
                    "json",
                ],
                cwd=str(Path(temp_path).parent),
            )
        finally:
            if temp_path:
                Path(temp_path).unlink(missing_ok=True)

    print(
        json.dumps(
            {
                "ok": True,
                "workflow_id": args.workflow_id,
                "title": title,
                "changed": changed,
                "applied": bool(args.apply and changed),
                "gate_fields": [item["field_name"] for item in REQUIRED_GATE_CONDITIONS],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
