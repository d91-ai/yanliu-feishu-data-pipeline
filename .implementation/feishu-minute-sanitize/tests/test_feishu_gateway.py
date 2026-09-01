from __future__ import annotations

import io
from pathlib import Path
import sys
import unittest
from unittest import mock
import urllib.error


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from feishu_gateway import FeishuOpenApiGateway, FeishuSettings, GatewayError, RemoteFile  # noqa: E402


class GatewaySafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gateway = FeishuOpenApiGateway(
            FeishuSettings(
                app_id="app-id",
                app_secret="app-secret",
                bitable_app_token="APP_PRIVATE_TOKEN",
                source_table_id="TABLE_PRIVATE_ID",
                target_table_id="target-table",
            )
        )

    def test_http_error_does_not_expose_resource_tokens(self) -> None:
        path = "/bitable/v1/apps/APP_PRIVATE_TOKEN/tables/TABLE_PRIVATE_ID/records/RECORD_PRIVATE_ID"
        error = urllib.error.HTTPError("https://example.invalid", 403, "forbidden", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(GatewayError) as caught:
                self.gateway._request_json("GET", path)
        public = caught.exception.safe_message
        for private in ("APP_PRIVATE_TOKEN", "TABLE_PRIVATE_ID", "RECORD_PRIVATE_ID", path):
            self.assertNotIn(private, public)
        self.assertIn("403", public)

    def test_http_error_keeps_only_numeric_remote_code(self) -> None:
        body = (
            b'{"code":1254608,"msg":"PRIVATE_REMOTE_MESSAGE",'
            b'"data":{"record_id":"PRIVATE_RECORD_ID"}}'
        )
        error = urllib.error.HTTPError(
            "https://example.invalid",
            403,
            "forbidden",
            {},
            io.BytesIO(body),
        )
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(GatewayError) as caught:
                self.gateway._request_json("POST", "/records")
        failure = caught.exception
        self.assertEqual(failure.http_status, 403)
        self.assertEqual(failure.remote_code, "1254608")
        self.assertIn("1254608", failure.safe_message)
        self.assertNotIn("PRIVATE_REMOTE_MESSAGE", failure.safe_message)
        self.assertNotIn("PRIVATE_RECORD_ID", failure.safe_message)

    def test_http_error_ignores_non_numeric_remote_code(self) -> None:
        error = urllib.error.HTTPError(
            "https://example.invalid",
            403,
            "forbidden",
            {},
            io.BytesIO(b'{"code":"PRIVATE_CODE","msg":"PRIVATE_MESSAGE"}'),
        )
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(GatewayError) as caught:
                self.gateway._request_json("POST", "/records")
        self.assertEqual(caught.exception.http_status, 403)
        self.assertEqual(caught.exception.remote_code, "")
        self.assertNotIn("PRIVATE", caught.exception.safe_message)

    def test_api_error_keeps_remote_code_without_remote_message(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = (
            b'{"code":1254608,"msg":"PRIVATE_REMOTE_MESSAGE",'
            b'"data":{"record_id":"PRIVATE_RECORD_ID"}}'
        )
        with mock.patch("urllib.request.urlopen", return_value=response):
            with self.assertRaises(GatewayError) as caught:
                self.gateway._request_json("POST", "/records")
        failure = caught.exception
        self.assertEqual(failure.remote_code, "1254608")
        self.assertNotIn("PRIVATE_REMOTE_MESSAGE", failure.safe_message)
        self.assertNotIn("PRIVATE_RECORD_ID", failure.safe_message)

    def test_ensure_auditable_version_reuses_existing_history(self) -> None:
        remote = RemoteFile("file-token", "https://example.test/file/file-token", "review.md", b"review\n")
        versions = [{"version": "v2", "edited_at": "2", "is_deleted": False}]
        with (
            mock.patch.object(self.gateway, "_file_versions", return_value=versions),
            mock.patch.object(self.gateway, "_download", return_value=remote.content),
            mock.patch.object(self.gateway, "_overwrite") as overwrite,
        ):
            result = self.gateway.ensure_auditable_version(remote, content_type="text/markdown")
        self.assertEqual(result.version, "v2")
        self.assertEqual(result.content, remote.content)
        overwrite.assert_not_called()

    def test_ensure_auditable_version_creates_first_version(self) -> None:
        remote = RemoteFile("file-token", "https://example.test/file/file-token", "review.md", b"review\n")
        with (
            mock.patch.object(self.gateway, "_file_versions", return_value=[]),
            mock.patch.object(self.gateway, "_overwrite", return_value="v1") as overwrite,
            mock.patch.object(self.gateway, "_download", return_value=remote.content),
        ):
            result = self.gateway.ensure_auditable_version(
                remote,
                content_type="text/markdown; charset=utf-8",
            )
        self.assertEqual(result.version, "v1")
        overwrite.assert_called_once_with(
            remote.token,
            remote.name,
            remote.content,
            "text/markdown; charset=utf-8",
        )

    def test_ensure_auditable_version_rejects_hash_mismatch(self) -> None:
        remote = RemoteFile("file-token", "https://example.test/file/file-token", "review.md", b"review\n")
        with (
            mock.patch.object(self.gateway, "_file_versions", return_value=[]),
            mock.patch.object(self.gateway, "_overwrite", return_value="v1"),
            mock.patch.object(self.gateway, "_download", return_value=b"different\n"),
        ):
            with self.assertRaises(GatewayError) as caught:
                self.gateway.ensure_auditable_version(remote, content_type="text/markdown")
        self.assertEqual(caught.exception.code, "drive_version_hash_mismatch")

    def test_ensure_auditable_version_is_idempotent_after_bootstrap(self) -> None:
        remote = RemoteFile("file-token", "https://example.test/file/file-token", "review.md", b"review\n")
        with (
            mock.patch.object(
                self.gateway,
                "_file_versions",
                side_effect=[[], [{"version": "v1", "edited_at": "1", "is_deleted": False}]],
            ),
            mock.patch.object(self.gateway, "_overwrite", return_value="v1") as overwrite,
            mock.patch.object(self.gateway, "_download", return_value=remote.content),
        ):
            first = self.gateway.ensure_auditable_version(remote, content_type="text/markdown")
            second = self.gateway.ensure_auditable_version(first, content_type="text/markdown")
        self.assertEqual(first.version, "v1")
        self.assertEqual(second.version, "v1")
        overwrite.assert_called_once()

    def test_ensure_auditable_version_rejects_history_without_version(self) -> None:
        remote = RemoteFile("file-token", "https://example.test/file/file-token", "review.md", b"review\n")
        with mock.patch.object(
            self.gateway,
            "_file_versions",
            return_value=[{"edited_at": "1", "is_deleted": False}],
        ):
            with self.assertRaises(GatewayError) as caught:
                self.gateway.ensure_auditable_version(remote, content_type="text/markdown")
        self.assertEqual(caught.exception.code, "drive_version_missing")

    def test_overwrite_sends_file_token_and_requires_version(self) -> None:
        with mock.patch.object(
            self.gateway,
            "_send_upload",
            return_value={"file_token": "file-token", "version": "v1"},
        ) as send_upload:
            version = self.gateway._overwrite(
                "file-token",
                "review.md",
                b"review\n",
                "text/markdown",
            )
        self.assertEqual(version, "v1")
        body = send_upload.call_args.args[1]
        self.assertIn(b'name="file_token"', body)
        self.assertIn(b"file-token", body)
        self.assertIn(b'name="parent_node"', body)

    def test_overwrite_rejects_incomplete_response(self) -> None:
        with mock.patch.object(
            self.gateway,
            "_send_upload",
            return_value={"file_token": "file-token"},
        ):
            with self.assertRaises(GatewayError) as caught:
                self.gateway._overwrite("file-token", "review.md", b"review\n", "text/markdown")
        self.assertEqual(caught.exception.code, "drive_overwrite_invalid")

    def test_overwrite_rejects_different_file_token(self) -> None:
        with mock.patch.object(
            self.gateway,
            "_send_upload",
            return_value={"file_token": "other-token", "version": "v1"},
        ):
            with self.assertRaises(GatewayError) as caught:
                self.gateway._overwrite("file-token", "review.md", b"review\n", "text/markdown")
        self.assertEqual(caught.exception.code, "drive_overwrite_invalid")


if __name__ == "__main__":
    unittest.main()
