#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Mapping
import urllib.error
import urllib.parse
import urllib.request
import uuid


SERVICE_NAME = "feishu-structured-generate"
OPENAPI_BASE = "https://open.feishu.cn/open-apis"
FILE_CREATED_SUBSCRIBE_EVENT_TYPE = "file.created_in_folder_v1"

TYPE_TEXT = 1
TYPE_NUMBER = 2
TYPE_SINGLE_SELECT = 3
TYPE_DATE = 5
TYPE_CHECKBOX = 7
TYPE_URL = 15

STATUS_RUNNING = "生成中"
STATUS_GENERATED = "已生成"
STATUS_NO_ROWS = "无可结构化标的"
STATUS_FAILED = "生成失败"
REQUIRED_STATUS_OPTIONS = [STATUS_RUNNING, STATUS_GENERATED, STATUS_NO_ROWS, STATUS_FAILED]

FIELD_TABLE_STATUS = "表格生成状态"
FIELD_TABLE_LINK = "表格链接"
FIELD_GENERATED_AT = "生成时间"
FIELD_TABLE_ROWS = "表格行数"
FIELD_TABLE_ERROR = "表格生成错误"
FIELD_MEETING_DATE = "会议日期"
FIELD_ARCHIVE_STATUS = "归档状态"
FIELD_ARCHIVE_LINK = "归档链接"
FIELD_FILE_NAME = "文件名"

DEFAULT_REVIEW_FIELD_NAMES = ("审核状态", "已审核")


class StructuredError(Exception):
    def __init__(self, error_code: str, message: str, http_status: int = 500):
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.http_status = http_status


class FeishuApiError(StructuredError):
    def __init__(self, message: str, http_status: int = 502):
        super().__init__("feishu_api_error", message, http_status)


@dataclass(frozen=True)
class Config:
    app_id: str
    app_secret: str
    source_base_token: str
    source_table_id: str
    structured_base_token: str
    structured_table_id: str
    structured_pending_folder_token: str
    structured_archive_folder_token: str
    structured_http_token: str
    skill_script: Path
    output_dir: Path
    folder_registry_path: Path
    http_host: str = "0.0.0.0"
    http_port: int = 8790
    openapi_base: str = OPENAPI_BASE
    user_id_type: str = "open_id"
    review_field_names: tuple[str, ...] = DEFAULT_REVIEW_FIELD_NAMES
    max_error_chars: int = 300


_token_cache: dict[str, Any] = {}


def parse_dotenv_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key:
            values[key] = value
    return values


def load_env_files() -> dict[str, str]:
    values: dict[str, str] = {}
    explicit = os.environ.get("FEISHU_STRUCTURED_ENV_FILE", "").strip()
    candidates = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates.append(Path.cwd() / ".env")
    candidates.append(Path(__file__).resolve().parent / ".env")
    for path in candidates:
        if path.exists():
            values.update(parse_dotenv_file(path))
            break
    values.update(os.environ)
    return values


def path_from_env(value: str, base: Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return base / path


def int_from_env(env: Mapping[str, str], name: str, default: int) -> int:
    raw = str(env.get(name, "")).strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise StructuredError("invalid_config", f"{name} must be an integer.") from exc
    if value <= 0:
        raise StructuredError("invalid_config", f"{name} must be positive.")
    return value


def read_config() -> Config:
    env = load_env_files()
    base_dir = Path(__file__).resolve().parent
    required_names = [
        "FEISHU_APP_ID",
        "FEISHU_APP_SECRET",
        "FEISHU_SOURCE_BITABLE_APP_TOKEN",
        "FEISHU_SOURCE_TABLE_ID",
        "FEISHU_STRUCTURED_BITABLE_APP_TOKEN",
        "FEISHU_STRUCTURED_TABLE_ID",
        "FEISHU_STRUCTURED_PENDING_FOLDER_TOKEN",
        "FEISHU_STRUCTURED_ARCHIVE_FOLDER_TOKEN",
    ]
    missing = [name for name in required_names if not str(env.get(name, "")).strip()]
    if missing:
        raise StructuredError("invalid_config", f"Missing required config: {', '.join(missing)}")
    skill_script = path_from_env(
        str(env.get("STRUCTURED_TABLE_SKILL_SCRIPT", "/skills/meeting-minutes-structured-table/scripts/generate_table.py")),
        base_dir,
    )
    output_dir = path_from_env(str(env.get("STRUCTURED_OUTPUT_DIR", "data/structured_outputs")), base_dir)
    registry_path = path_from_env(str(env.get("STRUCTURED_FOLDER_REGISTRY_PATH", "data/structured_folder_registry.json")), base_dir)
    review_names = tuple(
        item.strip()
        for item in re.split(r"[,;，；\s]+", str(env.get("FEISHU_SOURCE_REVIEW_FIELD_NAMES", ",".join(DEFAULT_REVIEW_FIELD_NAMES))))
        if item.strip()
    )
    return Config(
        app_id=str(env["FEISHU_APP_ID"]).strip(),
        app_secret=str(env["FEISHU_APP_SECRET"]).strip(),
        source_base_token=str(env["FEISHU_SOURCE_BITABLE_APP_TOKEN"]).strip(),
        source_table_id=str(env["FEISHU_SOURCE_TABLE_ID"]).strip(),
        structured_base_token=str(env["FEISHU_STRUCTURED_BITABLE_APP_TOKEN"]).strip(),
        structured_table_id=str(env["FEISHU_STRUCTURED_TABLE_ID"]).strip(),
        structured_pending_folder_token=str(env["FEISHU_STRUCTURED_PENDING_FOLDER_TOKEN"]).strip(),
        structured_archive_folder_token=str(env["FEISHU_STRUCTURED_ARCHIVE_FOLDER_TOKEN"]).strip(),
        structured_http_token=str(env.get("FEISHU_STRUCTURED_HTTP_TOKEN", "")).strip(),
        skill_script=skill_script,
        output_dir=output_dir,
        folder_registry_path=registry_path,
        http_host=str(env.get("FEISHU_STRUCTURED_HTTP_HOST", "127.0.0.1")).strip() or "127.0.0.1",
        http_port=int_from_env(env, "FEISHU_STRUCTURED_HTTP_PORT", 8790),
        openapi_base=str(env.get("FEISHU_OPENAPI_BASE", OPENAPI_BASE)).strip().rstrip("/") or OPENAPI_BASE,
        user_id_type=str(env.get("FEISHU_USER_ID_TYPE", "open_id")).strip() or "open_id",
        review_field_names=review_names or DEFAULT_REVIEW_FIELD_NAMES,
        max_error_chars=int_from_env(env, "STRUCTURED_MAX_ERROR_CHARS", 300),
    )


def configure_logging() -> None:
    level_name = os.environ.get("FEISHU_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(message)s")


def request_json(
    cfg: Config,
    method: str,
    path: str,
    *,
    token: str | None = None,
    body: dict[str, Any] | None = None,
    query: dict[str, Any] | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    url = cfg.openapi_base + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    if method.upper() in {"POST", "PUT"} and data is None:
        data = b""
    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise FeishuApiError(f"HTTP {exc.code} {method} {path}: {details[:500]}") from exc
    except urllib.error.URLError as exc:
        raise FeishuApiError(f"Could not reach Feishu OpenAPI: {exc}") from exc
    if not payload:
        return {}
    try:
        result = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise FeishuApiError(f"Invalid JSON response from {path}: {payload[:300]}") from exc
    if result.get("code", 0) != 0:
        raise FeishuApiError(f"Feishu API error on {method} {path}: {str(result)[:500]}")
    return result


def get_tenant_access_token(cfg: Config) -> str:
    now = time.time()
    token_key = f"tenant_access_token:{cfg.app_id}"
    expires_key = f"expires_at:{cfg.app_id}"
    cached = _token_cache.get(token_key)
    expires_at = float(_token_cache.get(expires_key, 0))
    if cached and expires_at - 120 > now:
        return str(cached)
    result = request_json(
        cfg,
        "POST",
        "/auth/v3/tenant_access_token/internal",
        body={"app_id": cfg.app_id, "app_secret": cfg.app_secret},
    )
    token = result.get("tenant_access_token")
    if not token:
        raise FeishuApiError("tenant_access_token missing in response")
    _token_cache[token_key] = token
    _token_cache[expires_key] = now + int(result.get("expire", 7200))
    return str(token)


def list_bitable_fields(cfg: Config, base_token: str, table_id: str) -> list[dict[str, Any]]:
    token = get_tenant_access_token(cfg)
    fields: list[dict[str, Any]] = []
    page_token = ""
    path = f"/bitable/v1/apps/{urllib.parse.quote(base_token)}/tables/{urllib.parse.quote(table_id)}/fields"
    while True:
        query: dict[str, Any] = {"page_size": 100}
        if page_token:
            query["page_token"] = page_token
        result = request_json(cfg, "GET", path, token=token, query=query)
        data = result.get("data", {})
        fields.extend(data.get("items", []) or [])
        if not data.get("has_more"):
            return fields
        page_token = str(data.get("page_token") or data.get("next_page_token") or "")
        if not page_token:
            return fields


def fields_by_name(fields: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(field.get("field_name") or ""): field for field in fields if field.get("field_name")}


def get_bitable_record(cfg: Config, record_id: str) -> dict[str, Any]:
    token = get_tenant_access_token(cfg)
    path = (
        f"/bitable/v1/apps/{urllib.parse.quote(cfg.source_base_token)}"
        f"/tables/{urllib.parse.quote(cfg.source_table_id)}"
        f"/records/{urllib.parse.quote(record_id)}"
    )
    result = request_json(cfg, "GET", path, token=token, query={"user_id_type": cfg.user_id_type})
    return result.get("data", {}).get("record", {}) or {}


def update_bitable_record(cfg: Config, record_id: str, record_fields: dict[str, Any]) -> dict[str, Any]:
    token = get_tenant_access_token(cfg)
    path = (
        f"/bitable/v1/apps/{urllib.parse.quote(cfg.source_base_token)}"
        f"/tables/{urllib.parse.quote(cfg.source_table_id)}"
        f"/records/{urllib.parse.quote(record_id)}"
    )
    return request_json(
        cfg,
        "PUT",
        path,
        token=token,
        query={"user_id_type": cfg.user_id_type},
        body={"fields": record_fields},
    )


def create_bitable_field(cfg: Config, name: str, field_json: dict[str, Any]) -> dict[str, Any]:
    token = get_tenant_access_token(cfg)
    path = (
        f"/base/v3/bases/{urllib.parse.quote(cfg.source_base_token)}"
        f"/tables/{urllib.parse.quote(cfg.source_table_id)}/fields"
    )
    body = dict(field_json)
    body["name"] = name
    return request_json(cfg, "POST", path, token=token, body=body)


def update_bitable_field(cfg: Config, field_id: str, field_json: dict[str, Any]) -> dict[str, Any]:
    token = get_tenant_access_token(cfg)
    path = (
        f"/base/v3/bases/{urllib.parse.quote(cfg.source_base_token)}"
        f"/tables/{urllib.parse.quote(cfg.source_table_id)}/fields/{urllib.parse.quote(field_id)}"
    )
    return request_json(cfg, "PUT", path, token=token, body=field_json)


def field_option_names(field: dict[str, Any]) -> list[str]:
    options = field.get("property", {}).get("options", [])
    if not isinstance(options, list):
        return []
    names = []
    for option in options:
        if isinstance(option, dict) and option.get("name"):
            names.append(str(option["name"]))
    return names


def required_source_field_specs() -> dict[str, dict[str, Any]]:
    return {
        FIELD_TABLE_STATUS: {
            "type": "select",
            "multiple": False,
            "options": [
                {"name": STATUS_RUNNING, "hue": "Blue", "lightness": "Light"},
                {"name": STATUS_GENERATED, "hue": "Green", "lightness": "Light"},
                {"name": STATUS_NO_ROWS, "hue": "Gray", "lightness": "Light"},
                {"name": STATUS_FAILED, "hue": "Red", "lightness": "Light"},
            ],
        },
        FIELD_TABLE_ROWS: {"type": "number", "style": {"type": "plain", "precision": 0}},
        FIELD_TABLE_ERROR: {"type": "text"},
        FIELD_TABLE_LINK: {"type": "text", "style": {"type": "url"}},
        FIELD_GENERATED_AT: {"type": "datetime", "style": {"format": "yyyy-MM-dd HH:mm"}},
    }


def validate_field_type(field: dict[str, Any], expected_types: set[int]) -> bool:
    try:
        return int(field.get("type", 0)) in expected_types
    except (TypeError, ValueError):
        return False


def source_field_issues(cfg: Config, fields: list[dict[str, Any]]) -> list[str]:
    by_name = fields_by_name(fields)
    issues: list[str] = []
    for name, expected_types in {
        FIELD_TABLE_STATUS: {TYPE_SINGLE_SELECT},
        FIELD_TABLE_ROWS: {TYPE_NUMBER},
        FIELD_TABLE_ERROR: {TYPE_TEXT},
        FIELD_TABLE_LINK: {TYPE_TEXT, TYPE_URL},
        FIELD_GENERATED_AT: {TYPE_DATE},
        FIELD_MEETING_DATE: {TYPE_DATE},
        FIELD_ARCHIVE_STATUS: {TYPE_SINGLE_SELECT, TYPE_TEXT},
        FIELD_ARCHIVE_LINK: {TYPE_URL, TYPE_TEXT},
    }.items():
        field = by_name.get(name)
        if not field:
            issues.append(f"missing field: {name}")
            continue
        if not validate_field_type(field, expected_types):
            issues.append(f"field type mismatch: {name}")
    if not any(name in by_name and validate_field_type(by_name[name], {TYPE_CHECKBOX}) for name in cfg.review_field_names):
        issues.append(f"missing review checkbox field: {'/'.join(cfg.review_field_names)}")
    status_field = by_name.get(FIELD_TABLE_STATUS)
    if status_field and validate_field_type(status_field, {TYPE_SINGLE_SELECT}):
        options = set(field_option_names(status_field))
        missing_options = [item for item in REQUIRED_STATUS_OPTIONS if item not in options]
        if missing_options:
            issues.append(f"missing {FIELD_TABLE_STATUS} options: {', '.join(missing_options)}")
    return issues


def init_fields(cfg: Config, apply: bool) -> dict[str, Any]:
    fields = list_bitable_fields(cfg, cfg.source_base_token, cfg.source_table_id)
    by_name = fields_by_name(fields)
    created: list[str] = []
    would_create: list[str] = []
    updated_options: list[str] = []
    would_update_options: list[str] = []
    errors: list[str] = []
    specs = required_source_field_specs()

    for name, spec in specs.items():
        field = by_name.get(name)
        if not field:
            if apply:
                create_bitable_field(cfg, name, spec)
                created.append(name)
            else:
                would_create.append(name)
            continue
        expected_types = {
            FIELD_TABLE_STATUS: {TYPE_SINGLE_SELECT},
            FIELD_TABLE_ROWS: {TYPE_NUMBER},
            FIELD_TABLE_ERROR: {TYPE_TEXT},
            FIELD_TABLE_LINK: {TYPE_TEXT, TYPE_URL},
            FIELD_GENERATED_AT: {TYPE_DATE},
        }[name]
        if not validate_field_type(field, expected_types):
            errors.append(f"field type mismatch: {name}")
            continue
        if name == FIELD_TABLE_STATUS:
            existing_options = field_option_names(field)
            missing_options = [item for item in REQUIRED_STATUS_OPTIONS if item not in set(existing_options)]
            if missing_options:
                if apply:
                    merged = [{"name": option} for option in [*existing_options, *missing_options]]
                    update_bitable_field(
                        cfg,
                        str(field.get("field_id")),
                        {"name": name, "type": "select", "multiple": False, "options": merged},
                    )
                    updated_options.append(name)
                else:
                    would_update_options.append(name)

    refreshed = list_bitable_fields(cfg, cfg.source_base_token, cfg.source_table_id) if apply else fields
    validation_issues = source_field_issues(cfg, refreshed)
    return {
        "ok": not errors and not validation_issues,
        "apply": apply,
        "created": created,
        "would_create": would_create,
        "updated_options": updated_options,
        "would_update_options": would_update_options,
        "errors": errors,
        "validation_issues": validation_issues,
    }


def plain_field_value(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        for key in ("text", "link", "url", "name", "value"):
            if value.get(key):
                return str(value[key])
        return ""
    if isinstance(value, list):
        parts = [plain_field_value(item) for item in value]
        return ",".join(part for part in parts if part)
    return str(value)


def url_from_field_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("link") or value.get("url") or value.get("text") or "")
    if isinstance(value, list):
        for item in value:
            url = url_from_field_value(item)
            if url:
                return url
    return ""


def checkbox_is_checked(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "y", "checked", "是"}
    if isinstance(value, dict):
        return any(checkbox_is_checked(value.get(key)) for key in ("checked", "value", "text") if key in value)
    if isinstance(value, list):
        return any(checkbox_is_checked(item) for item in value)
    return False


def ms_from_record_time(value: Any) -> int | None:
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        if stripped.isdigit():
            return int(stripped)
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"):
            try:
                return int(datetime.strptime(stripped, fmt).timestamp() * 1000)
            except ValueError:
                continue
    if isinstance(value, dict):
        for key in ("timestamp", "value", "date"):
            parsed = ms_from_record_time(value.get(key))
            if parsed is not None:
                return parsed
    return None


def date_text_from_ms(ms: int, offset_hours: int = 8) -> str:
    tz = timezone(timedelta(hours=offset_hours))
    return datetime.fromtimestamp(ms / 1000, tz=tz).strftime("%Y-%m-%d")


def month_from_date_text(date_text: str) -> str:
    parsed = datetime.strptime(date_text, "%Y-%m-%d")
    return parsed.strftime("%Y-%m")


def markdown_field(markdown: str, label: str) -> str:
    patterns = [
        rf"^\s*\*\*{re.escape(label)}\*\*\s*[:：]\s*(.+?)\s*$",
        rf"^\s*{re.escape(label)}\s*[:：]\s*(.+?)\s*$",
    ]
    for pattern in patterns:
        match = re.search(pattern, markdown, flags=re.M)
        if match:
            return match.group(1).strip().strip("*").strip()
    return ""


def normalize_date(value: str) -> str:
    value = str(value or "").strip()
    match = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", value)
    if not match:
        return ""
    year, month, day = match.groups()
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"


def parse_drive_url(url: str) -> tuple[str, str]:
    parsed = urllib.parse.urlparse(url)
    parts = [urllib.parse.unquote(part) for part in parsed.path.split("/") if part]
    segment_to_type = {
        "file": "file",
        "doc": "doc",
        "docx": "docx",
        "sheet": "sheet",
        "sheets": "sheet",
        "base": "bitable",
        "mindnote": "mindnote",
        "mindnotes": "mindnote",
        "slides": "slides",
    }
    for index, segment in enumerate(parts[:-1]):
        file_type = segment_to_type.get(segment)
        if file_type and parts[index + 1]:
            return parts[index + 1], file_type
    raise StructuredError("unsupported_archive_url", "Unsupported or invalid archive URL.", 400)


def get_file_meta(cfg: Config, file_token: str, file_type: str) -> dict[str, Any]:
    token = get_tenant_access_token(cfg)
    result = request_json(
        cfg,
        "POST",
        "/drive/v1/metas/batch_query",
        token=token,
        query={"user_id_type": cfg.user_id_type},
        body={"request_docs": [{"doc_token": file_token, "doc_type": file_type}], "with_url": True},
    )
    data = result.get("data", {})
    failed = data.get("failed_list") or []
    if failed:
        raise FeishuApiError(f"Failed to get file metadata: {failed}")
    metas = data.get("metas") or []
    if not metas:
        raise FeishuApiError("File metadata response contained no metas.")
    return metas[0]


def download_drive_file(cfg: Config, file_token: str) -> bytes:
    token = get_tenant_access_token(cfg)
    url = f"{cfg.openapi_base}/drive/v1/files/{urllib.parse.quote(file_token)}/download"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise FeishuApiError(f"HTTP {exc.code} download archive file: {detail[:300]}") from exc


def list_drive_folder_items(cfg: Config, folder_token: str) -> list[dict[str, Any]]:
    token = get_tenant_access_token(cfg)
    items: list[dict[str, Any]] = []
    page_token = ""
    while True:
        query: dict[str, Any] = {
            "folder_token": folder_token,
            "user_id_type": cfg.user_id_type,
            "page_size": 200,
        }
        if page_token:
            query["page_token"] = page_token
        result = request_json(cfg, "GET", "/drive/v1/files", token=token, query=query)
        data = result.get("data", {})
        items.extend(data.get("files") or data.get("items") or [])
        if not data.get("has_more"):
            return items
        page_token = str(data.get("next_page_token") or data.get("page_token") or "")
        if not page_token:
            return items


def folder_token_from_item(item: dict[str, Any]) -> str:
    return str(item.get("token") or item.get("folder_token") or item.get("file_token") or "")


def find_child_folder(cfg: Config, parent_folder_token: str, name: str) -> dict[str, Any] | None:
    for item in list_drive_folder_items(cfg, parent_folder_token):
        if item.get("type") == "folder" and item.get("name") == name:
            return item
    return None


def create_drive_folder(cfg: Config, parent_folder_token: str, name: str) -> dict[str, Any]:
    token = get_tenant_access_token(cfg)
    result = request_json(
        cfg,
        "POST",
        "/drive/v1/files/create_folder",
        token=token,
        body={"name": name, "folder_token": parent_folder_token},
    )
    return result.get("data", {}).get("folder", {}) or result.get("data", {}) or {}


def ensure_child_folder(cfg: Config, parent_folder_token: str, name: str) -> str:
    existing = find_child_folder(cfg, parent_folder_token, name)
    if existing:
        token = folder_token_from_item(existing)
        if not token:
            raise FeishuApiError(f"Existing folder {name} has no token.")
        return token
    created = create_drive_folder(cfg, parent_folder_token, name)
    token = folder_token_from_item(created)
    if not token:
        raise FeishuApiError(f"Create folder response did not include token for {name}.")
    return token


def subscribe_folder(cfg: Config, folder_token: str) -> str:
    token = get_tenant_access_token(cfg)
    path = f"/drive/v1/files/{urllib.parse.quote(folder_token)}/subscribe"
    try:
        request_json(
            cfg,
            "POST",
            path,
            token=token,
            query={"file_type": "folder", "event_type": FILE_CREATED_SUBSCRIBE_EVENT_TYPE},
        )
        return "subscribed"
    except FeishuApiError as exc:
        text = exc.message.lower()
        if "already" in text or "exist" in text or "repeat" in text or "duplicate" in text:
            return "already_subscribed"
        raise


def load_folder_registry(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"months": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise StructuredError("invalid_registry", f"Invalid folder registry JSON: {path}") from exc
    if not isinstance(data, dict):
        raise StructuredError("invalid_registry", f"Folder registry must be a JSON object: {path}")
    data.setdefault("months", {})
    return data


def save_folder_registry(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def ensure_month_folders(cfg: Config, month: str) -> dict[str, Any]:
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        raise StructuredError("invalid_month", "Month must use YYYY-MM.")
    registry = load_folder_registry(cfg.folder_registry_path)
    entry = registry.setdefault("months", {}).setdefault(month, {})
    if not isinstance(entry, dict):
        raise StructuredError("invalid_registry", f"Folder registry entry for {month} is invalid.")

    source_token = str(entry.get("source_folder_token") or "")
    archive_token = str(entry.get("archive_folder_token") or "")
    if not source_token:
        source_token = ensure_child_folder(cfg, cfg.structured_pending_folder_token, month)
    if not archive_token:
        archive_token = ensure_child_folder(cfg, cfg.structured_archive_folder_token, month)
    subscribe_status = subscribe_folder(cfg, source_token)

    entry.update(
        {
            "source_parent_folder_token": cfg.structured_pending_folder_token,
            "source_folder_token": source_token,
            "archive_parent_folder_token": cfg.structured_archive_folder_token,
            "archive_folder_token": archive_token,
            "subscribed": True,
            "subscribe_status": subscribe_status,
            "updated_at": int(time.time() * 1000),
        }
    )
    save_folder_registry(cfg.folder_registry_path, registry)
    return {"month": month, "source_folder_token": source_token, "archive_folder_token": archive_token}


def unique_upload_name(original_name: str, existing_names: set[str]) -> str:
    if original_name not in existing_names:
        return original_name
    path = Path(original_name)
    stem = path.stem
    suffix = path.suffix
    for index in range(2, 10000):
        candidate = f"{stem} ({index}){suffix}"
        if candidate not in existing_names:
            return candidate
    raise StructuredError("duplicate_name_exhausted", "Could not generate a unique upload name.", 409)


def encode_multipart_upload(file_name: str, parent_node: str, data: bytes) -> tuple[str, bytes]:
    boundary = "----feishu-structured-generate-" + secrets.token_hex(16)
    parts: list[bytes] = []

    def add_field(name: str, value: str) -> None:
        parts.append(f"--{boundary}\r\n".encode("utf-8"))
        parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        parts.append(value.encode("utf-8"))
        parts.append(b"\r\n")

    add_field("file_name", file_name)
    add_field("parent_type", "explorer")
    add_field("parent_node", parent_node)
    add_field("size", str(len(data)))
    safe_name = file_name.replace('"', "")
    parts.append(f"--{boundary}\r\n".encode("utf-8"))
    parts.append(
        (
            f'Content-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'
            "Content-Type: text/markdown\r\n\r\n"
        ).encode("utf-8")
    )
    parts.append(data)
    parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return f"multipart/form-data; boundary={boundary}", b"".join(parts)


def upload_markdown_file(cfg: Config, folder_token: str, file_name: str, data: bytes) -> str:
    token = get_tenant_access_token(cfg)
    content_type, body = encode_multipart_upload(file_name, folder_token, data)
    url = f"{cfg.openapi_base}/drive/v1/files/upload_all"
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": content_type},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise FeishuApiError(f"HTTP {exc.code} upload structured file: {detail[:300]}") from exc
    result = json.loads(payload)
    if result.get("code", 0) != 0:
        raise FeishuApiError(f"Feishu upload failed: {str(result)[:300]}")
    file_token = result.get("data", {}).get("file_token")
    if not file_token:
        raise FeishuApiError("Upload response did not include file_token.")
    return str(file_token)


def resolve_uploaded_file_url(cfg: Config, folder_token: str, file_token: str, file_name: str) -> str:
    items = list_drive_folder_items(cfg, folder_token)
    for item in items:
        if str(item.get("token") or item.get("file_token") or "") == file_token and item.get("url"):
            return str(item["url"])
    for item in items:
        if item.get("name") == file_name and item.get("url"):
            return str(item["url"])
    raise StructuredError("uploaded_url_missing", "Uploaded file URL could not be resolved.", 500)


def source_link_value(cfg: Config, fields_by_name_value: dict[str, dict[str, Any]], url: str, text: str) -> Any:
    field = fields_by_name_value.get(FIELD_TABLE_LINK, {})
    if validate_field_type(field, {TYPE_URL}):
        return {"text": text, "link": url}
    return url


def update_source_status(
    cfg: Config,
    record_id: str,
    status: str,
    *,
    table_link_url: str = "",
    table_link_text: str = "",
    row_count: int | None = None,
    error: str = "",
    fields: list[dict[str, Any]] | None = None,
) -> None:
    field_map = fields_by_name(fields or list_bitable_fields(cfg, cfg.source_base_token, cfg.source_table_id))
    payload: dict[str, Any] = {
        FIELD_TABLE_STATUS: status,
        FIELD_GENERATED_AT: int(time.time() * 1000),
        FIELD_TABLE_ERROR: truncate_error(error, cfg.max_error_chars) if error else "",
    }
    if row_count is not None:
        payload[FIELD_TABLE_ROWS] = row_count
    if table_link_url:
        payload[FIELD_TABLE_LINK] = source_link_value(cfg, field_map, table_link_url, table_link_text or table_link_url)
    update_bitable_record(cfg, record_id, payload)


def truncate_error(error: str, max_chars: int) -> str:
    text = re.sub(r"\s+", " ", str(error or "")).strip()
    return text[:max_chars]


def now_shanghai_iso() -> str:
    return datetime.now(timezone(timedelta(hours=8))).replace(microsecond=0).isoformat()


def output_file_name_from_source(source_file_name: str) -> str:
    name = Path(source_file_name.replace("\x00", "")).name
    if not name:
        name = "结构化表格.md"
    if name.lower().endswith(".md"):
        name = name[:-3]
    return f"{name} - 结构化表格.md"


def run_skill(
    cfg: Config,
    *,
    source_markdown_path: Path,
    output_path: Path,
    json_output_path: Path,
    source_record_id: str,
    source_archive_url: str,
    source_file_name: str,
    meeting_date: str,
) -> list[dict[str, Any]]:
    if not cfg.skill_script.exists():
        raise StructuredError("skill_missing", f"Skill script does not exist: {cfg.skill_script}", 500)
    cmd = [
        sys.executable,
        str(cfg.skill_script),
        "--meeting-markdown",
        str(source_markdown_path),
        "--output",
        str(output_path),
        "--json-output",
        str(json_output_path),
        "--source-record-id",
        source_record_id,
        "--source-archive-url",
        source_archive_url,
        "--source-file-name",
        source_file_name,
        "--generated-at",
        now_shanghai_iso(),
    ]
    if meeting_date:
        cmd.extend(["--meeting-date", meeting_date])
    result = subprocess.run(cmd, text=True, capture_output=True, timeout=120, check=False)
    if result.returncode != 0:
        stderr = truncate_error(result.stderr or result.stdout, cfg.max_error_chars)
        raise StructuredError("skill_failed", f"Skill failed: {stderr}", 500)
    try:
        rows = json.loads(json_output_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise StructuredError("skill_bad_json", "Skill JSON output could not be parsed.", 500) from exc
    if not isinstance(rows, list):
        raise StructuredError("skill_bad_json", "Skill JSON output must be an array.", 500)
    return rows


def save_local_backup(cfg: Config, month: str, file_name: str, content: bytes) -> Path:
    target_dir = cfg.output_dir / month
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / file_name
    target_path.write_bytes(content)
    index_path = cfg.output_dir / "index.jsonl"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    return target_path


def append_index(cfg: Config, item: dict[str, Any]) -> None:
    index_path = cfg.output_dir / "index.jsonl"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    with index_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")


def record_review_ok(cfg: Config, fields: dict[str, Any]) -> bool:
    return any(checkbox_is_checked(fields.get(name)) for name in cfg.review_field_names)


def get_record_meeting_date(fields: dict[str, Any]) -> str:
    ms = ms_from_record_time(fields.get(FIELD_MEETING_DATE))
    return date_text_from_ms(ms) if ms is not None else ""


def resolve_meeting_date(fields: dict[str, Any], markdown: str, source_file_name: str) -> str:
    source_date = get_record_meeting_date(fields)
    markdown_date = normalize_date(markdown_field(markdown, "会议日期"))
    if source_date:
        if markdown_date and markdown_date != source_date:
            logging.warning("meeting_date_mismatch source=%s markdown=%s", source_date, markdown_date)
        return source_date
    if markdown_date:
        return markdown_date
    file_date = normalize_date(source_file_name)
    if file_date:
        return file_date
    raise StructuredError("meeting_date_missing", "无法确定会议日期。", 500)


def generate_for_record(cfg: Config, record_id: str) -> tuple[int, dict[str, Any]]:
    source_fields_list = list_bitable_fields(cfg, cfg.source_base_token, cfg.source_table_id)
    issues = source_field_issues(cfg, source_fields_list)
    if issues:
        return 500, {"ok": False, "status": "config_error", "error_code": "field_config_error", "issues": issues}

    record = get_bitable_record(cfg, record_id)
    fields = record.get("fields", {})
    if not isinstance(fields, dict):
        raise StructuredError("invalid_record", "Record response has no fields.", 500)

    existing_status = plain_field_value(fields.get(FIELD_TABLE_STATUS))
    existing_link = url_from_field_value(fields.get(FIELD_TABLE_LINK))
    if existing_status == STATUS_GENERATED and existing_link:
        return 200, {"ok": True, "status": "skipped_existing", "record_id": record_id}
    if existing_status == STATUS_NO_ROWS:
        return 200, {"ok": True, "status": "skipped_no_rows", "record_id": record_id}
    if existing_status == STATUS_RUNNING:
        return 409, {"ok": False, "status": "already_running", "record_id": record_id}
    if not record_review_ok(cfg, fields):
        return 409, {"ok": False, "status": "not_ready", "reason": "review_not_checked", "record_id": record_id}
    if plain_field_value(fields.get(FIELD_ARCHIVE_STATUS)) != "已归档":
        return 409, {"ok": False, "status": "not_ready", "reason": "archive_status_not_done", "record_id": record_id}
    archive_url = url_from_field_value(fields.get(FIELD_ARCHIVE_LINK))
    if not archive_url:
        return 409, {"ok": False, "status": "not_ready", "reason": "archive_link_missing", "record_id": record_id}

    update_source_status(cfg, record_id, STATUS_RUNNING, fields=source_fields_list)
    try:
        result = process_record_after_lock(cfg, record_id, fields, archive_url, source_fields_list)
        return 200, result
    except Exception as exc:
        error = truncate_error(str(exc), cfg.max_error_chars)
        try:
            update_source_status(cfg, record_id, STATUS_FAILED, error=error, fields=source_fields_list)
        except Exception:
            logging.exception("Failed to write failure status for record_id=%s", record_id)
        logging.exception("generate_failed record_id=%s", record_id)
        return 500, {"ok": False, "status": "failed", "error_code": getattr(exc, "error_code", "failed"), "message": error}


def process_record_after_lock(
    cfg: Config,
    record_id: str,
    fields: dict[str, Any],
    archive_url: str,
    source_fields_list: list[dict[str, Any]],
) -> dict[str, Any]:
    file_token, file_type = parse_drive_url(archive_url)
    if file_type != "file":
        raise StructuredError("unsupported_archive_type", "仅支持已归档 Markdown 文件。", 500)
    meta = get_file_meta(cfg, file_token, file_type)
    source_file_name = str(meta.get("name") or meta.get("title") or plain_field_value(fields.get(FIELD_FILE_NAME)) or file_token)
    if not source_file_name.lower().endswith(".md"):
        raise StructuredError("unsupported_archive_type", "仅支持已归档 Markdown 文件。", 500)
    markdown_bytes = download_drive_file(cfg, file_token)
    try:
        markdown_text = markdown_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StructuredError("archive_decode_failed", "归档 Markdown 不是 UTF-8 文本。", 500) from exc
    meeting_date = resolve_meeting_date(fields, markdown_text, source_file_name)
    month = month_from_date_text(meeting_date)
    output_name = output_file_name_from_source(source_file_name)

    with tempfile.TemporaryDirectory(prefix="feishu-structured-") as tmpdir:
        tmp_path = Path(tmpdir)
        source_path = tmp_path / "source.md"
        output_path = tmp_path / "structured.md"
        json_path = tmp_path / "structured.json"
        source_path.write_text(markdown_text, encoding="utf-8")
        rows = run_skill(
            cfg,
            source_markdown_path=source_path,
            output_path=output_path,
            json_output_path=json_path,
            source_record_id=record_id,
            source_archive_url=archive_url,
            source_file_name=source_file_name,
            meeting_date=meeting_date,
        )
        row_count = len(rows)
        if row_count == 0:
            update_source_status(cfg, record_id, STATUS_NO_ROWS, row_count=0, fields=source_fields_list)
            return {"ok": True, "status": "no_rows", "record_id": record_id, "row_count": 0}

        month_folders = ensure_month_folders(cfg, month)
        target_folder = str(month_folders["source_folder_token"])
        existing_names = {str(item.get("name")) for item in list_drive_folder_items(cfg, target_folder) if item.get("name")}
        upload_name = unique_upload_name(output_name, existing_names)
        content = output_path.read_bytes()
        file_token = upload_markdown_file(cfg, target_folder, upload_name, content)
        uploaded_url = resolve_uploaded_file_url(cfg, target_folder, file_token, upload_name)
        backup_path = save_local_backup(cfg, month, upload_name, content)
        append_index(
            cfg,
            {
                "source_record_id": record_id,
                "meeting_date": meeting_date,
                "row_count": row_count,
                "local_path": str(backup_path),
                "feishu_url": uploaded_url,
                "generated_at": now_shanghai_iso(),
                "file_name": upload_name,
            },
        )
        update_source_status(
            cfg,
            record_id,
            STATUS_GENERATED,
            table_link_url=uploaded_url,
            table_link_text=upload_name,
            row_count=row_count,
            fields=source_fields_list,
        )
        return {
            "ok": True,
            "status": "generated",
            "record_id": record_id,
            "row_count": row_count,
            "month": month,
            "file_name": upload_name,
            "url": uploaded_url,
            "local_path": str(backup_path),
        }


def health_payload(cfg: Config) -> dict[str, Any]:
    return {
        "ok": True,
        "service": SERVICE_NAME,
        "http_token_configured": bool(cfg.structured_http_token),
        "skill_script_exists": cfg.skill_script.exists(),
        "output_dir": str(cfg.output_dir),
    }


def doctor(cfg: Config, online: bool = False) -> int:
    status: dict[str, Any] = {
        "ok": True,
        "service": SERVICE_NAME,
        "python": sys.version.split()[0],
        "http_token_configured": bool(cfg.structured_http_token),
        "skill_script_exists": cfg.skill_script.exists(),
        "output_dir": str(cfg.output_dir),
        "folder_registry_path": str(cfg.folder_registry_path),
    }
    if online:
        try:
            fields = list_bitable_fields(cfg, cfg.source_base_token, cfg.source_table_id)
            issues = source_field_issues(cfg, fields)
            status["source_field_count"] = len(fields)
            status["source_field_issues"] = issues
            status["ok"] = not issues and status["skill_script_exists"]
        except Exception as exc:
            status["ok"] = False
            status["online_error"] = truncate_error(str(exc), cfg.max_error_chars)
    print(json.dumps(status, ensure_ascii=False, indent=2))
    return 0 if status["ok"] else 1


def make_handler(cfg: Config) -> type[BaseHTTPRequestHandler]:
    class StructuredHandler(BaseHTTPRequestHandler):
        server_version = "FeishuStructuredGenerate/0.1.0"

        def log_message(self, format: str, *args: Any) -> None:
            logging.info("http %s - %s", self.client_address[0], format % args)

        def write_json(self, status_code: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            path = urllib.parse.urlparse(self.path).path
            if path == "/healthz":
                self.write_json(200, health_payload(cfg))
                return
            self.write_json(404, {"ok": False, "error_code": "not_found"})

        def do_POST(self) -> None:
            path = urllib.parse.urlparse(self.path).path
            if path != "/generate":
                self.write_json(404, {"ok": False, "error_code": "not_found"})
                return
            if not cfg.structured_http_token:
                self.write_json(500, {"ok": False, "status": "config_error", "error_code": "structured_http_token_not_configured"})
                return
            if self.headers.get("X-Structured-Token", "") != cfg.structured_http_token:
                self.write_json(401, {"ok": False, "error_code": "unauthorized"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.write_json(400, {"ok": False, "error_code": "invalid_content_length"})
                return
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self.write_json(400, {"ok": False, "error_code": "invalid_json"})
                return
            record_id = str(payload.get("record_id") or payload.get("recordId") or "").strip()
            if not record_id:
                self.write_json(400, {"ok": False, "error_code": "missing_record_id"})
                return
            status_code, result = generate_for_record(cfg, record_id)
            self.write_json(status_code, result)

    return StructuredHandler


def serve(cfg: Config) -> None:
    server = ThreadingHTTPServer((cfg.http_host, cfg.http_port), make_handler(cfg))
    logging.info("Starting %s on %s:%s", SERVICE_NAME, cfg.http_host, cfg.http_port)
    server.serve_forever()


def init_config(force: bool = False) -> None:
    target = Path(__file__).resolve().parent / ".env.example"
    if target.exists() and not force:
        print(f"{target} already exists")
        return
    target.write_text(
        """FEISHU_APP_ID=
FEISHU_APP_SECRET=

FEISHU_SOURCE_BITABLE_APP_TOKEN=
FEISHU_SOURCE_TABLE_ID=
FEISHU_SOURCE_REVIEW_FIELD_NAMES=审核状态,已审核

FEISHU_STRUCTURED_BITABLE_APP_TOKEN=
FEISHU_STRUCTURED_TABLE_ID=
FEISHU_STRUCTURED_PENDING_FOLDER_TOKEN=
FEISHU_STRUCTURED_ARCHIVE_FOLDER_TOKEN=

FEISHU_STRUCTURED_HTTP_TOKEN=
FEISHU_STRUCTURED_HTTP_HOST=127.0.0.1
FEISHU_STRUCTURED_HTTP_PORT=8790
FEISHU_USER_ID_TYPE=open_id
FEISHU_LOG_LEVEL=INFO

STRUCTURED_TABLE_SKILL_SCRIPT=/skills/meeting-minutes-structured-table/scripts/generate_table.py
STRUCTURED_OUTPUT_DIR=/app/structured_outputs
STRUCTURED_FOLDER_REGISTRY_PATH=data/structured_folder_registry.json

FEISHU_ENABLE_LEGACY_STRUCTURED_SERVICE=
""",
        encoding="utf-8",
    )


def write_secret_file(path_value: str, token: str) -> None:
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token + "\n", encoding="utf-8")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def make_http_token(args: argparse.Namespace) -> int:
    token = "fsg_" + secrets.token_urlsafe(32)
    if args.write_token_file:
        write_secret_file(args.write_token_file, token)
        print(f"token_file: {args.write_token_file}")
    else:
        print(token)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="structured_generate_service.py")
    sub = parser.add_subparsers(dest="command", required=True)
    doctor_parser = sub.add_parser("doctor")
    doctor_parser.add_argument("--online", action="store_true")
    sub.add_parser("serve")
    init_fields_parser = sub.add_parser("init-fields")
    init_fields_parser.add_argument("--apply", action="store_true")
    init_config_parser = sub.add_parser("init-config")
    init_config_parser.add_argument("--force", action="store_true")
    token_parser = sub.add_parser("make-token")
    token_parser.add_argument("--write-token-file")
    generate_parser = sub.add_parser("generate-record")
    generate_parser.add_argument("record_id")
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    args = build_parser().parse_args(argv)
    if args.command == "make-token":
        return make_http_token(args)
    if args.command == "init-config":
        init_config(force=args.force)
        return 0
    if args.command in {"serve", "init-fields", "generate-record"} and os.environ.get(
        "FEISHU_ENABLE_LEGACY_STRUCTURED_SERVICE", ""
    ) != "I_UNDERSTAND_THIS_IS_LEGACY":
        raise SystemExit(
            "Legacy structured service is disabled. Use "
            ".implementation/version-retention/feishu-structured-generate instead."
        )
    cfg = read_config()
    if args.command == "doctor":
        return doctor(cfg, online=args.online)
    if args.command == "init-fields":
        print(json.dumps(init_fields(cfg, apply=args.apply), ensure_ascii=False, indent=2))
        return 0
    if args.command == "generate-record":
        status_code, result = generate_for_record(cfg, args.record_id)
        print(json.dumps({"http_status": status_code, **result}, ensure_ascii=False, indent=2))
        return 0 if status_code < 400 else 1
    if args.command == "serve":
        serve(cfg)
        return 0
    raise SystemExit(f"Unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
