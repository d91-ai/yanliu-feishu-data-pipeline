from __future__ import annotations

import http.client
import json
from pathlib import Path
import stat
import tempfile
import threading
import time
import unittest
from unittest import mock

import semantic_worker as worker
import structured_generate_service as service


def make_config(root: Path) -> service.Config:
    return service.Config(
        app_id="app",
        app_secret="secret",
        source_base_token="source-base",
        source_table_id="source-table",
        structured_base_token="structured-base",
        structured_table_id="structured-table",
        official_json_table_id="official-table",
        structured_pending_folder_token="pending",
        structured_archive_folder_token="archive",
        structured_official_json_folder_token="official",
        structured_http_token="http-token",
        skill_script=root / "generate.py",
        skill_script_sha256="0" * 64,
        skill_json_script=root / "generate_table.py",
        skill_json_script_sha256="1" * 64,
        output_dir=root / "outputs",
        folder_registry_path=root / "registry.json",
        skill_contract_version=9,
        security_master_path=root / "security_master.csv",
        semantic_job_dir=root / "jobs",
        structured_baseline_http_url="http://baseline.test/capture-baseline",
        structured_baseline_http_token="baseline-token",
    )


class StructuredServiceSafetyTests(unittest.TestCase):
    def test_structured_review_writeback_creates_idempotent_record_and_baseline(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = make_config(Path(temp_dir))
            create_result = {"data": {"record": {"record_id": "structured-rec"}}}
            create = mock.Mock(return_value=create_result)
            baseline = mock.Mock(return_value={"status": "baseline_captured"})
            with mock.patch.multiple(
                service,
                list_bitable_fields=mock.Mock(return_value=[]),
                structured_md_field_issues=mock.Mock(return_value=[]),
                list_bitable_records=mock.Mock(return_value=[]),
                create_bitable_record_in=create,
                capture_structured_baseline=baseline,
            ):
                result = service.write_structured_review_record(
                    cfg,
                    source_record_id="source-rec",
                    source_fields={service.FIELD_MEETING_TYPE: "多人复盘会"},
                    meeting_uid="mtg_550e8400e29b41d4a716446655440000",
                    source_archive_url="https://example.test/file/source",
                    source_file_name="2032-08-09 - 测试.md",
                    meeting_date="2032-08-09",
                    meeting_series="测试",
                    file_name="2032-08-09 - 测试 - 标的观点.md",
                    file_url="https://example.test/file/review",
                    row_count=12,
                )

        self.assertEqual(result["record_id"], "structured-rec")
        self.assertTrue(result["created"])
        client_token = create.call_args.kwargs["client_token"]
        self.assertEqual(service.uuid.UUID(client_token).version, 4)
        payload = create.call_args.args[3]
        self.assertEqual(payload[service.FIELD_STRUCTURED_VIEWPOINT_COUNT], 12)
        self.assertFalse(payload[service.FIELD_STRUCTURED_APPROVED])
        self.assertEqual(payload[service.FIELD_VERSION_STATUS], service.VERSION_STATUS_PENDING)
        baseline.assert_called_once_with(cfg, "structured-rec")

    def test_structured_review_writeback_reuses_identical_unreviewed_record(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = make_config(Path(temp_dir))
            existing = {
                "record_id": "structured-rec",
                "fields": {
                    service.FIELD_SOURCE_RECORD: "source-rec",
                    service.FIELD_MEETING_UID: "mtg_550e8400e29b41d4a716446655440000",
                    service.FIELD_STRUCTURED_TABLE_NAME: "2032-08-09 - 测试 - 标的观点",
                    service.FIELD_STRUCTURED_MD_LINK: "https://example.test/file/review",
                    service.FIELD_STRUCTURED_VIEWPOINT_COUNT: 12,
                    service.FIELD_STRUCTURED_APPROVED: False,
                    service.FIELD_ARCHIVE_STATUS: "待归档",
                },
            }
            create = mock.Mock()
            update = mock.Mock()
            baseline = mock.Mock(return_value={"status": "baseline_exists"})
            with mock.patch.multiple(
                service,
                list_bitable_fields=mock.Mock(return_value=[]),
                structured_md_field_issues=mock.Mock(return_value=[]),
                list_bitable_records=mock.Mock(return_value=[existing]),
                create_bitable_record_in=create,
                update_bitable_record_in=update,
                capture_structured_baseline=baseline,
            ):
                result = service.write_structured_review_record(
                    cfg,
                    source_record_id="source-rec",
                    source_fields={},
                    meeting_uid="mtg_550e8400e29b41d4a716446655440000",
                    source_archive_url="https://example.test/file/source",
                    source_file_name="2032-08-09 - 测试.md",
                    meeting_date="2032-08-09",
                    meeting_series="测试",
                    file_name="2032-08-09 - 测试 - 标的观点.md",
                    file_url="https://example.test/file/review",
                    row_count=12,
                )

        self.assertFalse(result["created"])
        self.assertFalse(result["updated"])
        create.assert_not_called()
        update.assert_not_called()
        baseline.assert_called_once_with(cfg, "structured-rec")

    def test_transfer_output_owner_preserves_app_full_access_and_location(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = make_config(Path(temp_dir))
            object.__setattr__(cfg, "output_owner_open_id", "ou_owner")
            with mock.patch.object(service, "request_json", return_value={}) as request:
                service.transfer_output_owner(cfg, "file/one", token="tenant-token")

        self.assertEqual(request.call_args.args[1:3], ("POST", "/drive/v1/permissions/file%2Fone/members/transfer_owner"))
        self.assertEqual(
            request.call_args.kwargs["query"],
            {
                "type": "file",
                "need_notification": False,
                "remove_old_owner": False,
                "old_owner_perm": "full_access",
                "stay_put": True,
            },
        )
        self.assertEqual(
            request.call_args.kwargs["body"],
            {"member_id": "ou_owner", "member_type": "openid"},
        )

    def test_transfer_output_owner_failure_is_distinct(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = make_config(Path(temp_dir))
            object.__setattr__(cfg, "output_owner_open_id", "ou_owner")
            with mock.patch.object(
                service,
                "request_json",
                side_effect=service.FeishuApiError("denied"),
            ), self.assertRaises(service.StructuredError) as caught:
                service.transfer_output_owner(cfg, "file-one", token="tenant-token")

        self.assertEqual(caught.exception.error_code, "owner_transfer_failed")
        self.assertEqual(caught.exception.http_status, 502)

    def test_mutating_cli_requires_apply_before_config_is_loaded(self):
        for command in (
            ["serve"],
            ["generate-record", "rec-one"],
            ["complete-job", "job-one"],
            ["fail-job", "job-one"],
            ["generate-official-json-record", "rec-one"],
        ):
            with self.subTest(command=command), self.assertRaisesRegex(SystemExit, "requires explicit --apply"):
                service.main(command)

    def test_secret_file_is_private_and_atomic(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "secrets" / "token.txt"
            service.write_secret_file(str(path), "token")
            self.assertEqual(path.read_text(encoding="utf-8"), "token\n")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertFalse(list(path.parent.glob(".*.tmp")))

    def test_business_backups_jobs_and_index_are_private(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = make_config(Path(temp_dir))
            backup = service.save_local_backup(cfg, "2032-07", "review.md", b"private meeting\n")
            service.append_index(cfg, {"record_id": "rec-1", "path": str(backup)})
            job_root = service.ensure_semantic_job_dirs(cfg)
            service.write_json_atomic(job_root / "pending" / "job-1" / "context.json", {"record_id": "rec-1"})

            self.assertEqual(stat.S_IMODE(cfg.output_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(backup.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE((cfg.output_dir / "index.jsonl").stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(job_root.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((job_root / "pending").stat().st_mode), 0o700)
            self.assertEqual(
                stat.S_IMODE((job_root / "pending" / "job-1" / "context.json").stat().st_mode),
                0o600,
            )

    def test_semantic_worker_writes_private_files_and_syncs_queue_moves(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "processing" / "job-1" / "result.json"
            worker.write_json(output, {"ok": True})
            self.assertEqual(stat.S_IMODE(output.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)

            target_root = root / "done"
            worker.ensure_private_directory(target_root)
            target = target_root / output.parent.name
            with mock.patch.object(worker, "fsync_directory") as sync:
                worker.durable_replace(output.parent, target)
            self.assertEqual(
                [call.args[0] for call in sync.call_args_list],
                [target_root, output.parent.parent],
            )

    def test_service_queue_publish_syncs_target_then_source_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / ".creating-job"
            target_root = root / "pending"
            source.mkdir()
            target_root.mkdir()
            target = target_root / "job"
            with mock.patch.object(service, "fsync_directory") as sync:
                service.durable_replace(source, target)
            self.assertEqual(
                [call.args[0] for call in sync.call_args_list],
                [target_root, root],
            )

    def test_cross_record_same_name_uploads_are_serialized_and_backups_are_isolated(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = make_config(Path(temp_dir))
            uploaded_names: list[str] = []
            state_lock = threading.Lock()

            def list_items(_cfg, _folder_token):
                time.sleep(0.02)
                with state_lock:
                    return [{"name": name} for name in uploaded_names]

            def upload(_cfg, _folder_token, name, _content, **_kwargs):
                with state_lock:
                    uploaded_names.append(name)
                    return f"token-{len(uploaded_names)}"

            results: list[tuple[str, str, Path]] = []
            errors: list[BaseException] = []

            def worker(record_id: str, source_hash: str) -> None:
                try:
                    results.append(
                        service.upload_official_json_artifact(
                            cfg,
                            official_folder_token="folder",
                            month="2032-07",
                            source_record_id=record_id,
                            source_md_hash=source_hash,
                            json_name="same.json",
                            content=record_id.encode("utf-8"),
                        )
                    )
                except BaseException as exc:  # pragma: no cover - asserted below
                    errors.append(exc)

            with mock.patch.multiple(
                service,
                list_drive_folder_items=mock.Mock(side_effect=list_items),
                upload_drive_file=mock.Mock(side_effect=upload),
                resolve_uploaded_file_url=mock.Mock(
                    side_effect=lambda _cfg, _folder, token, name: f"https://example.test/{token}/{name}"
                ),
            ):
                threads = [
                    threading.Thread(target=worker, args=("record-one", "a" * 64)),
                    threading.Thread(target=worker, args=("record-two", "b" * 64)),
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=3)

            self.assertFalse(errors)
            self.assertEqual(set(uploaded_names), {"same.json", "same (2).json"})
            self.assertEqual(len(results), 2)
            self.assertEqual(len({result[2].parent for result in results}), 2)

    def test_lost_upload_response_reconciles_exact_name_and_hash_without_duplicate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = make_config(Path(temp_dir))
            content = b'{"metadata":{},"rows":[]}'
            items: list[dict[str, str]] = []
            uploads = 0

            def upload(_cfg, _folder, name, uploaded_content, **_kwargs):
                nonlocal uploads
                uploads += 1
                items.append({"name": name, "token": "remote-token"})
                self.assertEqual(uploaded_content, content)
                raise RuntimeError("response lost after remote upload")

            with mock.patch.multiple(
                service,
                list_drive_folder_items=mock.Mock(side_effect=lambda *_args: list(items)),
                upload_drive_file=mock.Mock(side_effect=upload),
                download_drive_file=mock.Mock(return_value=content),
                resolve_uploaded_file_url=mock.Mock(return_value="https://example.test/file/remote-token"),
            ):
                first = service.upload_official_json_artifact(
                    cfg,
                    official_folder_token="folder",
                    month="2032-07",
                    source_record_id="record-one",
                    source_md_hash="a" * 64,
                    json_name="same.json",
                    content=content,
                )
                second = service.upload_official_json_artifact(
                    cfg,
                    official_folder_token="folder",
                    month="2032-07",
                    source_record_id="record-one",
                    source_md_hash="a" * 64,
                    json_name="same.json",
                    content=content,
                )

            self.assertEqual(uploads, 1)
            self.assertEqual(first[0], "same.json")
            self.assertEqual(second[0], "same.json")
            self.assertEqual(len(items), 1)

    def test_official_json_record_lookup_fails_on_multiple_sources(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = make_config(Path(temp_dir))
            records = [
                {"record_id": "one", "fields": {service.FIELD_OFFICIAL_SOURCE_MD_RECORD: "source-rec"}},
                {"record_id": "two", "fields": {service.FIELD_OFFICIAL_SOURCE_MD_RECORD: "source-rec"}},
            ]
            with mock.patch.object(service, "list_bitable_records", return_value=records):
                with self.assertRaises(service.StructuredError) as caught:
                    service.find_existing_official_json_record(cfg, "official-table", "source-rec")
            self.assertEqual(caught.exception.error_code, "official_json_record_ambiguous")

    def test_official_json_ambiguity_fails_before_status_write_or_download(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = make_config(Path(temp_dir))
            fields = {
                service.FIELD_STRUCTURED_APPROVED: True,
                service.FIELD_STRUCTURED_ARCHIVE_LINK: "https://example.test/file/source-md",
                service.FIELD_ARCHIVE_STATUS: "已归档",
                service.FIELD_VERSION_STATUS: "已完成",
                service.FIELD_APPROVED_SHA256: "a" * 64,
                service.FIELD_MEETING_UID: "mtg_550e8400e29b41d4a716446655440000",
            }
            status_write = mock.Mock()
            download = mock.Mock()
            with mock.patch.multiple(
                service,
                list_bitable_fields=mock.Mock(return_value=[]),
                structured_md_field_issues=mock.Mock(return_value=[]),
                official_json_field_issues=mock.Mock(return_value=[]),
                get_bitable_record_from=mock.Mock(return_value={"fields": fields}),
                resolve_bitable_table_id=mock.Mock(return_value="official-table"),
                find_existing_official_json_record=mock.Mock(
                    side_effect=service.StructuredError(
                        "official_json_record_ambiguous",
                        "multiple official JSON records",
                        409,
                    )
                ),
                update_structured_json_status=status_write,
                download_drive_file=download,
            ):
                with self.assertRaises(service.StructuredError) as caught:
                    service._generate_official_json_for_record_unlocked(cfg, "source-rec")

            self.assertEqual(caught.exception.error_code, "official_json_record_ambiguous")
            status_write.assert_not_called()
            download.assert_not_called()

    def test_cleanup_deletes_only_pipeline_resolved_previous_token(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = make_config(Path(temp_dir))
            items = [
                {"name": "old.json", "type": "file", "token": "old-token"},
                {"name": "new.json", "type": "file", "token": "new-token"},
                {"name": "other.json", "type": "file", "token": "other-token"},
            ]

            def delete(_cfg, token):
                items[:] = [item for item in items if item["token"] != token]

            with mock.patch.multiple(
                service,
                list_drive_folder_items=mock.Mock(side_effect=lambda *_args: list(items)),
                delete_drive_file=mock.Mock(side_effect=delete),
            ):
                deleted = service.cleanup_superseded_official_json_files(
                    cfg,
                    official_folder_token="folder",
                    keep_file_token="new-token",
                    superseded_file_tokens=("old-token",),
                )

            self.assertEqual(deleted, 1)
            self.assertEqual({item["token"] for item in items}, {"new-token", "other-token"})

    def test_duplicate_content_length_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = make_config(Path(temp_dir))
            server = service.ThreadingHTTPServer(("127.0.0.1", 0), service.make_handler(cfg))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
                connection.putrequest("POST", "/generate")
                connection.putheader("X-Structured-Token", "http-token")
                connection.putheader("Content-Type", "application/json")
                connection.putheader("Content-Length", "2")
                connection.putheader("Content-Length", "3")
                connection.endheaders(b"{}")
                response = connection.getresponse()
                payload = json.loads(response.read().decode("utf-8"))
                connection.close()
                self.assertEqual(response.status, 400)
                self.assertEqual(payload["error_code"], "ambiguous_content_length")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
