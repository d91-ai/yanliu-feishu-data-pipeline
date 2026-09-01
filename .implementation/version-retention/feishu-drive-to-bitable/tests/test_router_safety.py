from __future__ import annotations

import hashlib
import http.client
import importlib.util
import json
from pathlib import Path
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "feishu_drive_to_bitable.py"
SPEC = importlib.util.spec_from_file_location("feishu_drive_to_bitable", MODULE_PATH)
assert SPEC and SPEC.loader
router = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = router
SPEC.loader.exec_module(router)


def make_config(**overrides):
    values = {
        "app_id": "app",
        "app_secret": "secret",
        "folder_token": "source-folder",
        "source_folder_tokens": (),
        "archive_root_folder_token": "archive-folder",
        "folder_registry_path": str(Path(tempfile.gettempdir()) / "data-pipeline-router-tests" / "folder_registry.json"),
        "bitable_app_token": "base-token",
        "bitable_table_id": "table-id",
        "archive_http_token": "test-token",
        "event_spool_dir": str(Path(tempfile.gettempdir()) / "data-pipeline-router-tests" / "event-spool"),
    }
    values.update(overrides)
    return router.Config(**values)


class ConfigSafetyTests(unittest.TestCase):
    def base_env(self):
        return {
            "FEISHU_APP_ID": "app",
            "FEISHU_APP_SECRET": "secret",
            "FEISHU_FOLDER_TOKEN": "source",
            "FEISHU_ARCHIVE_ROOT_FOLDER_TOKEN": "archive",
            "FEISHU_BITABLE_APP_TOKEN": "base",
            "FEISHU_BITABLE_TABLE_ID": "table",
            "FEISHU_VERSION_CONFIG_PATH": "",
        }

    def test_real_write_requires_version_enforcement(self):
        env = self.base_env()
        env["FEISHU_DRY_RUN"] = "false"
        with self.assertRaises(SystemExit):
            router.config_from_env(env)

    def test_enabled_http_requires_authentication(self):
        env = self.base_env()
        env["FEISHU_ARCHIVE_HTTP_ENABLED"] = "true"
        with self.assertRaises(SystemExit):
            router.config_from_env(env)

    def test_pipeline_mode_is_explicit(self):
        env = self.base_env()
        env["FEISHU_PIPELINE_MODE"] = "invalid"
        with self.assertRaisesRegex(SystemExit, "legacy or unified"):
            router.config_from_env(env)

    def test_unified_mode_requires_field_binding_manifest(self):
        env = self.base_env()
        env["FEISHU_PIPELINE_MODE"] = "unified"
        with self.assertRaisesRegex(SystemExit, "FEISHU_FIELD_BINDINGS_PATH"):
            router.config_from_env(env)
        env["FEISHU_FIELD_BINDINGS_PATH"] = "/app/.field-bindings.json"
        self.assertEqual(
            router.config_from_env(env).field_bindings_path,
            "/app/.field-bindings.json",
        )

    def test_pipeline_event_watermark_must_be_non_negative(self):
        env = self.base_env()
        env["FEISHU_PIPELINE_EVENT_NOT_BEFORE_MS"] = "-1"
        with self.assertRaisesRegex(SystemExit, "must be non-negative"):
            router.config_from_env(env)

    def test_supported_source_meeting_types_are_exact(self):
        self.assertEqual(router.source_meeting_type("2032-07-14 - 日度复盘.md"), "多人复盘会")
        self.assertEqual(
            router.source_meeting_type("2032-07-14 - 某公司 - 上市公司交流.md"),
            "公司交流",
        )
        with self.assertRaises(ValueError):
            router.source_meeting_type("2032-07-14 - 某公司 - 未知类型.md")

    def test_route_file_inherits_only_shared_app_credentials(self):
        route_body = """\
FEISHU_FOLDER_TOKEN=route-source
FEISHU_ARCHIVE_ROOT_FOLDER_TOKEN=route-archive
FEISHU_BITABLE_APP_TOKEN=route-base
FEISHU_BITABLE_TABLE_ID=route-table
FEISHU_VERSION_CONFIG_PATH=
"""
        shared_env = {
            "FEISHU_APP_ID": "shared-app",
            "FEISHU_APP_SECRET": "shared-secret",
            # A resource identifier in the process environment must never fill
            # a missing route-local identifier.
            "FEISHU_FOLDER_TOKEN": "wrong-global-source",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / ".env.route"
            path.write_text(route_body, encoding="utf-8")
            with mock.patch.dict(router.os.environ, shared_env, clear=True):
                cfg = router.read_config_from_env_file(path)
        self.assertEqual(cfg.app_id, "shared-app")
        self.assertEqual(cfg.app_secret, "shared-secret")
        self.assertEqual(cfg.folder_token, "route-source")

    def test_route_file_does_not_inherit_missing_resource_identifiers(self):
        route_body = """\
FEISHU_ARCHIVE_ROOT_FOLDER_TOKEN=route-archive
FEISHU_BITABLE_APP_TOKEN=route-base
FEISHU_BITABLE_TABLE_ID=route-table
FEISHU_VERSION_CONFIG_PATH=
"""
        shared_env = {
            "FEISHU_APP_ID": "shared-app",
            "FEISHU_APP_SECRET": "shared-secret",
            "FEISHU_FOLDER_TOKEN": "wrong-global-source",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / ".env.route"
            path.write_text(route_body, encoding="utf-8")
            with mock.patch.dict(router.os.environ, shared_env, clear=True):
                with self.assertRaisesRegex(SystemExit, "FEISHU_FOLDER_TOKEN"):
                    router.read_config_from_env_file(path)

    def test_route_cli_loads_only_sibling_router_credentials(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            router_env = root / ".env.router"
            route_env = root / ".env.meeting-minutes"
            router_env.write_text(
                "FEISHU_APP_ID=shared-app\n"
                "FEISHU_APP_SECRET=shared-secret\n"
                "FEISHU_FOLDER_TOKEN=wrong-global-source\n",
                encoding="utf-8",
            )
            route_env.write_text(
                "FEISHU_FOLDER_TOKEN=route-source\n"
                "FEISHU_ARCHIVE_ROOT_FOLDER_TOKEN=route-archive\n"
                "FEISHU_BITABLE_APP_TOKEN=route-base\n"
                "FEISHU_BITABLE_TABLE_ID=route-table\n"
                "FEISHU_VERSION_CONFIG_PATH=\n",
                encoding="utf-8",
            )
            with mock.patch.dict(
                router.os.environ,
                {"FEISHU_ENV_FILE": str(route_env)},
                clear=True,
            ):
                router.load_dotenv()
                cfg = router.config_from_env(router.os.environ)
            self.assertEqual(cfg.app_id, "shared-app")
            self.assertEqual(cfg.app_secret, "shared-secret")
            self.assertEqual(cfg.folder_token, "route-source")


class MeetingSourceRegistrationTests(unittest.TestCase):
    def test_structured_viewpoint_count_uses_actual_cards(self):
        content = (
            "# 标的观点审阅表\n\n"
            "## 观点 1\n\n正文\n\n"
            "## 观点\n\n正文\n\n"
            "> ## 观点 99\n"
        ).encode("utf-8")
        self.assertEqual(router.structured_viewpoint_count(content), 2)

    def test_structured_viewpoint_count_rejects_missing_cards(self):
        with self.assertRaisesRegex(ValueError, "no viewpoint cards"):
            router.structured_viewpoint_count(b"# unrelated\n")

    def test_v7_review_markdown_is_recognized_without_frontmatter(self):
        content = "# 标的观点审阅表\n\n- 会议日期：2032-08-09\n".encode("utf-8")
        self.assertTrue(router.is_v7_review_markdown(content))
        with self.assertRaisesRegex(ValueError, "missing frontmatter"):
            router.parse_structured_frontmatter(content)

    def test_unrelated_markdown_is_not_treated_as_v7_review(self):
        self.assertFalse(router.is_v7_review_markdown(b"# unrelated\n"))

    def test_meeting_source_content_shape_is_not_an_ingestion_gate(self):
        cfg = make_config(meeting_contract_enabled=True, dry_run=True)
        event = {
            "header": {"event_id": "evt-free-form", "create_time": "1784785083000"},
            "event": {
                "folder_token": "source-folder",
                "file_token": "file-token",
                "file_type": "file",
            },
        }
        with mock.patch.object(
            router,
            "get_file_meta",
            return_value={"title": "free-form.md", "create_time": "1784785083"},
        ), mock.patch.object(
            router,
            "download_drive_file_version",
            side_effect=AssertionError("meeting source content must not be inspected during registration"),
        ), mock.patch.object(
            router,
            "validate_meeting_contract_content",
            side_effect=AssertionError("meeting source content must not be rejected during registration"),
        ), mock.patch.object(
            router,
            "list_bitable_fields",
            return_value=[],
        ), mock.patch.object(
            router,
            "build_record_fields",
            return_value=({"文件名": "free-form"}, []),
        ), mock.patch.object(
            router,
            "enrich_meeting_contract_record_fields",
        ), mock.patch.object(
            router,
            "require_reconcile_file_token_field",
        ):
            router.process_file_created_event(cfg, event)


class FileEventSpoolTests(unittest.TestCase):
    def test_spool_is_durable_idempotent_and_replayable(self):
        event = {"header": {"event_id": "evt-1"}, "event": {"file_token": "sensitive"}}
        with tempfile.TemporaryDirectory() as temp_dir:
            spool = router.FileEventSpool(temp_dir)
            key = spool.enqueue(event)
            self.assertEqual(spool.enqueue(event), key)
            self.assertEqual(len(list(spool.pending.glob("*.json"))), 1)
            self.assertEqual(stat.S_IMODE(spool.root.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(next(spool.pending.glob("*.json")).stat().st_mode), 0o600)

            item = spool.claim()
            self.assertIsNotNone(item)
            self.assertEqual(spool.recover_processing(), 1)
            recovered = spool.claim()
            self.assertIsNotNone(recovered)
            spool.reject(recovered, ValueError("business content must not be logged"))
            dead_payload = json.loads(next(spool.dead_letter.glob("*.json")).read_text(encoding="utf-8"))
            self.assertEqual(dead_payload["last_error_code"], "invalid_input")
            self.assertEqual(spool.replay_dead_letters(key), 1)
            replayed = spool.claim()
            self.assertIsNotNone(replayed)
            spool.acknowledge(replayed)
            self.assertFalse(list(spool.pending.glob("*.json")))
            self.assertFalse(list(spool.processing.glob("*.json")))

    def test_spool_syncs_directories_for_create_move_and_delete(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            spool = router.FileEventSpool(temp_dir)
            with mock.patch.object(router, "fsync_directory") as sync:
                spool.enqueue({"header": {"event_id": "evt-sync"}, "event": {}})
                item = spool.claim()
                self.assertIsNotNone(item)
                assert item is not None
                spool.acknowledge(item)

            self.assertEqual(
                [call.args[0] for call in sync.call_args_list],
                [spool.pending, spool.processing, spool.pending, spool.processing],
            )

    def test_replay_cleans_stale_dead_letter_when_active_copy_exists(self):
        event = {"header": {"event_id": "evt-replay-crash"}, "event": {"file_token": "one"}}
        with tempfile.TemporaryDirectory() as temp_dir:
            spool = router.FileEventSpool(temp_dir)
            key = spool.enqueue(event)
            item = spool.claim()
            self.assertIsNotNone(item)
            assert item is not None
            spool.reject(item, RuntimeError("retry"))

            dead_path = spool.dead_letter / f"{key}.json"
            envelope = json.loads(dead_path.read_text(encoding="utf-8"))
            envelope["attempts"] = 0
            envelope.pop("last_error_code", None)
            spool._write_atomic(spool.pending / dead_path.name, envelope)

            self.assertEqual(spool.replay_dead_letters(key), 1)
            self.assertFalse(dead_path.exists())
            claimed = spool.claim()
            self.assertIsNotNone(claimed)
            assert claimed is not None
            spool.acknowledge(claimed)
            self.assertEqual(spool.replay_dead_letters(key), 0)
            self.assertIsNone(spool.claim())

    def test_recover_processing_keeps_rejected_envelope_in_dead_letter(self):
        event = {"header": {"event_id": "evt-reject-crash"}, "event": {"file_token": "one"}}
        with tempfile.TemporaryDirectory() as temp_dir:
            spool = router.FileEventSpool(temp_dir)
            spool.enqueue(event)
            item = spool.claim()
            self.assertIsNotNone(item)
            assert item is not None

            rejected = dict(item.envelope)
            rejected["attempts"] = 1
            rejected["last_error_code"] = "transient_error"
            # Simulate a crash after the processing envelope was durably
            # updated but before processing -> dead-letter rename.
            spool._write_atomic(item.path, rejected)

            restarted = router.FileEventSpool(temp_dir)
            self.assertEqual(restarted.recover_processing(), 1)
            self.assertFalse(list(restarted.pending.glob("*.json")))
            dead = list(restarted.dead_letter.glob("*.json"))
            self.assertEqual(len(dead), 1)
            self.assertEqual(json.loads(dead[0].read_text(encoding="utf-8"))["attempts"], 1)
            self.assertIsNone(restarted.claim())


class FolderRegistrySafetyTests(unittest.TestCase):
    def test_registry_write_is_private_atomic_and_lock_serializes_writers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "data" / "folders.json"
            cfg = make_config(folder_registry_path=str(path))
            router.save_folder_registry(cfg, {"months": {"2032-08": {"source_folder_token": "source"}}})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertFalse(list(path.parent.glob(".*.tmp")))

            state = {"active": 0, "max_active": 0}
            state_lock = threading.Lock()

            def hold_lock() -> None:
                with router.folder_registry_lock(cfg, exclusive=True):
                    with state_lock:
                        state["active"] += 1
                        state["max_active"] = max(state["max_active"], state["active"])
                    time.sleep(0.03)
                    with state_lock:
                        state["active"] -= 1

            threads = [threading.Thread(target=hold_lock) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)
            self.assertEqual(state["max_active"], 1)
            self.assertEqual(stat.S_IMODE(path.with_name(path.name + ".lock").stat().st_mode), 0o600)


class RecordOperationLockTests(unittest.TestCase):
    def test_record_lock_serializes_threads_and_is_private(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = make_config(
                event_spool_dir=str(Path(temp_dir) / "event-spool"),
                folder_registry_path=str(Path(temp_dir) / "folder_registry.json"),
            )
            state = {"active": 0, "max_active": 0}
            state_lock = threading.Lock()

            def hold_lock() -> None:
                with router.record_operation_lock(cfg, "rec-1"):
                    with state_lock:
                        state["active"] += 1
                        state["max_active"] = max(state["max_active"], state["active"])
                    time.sleep(0.03)
                    with state_lock:
                        state["active"] -= 1

            threads = [threading.Thread(target=hold_lock) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)

            lock_files = list((Path(temp_dir) / "record-locks").glob("*.lock"))
            self.assertEqual(state["max_active"], 1)
            self.assertEqual(len(lock_files), 1)
            self.assertEqual(stat.S_IMODE(lock_files[0].stat().st_mode), 0o600)

    def test_archived_migration_uses_the_same_record_lock(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = make_config(folder_registry_path=str(Path(temp_dir) / "folder_registry.json"))
            state = {"active": 0, "max_active": 0}
            state_lock = threading.Lock()

            def migrate(_cfg, _record_id):
                with state_lock:
                    state["active"] += 1
                    state["max_active"] = max(state["max_active"], state["active"])
                time.sleep(0.03)
                with state_lock:
                    state["active"] -= 1
                return {"status": "migrated"}

            with mock.patch.object(router, "_migrate_archived_record_unlocked", side_effect=migrate):
                threads = [
                    threading.Thread(target=router.migrate_archived_record, args=(cfg, "rec-1"))
                    for _ in range(2)
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=2)

            self.assertEqual(state["max_active"], 1)

    def test_migration_failure_does_not_downgrade_confirmed_terminal_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = make_config(folder_registry_path=str(Path(temp_dir) / "folder_registry.json"))
            with mock.patch.object(
                router,
                "migrate_archived_record",
                side_effect=RuntimeError("response lost"),
            ), mock.patch.object(
                router,
                "migration_terminal_is_complete",
                return_value=True,
            ), mock.patch.object(router, "update_bitable_record") as update:
                result = router.migrate_archived_record_with_failure_status(cfg, "rec-1")

            self.assertEqual(result["status"], "migration_reconciled")
            update.assert_not_called()

    def test_baseline_failure_does_not_downgrade_confirmed_terminal_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = make_config(folder_registry_path=str(Path(temp_dir) / "folder_registry.json"))
            with mock.patch.object(
                router,
                "capture_baseline_for_record",
                side_effect=RuntimeError("response lost"),
            ), mock.patch.object(
                router, "baseline_terminal_result", return_value={"status": "baseline_reconciled"}
            ), mock.patch.object(router, "update_bitable_record") as update:
                result = router.capture_baseline_for_record_with_failure_status(cfg, "rec-1")

            self.assertEqual(result["status"], "baseline_reconciled")
            update.assert_not_called()

    def test_file_token_field_is_required_for_reconciliation(self):
        cfg = make_config()
        with self.assertRaisesRegex(ValueError, "Exactly one writable file-token"):
            router.require_reconcile_file_token_field(
                cfg,
                [{"field_id": "name", "field_name": "文件名", "type": router.TYPE_TEXT}],
                {"文件名": "meeting.md"},
                "file-token",
            )


class CreateReconciliationTests(unittest.TestCase):
    def test_file_token_reconciliation_uses_the_same_normalized_alias_rule(self):
        cfg = make_config()
        records = [{"record_id": "rec-123", "fields": {"file token": "file-1"}}]
        with mock.patch.object(router, "list_bitable_records", return_value=records):
            matches = router.find_bitable_records_by_file_token(cfg, "file-1")
        self.assertEqual([record["record_id"] for record in matches], ["rec-123"])

    def test_missing_create_id_is_reconciled_by_exact_file_token(self):
        cfg = make_config()
        with mock.patch.object(router, "create_bitable_record", return_value={}), mock.patch.object(
            router,
            "find_bitable_records_by_file_token",
            return_value=[{"record_id": "rec-123"}],
        ):
            record_id = router.create_bitable_record_reconciled(
                cfg,
                {"File Token": "file-1"},
                "client-token",
                "file-1",
            )
        self.assertEqual(record_id, "rec-123")

    def test_ambiguous_reconciliation_fails_closed(self):
        cfg = make_config()
        with mock.patch.object(router, "create_bitable_record", return_value={}), mock.patch.object(
            router,
            "find_bitable_records_by_file_token",
            return_value=[{"record_id": "one"}, {"record_id": "two"}],
        ):
            with self.assertRaisesRegex(router.FeishuApiError, "record_create_ambiguous"):
                router.create_bitable_record_reconciled(cfg, {}, "client-token", "file-1")


class UnifiedPipelineRouterTests(unittest.TestCase):
    REQUIRED_FIELDS = [
        {"field_id": f"field-{index}", "field_name": name, "type": router.TYPE_TEXT}
        for index, name in enumerate(sorted(router.UNIFIED_PIPELINE_REQUIRED_FIELDS))
    ]

    @classmethod
    def field_id(cls, field_name):
        return next(
            field["field_id"]
            for field in cls.REQUIRED_FIELDS
            if field["field_name"] == field_name
        )

    @classmethod
    def edited_action(cls, record_id, *field_names):
        return {
            "action": "record_edited",
            "record_id": record_id,
            "after_value": [
                {"field_id": cls.field_id(field_name), "field_value": "changed"}
                for field_name in field_names
            ],
        }

    def file_event(self, cfg, *, event_id="event-one"):
        return {
            "header": {
                "event_type": router.FILE_CREATED_EVENT_TYPE,
                "event_id": event_id,
                "create_time": "1786579200000",
            },
            "event": {
                "folder_token": cfg.folder_token,
                "file_token": "source-token",
                "file_type": "file",
            },
        }

    def test_registered_file_event_does_not_create_a_second_base_record(self):
        cfg = make_config(pipeline_mode="unified")
        with mock.patch.object(
            router, "list_bitable_fields", return_value=self.REQUIRED_FIELDS
        ), mock.patch.object(
            router,
            "find_unified_records_by_file_token",
            return_value=[{"record_id": "record-one"}],
        ), mock.patch.object(router, "create_bitable_record_reconciled") as create, mock.patch.object(
            router, "capture_baseline_for_record_with_failure_status"
        ) as baseline:
            router.process_file_created_event(cfg, self.file_event(cfg))
        create.assert_not_called()
        baseline.assert_not_called()

    def test_pre_cutover_base_event_is_ignored_before_any_record_read(self):
        cfg = make_config(
            pipeline_mode="unified",
            form_ingress_enabled=True,
            pipeline_event_not_before_ms=1786579201000,
        )
        event = {
            "header": {
                "event_type": router.BITABLE_RECORD_CHANGED_EVENT_TYPE,
                "create_time": "1786579200000",
            },
            "event": {
                "file_token": cfg.bitable_app_token,
                "table_id": cfg.bitable_table_id,
                "action_list": [{"record_id": "record-one"}],
            },
        }
        with mock.patch.object(
            router, "process_form_attachment_ingress"
        ) as ingress, mock.patch.object(router, "get_bitable_record") as get_record:
            router.process_bitable_record_changed_event(cfg, event)
        ingress.assert_not_called()
        get_record.assert_not_called()

    def test_deleted_base_record_is_ignored_before_any_record_read(self):
        cfg = make_config(pipeline_mode="unified", form_ingress_enabled=True)
        event = {
            "header": {
                "event_type": router.BITABLE_RECORD_CHANGED_EVENT_TYPE,
                "create_time": "1786579200000",
            },
            "event": {
                "file_token": cfg.bitable_app_token,
                "table_id": cfg.bitable_table_id,
                "action_list": [
                    {"action": "record_deleted", "record_id": "record-one"}
                ],
            },
        }
        with mock.patch.object(
            router, "process_form_attachment_ingress"
        ) as ingress, mock.patch.object(router, "get_bitable_record") as get_record:
            router.process_bitable_record_changed_event(cfg, event)
        ingress.assert_not_called()
        get_record.assert_not_called()

    def test_cutover_watermark_rejects_invalid_event_time(self):
        cfg = make_config(
            pipeline_mode="unified", pipeline_event_not_before_ms=1786579201000
        )
        event = {
            "header": {
                "event_type": router.BITABLE_RECORD_CHANGED_EVENT_TYPE,
                "create_time": "invalid",
            },
            "event": {
                "file_token": cfg.bitable_app_token,
                "table_id": cfg.bitable_table_id,
                "action_list": [{"record_id": "record-one"}],
            },
        }
        with self.assertRaisesRegex(ValueError, "unified_event_time_invalid"):
            router.process_bitable_record_changed_event(cfg, event)

    def test_unregistered_file_is_privately_recorded_and_dead_lettered(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = make_config(
                pipeline_mode="unified",
                unregistered_file_spool_dir=str(Path(directory) / "unregistered"),
            )
            with mock.patch.object(
                router, "list_bitable_fields", return_value=self.REQUIRED_FIELDS
            ), mock.patch.object(
                router, "find_unified_records_by_file_token", return_value=[]
            ), mock.patch.object(router, "create_bitable_record_reconciled") as create:
                with self.assertRaises(router.PipelineBindingPendingError):
                    router.process_file_created_event(cfg, self.file_event(cfg))
            files = list((Path(directory) / "unregistered").glob("*.json"))
            self.assertEqual(len(files), 1)
            self.assertEqual(stat.S_IMODE(files[0].stat().st_mode), 0o600)
            payload = json.loads(files[0].read_text(encoding="utf-8"))
            self.assertEqual(payload["file_token"], "source-token")
            self.assertEqual(payload["reason"], "pipeline_binding_pending")
            create.assert_not_called()

    def test_reviewed_industry_markdown_enqueues_one_hash_bound_job(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = make_config(
                pipeline_mode="unified",
                pipeline_review_job_spool_dir=str(Path(directory) / "review-jobs"),
            )
            event = {
                "header": {
                    "event_type": router.BITABLE_RECORD_CHANGED_EVENT_TYPE,
                    "create_time": "1786579200000",
                },
                "event": {
                    "file_token": cfg.bitable_app_token,
                    "table_id": cfg.bitable_table_id,
                    "action_list": [
                        self.edited_action("record-one", "行业与市场观点审核")
                    ],
                },
            }
            fields = {
                "会议ID": "mtg_550e8400e29b41d4a716446655440000",
                "数据版本": 2,
                "源纪要审核": "未审核",
                "行业与市场观点审核": "已审核",
                "标的观点审核": "未审核",
                "行业与市场观点MD": {
                    "link": "https://example.test/file/review-token",
                    "text": "review.md",
                },
            }
            with mock.patch.object(
                router, "list_bitable_fields", return_value=self.REQUIRED_FIELDS
            ), mock.patch.object(
                router, "get_bitable_record", return_value={"fields": fields}
            ), mock.patch.object(
                router, "download_drive_file_version", return_value=b"# reviewed\n"
            ):
                router.process_bitable_record_changed_event(cfg, event)
            jobs = list((Path(directory) / "review-jobs" / "pending").glob("*.json"))
            self.assertEqual(len(jobs), 1)
            job = json.loads(jobs[0].read_text(encoding="utf-8"))
            self.assertEqual(job["artifact_type"], "industry_market_viewpoints")
            self.assertEqual(job["review_file_token"], "review-token")
            self.assertEqual(job["data_version"], 2)
            self.assertEqual(job["review_md_sha256"], hashlib.sha256(b"# reviewed\n").hexdigest())

    def test_completed_review_job_is_not_requeued_by_later_base_event(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            review_root = root / "review-jobs"
            cfg = make_config(
                pipeline_mode="unified",
                pipeline_review_job_spool_dir=str(review_root),
                pipeline_worker_receipt_dir=str(root / "receipts"),
            )
            event = {
                "header": {
                    "event_type": router.BITABLE_RECORD_CHANGED_EVENT_TYPE,
                    "create_time": "1786579200000",
                },
                "event": {
                    "file_token": cfg.bitable_app_token,
                    "table_id": cfg.bitable_table_id,
                    "action_list": [
                        self.edited_action("record-one", "行业与市场观点审核")
                    ],
                },
            }
            fields = {
                "会议ID": "mtg_550e8400e29b41d4a716446655440000",
                "数据版本": 2,
                "源纪要审核": "未审核",
                "行业与市场观点审核": "已审核",
                "标的观点审核": "未审核",
                "行业与市场观点MD": {
                    "link": "https://example.test/file/review-token",
                    "text": "review.md",
                },
            }
            with mock.patch.object(
                router, "list_bitable_fields", return_value=self.REQUIRED_FIELDS
            ), mock.patch.object(
                router, "get_bitable_record", return_value={"fields": fields}
            ), mock.patch.object(
                router, "download_drive_file_version", return_value=b"# reviewed\n"
            ):
                router.process_bitable_record_changed_event(cfg, event)
                pending = next((review_root / "pending").glob("*.json"))
                done = review_root / "done" / pending.name
                done.parent.mkdir(parents=True)
                pending.replace(done)
                event["header"]["create_time"] = "1786579201000"
                router.process_bitable_record_changed_event(cfg, event)
            self.assertFalse(list((review_root / "pending").glob("*.json")))
            self.assertEqual(len(list((review_root / "done").glob("*.json"))), 1)

    def test_non_review_writeback_does_not_requeue_historical_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = make_config(
                pipeline_mode="unified",
                pipeline_review_job_spool_dir=str(Path(directory) / "review-jobs"),
            )
            event = {
                "header": {
                    "event_type": router.BITABLE_RECORD_CHANGED_EVENT_TYPE,
                    "create_time": "1786579200000",
                },
                "event": {
                    "file_token": cfg.bitable_app_token,
                    "table_id": cfg.bitable_table_id,
                    "action_list": [
                        self.edited_action("record-one", "行业与市场观点JSON")
                    ],
                },
            }
            with mock.patch.object(
                router, "list_bitable_fields", return_value=self.REQUIRED_FIELDS
            ), mock.patch.object(router, "get_bitable_record") as get_record, mock.patch.object(
                router, "download_drive_file_version"
            ) as download:
                router.process_bitable_record_changed_event(cfg, event)
            get_record.assert_not_called()
            download.assert_not_called()
            self.assertFalse(
                list((Path(directory) / "review-jobs" / "pending").glob("*.json"))
            )

    def test_review_receipt_prevents_requeue_after_terminal_file_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            review_root = root / "review-jobs"
            receipt_root = root / "receipts"
            cfg = make_config(
                pipeline_mode="unified",
                pipeline_review_job_spool_dir=str(review_root),
                pipeline_worker_receipt_dir=str(receipt_root),
            )
            fields = {
                "会议ID": "mtg_550e8400e29b41d4a716446655440000",
                "数据版本": 2,
                "源纪要审核": "未审核",
                "行业与市场观点审核": "已审核",
                "标的观点审核": "未审核",
                "行业与市场观点MD": {
                    "link": "https://example.test/file/review-token",
                    "text": "review.md",
                },
            }
            with mock.patch.object(
                router, "download_drive_file_version", return_value=b"# reviewed\n"
            ):
                queued = router.enqueue_unified_review_jobs(
                    cfg,
                    record_id="record-one",
                    fields=fields,
                    event_time="1786579200000",
                )
            self.assertEqual(queued, ["industry_market_viewpoints"])
            pending = next((review_root / "pending").glob("*.json"))
            job = json.loads(pending.read_text(encoding="utf-8"))
            pending.unlink()
            receipt_name = hashlib.sha256(
                f"review\0{job['job_id']}".encode("utf-8")
            ).hexdigest()
            receipt_root.mkdir(parents=True)
            (receipt_root / f"{receipt_name}.json").write_text(
                json.dumps(
                    {
                        "queue_name": "review",
                        "job_id": job["job_id"],
                        "result": {"status": "generated"},
                        "completed_at": "2032-08-13T00:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(
                router, "download_drive_file_version", return_value=b"# reviewed\n"
            ):
                queued_again = router.enqueue_unified_review_jobs(
                    cfg,
                    record_id="record-one",
                    fields=fields,
                    event_time="1786579201000",
                )
            self.assertEqual(queued_again, [])
            self.assertFalse(list((review_root / "pending").glob("*.json")))

    def test_same_review_job_in_multiple_queue_states_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            review_root = root / "review-jobs"
            cfg = make_config(
                pipeline_mode="unified",
                pipeline_review_job_spool_dir=str(review_root),
                pipeline_worker_receipt_dir=str(root / "receipts"),
            )
            fields = {
                "会议ID": "mtg_550e8400e29b41d4a716446655440000",
                "数据版本": 2,
                "源纪要审核": "未审核",
                "行业与市场观点审核": "已审核",
                "标的观点审核": "未审核",
                "行业与市场观点MD": {
                    "link": "https://example.test/file/review-token",
                    "text": "review.md",
                },
            }
            with mock.patch.object(
                router, "download_drive_file_version", return_value=b"# reviewed\n"
            ):
                router.enqueue_unified_review_jobs(
                    cfg,
                    record_id="record-one",
                    fields=fields,
                    event_time="1786579200000",
                )
                pending = next((review_root / "pending").glob("*.json"))
                done = review_root / "done" / pending.name
                done.parent.mkdir(parents=True)
                done.write_bytes(pending.read_bytes())
                with self.assertRaisesRegex(
                    ValueError, "unified_review_job_multiple_states"
                ):
                    router.enqueue_unified_review_jobs(
                        cfg,
                        record_id="record-one",
                        fields=fields,
                        event_time="1786579201000",
                    )


class FieldBindingTests(unittest.TestCase):
    def write_manifest(self, root, fields):
        path = Path(root) / "field-bindings.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": router.FIELD_BINDINGS_SCHEMA_VERSION,
                    "base_token": "base-token",
                    "table_id": "table-id",
                    "fields": fields,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return str(path)

    def unified_binding_fixture(self, root):
        logical_keys = sorted(
            router.UNIFIED_PIPELINE_REQUIRED_FIELDS
            | router.FORM_INGRESS_REQUIRED_FIELDS
            | {router.FIELD_FORM_ATTACHMENT}
        )
        bindings = {
            logical_key: f"fldTest{index:04d}"
            for index, logical_key in enumerate(logical_keys, start=1)
        }
        raw_fields = []
        for index, logical_key in enumerate(logical_keys, start=1):
            field_type = router.TYPE_TEXT
            if logical_key == router.FIELD_FORM_ATTACHMENT:
                field_type = router.TYPE_ATTACHMENT
            elif logical_key == "会议日期":
                field_type = router.TYPE_DATE
            elif logical_key == "数据版本":
                field_type = router.TYPE_NUMBER
            elif logical_key in {
                "会议系列",
                "会议类型",
                "源纪要审核",
                "行业与市场观点审核",
                "标的观点审核",
            }:
                field_type = router.TYPE_SINGLE_SELECT
            elif logical_key in {"会议纪要MD", "会议纪要审核前MD"}:
                field_type = router.TYPE_URL
            raw_fields.append(
                {
                    "field_id": bindings[logical_key],
                    "field_name": f"renamed-{index}",
                    "type": field_type,
                }
            )
        cfg = make_config(
            pipeline_mode="unified",
            form_ingress_enabled=True,
            field_bindings_path=self.write_manifest(root, bindings),
        )
        return cfg, bindings, raw_fields

    def test_bound_field_renames_are_canonicalized_without_warning(self):
        with tempfile.TemporaryDirectory() as root:
            cfg, bindings, raw_fields = self.unified_binding_fixture(root)
            with mock.patch.object(
                router, "_list_bitable_fields_raw", return_value=raw_fields
            ), mock.patch.object(router.logging, "warning") as warning:
                fields = router.list_bitable_fields(cfg)
                router.validate_form_ingress_schema(cfg, fields)
                review_id = bindings["源纪要审核"]
                changed = router.changed_unified_review_artifact_types(
                    {
                        "action": "record_edited",
                        "after_value": [{"field_id": review_id}],
                    },
                    fields,
                )
            self.assertEqual(
                {field["field_name"] for field in fields},
                set(bindings),
            )
            self.assertEqual(changed, {"meeting_minutes"})
            warning.assert_not_called()

    def test_record_reads_and_writes_resolve_current_name_from_field_id(self):
        with tempfile.TemporaryDirectory() as root:
            cfg, bindings, raw_fields = self.unified_binding_fixture(root)
            logical_key = "会议ID"
            current_name = next(
                field["field_name"]
                for field in raw_fields
                if field["field_id"] == bindings[logical_key]
            )
            response = {
                "data": {
                    "record": {
                        "record_id": "rec-test",
                        "fields": {current_name: "mtg_test"},
                    }
                }
            }
            with mock.patch.object(
                router, "_list_bitable_fields_raw", return_value=raw_fields
            ), mock.patch.object(router, "get_tenant_access_token", return_value="token"), mock.patch.object(
                router, "request_json", return_value=response
            ) as request:
                record = router.get_bitable_record(cfg, "rec-test")
                router.update_bitable_record(cfg, "rec-test", {logical_key: "mtg_new"})
            self.assertEqual(record["fields"], {logical_key: "mtg_test"})
            self.assertEqual(request.call_args.kwargs["body"]["fields"], {current_name: "mtg_new"})

    def test_manifest_is_bound_to_exact_base_and_table(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(self.write_manifest(root, {"会议ID": "fldTest0001"}))
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["table_id"] = "other-table"
            path.write_text(json.dumps(payload), encoding="utf-8")
            cfg = make_config(field_bindings_path=str(path))
            with self.assertRaisesRegex(ValueError, "field_bindings_resource_mismatch"):
                router.load_field_bindings(cfg)

    def test_attachment_metadata_is_filtered_by_bound_field_id(self):
        with tempfile.TemporaryDirectory() as root:
            field_id = "fldAttachment1"
            cfg = make_config(
                field_bindings_path=self.write_manifest(
                    root, {router.FIELD_FORM_ATTACHMENT: field_id}
                )
            )
            raw_fields = [
                {
                    "field_id": field_id,
                    "field_name": "renamed-attachment",
                    "type": router.TYPE_ATTACHMENT,
                }
            ]
            response = {
                "data": {
                    "attachments": {
                        "rec-test": {
                            field_id: [{"file_token": "wanted"}],
                            "fldOther0001": [{"file_token": "other"}],
                        }
                    }
                }
            }
            with mock.patch.object(
                router, "_list_bitable_fields_raw", return_value=raw_fields
            ), mock.patch.object(router, "get_tenant_access_token", return_value="token"), mock.patch.object(
                router, "request_json", return_value=response
            ):
                attachments = router.get_attachments(
                    cfg, "rec-test", router.FIELD_FORM_ATTACHMENT
                )
            self.assertEqual([item["file_token"] for item in attachments], ["wanted"])


class FormIngressTests(unittest.TestCase):
    def form_fields(self, cfg):
        fields = [{"field_name": cfg.form_attachment_field, "type": router.TYPE_ATTACHMENT}]
        for name in sorted(router.FORM_INGRESS_REQUIRED_FIELDS):
            field_type = router.TYPE_TEXT
            if name == "会议日期":
                field_type = router.TYPE_DATE
            elif name == "数据版本":
                field_type = router.TYPE_NUMBER
            elif name in {"会议纪要MD", "会议纪要审核前MD"}:
                field_type = router.TYPE_URL
            elif name == "会议系列":
                field_type = router.TYPE_SINGLE_SELECT
            fields.append({"field_name": name, "type": field_type})
        return fields

    def cfg(self, root, **overrides):
        values = {
            "pipeline_mode": "unified",
            "form_ingress_enabled": True,
            "dry_run": False,
            "folder_registry_path": str(Path(root) / "folders.json"),
            "form_ingestion_receipt_dir": str(Path(root) / "receipts"),
            "generation_job_spool_dir": str(Path(root) / "generation"),
        }
        values.update(overrides)
        return make_config(**values)

    def record(self, cfg, token="attachment-1", uid="", version=0, name="input.md"):
        return {
            "fields": {
                cfg.form_attachment_field: [{"file_token": token, "name": name}],
                "会议ID": uid,
                "会议日期": 1786636800000,
                "会议系列": "Alpha",
                "会议类型": "公司交流",
                "数据版本": version,
                "源纪要审核": "未审核",
                "行业与市场观点审核": "未审核",
                "标的观点审核": "未审核",
            }
        }

    def test_form_ingress_is_disabled_by_default_and_requires_unified(self):
        cfg = make_config()
        self.assertFalse(cfg.form_ingress_enabled)
        env = {
            "FEISHU_APP_ID": "app",
            "FEISHU_APP_SECRET": "secret",
            "FEISHU_FOLDER_TOKEN": "source",
            "FEISHU_ARCHIVE_ROOT_FOLDER_TOKEN": "archive",
            "FEISHU_BITABLE_APP_TOKEN": "base",
            "FEISHU_BITABLE_TABLE_ID": "table",
            "FEISHU_FORM_INGRESS_ENABLED": "true",
            "FEISHU_PIPELINE_MODE": "legacy",
            "FEISHU_VERSION_CONFIG_PATH": "",
        }
        with self.assertRaisesRegex(SystemExit, "requires FEISHU_PIPELINE_MODE=unified"):
            router.config_from_env(env)

    def test_form_schema_requires_attachment_type_and_single_select_series(self):
        cfg = self.cfg("/tmp/form-schema-test")
        fields = self.form_fields(cfg)
        next(field for field in fields if field["field_name"] == "会议系列")["type"] = router.TYPE_NUMBER
        with self.assertRaisesRegex(ValueError, "form_ingress_field_type_invalid:会议系列"):
            router.validate_form_ingress_schema(cfg, fields)
        fields = self.form_fields(cfg)
        fields[0]["type"] = router.TYPE_TEXT
        with self.assertRaisesRegex(ValueError, "form_attachment_field_type_invalid"):
            router.validate_form_ingress_schema(cfg, fields)

    def test_form_series_and_type_are_safe_path_components(self):
        cfg = self.cfg("/tmp/form-component-test")
        fields = {
            "会议日期": 1786636800000,
            "会议系列": " Alpha\tSeries ",
            "会议类型": "公司交流",
        }
        self.assertEqual(
            router.form_meeting_metadata_from_fields(cfg, fields)[1], "Alpha Series"
        )
        for value, code in [("bad/name", "invalid"), ("x" * 41, "too_long")]:
            fields["会议系列"] = value
            with self.assertRaisesRegex(ValueError, f"form_meeting_series_{code}"):
                router.form_meeting_metadata_from_fields(cfg, fields)

    def test_form_attachment_count_name_empty_and_size_guards(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = self.cfg(root, form_attachment_max_bytes=4)
            fields = self.form_fields(cfg)
            record = self.record(cfg)
            cases = [
                ([], "ignored"),
                ([{"file_token": "attachment-1", "name": "a.md"}, {"file_token": "attachment-1", "name": "b.md"}], "count"),
                ([{"file_token": "attachment-1", "name": "a.txt"}], "markdown"),
                ([{"file_token": "attachment-1", "name": "a.md"}], "empty"),
                ([{"file_token": "attachment-1", "name": "a.md", "size": 5}], "large"),
            ]
            for attachments, expected in cases:
                with self.subTest(expected=expected):
                    patches = [
                        mock.patch.object(router, "list_bitable_fields", return_value=fields),
                        mock.patch.object(router, "get_bitable_record", return_value=record),
                        mock.patch.object(router, "get_attachments", return_value=attachments),
                    ]
                    for patcher in patches:
                        patcher.start()
                    try:
                        if expected == "ignored":
                            self.assertEqual(
                                router.process_form_attachment_ingress(cfg, "rec"),
                                {"status": "ignored", "reason": "form_attachment_missing"},
                            )
                        else:
                            media = b"" if expected == "empty" else b"hello"
                            with mock.patch.object(router, "download_drive_media", return_value=media):
                                with self.assertRaises(ValueError) as ctx:
                                    router.process_form_attachment_ingress(cfg, "rec")
                            self.assertIn(expected, str(ctx.exception))
                    finally:
                        for patcher in reversed(patches):
                            patcher.stop()

    def test_other_attachment_field_is_not_selected(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = self.cfg(root)
            fields = self.form_fields(cfg)
            record = self.record(cfg, token="target")
            metadata = [
                {"file_token": "other", "name": "other.md", "extra": "wrong"},
                {"file_token": "target", "name": "input.md", "extra": "right"},
            ]
            with mock.patch.object(router, "list_bitable_fields", return_value=fields), mock.patch.object(
                router, "get_bitable_record", return_value=record
            ), mock.patch.object(router, "get_attachments", return_value=metadata), mock.patch.object(
                router, "download_drive_media", return_value=b"# meeting\n"
            ) as media, mock.patch.object(router, "ensure_form_ingress_folders", return_value=("month", "review")), mock.patch.object(
                router,
                "upload_version_artifact",
                side_effect=[("source", "https://drive/source"), ("review", "https://drive/review")],
            ), mock.patch.object(router, "download_drive_file_version", return_value=b"# meeting\n"), mock.patch.object(
                router, "update_bitable_record"
            ):
                router.process_form_attachment_ingress(cfg, "rec")
            self.assertEqual(media.call_args.args[1], "target")
            self.assertEqual(media.call_args.kwargs["extra"], "right")

    def test_form_ingress_v1_duplicate_and_generation_payload(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = self.cfg(root, meeting_contract_enabled=True)
            fields = self.form_fields(cfg)
            record = self.record(cfg)
            with mock.patch.object(router, "list_bitable_fields", return_value=fields), mock.patch.object(
                router, "get_bitable_record", side_effect=[record, record, record]
            ), mock.patch.object(
                router,
                "get_attachments",
                return_value=[{"file_token": "attachment-1", "name": "input.md", "extra": "opaque"}],
            ), mock.patch.object(
                router, "download_drive_media", return_value=b"# meeting\n"
            ) as media, mock.patch.object(
                router,
                "validate_meeting_contract_content",
                side_effect=AssertionError("form ingress must not gate on body contract"),
            ), mock.patch.object(
                router,
                "validate_meeting_contract_content",
                side_effect=AssertionError("form ingress must not gate on body contract"),
            ), mock.patch.object(
                router, "ensure_form_ingress_folders", return_value=("month", "review")
            ) as ensure_folders, mock.patch.object(
                router,
                "upload_version_artifact",
                side_effect=[("source", "https://drive/source"), ("review", "https://drive/review")],
            ) as upload, mock.patch.object(
                router, "download_drive_file_version", return_value=b"# meeting\n"
            ), mock.patch.object(router, "update_bitable_record") as update:
                first = router.process_form_attachment_ingress(cfg, "rec-1")
                second = router.process_form_attachment_ingress(cfg, "rec-1")
            self.assertEqual(first["data_version"], 1)
            self.assertEqual(second["status"], "already_ingested")
            self.assertEqual(upload.call_count, 2)
            self.assertEqual(update.call_count, 1)
            meeting_uid = update.call_args.args[2]["会议ID"]
            self.assertTrue(meeting_uid.startswith("mtg_"))
            self.assertIn(meeting_uid, upload.call_args_list[0].args[2])
            self.assertIn(meeting_uid, upload.call_args_list[1].args[2])
            self.assertEqual(
                update.call_args.args[2]["会议名"],
                "2026-08-14 - input",
            )
            self.assertEqual(media.call_args.kwargs["extra"], "opaque")
            ensure_folders.assert_called_once_with(cfg, "2026-08", dry_run=False)
            jobs = sorted((Path(root) / "generation" / "pending").glob("*.json"))
            self.assertEqual(len(jobs), 2)
            payload = json.loads(jobs[0].read_text(encoding="utf-8"))
            self.assertEqual(payload["job_version"], 1)
            self.assertEqual(payload["state"], "pending")
            self.assertIn(payload["artifact_type"], {"industry_market_viewpoints", "structured_viewpoints"})
            self.assertEqual(payload["data_version"], 1)

    def test_meeting_name_is_a_compact_label_not_an_identity(self):
        self.assertEqual(
            router.meeting_name_from_filename(
                "2032-08-14 - Alpha 讨论 - 会议纪要 - v1.md",
                "Alpha",
                "2032-08-14",
            ),
            "2032-08-14 - Alpha 讨论",
        )
        self.assertEqual(router.meeting_name_from_filename("会议纪要.md", "Alpha"), "会议纪要")

    def test_form_ingress_same_record_new_attachment_increments_v2(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = self.cfg(root)
            fields = self.form_fields(cfg)
            first = self.record(cfg, token="attachment-1")
            first["fields"]["数据版本"] = 0
            first_uid = "mtg_" + "1" * 32
            second = self.record(cfg, token="attachment-2", uid=first_uid, version=1)
            records = [first, first, second, second]
            attachments = [
                [{"file_token": "attachment-1", "name": "first.md"}],
                [{"file_token": "attachment-2", "name": "second.md"}],
            ]
            with mock.patch.object(router, "list_bitable_fields", return_value=fields), mock.patch.object(
                router, "get_bitable_record", side_effect=records
            ), mock.patch.object(router, "get_attachments", side_effect=attachments), mock.patch.object(
                router, "download_drive_media", side_effect=[b"one", b"two"]
            ), mock.patch.object(router, "ensure_form_ingress_folders", return_value=("month", "review")), mock.patch.object(
                router,
                "upload_version_artifact",
                side_effect=[("s1", "https://s/1"), ("r1", "https://r/1"), ("s2", "https://s/2"), ("r2", "https://r/2")],
            ), mock.patch.object(router, "download_drive_file_version", side_effect=[b"one", b"one", b"two", b"two"]), mock.patch.object(
                router, "update_bitable_record"
            ):
                self.assertEqual(router.process_form_attachment_ingress(cfg, "rec")["data_version"], 1)
                self.assertEqual(router.process_form_attachment_ingress(cfg, "rec")["data_version"], 2)

    def test_router_base_event_routes_by_exact_app_and_table(self):
        cfg = make_config(pipeline_mode="unified")
        other = make_config(
            pipeline_mode="unified", bitable_app_token="other-base", bitable_table_id="other-table"
        )
        event = {
            "header": {"event_type": router.BITABLE_RECORD_CHANGED_EVENT_TYPE},
            "event": {
                "file_token": cfg.bitable_app_token,
                "table_id": cfg.bitable_table_id,
                "action_list": [],
            },
        }
        with mock.patch.object(router, "process_bitable_record_changed_event") as process:
            router.process_router_event([cfg, other], event)
        process.assert_called_once_with(cfg, event)

    def test_get_attachments_uses_base_v3_record_list_and_preserves_extra(self):
        cfg = make_config()
        with mock.patch.object(router, "get_tenant_access_token", return_value="tenant"), mock.patch.object(
            router,
            "request_json",
            return_value={
                "data": {
                    "record_attachment_info": [
                        {
                            "record_id": "rec",
                            "fields": [
                                {
                                    "field_name": cfg.form_attachment_field,
                                    "attachments": [
                                        {"file_token": "a", "name": "a.md", "extra": "opaque"}
                                    ],
                                }
                            ],
                        }
                    ]
                }
            },
        ) as request:
            attachments = router.get_attachments(cfg, "rec", cfg.form_attachment_field)
        self.assertEqual(attachments[0]["extra"], "opaque")
        self.assertEqual(request.call_args.args[1], "POST")
        self.assertIn("/base/v3/bases/", request.call_args.args[2])
        self.assertEqual(request.call_args.kwargs["body"], {"record_id_list": ["rec"]})

    def test_get_attachments_supports_live_record_field_map_and_extra_info(self):
        cfg = make_config()
        with mock.patch.object(router, "get_tenant_access_token", return_value="tenant"), mock.patch.object(
            router,
            "request_json",
            return_value={
                "data": {
                    "attachments": {
                        "rec": {
                            "attachment-field-id": [
                                {
                                    "file_token": "a",
                                    "name": "a.md",
                                    "extra_info": "opaque-live-extra",
                                }
                            ]
                        }
                    }
                }
            },
        ):
            attachments = router.get_attachments(cfg, "rec", cfg.form_attachment_field)
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0]["field_id"], "attachment-field-id")
        self.assertEqual(attachments[0]["extra"], "opaque-live-extra")

    def test_bitable_subscription_omits_event_type_but_folder_keeps_it(self):
        cfg = make_config()
        with mock.patch.object(router, "get_tenant_access_token", return_value="tenant"), mock.patch.object(
            router, "request_json", return_value={}
        ) as request:
            router.subscribe_bitable_record_changes(cfg)
            router.subscribe_folder(cfg, "folder-token")

        self.assertEqual(
            request.call_args_list[0].kwargs["query"],
            {"file_type": "bitable"},
        )
        self.assertEqual(
            request.call_args_list[1].kwargs["query"],
            {
                "file_type": "folder",
                "event_type": router.FILE_CREATED_SUBSCRIBE_EVENT_TYPE,
            },
        )

    def test_form_owned_drive_file_event_is_suppressed_before_binding_scan(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = make_config(
                pipeline_mode="unified",
                form_ingress_enabled=True,
                form_ingestion_receipt_dir=str(Path(root) / "receipts"),
            )
            path = router.form_ingestion_receipt_path(cfg, "rec", "source-token", "b" * 64)
            path.write_text(
                json.dumps({"record_id": "rec", "source_file_token": "source-token"}),
                encoding="utf-8",
            )
            with mock.patch.object(router, "find_unified_records_by_file_token", side_effect=AssertionError):
                self.assertTrue(
                    router.process_unified_file_created_event(
                        cfg,
                        {},
                        fields=[],
                        file_token="source-token",
                        folder_token="source-folder",
                    )
                )

    def test_upload_response_loss_reconciles_exact_name_and_hash(self):
        cfg = make_config()
        content = b"# exact\n"
        with mock.patch.object(router, "find_drive_file_by_name", return_value={
            "type": "file", "name": "exact.md", "token": "existing", "url": "https://drive/existing"
        }), mock.patch.object(
            router, "upload_drive_file_bytes", side_effect=router.FeishuApiError("lost")
        ), mock.patch.object(router, "download_drive_file_version", return_value=content):
            token, url = router.upload_version_artifact(cfg, "folder", "exact.md", content)
        self.assertEqual((token, url), ("existing", "https://drive/existing"))

    def test_base_commit_response_loss_reconciles_fresh_read(self):
        cfg = make_config()
        expected = {"会议ID": "mtg_" + "a" * 32, "数据版本": 1}
        with mock.patch.object(
            router, "update_bitable_record", side_effect=router.FeishuApiError("lost")
        ), mock.patch.object(
            router,
            "get_bitable_record",
            return_value={
                "fields": {
                    "会议ID": expected["会议ID"],
                    "数据版本": 1,
                    cfg.form_attachment_field: [{"file_token": "attachment"}],
                }
            },
        ):
            result = router.update_bitable_record_reconciled_form(
                cfg, "rec", expected, attachment_file_token="attachment"
            )
        self.assertEqual(result, "committed")

    def test_base_commit_response_loss_rejects_changed_attachment(self):
        cfg = make_config()
        expected = {"会议ID": "mtg_" + "a" * 32, "数据版本": 1}
        with mock.patch.object(
            router, "update_bitable_record", side_effect=router.FeishuApiError("lost")
        ), mock.patch.object(
            router,
            "get_bitable_record",
            return_value={
                "fields": {
                    "会议ID": expected["会议ID"],
                    "数据版本": 1,
                    cfg.form_attachment_field: [{"file_token": "new-attachment"}],
                }
            },
        ):
            with self.assertRaisesRegex(router.FeishuApiError, "lost"):
                router.update_bitable_record_reconciled_form(
                    cfg, "rec", expected, attachment_file_token="attachment"
                )

    def test_existing_invalid_meeting_uid_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = self.cfg(root)
            fields = self.form_fields(cfg)
            record = self.record(cfg, uid="legacy-invalid")
            with mock.patch.object(
                router, "list_bitable_fields", return_value=fields
            ), mock.patch.object(
                router, "get_bitable_record", return_value=record
            ), mock.patch.object(
                router,
                "get_attachments",
                return_value=[{"file_token": "attachment-1", "name": "input.md"}],
            ), mock.patch.object(
                router, "download_drive_media", return_value=b"# meeting\n"
            ):
                with self.assertRaisesRegex(
                    ValueError, "form_existing_meeting_uid_invalid"
                ):
                    router.process_form_attachment_ingress(cfg, "rec")

    def test_uploaded_receipt_replay_does_not_upload_again(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = self.cfg(root)
            fields = self.form_fields(cfg)
            content = b"# meeting\n"
            content_hash = hashlib.sha256(content).hexdigest()
            record = self.record(cfg)
            receipt_path = router.form_ingestion_receipt_path(
                cfg, "rec", "attachment-1", content_hash
            )
            router.write_private_json(
                receipt_path,
                {
                    "schema_version": 1,
                    "status": "uploaded",
                    "record_id": "rec",
                    "attachment_file_token": "attachment-1",
                    "attachment_name": "input.md",
                    "content_sha256": content_hash,
                    "meeting_uid": "mtg_" + "a" * 32,
                    "meeting_date": "2026-08-14",
                    "meeting_series": "Alpha",
                    "meeting_type": "公司交流",
                    "data_version": 1,
                    "created_at": "2032-08-14T00:00:00+00:00",
                    "source_file_token": "source",
                    "source_url": "https://drive/source",
                    "review_file_token": "review",
                    "review_url": "https://drive/review",
                    "normalized_file_name": (
                        "2026-08-14 - Alpha - mtg_"
                        + "a" * 32
                        + " - 会议纪要 - v1.md"
                    ),
                },
            )
            with mock.patch.object(router, "list_bitable_fields", return_value=fields), mock.patch.object(
                router, "get_bitable_record", side_effect=[record, record]
            ), mock.patch.object(
                router,
                "get_attachments",
                return_value=[{"file_token": "attachment-1", "name": "input.md"}],
            ), mock.patch.object(
                router, "download_drive_media", return_value=content
            ), mock.patch.object(
                router, "upload_version_artifact"
            ) as upload, mock.patch.object(
                router, "update_bitable_record"
            ) as update:
                result = router.process_form_attachment_ingress(cfg, "rec")
            self.assertEqual(result["status"], "ingested")
            upload.assert_not_called()
            update.assert_called_once()

    def test_committed_receipt_replay_only_repairs_generation_jobs(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = self.cfg(root)
            fields = self.form_fields(cfg)
            content = b"# meeting\n"
            content_hash = hashlib.sha256(content).hexdigest()
            record = self.record(cfg)
            receipt_path = router.form_ingestion_receipt_path(
                cfg, "rec", "attachment-1", content_hash
            )
            router.write_private_json(
                receipt_path,
                {
                    "schema_version": 1,
                    "status": "committed",
                    "record_id": "rec",
                    "attachment_file_token": "attachment-1",
                    "attachment_name": "input.md",
                    "content_sha256": content_hash,
                    "meeting_uid": "mtg_" + "b" * 32,
                    "meeting_date": "2026-08-14",
                    "meeting_series": "Alpha",
                    "meeting_type": "公司交流",
                    "data_version": 1,
                    "created_at": "2032-08-14T00:00:00+00:00",
                    "source_file_token": "source",
                    "source_url": "https://drive/source",
                    "review_file_token": "review",
                    "review_url": "https://drive/review",
                    "normalized_file_name": (
                        "2026-08-14 - Alpha - mtg_"
                        + "b" * 32
                        + " - 会议纪要 - v1.md"
                    ),
                },
            )
            with mock.patch.object(router, "list_bitable_fields", return_value=fields), mock.patch.object(
                router, "get_bitable_record", return_value=record
            ), mock.patch.object(
                router,
                "get_attachments",
                return_value=[{"file_token": "attachment-1", "name": "input.md"}],
            ), mock.patch.object(
                router, "download_drive_media", return_value=content
            ), mock.patch.object(
                router, "upload_version_artifact"
            ) as upload, mock.patch.object(
                router, "update_bitable_record"
            ) as update:
                result = router.process_form_attachment_ingress(cfg, "rec")
            self.assertEqual(result["generation_queued"], [
                "industry_market_viewpoints",
                "structured_viewpoints",
            ])
            upload.assert_not_called()
            update.assert_not_called()
            self.assertEqual(
                len(list((Path(root) / "generation" / "pending").glob("*.json"))),
                2,
            )

    def test_attachment_change_before_commit_is_blocked(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = self.cfg(root)
            fields = self.form_fields(cfg)
            initial = self.record(cfg, token="attachment-1")
            changed = self.record(cfg, token="attachment-2")
            with mock.patch.object(router, "list_bitable_fields", return_value=fields), mock.patch.object(
                router, "get_bitable_record", side_effect=[initial, changed]
            ), mock.patch.object(
                router, "get_attachments", return_value=[{"file_token": "attachment-1", "name": "input.md"}]
            ), mock.patch.object(router, "download_drive_media", return_value=b"# meeting\n"), mock.patch.object(
                router, "ensure_form_ingress_folders", return_value=("month", "review")
            ), mock.patch.object(
                router,
                "upload_version_artifact",
                side_effect=[("source", "https://drive/source"), ("review", "https://drive/review")],
            ), mock.patch.object(router, "download_drive_file_version", return_value=b"# meeting\n"), mock.patch.object(
                router, "update_bitable_record"
            ) as update:
                with self.assertRaisesRegex(router.ArchivePreconditionError, "form_attachment_changed_before_commit"):
                    router.process_form_attachment_ingress(cfg, "rec")
            update.assert_not_called()

    def test_month_and_review_before_folder_creation_is_idempotent(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = self.cfg(root, version_root_folder_token="version-root", version_category="meeting-minutes")
            with mock.patch.object(router, "find_child_folder", side_effect=[None, {"type": "folder", "token": "month"}]), mock.patch.object(
                router, "create_drive_folder", return_value={"token": "month"}
            ) as create, mock.patch.object(
                router, "ensure_version_baseline_folder", return_value="review"
            ) as baseline:
                self.assertEqual(router.ensure_form_ingress_folders(cfg, "2032-08", dry_run=False), ("month", "review"))
                self.assertEqual(router.ensure_form_ingress_folders(cfg, "2032-08", dry_run=False), ("month", "review"))
            self.assertEqual(create.call_count, 1)
            baseline.assert_called_once_with(cfg, "2032-08")


class ApprovalAndHttpTests(unittest.TestCase):
    def test_structured_archive_syncs_approved_viewpoint_count(self):
        cfg = make_config(
            structured_metadata_enabled=True,
            archive_dry_run=False,
            archive_original_time_field="会议日期",
            archive_file_link_field="待审核MD链接",
            archive_file_name_field="表格名",
            archive_status_field="归档状态",
            archive_link_field="审核后归档MD链接",
            archive_time_field="归档时间",
            version_capture_enabled=True,
            version_capture_enforce=True,
            version_baseline_link_field="审核前基线MD链接",
        )
        fields = {
            "已审核": True,
            "归档状态": "待归档",
            "审核后归档MD链接": "",
            "待审核MD链接": "https://example.invalid/file/review",
            "会议日期": 1785859200000,
            "表格名": "review.md",
        }
        approved = "# review\n\n## 观点 1\n\n## 观点 2\n".encode("utf-8")
        with mock.patch.object(
            router, "get_bitable_record", return_value={"fields": fields}
        ), mock.patch.object(
            router, "get_file_meta", return_value={"title": "review.md"}
        ), mock.patch.object(
            router,
            "capture_baseline_for_record_with_failure_status",
            return_value={"sha256": "baseline"},
        ), mock.patch.object(
            router, "ensure_child_folder", return_value="month-folder"
        ), mock.patch.object(
            router, "latest_file_version", return_value=({"version": "2"}, approved)
        ), mock.patch.object(
            router,
            "upload_version_artifact",
            return_value=("archive-token", "https://example.invalid/file/archive"),
        ), mock.patch.object(router, "update_bitable_record") as update:
            result = router._archive_record_unlocked(cfg, "rec-1")

        self.assertEqual(result["status"], "archived")
        self.assertEqual(update.call_args_list[-1].args[2][router.FIELD_VIEWPOINT_COUNT], 2)

    def test_invalid_action_list_log_does_not_echo_event_content(self):
        cfg = make_config()
        event = {
            "event": {
                "file_token": cfg.bitable_app_token,
                "table_id": cfg.bitable_table_id,
                "action_list": {"secret": "do-not-log-this"},
            }
        }
        with self.assertLogs(level="INFO") as captured:
            router.process_bitable_record_changed_event(cfg, event)
        joined = "\n".join(captured.output)
        self.assertIn("invalid action_list type", joined)
        self.assertNotIn("do-not-log-this", joined)

    def test_changed_event_uses_configured_approval_field(self):
        cfg = make_config(archive_review_field="人工确认")
        event = {
            "header": {"event_type": router.BITABLE_RECORD_CHANGED_EVENT_TYPE},
            "event": {
                "file_token": cfg.bitable_app_token,
                "table_id": cfg.bitable_table_id,
                "action_list": [{"record_id": "rec-123"}],
            },
        }
        record = {
            "fields": {
                "人工确认": True,
                "已审核": False,
                cfg.archive_file_link_field: "https://example.invalid/file/abc",
                cfg.archive_link_field: "",
            }
        }
        with mock.patch.object(router, "get_bitable_record", return_value=record), mock.patch.object(
            router,
            "archive_record_with_failure_status",
            return_value={"status": "dry_run"},
        ) as archive:
            router.process_bitable_record_changed_event(cfg, event)
        archive.assert_called_once_with(cfg, "rec-123")

    def request(self, server, method, path, body=b"", headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, payload

    def test_http_boundary_and_precondition_mapping(self):
        cfg = make_config(archive_max_body_bytes=32)
        server = router.ThreadingHTTPServer(("127.0.0.1", 0), router.make_archive_handler(cfg))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            status, payload = self.request(server, "GET", "/healthz")
            self.assertEqual((status, payload), (200, {"ok": True, "ready": True}))

            body = json.dumps({"record_id": "rec-123"}).encode("utf-8")
            status, payload = self.request(
                server,
                "POST",
                "/archive",
                body,
                {"Content-Type": "application/json", "X-Archive-Token": "wrong"},
            )
            self.assertEqual((status, payload["error"]), (401, "unauthorized"))

            status, payload = self.request(
                server,
                "POST",
                "/archive",
                b"x" * 33,
                {"Content-Type": "application/json", "X-Archive-Token": "test-token"},
            )
            self.assertEqual((status, payload["error"]), (413, "request_too_large"))

            with mock.patch.object(
                router,
                "archive_record_with_failure_status",
                side_effect=router.ArchivePreconditionError("approval_required"),
            ):
                status, payload = self.request(
                    server,
                    "POST",
                    "/archive",
                    body,
                    {"Content-Type": "application/json", "X-Archive-Token": "test-token"},
                )
            self.assertEqual((status, payload["error"]), (409, "approval_required"))

            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            connection.putrequest("POST", "/archive")
            connection.putheader("Content-Type", "application/json")
            connection.putheader("X-Archive-Token", "test-token")
            connection.putheader("Content-Length", "2")
            connection.putheader("Content-Length", "3")
            connection.endheaders(b"{}")
            response = connection.getresponse()
            duplicate_payload = json.loads(response.read().decode("utf-8"))
            connection.close()
            self.assertEqual((response.status, duplicate_payload["error"]), (400, "ambiguous_content_length"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
