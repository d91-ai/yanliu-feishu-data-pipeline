from __future__ import annotations

import importlib.util
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


audit = load("publish_audit_tested", ROOT / "audit_publish_directory.py")
migration = load("unified_migration_tested", ROOT / "migrate_unified_base.py")
reconcile = load("publish_reconcile_tested", ROOT / "plan_publish_reconcile.py")
repair = load("baseline_repair_tested", ROOT / "repair_baselines.py")
base_cutover = load("unified_base_cutover_tested", ROOT / "apply_unified_base.py")
readability = load(
    "unified_base_readability_tested", ROOT / "refine_unified_base_readability.py"
)
UID = "mtg_550e8400e29b41d4a716446655440000"


def metadata(artifact_type="industry_market_viewpoints", version=1):
    return {
        "schema_version": 1,
        "meeting_uid": UID,
        "meeting_date": "2032-08-13",
        "meeting_series": "示例研究周会",
        "meeting_type": "多人复盘会",
        "artifact_type": artifact_type,
        "data_version": version,
        "quality_status": "unreviewed",
        "source_review_status": "未审核",
        "artifact_review_status": "未审核",
        "source_md_sha256": "a" * 64,
        "review_md_sha256": "b" * 64,
        "item_count": 0,
        "generated_at": "2032-08-13T09:00:00+08:00",
    }


class PublishAuditTests(unittest.TestCase):
    def test_clean_current_file(self):
        artifact = {"metadata": metadata(), "items": []}
        base = [
            {
                "record_id": "rec-one",
                "fields": {
                    "会议ID": UID,
                    "数据版本": 1,
                    "行业与市场观点JSON": "https://example.test/file/json-token",
                },
            }
        ]
        files = [{"file_token": "json-token", "artifact": artifact}]
        result = audit.audit(base, files, manifest_root=Path("."))
        self.assertTrue(result["ok"])

    def test_orphan_and_identity_conflict_are_reported(self):
        artifact = {"metadata": metadata(), "items": []}
        conflicting = {"metadata": metadata(), "items": [{"different": True}]}
        files = [
            {"file_token": "one", "artifact": artifact},
            {"file_token": "two", "artifact": conflicting},
        ]
        result = audit.audit([], files, manifest_root=Path("."))
        codes = {item["code"] for item in result["issues"]}
        self.assertIn("same_identity_hash_conflict", codes)
        self.assertIn("orphan_json", codes)

    def test_manifest_hash_must_match_exact_artifact_bytes(self):
        result = audit.audit(
            [],
            [{"file_token": "one", "artifact": {"metadata": metadata()}, "sha256": "1" * 64}],
            manifest_root=Path("."),
        )
        self.assertEqual(result["issues"][0]["code"], "artifact_invalid")
        self.assertIn("does not match", result["issues"][0]["detail"])

    def test_missing_current_file_is_fail_closed(self):
        result = audit.audit(
            [
                {
                    "record_id": "rec-one",
                    "fields": {
                        "会议ID": UID,
                        "数据版本": 1,
                        "标的观点JSON": "https://example.test/file/missing",
                    },
                }
            ],
            [],
            manifest_root=Path("."),
        )
        self.assertEqual(result["issues"][0]["code"], "base_current_file_missing_or_invalid")


class MigrationTests(unittest.TestCase):
    def source(self, record_id="source-one", uid=""):
        fields = {
            "文件名": "2032-08-13 - 示例研究周会",
            "会议日期": "2032-08-13",
            "会议系列": "示例研究周会",
            "会议类型": "多人复盘会",
            "文档链接": "https://example.test/file/source",
            "审核前版本链接": "https://example.test/file/source-baseline",
            "审核状态": True,
            "归档链接": "https://example.test/file/source-reviewed",
        }
        if uid:
            fields["会议UID"] = uid
        return {"record_id": record_id, "fields": fields}

    def test_missing_uid_is_generated_deterministically_and_twenty_one_fields_are_emitted(self):
        first = migration.build_plan([self.source()], [], [], [])
        second = migration.build_plan([self.source()], [], [], [])
        self.assertEqual(first["records"][0]["fields"]["会议ID"], second["records"][0]["fields"]["会议ID"])
        self.assertTrue(first["records"][0]["meeting_uid_generated"])
        self.assertEqual(len(first["records"][0]["fields"]), 21)
        self.assertEqual(
            first["records"][0]["fields"]["会议名"],
            "2032-08-13 - 示例研究周会",
        )
        self.assertEqual(
            first["records"][0]["fields"]["会议纪要MD"],
            "https://example.test/file/source-reviewed",
        )

    def test_readability_plan_derives_names_without_exposing_uid(self):
        plan = readability.build_name_plan([self.source(record_id="rec-one")], 1)
        self.assertEqual(plan, {"rec-one": "2032-08-13 - 示例研究周会"})
        self.assertNotIn("mtg_", next(iter(plan.values())))

    def test_structured_and_official_json_are_joined_by_record_identity(self):
        structured = [
            {
                "record_id": "structured-one",
                "fields": {
                    "源纪要记录": ["source-one"],
                    "待审核MD链接": "https://example.test/file/structured",
                    "已审核": True,
                    "审核后归档MD链接": "https://example.test/file/structured-reviewed",
                },
            }
        ]
        official = [
            {
                "record_id": "json-one",
                "fields": {
                    "来源结构化MD记录": ["structured-one"],
                    "JSON链接": "https://example.test/file/json",
                },
            }
        ]
        plan = migration.build_plan([self.source()], structured, official, [])
        fields = plan["records"][0]["fields"]
        self.assertEqual(fields["标的观点JSON"], "https://example.test/file/json")
        self.assertEqual(fields["标的观点审核"], "已审核")
        self.assertEqual(
            fields["标的观点MD"],
            "https://example.test/file/structured-reviewed",
        )

    def test_legacy_structured_source_link_is_preserved_and_requires_baseline(self):
        source = self.source()
        source["fields"]["表格链接"] = "https://example.test/file/legacy-structured"
        plan = migration.build_plan([source], [], [], [])
        self.assertEqual(
            plan["records"][0]["fields"]["标的观点MD"],
            "https://example.test/file/legacy-structured",
        )
        self.assertIn(
            "structured_baseline_required",
            {issue["code"] for issue in plan["issues"]},
        )

    def test_missing_source_baseline_blocks_local_apply(self):
        source = self.source()
        source["fields"]["审核前版本链接"] = ""
        plan = migration.build_plan([source], [], [], [])
        self.assertIn(
            "source_baseline_required",
            {issue["code"] for issue in plan["issues"]},
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(migration.MigrationError, "unresolved issues"):
                migration.write_local_apply(Path(directory) / "snapshot.json", plan)

    def test_verified_baseline_receipts_close_exact_legacy_gaps(self):
        source = self.source(uid=UID)
        source["fields"]["审核前版本链接"] = ""
        source["fields"]["表格链接"] = "https://example.test/file/legacy-structured"
        repairs = {
            ("source-one", "meeting_minutes"): {
                "meeting_uid": UID,
                "target_url": "https://example.test/file/source-baseline-repaired",
            },
            ("source-one", "structured_viewpoints"): {
                "meeting_uid": UID,
                "target_url": "https://example.test/file/structured-baseline-repaired",
            },
        }
        plan = migration.build_plan([source], [], [], [], repairs)
        self.assertEqual(plan["issue_count"], 0)
        self.assertEqual(
            plan["records"][0]["fields"]["会议纪要审核前MD"],
            "https://example.test/file/source-baseline-repaired",
        )
        self.assertEqual(
            plan["records"][0]["fields"]["标的观点审核前MD"],
            "https://example.test/file/structured-baseline-repaired",
        )

    def test_baseline_receipt_uid_mismatch_and_unmatched_receipt_block(self):
        mismatched = {
            ("source-one", "meeting_minutes"): {
                "meeting_uid": "mtg_11111111111111111111111111111111",
                "target_url": "https://example.test/file/baseline",
            },
            ("unknown", "structured_viewpoints"): {
                "meeting_uid": UID,
                "target_url": "https://example.test/file/unmatched",
            },
        }
        plan = migration.build_plan([self.source(uid=UID)], [], [], [], mismatched)
        codes = {issue["code"] for issue in plan["issues"]}
        self.assertIn("baseline_repair_uid_mismatch", codes)
        self.assertIn("baseline_repair_unmatched", codes)

    def test_ambiguous_structured_records_block_the_source(self):
        structured = [
            {"record_id": "s1", "fields": {"源纪要记录": ["source-one"]}},
            {"record_id": "s2", "fields": {"源纪要记录": ["source-one"]}},
        ]
        plan = migration.build_plan([self.source()], structured, [], [])
        self.assertEqual(plan["planned_count"], 0)
        self.assertEqual(plan["issues"][0]["code"], "structured_record_ambiguous")

    def test_local_apply_is_idempotent_and_never_calls_feishu(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "snapshot.json"
            plan = migration.build_plan([self.source()], [], [], [])
            self.assertEqual(migration.write_local_apply(target, plan), "written_local_snapshot")
            self.assertEqual(migration.write_local_apply(target, plan), "skipped_idempotent")


class ReconcilePlanTests(unittest.TestCase):
    def test_only_safe_recovery_actions_are_planned(self):
        plan = reconcile.build_plan(
            {
                "issues": [
                    {
                        "code": "orphan_json",
                        "file_token": "orphan",
                        "meeting_uid": UID,
                        "artifact_type": "structured_viewpoints",
                    },
                    {
                        "code": "base_current_file_missing_or_invalid",
                        "record_id": "rec-one",
                        "field": "标的观点JSON",
                        "file_token": "missing",
                    },
                    {"code": "same_identity_hash_conflict", "meeting_uid": UID},
                ]
            }
        )
        self.assertEqual(plan["action_count"], 2)
        self.assertEqual(plan["blocked_count"], 1)
        self.assertFalse(any(item["destructive"] for item in plan["actions"]))


class FakeRepairRouter:
    def __init__(self):
        self.source_record = {
            "fields": {
                "会议UID": UID,
                "会议日期": "2032-08-13",
                "会议系列": "示例研究周会",
                "审核状态": False,
                "文档链接": "https://example.test/file/source-token",
            }
        }
        self.content = b"# meeting\n"
        self.version = {"version": "version-one", "tag": 1}
        self.folder_items = {
            "version-root": [{"type": "folder", "name": "会议纪要", "token": "category"}],
            "category": [{"type": "folder", "name": "2032-08", "token": "month"}],
            "month": [{"type": "folder", "name": "审核前", "token": "baseline"}],
            "baseline": [],
        }
        self.upload_count = 0

    @staticmethod
    def plain_field_value(value):
        return str(value or "")

    def get_bitable_record(self, _cfg, _record_id):
        return self.source_record

    @staticmethod
    def form_meeting_date_from_field(_cfg, value):
        return str(value)

    @staticmethod
    def checkbox_is_checked(value):
        return bool(value)

    @staticmethod
    def url_from_field_value(value):
        return str(value)

    @staticmethod
    def parse_drive_url(_url):
        return "source-token", "file"

    def first_valid_file_version(self, _cfg, _token):
        return self.version, self.content

    @staticmethod
    def sha256_hex(content):
        import hashlib

        return hashlib.sha256(content).hexdigest()

    @staticmethod
    def get_file_meta(_cfg, _token, _file_type):
        return {"title": "2032-08-13 - 示例研究周会.md"}

    @staticmethod
    def baseline_artifact_name(file_name, file_token, version_info):
        return f"{Path(file_name).stem} - 审核前 - {file_token[-8:]} - v{version_info['tag']}.md"

    def list_drive_folder_items(self, _cfg, folder_token):
        return list(self.folder_items.get(folder_token, []))

    @staticmethod
    def drive_item_token(item):
        return str(item.get("token") or "")

    def download_drive_file_version(self, _cfg, token):
        if token == "uploaded-token":
            return self.content
        raise AssertionError(f"unexpected download token: {token}")

    def upload_version_artifact(self, _cfg, folder_token, file_name, content):
        self.upload_count += 1
        self.assert_upload(folder_token, file_name, content)
        self.folder_items[folder_token] = [
            {
                "type": "file",
                "name": file_name,
                "token": "uploaded-token",
                "url": "https://example.test/file/uploaded-token",
            }
        ]
        return "uploaded-token", "https://example.test/file/uploaded-token"

    def assert_upload(self, folder_token, file_name, content):
        if folder_token != "baseline" or content != self.content or not file_name.endswith(".md"):
            raise AssertionError("unexpected upload")

    @staticmethod
    def resolve_drive_file_url(_cfg, token, _folder):
        return f"https://example.test/file/{token}"

    @staticmethod
    def write_private_json(path, payload):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")


class BaselineRepairTests(unittest.TestCase):
    def setUp(self):
        self.router = FakeRepairRouter()
        self.source_cfg = SimpleNamespace()
        self.artifact_cfg = SimpleNamespace(
            version_root_folder_token="version-root",
            version_category="会议纪要",
        )
        content_hash = self.router.sha256_hex(self.router.content)
        self.target = {
            "record_id": "source-one",
            "meeting_uid": UID,
            "meeting_date": "2032-08-13",
            "meeting_series": "示例研究周会",
            "expected_reviewed": False,
            "artifact_type": "meeting_minutes",
            "source_file_token": "source-token",
            "source_version": "version-one",
            "source_sha256": content_hash,
            "source_size_bytes": len(self.router.content),
            "target_folder_token": "baseline",
            "target_name": "2032-08-13 - 示例研究周会 - 审核前 - ce-token - v1.md",
        }

    def test_manifest_rejects_bad_size_and_duplicate_identity(self):
        manifest = {
            "schema_version": 1,
            "mode": "production-cutover-preflight",
            "targets": [self.target],
        }
        self.assertEqual(len(repair.validate_manifest(manifest)), 1)
        invalid = json.loads(json.dumps(manifest))
        invalid["targets"][0]["source_size_bytes"] = True
        with self.assertRaisesRegex(repair.RepairError, "source size"):
            repair.validate_manifest(invalid)
        duplicate = {**manifest, "targets": [self.target, dict(self.target)]}
        with self.assertRaisesRegex(repair.RepairError, "duplicate"):
            repair.validate_manifest(duplicate)

    def test_preflight_is_read_only_and_detects_record_drift(self):
        result = repair.preflight_target(
            self.router, self.source_cfg, self.artifact_cfg, self.target
        )
        self.assertEqual(result["status"], "ready_to_upload")
        self.assertEqual(self.router.upload_count, 0)
        self.router.source_record["fields"]["会议系列"] = "changed"
        with self.assertRaisesRegex(repair.RepairError, "meeting_series mismatch"):
            repair.preflight_target(
                self.router, self.source_cfg, self.artifact_cfg, self.target
            )

    def test_preflight_rejects_duplicate_exact_target(self):
        item = {
            "type": "file",
            "name": self.target["target_name"],
            "token": "uploaded-token",
        }
        self.router.folder_items["baseline"] = [item, dict(item, token="other")]
        with self.assertRaisesRegex(repair.RepairError, "ambiguous"):
            repair.preflight_target(
                self.router, self.source_cfg, self.artifact_cfg, self.target
            )

    def test_apply_uploads_once_writes_receipt_and_replay_uses_existing(self):
        with tempfile.TemporaryDirectory() as directory:
            result = repair.preflight_target(
                self.router, self.source_cfg, self.artifact_cfg, self.target
            )
            receipt = repair.apply_target(
                self.router, self.artifact_cfg, result, Path(directory)
            )
            self.assertEqual(receipt["status"], "uploaded_verified")
            self.assertEqual(self.router.upload_count, 1)
            replay = repair.preflight_target(
                self.router, self.source_cfg, self.artifact_cfg, self.target
            )
            repair.apply_target(self.router, self.artifact_cfg, replay, Path(directory))
            self.assertEqual(self.router.upload_count, 1)


class FakeBaseCutoverClient:
    def __init__(self, config, source_export):
        self.config = config
        meeting_id_field = next(
            item for item in config["reused_fields"] if item["target_name"] == "会议ID"
        )
        self.table_value = {
            "id": config["source_table_id"],
            "name": config["current_table_name"],
            "primary_field": meeting_id_field["field_id"],
        }
        self.field_values = []
        self.values_by_id = {}
        source_fields = source_export["records"][0]["fields"]
        for item in config["reused_fields"]:
            definition = copy.deepcopy(item["definition"])
            definition["id"] = item["field_id"]
            definition["name"] = item["current_name"]
            self.field_values.append(definition)
            self.values_by_id[item["field_id"]] = source_fields.get(item["current_name"], "")
        self.legacy_values = dict(source_fields)
        self.view_values = [
            {"id": "view-old", "name": "审计详情", "type": "grid"}
        ]
        self.view_visible = {"view-old": ["文件名"]}
        self.workflow_values = dict(config["old_workflows"])
        self.field_create_count = 0
        self.record_update_count = 0
        self.next_view = 1

    def table(self, _base, _table):
        return copy.deepcopy(self.table_value)

    def fields(self, _base, _table):
        return copy.deepcopy(self.field_values)

    def views(self, _base, _table):
        return copy.deepcopy(self.view_values)

    def workflows(self, _base):
        return dict(self.workflow_values)

    def _field_by_identifier(self, identifier):
        for field in self.field_values:
            if field.get("id") == identifier or field.get("name") == identifier:
                return field
        return None

    def records(self, _base, table_id, record_ids, field_ids):
        if table_id != self.config["source_table_id"]:
            return {}
        result = {}
        for record_id in record_ids:
            if record_id != "source-one":
                continue
            by_name = {}
            by_id = {}
            for identifier in field_ids:
                field = self._field_by_identifier(identifier)
                if field:
                    value = self.values_by_id.get(field["id"], "")
                    if field.get("type") == "select" and value not in (None, ""):
                        value = value if isinstance(value, list) else [value]
                    by_name[field["name"]] = value
                    by_id[field["id"]] = value
                else:
                    value = self.legacy_values.get(identifier, "")
                    by_name[identifier] = value
                    by_id[identifier] = value
            result[record_id] = {"by_name": by_name, "by_id": by_id}
        return result

    def field_create(self, _base, _table, definition):
        self.field_create_count += 1
        field = copy.deepcopy(definition)
        field["id"] = f"fld-new-{self.field_create_count}"
        self.field_values.append(field)
        self.values_by_id[field["id"]] = ""

    def field_update(self, _base, _table, field_id, definition):
        for index, field in enumerate(self.field_values):
            if field.get("id") == field_id:
                replacement = copy.deepcopy(definition)
                replacement["id"] = field_id
                self.field_values[index] = replacement
                return
        raise AssertionError("missing field update target")

    def record_update(self, _base, _table, record_id, patch):
        self.assert_record(record_id)
        self.record_update_count += 1
        self.values_by_id.update(patch)

    @staticmethod
    def assert_record(record_id):
        if record_id != "source-one":
            raise AssertionError("unexpected record")

    def table_rename(self, _base, _table, name):
        self.table_value["name"] = name

    def view_create(self, _base, _table, name, view_type):
        view_id = f"view-new-{self.next_view}"
        self.next_view += 1
        self.view_values.append({"id": view_id, "name": name, "type": view_type})
        self.view_visible[view_id] = []

    def view_set_fields(self, _base, _table, view_id, fields):
        names = [
            self._field_by_identifier(identifier)["name"] for identifier in fields
        ]
        view = next(item for item in self.view_values if item["id"] == view_id)
        if any("审核" in name for name in names) and len(self.view_visible[view_id]) > len(names):
            raise base_cutover.CliError("no operation produced")
        self.view_visible[view_id] = names

    def view_rename(self, _base, _table, view_id, name):
        view = next(item for item in self.view_values if item["id"] == view_id)
        view["name"] = name

    def view_fields(self, _base, _table, view_id):
        return list(self.view_visible[view_id])


class UnifiedBaseCutoverTests(unittest.TestCase):
    @staticmethod
    def fixtures():
        config = json.loads(
                (ROOT / "cutover_config" / "example-unified-base.v1.json").read_text(encoding="utf-8")
        )
        config["snapshot_tables"] = {
            "source": {"table_id": config["source_table_id"], "record_count": 1},
            "structured": {"table_id": "tbl-structured", "record_count": 0},
            "sanitized": {"table_id": "tbl-sanitized", "record_count": 0},
            "official": {"table_id": "tbl-official", "record_count": 0},
        }
        source = {
            "record_id": "source-one",
            "fields": {
                "会议UID": UID,
                "会议日期": "2032-08-13",
                "会议系列": ["示例研究周会"],
                "会议类型": ["多人复盘会"],
                "文档链接": "https://example.test/file/source",
                "审核前版本链接": "https://example.test/file/source-baseline",
                "归档链接": "https://example.test/file/source-reviewed",
                "审核状态": True,
                "表格链接": "",
                "脱敏MD链接": "",
                "文件名": "2032-08-13 - 示例研究周会",
            },
        }
        source_export = {"records": [source]}
        exports = {
            "source": source_export,
            "structured": {"records": []},
            "sanitized": {"records": []},
            "official": {"records": []},
        }
        plan = migration.build_plan([source], [], [], [])
        for item in plan["records"]:
            item["fields"].pop("会议名")
        migration_snapshot = {"plan_sha256": migration.canonical_hash(plan), **plan}
        schema = json.loads(
                (ROOT / "cutover_config" / "unified-base.v1.schema.json").read_text(encoding="utf-8")
        )
        return config, schema, migration_snapshot, exports

    def test_primary_field_is_the_final_meeting_id(self):
        config, schema, migration_snapshot, _exports = self.fixtures()
        validated = base_cutover.validate_config(config)
        names = base_cutover.validate_schema(schema, validated)
        base_cutover.validate_migration(migration_snapshot, names, 1)
        meeting_id = next(
            item for item in validated["reused_fields"] if item["target_name"] == "会议ID"
        )
        self.assertEqual(meeting_id["field_id"], "fld_example_meeting_id")
        self.assertEqual(meeting_id["current_name"], "文件名")

    def test_business_values_match_live_single_select_and_url_shapes(self):
        self.assertEqual(
            base_cutover.normalized_business_value("会议系列", ["示例研究周会"]),
            "示例研究周会",
        )
        url = "https://example.test/file/source-baseline"
        self.assertEqual(
            base_cutover.normalized_business_value(
                "会议纪要审核前MD", f"[{url}]({url})"
            ),
            url,
        )
        self.assertEqual(
            base_cutover.normalized_business_value("会议纪要审核前MD", url),
            url,
        )

    def test_preflight_and_resumable_apply_are_idempotent(self):
        config, schema, migration_snapshot, exports = self.fixtures()
        config = base_cutover.validate_config(config)
        names = base_cutover.validate_schema(schema, config)
        migration_snapshot = base_cutover.validate_migration(
            migration_snapshot, names, 1
        )
        client = FakeBaseCutoverClient(config, exports["source"])
        state = base_cutover.preflight(
            client, config, migration_snapshot, exports, maintenance=False
        )
        self.assertTrue(state["legacy_state"])
        self.assertEqual(len(state["missing_new_fields"]), 13)
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "journal.json"
            result = base_cutover.apply_cutover(
                client,
                config,
                migration_snapshot,
                state,
                journal_path=journal,
            )
            self.assertEqual(result["status"], "applied_verified")
            self.assertEqual(client.table_value["name"], "会议数据库")
            primary = next(
                item
                for item in client.field_values
                if item.get("id") == "fld_example_meeting_id"
            )
            self.assertEqual(primary["name"], "会议ID")
            first_field_creates = client.field_create_count
            first_record_updates = client.record_update_count
            base_cutover.apply_cutover(
                client,
                config,
                migration_snapshot,
                state,
                journal_path=journal,
            )
            self.assertEqual(client.field_create_count, first_field_creates)
            self.assertEqual(client.record_update_count, first_record_updates)
            saved = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual(saved["stage"], "complete")


if __name__ == "__main__":
    unittest.main()
