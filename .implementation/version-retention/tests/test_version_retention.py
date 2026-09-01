from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MEETING_UID = "mtg_550e8400e29b41d4a716446655440000"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


archive = load_module(
    "version_archive_service",
    ROOT / "feishu-drive-to-bitable" / "feishu_drive_to_bitable.py",
)
structured = load_module(
    "version_structured_service",
    ROOT / "feishu-structured-generate" / "structured_generate_service.py",
)


def archive_config(**overrides):
    values = {
        "app_id": "app",
        "app_secret": "secret",
        "folder_token": "source",
        "source_folder_tokens": (),
        "archive_root_folder_token": "archive-root",
        "folder_registry_path": str(Path(tempfile.gettempdir()) / "data-pipeline-version-tests" / "folders.json"),
        "bitable_app_token": "base",
        "bitable_table_id": "table",
        "archive_dry_run": False,
        "archive_original_time_field": "上传时间",
        "version_capture_enabled": True,
        "version_capture_enforce": True,
        "version_root_folder_token": "version-root",
        "version_category": "结构化表格",
        "event_spool_dir": str(Path(tempfile.gettempdir()) / "data-pipeline-version-tests" / "event-spool"),
    }
    values.update(overrides)
    return archive.Config(**values)


class ArchiveVersionRetentionTests(unittest.TestCase):
    def test_meeting_contract_validator_accepts_only_the_pinned_script(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            validator = Path(tmpdir) / "validator.py"
            validator.write_text('print("{\\"ok\\": true}")\n', encoding="utf-8")
            digest = hashlib.sha256(validator.read_bytes()).hexdigest()
            cfg = archive_config(
                meeting_contract_enabled=True,
                meeting_contract_validator=str(validator),
                meeting_contract_validator_sha256=digest,
            )

            archive.validate_meeting_contract_content(cfg, b"# valid fixture\n")

            mismatched = archive_config(
                meeting_contract_enabled=True,
                meeting_contract_validator=str(validator),
                meeting_contract_validator_sha256="0" * 64,
            )
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                archive.validate_meeting_contract_content(mismatched, b"# valid fixture\n")

    def test_meeting_contract_body_validator_is_not_an_ingestion_gate(self):
        cfg = archive_config(
            dry_run=False,
            meeting_contract_enabled=True,
            meeting_contract_validator="/validator.py",
            meeting_contract_validator_sha256="0" * 64,
        )
        event = {
            "header": {"event_id": "evt-invalid-meeting", "create_time": "1780000000000"},
            "event": {"folder_token": "source", "file_token": "meeting-token", "file_type": "file"},
        }
        source_field = {
            "field_id": "fld-source",
            "field_name": "文档来源",
            "type": archive.TYPE_SINGLE_SELECT,
            "property": {"options": [{"name": "会议纪要"}]},
        }
        token_field = {
            "field_id": "fld-token",
            "field_name": "文件Token",
            "type": archive.TYPE_TEXT,
        }
        with (
            mock.patch.object(
                archive,
                "get_file_meta",
                return_value={"title": "2032-07-13 - invalid.md", "create_time": "1780000000"},
            ),
            mock.patch.object(
                archive,
                "validate_meeting_contract_content",
                side_effect=ValueError("Meeting minutes contract validation failed"),
            ) as validate,
            mock.patch.object(
                archive, "list_bitable_fields", return_value=[source_field, token_field]
            ),
            mock.patch.object(
                archive,
                "build_record_fields",
                return_value=({"文件Token": "meeting-token"}, []),
            ),
            mock.patch.object(
                archive, "create_bitable_record_reconciled", return_value="rec-created"
            ) as create,
            mock.patch.object(
                archive, "capture_baseline_for_record_with_failure_status", return_value={}
            ),
        ):
            archive.process_file_created_event(cfg, event)

        validate.assert_not_called()
        create.assert_called_once()

    def test_meeting_contract_pass_marks_document_source_before_creation(self):
        cfg = archive_config(
            dry_run=False,
            version_capture_enabled=False,
            meeting_contract_enabled=True,
            meeting_contract_validator="/validator.py",
            meeting_contract_validator_sha256="0" * 64,
        )
        event = {
            "header": {"event_id": "evt-valid-meeting", "create_time": "1780000000000"},
            "event": {"folder_token": "source", "file_token": "meeting-token", "file_type": "file"},
        }
        source_field = {
            "field_id": "fld-source",
            "field_name": "文档来源",
            "type": archive.TYPE_SINGLE_SELECT,
            "property": {"options": [{"name": "会议纪要"}]},
        }
        file_token_field = {
            "field_id": "fld-file-token",
            "field_name": "文件Token",
            "type": archive.TYPE_TEXT,
        }
        with (
            mock.patch.object(
                archive,
                "get_file_meta",
                return_value={"title": "2032-07-13 - valid.md", "create_time": "1780000000"},
            ),
            mock.patch.object(archive, "download_drive_file_version", return_value=b"valid meeting"),
            mock.patch.object(archive, "validate_meeting_contract_content"),
            mock.patch.object(archive, "list_bitable_fields", return_value=[source_field, file_token_field]),
            mock.patch.object(
                archive,
                "build_record_fields",
                return_value=(
                    {
                        "文档链接": "https://example.test/file/meeting-token",
                        "文件Token": "meeting-token",
                    },
                    [],
                ),
            ),
            mock.patch.object(
                archive,
                "create_bitable_record",
                return_value={"data": {"record": {"record_id": "rec-valid"}}},
            ) as create,
        ):
            archive.process_file_created_event(cfg, event)

        self.assertEqual(create.call_args.args[1]["文档来源"], "会议纪要")

    def test_capture_baseline_uses_first_valid_version_and_writes_auditable_fields(self):
        cfg = archive_config()
        fields = {
            "文档链接": {"link": "https://example.feishu.cn/file/source-token"},
            "上传时间": 1780000000000,
            "文件名": "review.md",
        }
        with (
            mock.patch.object(archive, "update_bitable_record") as update,
            mock.patch.object(archive, "get_file_meta", return_value={"title": "review.md"}),
            mock.patch.object(
                archive,
                "first_valid_file_version",
                return_value=({"version": "1001", "tag": 1}, b"baseline\n"),
            ),
            mock.patch.object(archive, "ensure_version_baseline_folder", return_value="baseline-folder"),
            mock.patch.object(
                archive,
                "upload_version_artifact",
                return_value=("baseline-token", "https://example.feishu.cn/file/baseline-token"),
            ),
        ):
            result = archive.capture_baseline_for_record(cfg, "rec1", fields=fields)

        self.assertEqual(result["status"], "baseline_captured")
        self.assertEqual(result["version"], "1001")
        self.assertEqual(result["sha256"], archive.sha256_hex(b"baseline\n"))
        final_fields = update.call_args_list[-1].args[2]
        self.assertEqual(final_fields[archive.FIELD_VERSION_STATUS], archive.VERSION_STATUS_BASELINE)
        self.assertEqual(final_fields[archive.FIELD_BASELINE_VERSION], "1001")
        self.assertIn("审核前", final_fields[archive.FIELD_BASELINE_LINK]["text"])

    def test_capture_baseline_accepts_event_month_before_meeting_date_is_populated(self):
        cfg = archive_config(archive_original_time_field="会议日期")
        fields = {
            "文档链接": {"link": "https://example.feishu.cn/file/source-token"},
            "文件名": "review.md",
        }
        with (
            mock.patch.object(archive, "update_bitable_record"),
            mock.patch.object(archive, "get_file_meta", return_value={"title": "review.md"}),
            mock.patch.object(
                archive,
                "first_valid_file_version",
                return_value=({"version": "1001", "tag": 1}, b"baseline\n"),
            ),
            mock.patch.object(archive, "ensure_version_baseline_folder", return_value="baseline-folder") as ensure,
            mock.patch.object(
                archive,
                "upload_version_artifact",
                return_value=("baseline-token", "https://example.feishu.cn/file/baseline-token"),
            ),
        ):
            result = archive.capture_baseline_for_record(
                cfg,
                "rec1",
                fields=fields,
                month_override="2032-07",
            )

        self.assertEqual(result["month"], "2032-07")
        ensure.assert_called_once_with(cfg, "2032-07")

    def test_file_created_event_passes_source_folder_month_to_baseline_capture(self):
        cfg = archive_config(dry_run=False)
        event = {
            "header": {"event_id": "evt1", "create_time": "1780000000000"},
            "event": {
                "folder_token": "month-folder",
                "file_token": "source-token",
                "file_type": "file",
            },
        }
        with (
            mock.patch.object(archive, "allowed_source_folder_tokens", return_value={"month-folder"}),
            mock.patch.object(archive, "month_for_source_folder", return_value="2032-07"),
            mock.patch.object(archive, "get_file_meta", return_value={"title": "review.md", "create_time": "1780000000"}),
            mock.patch.object(
                archive,
                "list_bitable_fields",
                return_value=[{"field_id": "fld-token", "field_name": "文件Token", "type": archive.TYPE_TEXT}],
            ),
            mock.patch.object(
                archive,
                "build_record_fields",
                return_value=({"文档链接": "url", "文件Token": "source-token"}, []),
            ),
            mock.patch.object(
                archive,
                "create_bitable_record",
                return_value={"data": {"record": {"record_id": "rec1"}}},
            ),
            mock.patch.object(archive, "capture_baseline_for_record", return_value={"status": "baseline_captured"}) as capture,
        ):
            archive.process_file_created_event(cfg, event)

        capture.assert_called_once_with(cfg, "rec1", month_override="2032-07")

    def test_missing_file_token_field_fails_before_record_creation(self):
        cfg = archive_config(dry_run=False, version_capture_enabled=False, version_capture_enforce=False)
        event = {
            "header": {"event_id": "evt-no-token", "create_time": "1780000000000"},
            "event": {"folder_token": "source", "file_token": "source-token", "file_type": "file"},
        }
        with (
            mock.patch.object(
                archive,
                "get_file_meta",
                return_value={"title": "review.md", "create_time": "1780000000"},
            ),
            mock.patch.object(
                archive,
                "list_bitable_fields",
                return_value=[{"field_id": "name", "field_name": "文件名", "type": archive.TYPE_TEXT}],
            ),
            mock.patch.object(archive, "build_record_fields", return_value=({"文件名": "review.md"}, [])),
            mock.patch.object(archive, "create_bitable_record_reconciled") as create,
        ):
            with self.assertRaisesRegex(ValueError, "file-token"):
                archive.process_file_created_event(cfg, event)
        create.assert_not_called()

    def test_structured_v3_frontmatter_enriches_record_before_creation(self):
        cfg = archive_config(structured_metadata_enabled=True)

        def field(name, field_type, options=()):
            payload = {"field_id": f"fld-{name}", "field_name": name, "type": field_type}
            if options:
                payload["property"] = {"options": [{"name": option} for option in options]}
            return payload

        fields = [
            field("源纪要记录", archive.TYPE_TEXT),
            field("源纪要链接", archive.TYPE_URL),
            field("观点数", archive.TYPE_NUMBER),
            field("会议日期", archive.TYPE_DATE),
            field("会议系列", archive.TYPE_SINGLE_SELECT, ("吴老师",)),
            field("会议类型", archive.TYPE_SINGLE_SELECT, ("多人复盘会",)),
            field("文档来源", archive.TYPE_SINGLE_SELECT, ("会议纪要",)),
            field("已审核", archive.TYPE_CHECKBOX),
            field("归档状态", archive.TYPE_SINGLE_SELECT, ("待归档",)),
        ]
        content = b"""---
artifact_stage: \"structured_review_md\"
schema_version: 3
source_record_id: \"recv-source-1\"
source_archive_url: \"https://example.feishu.cn/file/source-token\"
source_file_name: \"2032-07-13 - \xe7\xba\xaa\xe5\x8d\x9a\xe4\xba\xa4\xe6\xb5\x81\xe4\xbc\x9a - \xe5\xa4\x9a\xe4\xba\xba\xe5\xa4\x8d\xe7\x9b\x98\xe4\xbc\x9a.md\"
viewpoint_count: 17
---

# \xe8\xa7\x82\xe7\x82\xb9\xe4\xba\x8b\xe5\xae\x9e\xe5\xae\xa1\xe9\x98\x85\xe8\xa1\xa8
"""
        record_fields = {"待审核MD链接": {"link": "https://example.feishu.cn/file/review"}}
        report = []

        archive.enrich_structured_record_fields(
            cfg,
            fields,
            title="2032-07-13 - 吴老师 - 标的观点.md",
            content=content,
            record_fields=record_fields,
            report=report,
        )

        self.assertEqual(record_fields["源纪要记录"], "recv-source-1")
        self.assertEqual(record_fields["源纪要链接"]["link"], "https://example.feishu.cn/file/source-token")
        self.assertEqual(record_fields["观点数"], 17)
        self.assertEqual(record_fields["会议系列"], "吴老师")
        self.assertEqual(record_fields["会议类型"], "多人复盘会")
        self.assertEqual(record_fields["文档来源"], "会议纪要")
        self.assertFalse(record_fields["已审核"])
        self.assertEqual(record_fields["归档状态"], "待归档")
        self.assertIn("validated structured_review_md frontmatter", report)

    def test_structured_v3_event_creates_record(self):
        cfg = archive_config(
            structured_metadata_enabled=True,
            dry_run=False,
            version_capture_enabled=False,
        )

        def field(name, field_type, options=()):
            payload = {"field_id": f"fld-{name}", "field_name": name, "type": field_type}
            if options:
                payload["property"] = {"options": [{"name": option} for option in options]}
            return payload

        fields = [
            field("文件Token", archive.TYPE_TEXT),
            field("源纪要记录", archive.TYPE_TEXT),
            field("源纪要链接", archive.TYPE_URL),
            field("观点数", archive.TYPE_NUMBER),
            field("会议日期", archive.TYPE_DATE),
            field("会议系列", archive.TYPE_SINGLE_SELECT, ("科技",)),
            field("会议类型", archive.TYPE_SINGLE_SELECT, ("多人复盘会",)),
            field("文档来源", archive.TYPE_SINGLE_SELECT, ("会议纪要",)),
            field("已审核", archive.TYPE_CHECKBOX),
            field("归档状态", archive.TYPE_SINGLE_SELECT, ("待归档",)),
        ]
        content = b"""---
artifact_stage: "structured_review_md"
schema_version: 3
source_record_id: "recv-source-1"
source_archive_url: "https://example.feishu.cn/file/source-token"
source_file_name: "2032-07-30 - \xe7\xa7\x91\xe6\x8a\x80.md"
viewpoint_count: 36
---
"""
        event = {
            "header": {"event_id": "evt-structured-v3", "create_time": "1780000000000"},
            "event": {"folder_token": "source", "file_token": "review-token", "file_type": "file"},
        }
        with (
            mock.patch.object(
                archive,
                "get_file_meta",
                return_value={"title": "2032-07-30 - 科技 - 标的观点.md", "create_time": "1780000000"},
            ),
            mock.patch.object(archive, "download_drive_file_version", return_value=content),
            mock.patch.object(archive, "list_bitable_fields", return_value=fields),
            mock.patch.object(
                archive,
                "build_record_fields",
                return_value=({"待审核MD链接": {"link": "https://example.feishu.cn/file/review-token"}, "文件Token": "review-token"}, []),
            ),
            mock.patch.object(
                archive,
                "create_bitable_record_reconciled",
                return_value="rec-structured-v3",
            ) as create,
        ):
            archive.process_file_created_event(cfg, event)

        created_fields = create.call_args.args[1]
        self.assertEqual(created_fields["源纪要记录"], "recv-source-1")
        self.assertEqual(created_fields["观点数"], 36)

    def test_structured_frontmatter_rejects_unknown_schema_version(self):
        with self.assertRaisesRegex(ValueError, "contract is unsupported"):
            archive.parse_structured_frontmatter(
                b"""---
artifact_stage: "structured_review_md"
schema_version: 4
source_record_id: "recv-source-1"
source_archive_url: "https://example.feishu.cn/file/source-token"
source_file_name: "2032-07-30 - source.md"
viewpoint_count: 1
---
"""
            )

    def test_structured_frontmatter_rejects_incomplete_metadata(self):
        with self.assertRaisesRegex(ValueError, "missing"):
            archive.parse_structured_frontmatter(
                b"---\nartifact_stage: \"structured_review_md\"\nschema_version: 2\n---\n"
            )

    def test_structured_metadata_requires_meeting_type_in_source_filename(self):
        with self.assertRaisesRegex(ValueError, "supported meeting naming contract"):
            archive.source_meeting_type("2032-07-13.md")

    def test_structured_metadata_requires_configured_select_options(self):
        field = {
            "field_id": "fld-series",
            "field_name": "会议系列",
            "type": archive.TYPE_SINGLE_SELECT,
            "property": {"options": []},
        }
        with self.assertRaisesRegex(ValueError, "no configured options"):
            archive.require_known_select_value(field, "吴老师")

    def test_structured_metadata_rejects_unknown_select_value(self):
        field = {
            "field_id": "fld-series",
            "field_name": "会议系列",
            "type": archive.TYPE_SINGLE_SELECT,
            "property": {"options": [{"name": "陈老师"}]},
        }
        with self.assertRaisesRegex(ValueError, "not configured"):
            archive.require_known_select_value(field, "吴老师")

    def test_structured_metadata_failure_never_creates_partial_record(self):
        cfg = archive_config(structured_metadata_enabled=True, dry_run=False)
        event = {
            "header": {"event_id": "evt-structured", "create_time": "1780000000000"},
            "event": {"folder_token": "source", "file_token": "review-token", "file_type": "file"},
        }
        with (
            mock.patch.object(
                archive,
                "get_file_meta",
                return_value={"title": "2032-07-13 - 吴老师 - 标的观点.md", "create_time": "1780000000"},
            ),
            mock.patch.object(archive, "list_bitable_fields", return_value=[]),
            mock.patch.object(archive, "build_record_fields", return_value=({"待审核MD链接": {"link": "url"}}, [])),
            mock.patch.object(archive, "download_drive_file_version", return_value=b"invalid"),
            mock.patch.object(
                archive,
                "enrich_structured_record_fields",
                side_effect=ValueError("invalid structured metadata"),
            ),
            mock.patch.object(archive, "create_bitable_record") as create,
        ):
            with self.assertRaisesRegex(ValueError, "invalid structured metadata"):
                archive.process_file_created_event(cfg, event)

        create.assert_not_called()

    def test_router_retries_transient_api_failure(self):
        with (
            mock.patch.object(
                archive,
                "process_router_event",
                side_effect=[archive.FeishuApiError("temporary"), None],
            ) as process,
            mock.patch.object(archive.time, "sleep") as sleep,
        ):
            archive.process_router_event_with_retry([], {"header": {}, "event": {}})

        self.assertEqual(process.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_router_does_not_retry_contract_failure(self):
        with (
            mock.patch.object(
                archive,
                "process_router_event",
                side_effect=ValueError("invalid structured metadata"),
            ) as process,
            mock.patch.object(archive.time, "sleep") as sleep,
        ):
            with self.assertRaisesRegex(ValueError, "invalid structured metadata"):
                archive.process_router_event_with_retry([], {"header": {}, "event": {}})

        process.assert_called_once()
        sleep.assert_not_called()

    def test_archive_uploads_locked_latest_bytes_and_compares_hash(self):
        cfg = archive_config()
        baseline = b"before\n"
        approved = b"after\n"
        fields = {
            "文档链接": {"link": "https://example.feishu.cn/file/source-token"},
            "上传时间": 1780000000000,
            "文件名": "review.md",
            "已审核": True,
            "归档状态": "待归档",
        }
        with (
            mock.patch.object(archive, "get_bitable_record", return_value={"fields": fields}),
            mock.patch.object(archive, "get_file_meta", return_value={"title": "review.md"}),
            mock.patch.object(
                archive,
                "capture_baseline_for_record",
                return_value={"status": "baseline_captured", "sha256": archive.sha256_hex(baseline)},
            ),
            mock.patch.object(archive, "ensure_child_folder", return_value="archive-month"),
            mock.patch.object(
                archive,
                "latest_file_version",
                return_value=({"version": "1002", "tag": 2}, approved),
            ),
            mock.patch.object(
                archive,
                "upload_version_artifact",
                return_value=("approved-token", "https://example.feishu.cn/file/approved-token"),
            ) as upload,
            mock.patch.object(archive, "copy_drive_file") as copy_file,
            mock.patch.object(archive, "update_bitable_record") as update,
        ):
            result = archive.archive_record(cfg, "rec1")

        self.assertEqual(result["version_diff"], archive.VERSION_DIFF_CHANGED)
        upload.assert_called_once_with(cfg, "archive-month", "review.md", approved)
        copy_file.assert_not_called()
        final_fields = update.call_args_list[-1].args[2]
        self.assertEqual(final_fields[archive.FIELD_VERSION_STATUS], archive.VERSION_STATUS_COMPLETE)
        self.assertEqual(final_fields[archive.FIELD_APPROVED_VERSION], "1002")
        self.assertEqual(final_fields[archive.FIELD_APPROVED_SHA256], archive.sha256_hex(approved))

    def test_enforcement_blocks_approval_without_baseline_hash(self):
        cfg = archive_config()
        fields = {
            "文档链接": {"link": "https://example.feishu.cn/file/source-token"},
            "上传时间": 1780000000000,
            "文件名": "review.md",
            "已审核": True,
            "归档状态": "待归档",
        }
        with (
            mock.patch.object(archive, "get_bitable_record", return_value={"fields": fields}),
            mock.patch.object(archive, "get_file_meta", return_value={"title": "review.md"}),
            mock.patch.object(archive, "capture_baseline_for_record", return_value={"status": "skipped"}),
        ):
            with self.assertRaisesRegex(ValueError, "baseline"):
                archive.archive_record(cfg, "rec1")

    def test_migrate_archived_record_matches_historical_source_version_by_archive_hash(self):
        cfg = archive_config()
        fields = {
            "文档链接": {"link": "https://example.feishu.cn/file/source-token"},
            "归档链接": {"link": "https://example.feishu.cn/file/archive-token"},
            "上传时间": 1780000000000,
            "归档时间": 3000,
            "文件名": "review.md",
        }
        versions = [
            {"version": "1001", "tag": 1, "edited_at": "1000"},
            {"version": "1002", "tag": 2, "edited_at": "2000"},
            {"version": "1003", "tag": 3, "edited_at": "4000"},
        ]
        contents = {
            ("source-token", "1001"): b"before\n",
            ("source-token", "1002"): b"approved\n",
            ("source-token", "1003"): b"after-archive\n",
            ("archive-token", ""): b"approved\n",
        }

        def download(_cfg, token, version=""):
            return contents[(token, version)]

        with (
            mock.patch.object(archive, "get_bitable_record", return_value={"fields": fields}),
            mock.patch.object(archive, "list_drive_file_versions", return_value=versions),
            mock.patch.object(archive, "download_drive_file_version", side_effect=download),
            mock.patch.object(archive, "get_file_meta", return_value={"title": "review.md"}),
            mock.patch.object(archive, "ensure_version_baseline_folder", return_value="baseline-folder"),
            mock.patch.object(
                archive,
                "upload_version_artifact",
                return_value=("baseline-token", "https://example.feishu.cn/file/baseline-token"),
            ),
            mock.patch.object(archive, "update_bitable_record") as update,
        ):
            result = archive.migrate_archived_record(cfg, "rec1")

        self.assertEqual(result["approved_version"], "1002")
        self.assertEqual(result["version_diff"], archive.VERSION_DIFF_CHANGED)
        final_fields = update.call_args.args[2]
        self.assertEqual(final_fields[archive.FIELD_BASELINE_VERSION], "1001")
        self.assertEqual(final_fields[archive.FIELD_APPROVED_VERSION], "1002")
        self.assertEqual(final_fields[archive.FIELD_APPROVED_SHA256], archive.sha256_hex(b"approved\n"))

    def test_migrate_archived_record_refuses_legacy_blank_template(self):
        cfg = archive_config()
        fields = {
            "文档链接": {"link": "https://example.feishu.cn/file/source-token"},
            "归档链接": {"link": "https://example.feishu.cn/file/archive-token"},
            "上传时间": 1780000000000,
            "归档时间": 3000,
        }
        versions = [
            {"version": "1001", "tag": 1, "edited_at": "1000"},
            {"version": "1002", "tag": 2, "edited_at": "2000"},
        ]
        with (
            mock.patch.object(archive, "get_bitable_record", return_value={"fields": fields}),
            mock.patch.object(archive, "list_drive_file_versions", return_value=versions),
            mock.patch.object(archive, "download_drive_file_version", return_value=b"\n"),
        ):
            with self.assertRaisesRegex(ValueError, "blank-template"):
                archive.migrate_archived_record(cfg, "rec1")

    def test_archive_failure_writes_specific_version_error(self):
        cfg = archive_config()
        with (
            mock.patch.object(archive, "archive_record", side_effect=ValueError("missing approved version")),
            mock.patch.object(
                archive,
                "get_bitable_record",
                return_value={"fields": {"归档状态": "归档中", "归档链接": ""}},
            ),
            mock.patch.object(archive, "update_bitable_record") as update,
        ):
            with self.assertRaisesRegex(ValueError, "missing approved version"):
                archive.archive_record_with_failure_status(cfg, "rec1")

        failure_fields = update.call_args.args[2]
        self.assertEqual(failure_fields[archive.FIELD_VERSION_ERROR], "invalid_input")
        self.assertEqual(failure_fields[archive.FIELD_VERSION_STATUS], archive.VERSION_STATUS_FAILED)

    def test_archive_failure_does_not_downgrade_complete_terminal_state(self):
        cfg = archive_config()
        terminal_fields = {
            "归档状态": "已归档",
            "归档链接": {"link": "https://example.test/file/archive"},
            archive.FIELD_VERSION_STATUS: archive.VERSION_STATUS_COMPLETE,
            archive.FIELD_BASELINE_LINK: {"link": "https://example.test/file/baseline"},
            archive.FIELD_BASELINE_VERSION: "v1",
            archive.FIELD_BASELINE_SHA256: "a" * 64,
            archive.FIELD_APPROVED_VERSION: "v2",
            archive.FIELD_APPROVED_SHA256: "b" * 64,
        }
        with (
            mock.patch.object(archive, "archive_record", side_effect=ValueError("response lost")),
            mock.patch.object(archive, "get_bitable_record", return_value={"fields": terminal_fields}),
            mock.patch.object(archive, "update_bitable_record") as update,
        ):
            result = archive.archive_record_with_failure_status(cfg, "rec1")
        self.assertEqual(result["status"], "archive_reconciled")
        update.assert_not_called()


class StructuredBoundaryTests(unittest.TestCase):
    def setUp(self):
        self._locks = tempfile.TemporaryDirectory()
        self.addCleanup(self._locks.cleanup)
        self.lock_job_dir = Path(self._locks.name) / "jobs"

    def test_generate_for_record_queues_after_all_source_gates(self):
        cfg = SimpleNamespace(
            source_base_token="base",
            source_table_id="table",
            source_version_retention_enforce=True,
            review_field_names=("已审核",),
            max_error_chars=500,
            semantic_job_dir=self.lock_job_dir,
        )
        archive_url = "https://example.feishu.cn/file/approved"
        record = {
            "fields": {
                "已审核": True,
                structured.FIELD_ARCHIVE_STATUS: "已归档",
                structured.FIELD_VERSION_STATUS: "已完成",
                structured.FIELD_SOURCE_ARCHIVE_LINK: {"link": archive_url},
                structured.FIELD_APPROVED_SHA256: "approved-sha",
            }
        }
        with (
            mock.patch.object(structured, "list_bitable_fields", return_value=[]),
            mock.patch.object(structured, "source_field_issues", return_value=[]),
            mock.patch.object(structured, "get_bitable_record", return_value=record),
            mock.patch.object(
                structured,
                "create_generation_job",
                return_value={"ok": True, "status": "queued", "record_id": "rec1", "job_id": "job1"},
            ) as queue,
            mock.patch.object(structured, "update_source_status") as update,
        ):
            status, payload = structured.generate_for_record(cfg, "rec1")

        self.assertEqual(status, 202)
        self.assertEqual(payload["status"], "queued")
        queue.assert_called_once_with(cfg, "rec1", record["fields"], archive_url)
        self.assertEqual(update.call_args.args[2], structured.STATUS_RUNNING)

    def test_source_structured_generation_reads_only_approved_archive(self):
        markdown_bytes = b"# approved meeting archive\n"
        archive_url = "https://example.feishu.cn/file/approved-meeting"
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        cfg = SimpleNamespace(
            source_version_retention_enforce=True,
            semantic_job_dir=Path(tempdir.name) / "jobs",
        )
        fields = {
            structured.FIELD_APPROVED_SHA256: structured.hashlib.sha256(markdown_bytes).hexdigest(),
            structured.FIELD_FILE_NAME: "2032-07-10 - pilot.md",
            structured.FIELD_MEETING_UID: MEETING_UID,
        }

        with (
            mock.patch.object(structured, "parse_drive_url", return_value=("approved-meeting", "file")) as parse_url,
            mock.patch.object(structured, "get_file_meta", return_value={"name": "2032-07-10 - pilot.md"}),
            mock.patch.object(structured, "download_drive_file", return_value=markdown_bytes) as download,
            mock.patch.object(structured, "resolve_meeting_date", return_value="2032-07-10"),
            mock.patch.object(structured, "resolve_meeting_series", return_value="pilot"),
        ):
            result = structured.create_generation_job(cfg, "rec1", fields, archive_url)

        self.assertEqual(result["status"], "queued")
        parse_url.assert_called_once_with(archive_url)
        download.assert_called_once_with(cfg, "approved-meeting")
        job_dir = cfg.semantic_job_dir / "pending" / result["job_id"]
        self.assertEqual((job_dir / "source.md").read_bytes(), markdown_bytes)
        context = json.loads((job_dir / "context.json").read_text(encoding="utf-8"))
        self.assertEqual(context["source_archive_url"], archive_url)
        self.assertEqual(context["approved_sha256"], hashlib.sha256(markdown_bytes).hexdigest())

    def test_source_structured_generation_rejects_archive_hash_mismatch(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        cfg = SimpleNamespace(
            source_version_retention_enforce=True,
            semantic_job_dir=Path(tempdir.name) / "jobs",
        )
        fields = {
            structured.FIELD_APPROVED_SHA256: "wrong-approved-sha256",
            structured.FIELD_FILE_NAME: "2032-07-10 - pilot.md",
            structured.FIELD_MEETING_UID: MEETING_UID,
        }

        with (
            mock.patch.object(structured, "parse_drive_url", return_value=("approved-meeting", "file")),
            mock.patch.object(structured, "get_file_meta", return_value={"name": "2032-07-10 - pilot.md"}),
            mock.patch.object(structured, "download_drive_file", return_value=b"tampered archive"),
        ):
            with self.assertRaisesRegex(structured.StructuredError, "归档文件与审核后内容哈希不一致"):
                structured.create_generation_job(
                    cfg,
                    "rec1",
                    fields,
                    "https://example.feishu.cn/file/approved-meeting",
                )

        self.assertFalse((cfg.semantic_job_dir / "pending").exists())

    def test_current_skill_is_called_with_semantic_contract_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "source.md"
            claims = root / "claim_units.json"
            output = root / "structured.md"
            source.write_text("# meeting\n", encoding="utf-8")
            claims.write_text("[]\n", encoding="utf-8")
            skill_script = root / "generate_table.py"
            skill_script.write_text("# placeholder\n", encoding="utf-8")
            cfg = SimpleNamespace(
                skill_script=skill_script,
                skill_script_sha256=hashlib.sha256(skill_script.read_bytes()).hexdigest(),
                skill_contract_version=7,
                max_error_chars=500,
            )

            def run_command(cmd, **_kwargs):
                output.write_text("---\nviewpoint_count: 0\n---\n# 观点事实审阅表\n", encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with mock.patch.object(structured.subprocess, "run", side_effect=run_command) as run:
                row_count = structured.run_skill(
                    cfg,
                    source_markdown_path=source,
                    claim_units_path=claims,
                    output_path=output,
                    source_record_id="rec1",
                    source_archive_url="https://example.feishu.cn/file/archive",
                    source_file_name="meeting.md",
                    meeting_date="2032-07-10",
                    model_version="codex-local-agent",
                    meeting_uid=MEETING_UID,
                )

        cmd = run.call_args.args[0]
        self.assertEqual(row_count, 0)
        self.assertIn("--claim-units", cmd)
        self.assertNotIn("--semantic-rows", cmd)
        self.assertNotIn("--json-output", cmd)
        self.assertIn("--model-version", cmd)
        self.assertIn("--meeting-uid", cmd)
        self.assertEqual(cmd[cmd.index("--meeting-uid") + 1], MEETING_UID)
        self.assertEqual(cmd[cmd.index("--schema-version") + 1], "7")

    def test_complete_job_rechecks_current_archive_hash_before_skill(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "jobs"
            job_id = "rec1-approved-a01"
            job_dir = root / "processing" / job_id
            job_dir.mkdir(parents=True)
            archive_url = "https://example.feishu.cn/file/approved"
            approved_bytes = b"approved archive"
            approved_hash = hashlib.sha256(approved_bytes).hexdigest()
            (job_dir / "source.md").write_bytes(approved_bytes)
            (job_dir / "context.json").write_text(
                json.dumps(
                    {
                        "job_id": job_id,
                        "record_id": "rec1",
                        "source_archive_url": archive_url,
                        "approved_sha256": approved_hash,
                    }
                ),
                encoding="utf-8",
            )
            cfg = SimpleNamespace(
                source_base_token="base",
                source_table_id="table",
                source_version_retention_enforce=True,
                review_field_names=("已审核",),
                semantic_job_dir=root,
                max_error_chars=500,
            )
            fields = {
                "已审核": True,
                structured.FIELD_ARCHIVE_STATUS: "已归档",
                structured.FIELD_VERSION_STATUS: "已完成",
                structured.FIELD_SOURCE_ARCHIVE_LINK: {"link": archive_url},
                structured.FIELD_APPROVED_SHA256: approved_hash,
                structured.FIELD_FILE_NAME: "meeting.md",
            }
            run_skill = mock.Mock()
            with (
                mock.patch.object(structured, "list_bitable_fields", return_value=[]),
                mock.patch.object(structured, "source_field_issues", return_value=[]),
                mock.patch.object(structured, "get_bitable_record", return_value={"fields": fields}),
                mock.patch.object(structured, "parse_drive_url", return_value=("approved", "file")),
                mock.patch.object(structured, "get_file_meta", return_value={"name": "meeting.md"}),
                mock.patch.object(structured, "download_drive_file", return_value=b"changed archive"),
                mock.patch.object(structured, "run_skill", run_skill),
            ):
                with self.assertRaisesRegex(structured.StructuredError, "归档文件与审核后内容哈希不一致"):
                    structured.complete_generation_job(cfg, job_id)

        run_skill.assert_not_called()

    def test_official_json_waits_for_completed_version_retention(self):
        cfg = mock.Mock()
        cfg.structured_base_token = "base"
        cfg.structured_table_id = "table"
        cfg.structured_version_retention_enforce = True
        cfg.semantic_job_dir = self.lock_job_dir
        record = {
            "fields": {
                structured.FIELD_STRUCTURED_APPROVED: True,
                structured.FIELD_ARCHIVE_STATUS: "已归档",
                structured.FIELD_VERSION_STATUS: "基线已留存",
                structured.FIELD_STRUCTURED_ARCHIVE_LINK: {"link": "https://example.feishu.cn/file/approved"},
                structured.FIELD_MEETING_UID: MEETING_UID,
            }
        }
        with (
            mock.patch.object(structured, "list_bitable_fields", return_value=[]),
            mock.patch.object(structured, "structured_md_field_issues", return_value=[]),
            mock.patch.object(structured, "get_bitable_record_from", return_value=record),
        ):
            status, payload = structured.generate_official_json_for_record(cfg, "rec1")

        self.assertEqual(status, 409)
        self.assertEqual(payload["reason"], "version_retention_not_done")

    def test_closed_flag_fails_closed_before_reading_mutable_draft(self):
        cfg = mock.Mock()
        cfg.structured_base_token = "base"
        cfg.structured_table_id = "table"
        cfg.structured_version_retention_enforce = False
        record = {
            "fields": {
                structured.FIELD_STRUCTURED_APPROVED: True,
                structured.FIELD_STRUCTURED_MD_LINK: {"link": "https://example.feishu.cn/file/review"},
                structured.FIELD_STRUCTURED_CURRENT_MD_HASH: "same-hash",
                structured.FIELD_STRUCTURED_JSON_SOURCE_MD_HASH: "same-hash",
                structured.FIELD_STRUCTURED_JSON_LINK: {"link": "https://example.feishu.cn/file/json"},
                structured.FIELD_STRUCTURED_NEEDS_JSON_REGEN: False,
                structured.FIELD_MEETING_UID: MEETING_UID,
            }
        }
        with (
            mock.patch.object(structured, "list_bitable_fields", return_value=[]),
            mock.patch.object(structured, "structured_md_field_issues", return_value=[]),
            mock.patch.object(structured, "get_bitable_record_from", return_value=record),
        ):
            status, payload = structured.generate_official_json_for_record(cfg, "rec1")

        self.assertEqual(status, 500)
        self.assertEqual(payload["error_code"], "version_retention_required")

    def test_enforced_official_json_separates_content_sha256_from_semantic_hash(self):
        markdown_bytes = b"# approved archive\n"
        approved_sha256 = structured.hashlib.sha256(markdown_bytes).hexdigest()
        semantic_hash = "semantic-review-fields-hash"
        record = {
            "fields": {
                structured.FIELD_STRUCTURED_APPROVED: True,
                structured.FIELD_ARCHIVE_STATUS: "已归档",
                structured.FIELD_VERSION_STATUS: "已完成",
                structured.FIELD_STRUCTURED_ARCHIVE_LINK: {"link": "https://example.feishu.cn/file/approved"},
                structured.FIELD_APPROVED_SHA256: approved_sha256,
                structured.FIELD_STRUCTURED_TABLE_NAME: "2032-07-10 - pilot - 结构化表格",
                structured.FIELD_STRUCTURED_VIEWPOINT_COUNT: 1,
                structured.FIELD_STRUCTURED_NEEDS_JSON_REGEN: False,
                structured.FIELD_MEETING_UID: MEETING_UID,
            }
        }
        status_updates = []

        def prepare(_cfg, *, output_dir, meeting_uid, **_kwargs):
            output_dir.mkdir(parents=True, exist_ok=True)
            json_path = output_dir / "pilot.json"
            json_path.write_text('{"metadata":{},"rows":[]}', encoding="utf-8")
            return {
                "json_path": str(json_path),
                "source_md_hash": semantic_hash,
                "row_count": 1,
                "meeting_uid": meeting_uid,
                "schema_version": 7,
                "security_master_version": "sha256:" + structured.file_sha256(cfg.security_master_path),
            }

        def update_status(*_args, **kwargs):
            status_updates.append(kwargs)

        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = mock.Mock()
            cfg.structured_base_token = "base"
            cfg.structured_table_id = "table"
            cfg.official_json_table_id = "official"
            cfg.structured_version_retention_enforce = True
            cfg.max_error_chars = 500
            cfg.output_dir = Path(tmpdir)
            cfg.semantic_job_dir = self.lock_job_dir
            cfg.official_json_prepare_script_sha256 = "1" * 64
            cfg.skill_contract_version = 7
            cfg.security_master_path = Path(tmpdir) / "security-master.csv"
            cfg.security_master_path.write_text("code,name\n000001,星河银行\n", encoding="utf-8")
            with (
                mock.patch.object(structured, "list_bitable_fields", return_value=[]),
                mock.patch.object(structured, "structured_md_field_issues", return_value=[]),
                mock.patch.object(structured, "get_bitable_record_from", return_value=record),
                mock.patch.object(structured, "resolve_bitable_table_id", return_value="official"),
                mock.patch.object(structured, "official_json_field_issues", return_value=[]),
                mock.patch.object(structured, "update_structured_json_status", side_effect=update_status),
                mock.patch.object(structured, "parse_drive_url", return_value=("approved", "file")),
                mock.patch.object(structured, "get_file_meta", return_value={"name": "2032-07-10 - pilot - 结构化表格.md"}),
                mock.patch.object(structured, "download_drive_file", return_value=markdown_bytes),
                mock.patch.object(structured, "run_prepare_official_json", side_effect=prepare),
                mock.patch.object(structured, "ensure_official_json_month_folder", return_value="folder"),
                mock.patch.object(structured, "list_drive_folder_items", return_value=[]),
                mock.patch.object(structured, "save_local_backup", return_value=Path(tmpdir) / "backup.json"),
                mock.patch.object(structured, "upload_drive_file", return_value="json-token"),
                mock.patch.object(structured, "resolve_uploaded_file_url", return_value="https://example.feishu.cn/file/json"),
                mock.patch.object(structured, "find_existing_official_json_record", return_value=""),
                mock.patch.object(structured, "write_official_json_artifact_record", return_value="official-rec"),
                mock.patch.object(structured, "cleanup_superseded_official_json_files", return_value=0),
            ):
                status, payload = structured.generate_official_json_for_record(cfg, "rec1")

        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "generated")
        self.assertEqual(payload["source_md_hash"], semantic_hash)
        generated = next(item for item in status_updates if item["status"] == structured.STATUS_GENERATED)
        self.assertEqual(generated["current_hash"], semantic_hash)
        self.assertNotEqual(approved_sha256, semantic_hash)

    def test_enforced_official_json_never_bypasses_archive_content_hash(self):
        record = {
            "fields": {
                structured.FIELD_STRUCTURED_APPROVED: True,
                structured.FIELD_ARCHIVE_STATUS: "已归档",
                structured.FIELD_VERSION_STATUS: "已完成",
                structured.FIELD_STRUCTURED_ARCHIVE_LINK: {"link": "https://example.feishu.cn/file/approved"},
                structured.FIELD_APPROVED_SHA256: "wrong-approved-sha256",
                structured.FIELD_STRUCTURED_TABLE_NAME: "2032-07-10 - pilot - 结构化表格",
                structured.FIELD_STRUCTURED_NEEDS_JSON_REGEN: True,
                structured.FIELD_MEETING_UID: MEETING_UID,
            }
        }
        cfg = mock.Mock()
        cfg.structured_base_token = "base"
        cfg.structured_table_id = "table"
        cfg.official_json_table_id = "official"
        cfg.structured_version_retention_enforce = True
        cfg.max_error_chars = 500
        cfg.semantic_job_dir = self.lock_job_dir
        prepare = mock.Mock()
        upload = mock.Mock()
        with (
            mock.patch.object(structured, "list_bitable_fields", return_value=[]),
            mock.patch.object(structured, "structured_md_field_issues", return_value=[]),
            mock.patch.object(structured, "get_bitable_record_from", return_value=record),
            mock.patch.object(structured, "resolve_bitable_table_id", return_value="official"),
            mock.patch.object(structured, "official_json_field_issues", return_value=[]),
            mock.patch.object(structured, "update_structured_json_status"),
            mock.patch.object(structured, "parse_drive_url", return_value=("approved", "file")),
            mock.patch.object(structured, "get_file_meta", return_value={"name": "2032-07-10 - pilot - 结构化表格.md"}),
            mock.patch.object(structured, "download_drive_file", return_value=b"actual approved archive"),
            mock.patch.object(structured, "run_prepare_official_json", prepare),
            mock.patch.object(structured, "upload_drive_file", upload),
        ):
            status, payload = structured.generate_official_json_for_record(cfg, "rec1")

        self.assertEqual(status, 409)
        self.assertEqual(payload["reason"], "approved_content_hash_missing")
        prepare.assert_not_called()
        upload.assert_not_called()


if __name__ == "__main__":
    unittest.main()
