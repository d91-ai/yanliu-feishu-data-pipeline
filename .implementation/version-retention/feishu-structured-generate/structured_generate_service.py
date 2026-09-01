#!/usr/bin/env python3
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import fcntl
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
import threading
import time
from typing import Any, Mapping
import urllib.error
import urllib.parse
import urllib.request
import uuid

from skill_contract import load_skill_contract


SERVICE_NAME = "feishu-structured-generate"
OPENAPI_BASE = "https://open.feishu.cn/open-apis"
FILE_CREATED_SUBSCRIBE_EVENT_TYPE = "file.created_in_folder_v1"

TYPE_TEXT = 1
TYPE_NUMBER = 2
TYPE_SINGLE_SELECT = 3
TYPE_DATE = 5
TYPE_CHECKBOX = 7
TYPE_URL = 15
TYPE_LINK = 18
TYPE_DUPLEX_LINK = 21

STATUS_RUNNING = "生成中"
STATUS_GENERATED = "已生成"
STATUS_NO_ROWS = "无可结构化标的"
STATUS_FAILED = "生成失败"
REQUIRED_STATUS_OPTIONS = [STATUS_RUNNING, STATUS_GENERATED, STATUS_NO_ROWS, STATUS_FAILED]

OFFICIAL_JSON_TABLE_NAME = "正式JSON"
OFFICIAL_JSON_STATUS_UP_TO_DATE = "无需生成"

FIELD_TABLE_STATUS = "表格生成状态"
FIELD_TABLE_LINK = "表格链接"
FIELD_GENERATED_AT = "生成时间"
FIELD_TABLE_ROWS = "表格行数"
FIELD_TABLE_ERROR = "表格生成错误"
FIELD_MEETING_DATE = "会议日期"
FIELD_MEETING_SERIES = "会议系列"
FIELD_ARCHIVE_STATUS = "归档状态"
FIELD_SOURCE_ARCHIVE_LINK = "归档链接"
FIELD_STRUCTURED_ARCHIVE_LINK = "审核后归档MD链接"
FIELD_VERSION_STATUS = "版本留存状态"
FIELD_APPROVED_SHA256 = "审核后内容SHA256"
FIELD_FILE_NAME = "文件名"
FIELD_MEETING_UID = "会议UID"
FIELD_MEETING_TYPE = "会议类型"
FIELD_DOCUMENT_SOURCE = "文档来源"
FIELD_SOURCE_RECORD = "源纪要记录"
FIELD_SOURCE_LINK = "源纪要链接"

FIELD_STRUCTURED_TABLE_NAME = "表格名"
FIELD_STRUCTURED_MD_LINK = "待审核MD链接"
FIELD_STRUCTURED_VIEWPOINT_COUNT = "观点数"
FIELD_STRUCTURED_APPROVED = "已审核"
FIELD_STRUCTURED_CURRENT_MD_HASH = "当前MD字段hash"
FIELD_STRUCTURED_JSON_STATUS = "JSON状态"
FIELD_STRUCTURED_JSON_LINK = "正式JSON链接"
FIELD_STRUCTURED_JSON_ROW_COUNT = "JSON行数"
FIELD_STRUCTURED_JSON_GENERATED_AT = "JSON生成时间"
FIELD_STRUCTURED_JSON_SOURCE_MD_HASH = "JSON来源MD字段hash"
FIELD_STRUCTURED_NEEDS_JSON_REGEN = "需要重新生成JSON"
FIELD_STRUCTURED_ERROR = "错误信息"
FIELD_BASELINE_LINK = "审核前版本链接"
FIELD_STRUCTURED_BASELINE_LINK = "审核前基线MD链接"
FIELD_BASELINE_VERSION = "审核前文件版本号"
FIELD_BASELINE_SHA256 = "审核前内容SHA256"
FIELD_APPROVED_VERSION = "审核后文件版本号"
FIELD_VERSION_DIFF = "版本差异"
FIELD_VERSION_ERROR = "版本留存错误"
FIELD_ARCHIVE_TIME = "归档时间"

VERSION_STATUS_PENDING = "待留存"
VERSION_DIFF_PENDING = "未比较"

FIELD_OFFICIAL_JSON_FILE = "JSON文件"
FIELD_OFFICIAL_SOURCE_MD_RECORD = "源MD记录"
FIELD_OFFICIAL_SOURCE_MD_LINK = "源MD链接"
FIELD_OFFICIAL_SOURCE_MD_HASH = "源MD字段hash"
FIELD_OFFICIAL_JSON_LINK = "JSON链接"
FIELD_OFFICIAL_JSON_ROW_COUNT = "JSON行数"
FIELD_OFFICIAL_STATUS = "生成状态"
FIELD_OFFICIAL_GENERATED_AT = "生成时间"
FIELD_OFFICIAL_SOURCE_BASE_STATUS = "源Base审核状态"
FIELD_OFFICIAL_ERROR = "错误信息"

DEFAULT_REVIEW_FIELD_NAMES = ("审核状态", "已审核")
MEETING_UID_PATTERN = re.compile(r"^mtg_[0-9a-f]{32}$")


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
    official_json_table_id: str
    structured_pending_folder_token: str
    structured_archive_folder_token: str
    structured_official_json_folder_token: str
    structured_http_token: str
    skill_script: Path
    skill_script_sha256: str
    skill_json_script: Path
    skill_json_script_sha256: str
    output_dir: Path
    folder_registry_path: Path
    skill_contract_version: int = 0
    skill_runtime_sha256: str = ""
    skill_contract_manifest: Path | None = None
    skill_prompt_path: Path | None = None
    skill_claim_schema_path: Path | None = None
    security_master_path: Path | None = None
    security_master_cli_flag: str = "--security-master"
    semantic_job_dir: Path | None = None
    source_version_retention_enforce: bool = True
    structured_version_retention_enforce: bool = True
    http_host: str = "127.0.0.1"
    http_port: int = 8790
    openapi_base: str = OPENAPI_BASE
    user_id_type: str = "open_id"
    review_field_names: tuple[str, ...] = DEFAULT_REVIEW_FIELD_NAMES
    max_error_chars: int = 300
    max_http_body_bytes: int = 4096
    output_owner_open_id: str = ""
    structured_baseline_http_url: str = ""
    structured_baseline_http_token: str = ""
    structured_baseline_http_env_file: Path | None = None


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


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise StructuredError("pinned_file_unreadable", "Pinned runtime file is not readable.", 503) from exc
    return digest.hexdigest()


def require_pinned_file(path: Path, expected_sha256: str, label: str) -> None:
    expected = str(expected_sha256 or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise StructuredError("invalid_config", f"{label} SHA256 pin is missing or invalid.", 500)
    if path.is_symlink() or not path.is_file():
        raise StructuredError("pinned_file_invalid", f"{label} must be a regular non-symlink file.", 503)
    if file_sha256(path) != expected:
        raise StructuredError("pinned_file_hash_mismatch", f"{label} does not match its configured SHA256.", 503)


def require_pinned_skill_runtime(cfg: Config) -> None:
    try:
        current = load_skill_contract(cfg.skill_script).runtime_sha256
    except RuntimeError as exc:
        raise StructuredError("invalid_skill_contract", str(exc), 503) from exc
    if current != cfg.skill_runtime_sha256:
        raise StructuredError(
            "pinned_skill_runtime_hash_mismatch",
            "Structured Skill runtime tree does not match its configured SHA256.",
            503,
        )


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


def bool_from_value(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def version_enforce_for_table(env: Mapping[str, str], table_id: str, env_name: str, base_dir: Path) -> bool:
    if env_name in env:
        return bool_from_value(env.get(env_name), False)
    raw_path = str(env.get("FEISHU_VERSION_CONFIG_PATH", "data/version_retention.json")).strip()
    if not raw_path:
        return False
    path = path_from_env(raw_path, base_dir)
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StructuredError("invalid_config", f"Invalid version retention config: {exc}") from exc
    tables = payload.get("tables", {}) if isinstance(payload, dict) else {}
    settings = tables.get(table_id, {}) if isinstance(tables, dict) else {}
    if not isinstance(settings, dict):
        raise StructuredError("invalid_config", f"Invalid version retention settings for table {table_id}")
    return bool_from_value(settings.get("enforce"), False)


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
        "FEISHU_STRUCTURED_OFFICIAL_JSON_FOLDER_TOKEN",
    ]
    missing = [name for name in required_names if not str(env.get(name, "")).strip()]
    if missing:
        raise StructuredError("invalid_config", f"Missing required config: {', '.join(missing)}")
    skill_script = path_from_env(
        str(env.get("STRUCTURED_TABLE_SKILL_SCRIPT", "/skills/meeting-minutes-structured-table/scripts/generate_table.py")),
        base_dir,
    )
    try:
        skill_contract = load_skill_contract(skill_script)
    except RuntimeError as exc:
        raise StructuredError("invalid_skill_contract", str(exc)) from exc
    if skill_contract.contract_version != 9 or skill_contract.schema_version != 9:
        raise StructuredError(
            "invalid_skill_contract",
            "Persistent deployment source requires Skill contract v9 / schema v9.",
        )
    skill_json_script = skill_contract.generate_script
    output_dir = path_from_env(str(env.get("STRUCTURED_OUTPUT_DIR", "data/structured_outputs")), base_dir)
    registry_path = path_from_env(str(env.get("STRUCTURED_FOLDER_REGISTRY_PATH", "data/structured_folder_registry.json")), base_dir)
    semantic_job_dir = path_from_env(
        str(env.get("STRUCTURED_SEMANTIC_JOB_DIR", str(output_dir / ".semantic-jobs"))),
        base_dir,
    )
    security_master_path = skill_contract.security_master_path
    review_names = tuple(
        item.strip()
        for item in re.split(r"[,;，；\s]+", str(env.get("FEISHU_SOURCE_REVIEW_FIELD_NAMES", ",".join(DEFAULT_REVIEW_FIELD_NAMES))))
        if item.strip()
    )
    source_enforce = version_enforce_for_table(
        env,
        str(env["FEISHU_SOURCE_TABLE_ID"]).strip(),
        "FEISHU_SOURCE_VERSION_RETENTION_ENFORCE",
        base_dir,
    )
    structured_enforce = version_enforce_for_table(
        env,
        str(env["FEISHU_STRUCTURED_TABLE_ID"]).strip(),
        "FEISHU_STRUCTURED_VERSION_RETENTION_ENFORCE",
        base_dir,
    )
    if not source_enforce or not structured_enforce:
        raise StructuredError(
            "version_retention_required",
            "Source and structured version-retention enforcement must both be enabled.",
            500,
        )
    config = Config(
        app_id=str(env["FEISHU_APP_ID"]).strip(),
        app_secret=str(env["FEISHU_APP_SECRET"]).strip(),
        source_base_token=str(env["FEISHU_SOURCE_BITABLE_APP_TOKEN"]).strip(),
        source_table_id=str(env["FEISHU_SOURCE_TABLE_ID"]).strip(),
        structured_base_token=str(env["FEISHU_STRUCTURED_BITABLE_APP_TOKEN"]).strip(),
        structured_table_id=str(env["FEISHU_STRUCTURED_TABLE_ID"]).strip(),
        official_json_table_id=str(env.get("FEISHU_OFFICIAL_JSON_TABLE_ID", OFFICIAL_JSON_TABLE_NAME)).strip() or OFFICIAL_JSON_TABLE_NAME,
        structured_pending_folder_token=str(env["FEISHU_STRUCTURED_PENDING_FOLDER_TOKEN"]).strip(),
        structured_archive_folder_token=str(env["FEISHU_STRUCTURED_ARCHIVE_FOLDER_TOKEN"]).strip(),
        structured_official_json_folder_token=str(env.get("FEISHU_STRUCTURED_OFFICIAL_JSON_FOLDER_TOKEN", "")).strip(),
        structured_http_token=str(env.get("FEISHU_STRUCTURED_HTTP_TOKEN", "")).strip(),
        skill_script=skill_script,
        skill_script_sha256=file_sha256(skill_contract.generate_script),
        skill_json_script=skill_json_script,
        skill_json_script_sha256=file_sha256(skill_contract.generate_script),
        output_dir=output_dir,
        folder_registry_path=registry_path,
        skill_contract_version=skill_contract.schema_version,
        skill_runtime_sha256=skill_contract.runtime_sha256,
        skill_contract_manifest=skill_contract.manifest_path,
        skill_prompt_path=skill_contract.prompt_path,
        skill_claim_schema_path=skill_contract.claim_schema_path,
        security_master_path=security_master_path,
        security_master_cli_flag=skill_contract.security_master_cli_flag,
        semantic_job_dir=semantic_job_dir,
        source_version_retention_enforce=source_enforce,
        structured_version_retention_enforce=structured_enforce,
        http_host=str(env.get("FEISHU_STRUCTURED_HTTP_HOST", "127.0.0.1")).strip() or "127.0.0.1",
        http_port=int_from_env(env, "FEISHU_STRUCTURED_HTTP_PORT", 8790),
        openapi_base=str(env.get("FEISHU_OPENAPI_BASE", OPENAPI_BASE)).strip().rstrip("/") or OPENAPI_BASE,
        user_id_type=str(env.get("FEISHU_USER_ID_TYPE", "open_id")).strip() or "open_id",
        review_field_names=review_names or DEFAULT_REVIEW_FIELD_NAMES,
        max_error_chars=int_from_env(env, "STRUCTURED_MAX_ERROR_CHARS", 300),
        max_http_body_bytes=int_from_env(env, "STRUCTURED_MAX_HTTP_BODY_BYTES", 4096),
        output_owner_open_id=str(env.get("FEISHU_OUTPUT_OWNER_OPEN_ID", "")).strip(),
        structured_baseline_http_url=str(
            env.get(
                "FEISHU_STRUCTURED_BASELINE_HTTP_URL",
                "http://host.docker.internal:8788/capture-baseline",
            )
        ).strip(),
        structured_baseline_http_token=str(
            env.get("FEISHU_STRUCTURED_BASELINE_HTTP_TOKEN", "")
        ).strip(),
        structured_baseline_http_env_file=path_from_env(
            str(
                env.get(
                    "FEISHU_STRUCTURED_BASELINE_HTTP_ENV_FILE",
                    "/run/secrets/structured-archive.env",
                )
            ),
            base_dir,
        ),
    )
    require_pinned_file(config.skill_script, config.skill_script_sha256, "Structured skill script")
    require_pinned_file(
        config.skill_json_script,
        config.skill_json_script_sha256,
        "Structured Skill JSON entrypoint",
    )
    return config


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
        raise FeishuApiError(f"Feishu OpenAPI returned HTTP {exc.code}.") from exc
    except urllib.error.URLError as exc:
        raise FeishuApiError("Could not reach Feishu OpenAPI.") from exc
    if not payload:
        return {}
    try:
        result = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise FeishuApiError("Feishu OpenAPI returned invalid JSON.") from exc
    if result.get("code", 0) != 0:
        code = result.get("code")
        remote_code = str(code) if isinstance(code, int) and not isinstance(code, bool) else "unknown"
        raise FeishuApiError(f"Feishu OpenAPI rejected the request (code={remote_code}).")
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


def list_bitable_tables(cfg: Config, base_token: str) -> list[dict[str, Any]]:
    token = get_tenant_access_token(cfg)
    tables: list[dict[str, Any]] = []
    page_token = ""
    path = f"/bitable/v1/apps/{urllib.parse.quote(base_token)}/tables"
    while True:
        query: dict[str, Any] = {"page_size": 100}
        if page_token:
            query["page_token"] = page_token
        result = request_json(cfg, "GET", path, token=token, query=query)
        data = result.get("data", {})
        tables.extend(data.get("items") or [])
        if not data.get("has_more"):
            return tables
        page_token = str(data.get("page_token") or data.get("next_page_token") or "")
        if not page_token:
            return tables


def resolve_bitable_table_id(cfg: Config, base_token: str, table_id_or_name: str) -> str:
    value = str(table_id_or_name or "").strip()
    if not value:
        raise StructuredError("table_id_missing", "Table ID is not configured.", 500)
    if value.startswith("tbl"):
        return value
    for table in list_bitable_tables(cfg, base_token):
        if table.get("name") == value:
            table_id = str(table.get("table_id") or "")
            if table_id:
                return table_id
    raise StructuredError("table_not_visible", f"Table is not visible to the service identity: {value}", 500)


def fields_by_name(fields: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(field.get("field_name") or ""): field for field in fields if field.get("field_name")}


def get_bitable_record_from(cfg: Config, base_token: str, table_id: str, record_id: str) -> dict[str, Any]:
    token = get_tenant_access_token(cfg)
    path = (
        f"/bitable/v1/apps/{urllib.parse.quote(base_token)}"
        f"/tables/{urllib.parse.quote(table_id)}"
        f"/records/{urllib.parse.quote(record_id)}"
    )
    result = request_json(cfg, "GET", path, token=token, query={"user_id_type": cfg.user_id_type})
    return result.get("data", {}).get("record", {}) or {}


def get_bitable_record(cfg: Config, record_id: str) -> dict[str, Any]:
    return get_bitable_record_from(cfg, cfg.source_base_token, cfg.source_table_id, record_id)


def list_bitable_records(
    cfg: Config,
    base_token: str,
    table_id: str,
    *,
    page_size: int = 100,
) -> list[dict[str, Any]]:
    token = get_tenant_access_token(cfg)
    records: list[dict[str, Any]] = []
    page_token = ""
    path = f"/bitable/v1/apps/{urllib.parse.quote(base_token)}/tables/{urllib.parse.quote(table_id)}/records"
    while True:
        query: dict[str, Any] = {"user_id_type": cfg.user_id_type, "page_size": page_size}
        if page_token:
            query["page_token"] = page_token
        result = request_json(cfg, "GET", path, token=token, query=query)
        data = result.get("data", {})
        records.extend(data.get("items") or [])
        if not data.get("has_more"):
            return records
        page_token = str(data.get("page_token") or data.get("next_page_token") or "")
        if not page_token:
            return records


def update_bitable_record_in(
    cfg: Config,
    base_token: str,
    table_id: str,
    record_id: str,
    record_fields: dict[str, Any],
) -> dict[str, Any]:
    token = get_tenant_access_token(cfg)
    path = (
        f"/bitable/v1/apps/{urllib.parse.quote(base_token)}"
        f"/tables/{urllib.parse.quote(table_id)}"
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


def update_bitable_record(cfg: Config, record_id: str, record_fields: dict[str, Any]) -> dict[str, Any]:
    return update_bitable_record_in(cfg, cfg.source_base_token, cfg.source_table_id, record_id, record_fields)


def create_bitable_record_in(
    cfg: Config,
    base_token: str,
    table_id: str,
    record_fields: dict[str, Any],
    *,
    client_token: str = "",
) -> dict[str, Any]:
    token = get_tenant_access_token(cfg)
    path = f"/bitable/v1/apps/{urllib.parse.quote(base_token)}/tables/{urllib.parse.quote(table_id)}/records"
    query = {"user_id_type": cfg.user_id_type}
    if client_token:
        query["client_token"] = client_token
    return request_json(
        cfg,
        "POST",
        path,
        token=token,
        query=query,
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
        FIELD_SOURCE_ARCHIVE_LINK: {TYPE_URL, TYPE_TEXT},
        FIELD_VERSION_STATUS: {TYPE_SINGLE_SELECT, TYPE_TEXT},
        FIELD_APPROVED_SHA256: {TYPE_TEXT},
        FIELD_MEETING_UID: {TYPE_TEXT},
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


def structured_md_field_issues(fields: list[dict[str, Any]]) -> list[str]:
    by_name = fields_by_name(fields)
    issues: list[str] = []
    expected: dict[str, set[int]] = {
        FIELD_STRUCTURED_TABLE_NAME: {TYPE_TEXT},
        FIELD_STRUCTURED_MD_LINK: {TYPE_TEXT, TYPE_URL},
        FIELD_STRUCTURED_VIEWPOINT_COUNT: {TYPE_NUMBER},
        FIELD_STRUCTURED_APPROVED: {TYPE_CHECKBOX},
        FIELD_ARCHIVE_STATUS: {TYPE_SINGLE_SELECT, TYPE_TEXT},
        FIELD_STRUCTURED_ARCHIVE_LINK: {TYPE_TEXT, TYPE_URL},
        FIELD_VERSION_STATUS: {TYPE_SINGLE_SELECT, TYPE_TEXT},
        FIELD_APPROVED_SHA256: {TYPE_TEXT},
        FIELD_STRUCTURED_CURRENT_MD_HASH: {TYPE_TEXT},
        FIELD_STRUCTURED_JSON_STATUS: {TYPE_TEXT, TYPE_SINGLE_SELECT},
        FIELD_STRUCTURED_JSON_LINK: {TYPE_TEXT, TYPE_URL},
        FIELD_STRUCTURED_JSON_ROW_COUNT: {TYPE_NUMBER},
        FIELD_STRUCTURED_JSON_GENERATED_AT: {TYPE_DATE},
        FIELD_STRUCTURED_JSON_SOURCE_MD_HASH: {TYPE_TEXT},
        FIELD_STRUCTURED_NEEDS_JSON_REGEN: {TYPE_CHECKBOX},
        FIELD_STRUCTURED_ERROR: {TYPE_TEXT},
        FIELD_MEETING_UID: {TYPE_TEXT},
        FIELD_MEETING_DATE: {TYPE_DATE},
        FIELD_MEETING_SERIES: {TYPE_TEXT, TYPE_SINGLE_SELECT},
        FIELD_MEETING_TYPE: {TYPE_TEXT, TYPE_SINGLE_SELECT},
        FIELD_DOCUMENT_SOURCE: {TYPE_TEXT, TYPE_SINGLE_SELECT},
        FIELD_SOURCE_RECORD: {TYPE_TEXT, TYPE_LINK, TYPE_DUPLEX_LINK},
        FIELD_SOURCE_LINK: {TYPE_TEXT, TYPE_URL},
        FIELD_GENERATED_AT: {TYPE_DATE},
        FIELD_BASELINE_VERSION: {TYPE_TEXT},
        FIELD_BASELINE_SHA256: {TYPE_TEXT},
        FIELD_APPROVED_VERSION: {TYPE_TEXT},
        FIELD_VERSION_DIFF: {TYPE_TEXT, TYPE_SINGLE_SELECT},
        FIELD_VERSION_ERROR: {TYPE_TEXT},
        FIELD_ARCHIVE_TIME: {TYPE_DATE},
    }
    for name, expected_types in expected.items():
        field = by_name.get(name)
        if not field:
            issues.append(f"missing field: {name}")
        elif not validate_field_type(field, expected_types):
            issues.append(f"field type mismatch: {name}")
    baseline_fields = [
        by_name.get(FIELD_BASELINE_LINK),
        by_name.get(FIELD_STRUCTURED_BASELINE_LINK),
    ]
    if not any(field and validate_field_type(field, {TYPE_TEXT, TYPE_URL}) for field in baseline_fields):
        issues.append(
            f"missing field: {FIELD_BASELINE_LINK}/{FIELD_STRUCTURED_BASELINE_LINK}"
        )
    return issues


def official_json_field_issues(fields: list[dict[str, Any]]) -> list[str]:
    by_name = fields_by_name(fields)
    issues: list[str] = []
    expected: dict[str, set[int]] = {
        FIELD_OFFICIAL_JSON_FILE: {TYPE_TEXT},
        FIELD_OFFICIAL_SOURCE_MD_RECORD: {TYPE_TEXT, TYPE_LINK, TYPE_DUPLEX_LINK},
        FIELD_OFFICIAL_SOURCE_MD_LINK: {TYPE_TEXT, TYPE_URL},
        FIELD_OFFICIAL_SOURCE_MD_HASH: {TYPE_TEXT},
        FIELD_OFFICIAL_JSON_LINK: {TYPE_TEXT, TYPE_URL},
        FIELD_OFFICIAL_JSON_ROW_COUNT: {TYPE_NUMBER},
        FIELD_OFFICIAL_STATUS: {TYPE_SINGLE_SELECT, TYPE_TEXT},
        FIELD_OFFICIAL_GENERATED_AT: {TYPE_DATE},
        FIELD_OFFICIAL_SOURCE_BASE_STATUS: {TYPE_TEXT, TYPE_SINGLE_SELECT},
        FIELD_OFFICIAL_ERROR: {TYPE_TEXT},
        FIELD_MEETING_UID: {TYPE_TEXT},
    }
    for name, expected_types in expected.items():
        field = by_name.get(name)
        if not field:
            issues.append(f"missing field: {name}")
        elif not validate_field_type(field, expected_types):
            issues.append(f"field type mismatch: {name}")
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


def meeting_uid_value(value: Any) -> str:
    meeting_uid = plain_field_value(value).strip().lower()
    if not MEETING_UID_PATTERN.fullmatch(meeting_uid):
        raise StructuredError("meeting_uid_missing", "会议UID缺失或格式无效。", 409)
    return meeting_uid


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


def number_field_value(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    text = plain_field_value(value).strip()
    if text.isdigit():
        return int(text)
    return None


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


def ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def durable_replace(source: Path, target: Path) -> None:
    os.replace(source, target)
    fsync_directory(target.parent)
    if source.parent != target.parent:
        fsync_directory(source.parent)


def atomic_private_bytes(path: Path, content: bytes) -> None:
    ensure_private_directory(path.parent)
    if path.is_symlink():
        raise StructuredError("unsafe_output_path", "Refusing to replace a symbolic link.", 500)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        durable_replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def atomic_private_text(path: Path, content: str) -> None:
    atomic_private_bytes(path, content.encode("utf-8"))


def save_folder_registry(path: Path, data: dict[str, Any]) -> None:
    atomic_private_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


@contextmanager
def exclusive_file_lock(path: Path):
    ensure_private_directory(path.parent)
    with path.open("a+") as handle:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _ensure_month_folders_unlocked(cfg: Config, month: str) -> dict[str, Any]:
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


def ensure_month_folders(cfg: Config, month: str) -> dict[str, Any]:
    lock_path = Path(cfg.folder_registry_path).with_suffix(Path(cfg.folder_registry_path).suffix + ".lock")
    with exclusive_file_lock(lock_path):
        return _ensure_month_folders_unlocked(cfg, month)


def _ensure_official_json_month_folder_unlocked(cfg: Config, month: str) -> str:
    if not cfg.structured_official_json_folder_token:
        raise StructuredError("official_json_folder_not_configured", "FEISHU_STRUCTURED_OFFICIAL_JSON_FOLDER_TOKEN is not configured.", 500)
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        raise StructuredError("invalid_month", "Month must use YYYY-MM.")
    registry = load_folder_registry(cfg.folder_registry_path)
    entry = registry.setdefault("months", {}).setdefault(month, {})
    if not isinstance(entry, dict):
        raise StructuredError("invalid_registry", f"Folder registry entry for {month} is invalid.")
    official_token = str(entry.get("official_json_folder_token") or "")
    if not official_token:
        official_token = ensure_child_folder(cfg, cfg.structured_official_json_folder_token, month)
    entry.update(
        {
            "official_json_parent_folder_token": cfg.structured_official_json_folder_token,
            "official_json_folder_token": official_token,
            "updated_at": int(time.time() * 1000),
        }
    )
    save_folder_registry(cfg.folder_registry_path, registry)
    return official_token


def ensure_official_json_month_folder(cfg: Config, month: str) -> str:
    lock_path = Path(cfg.folder_registry_path).with_suffix(Path(cfg.folder_registry_path).suffix + ".lock")
    with exclusive_file_lock(lock_path):
        return _ensure_official_json_month_folder_unlocked(cfg, month)


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


def encode_multipart_upload(
    file_name: str,
    parent_node: str,
    data: bytes,
    *,
    content_type: str,
    file_token: str = "",
) -> tuple[str, bytes]:
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
    if file_token:
        add_field("file_token", file_token)
    safe_name = file_name.replace('"', "")
    parts.append(f"--{boundary}\r\n".encode("utf-8"))
    parts.append(
        (
            f'Content-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("utf-8")
    )
    parts.append(data)
    parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return f"multipart/form-data; boundary={boundary}", b"".join(parts)


def upload_drive_file(
    cfg: Config,
    folder_token: str,
    file_name: str,
    data: bytes,
    *,
    content_type: str = "application/octet-stream",
    file_token: str = "",
) -> str:
    is_new_file = not file_token
    token = get_tenant_access_token(cfg)
    multipart_content_type, body = encode_multipart_upload(
        file_name,
        folder_token,
        data,
        content_type=content_type,
        file_token=file_token,
    )
    url = f"{cfg.openapi_base}/drive/v1/files/upload_all"
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": multipart_content_type},
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
    uploaded_token = str(file_token)
    if is_new_file:
        transfer_output_owner(cfg, uploaded_token, token=token)
    return uploaded_token


def transfer_output_owner(cfg: Config, file_token: str, *, token: str = "") -> None:
    owner_open_id = cfg.output_owner_open_id.strip()
    if not owner_open_id:
        return
    access_token = token or get_tenant_access_token(cfg)
    path = f"/drive/v1/permissions/{urllib.parse.quote(file_token, safe='')}/members/transfer_owner"
    try:
        request_json(
            cfg,
            "POST",
            path,
            token=access_token,
            query={
                "type": "file",
                "need_notification": False,
                "remove_old_owner": False,
                "old_owner_perm": "full_access",
                "stay_put": True,
            },
            body={"member_id": owner_open_id, "member_type": "openid"},
        )
    except FeishuApiError as exc:
        raise StructuredError(
            "owner_transfer_failed",
            "File was uploaded, but ownership could not be transferred.",
            502,
        ) from exc


def upload_markdown_file(cfg: Config, folder_token: str, file_name: str, data: bytes) -> str:
    return upload_drive_file(cfg, folder_token, file_name, data, content_type="text/markdown")


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


def url_cell_value(fields_by_name_value: dict[str, dict[str, Any]], field_name: str, url: str, text: str = "") -> Any:
    field = fields_by_name_value.get(field_name, {})
    if validate_field_type(field, {TYPE_URL}):
        return {"text": text or url, "link": url}
    return url


def record_link_cell_value(fields_by_name_value: dict[str, dict[str, Any]], field_name: str, record_id: str) -> Any:
    field = fields_by_name_value.get(field_name, {})
    if validate_field_type(field, {TYPE_LINK, TYPE_DUPLEX_LINK}):
        return [record_id]
    return record_id


def structured_baseline_link_field(field_map: dict[str, dict[str, Any]]) -> str:
    if FIELD_STRUCTURED_BASELINE_LINK in field_map:
        return FIELD_STRUCTURED_BASELINE_LINK
    return FIELD_BASELINE_LINK


def structured_baseline_http_token(cfg: Config) -> str:
    if cfg.structured_baseline_http_token:
        return cfg.structured_baseline_http_token
    env_file = cfg.structured_baseline_http_env_file
    if env_file and env_file.is_file():
        return str(parse_dotenv_file(env_file).get("FEISHU_ARCHIVE_HTTP_TOKEN", "")).strip()
    return ""


def capture_structured_baseline(cfg: Config, record_id: str) -> dict[str, Any]:
    url = cfg.structured_baseline_http_url.strip()
    token = structured_baseline_http_token(cfg)
    if not url or not token:
        raise StructuredError(
            "structured_baseline_config_missing",
            "Structured review baseline service URL or token is not configured.",
            500,
        )
    request = urllib.request.Request(
        url,
        data=json.dumps({"record_id": record_id}).encode("utf-8"),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "X-Archive-Token": token,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, UnicodeError, json.JSONDecodeError) as exc:
        raise StructuredError(
            "structured_baseline_failed",
            f"Structured review baseline capture failed: {exc}",
            502,
        ) from exc
    if not isinstance(payload, dict) or payload.get("status") not in {
        "baseline_captured",
        "baseline_exists",
    }:
        raise StructuredError(
            "structured_baseline_failed",
            f"Structured review baseline service returned an invalid result: {str(payload)[:300]}",
            502,
        )
    return payload


def find_structured_review_record(
    cfg: Config,
    *,
    source_record_id: str,
    meeting_uid: str,
) -> dict[str, Any] | None:
    matches: list[dict[str, Any]] = []
    for record in list_bitable_records(cfg, cfg.structured_base_token, cfg.structured_table_id):
        fields = record.get("fields", {})
        if not isinstance(fields, dict):
            continue
        source_matches = source_record_link_matches(fields.get(FIELD_SOURCE_RECORD), source_record_id)
        uid_matches = plain_field_value(fields.get(FIELD_MEETING_UID)) == meeting_uid
        if source_matches or uid_matches:
            if not source_matches or not uid_matches:
                raise StructuredError(
                    "structured_record_identity_conflict",
                    "Existing structured record has conflicting source record or meeting UID.",
                    409,
                )
            matches.append(record)
    if len(matches) > 1:
        raise StructuredError(
            "structured_record_ambiguous",
            "Multiple structured records match the same source record and meeting UID.",
            409,
        )
    return matches[0] if matches else None


def meeting_date_ms(meeting_date: str) -> int:
    try:
        value = datetime.strptime(meeting_date, "%Y-%m-%d").replace(
            tzinfo=timezone(timedelta(hours=8))
        )
    except ValueError as exc:
        raise StructuredError("meeting_date_invalid", "Meeting date must use YYYY-MM-DD.", 409) from exc
    return int(value.timestamp() * 1000)


def deterministic_uuid4(seed: str) -> str:
    digest = hashlib.sha256(seed.encode("utf-8")).digest()[:16]
    return str(uuid.UUID(bytes=digest, version=4))


def write_structured_review_record(
    cfg: Config,
    *,
    source_record_id: str,
    source_fields: dict[str, Any],
    meeting_uid: str,
    source_archive_url: str,
    source_file_name: str,
    meeting_date: str,
    meeting_series: str,
    file_name: str,
    file_url: str,
    row_count: int,
) -> dict[str, Any]:
    if not meeting_uid_value(meeting_uid):
        raise StructuredError("meeting_uid_invalid", "Meeting UID is invalid.", 409)
    if row_count <= 0:
        raise StructuredError("viewpoint_count_invalid", "Viewpoint count must be positive.", 409)
    structured_fields = list_bitable_fields(cfg, cfg.structured_base_token, cfg.structured_table_id)
    issues = structured_md_field_issues(structured_fields)
    if issues:
        raise StructuredError("field_config_error", "; ".join(issues), 500)
    field_map = fields_by_name(structured_fields)
    baseline_link_field = structured_baseline_link_field(field_map)
    now_ms = int(time.time() * 1000)
    payload: dict[str, Any] = {
        FIELD_STRUCTURED_TABLE_NAME: Path(file_name).stem,
        FIELD_STRUCTURED_MD_LINK: url_cell_value(
            field_map, FIELD_STRUCTURED_MD_LINK, file_url, file_name
        ),
        FIELD_STRUCTURED_VIEWPOINT_COUNT: row_count,
        FIELD_STRUCTURED_APPROVED: False,
        FIELD_ARCHIVE_STATUS: "待归档",
        FIELD_STRUCTURED_ARCHIVE_LINK: None,
        FIELD_ARCHIVE_TIME: None,
        FIELD_MEETING_UID: meeting_uid,
        FIELD_MEETING_DATE: meeting_date_ms(meeting_date),
        FIELD_MEETING_SERIES: meeting_series,
        FIELD_MEETING_TYPE: plain_field_value(source_fields.get(FIELD_MEETING_TYPE)) or "多人复盘会",
        FIELD_DOCUMENT_SOURCE: plain_field_value(source_fields.get(FIELD_DOCUMENT_SOURCE)) or "会议纪要",
        FIELD_SOURCE_RECORD: record_link_cell_value(
            field_map, FIELD_SOURCE_RECORD, source_record_id
        ),
        FIELD_SOURCE_LINK: url_cell_value(
            field_map, FIELD_SOURCE_LINK, source_archive_url, source_file_name
        ),
        FIELD_GENERATED_AT: now_ms,
        FIELD_STRUCTURED_CURRENT_MD_HASH: "",
        FIELD_STRUCTURED_JSON_STATUS: "待审核（schema v9）",
        FIELD_STRUCTURED_JSON_LINK: None,
        FIELD_STRUCTURED_JSON_ROW_COUNT: None,
        FIELD_STRUCTURED_JSON_GENERATED_AT: None,
        FIELD_STRUCTURED_JSON_SOURCE_MD_HASH: "",
        FIELD_STRUCTURED_NEEDS_JSON_REGEN: True,
        FIELD_STRUCTURED_ERROR: "",
        baseline_link_field: None,
        FIELD_BASELINE_VERSION: "",
        FIELD_BASELINE_SHA256: "",
        FIELD_APPROVED_VERSION: "",
        FIELD_APPROVED_SHA256: "",
        FIELD_VERSION_DIFF: VERSION_DIFF_PENDING,
        FIELD_VERSION_STATUS: VERSION_STATUS_PENDING,
        FIELD_VERSION_ERROR: "",
    }
    existing = find_structured_review_record(
        cfg,
        source_record_id=source_record_id,
        meeting_uid=meeting_uid,
    )
    if existing:
        existing_fields = existing.get("fields", {})
        if not isinstance(existing_fields, dict):
            raise StructuredError("structured_record_invalid", "Structured record has no fields.", 500)
        if (
            checkbox_is_checked(existing_fields.get(FIELD_STRUCTURED_APPROVED))
            or plain_field_value(existing_fields.get(FIELD_ARCHIVE_STATUS)) not in {"", "待归档"}
            or bool(url_from_field_value(existing_fields.get(FIELD_STRUCTURED_JSON_LINK)))
        ):
            raise StructuredError(
                "structured_record_locked",
                "Reviewed, archiving, archived, or published structured record cannot be overwritten.",
                409,
            )
        record_id = str(existing.get("record_id") or "")
        if not record_id:
            raise StructuredError("structured_record_invalid", "Structured record ID is missing.", 500)
        same_artifact = (
            url_from_field_value(existing_fields.get(FIELD_STRUCTURED_MD_LINK)) == file_url
            and number_field_value(existing_fields.get(FIELD_STRUCTURED_VIEWPOINT_COUNT)) == row_count
            and plain_field_value(existing_fields.get(FIELD_STRUCTURED_TABLE_NAME)) == Path(file_name).stem
        )
        if not same_artifact:
            update_bitable_record_in(
                cfg,
                cfg.structured_base_token,
                cfg.structured_table_id,
                record_id,
                payload,
            )
    else:
        client_token = deterministic_uuid4(
            f"structured-review:{cfg.structured_base_token}:{cfg.structured_table_id}:{source_record_id}:{meeting_uid}"
        )
        result = create_bitable_record_in(
            cfg,
            cfg.structured_base_token,
            cfg.structured_table_id,
            payload,
            client_token=client_token,
        )
        record = result.get("data", {}).get("record", {})
        record_ids = record.get("record_id_list") or []
        record_id = str(record.get("record_id") or (record_ids[0] if record_ids else ""))
        if not record_id:
            raise StructuredError(
                "structured_record_create_failed",
                "Structured record create response has no record ID.",
                502,
            )
    baseline = capture_structured_baseline(cfg, record_id)
    return {
        "record_id": record_id,
        "created": existing is None,
        "updated": existing is not None and not same_artifact,
        "baseline": baseline,
    }


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


def stable_error_code(exc: BaseException) -> str:
    value = str(getattr(exc, "error_code", "internal_error"))
    return value if re.fullmatch(r"[a-z0-9_]{1,80}", value) else "internal_error"


def now_shanghai_iso() -> str:
    return datetime.now(timezone(timedelta(hours=8))).replace(microsecond=0).isoformat()


def clean_file_name_part(value: str) -> str:
    text = str(value or "").replace("\x00", "").replace("/", " ").replace("\\", " ")
    return re.sub(r"\s+", " ", text).strip()


def resolve_meeting_series(fields: dict[str, Any], markdown: str) -> str:
    source_series = clean_file_name_part(plain_field_value(fields.get(FIELD_MEETING_SERIES)))
    if source_series:
        return source_series
    markdown_series = clean_file_name_part(markdown_field(markdown, "会议系列"))
    if markdown_series:
        return markdown_series
    raise StructuredError("meeting_series_missing", "无法确定会议系列。", 500)


def output_file_name_from_fields(meeting_date: str, meeting_series: str) -> str:
    date_part = clean_file_name_part(meeting_date)
    series_part = clean_file_name_part(meeting_series)
    if not date_part or not series_part:
        raise StructuredError("structured_file_name_missing", "无法生成结构化表格文件名。", 500)
    return f"{date_part} - {series_part} - 标的观点.md"


def run_skill(
    cfg: Config,
    *,
    source_markdown_path: Path,
    claim_units_path: Path,
    output_path: Path,
    meeting_date: str,
    meeting_uid: str,
) -> int:
    require_pinned_skill_runtime(cfg)
    require_pinned_file(cfg.skill_script, cfg.skill_script_sha256, "Structured skill script")
    cmd = [
        sys.executable,
        str(cfg.skill_script),
        "--claim-units",
        str(claim_units_path),
        "--meeting-markdown",
        str(source_markdown_path),
        "--output",
        str(output_path),
        "--meeting-id",
        meeting_uid,
    ]
    if meeting_date:
        cmd.extend(["--meeting-date", meeting_date])
    result = subprocess.run(cmd, text=True, capture_output=True, timeout=120, check=False)
    if result.returncode != 0:
        stderr = truncate_error(result.stderr or result.stdout, cfg.max_error_chars)
        raise StructuredError("skill_failed", f"Skill failed: {stderr}", 500)
    if not output_path.exists():
        raise StructuredError("skill_missing_output", "Skill did not write draft Markdown output.", 500)
    if result.stderr.strip():
        logging.warning("structured_skill_warning %s", truncate_error(result.stderr, cfg.max_error_chars))
    return len(re.findall(r"^## 观点 \d+\s*$", output_path.read_text(encoding="utf-8"), re.MULTILINE))


def save_local_backup(cfg: Config, month: str, file_name: str, content: bytes) -> Path:
    target_dir = cfg.output_dir / month
    ensure_private_directory(cfg.output_dir)
    ensure_private_directory(target_dir)
    target_path = target_dir / file_name
    atomic_private_bytes(target_path, content)
    return target_path


def upload_official_json_artifact(
    cfg: Config,
    *,
    official_folder_token: str,
    month: str,
    source_record_id: str,
    source_md_hash: str,
    json_name: str,
    content: bytes,
) -> tuple[str, str, Path]:
    """Serialize same-folder name selection and keep per-source recovery evidence."""
    with record_lock(cfg, "official-folder-upload", official_folder_token):
        backup_key = hashlib.sha256(
            f"{source_record_id}\0{source_md_hash}".encode("utf-8")
        ).hexdigest()[:16]
        backup_dir = cfg.output_dir / "official-json" / month / backup_key
        ensure_private_directory(backup_dir)
        intent_path = backup_dir / "upload-intent.json"
        content_hash = hashlib.sha256(content).hexdigest()
        folder_hash = hashlib.sha256(official_folder_token.encode("utf-8")).hexdigest()

        intent: dict[str, Any] = {}
        if intent_path.exists():
            try:
                parsed = json.loads(intent_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise StructuredError(
                    "official_json_upload_intent_invalid",
                    "Official JSON upload intent is invalid.",
                    503,
                ) from exc
            if not isinstance(parsed, dict):
                raise StructuredError(
                    "official_json_upload_intent_invalid",
                    "Official JSON upload intent is invalid.",
                    503,
                )
            if (
                parsed.get("source_md_hash") == source_md_hash
                and parsed.get("content_sha256") == content_hash
                and parsed.get("folder_hash") == folder_hash
            ):
                intent = parsed

        items = list_drive_folder_items(cfg, official_folder_token)
        existing_names = {str(item.get("name")) for item in items if item.get("name")}

        def reconcile_exact(name: str, current_items: list[dict[str, Any]]) -> str:
            matches = [item for item in current_items if str(item.get("name") or "") == name]
            if len(matches) > 1:
                raise StructuredError(
                    "official_json_upload_ambiguous",
                    "Multiple Drive files match the pending official JSON upload.",
                    409,
                )
            if not matches:
                return ""
            token = str(matches[0].get("token") or matches[0].get("file_token") or "").strip()
            if not token:
                raise StructuredError(
                    "official_json_upload_token_missing",
                    "Matching Drive file has no token.",
                    503,
                )
            if hashlib.sha256(download_drive_file(cfg, token)).hexdigest() != content_hash:
                return ""
            return token

        upload_name = str(intent.get("upload_name") or "")
        if upload_name:
            reconciled_token = reconcile_exact(upload_name, items)
            if reconciled_token:
                transfer_output_owner(cfg, reconciled_token)
                backup_path = save_local_backup(
                    cfg,
                    f"official-json/{month}/{backup_key}",
                    upload_name,
                    content,
                )
                uploaded_url = resolve_uploaded_file_url(
                    cfg, official_folder_token, reconciled_token, upload_name
                )
                return upload_name, uploaded_url, backup_path
            if upload_name in existing_names:
                upload_name = ""
        if not upload_name:
            upload_name = unique_upload_name(json_name, existing_names)

        backup_path = save_local_backup(
            cfg,
            f"official-json/{month}/{backup_key}",
            upload_name,
            content,
        )
        atomic_private_text(
            intent_path,
            json.dumps(
                {
                    "schema_version": 1,
                    "source_md_hash": source_md_hash,
                    "content_sha256": content_hash,
                    "folder_hash": folder_hash,
                    "upload_name": upload_name,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
        )
        try:
            uploaded_token = upload_drive_file(
                cfg,
                official_folder_token,
                upload_name,
                content,
                content_type="application/json",
            )
            uploaded_url = resolve_uploaded_file_url(
                cfg,
                official_folder_token,
                uploaded_token,
                upload_name,
            )
        except Exception as exc:
            try:
                reconciled_items = list_drive_folder_items(cfg, official_folder_token)
                reconciled_token = reconcile_exact(upload_name, reconciled_items)
                if not reconciled_token:
                    raise exc
                transfer_output_owner(cfg, reconciled_token)
                uploaded_url = resolve_uploaded_file_url(
                    cfg, official_folder_token, reconciled_token, upload_name
                )
            except Exception as reconcile_exc:
                if reconcile_exc is exc:
                    raise
                raise StructuredError(
                    "official_json_upload_outcome_uncertain",
                    "Official JSON upload outcome could not be reconciled.",
                    503,
                ) from reconcile_exc
        return upload_name, uploaded_url, backup_path


def delete_drive_file(cfg: Config, file_token: str) -> None:
    token = get_tenant_access_token(cfg)
    request_json(
        cfg,
        "DELETE",
        f"/drive/v1/files/{urllib.parse.quote(file_token, safe='')}",
        token=token,
        query={"type": "file"},
    )


def cleanup_superseded_official_json_files(
    cfg: Config,
    *,
    official_folder_token: str,
    keep_file_token: str,
    superseded_file_tokens: tuple[str, ...] = (),
) -> int:
    """Delete only old JSON tokens resolved from the pipeline's prior authority."""
    if not keep_file_token:
        raise StructuredError(
            "official_json_keep_token_missing",
            "Authoritative official JSON file token is missing.",
            503,
        )
    listed = {
        str(item.get("token") or item.get("file_token") or "").strip()
        for item in list_drive_folder_items(cfg, official_folder_token)
        if str(item.get("type") or "") == "file"
    }
    if keep_file_token not in listed:
        raise StructuredError(
            "official_json_cleanup_incomplete",
            "The authoritative official JSON file could not be confirmed.",
            503,
        )
    candidates = {
        str(token).strip()
        for token in superseded_file_tokens
        if str(token).strip() and str(token).strip() != keep_file_token
    }
    for file_token in sorted(candidates & listed):
        delete_drive_file(cfg, file_token)
    remaining = {
        str(item.get("token") or item.get("file_token") or "").strip()
        for item in list_drive_folder_items(cfg, official_folder_token)
        if str(item.get("type") or "") == "file"
    }
    if keep_file_token not in remaining or candidates & remaining:
        raise StructuredError(
            "official_json_cleanup_incomplete",
            "Official JSON cleanup did not leave exactly one authoritative file.",
            503,
        )
    return len(candidates & listed)


def append_index(cfg: Config, item: dict[str, Any]) -> None:
    index_path = cfg.output_dir / "index.jsonl"
    ensure_private_directory(index_path.parent)
    line = (json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    with exclusive_file_lock(index_path.with_name(index_path.name + ".lock")):
        fd = os.open(index_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.fchmod(fd, 0o600)
            remaining = memoryview(line)
            while remaining:
                written = os.write(fd, remaining)
                if written <= 0:
                    raise OSError("index append made no progress")
                remaining = remaining[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        fsync_directory(index_path.parent)


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


JOB_STATES = ("pending", "processing", "done", "failed")
JOB_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")


def semantic_job_root(cfg: Config) -> Path:
    configured = getattr(cfg, "semantic_job_dir", None)
    if configured:
        return Path(configured)
    return Path(cfg.output_dir) / ".semantic-jobs"


@contextmanager
def record_lock(cfg: Config, scope: str, record_id: str):
    digest = hashlib.sha256(f"{scope}\0{record_id}".encode("utf-8")).hexdigest()
    lock_path = semantic_job_root(cfg) / "locks" / f"{scope}-{digest}.lock"
    with exclusive_file_lock(lock_path):
        yield


def ensure_semantic_job_dirs(cfg: Config) -> Path:
    root = semantic_job_root(cfg)
    ensure_private_directory(root)
    for state in JOB_STATES:
        ensure_private_directory(root / state)
    return root


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    atomic_private_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise StructuredError("semantic_job_invalid", f"Missing {label}: {path.name}", 500) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise StructuredError("semantic_job_invalid", f"Invalid {label}: {path.name}", 500) from exc
    if not isinstance(value, dict):
        raise StructuredError("semantic_job_invalid", f"{label} must be a JSON object.", 500)
    return value


def job_context_if_matches(path: Path, record_id: str, approved_sha256: str) -> dict[str, Any] | None:
    try:
        context = read_json_object(path / "context.json", "job context")
    except StructuredError:
        return None
    if context.get("record_id") == record_id and context.get("approved_sha256") == approved_sha256:
        return context
    return None


def active_generation_job(cfg: Config, record_id: str, approved_sha256: str) -> tuple[Path, dict[str, Any]] | None:
    root = semantic_job_root(cfg)
    for state in ("pending", "processing"):
        state_dir = root / state
        if not state_dir.exists():
            continue
        for path in sorted(state_dir.iterdir()):
            if not path.is_dir():
                continue
            context = job_context_if_matches(path, record_id, approved_sha256)
            if context is not None:
                return path, context
    return None


def next_generation_attempt(cfg: Config, record_id: str, approved_sha256: str) -> int:
    root = semantic_job_root(cfg)
    attempts: list[int] = []
    for state in JOB_STATES:
        state_dir = root / state
        if not state_dir.exists():
            continue
        for path in state_dir.iterdir():
            if not path.is_dir():
                continue
            context = job_context_if_matches(path, record_id, approved_sha256)
            if context is not None:
                try:
                    attempts.append(int(context.get("attempt") or 0))
                except (TypeError, ValueError):
                    continue
    return max(attempts, default=0) + 1


def create_generation_job(
    cfg: Config,
    record_id: str,
    fields: dict[str, Any],
    archive_url: str,
) -> dict[str, Any]:
    file_token, file_type = parse_drive_url(archive_url)
    if file_type != "file":
        raise StructuredError("unsupported_archive_type", "仅支持已归档 Markdown 文件。", 500)
    meta = get_file_meta(cfg, file_token, file_type)
    source_file_name = str(
        meta.get("name")
        or meta.get("title")
        or plain_field_value(fields.get(FIELD_FILE_NAME))
        or file_token
    )
    if not source_file_name.lower().endswith(".md"):
        raise StructuredError("unsupported_archive_type", "仅支持已归档 Markdown 文件。", 500)
    markdown_bytes = download_drive_file(cfg, file_token)
    actual_hash = hashlib.sha256(markdown_bytes).hexdigest()
    approved_hash = plain_field_value(fields.get(FIELD_APPROVED_SHA256)).lower()
    if not re.fullmatch(r"[0-9a-f]{64}", approved_hash) or actual_hash != approved_hash:
        raise StructuredError("approved_archive_hash_mismatch", "归档文件与审核后内容哈希不一致。", 409)
    try:
        markdown_text = markdown_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StructuredError("archive_decode_failed", "归档 Markdown 不是 UTF-8 文本。", 500) from exc
    meeting_date = resolve_meeting_date(fields, markdown_text, source_file_name)
    meeting_series = resolve_meeting_series(fields, markdown_text)
    meeting_uid = meeting_uid_value(fields.get(FIELD_MEETING_UID))
    month = month_from_date_text(meeting_date)
    output_name = output_file_name_from_fields(meeting_date, meeting_series)

    existing = active_generation_job(cfg, record_id, approved_hash)
    if existing is not None:
        path, context = existing
        return {
            "ok": True,
            "status": "queued_existing",
            "record_id": record_id,
            "job_id": context.get("job_id") or path.name,
        }

    root = ensure_semantic_job_dirs(cfg)
    attempt = next_generation_attempt(cfg, record_id, approved_hash)
    safe_record = re.sub(r"[^A-Za-z0-9_.-]+", "-", record_id).strip("-") or "record"
    job_id = f"{safe_record}-{approved_hash[:12]}-a{attempt:02d}"
    temporary = root / f".creating-{job_id}-{uuid.uuid4().hex}"
    target = root / "pending" / job_id
    temporary.mkdir(parents=True, exist_ok=False, mode=0o700)
    os.chmod(temporary, 0o700)
    try:
        atomic_private_bytes(temporary / "source.md", markdown_bytes)
        context = {
            "job_id": job_id,
            "attempt": attempt,
            "record_id": record_id,
            "source_archive_url": archive_url,
            "approved_sha256": approved_hash,
            "source_file_name": source_file_name,
            "meeting_date": meeting_date,
            "meeting_series": meeting_series,
            "meeting_uid": meeting_uid,
            "month": month,
            "output_name": output_name,
            "queued_at": now_shanghai_iso(),
        }
        write_json_atomic(temporary / "context.json", context)
        durable_replace(temporary, target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return {"ok": True, "status": "queued", "record_id": record_id, "job_id": job_id}


def locate_generation_job(cfg: Config, job_id: str, states: tuple[str, ...] = ("processing", "pending")) -> Path:
    if not JOB_ID_PATTERN.fullmatch(job_id):
        raise StructuredError("semantic_job_invalid", "Invalid semantic job id.", 400)
    root = semantic_job_root(cfg)
    for state in states:
        path = root / state / job_id
        if path.is_dir():
            return path
    raise StructuredError("semantic_job_not_found", f"Semantic job not found: {job_id}", 404)


def generate_for_record(cfg: Config, record_id: str) -> tuple[int, dict[str, Any]]:
    with record_lock(cfg, "source", record_id):
        return _generate_for_record_unlocked(cfg, record_id)


def _generate_for_record_unlocked(cfg: Config, record_id: str) -> tuple[int, dict[str, Any]]:
    if not cfg.source_version_retention_enforce:
        return 500, {
            "ok": False,
            "status": "config_error",
            "error_code": "version_retention_required",
        }
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
        approved_hash = plain_field_value(fields.get(FIELD_APPROVED_SHA256))
        existing_job = active_generation_job(cfg, record_id, approved_hash) if approved_hash else None
        if existing_job is not None:
            path, context = existing_job
            return 202, {
                "ok": True,
                "status": "queued_existing",
                "record_id": record_id,
                "job_id": context.get("job_id") or path.name,
            }
        logging.warning("Recovering stale running status without a durable job record_id_hash=%s", hashlib.sha256(record_id.encode()).hexdigest()[:12])
    if not record_review_ok(cfg, fields):
        return 409, {"ok": False, "status": "not_ready", "reason": "review_not_checked", "record_id": record_id}
    if plain_field_value(fields.get(FIELD_ARCHIVE_STATUS)) != "已归档":
        return 409, {"ok": False, "status": "not_ready", "reason": "archive_status_not_done", "record_id": record_id}
    if plain_field_value(fields.get(FIELD_VERSION_STATUS)) != "已完成":
        return 409, {"ok": False, "status": "not_ready", "reason": "version_retention_not_done", "record_id": record_id}
    archive_url = url_from_field_value(fields.get(FIELD_SOURCE_ARCHIVE_LINK))
    if not archive_url:
        return 409, {"ok": False, "status": "not_ready", "reason": "archive_link_missing", "record_id": record_id}

    try:
        result = create_generation_job(cfg, record_id, fields, archive_url)
        try:
            update_source_status(cfg, record_id, STATUS_RUNNING, fields=source_fields_list)
        except Exception as exc:
            logging.error(
                "source_status_update_failed record_id_hash=%s code=%s",
                hashlib.sha256(record_id.encode()).hexdigest()[:12],
                getattr(exc, "error_code", "internal_error"),
            )
            return 202, {**result, "warning_code": "source_status_update_failed"}
        return 202, result
    except Exception as exc:
        error_code = str(getattr(exc, "error_code", "generation_queue_failed"))
        try:
            update_source_status(cfg, record_id, STATUS_FAILED, error=error_code, fields=source_fields_list)
        except Exception:
            logging.error("failure_status_write_failed record_id_hash=%s", hashlib.sha256(record_id.encode()).hexdigest()[:12])
        logging.error(
            "generation_queue_failed record_id_hash=%s code=%s",
            hashlib.sha256(record_id.encode()).hexdigest()[:12],
            error_code,
        )
        return 500, {"ok": False, "status": "failed", "error_code": error_code, "message": "Generation could not be queued."}


def process_record_after_lock(
    cfg: Config,
    record_id: str,
    fields: dict[str, Any],
    archive_url: str,
    source_fields_list: list[dict[str, Any]],
    *,
    job_dir: Path,
    context: dict[str, Any],
) -> dict[str, Any]:
    file_token, file_type = parse_drive_url(archive_url)
    if file_type != "file":
        raise StructuredError("unsupported_archive_type", "仅支持已归档 Markdown 文件。", 500)
    meta = get_file_meta(cfg, file_token, file_type)
    source_file_name = str(meta.get("name") or meta.get("title") or plain_field_value(fields.get(FIELD_FILE_NAME)) or file_token)
    if not source_file_name.lower().endswith(".md"):
        raise StructuredError("unsupported_archive_type", "仅支持已归档 Markdown 文件。", 500)
    markdown_bytes = download_drive_file(cfg, file_token)
    approved_hash = plain_field_value(fields.get(FIELD_APPROVED_SHA256)).lower()
    actual_hash = hashlib.sha256(markdown_bytes).hexdigest()
    if not re.fullmatch(r"[0-9a-f]{64}", approved_hash) or actual_hash != approved_hash:
        raise StructuredError("approved_archive_hash_mismatch", "归档文件与审核后内容哈希不一致。", 409)
    if context.get("source_archive_url") != archive_url or context.get("approved_sha256") != approved_hash:
        raise StructuredError("semantic_job_source_changed", "排队后审核归档来源已变化，拒绝继续生成。", 409)
    queued_source = job_dir / "source.md"
    if not queued_source.exists() or hashlib.sha256(queued_source.read_bytes()).hexdigest() != actual_hash:
        raise StructuredError("semantic_job_source_changed", "任务中的审核后源文件与当前归档不一致。", 409)
    try:
        markdown_text = markdown_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StructuredError("archive_decode_failed", "归档 Markdown 不是 UTF-8 文本。", 500) from exc
    meeting_date = resolve_meeting_date(fields, markdown_text, source_file_name)
    meeting_series = resolve_meeting_series(fields, markdown_text)
    meeting_uid = meeting_uid_value(fields.get(FIELD_MEETING_UID))
    if meeting_uid_value(context.get("meeting_uid")) != meeting_uid:
        raise StructuredError("semantic_job_source_changed", "排队后会议UID已变化，拒绝继续生成。", 409)
    month = month_from_date_text(meeting_date)
    output_name = output_file_name_from_fields(meeting_date, meeting_series)
    claim_units_path = job_dir / "claim_units.json"
    output_path = job_dir / "structured.md"
    metadata = read_json_object(job_dir / "model_metadata.json", "model metadata")
    model_version = str(metadata.get("model_version") or "").strip()
    try:
        schema_version = int(metadata.get("schema_version") or 0)
        contract_version = int(metadata.get("contract_version") or 0)
    except (TypeError, ValueError):
        schema_version = 0
        contract_version = 0
    worker_skill_sha256 = str(metadata.get("skill_script_sha256") or "").strip().lower()
    worker_runtime_sha256 = str(metadata.get("skill_runtime_sha256") or "").strip().lower()
    if (
        not model_version
        or schema_version != cfg.skill_contract_version
        or contract_version != 9
        or worker_skill_sha256 != cfg.skill_script_sha256
        or worker_runtime_sha256 != cfg.skill_runtime_sha256
    ):
        raise StructuredError("semantic_metadata_missing", "模型或 Skill 契约版本信息缺失或不一致。", 500)

    row_count = run_skill(
        cfg,
        source_markdown_path=queued_source,
        claim_units_path=claim_units_path,
        output_path=output_path,
        meeting_date=meeting_date,
        meeting_uid=meeting_uid,
    )
    if row_count == 0:
        update_source_status(cfg, record_id, STATUS_NO_ROWS, row_count=0, fields=source_fields_list)
        return {"ok": True, "status": "no_rows", "record_id": record_id, "row_count": 0}

    month_folders = ensure_month_folders(cfg, month)
    target_folder = str(month_folders["source_folder_token"])
    existing_names = {str(item.get("name")) for item in list_drive_folder_items(cfg, target_folder) if item.get("name")}
    upload_name = unique_upload_name(output_name, existing_names)
    content = output_path.read_bytes()
    uploaded_token = upload_markdown_file(cfg, target_folder, upload_name, content)
    uploaded_url = resolve_uploaded_file_url(cfg, target_folder, uploaded_token, upload_name)
    backup_path = save_local_backup(cfg, month, upload_name, content)
    structured_writeback = write_structured_review_record(
        cfg,
        source_record_id=record_id,
        source_fields=fields,
        meeting_uid=meeting_uid,
        source_archive_url=archive_url,
        source_file_name=source_file_name,
        meeting_date=meeting_date,
        meeting_series=meeting_series,
        file_name=upload_name,
        file_url=uploaded_url,
        row_count=row_count,
    )
    append_index(
        cfg,
        {
            "source_record_id": record_id,
            "source_archive_url": archive_url,
            "source_archive_sha256": actual_hash,
            "semantic_job_id": context.get("job_id"),
            "meeting_date": meeting_date,
            "row_count": row_count,
            "local_path": str(backup_path),
            "feishu_url": uploaded_url,
            "generated_at": now_shanghai_iso(),
            "file_name": upload_name,
            "model_version": model_version,
            "schema_version": schema_version,
            "skill_contract_sha256": str(metadata.get("skill_contract_sha256") or ""),
            "structured_record_id": structured_writeback["record_id"],
            "structured_baseline_status": structured_writeback["baseline"].get("status"),
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
        "structured_record_id": structured_writeback["record_id"],
        "structured_baseline_status": structured_writeback["baseline"].get("status"),
    }


def complete_generation_job(cfg: Config, job_id: str) -> dict[str, Any]:
    job_dir = locate_generation_job(cfg, job_id)
    context = read_json_object(job_dir / "context.json", "job context")
    record_id = str(context.get("record_id") or "").strip()
    if not record_id:
        raise StructuredError("semantic_job_invalid", "Semantic job has no record_id.", 500)
    with record_lock(cfg, "source", record_id):
        return _complete_generation_job_unlocked(cfg, job_id)


def _complete_generation_job_unlocked(cfg: Config, job_id: str) -> dict[str, Any]:
    if not cfg.source_version_retention_enforce:
        raise StructuredError("version_retention_required", "Source version retention must be enforced.", 500)
    job_dir = locate_generation_job(cfg, job_id)
    context = read_json_object(job_dir / "context.json", "job context")
    record_id = str(context.get("record_id") or "").strip()
    if not record_id:
        raise StructuredError("semantic_job_invalid", "Semantic job has no record_id.", 500)
    source_fields_list = list_bitable_fields(cfg, cfg.source_base_token, cfg.source_table_id)
    issues = source_field_issues(cfg, source_fields_list)
    if issues:
        raise StructuredError("field_config_error", "; ".join(issues), 500)
    record = get_bitable_record(cfg, record_id)
    fields = record.get("fields", {})
    if not isinstance(fields, dict):
        raise StructuredError("invalid_record", "Record response has no fields.", 500)
    existing_status = plain_field_value(fields.get(FIELD_TABLE_STATUS))
    existing_link = url_from_field_value(fields.get(FIELD_TABLE_LINK))
    if existing_status == STATUS_GENERATED and existing_link:
        result = {"ok": True, "status": "skipped_existing", "record_id": record_id, "job_id": job_id}
        write_json_atomic(job_dir / "result.json", result)
        return result
    if existing_status == STATUS_NO_ROWS:
        result = {"ok": True, "status": "skipped_no_rows", "record_id": record_id, "job_id": job_id, "row_count": 0}
        write_json_atomic(job_dir / "result.json", result)
        return result
    if not record_review_ok(cfg, fields):
        raise StructuredError("review_not_checked", "源会议纪要尚未审核。", 409)
    if plain_field_value(fields.get(FIELD_ARCHIVE_STATUS)) != "已归档":
        raise StructuredError("archive_status_not_done", "源会议纪要尚未归档。", 409)
    if plain_field_value(fields.get(FIELD_VERSION_STATUS)) != "已完成":
        raise StructuredError("version_retention_not_done", "源会议纪要版本留存尚未完成。", 409)
    archive_url = url_from_field_value(fields.get(FIELD_SOURCE_ARCHIVE_LINK))
    if not archive_url:
        raise StructuredError("archive_link_missing", "源会议纪要缺少归档链接。", 409)
    result = process_record_after_lock(
        cfg,
        record_id,
        fields,
        archive_url,
        source_fields_list,
        job_dir=job_dir,
        context=context,
    )
    write_json_atomic(job_dir / "result.json", result)
    return {**result, "job_id": job_id}


def fail_generation_job(cfg: Config, job_id: str) -> dict[str, Any]:
    job_dir = locate_generation_job(cfg, job_id)
    context = read_json_object(job_dir / "context.json", "job context")
    record_id = str(context.get("record_id") or "").strip()
    if not record_id:
        raise StructuredError("semantic_job_invalid", "Semantic job has no record_id.", 500)
    with record_lock(cfg, "source", record_id):
        return _fail_generation_job_unlocked(cfg, job_id)


def _fail_generation_job_unlocked(cfg: Config, job_id: str) -> dict[str, Any]:
    job_dir = locate_generation_job(cfg, job_id)
    context = read_json_object(job_dir / "context.json", "job context")
    record_id = str(context.get("record_id") or "").strip()
    if not record_id:
        raise StructuredError("semantic_job_invalid", "Semantic job has no record_id.", 500)
    source_fields_list = list_bitable_fields(cfg, cfg.source_base_token, cfg.source_table_id)
    record = get_bitable_record(cfg, record_id)
    fields = record.get("fields", {})
    if not isinstance(fields, dict):
        raise StructuredError("invalid_record", "Record response has no fields.", 500)
    current_status = plain_field_value(fields.get(FIELD_TABLE_STATUS))
    current_link = url_from_field_value(fields.get(FIELD_TABLE_LINK))
    if (current_status == STATUS_GENERATED and current_link) or current_status == STATUS_NO_ROWS:
        result = {
            "ok": True,
            "status": "skipped_terminal",
            "record_id": record_id,
            "job_id": job_id,
        }
        write_json_atomic(job_dir / "result.json", result)
        return result
    error = "semantic_worker_failed"
    update_source_status(cfg, record_id, STATUS_FAILED, error=error, fields=source_fields_list)
    result = {"ok": False, "status": "failed", "record_id": record_id, "job_id": job_id, "message": error}
    write_json_atomic(job_dir / "failure.json", result)
    return result


URL_PATTERN = re.compile(r"https://[^\s)\]>]+")


def first_url(value: Any) -> str:
    text = url_from_field_value(value) or plain_field_value(value)
    match = URL_PATTERN.search(text)
    return match.group(0) if match else ""


def source_record_link_matches(value: Any, record_id: str) -> bool:
    if isinstance(value, str):
        return value == record_id
    if isinstance(value, dict):
        return any(source_record_link_matches(value.get(key), record_id) for key in ("id", "record_id", "record_ids", "text", "value"))
    if isinstance(value, list):
        return any(source_record_link_matches(item, record_id) for item in value)
    return False


def run_generate_structured_json(
    cfg: Config,
    *,
    approved_markdown_path: Path,
    output_dir: Path,
    json_name: str,
    meeting_uid: str,
) -> dict[str, Any]:
    require_pinned_skill_runtime(cfg)
    require_pinned_file(cfg.skill_script, cfg.skill_script_sha256, "Structured skill script")
    require_pinned_file(
        cfg.skill_json_script,
        cfg.skill_json_script_sha256,
        "Structured Skill JSON entrypoint",
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / json_name
    cmd = [
        sys.executable,
        str(cfg.skill_json_script),
        "--structured-markdown",
        str(approved_markdown_path),
        "--meeting-id",
        meeting_uid,
        "--output",
        str(json_path),
    ]
    if cfg.security_master_path:
        cmd.extend([cfg.security_master_cli_flag, str(cfg.security_master_path)])
    result = subprocess.run(cmd, text=True, capture_output=True, timeout=120, check=False)
    if result.returncode != 0:
        stderr = truncate_error(result.stderr or result.stdout, cfg.max_error_chars)
        raise StructuredError("structured_json_generate_failed", f"Structured JSON generation failed: {stderr}", 500)
    if result.stderr.strip():
        logging.warning("structured_skill_warning %s", truncate_error(result.stderr, cfg.max_error_chars))
    try:
        envelope = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StructuredError("structured_json_generate_bad_output", "Official JSON output could not be parsed.", 500) from exc
    metadata = envelope.get("metadata") if isinstance(envelope, dict) else None
    rows = envelope.get("rows") if isinstance(envelope, dict) else None
    if not isinstance(metadata, dict) or not isinstance(rows, list) or not json_path.is_file():
        raise StructuredError(
            "structured_json_generate_bad_output",
            "Structured JSON output is missing metadata, rows, or the JSON artifact.",
            500,
        )
    if set(metadata) != {
        "meeting_id",
        "structured_markdown_sha256",
        "schema_version",
        "security_master_version",
    }:
        raise StructuredError(
            "structured_json_generate_bad_output",
            "Official JSON metadata does not match the v9 viewpoints schema.",
            500,
        )
    return {
        "json_path": str(json_path),
        "source_md_hash": str(metadata.get("structured_markdown_sha256") or ""),
        "row_count": len(rows),
        "meeting_uid": str(metadata.get("meeting_id") or ""),
        "schema_version": int(metadata.get("schema_version") or 0),
        "security_master_version": str(metadata.get("security_master_version") or ""),
    }


def official_json_file_name(source_file_name: str, table_name: str) -> str:
    source = clean_file_name_part(source_file_name)
    if source.lower().endswith(".md"):
        return source[:-3] + ".json"
    if source:
        return Path(source).stem + ".json"
    name = clean_file_name_part(table_name)
    if not name:
        raise StructuredError("official_json_file_name_missing", "无法生成正式 JSON 文件名。", 500)
    if name.endswith(" - 标的观点"):
        return f"{name}.json"
    return f"{name} - 标的观点.json"


def find_existing_official_json_record(
    cfg: Config,
    official_table_id: str,
    source_md_record_id: str,
) -> str:
    matches: list[str] = []
    for record in list_bitable_records(cfg, cfg.structured_base_token, official_table_id):
        fields = record.get("fields", {})
        if isinstance(fields, dict) and source_record_link_matches(fields.get(FIELD_OFFICIAL_SOURCE_MD_RECORD), source_md_record_id):
            record_id = str(record.get("record_id") or "").strip()
            if not record_id:
                raise StructuredError(
                    "official_json_record_id_missing",
                    "Matching official JSON record has no record ID.",
                    503,
                )
            matches.append(record_id)
    if len(matches) > 1:
        raise StructuredError(
            "official_json_record_ambiguous",
            "Multiple official JSON records reference the same source Markdown.",
            409,
        )
    return matches[0] if matches else ""


def write_official_json_artifact_record(
    cfg: Config,
    *,
    official_table_id: str,
    fields: list[dict[str, Any]],
    source_md_record_id: str,
    source_md_url: str,
    source_md_hash: str,
    meeting_uid: str,
    json_file_name: str,
    json_url: str,
    row_count: int,
    generated_at_ms: int,
    existing_record_id: str = "",
) -> str:
    field_map = fields_by_name(fields)
    payload = {
        FIELD_MEETING_UID: meeting_uid,
        FIELD_OFFICIAL_JSON_FILE: json_file_name,
        FIELD_OFFICIAL_SOURCE_MD_RECORD: record_link_cell_value(field_map, FIELD_OFFICIAL_SOURCE_MD_RECORD, source_md_record_id),
        FIELD_OFFICIAL_SOURCE_MD_LINK: url_cell_value(field_map, FIELD_OFFICIAL_SOURCE_MD_LINK, source_md_url, source_md_url),
        FIELD_OFFICIAL_SOURCE_MD_HASH: source_md_hash,
        FIELD_OFFICIAL_JSON_LINK: url_cell_value(field_map, FIELD_OFFICIAL_JSON_LINK, json_url, json_file_name),
        FIELD_OFFICIAL_JSON_ROW_COUNT: row_count,
        FIELD_OFFICIAL_STATUS: STATUS_GENERATED,
        FIELD_OFFICIAL_GENERATED_AT: generated_at_ms,
        FIELD_OFFICIAL_SOURCE_BASE_STATUS: "已审核",
        FIELD_OFFICIAL_ERROR: "",
    }
    existing_record_id = existing_record_id or find_existing_official_json_record(
        cfg,
        official_table_id,
        source_md_record_id,
    )
    if existing_record_id:
        update_bitable_record_in(cfg, cfg.structured_base_token, official_table_id, existing_record_id, payload)
        return existing_record_id
    client_token = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"official-json:{cfg.structured_base_token}:{official_table_id}:{source_md_record_id}",
        )
    )
    try:
        result = create_bitable_record_in(
            cfg,
            cfg.structured_base_token,
            official_table_id,
            payload,
            client_token=client_token,
        )
    except FeishuApiError:
        reconciled = find_existing_official_json_record(
            cfg,
            official_table_id,
            source_md_record_id,
        )
        if reconciled:
            return reconciled
        raise
    record = result.get("data", {}).get("record", {})
    record_ids = record.get("record_id_list") or []
    if record_ids and str(record_ids[0] or "").strip():
        return str(record_ids[0]).strip()
    record_id = str(record.get("record_id") or "")
    if not record_id:
        record_id = find_existing_official_json_record(
            cfg,
            official_table_id,
            source_md_record_id,
        )
    if not record_id:
        raise StructuredError(
            "official_json_record_id_missing",
            "Official JSON record creation did not return a recoverable record ID.",
            503,
        )
    return record_id


def restore_official_json_artifact_record(
    cfg: Config,
    *,
    official_table_id: str,
    fields: list[dict[str, Any]],
    record_id: str,
    source_md_record_id: str,
    snapshot_fields: dict[str, Any],
) -> None:
    field_map = fields_by_name(fields)
    source_md_url = first_url(snapshot_fields.get(FIELD_OFFICIAL_SOURCE_MD_LINK))
    json_url = first_url(snapshot_fields.get(FIELD_OFFICIAL_JSON_LINK))
    json_file_name = plain_field_value(snapshot_fields.get(FIELD_OFFICIAL_JSON_FILE))
    payload = {
        FIELD_MEETING_UID: plain_field_value(snapshot_fields.get(FIELD_MEETING_UID)) or None,
        FIELD_OFFICIAL_JSON_FILE: json_file_name or None,
        FIELD_OFFICIAL_SOURCE_MD_RECORD: record_link_cell_value(
            field_map,
            FIELD_OFFICIAL_SOURCE_MD_RECORD,
            source_md_record_id,
        ),
        FIELD_OFFICIAL_SOURCE_MD_LINK: (
            url_cell_value(field_map, FIELD_OFFICIAL_SOURCE_MD_LINK, source_md_url, source_md_url)
            if source_md_url
            else None
        ),
        FIELD_OFFICIAL_SOURCE_MD_HASH: plain_field_value(snapshot_fields.get(FIELD_OFFICIAL_SOURCE_MD_HASH)) or None,
        FIELD_OFFICIAL_JSON_LINK: (
            url_cell_value(field_map, FIELD_OFFICIAL_JSON_LINK, json_url, json_file_name or json_url)
            if json_url
            else None
        ),
        FIELD_OFFICIAL_JSON_ROW_COUNT: number_field_value(snapshot_fields.get(FIELD_OFFICIAL_JSON_ROW_COUNT)),
        FIELD_OFFICIAL_STATUS: plain_field_value(snapshot_fields.get(FIELD_OFFICIAL_STATUS)) or None,
        FIELD_OFFICIAL_GENERATED_AT: ms_from_record_time(snapshot_fields.get(FIELD_OFFICIAL_GENERATED_AT)),
        FIELD_OFFICIAL_SOURCE_BASE_STATUS: plain_field_value(
            snapshot_fields.get(FIELD_OFFICIAL_SOURCE_BASE_STATUS)
        )
        or None,
        FIELD_OFFICIAL_ERROR: plain_field_value(snapshot_fields.get(FIELD_OFFICIAL_ERROR)),
    }
    update_bitable_record_in(cfg, cfg.structured_base_token, official_table_id, record_id, payload)


def mark_official_json_artifact_failed(
    cfg: Config,
    *,
    official_table_id: str,
    record_id: str,
    error: str,
) -> None:
    update_bitable_record_in(
        cfg,
        cfg.structured_base_token,
        official_table_id,
        record_id,
        {
            FIELD_OFFICIAL_STATUS: STATUS_FAILED,
            FIELD_OFFICIAL_ERROR: truncate_error(error, cfg.max_error_chars),
        },
    )


def update_structured_json_status(
    cfg: Config,
    *,
    structured_fields: list[dict[str, Any]],
    record_id: str,
    status: str,
    current_hash: str = "",
    json_url: str = "",
    json_file_name: str = "",
    row_count: int | None = None,
    generated_at_ms: int | None = None,
    source_md_hash: str | None = None,
    needs_regeneration: bool | None = None,
    error: str = "",
) -> None:
    field_map = fields_by_name(structured_fields)
    payload: dict[str, Any] = {
        FIELD_STRUCTURED_JSON_STATUS: status,
        FIELD_STRUCTURED_ERROR: truncate_error(error, cfg.max_error_chars) if error else "",
    }
    if current_hash:
        payload[FIELD_STRUCTURED_CURRENT_MD_HASH] = current_hash
    if json_url:
        payload[FIELD_STRUCTURED_JSON_LINK] = url_cell_value(field_map, FIELD_STRUCTURED_JSON_LINK, json_url, json_file_name or json_url)
    if row_count is not None:
        payload[FIELD_STRUCTURED_JSON_ROW_COUNT] = row_count
    if generated_at_ms is not None:
        payload[FIELD_STRUCTURED_JSON_GENERATED_AT] = generated_at_ms
    if source_md_hash is not None:
        payload[FIELD_STRUCTURED_JSON_SOURCE_MD_HASH] = source_md_hash
    if needs_regeneration is not None:
        payload[FIELD_STRUCTURED_NEEDS_JSON_REGEN] = needs_regeneration
    update_bitable_record_in(cfg, cfg.structured_base_token, cfg.structured_table_id, record_id, payload)


def official_json_terminal_is_complete(
    cfg: Config,
    *,
    official_table_id: str,
    source_md_record_id: str,
    source_md_hash: str,
    json_file_name: str,
    json_url: str,
    row_count: int,
) -> tuple[bool, str]:
    """Re-read both Base records before treating an uncertain commit as failed."""
    source_record = get_bitable_record_from(
        cfg,
        cfg.structured_base_token,
        cfg.structured_table_id,
        source_md_record_id,
    )
    source_fields = source_record.get("fields", {})
    if not isinstance(source_fields, dict):
        raise StructuredError("invalid_record", "Structured record response has no fields.", 503)

    official_record_id = find_existing_official_json_record(
        cfg,
        official_table_id,
        source_md_record_id,
    )
    if not official_record_id:
        return False, ""
    official_record = get_bitable_record_from(
        cfg,
        cfg.structured_base_token,
        official_table_id,
        official_record_id,
    )
    official_fields = official_record.get("fields", {})
    if not isinstance(official_fields, dict):
        raise StructuredError("invalid_record", "Official JSON record response has no fields.", 503)

    source_complete = (
        plain_field_value(source_fields.get(FIELD_STRUCTURED_JSON_STATUS)) == STATUS_GENERATED
        and plain_field_value(source_fields.get(FIELD_STRUCTURED_CURRENT_MD_HASH)) == source_md_hash
        and plain_field_value(source_fields.get(FIELD_STRUCTURED_JSON_SOURCE_MD_HASH)) == source_md_hash
        and first_url(source_fields.get(FIELD_STRUCTURED_JSON_LINK)) == json_url
        and number_field_value(source_fields.get(FIELD_STRUCTURED_JSON_ROW_COUNT)) == row_count
        and FIELD_STRUCTURED_NEEDS_JSON_REGEN in source_fields
        and not checkbox_is_checked(source_fields.get(FIELD_STRUCTURED_NEEDS_JSON_REGEN))
    )
    official_complete = (
        plain_field_value(official_fields.get(FIELD_OFFICIAL_STATUS)) == STATUS_GENERATED
        and plain_field_value(official_fields.get(FIELD_OFFICIAL_SOURCE_MD_HASH)) == source_md_hash
        and plain_field_value(official_fields.get(FIELD_OFFICIAL_JSON_FILE)) == json_file_name
        and first_url(official_fields.get(FIELD_OFFICIAL_JSON_LINK)) == json_url
        and number_field_value(official_fields.get(FIELD_OFFICIAL_JSON_ROW_COUNT)) == row_count
    )
    return source_complete and official_complete, official_record_id


def generate_official_json_for_record(cfg: Config, record_id: str) -> tuple[int, dict[str, Any]]:
    if not cfg.structured_version_retention_enforce:
        return 500, {
            "ok": False,
            "status": "config_error",
            "error_code": "version_retention_required",
        }
    with record_lock(cfg, "official", record_id):
        return _generate_official_json_for_record_unlocked(cfg, record_id)


def _generate_official_json_for_record_unlocked(cfg: Config, record_id: str) -> tuple[int, dict[str, Any]]:
    if not cfg.structured_version_retention_enforce:
        return 500, {
            "ok": False,
            "status": "config_error",
            "error_code": "version_retention_required",
        }
    structured_fields = list_bitable_fields(cfg, cfg.structured_base_token, cfg.structured_table_id)
    structured_issues = structured_md_field_issues(structured_fields)
    if structured_issues:
        return 500, {"ok": False, "status": "config_error", "error_code": "structured_field_config_error", "issues": structured_issues}

    record = get_bitable_record_from(cfg, cfg.structured_base_token, cfg.structured_table_id, record_id)
    fields = record.get("fields", {})
    if not isinstance(fields, dict):
        raise StructuredError("invalid_record", "Record response has no fields.", 500)

    if not checkbox_is_checked(fields.get(FIELD_STRUCTURED_APPROVED)):
        return 409, {"ok": False, "status": "not_ready", "reason": "review_not_checked", "record_id": record_id}
    meeting_uid = meeting_uid_value(fields.get(FIELD_MEETING_UID))
    semantic_current_hash = plain_field_value(fields.get(FIELD_STRUCTURED_CURRENT_MD_HASH))
    if plain_field_value(fields.get(FIELD_ARCHIVE_STATUS)) != "已归档":
        return 409, {"ok": False, "status": "not_ready", "reason": "archive_status_not_done", "record_id": record_id}
    if plain_field_value(fields.get(FIELD_VERSION_STATUS)) != "已完成":
        return 409, {"ok": False, "status": "not_ready", "reason": "version_retention_not_done", "record_id": record_id}
    md_url = first_url(fields.get(FIELD_STRUCTURED_ARCHIVE_LINK))
    if not md_url:
        return 409, {"ok": False, "status": "not_ready", "reason": "approved_archive_link_missing", "record_id": record_id}
    approved_content_hash = plain_field_value(fields.get(FIELD_APPROVED_SHA256)).lower()
    if not re.fullmatch(r"[0-9a-f]{64}", approved_content_hash):
        return 409, {"ok": False, "status": "not_ready", "reason": "approved_content_hash_missing", "record_id": record_id}
    current_hash = semantic_current_hash
    source_hash = plain_field_value(fields.get(FIELD_STRUCTURED_JSON_SOURCE_MD_HASH))
    existing_json_url = first_url(fields.get(FIELD_STRUCTURED_JSON_LINK))
    previous_json_token = ""
    if existing_json_url:
        previous_json_token, previous_json_type = parse_drive_url(existing_json_url)
        if previous_json_type != "file":
            raise StructuredError(
                "official_json_keep_type_invalid",
                "The previous authoritative JSON link is not a Drive file.",
                409,
            )
    force_regeneration = checkbox_is_checked(fields.get(FIELD_STRUCTURED_NEEDS_JSON_REGEN))
    official_table_id = resolve_bitable_table_id(cfg, cfg.structured_base_token, cfg.official_json_table_id)
    official_fields = list_bitable_fields(cfg, cfg.structured_base_token, official_table_id)
    official_issues = official_json_field_issues(official_fields)
    if official_issues:
        return 500, {"ok": False, "status": "config_error", "error_code": "official_json_field_config_error", "issues": official_issues}
    existing_official_record_id = find_existing_official_json_record(
        cfg,
        official_table_id,
        record_id,
    )

    official_record_id = ""
    existing_official_fields: dict[str, Any] = {}
    terminal_expectation: dict[str, Any] | None = None
    backup_path: Path | None = None
    try:
        update_structured_json_status(
            cfg,
            structured_fields=structured_fields,
            record_id=record_id,
            status=STATUS_RUNNING,
            current_hash=current_hash,
            needs_regeneration=True,
        )
        file_token, file_type = parse_drive_url(md_url)
        if file_type != "file":
            raise StructuredError("unsupported_structured_md_type", "仅支持 Drive Markdown 文件。", 500)
        meta = get_file_meta(cfg, file_token, file_type)
        source_file_name = str(meta.get("name") or meta.get("title") or plain_field_value(fields.get(FIELD_STRUCTURED_TABLE_NAME)) or file_token)
        markdown_bytes = download_drive_file(cfg, file_token)
        downloaded_hash = hashlib.sha256(markdown_bytes).hexdigest()
        if downloaded_hash != approved_content_hash:
            raise StructuredError("approved_archive_hash_mismatch", "归档文件与审核后内容哈希不一致。", 409)
        try:
            markdown_text = markdown_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise StructuredError("structured_md_decode_failed", "结构化 Markdown 不是 UTF-8 文本。", 500) from exc
        meeting_date = normalize_date(markdown_field(markdown_text, "会议日期")) or normalize_date(source_file_name)
        if not meeting_date:
            raise StructuredError("meeting_date_missing", "无法确定会议日期。", 500)
        month = month_from_date_text(meeting_date)
        json_name = official_json_file_name(source_file_name, plain_field_value(fields.get(FIELD_STRUCTURED_TABLE_NAME)))
        with tempfile.TemporaryDirectory(prefix="feishu-official-json-") as tmpdir:
            tmp_path = Path(tmpdir)
            source_path = tmp_path / "source.md"
            output_dir = tmp_path / "official-json"
            source_path.write_bytes(markdown_bytes)
            prepared = run_generate_structured_json(
                cfg,
                approved_markdown_path=source_path,
                output_dir=output_dir,
                json_name=json_name,
                meeting_uid=meeting_uid,
            )
            json_path = Path(str(prepared.get("json_path") or ""))
            source_md_hash = str(prepared.get("source_md_hash") or "")
            row_count = int(prepared.get("row_count") or 0)
            if str(prepared.get("meeting_uid") or "") != meeting_uid:
                raise StructuredError("official_json_identity_mismatch", "正式 JSON 的会议UID与源记录不一致。", 500)
            if int(prepared.get("schema_version") or 0) != cfg.skill_contract_version:
                raise StructuredError("official_json_schema_mismatch", "正式 JSON 的 schema 版本与 Skill 契约不一致。", 500)
            expected_master_version = "sha256:" + file_sha256(cfg.security_master_path)
            if str(prepared.get("security_master_version") or "") != expected_master_version:
                raise StructuredError("official_json_security_master_mismatch", "正式 JSON 的证券主数据版本与 Skill 契约不一致。", 500)
            if not json_path.exists():
                raise StructuredError("official_json_missing", "Official JSON file was not created.", 500)
            if source_md_hash != downloaded_hash:
                raise StructuredError("source_md_hash_mismatch", "JSON 输入哈希与归档 Markdown 原始字节不一致。", 409)
            official_folder_token = ensure_official_json_month_folder(cfg, month)
            if (
                source_hash == source_md_hash
                and not force_regeneration
                and existing_json_url
            ):
                update_structured_json_status(
                    cfg,
                    structured_fields=structured_fields,
                    record_id=record_id,
                    status=STATUS_GENERATED,
                    current_hash=source_md_hash,
                    needs_regeneration=False,
                    error="",
                )
                try:
                    keep_file_token, keep_file_type = parse_drive_url(existing_json_url)
                    if keep_file_type != "file":
                        raise StructuredError(
                            "official_json_keep_type_invalid",
                            "Authoritative official JSON link is not a Drive file.",
                            503,
                        )
                    deleted_count = cleanup_superseded_official_json_files(
                        cfg,
                        official_folder_token=official_folder_token,
                        keep_file_token=keep_file_token,
                    )
                except Exception as cleanup_exc:
                    cleanup_error = stable_error_code(cleanup_exc)
                    logging.error(
                        "official_json_cleanup_pending record_id_hash=%s code=%s",
                        hashlib.sha256(record_id.encode()).hexdigest()[:12],
                        cleanup_error,
                    )
                    return 503, {
                        "ok": False,
                        "status": "generated_cleanup_pending",
                        "error_code": getattr(
                            cleanup_exc,
                            "error_code",
                            "official_json_cleanup_failed",
                        ),
                        "record_id": record_id,
                        "json_url": existing_json_url,
                    }
                return 200, {
                    "ok": True,
                    "status": "skipped_up_to_date",
                    "record_id": record_id,
                    "source_md_hash": source_md_hash,
                    "json_url": existing_json_url,
                    "superseded_files_deleted": deleted_count,
                }

            content = json_path.read_bytes()
            upload_name, uploaded_url, backup_path = upload_official_json_artifact(
                cfg,
                official_folder_token=official_folder_token,
                month=month,
                source_record_id=record_id,
                source_md_hash=source_md_hash,
                json_name=json_name,
                content=content,
            )
            generated_at_ms = int(time.time() * 1000)
            terminal_expectation = {
                "source_md_hash": source_md_hash,
                "json_file_name": upload_name,
                "json_url": uploaded_url,
                "row_count": row_count,
            }
            if existing_official_record_id:
                existing_official_record = get_bitable_record_from(
                    cfg,
                    cfg.structured_base_token,
                    official_table_id,
                    existing_official_record_id,
                )
                snapshot = existing_official_record.get("fields", {})
                if isinstance(snapshot, dict):
                    existing_official_fields = snapshot
            official_record_id = write_official_json_artifact_record(
                cfg,
                official_table_id=official_table_id,
                fields=official_fields,
                source_md_record_id=record_id,
                source_md_url=md_url,
                source_md_hash=source_md_hash,
                meeting_uid=meeting_uid,
                json_file_name=upload_name,
                json_url=uploaded_url,
                row_count=row_count,
                generated_at_ms=generated_at_ms,
                existing_record_id=existing_official_record_id,
            )
            update_structured_json_status(
                cfg,
                structured_fields=structured_fields,
                record_id=record_id,
                status=STATUS_GENERATED,
                current_hash=source_md_hash,
                json_url=uploaded_url,
                json_file_name=upload_name,
                row_count=row_count,
                generated_at_ms=generated_at_ms,
                source_md_hash=source_md_hash,
                needs_regeneration=False,
                error="",
            )
            try:
                keep_file_token, keep_file_type = parse_drive_url(uploaded_url)
                if keep_file_type != "file":
                    raise StructuredError(
                        "official_json_keep_type_invalid",
                        "Authoritative official JSON link is not a Drive file.",
                        503,
                    )
                deleted_count = cleanup_superseded_official_json_files(
                    cfg,
                    official_folder_token=official_folder_token,
                    keep_file_token=keep_file_token,
                    superseded_file_tokens=(previous_json_token,),
                )
            except Exception as cleanup_exc:
                cleanup_error = stable_error_code(cleanup_exc)
                logging.error(
                    "official_json_cleanup_pending record_id_hash=%s code=%s",
                    hashlib.sha256(record_id.encode()).hexdigest()[:12],
                    cleanup_error,
                )
                return 503, {
                    "ok": False,
                    "status": "generated_cleanup_pending",
                    "error_code": getattr(
                        cleanup_exc,
                        "error_code",
                        "official_json_cleanup_failed",
                    ),
                    "record_id": record_id,
                    "official_json_record_id": official_record_id,
                    "file_name": upload_name,
                    "url": uploaded_url,
                }
            return 200, {
                "ok": True,
                "status": "generated",
                "record_id": record_id,
                "official_json_record_id": official_record_id,
                "row_count": row_count,
                "source_md_hash": source_md_hash,
                "file_name": upload_name,
                "url": uploaded_url,
                "local_path": str(backup_path),
                "superseded_files_deleted": deleted_count,
            }
    except Exception as exc:
        error = stable_error_code(exc)
        if terminal_expectation is not None:
            try:
                terminal_complete, reconciled_record_id = official_json_terminal_is_complete(
                    cfg,
                    official_table_id=official_table_id,
                    source_md_record_id=record_id,
                    **terminal_expectation,
                )
            except Exception as reconcile_exc:
                logging.error(
                    "official_json_commit_reconcile_failed record_id_hash=%s code=%s",
                    hashlib.sha256(record_id.encode()).hexdigest()[:12],
                    stable_error_code(reconcile_exc),
                )
                return 503, {
                    "ok": False,
                    "status": "commit_outcome_uncertain",
                    "error_code": "official_json_commit_outcome_uncertain",
                    "record_id": record_id,
                }
            official_record_id = official_record_id or reconciled_record_id
            if terminal_complete:
                try:
                    keep_file_token, keep_file_type = parse_drive_url(
                        str(terminal_expectation["json_url"])
                    )
                    if keep_file_type != "file":
                        raise StructuredError(
                            "official_json_keep_type_invalid",
                            "Authoritative official JSON link is not a Drive file.",
                            503,
                        )
                    deleted_count = cleanup_superseded_official_json_files(
                        cfg,
                        official_folder_token=official_folder_token,
                        keep_file_token=keep_file_token,
                        superseded_file_tokens=(previous_json_token,),
                    )
                except Exception as cleanup_exc:
                    logging.error(
                        "official_json_cleanup_pending record_id_hash=%s code=%s",
                        hashlib.sha256(record_id.encode()).hexdigest()[:12],
                        stable_error_code(cleanup_exc),
                    )
                    return 503, {
                        "ok": False,
                        "status": "generated_cleanup_pending",
                        "error_code": getattr(
                            cleanup_exc,
                            "error_code",
                            "official_json_cleanup_failed",
                        ),
                        "record_id": record_id,
                        "json_url": terminal_expectation["json_url"],
                    }
                return 200, {
                    "ok": True,
                    "status": "generated_reconciled",
                    "record_id": record_id,
                    "official_json_record_id": official_record_id,
                    "row_count": terminal_expectation["row_count"],
                    "source_md_hash": terminal_expectation["source_md_hash"],
                    "file_name": terminal_expectation["json_file_name"],
                    "url": terminal_expectation["json_url"],
                    "local_path": str(backup_path) if backup_path is not None else "",
                    "superseded_files_deleted": deleted_count,
                }
        if official_record_id:
            try:
                if existing_official_record_id and existing_official_fields:
                    restore_official_json_artifact_record(
                        cfg,
                        official_table_id=official_table_id,
                        fields=official_fields,
                        record_id=official_record_id,
                        source_md_record_id=record_id,
                        snapshot_fields=existing_official_fields,
                    )
                elif not existing_official_record_id:
                    mark_official_json_artifact_failed(
                        cfg,
                        official_table_id=official_table_id,
                        record_id=official_record_id,
                        error=error,
                    )
            except Exception:
                logging.error("official_json_restore_failed code=%s", stable_error_code(exc))
        try:
            update_structured_json_status(
                cfg,
                structured_fields=structured_fields,
                record_id=record_id,
                status=STATUS_FAILED,
                current_hash=current_hash,
                source_md_hash=None,
                needs_regeneration=True,
                error=error,
            )
        except Exception:
            logging.error("official_json_failure_status_write_failed code=%s", stable_error_code(exc))
        logging.error(
            "official_json_generate_failed record_id_hash=%s code=%s",
            hashlib.sha256(record_id.encode()).hexdigest()[:12],
            stable_error_code(exc),
        )
        status = getattr(exc, "http_status", 500)
        return status, {"ok": False, "status": "failed", "error_code": getattr(exc, "error_code", "failed"), "message": error, "record_id": record_id}


def health_payload(cfg: Config) -> dict[str, Any]:
    job_root = semantic_job_root(cfg)
    issues: list[str] = []
    if not cfg.structured_http_token:
        issues.append("http_token_missing")
    if not cfg.source_version_retention_enforce or not cfg.structured_version_retention_enforce:
        issues.append("version_retention_not_enforced")
    if not cfg.structured_official_json_folder_token:
        issues.append("official_json_folder_missing")
    baseline_token_configured = bool(structured_baseline_http_token(cfg))
    if not cfg.structured_baseline_http_url or not baseline_token_configured:
        issues.append("structured_baseline_service_missing")
    try:
        require_pinned_file(cfg.skill_script, cfg.skill_script_sha256, "Structured skill script")
    except StructuredError as exc:
        issues.append(f"skill_{exc.error_code}")
    try:
        require_pinned_file(
            cfg.skill_json_script,
            cfg.skill_json_script_sha256,
            "Official JSON prepare script",
        )
    except StructuredError as exc:
        issues.append(f"prepare_{exc.error_code}")
    security_master_exists = bool(cfg.security_master_path and cfg.security_master_path.is_file())
    if not security_master_exists:
        issues.append("security_master_missing")
    return {
        "ok": not issues,
        "service": SERVICE_NAME,
        "generation_schema_version": cfg.skill_contract_version,
        "skill_contract_exists": bool(cfg.skill_contract_manifest and cfg.skill_contract_manifest.is_file()),
        "skill_contract_sha256": file_sha256(cfg.skill_contract_manifest) if cfg.skill_contract_manifest else "",
        "skill_prompt_sha256": file_sha256(cfg.skill_prompt_path) if cfg.skill_prompt_path else "",
        "skill_claim_schema_sha256": file_sha256(cfg.skill_claim_schema_path) if cfg.skill_claim_schema_path else "",
        "security_master_exists": security_master_exists,
        "security_master_sha256": file_sha256(cfg.security_master_path) if security_master_exists else "",
        "skill_manifest_matches_runtime": cfg.skill_contract_version > 0,
        "unified_json_entrypoint": cfg.skill_script.resolve() == cfg.skill_json_script.resolve(),
        "http_token_configured": bool(cfg.structured_http_token),
        "official_json_folder_configured": bool(cfg.structured_official_json_folder_token),
        "output_owner_configured": bool(cfg.output_owner_open_id),
        "structured_baseline_http_configured": bool(
            cfg.structured_baseline_http_url and baseline_token_configured
        ),
        "source_version_retention_enforce": cfg.source_version_retention_enforce,
        "structured_version_retention_enforce": cfg.structured_version_retention_enforce,
        "issues": issues,
        "semantic_pending_jobs": len(list((job_root / "pending").glob("*"))) if (job_root / "pending").exists() else 0,
        "semantic_processing_jobs": len(list((job_root / "processing").glob("*"))) if (job_root / "processing").exists() else 0,
    }


def doctor(cfg: Config, online: bool = False) -> int:
    readiness = health_payload(cfg)
    status: dict[str, Any] = {
        "ok": readiness["ok"],
        "service": SERVICE_NAME,
        "python": sys.version.split()[0],
        "readiness": readiness,
    }
    if online:
        try:
            fields = list_bitable_fields(cfg, cfg.source_base_token, cfg.source_table_id)
            issues = source_field_issues(cfg, fields)
            status["source_field_count"] = len(fields)
            status["source_field_issues"] = issues
            structured_fields = list_bitable_fields(cfg, cfg.structured_base_token, cfg.structured_table_id)
            structured_issues = structured_md_field_issues(structured_fields)
            status["structured_field_count"] = len(structured_fields)
            status["structured_field_issues"] = structured_issues
            try:
                official_table_id = resolve_bitable_table_id(cfg, cfg.structured_base_token, cfg.official_json_table_id)
                official_fields = list_bitable_fields(cfg, cfg.structured_base_token, official_table_id)
                official_issues = official_json_field_issues(official_fields)
                status["official_json_table_visible"] = True
                status["official_json_field_count"] = len(official_fields)
                status["official_json_field_issues"] = official_issues
            except Exception as exc:
                official_issues = [stable_error_code(exc)]
                status["official_json_table_visible"] = False
                status["official_json_field_issues"] = official_issues
            status["ok"] = bool(status["ok"] and (
                not issues
                and not structured_issues
                and not official_issues
            ))
        except Exception as exc:
            status["ok"] = False
            status["online_error"] = stable_error_code(exc)
    print(json.dumps(status, ensure_ascii=False, indent=2))
    return 0 if status["ok"] else 1


def make_handler(cfg: Config) -> type[BaseHTTPRequestHandler]:
    class StructuredHandler(BaseHTTPRequestHandler):
        server_version = "FeishuStructuredGenerate/0.1.0"

        def log_message(self, format: str, *args: Any) -> None:
            return

        def write_json(self, status_code: int, payload: dict[str, Any]) -> None:
            payload = {key: value for key, value in payload.items() if key != "local_path"}
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            path = urllib.parse.urlparse(self.path).path
            if path == "/healthz":
                payload = health_payload(cfg)
                self.write_json(200 if payload["ok"] else 503, payload)
                return
            self.write_json(404, {"ok": False, "error_code": "not_found"})

        def do_POST(self) -> None:
            path = urllib.parse.urlparse(self.path).path
            if path not in {"/generate", "/generate-official-json"}:
                self.write_json(404, {"ok": False, "error_code": "not_found"})
                return
            if not cfg.structured_http_token:
                self.write_json(500, {"ok": False, "status": "config_error", "error_code": "structured_http_token_not_configured"})
                return
            token_values = self.headers.get_all("X-Structured-Token", [])
            if len(token_values) != 1 or not secrets.compare_digest(token_values[0], cfg.structured_http_token):
                self.write_json(401, {"ok": False, "error_code": "unauthorized"})
                return
            if self.headers.get_all("Transfer-Encoding", []):
                self.write_json(400, {"ok": False, "error_code": "transfer_encoding_not_supported"})
                return
            length_values = self.headers.get_all("Content-Length", [])
            if not length_values:
                self.write_json(411, {"ok": False, "error_code": "content_length_required"})
                return
            if len(length_values) != 1:
                self.write_json(400, {"ok": False, "error_code": "ambiguous_content_length"})
                return
            try:
                length = int(length_values[0])
            except ValueError:
                self.write_json(400, {"ok": False, "error_code": "invalid_content_length"})
                return
            if length <= 0:
                self.write_json(400, {"ok": False, "error_code": "invalid_content_length"})
                return
            if length > cfg.max_http_body_bytes:
                self.write_json(413, {"ok": False, "error_code": "request_too_large"})
                return
            type_values = self.headers.get_all("Content-Type", [])
            if len(type_values) != 1 or type_values[0].split(";", 1)[0].strip().lower() != "application/json":
                self.write_json(415, {"ok": False, "error_code": "unsupported_media_type"})
                return
            try:
                body = self.rfile.read(length)
                if len(body) != length:
                    raise ValueError("short_read")
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                self.write_json(400, {"ok": False, "error_code": "invalid_json"})
                return
            if not isinstance(payload, dict):
                self.write_json(400, {"ok": False, "error_code": "invalid_json_object"})
                return
            raw_record_id = payload.get("record_id") or payload.get("recordId")
            if not isinstance(raw_record_id, str):
                self.write_json(400, {"ok": False, "error_code": "missing_record_id"})
                return
            record_id = raw_record_id.strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", record_id):
                self.write_json(400, {"ok": False, "error_code": "invalid_record_id"})
                return
            try:
                if path == "/generate-official-json":
                    status_code, result = generate_official_json_for_record(cfg, record_id)
                else:
                    status_code, result = generate_for_record(cfg, record_id)
            except StructuredError as exc:
                self.write_json(exc.http_status, {"ok": False, "error_code": exc.error_code})
                return
            except Exception:
                logging.error("structured_http_internal_error")
                self.write_json(500, {"ok": False, "error_code": "internal_error"})
                return
            self.write_json(status_code, result)

    return StructuredHandler


def serve(cfg: Config) -> None:
    readiness = health_payload(cfg)
    if not readiness["ok"]:
        raise SystemExit("Structured service is not ready: " + ",".join(readiness["issues"]))
    ensure_semantic_job_dirs(cfg)
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
FEISHU_OFFICIAL_JSON_TABLE_ID=正式JSON
FEISHU_STRUCTURED_PENDING_FOLDER_TOKEN=
FEISHU_STRUCTURED_ARCHIVE_FOLDER_TOKEN=
FEISHU_STRUCTURED_OFFICIAL_JSON_FOLDER_TOKEN=

FEISHU_STRUCTURED_HTTP_TOKEN=
FEISHU_STRUCTURED_HTTP_HOST=127.0.0.1
FEISHU_STRUCTURED_HTTP_PORT=8790
FEISHU_USER_ID_TYPE=open_id
FEISHU_LOG_LEVEL=INFO
FEISHU_VERSION_CONFIG_PATH=data/version_retention.json
FEISHU_SOURCE_VERSION_RETENTION_ENFORCE=true
FEISHU_STRUCTURED_VERSION_RETENTION_ENFORCE=true

STRUCTURED_TABLE_SKILL_SCRIPT=/skills/meeting-minutes-structured-table/scripts/generate_table.py
STRUCTURED_OUTPUT_DIR=/app/structured_outputs
STRUCTURED_FOLDER_REGISTRY_PATH=data/structured_folder_registry.json
STRUCTURED_SEMANTIC_JOB_DIR=/app/semantic-jobs
STRUCTURED_MAX_HTTP_BODY_BYTES=4096
""",
        encoding="utf-8",
    )


def write_secret_file(path_value: str, token: str) -> None:
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    ensure_private_directory(path.parent)
    if path.is_symlink():
        raise StructuredError("unsafe_output_path", "Refusing to replace a symbolic link.")
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(token + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        durable_replace(temporary, path)
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


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
    serve_parser = sub.add_parser("serve")
    serve_parser.add_argument("--apply", action="store_true", help="required because requests can write external records")
    init_fields_parser = sub.add_parser("init-fields")
    init_fields_parser.add_argument("--apply", action="store_true")
    init_config_parser = sub.add_parser("init-config")
    init_config_parser.add_argument("--force", action="store_true")
    token_parser = sub.add_parser("make-token")
    token_parser.add_argument("--write-token-file")
    generate_parser = sub.add_parser("generate-record")
    generate_parser.add_argument("record_id")
    generate_parser.add_argument("--apply", action="store_true", help="required to write the generation state")
    complete_parser = sub.add_parser("complete-job")
    complete_parser.add_argument("job_id")
    complete_parser.add_argument("--apply", action="store_true", help="required to publish job results")
    fail_parser = sub.add_parser("fail-job")
    fail_parser.add_argument("job_id")
    fail_parser.add_argument("--apply", action="store_true", help="required to publish a failure state")
    official_parser = sub.add_parser("generate-official-json-record")
    official_parser.add_argument("record_id")
    official_parser.add_argument("--apply", action="store_true", help="required to create official JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    args = build_parser().parse_args(argv)
    if args.command == "make-token":
        return make_http_token(args)
    if args.command == "init-config":
        init_config(force=args.force)
        return 0
    if args.command in {
        "serve",
        "generate-record",
        "complete-job",
        "fail-job",
        "generate-official-json-record",
    } and not args.apply:
        raise SystemExit(f"{args.command} requires explicit --apply")
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
    if args.command == "complete-job":
        try:
            result = complete_generation_job(cfg, args.job_id)
        except Exception as exc:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "status": "failed",
                        "error_code": getattr(exc, "error_code", "failed"),
                        "message": "Job completion failed.",
                        "job_id": args.job_id,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 1
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "fail-job":
        result = fail_generation_job(cfg, args.job_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "generate-official-json-record":
        status_code, result = generate_official_json_for_record(cfg, args.record_id)
        print(json.dumps({"http_status": status_code, **result}, ensure_ascii=False, indent=2))
        return 0 if status_code < 400 else 1
    if args.command == "serve":
        serve(cfg)
        return 0
    raise SystemExit(f"Unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
