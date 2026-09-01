from __future__ import annotations

import email.message
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock
import urllib.parse

import feishu_upload_service as service


def encode_api_multipart(
    file_name: str,
    data: bytes,
    *,
    fields: dict[str, str],
) -> tuple[str, bytes]:
    boundary = "test-api-boundary"
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.extend(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"),
                value.encode("utf-8"),
                b"\r\n",
            ]
        )
    parts.extend(
        [
            f"--{boundary}\r\n".encode("utf-8"),
            f'Content-Disposition: form-data; name="file"; filename="{file_name}"\r\n'.encode("utf-8"),
            b"Content-Type: text/markdown\r\n\r\n",
            data,
            b"\r\n",
            f"--{boundary}--\r\n".encode("utf-8"),
        ]
    )
    return f"multipart/form-data; boundary={boundary}", b"".join(parts)


class MeetingContractGateTests(unittest.TestCase):
    def config(self, validator: Path, digest: str) -> service.Config:
        return service.Config(
            app_id="app",
            app_secret="secret",
            parent_folder_token="folder",
            user_db_path=Path("users.json"),
            max_upload_bytes=1024,
            file_url_base="https://example.test/file/",
            meeting_contract_enabled=True,
            meeting_contract_validator=str(validator),
            meeting_contract_validator_sha256=digest,
        )

    def test_pinned_validator_accepts_contract_compliant_content(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            validator = Path(tmpdir) / "validator.py"
            validator.write_text('print("{\\"ok\\": true}")\n', encoding="utf-8")
            digest = hashlib.sha256(validator.read_bytes()).hexdigest()

            service.validate_meeting_contract(b"# meeting\n", self.config(validator, digest))

    def test_validator_rejection_blocks_upload_with_422(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            validator = Path(tmpdir) / "validator.py"
            validator.write_text('import sys\nprint("{\\"ok\\": false}")\nsys.exit(1)\n', encoding="utf-8")
            digest = hashlib.sha256(validator.read_bytes()).hexdigest()

            with self.assertRaises(service.UploadError) as caught:
                service.validate_meeting_contract(b"invalid\n", self.config(validator, digest))

        self.assertEqual(caught.exception.error_code, "meeting_contract_invalid")
        self.assertEqual(caught.exception.http_status, 422)

    def test_validator_hash_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            validator = Path(tmpdir) / "validator.py"
            validator.write_text('print("{\\"ok\\": true}")\n', encoding="utf-8")

            with self.assertRaises(service.UploadError) as caught:
                service.validate_meeting_contract(b"# meeting\n", self.config(validator, "0" * 64))

        self.assertEqual(caught.exception.error_code, "meeting_contract_config_invalid")
        self.assertEqual(caught.exception.http_status, 500)

    def test_contract_cannot_be_disabled(self):
        config = self.config(Path("/missing"), "")
        config.meeting_contract_enabled = False
        self.assertFalse(service.meeting_contract_config_ready(config))
        with self.assertRaisesRegex(service.UploadError, "cannot be disabled"):
            service.validate_meeting_contract(b"# meeting\n", config)


class UserDatabaseSafetyTests(unittest.TestCase):
    def test_concurrent_updates_preserve_all_users_and_private_modes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "data" / "upload_users.json"

            def append_user(index: int) -> None:
                with service.update_user_db(path) as data:
                    data["users"].append(
                        {
                            "user_id": f"user-{index}",
                            "enabled": True,
                            "token_hash": service.hash_token(f"token-{index}"),
                        }
                    )

            threads = [threading.Thread(target=append_user, args=(index,)) for index in range(12)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)

            users = service.load_user_db(path)["users"]
            self.assertEqual({user["user_id"] for user in users}, {f"user-{index}" for index in range(12)})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.with_name(path.name + ".lock").stat().st_mode), 0o600)
            self.assertFalse(list(path.parent.glob(".*.tmp")))

    def test_secret_file_is_atomically_written_with_private_mode(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "secrets" / "token.txt"
            service.write_secret_file(str(path), "sensitive-token")
            self.assertEqual(path.read_text(encoding="utf-8"), "sensitive-token\n")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_token_is_staged_before_activation_and_remains_recoverable_on_commit_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            final = Path(tmpdir) / "secrets" / "token.txt"
            final_path, staged = service.stage_secret_file(str(final), "new-token")
            self.assertFalse(final.exists())
            self.assertEqual(staged.read_text(encoding="utf-8"), "new-token\n")
            self.assertEqual(stat.S_IMODE(staged.stat().st_mode), 0o600)

            target = final.parent / "unexpected-target"
            target.write_text("old\n", encoding="utf-8")
            final.symlink_to(target)
            with self.assertRaises(service.UploadError) as caught:
                service.commit_staged_secret(final_path, staged)
            self.assertEqual(caught.exception.error_code, "token_file_commit_failed")
            self.assertTrue(staged.exists())
            service.discard_staged_secret(staged)

    def test_user_db_uncertain_commit_preserves_the_staged_token(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            users = root / "data" / "upload_users.json"
            service.save_user_db(
                users,
                {
                    "version": 1,
                    "users": [
                        {
                            "user_id": "one",
                            "enabled": True,
                            "token_hash": service.hash_token("old-token"),
                        }
                    ],
                },
            )
            config = service.Config(
                app_id="app",
                app_secret="secret",
                parent_folder_token="folder",
                user_db_path=users,
                max_upload_bytes=1024,
                file_url_base="https://example.test/file/",
            )
            args = SimpleNamespace(
                users_path=str(users),
                user_id="one",
                write_token_file=str(root / "secrets" / "token.txt"),
            )
            real_sync = service.fsync_directory
            sync_calls = 0

            def fail_user_db_sync(path: Path) -> None:
                nonlocal sync_calls
                sync_calls += 1
                if sync_calls == 2:
                    raise OSError("injected directory sync failure")
                real_sync(path)

            with mock.patch.object(service.Config, "from_env", return_value=config), mock.patch.object(
                service,
                "fsync_directory",
                side_effect=fail_user_db_sync,
            ), mock.patch("builtins.print"):
                with self.assertRaises(service.UploadError) as caught:
                    service.rotate_user(args)

            self.assertEqual(caught.exception.error_code, "user_db_commit_uncertain")
            staged = list((root / "secrets").glob(".token.txt.pending.*.token"))
            self.assertEqual(len(staged), 1)
            recovered_token = staged[0].read_text(encoding="utf-8").strip()
            stored_hash = service.load_user_db(users)["users"][0]["token_hash"]
            self.assertTrue(service.verify_token_hash(recovered_token, stored_hash))
            service.discard_staged_secret(staged[0])

    def test_concurrent_rotations_keep_the_database_and_token_file_consistent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            users = root / "data" / "upload_users.json"
            final_token = root / "secrets" / "token.txt"
            service.save_user_db(
                users,
                {
                    "version": 1,
                    "users": [
                        {
                            "user_id": "one",
                            "enabled": True,
                            "token_hash": service.hash_token("old-token"),
                        }
                    ],
                },
            )
            config = service.Config(
                app_id="app",
                app_secret="secret",
                parent_folder_token="folder",
                user_db_path=users,
                max_upload_bytes=1024,
                file_url_base="https://example.test/file/",
            )
            args = SimpleNamespace(
                users_path=str(users),
                user_id="one",
                write_token_file=str(final_token),
            )
            first_commit_started = threading.Event()
            second_commit_started = threading.Event()
            real_commit = service.commit_staged_secret
            errors: list[BaseException] = []

            def token_for_thread() -> str:
                return "fmu_" + threading.current_thread().name

            def ordered_commit(final_path: Path, staged_path: Path) -> None:
                token = staged_path.read_text(encoding="utf-8")
                if "rotate-A" in token:
                    first_commit_started.set()
                    second_commit_started.wait(timeout=0.2)
                else:
                    second_commit_started.set()
                real_commit(final_path, staged_path)

            def rotate() -> None:
                try:
                    service.rotate_user(args)
                except BaseException as exc:
                    errors.append(exc)

            with mock.patch.object(service.Config, "from_env", return_value=config), mock.patch.object(
                service,
                "make_token",
                side_effect=token_for_thread,
            ), mock.patch.object(
                service,
                "commit_staged_secret",
                side_effect=ordered_commit,
            ), mock.patch("builtins.print"):
                first = threading.Thread(target=rotate, name="rotate-A")
                second = threading.Thread(target=rotate, name="rotate-B")
                first.start()
                self.assertTrue(first_commit_started.wait(timeout=2))
                second.start()
                first.join(timeout=3)
                second.join(timeout=3)

            self.assertFalse(errors)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            final_value = final_token.read_text(encoding="utf-8").strip()
            stored_hash = service.load_user_db(users)["users"][0]["token_hash"]
            self.assertTrue(service.verify_token_hash(final_value, stored_hash))

    def test_upload_month_lock_serializes_same_month(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = service.Config(
                app_id="app",
                app_secret="secret",
                parent_folder_token="folder",
                user_db_path=Path(tmpdir) / "data" / "users.json",
                max_upload_bytes=1024,
                file_url_base="https://example.test/file/",
            )
            state = {"active": 0, "max_active": 0}
            guard = threading.Lock()

            def hold_lock() -> None:
                with service.upload_month_lock(config, "2032-07"):
                    with guard:
                        state["active"] += 1
                        state["max_active"] = max(state["max_active"], state["active"])
                    time.sleep(0.03)
                    with guard:
                        state["active"] -= 1

            threads = [threading.Thread(target=hold_lock) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)
            lock_files = list((config.user_db_path.parent / "upload-operation-locks").glob("*.lock"))
            self.assertEqual(state["max_active"], 1)
            self.assertEqual(len(lock_files), 1)
            self.assertEqual(stat.S_IMODE(lock_files[0].stat().st_mode), 0o600)


class UploadIdempotencyTests(unittest.TestCase):
    KEY = "fmu-v1-0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.config = service.Config(
            app_id="app",
            app_secret="secret",
            parent_folder_token="parent-folder",
            user_db_path=root / "data" / "users.json",
            max_upload_bytes=1024 * 1024,
            file_url_base="https://example.test/file/",
            baseline_parent_folder_token="baseline-parent",
            meeting_base_app_token="base-app",
            meeting_base_table_id="table-id",
            generation_job_spool_path=root / "data" / "jobs",
            meeting_registry_path=root / "data" / "registry",
        )
        self.user = {"user_id": "one", "name": "Reviewer"}
        self.upload = service.UploadedFile(
            field_name="file",
            file_name="2032-07-14 - review.md",
            content_type="text/markdown",
            data=b"# reviewed meeting\n",
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_transfer_output_owner_preserves_app_full_access_and_location(self):
        self.config.output_owner_open_id = "ou_owner"
        with mock.patch.object(service, "feishu_request_json", return_value={}) as request:
            service.transfer_output_owner(self.config, "tenant-token", "file/one")

        method, url = request.call_args.args[:2]
        self.assertEqual(method, "POST")
        self.assertIn("/drive/v1/permissions/file%2Fone/members/transfer_owner?", url)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.assertEqual(query["type"], ["file"])
        self.assertEqual(query["remove_old_owner"], ["false"])
        self.assertEqual(query["old_owner_perm"], ["full_access"])
        self.assertEqual(query["stay_put"], ["true"])
        body = json.loads(request.call_args.kwargs["body"].decode("utf-8"))
        self.assertEqual(body, {"member_id": "ou_owner", "member_type": "openid"})
        self.assertEqual(
            request.call_args.kwargs["headers"]["Authorization"],
            "Bearer tenant-token",
        )

    def test_owner_transfer_failure_has_distinct_error(self):
        self.config.output_owner_open_id = "ou_owner"
        with mock.patch.object(
            service,
            "feishu_request_json",
            side_effect=service.UploadError("feishu_permission_denied", "denied", 502),
        ), self.assertRaises(service.UploadError) as caught:
            service.transfer_output_owner(self.config, "tenant-token", "file-one")

        self.assertEqual(caught.exception.error_code, "owner_transfer_failed")
        self.assertEqual(caught.exception.http_status, 502)

    @staticmethod
    def headers(*pairs: tuple[str, str]) -> email.message.Message:
        result = email.message.Message()
        for name, value in pairs:
            result[name] = value
        return result

    def test_idempotency_header_is_optional_unique_and_strict(self):
        self.assertEqual(service.extract_idempotency_key(self.headers()), "")
        self.assertEqual(
            service.extract_idempotency_key(self.headers(("Idempotency-Key", self.KEY))),
            self.KEY,
        )
        duplicate = self.headers(("Idempotency-Key", self.KEY), ("Idempotency-Key", self.KEY))
        with self.assertRaises(service.UploadError) as caught:
            service.extract_idempotency_key(duplicate)
        self.assertEqual(caught.exception.error_code, "ambiguous_idempotency_key")
        for value in ("short", "x" * 129, "contains space 123456"):
            with self.subTest(value=value):
                with self.assertRaises(service.UploadError) as invalid:
                    service.extract_idempotency_key(self.headers(("Idempotency-Key", value)))
                self.assertEqual(invalid.exception.error_code, "invalid_idempotency_key")

    def test_completed_upload_is_replayed_without_second_remote_write(self):
        with mock.patch.object(service, "list_folder", return_value=[]), mock.patch.object(
            service,
            "upload_file",
            return_value="file-one",
        ) as upload_file, mock.patch.object(
            service,
            "resolve_uploaded_url",
            return_value="https://example.test/file/file-one",
        ):
            first = service.idempotent_upload(
                self.config,
                self.user,
                self.KEY,
                "tenant",
                "2032-07",
                "month-folder",
                self.upload,
                "request-one",
            )
            second = service.idempotent_upload(
                self.config,
                self.user,
                self.KEY,
                "tenant",
                "2032-07",
                "month-folder",
                self.upload,
                "request-two",
            )

        self.assertEqual(first["idempotency_status"], "created")
        self.assertEqual(second["idempotency_status"], "replayed")
        self.assertEqual(second["request_id"], "request-two")
        upload_file.assert_called_once()
        record_path = service.idempotency_record_path(self.config, "one", self.KEY)
        self.assertEqual(stat.S_IMODE(record_path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(record_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(record_path.with_suffix(".lock").stat().st_mode), 0o600)
        self.assertNotIn(self.KEY, record_path.read_text(encoding="utf-8"))

    def test_directory_fsync_uncertainty_is_never_treated_as_durable_success(self):
        path = service.idempotency_record_path(self.config, "one", self.KEY)
        record = {
            "version": service.IDEMPOTENCY_RECORD_VERSION,
            "status": "in_flight",
            "fingerprint": "a" * 64,
        }
        with mock.patch.object(
            service,
            "atomic_private_write",
            side_effect=service.AtomicCommitUncertain("directory sync failed"),
        ):
            with self.assertRaises(service.UploadError) as caught:
                service.save_idempotency_record(path, record)

        self.assertEqual(caught.exception.error_code, "idempotency_store_uncertain")
        self.assertEqual(caught.exception.http_status, 503)

    def test_completed_receipt_failure_is_uncertain_and_retry_does_not_upload_again(self):
        real_atomic_write = service.atomic_private_write
        write_calls = 0

        def fail_completed_write(path, text):
            nonlocal write_calls
            write_calls += 1
            if write_calls == 2:
                raise OSError("receipt write failed")
            return real_atomic_write(path, text)

        item = {
            "type": "file",
            "name": self.upload.file_name,
            "token": "file-one",
            "url": "https://example.test/file/file-one",
        }
        with mock.patch.object(service, "atomic_private_write", side_effect=fail_completed_write), mock.patch.object(
            service, "list_folder", return_value=[]
        ), mock.patch.object(service, "upload_file", return_value="file-one") as upload_file, mock.patch.object(
            service, "resolve_uploaded_url", return_value=item["url"]
        ):
            with self.assertRaises(service.UploadError) as caught:
                service.idempotent_upload(
                    self.config,
                    self.user,
                    self.KEY,
                    "tenant",
                    "2032-07",
                    "month-folder",
                    self.upload,
                    "request-one",
                )

        self.assertEqual(caught.exception.error_code, "idempotency_store_uncertain")
        self.assertEqual(caught.exception.http_status, 503)

        with mock.patch.object(service, "list_folder", return_value=[item]), mock.patch.object(
            service, "download_drive_file", return_value=self.upload.data
        ), mock.patch.object(service, "upload_file") as retry_upload:
            result = service.idempotent_upload(
                self.config,
                self.user,
                self.KEY,
                "tenant",
                "2032-07",
                "month-folder",
                self.upload,
                "request-two",
            )

        self.assertEqual(result["idempotency_status"], "reconciled")
        upload_file.assert_called_once()
        retry_upload.assert_not_called()

    def test_same_key_with_different_request_is_rejected(self):
        with mock.patch.object(service, "list_folder", return_value=[]), mock.patch.object(
            service,
            "upload_file",
            return_value="file-one",
        ), mock.patch.object(
            service,
            "resolve_uploaded_url",
            return_value="https://example.test/file/file-one",
        ):
            service.idempotent_upload(
                self.config,
                self.user,
                self.KEY,
                "tenant",
                "2032-07",
                "month-folder",
                self.upload,
                "request-one",
            )
            changed = service.UploadedFile("file", self.upload.file_name, "text/markdown", b"different\n")
            with self.assertRaises(service.UploadError) as caught:
                service.idempotent_upload(
                    self.config,
                    self.user,
                    self.KEY,
                    "tenant",
                    "2032-07",
                    "month-folder",
                    changed,
                    "request-two",
                )

        self.assertEqual(caught.exception.error_code, "idempotency_key_conflict")
        self.assertEqual(caught.exception.http_status, 409)

    def test_concurrent_same_key_uploads_once(self):
        calls = 0
        guard = threading.Lock()

        def upload_once(*args, **kwargs):
            nonlocal calls
            with guard:
                calls += 1
            time.sleep(0.04)
            return "file-one"

        results: list[dict[str, object]] = []
        errors: list[BaseException] = []

        def run(request_id: str) -> None:
            try:
                results.append(
                    service.idempotent_upload(
                        self.config,
                        self.user,
                        self.KEY,
                        "tenant",
                        "2032-07",
                        "month-folder",
                        self.upload,
                        request_id,
                    )
                )
            except BaseException as exc:
                errors.append(exc)

        with mock.patch.object(service, "list_folder", return_value=[]), mock.patch.object(
            service,
            "upload_file",
            side_effect=upload_once,
        ), mock.patch.object(
            service,
            "resolve_uploaded_url",
            return_value="https://example.test/file/file-one",
        ):
            threads = [threading.Thread(target=run, args=(f"request-{index}",)) for index in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=3)

        self.assertFalse(errors)
        self.assertEqual(calls, 1)
        self.assertEqual({str(item["idempotency_status"]) for item in results}, {"created", "replayed"})

    def write_in_flight_record(self) -> Path:
        content_sha = hashlib.sha256(self.upload.data).hexdigest()
        fingerprint = service.idempotency_fingerprint(
            user_id="one",
            file_name=self.upload.file_name,
            content_sha256=content_sha,
            month="2032-07",
            parent_folder_token=self.config.parent_folder_token,
        )
        path = service.idempotency_record_path(self.config, "one", self.KEY)
        service.save_idempotency_record(
            path,
            {
                "version": service.IDEMPOTENCY_RECORD_VERSION,
                "status": "in_flight",
                "fingerprint": fingerprint,
                "content_sha256": content_sha,
                "uploader": "Reviewer",
                "original_file_name": self.upload.file_name,
                "uploaded_file_name": self.upload.file_name,
                "month": "2032-07",
                "parent_folder_token": "...lder",
                "month_folder_token": "month-folder",
                "first_request_id": "request-one",
                "created_at": service.utc_now(),
            },
        )
        return path

    def test_in_flight_remote_hash_match_is_reconciled(self):
        self.write_in_flight_record()
        item = {
            "type": "file",
            "name": self.upload.file_name,
            "token": "file-one",
            "url": "https://example.test/file/file-one",
        }
        with mock.patch.object(service, "list_folder", return_value=[item]), mock.patch.object(
            service,
            "download_drive_file",
            return_value=self.upload.data,
        ), mock.patch.object(service, "upload_file") as upload_file:
            result = service.idempotent_upload(
                self.config,
                self.user,
                self.KEY,
                "tenant",
                "2032-07",
                "month-folder",
                self.upload,
                "request-two",
            )

        self.assertEqual(result["idempotency_status"], "reconciled")
        upload_file.assert_not_called()

    def test_remote_effect_timeout_is_reconciled_instead_of_becoming_internal_error(self):
        item = {
            "type": "file",
            "name": self.upload.file_name,
            "token": "file-one",
            "url": "https://example.test/file/file-one",
        }
        with mock.patch.object(service, "list_folder", side_effect=[[], [item]]), mock.patch.object(
            service, "upload_file", return_value="file-one"
        ) as upload_file, mock.patch.object(
            service, "resolve_uploaded_url", side_effect=TimeoutError("response lost")
        ), mock.patch.object(service, "download_drive_file", return_value=self.upload.data):
            result = service.idempotent_upload(
                self.config,
                self.user,
                self.KEY,
                "tenant",
                "2032-07",
                "month-folder",
                self.upload,
                "request-one",
            )

        self.assertEqual(result["idempotency_status"], "reconciled")
        upload_file.assert_called_once()

    def test_upload_and_reconciliation_timeouts_fail_closed_as_uncertain(self):
        with mock.patch.object(service, "list_folder", side_effect=[[], []]), mock.patch.object(
            service, "upload_file", side_effect=TimeoutError("response lost")
        ):
            with self.assertRaises(service.UploadError) as upload_uncertain:
                service.idempotent_upload(
                    self.config,
                    self.user,
                    self.KEY,
                    "tenant",
                    "2032-07",
                    "month-folder",
                    self.upload,
                    "request-one",
                )
        self.assertEqual(upload_uncertain.exception.error_code, "idempotency_outcome_uncertain")

        item = {"type": "file", "name": self.upload.file_name, "token": "file-one"}
        with mock.patch.object(service, "list_folder", return_value=[item]), mock.patch.object(
            service, "download_drive_file", side_effect=TimeoutError("download timed out")
        ):
            with self.assertRaises(service.UploadError) as download_uncertain:
                service.idempotent_upload(
                    self.config,
                    self.user,
                    self.KEY,
                    "tenant",
                    "2032-07",
                    "month-folder",
                    self.upload,
                    "request-two",
                )
        self.assertEqual(download_uncertain.exception.error_code, "idempotency_outcome_uncertain")

    def test_in_flight_mismatch_and_absence_fail_closed(self):
        self.write_in_flight_record()
        mismatch = {
            "type": "file",
            "name": self.upload.file_name,
            "token": "file-one",
            "url": "https://example.test/file/file-one",
        }
        with mock.patch.object(service, "list_folder", return_value=[mismatch]), mock.patch.object(
            service,
            "download_drive_file",
            return_value=b"different\n",
        ), mock.patch.object(service, "upload_file") as upload_file:
            with self.assertRaises(service.UploadError) as conflict:
                service.idempotent_upload(
                    self.config,
                    self.user,
                    self.KEY,
                    "tenant",
                    "2032-07",
                    "month-folder",
                    self.upload,
                    "request-two",
                )
        self.assertEqual(conflict.exception.error_code, "idempotency_reconciliation_conflict")
        upload_file.assert_not_called()

        with mock.patch.object(service, "list_folder", return_value=[]), mock.patch.object(
            service,
            "upload_file",
        ) as upload_file:
            with self.assertRaises(service.UploadError) as uncertain:
                service.idempotent_upload(
                    self.config,
                    self.user,
                    self.KEY,
                    "tenant",
                    "2032-07",
                    "month-folder",
                    self.upload,
                    "request-three",
                )
        self.assertEqual(uncertain.exception.error_code, "idempotency_outcome_uncertain")
        self.assertEqual(uncertain.exception.http_status, 503)
        upload_file.assert_not_called()

    def test_dry_run_with_key_creates_no_receipt(self):
        service.save_user_db(
            self.config.user_db_path,
            {
                "version": 1,
                "users": [
                    {
                        "user_id": "one",
                        "name": "Reviewer",
                        "enabled": True,
                        "token_hash": service.hash_token("upload-token"),
                    }
                ],
            },
        )
        content_type, body = encode_api_multipart(
            self.upload.file_name,
            self.upload.data,
            fields={
                "meeting_date": "2032-07-14",
                "meeting_series": "示例研究周会",
                "meeting_type": "多人复盘会",
                "dry_run": "true",
            },
        )
        handler = object.__new__(service.UploadHandler)
        handler.config = self.config
        handler.headers = self.headers(
            ("Authorization", "Bearer upload-token"),
            ("Idempotency-Key", self.KEY),
            ("Content-Length", str(len(body))),
            ("Content-Type", content_type),
        )
        handler.rfile = io.BytesIO(body)
        parsed = service.urllib.parse.urlparse("/api/upload")

        def list_items(config, tenant, folder):
            if folder == self.config.parent_folder_token:
                return [{"name": "2032-07", "type": "folder", "token": "month-folder"}]
            if folder == self.config.baseline_parent_folder_token:
                return [{"name": "2032-07", "type": "folder", "token": "baseline-month"}]
            return []

        with mock.patch.object(
            service,
            "validate_meeting_contract",
            side_effect=AssertionError("upload path must not enforce content structure"),
        ), mock.patch.object(
            service,
            "get_tenant_access_token",
            return_value="tenant",
        ), mock.patch.object(service, "list_folder", side_effect=list_items), mock.patch.object(
            service,
            "upload_file",
        ) as upload_file:
            result = handler.handle_upload(parsed, "request-one")

        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["idempotency_status"], "dry_run")
        self.assertEqual(
            result["normalized_file_name"],
            "2032-07-14 - 示例研究周会 - 会议纪要 - v1.md",
        )
        self.assertFalse((self.config.user_db_path.parent / "meeting-ingestion-receipts").exists())
        upload_file.assert_not_called()

    def test_write_without_idempotency_key_is_rejected_before_remote_calls(self):
        service.save_user_db(
            self.config.user_db_path,
            {
                "version": 1,
                "users": [
                    {
                        "user_id": "one",
                        "name": "Reviewer",
                        "enabled": True,
                        "token_hash": service.hash_token("upload-token"),
                    }
                ],
            },
        )
        content_type, body = encode_api_multipart(
            self.upload.file_name,
            self.upload.data,
            fields={
                "meeting_date": "2032-07-14",
                "meeting_series": "示例研究周会",
                "meeting_type": "多人复盘会",
            },
        )
        handler = object.__new__(service.UploadHandler)
        handler.config = self.config
        handler.headers = self.headers(
            ("Authorization", "Bearer upload-token"),
            ("Content-Length", str(len(body))),
            ("Content-Type", content_type),
        )
        handler.rfile = io.BytesIO(body)
        with mock.patch.object(service, "get_tenant_access_token") as tenant, mock.patch.object(
            service, "upload_file"
        ) as upload_file:
            with self.assertRaises(service.UploadError) as caught:
                handler.handle_upload(service.urllib.parse.urlparse("/api/upload"), "request-one")
        self.assertEqual(caught.exception.error_code, "missing_idempotency_key")
        tenant.assert_not_called()
        upload_file.assert_not_called()


class MeetingIngestionTransactionTests(unittest.TestCase):
    KEY = "ingest-v1-0123456789abcdef0123456789abcdef"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.config = service.Config(
            app_id="app",
            app_secret="secret",
            parent_folder_token="current-parent",
            baseline_parent_folder_token="baseline-parent",
            meeting_base_app_token="base-app",
            meeting_base_table_id="table-id",
            user_db_path=root / "data" / "users.json",
            max_upload_bytes=1024 * 1024,
            file_url_base="https://example.test/file/",
            generation_job_spool_path=root / "data" / "jobs",
            meeting_registry_path=root / "data" / "registry",
        )
        self.user = {"user_id": "one", "name": "Reviewer"}
        self.upload = service.UploadedFile(
            field_name="file",
            file_name="arbitrary-name.md",
            content_type="text/markdown",
            data=b"# meeting\n",
        )
        self.metadata = {
            "meeting_date": "2032-08-13",
            "meeting_series": "示例研究周会",
            "meeting_type": "多人复盘会",
            "meeting_uid": "",
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_updated_reviewed_source_and_branches_become_needs_review(self):
        receipt = {
            "meeting_uid": "mtg_550e8400e29b41d4a716446655440000",
            "meeting_date": "2032-08-13",
            "meeting_series": "示例研究周会",
            "meeting_type": "多人复盘会",
            "data_version": 2,
            "normalized_file_name": "2032-08-13 - 示例研究周会 - 会议纪要 - v2.md",
            "url": "https://example.test/file/source-v2",
            "baseline_file_name": "2032-08-13 - 示例研究周会 - 会议纪要 - v2 - 审核前.md",
            "baseline_url": "https://example.test/file/source-v2-baseline",
        }
        existing = {
            "fields": {
                "源纪要审核": "已审核",
                "行业与市场观点审核": "已审核",
                "标的观点审核": "未审核",
            }
        }
        fields = service.meeting_record_fields(receipt, existing)
        self.assertEqual(fields["源纪要审核"], "需重审")
        self.assertEqual(fields["行业与市场观点审核"], "需重审")
        self.assertEqual(fields["标的观点审核"], "未审核")

    def month_folders(self, _config, _tenant, folder):
        if folder == self.config.parent_folder_token:
            return [{"name": "2032-08", "type": "folder", "token": "current-month"}]
        if folder == self.config.baseline_parent_folder_token:
            return [{"name": "2032-08", "type": "folder", "token": "baseline-month"}]
        return []

    def test_first_ingestion_returns_uid_commits_registry_and_queues_two_jobs(self):
        with mock.patch.object(service, "list_folder", side_effect=self.month_folders), mock.patch.object(
            service,
            "upload_file_confirmed",
            return_value=("source-token", "https://example.test/source"),
        ) as upload, mock.patch.object(
            service,
            "copy_file_confirmed",
            return_value=("baseline-token", "https://example.test/baseline"),
        ) as baseline, mock.patch.object(
            service, "commit_meeting_record", return_value="record-one"
        ) as commit:
            result = service.idempotent_ingestion(
                self.config,
                self.user,
                self.KEY,
                "tenant",
                self.metadata,
                self.upload,
                "request-one",
            )

        self.assertEqual(result["status"], "created")
        self.assertRegex(result["meeting_uid"], r"^mtg_[0-9a-f]{32}$")
        self.assertEqual(result["record_id"], "record-one")
        self.assertEqual(result["data_version"], 1)
        self.assertEqual(
            result["normalized_file_name"],
            "2032-08-13 - 示例研究周会 - 会议纪要 - v1.md",
        )
        self.assertEqual(
            result["generation_queued"],
            ["industry_market_viewpoints", "structured_viewpoints"],
        )
        upload.assert_called_once()
        baseline.assert_called_once()
        commit.assert_called_once()

        registry = service.load_meeting_registry(self.config, result["meeting_uid"])
        self.assertEqual(registry["source_md_sha256"], hashlib.sha256(self.upload.data).hexdigest())
        jobs = sorted((self.config.generation_job_spool_path / "pending").glob("*.json"))
        self.assertEqual(len(jobs), 2)
        for path in jobs:
            job = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(job["meeting_uid"], result["meeting_uid"])
            self.assertEqual(job["record_id"], "record-one")
            self.assertEqual(job["input_file_token"], "source-token")

    def test_same_key_replays_without_remote_or_local_duplicate_effects(self):
        with mock.patch.object(service, "list_folder", side_effect=self.month_folders), mock.patch.object(
            service,
            "upload_file_confirmed",
            return_value=("source-token", "https://example.test/source"),
        ), mock.patch.object(
            service,
            "copy_file_confirmed",
            return_value=("baseline-token", "https://example.test/baseline"),
        ), mock.patch.object(service, "commit_meeting_record", return_value="record-one"):
            first = service.idempotent_ingestion(
                self.config,
                self.user,
                self.KEY,
                "tenant",
                self.metadata,
                self.upload,
                "request-one",
            )
        with mock.patch.object(service, "list_folder") as list_folder, mock.patch.object(
            service, "upload_file_confirmed"
        ) as upload, mock.patch.object(service, "commit_meeting_record") as commit:
            replay = service.idempotent_ingestion(
                self.config,
                self.user,
                self.KEY,
                "tenant",
                self.metadata,
                self.upload,
                "request-two",
            )
        self.assertEqual(replay["meeting_uid"], first["meeting_uid"])
        self.assertEqual(replay["idempotency_status"], "replayed")
        list_folder.assert_not_called()
        upload.assert_not_called()
        commit.assert_not_called()

    def test_new_key_same_uid_same_hash_is_unchanged_without_new_jobs(self):
        with mock.patch.object(service, "list_folder", side_effect=self.month_folders), mock.patch.object(
            service,
            "upload_file_confirmed",
            return_value=("source-token", "https://example.test/source"),
        ), mock.patch.object(
            service,
            "copy_file_confirmed",
            return_value=("baseline-token", "https://example.test/baseline"),
        ), mock.patch.object(service, "commit_meeting_record", return_value="record-one"):
            first = service.idempotent_ingestion(
                self.config,
                self.user,
                self.KEY,
                "tenant",
                self.metadata,
                self.upload,
                "request-one",
            )
        metadata = {**self.metadata, "meeting_uid": first["meeting_uid"]}
        with mock.patch.object(service, "list_folder") as list_folder, mock.patch.object(
            service, "upload_file_confirmed"
        ) as upload:
            unchanged = service.idempotent_ingestion(
                self.config,
                self.user,
                self.KEY + "-new",
                "tenant",
                metadata,
                self.upload,
                "request-two",
            )
        self.assertEqual(unchanged["status"], "unchanged")
        self.assertEqual(unchanged["data_version"], 1)
        self.assertEqual(unchanged["generation_queued"], [])
        list_folder.assert_not_called()
        upload.assert_not_called()

    def test_same_uid_new_content_increments_global_version(self):
        with mock.patch.object(service, "list_folder", side_effect=self.month_folders), mock.patch.object(
            service,
            "upload_file_confirmed",
            return_value=("source-token", "https://example.test/source"),
        ), mock.patch.object(
            service,
            "copy_file_confirmed",
            return_value=("baseline-token", "https://example.test/baseline"),
        ), mock.patch.object(service, "commit_meeting_record", return_value="record-one"):
            first = service.idempotent_ingestion(
                self.config,
                self.user,
                self.KEY,
                "tenant",
                self.metadata,
                self.upload,
                "request-one",
            )
        changed_upload = service.UploadedFile(
            "file", "new-name.md", "text/markdown", b"# corrected meeting\n"
        )
        metadata = {**self.metadata, "meeting_uid": first["meeting_uid"]}
        with mock.patch.object(service, "list_folder", side_effect=self.month_folders), mock.patch.object(
            service,
            "upload_file_confirmed",
            return_value=("source-token-v2", "https://example.test/source-v2"),
        ), mock.patch.object(
            service,
            "copy_file_confirmed",
            return_value=("baseline-token-v2", "https://example.test/baseline-v2"),
        ), mock.patch.object(service, "commit_meeting_record", return_value="record-one"):
            updated = service.idempotent_ingestion(
                self.config,
                self.user,
                self.KEY + "-v2",
                "tenant",
                metadata,
                changed_upload,
                "request-two",
            )
        self.assertEqual(updated["status"], "updated")
        self.assertEqual(updated["data_version"], 2)
        self.assertTrue(updated["normalized_file_name"].endswith(" - v2.md"))
        registry = service.load_meeting_registry(self.config, first["meeting_uid"])
        self.assertEqual(registry["data_version"], 2)
        self.assertEqual(registry["file_token"], "source-token-v2")

    def test_owner_transfer_failure_is_not_hidden_by_drive_reconciliation(self):
        with mock.patch.object(service, "upload_file", return_value="source-token"), mock.patch.object(
            service, "download_drive_file", return_value=self.upload.data
        ), mock.patch.object(
            service, "resolve_uploaded_url", return_value="https://example.test/source"
        ), mock.patch.object(
            service,
            "transfer_output_owner",
            side_effect=service.UploadError("owner_transfer_failed", "failed", 502),
        ), mock.patch.object(service, "find_drive_file_by_name_and_hash") as reconcile:
            with self.assertRaises(service.UploadError) as caught:
                service.upload_file_confirmed(
                    self.config,
                    "tenant",
                    "current-month",
                    "2032-08-13 - 示例研究周会 - 会议纪要 - v1.md",
                    self.upload.data,
                )
        self.assertEqual(caught.exception.error_code, "owner_transfer_failed")
        reconcile.assert_not_called()

    def test_receipt_loss_after_drive_effect_recovers_without_second_upload(self):
        real_atomic_write = service.atomic_private_write
        calls = 0

        def fail_drive_stage_receipt(path, text):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError("receipt write lost")
            return real_atomic_write(path, text)

        with mock.patch.object(service, "list_folder", side_effect=self.month_folders), mock.patch.object(
            service, "atomic_private_write", side_effect=fail_drive_stage_receipt
        ), mock.patch.object(
            service,
            "upload_file_confirmed",
            return_value=("source-token", "https://example.test/source"),
        ) as first_upload, mock.patch.object(service, "copy_file_confirmed") as baseline:
            with self.assertRaises(service.UploadError) as caught:
                service.idempotent_ingestion(
                    self.config,
                    self.user,
                    self.KEY,
                    "tenant",
                    self.metadata,
                    self.upload,
                    "request-one",
                )
        self.assertEqual(caught.exception.error_code, "ingestion_outcome_uncertain")
        first_upload.assert_called_once()
        baseline.assert_not_called()

        with mock.patch.object(
            service,
            "find_drive_file_by_name_and_hash",
            return_value=("source-token", "https://example.test/source"),
        ) as reconcile, mock.patch.object(
            service, "upload_file_confirmed"
        ) as retry_upload, mock.patch.object(
            service,
            "copy_file_confirmed",
            return_value=("baseline-token", "https://example.test/baseline"),
        ), mock.patch.object(service, "commit_meeting_record", return_value="record-one"):
            result = service.idempotent_ingestion(
                self.config,
                self.user,
                self.KEY,
                "tenant",
                self.metadata,
                self.upload,
                "request-two",
            )
        self.assertEqual(result["idempotency_status"], "reconciled")
        reconcile.assert_called_once()
        retry_upload.assert_not_called()


class HttpBoundaryTests(unittest.TestCase):
    def test_serve_requires_apply_before_configuration_is_loaded(self):
        with self.assertRaisesRegex(SystemExit, "requires explicit --apply"):
            service.main(["serve"])

    def headers(self, *pairs: tuple[str, str]) -> email.message.Message:
        headers = email.message.Message()
        for name, value in pairs:
            headers[name] = value
        return headers

    def test_request_headers_reject_smuggling_and_invalid_lengths(self):
        duplicate = self.headers(
            ("Content-Length", "10"),
            ("Content-Length", "11"),
            ("Content-Type", "multipart/form-data; boundary=x"),
        )
        with self.assertRaisesRegex(service.UploadError, "Exactly one Content-Length"):
            service.request_body_metadata(duplicate, 1024)

        transfer_encoded = self.headers(
            ("Transfer-Encoding", "chunked"),
            ("Content-Length", "10"),
            ("Content-Type", "multipart/form-data; boundary=x"),
        )
        with self.assertRaisesRegex(service.UploadError, "Transfer-Encoding"):
            service.request_body_metadata(transfer_encoded, 1024)

        negative = self.headers(
            ("Content-Length", "-1"),
            ("Content-Type", "multipart/form-data; boundary=x"),
        )
        with self.assertRaisesRegex(service.UploadError, "must be positive"):
            service.request_body_metadata(negative, 1024)

    def test_authentication_headers_must_be_unique_and_unambiguous(self):
        for headers in (
            self.headers(("Authorization", "Bearer one"), ("Authorization", "Bearer two")),
            self.headers(("X-Upload-Token", "one"), ("X-Upload-Token", "two")),
            self.headers(("Authorization", "Bearer one"), ("X-Upload-Token", "one")),
        ):
            with self.subTest(headers=list(headers.items())):
                with self.assertRaises(service.UploadError) as caught:
                    service.extract_upload_token(headers)
                self.assertEqual(caught.exception.error_code, "ambiguous_authentication")
        self.assertEqual(service.extract_upload_token(self.headers(("Authorization", "Bearer one"))), "one")

    def test_short_body_and_content_type_confusion_fail_closed(self):
        with self.assertRaisesRegex(service.UploadError, "ended before"):
            service.read_exact_body(io.BytesIO(b"short"), 10)
        with self.assertRaisesRegex(service.UploadError, "must be multipart/form-data"):
            service.parse_multipart_form("application/x-multipart/form-data; boundary=x", b"")

    def test_multiple_file_parts_are_rejected(self):
        boundary = "test-boundary"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="one.md"\r\n'
            "Content-Type: text/markdown\r\n\r\n"
            "one\r\n"
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="two.md"\r\n'
            "Content-Type: text/markdown\r\n\r\n"
            "two\r\n"
            f"--{boundary}--\r\n"
        ).encode("utf-8")
        with self.assertRaisesRegex(service.UploadError, "Exactly one file"):
            service.parse_multipart_form(f"multipart/form-data; boundary={boundary}", body)

    def test_meeting_metadata_rejects_unknown_duplicate_and_conflicting_values(self):
        with self.assertRaises(service.UploadError) as unknown:
            service.request_metadata_fields(
                {"meeting_date": "2032-08-13"}, {"legacy": ["value"]}
            )
        self.assertEqual(unknown.exception.error_code, "unexpected_query_parameter")
        with self.assertRaises(service.UploadError) as duplicate:
            service.request_metadata_fields(
                {}, {"meeting_date": ["2032-08-13", "2032-08-14"]}
            )
        self.assertEqual(duplicate.exception.error_code, "ambiguous_meeting_metadata")
        with self.assertRaises(service.UploadError) as conflict:
            service.request_metadata_fields(
                {"meeting_series": "示例研究周会"}, {"meeting_series": ["其他系列"]}
            )
        self.assertEqual(conflict.exception.error_code, "ambiguous_meeting_metadata")

    def test_markdown_must_be_utf8(self):
        with self.assertRaises(service.UploadError) as caught:
            service.validate_markdown_file("meeting.md", b"\xff\xfe", 1024)
        self.assertEqual(caught.exception.error_code, "invalid_markdown_encoding")


class ReadinessTests(unittest.TestCase):
    def test_readiness_requires_all_local_preconditions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            validator = root / "validator.py"
            validator.write_text('print("{\\"ok\\": true}")\n', encoding="utf-8")
            digest = hashlib.sha256(validator.read_bytes()).hexdigest()
            users = root / "users.json"
            service.save_user_db(
                users,
                {
                    "version": 1,
                    "users": [
                        {"user_id": "one", "enabled": True, "token_hash": service.hash_token("token")}
                    ],
                },
            )
            config = service.Config(
                app_id="app",
                app_secret="secret",
                parent_folder_token="folder",
                user_db_path=users,
                max_upload_bytes=1024,
                file_url_base="https://example.test/file/",
                meeting_contract_validator=str(validator),
                meeting_contract_validator_sha256=digest,
                baseline_parent_folder_token="baseline-folder",
                meeting_base_app_token="base-app",
                meeting_base_table_id="table-id",
                generation_job_spool_path=root / "jobs",
                meeting_registry_path=root / "registry",
            )
            self.assertEqual(service.local_readiness(config)["ready"], True)
            config.meeting_contract_validator_sha256 = "0" * 64
            self.assertEqual(service.local_readiness(config)["ready"], True)
            config.parent_folder_token = ""
            self.assertEqual(service.local_readiness(config)["ready"], False)


if __name__ == "__main__":
    unittest.main()
