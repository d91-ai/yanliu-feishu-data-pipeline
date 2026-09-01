from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
import sys
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


recovery = load("missed_ingress_recovery_tested", ROOT / "reconcile_missed_ingress.py")
workflows = load("collaboration_workflows_tested", ROOT / "provision_collaboration_workflows.py")


class FakeRouter:
    def __init__(self):
        now_ms = int(time.time() * 1000)
        self.records = [
            {"record_id": "candidate", "last_modified_time": now_ms,
             "fields": {"会议ID": "", "附件": [{"file_token": "safe-token"}]}},
            {"record_id": "complete", "last_modified_time": now_ms,
             "fields": {"会议ID": "mtg_done", "附件": [{"file_token": "safe-token"}]}},
        ]
        self.processed: list[str] = []

    def list_bitable_records(self, _cfg):
        return list(self.records)

    @staticmethod
    def plain_field_value(value):
        return str(value or "")

    @staticmethod
    def _attachment_items_from_field(value):
        return list(value or [])

    def process_form_attachment_ingress(self, _cfg, record_id):
        self.processed.append(record_id)
        return {"status": "created"}

    @staticmethod
    def safe_error_code(exc):
        return type(exc).__name__


class MissedIngressTests(unittest.TestCase):
    def test_plan_is_read_only(self):
        router = FakeRouter()
        result = recovery.reconcile_once(
            router, SimpleNamespace(form_attachment_field="附件"), 48, apply=False)
        self.assertEqual(result, (1, 0, 0))
        self.assertEqual(router.processed, [])

    def test_apply_recovers_only_missing_meeting_id(self):
        router = FakeRouter()
        result = recovery.reconcile_once(
            router, SimpleNamespace(form_attachment_field="附件"), 48, apply=True)
        self.assertEqual(result, (1, 1, 0))
        self.assertEqual(router.processed, ["candidate"])


class WorkflowTemplateTests(unittest.TestCase):
    def test_builds_five_notification_only_workflows(self):
        items = workflows.build_workflows("会议数据库", "ou_example", "审核人")
        self.assertEqual(len(items), 5)
        self.assertEqual(tuple(item["title"] for item in items), workflows.WORKFLOW_TITLES)
        action_types = {
            step["type"] for item in items for step in item["steps"]
            if step["id"].startswith("notify_")
        }
        self.assertEqual(action_types, {"LarkMessageAction"})
        self.assertNotIn("HTTPClientAction", str(items))


if __name__ == "__main__":
    unittest.main()
