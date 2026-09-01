from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

import structured_generate_service as service

MEETING_UID = "mtg_550e8400e29b41d4a716446655440000"


class OfficialJsonFailureSafetyTests(unittest.TestCase):
    def run_generation(
        self,
        *,
        source_commit_fails: bool,
        source_commit_response_lost: bool = False,
        current_hash: str = "new-hash",
        prepared_hash: str = "",
        force_regeneration: bool = True,
    ):
        events: list[str] = []
        source_update_calls: list[dict[str, object]] = []
        approved_content = "# 标的观点审阅表\n\n- 会议日期：2032-07-08\n".encode("utf-8")
        prepared_hash = prepared_hash or service.hashlib.sha256(approved_content).hexdigest()
        source_fields = {
            service.FIELD_STRUCTURED_APPROVED: True,
            service.FIELD_STRUCTURED_MD_LINK: "https://example.test/file/source-md",
            service.FIELD_STRUCTURED_ARCHIVE_LINK: "https://example.test/file/source-md",
            service.FIELD_ARCHIVE_STATUS: "已归档",
            service.FIELD_VERSION_STATUS: "已完成",
            service.FIELD_APPROVED_SHA256: service.hashlib.sha256(approved_content).hexdigest(),
            service.FIELD_STRUCTURED_CURRENT_MD_HASH: current_hash,
            service.FIELD_STRUCTURED_JSON_SOURCE_MD_HASH: "old-hash",
            service.FIELD_STRUCTURED_JSON_LINK: "https://example.test/file/old-json",
            service.FIELD_STRUCTURED_NEEDS_JSON_REGEN: force_regeneration,
            service.FIELD_STRUCTURED_VIEWPOINT_COUNT: 1,
            service.FIELD_STRUCTURED_TABLE_NAME: "2032-07-08 - test",
            service.FIELD_MEETING_UID: MEETING_UID,
        }
        old_official_fields = {
            service.FIELD_OFFICIAL_JSON_FILE: "old.json",
            service.FIELD_OFFICIAL_SOURCE_MD_RECORD: [{"id": "source-record"}],
            service.FIELD_OFFICIAL_SOURCE_MD_LINK: "https://example.test/file/source-md",
            service.FIELD_OFFICIAL_SOURCE_MD_HASH: "old-hash",
            service.FIELD_OFFICIAL_JSON_LINK: "https://example.test/file/old-json",
            service.FIELD_OFFICIAL_JSON_ROW_COUNT: 1,
            service.FIELD_OFFICIAL_STATUS: [service.STATUS_GENERATED],
            service.FIELD_OFFICIAL_GENERATED_AT: 1783640000000,
            service.FIELD_OFFICIAL_SOURCE_BASE_STATUS: "已审核",
            service.FIELD_OFFICIAL_ERROR: "",
            service.FIELD_MEETING_UID: MEETING_UID,
        }
        structured_schema: list[dict[str, object]] = []
        official_schema: list[dict[str, object]] = []

        def list_fields(_cfg, _base_token, table_id):
            return structured_schema if table_id == "source-table" else official_schema

        def get_record(_cfg, _base_token, table_id, _record_id):
            if table_id == "source-table":
                return {"fields": source_fields}
            return {"fields": old_official_fields}

        def prepare_json(_cfg, *, output_dir, **_kwargs):
            output_dir.mkdir(parents=True, exist_ok=True)
            json_path = output_dir / "prepared.json"
            json_path.write_text('{"metadata":{},"rows":[]}', encoding="utf-8")
            return {
                "json_path": str(json_path),
                "source_md_hash": prepared_hash,
                "row_count": 1,
                "meeting_uid": MEETING_UID,
                "schema_version": 9,
                "security_master_version": "sha256:" + service.hashlib.sha256(b"master").hexdigest(),
            }

        def save_backup(*_args, **_kwargs):
            events.append("backup")
            return Path("/tmp/official-json-backup.json")

        def upload(*_args, **_kwargs):
            events.append("upload")
            return "new-file-token"

        def write_official(*_args, **_kwargs):
            events.append("official")
            old_official_fields.update(
                {
                    service.FIELD_OFFICIAL_JSON_FILE: _kwargs["json_file_name"],
                    service.FIELD_OFFICIAL_SOURCE_MD_HASH: _kwargs["source_md_hash"],
                    service.FIELD_OFFICIAL_JSON_LINK: _kwargs["json_url"],
                    service.FIELD_OFFICIAL_JSON_ROW_COUNT: _kwargs["row_count"],
                    service.FIELD_OFFICIAL_STATUS: service.STATUS_GENERATED,
                    service.FIELD_OFFICIAL_ERROR: "",
                    service.FIELD_MEETING_UID: _kwargs["meeting_uid"],
                }
            )
            return "official-record"

        def update_source(*_args, status, **_kwargs):
            source_update_calls.append({"status": status, **_kwargs})
            if status == service.STATUS_GENERATED:
                events.append("source_generated")
                if source_commit_response_lost:
                    source_fields.update(
                        {
                            service.FIELD_STRUCTURED_JSON_STATUS: service.STATUS_GENERATED,
                            service.FIELD_STRUCTURED_CURRENT_MD_HASH: _kwargs["current_hash"],
                            service.FIELD_STRUCTURED_JSON_SOURCE_MD_HASH: _kwargs["source_md_hash"],
                            service.FIELD_STRUCTURED_JSON_LINK: _kwargs["json_url"],
                            service.FIELD_STRUCTURED_JSON_ROW_COUNT: _kwargs["row_count"],
                            service.FIELD_STRUCTURED_NEEDS_JSON_REGEN: False,
                        }
                    )
                    raise RuntimeError("response lost after source commit")
                if source_commit_fails:
                    raise RuntimeError("source commit failed")
            elif status == service.STATUS_FAILED:
                events.append("source_failed")

        upload_mock = mock.Mock(side_effect=upload)
        restore_mock = mock.Mock()
        mark_failed_mock = mock.Mock()

        with tempfile.TemporaryDirectory() as tmpdir:
            security_master = Path(tmpdir) / "security_master.csv"
            security_master.write_bytes(b"master")
            cfg = SimpleNamespace(
                structured_base_token="base-token",
                structured_table_id="source-table",
                official_json_table_id="official-table",
                structured_version_retention_enforce=True,
                max_error_chars=500,
                output_dir=Path(tmpdir),
                semantic_job_dir=Path(tmpdir) / "jobs",
                skill_json_script_sha256="1" * 64,
                skill_contract_version=9,
                security_master_path=security_master,
            )
            with mock.patch.multiple(
                service,
                list_bitable_fields=mock.Mock(side_effect=list_fields),
                structured_md_field_issues=mock.Mock(return_value=[]),
                get_bitable_record_from=mock.Mock(side_effect=get_record),
                resolve_bitable_table_id=mock.Mock(return_value="official-table"),
                official_json_field_issues=mock.Mock(return_value=[]),
                update_structured_json_status=mock.Mock(side_effect=update_source),
                parse_drive_url=mock.Mock(return_value=("source-md", "file")),
                get_file_meta=mock.Mock(return_value={"name": "2032-07-08 - test - 标的观点.md"}),
                download_drive_file=mock.Mock(return_value=approved_content),
                run_generate_structured_json=mock.Mock(side_effect=prepare_json),
                ensure_official_json_month_folder=mock.Mock(return_value="official-folder"),
                list_drive_folder_items=mock.Mock(
                    return_value=[{"name": "2032-07-08 - test - 标的观点.json"}]
                ),
                save_local_backup=mock.Mock(side_effect=save_backup),
                upload_drive_file=upload_mock,
                resolve_uploaded_file_url=mock.Mock(return_value="https://example.test/file/new-json"),
                cleanup_superseded_official_json_files=mock.Mock(return_value=1),
                find_existing_official_json_record=mock.Mock(return_value="official-record"),
                write_official_json_artifact_record=mock.Mock(side_effect=write_official),
                restore_official_json_artifact_record=restore_mock,
                mark_official_json_artifact_failed=mark_failed_mock,
            ):
                result = service.generate_official_json_for_record(cfg, "source-record")

        return result, events, upload_mock, restore_mock, mark_failed_mock, source_update_calls

    def test_success_uploads_new_version_before_committing_source_hash(self) -> None:
        (status, payload), events, upload_mock, restore_mock, _, _ = self.run_generation(
            source_commit_fails=False
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "generated")
        self.assertEqual(events, ["backup", "upload", "official", "source_generated"])
        upload_name = upload_mock.call_args.args[2]
        self.assertNotEqual(upload_name, "2032-07-08 - test - 标的观点.json")
        self.assertNotIn("file_token", upload_mock.call_args.kwargs)
        restore_mock.assert_not_called()

    def test_source_commit_failure_restores_official_record_and_preserves_old_file(self) -> None:
        (status, payload), events, upload_mock, restore_mock, _, _ = self.run_generation(
            source_commit_fails=True
        )

        self.assertEqual(status, 500)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(
            events,
            ["backup", "upload", "official", "source_generated", "source_failed"],
        )
        self.assertNotIn("file_token", upload_mock.call_args.kwargs)
        restore_mock.assert_called_once()

    def test_lost_source_commit_response_reconciles_complete_remote_terminal(self) -> None:
        (status, payload), events, _, restore_mock, mark_failed_mock, _ = self.run_generation(
            source_commit_fails=False,
            source_commit_response_lost=True,
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "generated_reconciled")
        self.assertEqual(events, ["backup", "upload", "official", "source_generated"])
        restore_mock.assert_not_called()
        mark_failed_mock.assert_not_called()

    def test_explicit_retry_accepts_current_markdown_hash_and_commits_it(self) -> None:
        (status, payload), _, _, _, _, source_updates = self.run_generation(
            source_commit_fails=False,
            current_hash="stale-current-hash",
            force_regeneration=True,
        )

        self.assertEqual(status, 200)
        expected_hash = service.hashlib.sha256(
            "# 标的观点审阅表\n\n- 会议日期：2032-07-08\n".encode("utf-8")
        ).hexdigest()
        self.assertEqual(payload["source_md_hash"], expected_hash)
        generated = next(call for call in source_updates if call["status"] == service.STATUS_GENERATED)
        self.assertEqual(generated["current_hash"], expected_hash)
        self.assertEqual(generated["source_md_hash"], expected_hash)

    def test_skill_reported_input_hash_mismatch_fails_before_upload(self) -> None:
        (status, payload), events, upload_mock, restore_mock, _, _ = self.run_generation(
            source_commit_fails=False,
            current_hash="stale-current-hash",
            prepared_hash="new-hash",
            force_regeneration=False,
        )

        self.assertEqual(status, 409)
        self.assertEqual(payload["error_code"], "source_md_hash_mismatch")
        self.assertEqual(events, ["source_failed"])
        upload_mock.assert_not_called()
        restore_mock.assert_not_called()

    def test_restore_payload_reconstructs_writable_link_and_url_values(self) -> None:
        cfg = SimpleNamespace(structured_base_token="base-token", max_error_chars=500)
        fields = [
            {"field_name": service.FIELD_OFFICIAL_SOURCE_MD_RECORD, "type": service.TYPE_LINK},
            {"field_name": service.FIELD_OFFICIAL_SOURCE_MD_LINK, "type": service.TYPE_URL},
            {"field_name": service.FIELD_OFFICIAL_JSON_LINK, "type": service.TYPE_URL},
        ]
        snapshot = {
            service.FIELD_MEETING_UID: MEETING_UID,
            service.FIELD_OFFICIAL_JSON_FILE: "old.json",
            service.FIELD_OFFICIAL_SOURCE_MD_LINK: {"text": "source", "link": "https://example.test/source"},
            service.FIELD_OFFICIAL_SOURCE_MD_HASH: "old-hash",
            service.FIELD_OFFICIAL_JSON_LINK: {"text": "old", "link": "https://example.test/old"},
            service.FIELD_OFFICIAL_JSON_ROW_COUNT: 14,
            service.FIELD_OFFICIAL_STATUS: [service.STATUS_GENERATED],
            service.FIELD_OFFICIAL_GENERATED_AT: 1783640000000,
            service.FIELD_OFFICIAL_SOURCE_BASE_STATUS: "已审核",
            service.FIELD_OFFICIAL_ERROR: "",
        }

        with mock.patch.object(service, "update_bitable_record_in") as update_mock:
            service.restore_official_json_artifact_record(
                cfg,
                official_table_id="official-table",
                fields=fields,
                record_id="official-record",
                source_md_record_id="source-record",
                snapshot_fields=snapshot,
            )

        payload = update_mock.call_args.args[4]
        self.assertEqual(payload[service.FIELD_OFFICIAL_SOURCE_MD_RECORD], ["source-record"])
        self.assertEqual(
            payload[service.FIELD_OFFICIAL_JSON_LINK],
            {"text": "old.json", "link": "https://example.test/old"},
        )
        self.assertEqual(payload[service.FIELD_OFFICIAL_SOURCE_MD_HASH], "old-hash")
        self.assertEqual(payload[service.FIELD_OFFICIAL_STATUS], service.STATUS_GENERATED)


if __name__ == "__main__":
    unittest.main()
