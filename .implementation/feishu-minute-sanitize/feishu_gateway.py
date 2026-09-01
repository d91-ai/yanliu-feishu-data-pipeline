#!/usr/bin/env python3
"""Small Feishu OpenAPI gateway used by the sanitization orchestrator."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import secrets
import time
from typing import Any, Protocol
import urllib.error
import urllib.parse
import urllib.request
import uuid


OPENAPI_BASE = "https://open.feishu.cn/open-apis"


class GatewayError(RuntimeError):
    """A content-free error suitable for service status fields."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int | None = None,
        remote_code: str = "",
    ):
        super().__init__(message)
        self.code = code
        self.safe_message = message
        self.http_status = http_status
        self.remote_code = remote_code


def _numeric_remote_code(value: Any) -> str:
    """Return an ASCII integer code without retaining other response content."""

    if isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        code = value.strip()
        if code.isascii() and code.isdigit():
            return code
    return ""


@dataclass(frozen=True)
class RemoteFile:
    token: str
    url: str
    name: str
    content: bytes
    version: str = ""

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


class WorkflowGateway(Protocol):
    def get_source_record(self, record_id: str) -> dict[str, Any]: ...

    def update_source_record(self, record_id: str, fields: dict[str, Any]) -> None: ...

    def get_target_record(self, record_id: str) -> dict[str, Any]: ...

    def update_target_record(self, record_id: str, fields: dict[str, Any]) -> None: ...

    def find_target_by_source_id(self, source_record_id: str) -> dict[str, Any] | None: ...

    def create_target_record(self, fields: dict[str, Any], *, client_token: str) -> dict[str, Any]: ...

    def fetch_file(self, url: str, *, require_version: bool = False) -> RemoteFile: ...

    def ensure_auditable_version(
        self,
        remote: RemoteFile,
        *,
        content_type: str,
    ) -> RemoteFile: ...

    def ensure_month_folder(self, root_token: str, month: str) -> str: ...

    def ensure_baseline_folder(self, version_root_token: str, month: str) -> str: ...

    def upload_or_reuse(
        self,
        folder_token: str,
        file_name: str,
        content: bytes,
        *,
        content_type: str,
    ) -> RemoteFile: ...

    def now_ms(self) -> int: ...


@dataclass(frozen=True)
class FeishuSettings:
    app_id: str
    app_secret: str
    bitable_app_token: str
    source_table_id: str
    target_table_id: str
    openapi_base: str = OPENAPI_BASE
    user_id_type: str = "open_id"


class FeishuOpenApiGateway:
    def __init__(self, settings: FeishuSettings):
        self.settings = settings
        self._tenant_token = ""
        self._tenant_expires_at = 0.0

    def now_ms(self) -> int:
        return int(time.time() * 1000)

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        token: str = "",
        timeout: int = 30,
    ) -> dict[str, Any]:
        url = self.settings.openapi_base.rstrip("/") + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            # Parse only the numeric remote code. Remote msg/data can contain private
            # metadata and must never be copied to exceptions, logs, or status fields.
            remote_code = ""
            try:
                error_raw = exc.read(64 * 1024)
                error_payload = json.loads(error_raw.decode("utf-8")) if error_raw else {}
                if isinstance(error_payload, dict):
                    remote_code = _numeric_remote_code(error_payload.get("code"))
            except Exception:
                # Error metadata parsing is best-effort; the safe HTTP failure is
                # still raised when urllib exposes no readable response body.
                pass
            safe_message = f"Feishu API returned HTTP {exc.code}."
            if remote_code:
                safe_message = f"Feishu API returned HTTP {exc.code} (code {remote_code})."
            raise GatewayError(
                "feishu_http_error",
                safe_message,
                http_status=exc.code,
                remote_code=remote_code,
            ) from exc
        except urllib.error.URLError as exc:
            raise GatewayError("feishu_unreachable", "Feishu API is unreachable.") from exc
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise GatewayError("feishu_invalid_json", "Feishu API returned invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise GatewayError("feishu_invalid_json", "Feishu API returned an invalid object.")
        if payload.get("code", 0) != 0:
            code = str(payload.get("code") or "unknown")
            raise GatewayError(
                "feishu_api_error",
                f"Feishu API rejected the request with code {code}.",
                remote_code=code,
            )
        return payload

    def _token(self) -> str:
        if self._tenant_token and self._tenant_expires_at - 120 > time.time():
            return self._tenant_token
        payload = self._request_json(
            "POST",
            "/auth/v3/tenant_access_token/internal",
            body={"app_id": self.settings.app_id, "app_secret": self.settings.app_secret},
        )
        token = str(payload.get("tenant_access_token") or "")
        if not token:
            raise GatewayError("tenant_token_missing", "Feishu tenant token response is incomplete.")
        self._tenant_token = token
        self._tenant_expires_at = time.time() + int(payload.get("expire") or 7200)
        return token

    def _record_path(self, table_id: str, record_id: str = "") -> str:
        path = (
            f"/bitable/v1/apps/{urllib.parse.quote(self.settings.bitable_app_token)}"
            f"/tables/{urllib.parse.quote(table_id)}/records"
        )
        return path + (f"/{urllib.parse.quote(record_id)}" if record_id else "")

    def _get_record(self, table_id: str, record_id: str) -> dict[str, Any]:
        payload = self._request_json(
            "GET",
            self._record_path(table_id, record_id),
            token=self._token(),
            query={"user_id_type": self.settings.user_id_type},
        )
        record = payload.get("data", {}).get("record")
        if not isinstance(record, dict):
            raise GatewayError("record_missing", "Bitable record response is incomplete.")
        return record

    def _update_record(self, table_id: str, record_id: str, fields: dict[str, Any]) -> None:
        self._request_json(
            "PUT",
            self._record_path(table_id, record_id),
            token=self._token(),
            query={"user_id_type": self.settings.user_id_type},
            body={"fields": fields},
        )

    def get_source_record(self, record_id: str) -> dict[str, Any]:
        return self._get_record(self.settings.source_table_id, record_id)

    def update_source_record(self, record_id: str, fields: dict[str, Any]) -> None:
        self._update_record(self.settings.source_table_id, record_id, fields)

    def get_target_record(self, record_id: str) -> dict[str, Any]:
        return self._get_record(self.settings.target_table_id, record_id)

    def update_target_record(self, record_id: str, fields: dict[str, Any]) -> None:
        self._update_record(self.settings.target_table_id, record_id, fields)

    def find_target_by_source_id(self, source_record_id: str) -> dict[str, Any] | None:
        payload = self._request_json(
            "POST",
            self._record_path(self.settings.target_table_id) + "/search",
            token=self._token(),
            query={"user_id_type": self.settings.user_id_type, "page_size": 2},
            body={
                "filter": {
                    "conjunction": "and",
                    "conditions": [
                        {"field_name": "来源记录ID", "operator": "is", "value": [source_record_id]}
                    ],
                }
            },
        )
        items = payload.get("data", {}).get("items") or []
        if not isinstance(items, list):
            raise GatewayError("record_search_invalid", "Bitable record search response is invalid.")
        if len(items) > 1:
            raise GatewayError("duplicate_source_records", "Multiple sanitization records share one source record ID.")
        return items[0] if items else None

    def create_target_record(self, fields: dict[str, Any], *, client_token: str) -> dict[str, Any]:
        payload = self._request_json(
            "POST",
            self._record_path(self.settings.target_table_id),
            token=self._token(),
            query={"user_id_type": self.settings.user_id_type, "client_token": client_token},
            body={"fields": fields},
        )
        record = payload.get("data", {}).get("record")
        if not isinstance(record, dict) or not record.get("record_id"):
            raise GatewayError("record_create_invalid", "Bitable create response is incomplete.")
        return record

    @staticmethod
    def _parse_drive_url(url: str) -> tuple[str, str]:
        parsed = urllib.parse.urlparse(url)
        parts = [urllib.parse.unquote(part) for part in parsed.path.split("/") if part]
        known = {"file": "file", "doc": "doc", "docx": "docx"}
        for index, part in enumerate(parts[:-1]):
            if part in known and parts[index + 1]:
                return parts[index + 1], known[part]
        raise GatewayError("drive_url_invalid", "Drive link is missing a supported file token.")

    def _get_meta(self, file_token: str, file_type: str) -> dict[str, Any]:
        payload = self._request_json(
            "POST",
            "/drive/v1/metas/batch_query",
            token=self._token(),
            query={"user_id_type": self.settings.user_id_type},
            body={"request_docs": [{"doc_token": file_token, "doc_type": file_type}], "with_url": True},
        )
        metas = payload.get("data", {}).get("metas") or []
        if not isinstance(metas, list) or not metas:
            raise GatewayError("drive_meta_missing", "Drive metadata response is incomplete.")
        return metas[0]

    def _file_versions(self, file_token: str) -> list[dict[str, Any]]:
        payload = self._request_json(
            "GET",
            f"/drive/v1/files/{urllib.parse.quote(file_token)}/history",
            token=self._token(),
            query={"only_tag": "true", "page_size": 200},
        )
        data = payload.get("data", {})
        values = data.get("versions") or data.get("file_versions") or data.get("items") or []
        if not isinstance(values, list):
            raise GatewayError("drive_versions_invalid", "Drive version history response is invalid.")
        return [value for value in values if isinstance(value, dict) and not value.get("is_deleted")]

    def _download(self, file_token: str, *, version: str = "") -> bytes:
        url = (
            self.settings.openapi_base.rstrip("/")
            + f"/drive/v1/files/{urllib.parse.quote(file_token)}/download"
        )
        if version:
            url += "?" + urllib.parse.urlencode({"version": version})
        request = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {self._token()}"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            raise GatewayError("drive_download_failed", f"Drive download returned HTTP {exc.code}.") from exc
        except urllib.error.URLError as exc:
            raise GatewayError("drive_download_failed", "Drive download is unreachable.") from exc

    @staticmethod
    def _version_sort_key(value: dict[str, Any]) -> tuple[int, str]:
        raw = value.get("edited_at") or value.get("create_time") or 0
        try:
            timestamp = int(str(raw))
        except ValueError:
            timestamp = 0
        return timestamp, str(value.get("version") or "")

    def fetch_file(self, url: str, *, require_version: bool = False) -> RemoteFile:
        file_token, file_type = self._parse_drive_url(url)
        if file_type != "file":
            raise GatewayError("unsupported_drive_type", "Only uploaded Drive files are supported.")
        meta = self._get_meta(file_token, file_type)
        version = ""
        if require_version:
            versions = sorted(self._file_versions(file_token), key=self._version_sort_key, reverse=True)
            if not versions:
                raise GatewayError("drive_version_missing", "Drive file has no auditable version history.")
            version = str(versions[0].get("version") or "")
            if not version:
                raise GatewayError("drive_version_missing", "Latest Drive version has no version identifier.")
        content = self._download(file_token, version=version)
        resolved_url = str(meta.get("url") or url)
        name = str(meta.get("name") or meta.get("title") or file_token)
        return RemoteFile(file_token, resolved_url, name, content, version)

    def _list_folder_items(self, folder_token: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        page_token = ""
        while True:
            query: dict[str, Any] = {
                "folder_token": folder_token,
                "user_id_type": self.settings.user_id_type,
                "page_size": 200,
            }
            if page_token:
                query["page_token"] = page_token
            payload = self._request_json("GET", "/drive/v1/files", token=self._token(), query=query)
            data = payload.get("data", {})
            items = data.get("files") or data.get("items") or []
            if not isinstance(items, list):
                raise GatewayError("drive_list_invalid", "Drive folder listing response is invalid.")
            result.extend(item for item in items if isinstance(item, dict))
            if not data.get("has_more"):
                return result
            page_token = str(data.get("next_page_token") or data.get("page_token") or "")
            if not page_token:
                return result

    @staticmethod
    def _item_token(item: dict[str, Any]) -> str:
        return str(item.get("token") or item.get("file_token") or item.get("folder_token") or "")

    def _ensure_child_folder(self, parent_token: str, name: str) -> str:
        for item in self._list_folder_items(parent_token):
            if item.get("type") == "folder" and item.get("name") == name:
                token = self._item_token(item)
                if not token:
                    raise GatewayError("folder_token_missing", "Existing Drive folder has no token.")
                return token
        payload = self._request_json(
            "POST",
            "/drive/v1/files/create_folder",
            token=self._token(),
            body={"name": name, "folder_token": parent_token},
        )
        folder = payload.get("data", {}).get("folder") or payload.get("data") or {}
        token = self._item_token(folder)
        if not token:
            raise GatewayError("folder_create_invalid", "Drive folder create response is incomplete.")
        return token

    def ensure_month_folder(self, root_token: str, month: str) -> str:
        return self._ensure_child_folder(root_token, month)

    def ensure_baseline_folder(self, version_root_token: str, month: str) -> str:
        month_token = self._ensure_child_folder(version_root_token, month)
        return self._ensure_child_folder(month_token, "审核前")

    @staticmethod
    def _multipart(
        file_name: str,
        folder_token: str,
        content: bytes,
        content_type: str,
        *,
        file_token: str = "",
    ) -> tuple[str, bytes]:
        boundary = "----minute-sanitize-" + secrets.token_hex(16)
        parts: list[bytes] = []

        def field(name: str, value: str) -> None:
            parts.extend(
                [
                    f"--{boundary}\r\n".encode(),
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                    value.encode(),
                    b"\r\n",
                ]
            )

        field("file_name", file_name)
        field("parent_type", "explorer")
        field("parent_node", folder_token)
        field("size", str(len(content)))
        if file_token:
            field("file_token", file_token)
        safe_name = file_name.replace('"', "")
        parts.extend(
            [
                f"--{boundary}\r\n".encode(),
                (
                    f'Content-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'
                    f"Content-Type: {content_type}\r\n\r\n"
                ).encode(),
                content,
                b"\r\n",
                f"--{boundary}--\r\n".encode(),
            ]
        )
        return f"multipart/form-data; boundary={boundary}", b"".join(parts)

    def _send_upload(self, multipart_type: str, body: bytes) -> dict[str, Any]:
        url = self.settings.openapi_base.rstrip("/") + "/drive/v1/files/upload_all"
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Authorization": f"Bearer {self._token()}", "Content-Type": multipart_type},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise GatewayError("drive_upload_failed", f"Drive upload returned HTTP {exc.code}.") from exc
        except urllib.error.URLError as exc:
            raise GatewayError("drive_upload_failed", "Drive upload is unreachable.") from exc
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise GatewayError("drive_upload_invalid", "Drive upload response is invalid.") from exc
        if payload.get("code", 0) != 0:
            raise GatewayError("drive_upload_failed", "Drive upload was rejected.")
        data = payload.get("data") or {}
        if not isinstance(data, dict):
            raise GatewayError("drive_upload_invalid", "Drive upload response is incomplete.")
        return data

    def _upload(self, folder_token: str, file_name: str, content: bytes, content_type: str) -> str:
        multipart_type, body = self._multipart(file_name, folder_token, content, content_type)
        data = self._send_upload(multipart_type, body)
        file_token = str(data.get("file_token") or "")
        if not file_token:
            raise GatewayError("drive_upload_invalid", "Drive upload response has no file token.")
        return file_token

    def _overwrite(
        self,
        file_token: str,
        file_name: str,
        content: bytes,
        content_type: str,
    ) -> str:
        multipart_type, body = self._multipart(
            file_name,
            "",
            content,
            content_type,
            file_token=file_token,
        )
        data = self._send_upload(multipart_type, body)
        returned_token = str(data.get("file_token") or "")
        version = str(data.get("version") or "")
        if returned_token != file_token or not version:
            raise GatewayError("drive_overwrite_invalid", "Drive overwrite response is incomplete.")
        return version

    def ensure_auditable_version(
        self,
        remote: RemoteFile,
        *,
        content_type: str,
    ) -> RemoteFile:
        versions = sorted(self._file_versions(remote.token), key=self._version_sort_key, reverse=True)
        if versions:
            version = str(versions[0].get("version") or "")
            if not version:
                raise GatewayError("drive_version_missing", "Latest Drive version has no version identifier.")
        else:
            # A newly uploaded Drive file has no tagged history yet. An identical
            # overwrite creates the first auditable version without changing content.
            version = self._overwrite(remote.token, remote.name, remote.content, content_type)
        content = self._download(remote.token, version=version)
        if hashlib.sha256(content).hexdigest() != remote.sha256:
            raise GatewayError("drive_version_hash_mismatch", "Drive version failed hash verification.")
        return RemoteFile(remote.token, remote.url, remote.name, content, version)

    def upload_or_reuse(
        self,
        folder_token: str,
        file_name: str,
        content: bytes,
        *,
        content_type: str,
    ) -> RemoteFile:
        expected = hashlib.sha256(content).hexdigest()
        for item in self._list_folder_items(folder_token):
            if item.get("type") != "file" or item.get("name") != file_name:
                continue
            token = self._item_token(item)
            if not token:
                raise GatewayError("drive_file_token_missing", "Existing Drive file has no token.")
            existing_content = self._download(token)
            if hashlib.sha256(existing_content).hexdigest() != expected:
                raise GatewayError("drive_name_hash_conflict", "Existing Drive file name has different content.")
            url = str(item.get("url") or self._get_meta(token, "file").get("url") or "")
            if not url:
                raise GatewayError("drive_file_url_missing", "Existing Drive file has no URL.")
            return RemoteFile(token, url, file_name, existing_content)
        token = self._upload(folder_token, file_name, content, content_type)
        meta = self._get_meta(token, "file")
        url = str(meta.get("url") or "")
        if not url:
            raise GatewayError("drive_file_url_missing", "Uploaded Drive file has no URL.")
        downloaded = self._download(token)
        if hashlib.sha256(downloaded).hexdigest() != expected:
            raise GatewayError("drive_roundtrip_hash_mismatch", "Uploaded Drive file failed hash verification.")
        return RemoteFile(token, url, file_name, downloaded)


def deterministic_client_token(seed: str) -> str:
    digest = hashlib.sha256(seed.encode("utf-8")).digest()[:16]
    return str(uuid.UUID(bytes=digest, version=4))
