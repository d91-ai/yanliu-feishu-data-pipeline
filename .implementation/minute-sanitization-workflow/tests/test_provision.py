from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "provision.py"
SPEC = importlib.util.spec_from_file_location("minute_sanitization_provision", SCRIPT)
provision = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules["minute_sanitization_provision"] = provision
SPEC.loader.exec_module(provision)


def materialized_field(spec):
    result = dict(spec)
    if spec.get("type") == "text" and (spec.get("style") or {}).get("type") == "url":
        result["type"] = 15
    else:
        result["type"] = {
            "text": 1,
            "number": 2,
            "select": 3,
            "datetime": 5,
            "checkbox": 7,
        }[spec["type"]]
    result["field_name"] = result.pop("name")
    if spec.get("type") == "select":
        result["property"] = {"options": spec["options"]}
        result.pop("options", None)
    return result


class FieldSchemaTests(unittest.TestCase):
    JSON_FIELDS = {
        "JSON状态",
        "JSON链接",
        "JSON内容SHA256",
        "JSON来源MD内容SHA256",
        "JSON条目数",
        "JSON生成时间",
        "JSON Schema版本",
    }

    def test_source_and_target_schema_contract(self):
        self.assertEqual(
            [item["name"] for item in provision.source_field_specs()],
            ["脱敏生成状态", "脱敏MD链接", "脱敏生成时间", "脱敏生成错误"],
        )
        target = provision.target_field_specs()
        self.assertEqual(target[0], {"name": "脱敏纪要", "type": "text"})
        self.assertEqual(len(target), 25)
        target_names = {item["name"] for item in target}
        self.assertTrue(self.JSON_FIELDS.isdisjoint(target_names))
        self.assertNotIn("RAG状态", target_names)

    def test_reconciliation_is_idempotent_for_materialized_schema(self):
        specs = provision.target_field_specs()
        existing = [materialized_field(item) for item in specs]
        missing, errors = provision.reconcile_fields(existing, specs, context="脱敏数据库")
        self.assertEqual(missing, [])
        self.assertEqual(errors, [])

        missing_again, errors_again = provision.reconcile_fields(existing, specs, context="脱敏数据库")
        self.assertEqual(missing_again, [])
        self.assertEqual(errors_again, [])

    def test_reconciliation_fails_closed_on_select_option_drift(self):
        specs = provision.source_field_specs()
        existing = [materialized_field(item) for item in specs]
        existing[0]["property"]["options"] = [{"name": "生成中"}]
        _missing, errors = provision.reconcile_fields(existing, specs, context="非结构化数据库")
        self.assertTrue(any("missing options" in item for item in errors))


class WorkflowContractTests(unittest.TestCase):
    def setUp(self):
        self.bodies = provision.build_workflows(
            "https://service.example.test/feishu-sanitize",
            "test-secret-value",
            "2032-07-13 12:00",
        )

    def test_review_workflow_has_two_status_branches_and_history_cutoff(self):
        trigger = self.bodies[provision.WORKFLOW_REVIEW]["steps"][0]
        groups = trigger["data"]["condition_list"]
        self.assertEqual(len(groups), 2)
        for group in groups:
            by_name = {item["field_name"]: item for item in group["conditions"]}
            self.assertEqual(by_name["归档时间"]["operator"], "isGreater")
            self.assertEqual(by_name["归档时间"]["value"][0]["value"], "2032/07/12")
        status_conditions = [
            next(item for item in group["conditions"] if item["field_name"] == "脱敏生成状态")
            for group in groups
        ]
        self.assertEqual({item["operator"] for item in status_conditions}, {"isEmpty", "is"})

    def test_all_workflows_only_post_record_id_and_have_bearer_auth(self):
        self.assertEqual(set(self.bodies), {provision.WORKFLOW_REVIEW, provision.WORKFLOW_ARCHIVE})
        for title, body in self.bodies.items():
            self.assertEqual([item["type"] for item in body["steps"]], ["ChangeRecordTrigger", "HTTPClientAction"])
            self.assertIn(
                "openAPIBatchUpdate",
                body["steps"][0]["data"]["trigger_control_list"],
            )
            action = body["steps"][1]
            raw = action["data"]["raw_body"]
            self.assertEqual(raw[0]["value"] + "VALUE" + raw[2]["value"], '{"record_id":"VALUE"}')
            self.assertEqual(action["data"]["url"][0]["value"].split("/feishu-sanitize", 1)[1], provision.WORKFLOW_ENDPOINTS[title])
            auth = next(item for item in action["data"]["headers"] if item["key"] == "Authorization")
            self.assertEqual(auth["value"][0]["value"], "Bearer test-secret-value")
            self.assertNotIn("status", body)

    def test_canonical_workflow_ignores_platform_option_ids(self):
        expected = self.bodies[provision.WORKFLOW_ARCHIVE]
        actual = json.loads(json.dumps(expected, ensure_ascii=False))
        for group in actual["steps"][0]["data"]["condition_list"]:
            for item in group["conditions"]:
                for value in item["value"]:
                    if value.get("value_type") == "option":
                        value["value"]["id"] = "opt_platform_generated"
        self.assertEqual(provision.canonical_workflow(actual), provision.canonical_workflow(expected))

    def test_active_contract_excludes_official_json_stage(self):
        self.assertFalse(hasattr(provision, "WORKFLOW_JSON"))
        self.assertNotIn("脱敏正式JSON生成", self.bodies)
        self.assertNotIn("/generate-official-json", provision.WORKFLOW_ENDPOINTS.values())
        serialized = json.dumps(self.bodies, ensure_ascii=False)
        self.assertNotIn("脱敏正式JSON生成", serialized)
        self.assertNotIn("/generate-official-json", serialized)


class DriveContractTests(unittest.TestCase):
    def test_active_contract_has_only_markdown_and_version_paths(self):
        paths = provision.drive_paths("2032-07")
        self.assertEqual(len(paths), 3)
        self.assertEqual(
            paths,
            [
                ("脱敏会议纪要（待审核）", "2032-07"),
                ("脱敏会议纪要（已审核）", "2032-07"),
                ("审核版本留存", "脱敏会议纪要", "2032-07", "审核前"),
            ],
        )
        self.assertNotIn("正式JSON", "/".join("/".join(path) for path in paths))


class SafetyTests(unittest.TestCase):
    def test_secret_redaction_covers_explicit_secret_bearer_token_and_private_url(self):
        text = (
            "secret-value Authorization: Bearer another.secret "
            "bascnAbCdEf123456 https://private.example.test/a/b?token=abc"
        )
        redacted = provision.redact_text(text, ["secret-value"])
        self.assertNotIn("secret-value", redacted)
        self.assertNotIn("another.secret", redacted)
        self.assertNotIn("bascnAbCdEf123456", redacted)
        self.assertNotIn("/a/b", redacted)
        self.assertIn("[REDACTED]", redacted)

    def test_folder_lookup_is_idempotent_and_rejects_same_name_file(self):
        token = provision.find_folder(
            [{"name": "2032-07", "type": "folder", "token": "fldExisting123456"}],
            "2032-07",
        )
        self.assertEqual(token, "fldExisting123456")
        with self.assertRaisesRegex(provision.ProvisionError, "non-folder"):
            provision.find_folder(
                [{"name": "2032-07", "type": "file", "token": "fileExisting"}],
                "2032-07",
            )

    def test_service_url_rejects_credentials_or_plain_http(self):
        for value in ("http://example.test", "https://user:user@example.invalid"):
            with self.subTest(value=value), self.assertRaises(provision.ProvisionError):
                provision.validate_service_base_url(value)


if __name__ == "__main__":
    unittest.main()
