from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "apply_version_workflow_gate.py"
SPEC = importlib.util.spec_from_file_location("version_workflow_gate", SCRIPT)
workflow_gate = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules["version_workflow_gate"] = workflow_gate
SPEC.loader.exec_module(workflow_gate)


class WorkflowGateTests(unittest.TestCase):
    def sample(self):
        return {
            "title": "正式JSON自动生成",
            "status": "enabled",
            "steps": [
                {
                    "id": "trigger",
                    "type": "ChangeRecordTrigger",
                    "data": {
                        "table_name": "结构化数据库",
                        "condition_list": [
                            {
                                "conjunction": "and",
                                "conditions": [
                                    {"field_name": "已审核", "operator": "is", "value": []},
                                    {"field_name": "JSON来源MD字段hash", "operator": "isEmpty", "value": []},
                                ],
                            },
                            {
                                "conjunction": "and",
                                "conditions": [
                                    {"field_name": "已审核", "operator": "is", "value": []},
                                    {"field_name": "需要重新生成JSON", "operator": "is", "value": []},
                                ],
                            },
                        ],
                    },
                },
                {"id": "action", "type": "HTTPClientAction", "data": {"headers": [{"key": "secret"}]}},
            ],
        }

    def test_adds_gate_to_both_or_branches_and_preserves_actions(self):
        workflow = self.sample()
        action_before = workflow["steps"][1]
        updated, changed = workflow_gate.add_version_gate(workflow)

        self.assertTrue(changed)
        self.assertIs(updated["steps"][1], action_before)
        expected = {"归档状态", "版本留存状态", "归档链接"}
        for group in updated["steps"][0]["data"]["condition_list"]:
            names = {item["field_name"] for item in group["conditions"]}
            self.assertTrue(expected.issubset(names))

    def test_second_application_is_idempotent(self):
        workflow, _changed = workflow_gate.add_version_gate(self.sample())
        for group in workflow["steps"][0]["data"]["condition_list"]:
            for condition in group["conditions"]:
                if condition["field_name"] in {"归档状态", "版本留存状态"}:
                    condition["value"][0]["value"]["id"] = "opt_existing"
        _updated, changed_again = workflow_gate.add_version_gate(workflow)
        self.assertFalse(changed_again)

    def test_rejects_existing_gate_with_wrong_option(self):
        workflow, _changed = workflow_gate.add_version_gate(self.sample())
        condition = workflow["steps"][0]["data"]["condition_list"][0]["conditions"][-3]
        condition["value"][0]["value"]["name"] = "归档中"
        with self.assertRaisesRegex(ValueError, "unexpected value"):
            workflow_gate.add_version_gate(workflow)

    def test_run_lark_forwards_private_tmp_working_directory(self):
        completed = mock.Mock(returncode=0, stdout="{}", stderr="")
        with mock.patch.object(workflow_gate.subprocess, "run", return_value=completed) as run:
            output = workflow_gate.run_lark(
                ["lark-cli", "base", "+workflow-update", "--json", "@payload.json"],
                cwd="/private/tmp",
            )

        self.assertEqual(output, "{}")
        self.assertEqual(run.call_args.kwargs["cwd"], "/private/tmp")


if __name__ == "__main__":
    unittest.main()
