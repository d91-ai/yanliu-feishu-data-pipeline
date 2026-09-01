#!/usr/bin/env python3
"""Minimal Feishu Drive folder -> Bitable record sync.

Uses Feishu official SDK for long-connection event delivery, and Feishu
official OpenAPI endpoints for metadata and Bitable operations.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

try:
    import lark_oapi as lark
except ImportError:  # pragma: no cover - used for operator-friendly startup errors.
    lark = None


OPENAPI_BASE = "https://open.feishu.cn/open-apis"
FILE_CREATED_EVENT_TYPE = "drive.file.created_in_folder_v1"
FILE_CREATED_SUBSCRIBE_EVENT_TYPE = "file.created_in_folder_v1"
BITABLE_RECORD_CHANGED_EVENT_TYPE = "drive.file.bitable_record_changed_v1"
BITABLE_RECORD_CHANGED_SUBSCRIBE_EVENT_TYPE = "file.bitable_record_changed_v1"
ROUTER_EVENT_MAX_ATTEMPTS = 3

VERSION_STATUS_PENDING = "待留存"
VERSION_STATUS_BASELINE = "基线已留存"
VERSION_STATUS_COMPLETE = "已完成"
VERSION_STATUS_FAILED = "留存失败"
VERSION_DIFF_PENDING = "未比较"
VERSION_DIFF_SAME = "无修改"
VERSION_DIFF_CHANGED = "有修改"
VERSION_DIFF_FAILED = "比较失败"

FIELD_BASELINE_LINK = "审核前版本链接"
FIELD_BASELINE_VERSION = "审核前文件版本号"
FIELD_BASELINE_SHA256 = "审核前内容SHA256"
FIELD_APPROVED_VERSION = "审核后文件版本号"
FIELD_APPROVED_SHA256 = "审核后内容SHA256"
FIELD_VERSION_DIFF = "版本差异"
FIELD_VERSION_STATUS = "版本留存状态"
FIELD_VERSION_ERROR = "版本留存错误"
FIELD_VIEWPOINT_COUNT = "观点数"
FIELD_FORM_ATTACHMENT = "会议纪要上传附件"
FIELD_BINDINGS_SCHEMA_VERSION = 1

UNIFIED_PIPELINE_REQUIRED_FIELDS = {
    "会议ID",
    "会议名",
    "会议日期",
    "会议系列",
    "会议类型",
    "数据版本",
    "会议纪要MD",
    "会议纪要审核前MD",
    "会议纪要审核后MD",
    "源纪要审核",
    "行业与市场观点MD",
    "行业与市场观点审核前MD",
    "行业与市场观点审核后MD",
    "行业与市场观点JSON",
    "行业与市场观点审核",
    "标的观点MD",
    "标的观点审核前MD",
    "标的观点审核后MD",
    "标的观点JSON",
    "标的观点审核",
    "脱敏会议纪要MD",
}

UNIFIED_REVIEW_BRANCHES = (
    ("meeting_minutes", "源纪要审核", "会议纪要MD"),
    ("industry_market_viewpoints", "行业与市场观点审核", "行业与市场观点MD"),
    ("structured_viewpoints", "标的观点审核", "标的观点MD"),
)

FORM_INGRESS_REQUIRED_FIELDS = {
    "会议ID",
    "会议名",
    "会议日期",
    "会议系列",
    "会议类型",
    "数据版本",
    "会议纪要MD",
    "会议纪要审核前MD",
    "源纪要审核",
    "行业与市场观点审核",
    "标的观点审核",
}

GENERATION_ARTIFACT_TYPES = ("industry_market_viewpoints", "structured_viewpoints")
MEETING_UID_PATTERN = re.compile(r"mtg_[0-9a-f]{32}")
FORM_RECEIPT_STATES = ("pending", "uploaded", "committed", "jobs_queued")

TYPE_TEXT = 1
TYPE_NUMBER = 2
TYPE_SINGLE_SELECT = 3
TYPE_MULTI_SELECT = 4
TYPE_DATE = 5
TYPE_CHECKBOX = 7
TYPE_USER = 11
TYPE_URL = 15
# Feishu Base's native attachment field type. Keep this local rather than
# accepting arbitrary writable fields: form ingress fails closed when the
# configured field is not an attachment column.
TYPE_ATTACHMENT = 17

READONLY_TYPES = {20, 1001, 1002, 1003, 1004, 1005}

DEFAULT_FIELD_ALIASES = {
    "file_link": ["文件", "文件链接", "文档链接", "链接", "访问链接", "URL", "url"],
    "file_name": ["文件名", "标题", "名称"],
    "upload_time": ["上传时间", "文件创建时间", "创建时间"],
    "uploader": ["上传人", "上传者", "操作者"],
    "owner": ["Owner", "owner", "所有者", "文件所有者"],
    "file_type": ["文件类型"],
    "file_token": ["文件Token", "文件 token", "file_token", "File Token"],
    "folder_token": ["文件夹Token", "文件夹 token", "folder_token", "Folder Token"],
    "source": ["来源"],
    "event_time": ["事件时间", "触发时间"],
}


class FeishuApiError(RuntimeError):
    pass


class ArchivePreconditionError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class PipelineBindingPendingError(FeishuApiError):
    """Raised when a unified-pipeline Drive event has no Base binding yet."""


def safe_error_code(exc: BaseException) -> str:
    """Return a bounded, content-free error code for logs and Base fields."""
    if isinstance(exc, ArchivePreconditionError):
        return exc.code
    if isinstance(exc, PipelineBindingPendingError):
        return "pipeline_binding_pending"
    if isinstance(exc, FeishuApiError):
        return "feishu_api_error"
    if isinstance(exc, (json.JSONDecodeError, UnicodeDecodeError)):
        return "invalid_json"
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, PermissionError):
        return "permission_denied"
    if isinstance(exc, FileNotFoundError):
        return "file_not_found"
    if isinstance(exc, ValueError):
        return "invalid_input"
    return "internal_error"


@dataclass(frozen=True)
class Config:
    app_id: str
    app_secret: str
    folder_token: str
    source_folder_tokens: tuple[str, ...]
    archive_root_folder_token: str
    folder_registry_path: str
    bitable_app_token: str
    bitable_table_id: str
    user_id_type: str = "open_id"
    dry_run: bool = True
    openapi_base: str = OPENAPI_BASE
    field_aliases_json: str = ""
    field_bindings_path: str = ""
    archive_http_enabled: bool = False
    archive_http_host: str = "127.0.0.1"
    archive_http_port: int = 8787
    archive_http_token: str = ""
    archive_allow_no_token: bool = False
    archive_dry_run: bool = True
    archive_original_time_field: str = "原始记录时间"
    archive_file_link_field: str = "文档链接"
    archive_file_name_field: str = "文件名"
    archive_status_field: str = "归档状态"
    archive_link_field: str = "归档链接"
    archive_time_field: str = "归档时间"
    archive_review_field: str = "已审核"
    archive_timezone_offset_hours: int = 8
    version_capture_enabled: bool = False
    version_capture_enforce: bool = False
    version_root_folder_token: str = ""
    version_category: str = ""
    version_baseline_link_field: str = FIELD_BASELINE_LINK
    structured_metadata_enabled: bool = False
    meeting_contract_enabled: bool = False
    meeting_contract_validator: str = ""
    meeting_contract_validator_sha256: str = ""
    event_spool_dir: str = "data/event-spool"
    archive_max_body_bytes: int = 16 * 1024
    pipeline_mode: str = "legacy"
    unregistered_file_spool_dir: str = "data/unregistered-files"
    pipeline_review_job_spool_dir: str = "data/pipeline-review-jobs"
    pipeline_worker_receipt_dir: str = "data/meeting-pipeline-receipts"
    # Prevent direct-cutover migration events from being replayed as fresh
    # review approvals when the unified listener starts.
    pipeline_event_not_before_ms: int = 0
    # Native Base form attachment ingress is deliberately opt-in.  Keep its
    # local receipts separate from Worker receipts so a retry can prove the
    # exact source attachment/hash without changing business fields.
    form_ingress_enabled: bool = False
    # Logical field key. When field bindings are configured, the live Base
    # field name is resolved from its stable field ID for every operation.
    form_attachment_field: str = FIELD_FORM_ATTACHMENT
    form_attachment_max_bytes: int = 10 * 1024 * 1024
    generation_job_spool_dir: str = "data/meeting-generation-jobs"
    form_ingestion_receipt_dir: str = "data/meeting-ingestion-receipts"


_token_cache: dict[str, Any] = {}
_record_lock_state = threading.local()


def parse_dotenv_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
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


def first_env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return default


def first_value(env: Mapping[str, str], *names: str, default: str = "") -> str:
    for name in names:
        value = str(env.get(name, "")).strip()
        if value:
            return value
    return default


def load_dotenv() -> None:
    explicit = os.environ.get("FEISHU_ENV_FILE")
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    candidates.append(Path.cwd() / ".env")
    candidates.append(Path(__file__).resolve().parent / ".env")

    for path in candidates:
        if not path.exists():
            continue
        for key, value in parse_dotenv_file(path).items():
            os.environ.setdefault(key, value)
        # Route-specific CLI commands use the same shared app credentials as
        # the router. Inherit only those credentials from the sibling router
        # env; never inherit folder, Base, table, or workflow resources.
        router_path = path.parent / ".env.router"
        if path.name != ".env.router" and router_path.is_file():
            shared = parse_dotenv_file(router_path)
            for canonical, alias in (
                ("FEISHU_APP_ID", "LARK_APP_ID"),
                ("FEISHU_APP_SECRET", "LARK_APP_SECRET"),
            ):
                if not first_env(canonical, alias):
                    value = first_value(shared, canonical, alias)
                    if value:
                        os.environ[canonical] = value
        return


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_bool_from(env: Mapping[str, str], name: str, default: bool) -> bool:
    value = env.get(name)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value.strip())
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer") from exc


def env_int_from(env: Mapping[str, str], name: str, default: int) -> int:
    value = env.get(name)
    if value is None or not str(value).strip():
        return default
    try:
        return int(str(value).strip())
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer") from exc


def split_tokens(value: str) -> tuple[str, ...]:
    tokens = [item.strip() for item in re.split(r"[,;\s]+", value) if item.strip()]
    return tuple(dict.fromkeys(tokens))


def bool_value(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def version_settings_from_file(env: Mapping[str, str], table_id: str) -> dict[str, Any]:
    raw_path = str(env.get("FEISHU_VERSION_CONFIG_PATH", "data/version_retention.json")).strip()
    if not raw_path:
        return {}
    path = Path(raw_path)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Invalid version retention config at {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"Version retention config must be a JSON object: {path}")
    tables = payload.get("tables", {})
    if not isinstance(tables, dict):
        raise SystemExit(f"Version retention config tables must be a JSON object: {path}")
    settings = tables.get(table_id, {})
    if not isinstance(settings, dict):
        raise SystemExit(f"Version retention settings for {table_id} must be a JSON object: {path}")
    return settings


def config_from_env(env: Mapping[str, str]) -> Config:
    required = {
        "FEISHU_APP_ID": first_value(env, "FEISHU_APP_ID", "LARK_APP_ID"),
        "FEISHU_APP_SECRET": first_value(env, "FEISHU_APP_SECRET", "LARK_APP_SECRET"),
        "FEISHU_FOLDER_TOKEN": str(env.get("FEISHU_FOLDER_TOKEN", "")).strip(),
        "FEISHU_ARCHIVE_ROOT_FOLDER_TOKEN": str(env.get(
            "FEISHU_ARCHIVE_ROOT_FOLDER_TOKEN", ""
        )).strip(),
        "FEISHU_BITABLE_APP_TOKEN": str(env.get("FEISHU_BITABLE_APP_TOKEN", "")).strip(),
        "FEISHU_BITABLE_TABLE_ID": str(env.get("FEISHU_BITABLE_TABLE_ID", "")).strip(),
    }
    missing = [key for key, value in required.items() if not value]
    if missing:
        raise SystemExit(f"Missing required environment variables: {', '.join(missing)}")
    version_settings = version_settings_from_file(env, required["FEISHU_BITABLE_TABLE_ID"])
    enabled_raw = env.get("FEISHU_VERSION_CAPTURE_ENABLED", version_settings.get("enabled"))
    enforce_raw = env.get("FEISHU_VERSION_CAPTURE_ENFORCE", version_settings.get("enforce"))
    version_root = str(
        env.get("FEISHU_VERSION_ROOT_FOLDER_TOKEN", version_settings.get("root_folder_token", ""))
    ).strip()
    version_category = str(env.get("FEISHU_VERSION_CATEGORY", version_settings.get("category", ""))).strip()
    config = Config(
        app_id=required["FEISHU_APP_ID"],
        app_secret=required["FEISHU_APP_SECRET"],
        folder_token=required["FEISHU_FOLDER_TOKEN"],
        source_folder_tokens=split_tokens(str(env.get("FEISHU_SOURCE_FOLDER_TOKENS", ""))),
        archive_root_folder_token=required["FEISHU_ARCHIVE_ROOT_FOLDER_TOKEN"],
        folder_registry_path=str(env.get("FEISHU_FOLDER_REGISTRY_PATH", "data/folder_registry.json")).strip()
        or "data/folder_registry.json",
        bitable_app_token=required["FEISHU_BITABLE_APP_TOKEN"],
        bitable_table_id=required["FEISHU_BITABLE_TABLE_ID"],
        user_id_type=str(env.get("FEISHU_USER_ID_TYPE", "open_id")).strip() or "open_id",
        dry_run=env_bool_from(env, "FEISHU_DRY_RUN", True),
        openapi_base=str(env.get("FEISHU_OPENAPI_BASE", OPENAPI_BASE)).strip() or OPENAPI_BASE,
        field_aliases_json=str(env.get("FEISHU_FIELD_ALIASES_JSON", "")).strip(),
        field_bindings_path=str(env.get("FEISHU_FIELD_BINDINGS_PATH", "")).strip(),
        archive_http_enabled=env_bool_from(env, "FEISHU_ARCHIVE_HTTP_ENABLED", False),
        archive_http_host=str(env.get("FEISHU_ARCHIVE_HTTP_HOST", "127.0.0.1")).strip() or "127.0.0.1",
        archive_http_port=env_int_from(env, "FEISHU_ARCHIVE_HTTP_PORT", 8787),
        archive_http_token=str(env.get("FEISHU_ARCHIVE_HTTP_TOKEN", "")).strip(),
        archive_allow_no_token=env_bool_from(env, "FEISHU_ARCHIVE_ALLOW_NO_TOKEN", False),
        archive_dry_run=env_bool_from(env, "FEISHU_ARCHIVE_DRY_RUN", True),
        archive_original_time_field=str(env.get("FEISHU_ARCHIVE_ORIGINAL_TIME_FIELD", "原始记录时间")).strip()
        or "原始记录时间",
        archive_file_link_field=str(env.get("FEISHU_ARCHIVE_FILE_LINK_FIELD", "文档链接")).strip() or "文档链接",
        archive_file_name_field=str(env.get("FEISHU_ARCHIVE_FILE_NAME_FIELD", "文件名")).strip() or "文件名",
        archive_status_field=str(env.get("FEISHU_ARCHIVE_STATUS_FIELD", "归档状态")).strip() or "归档状态",
        archive_link_field=str(env.get("FEISHU_ARCHIVE_LINK_FIELD", "归档链接")).strip() or "归档链接",
        archive_time_field=str(env.get("FEISHU_ARCHIVE_TIME_FIELD", "归档时间")).strip() or "归档时间",
        archive_review_field=str(env.get("FEISHU_ARCHIVE_REVIEW_FIELD", "已审核")).strip() or "已审核",
        archive_timezone_offset_hours=env_int_from(env, "FEISHU_ARCHIVE_TIMEZONE_OFFSET_HOURS", 8),
        version_capture_enabled=bool_value(enabled_raw, False),
        version_capture_enforce=bool_value(enforce_raw, False),
        version_root_folder_token=version_root,
        version_category=version_category,
        version_baseline_link_field=str(
            env.get("FEISHU_VERSION_BASELINE_LINK_FIELD", FIELD_BASELINE_LINK)
        ).strip()
        or FIELD_BASELINE_LINK,
        structured_metadata_enabled=env_bool_from(
            env,
            "FEISHU_STRUCTURED_METADATA_ENABLED",
            False,
        ),
        meeting_contract_enabled=env_bool_from(
            env,
            "FEISHU_MEETING_CONTRACT_ENABLED",
            False,
        ),
        meeting_contract_validator=str(env.get("FEISHU_MEETING_CONTRACT_VALIDATOR", "")).strip(),
        meeting_contract_validator_sha256=str(
            env.get("FEISHU_MEETING_CONTRACT_VALIDATOR_SHA256", "")
        ).strip().lower(),
        event_spool_dir=str(env.get("FEISHU_EVENT_SPOOL_DIR", "data/event-spool")).strip()
        or "data/event-spool",
        archive_max_body_bytes=env_int_from(env, "FEISHU_ARCHIVE_MAX_BODY_BYTES", 16 * 1024),
        pipeline_mode=str(env.get("FEISHU_PIPELINE_MODE", "legacy")).strip().lower()
        or "legacy",
        unregistered_file_spool_dir=str(
            env.get("FEISHU_UNREGISTERED_FILE_SPOOL_DIR", "data/unregistered-files")
        ).strip()
        or "data/unregistered-files",
        pipeline_review_job_spool_dir=str(
            env.get("FEISHU_PIPELINE_REVIEW_JOB_SPOOL_DIR", "data/pipeline-review-jobs")
        ).strip()
        or "data/pipeline-review-jobs",
        pipeline_worker_receipt_dir=str(
            env.get("FEISHU_PIPELINE_WORKER_RECEIPT_DIR", "data/meeting-pipeline-receipts")
        ).strip()
        or "data/meeting-pipeline-receipts",
        pipeline_event_not_before_ms=env_int_from(
            env, "FEISHU_PIPELINE_EVENT_NOT_BEFORE_MS", 0
        ),
        form_ingress_enabled=env_bool_from(env, "FEISHU_FORM_INGRESS_ENABLED", False),
        form_attachment_field=str(
            first_value(
                env,
                "FEISHU_FORM_ATTACHMENT_FIELD",
                "FEISHU_FORM_INGRESS_ATTACHMENT_FIELD",
                default=FIELD_FORM_ATTACHMENT,
            )
        ).strip()
        or FIELD_FORM_ATTACHMENT,
        form_attachment_max_bytes=env_int_from(
            env,
            "FEISHU_FORM_MAX_ATTACHMENT_BYTES",
            env_int_from(env, "FEISHU_FORM_ATTACHMENT_MAX_BYTES", 10 * 1024 * 1024),
        ),
        generation_job_spool_dir=str(
            first_value(
                env,
                "FEISHU_GENERATION_JOB_SPOOL_DIR",
                "FEISHU_GENERATION_JOB_SPOOL_PATH",
                default="data/meeting-generation-jobs",
            )
        ).strip()
        or "data/meeting-generation-jobs",
        form_ingestion_receipt_dir=str(
            first_value(
                env,
                "FEISHU_FORM_INGESTION_RECEIPT_DIR",
                "FEISHU_INGESTION_RECEIPT_DIR",
                default="data/meeting-ingestion-receipts",
            )
        ).strip()
        or "data/meeting-ingestion-receipts",
    )
    if config.meeting_contract_enabled and (
        not config.meeting_contract_validator
        or not re.fullmatch(r"[0-9a-f]{64}", config.meeting_contract_validator_sha256)
    ):
        raise SystemExit(
            "Meeting contract ingestion requires FEISHU_MEETING_CONTRACT_VALIDATOR "
            "and FEISHU_MEETING_CONTRACT_VALIDATOR_SHA256"
        )
    real_write_enabled = not config.dry_run or not config.archive_dry_run
    if real_write_enabled and (
        not config.version_capture_enabled or not config.version_capture_enforce
    ):
        raise SystemExit(
            "Non-dry-run operation requires version capture and enforcement to be enabled"
        )
    if real_write_enabled and not version_settings_ready(config):
        raise SystemExit(
            "Non-dry-run operation requires FEISHU_VERSION_ROOT_FOLDER_TOKEN "
            "and FEISHU_VERSION_CATEGORY"
        )
    if config.archive_max_body_bytes <= 0:
        raise SystemExit("FEISHU_ARCHIVE_MAX_BODY_BYTES must be positive")
    if config.form_attachment_max_bytes <= 0:
        raise SystemExit("FEISHU_FORM_MAX_ATTACHMENT_BYTES must be positive")
    if not config.form_attachment_field:
        raise SystemExit("FEISHU_FORM_ATTACHMENT_FIELD must not be empty")
    if config.pipeline_mode not in {"legacy", "unified"}:
        raise SystemExit("FEISHU_PIPELINE_MODE must be legacy or unified")
    if config.pipeline_event_not_before_ms < 0:
        raise SystemExit("FEISHU_PIPELINE_EVENT_NOT_BEFORE_MS must be non-negative")
    if config.form_ingress_enabled and config.pipeline_mode != "unified":
        raise SystemExit("FEISHU_FORM_INGRESS_ENABLED requires FEISHU_PIPELINE_MODE=unified")
    if config.pipeline_mode == "unified" and not config.field_bindings_path:
        raise SystemExit("FEISHU_PIPELINE_MODE=unified requires FEISHU_FIELD_BINDINGS_PATH")
    if config.archive_http_enabled and not (
        config.archive_http_token or config.archive_allow_no_token
    ):
        raise SystemExit(
            "Archive HTTP requires FEISHU_ARCHIVE_HTTP_TOKEN unless "
            "FEISHU_ARCHIVE_ALLOW_NO_TOKEN=true is explicitly set"
        )
    return config


def read_config() -> Config:
    load_dotenv()
    return config_from_env(os.environ)


def configure_logging() -> None:
    level_name = os.environ.get("FEISHU_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def request_json(
    cfg: Config,
    method: str,
    path: str,
    token: str | None = None,
    body: dict[str, Any] | None = None,
    query: dict[str, Any] | None = None,
    timeout: int = 20,
) -> dict[str, Any]:
    url = cfg.openapi_base.rstrip("/") + path
    if query:
        url += "?" + urllib.parse.urlencode(query)

    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    data = None if body is None else json.dumps(body).encode("utf-8")
    if method.upper() == "POST" and data is None:
        data = b""
    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise FeishuApiError(f"Feishu OpenAPI returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise FeishuApiError("Could not reach Feishu OpenAPI") from exc

    if not payload:
        return {}
    try:
        result = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise FeishuApiError("Feishu OpenAPI returned invalid JSON") from exc

    if result.get("code", 0) != 0:
        code = result.get("code")
        remote_code = str(code) if isinstance(code, int) and not isinstance(code, bool) else "unknown"
        raise FeishuApiError(f"Feishu OpenAPI rejected the request (code={remote_code})")
    return result


def resolve_project_path(path_value: str) -> Path:
    path = Path(path_value)
    if path.is_absolute():
        return path
    return Path(__file__).resolve().parent / path


def read_config_from_env_file(path_value: str | Path) -> Config:
    path = resolve_project_path(str(path_value))
    if not path.exists():
        raise SystemExit(f"Route env file does not exist: {path}")
    route_env = parse_dotenv_file(path)
    # Router credentials belong to the shared Feishu connection, not to each
    # business route.  Inherit only the app credentials from the router
    # process; resource identifiers remain route-local and fail closed.
    if not first_value(route_env, "FEISHU_APP_ID", "LARK_APP_ID"):
        shared_app_id = first_env("FEISHU_APP_ID", "LARK_APP_ID")
        if shared_app_id:
            route_env["FEISHU_APP_ID"] = shared_app_id
    if not first_value(route_env, "FEISHU_APP_SECRET", "LARK_APP_SECRET"):
        shared_app_secret = first_env("FEISHU_APP_SECRET", "LARK_APP_SECRET")
        if shared_app_secret:
            route_env["FEISHU_APP_SECRET"] = shared_app_secret
    return config_from_env(route_env)


def empty_folder_registry() -> dict[str, Any]:
    return {"months": {}}


def load_folder_registry(cfg: Config) -> dict[str, Any]:
    path = resolve_project_path(cfg.folder_registry_path)
    if not path.exists():
        return empty_folder_registry()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid folder registry JSON at {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"Folder registry must be a JSON object: {path}")
    months = data.setdefault("months", {})
    if not isinstance(months, dict):
        raise SystemExit(f"Folder registry months must be a JSON object: {path}")
    return data


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


def durable_unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    fsync_directory(path.parent)


def save_folder_registry(cfg: Config, registry: dict[str, Any]) -> None:
    path = resolve_project_path(cfg.folder_registry_path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(json.dumps(registry, ensure_ascii=False, indent=2) + "\n")
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


@contextmanager
def folder_registry_lock(cfg: Config, *, exclusive: bool):
    path = resolve_project_path(cfg.folder_registry_path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = path.with_name(path.name + ".lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "r+", encoding="utf-8") as handle:
            fd = -1
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        if fd >= 0:
            os.close(fd)


@contextmanager
def record_operation_lock(cfg: Config, record_id: str):
    """Serialize one record state machine across threads and sibling containers."""
    digest = hashlib.sha256(
        f"{cfg.bitable_app_token}\0{cfg.bitable_table_id}\0{record_id}".encode("utf-8")
    ).hexdigest()
    held = getattr(_record_lock_state, "keys", set())
    if digest in held:
        yield
        return

    root = resolve_project_path(cfg.folder_registry_path).parent / "record-locks"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    lock_path = root / f"{digest}.lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "r+", encoding="utf-8") as handle:
            fd = -1
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            held = set(held)
            held.add(digest)
            _record_lock_state.keys = held
            try:
                yield
            finally:
                held.remove(digest)
                _record_lock_state.keys = held
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        if fd >= 0:
            os.close(fd)


def folder_tokens_from_registry(cfg: Config) -> list[str]:
    tokens: list[str] = []
    for entry in load_folder_registry(cfg).get("months", {}).values():
        if not isinstance(entry, dict):
            continue
        token = str(entry.get("source_folder_token") or "").strip()
        if token:
            tokens.append(token)
    return tokens


def allowed_source_folder_tokens(cfg: Config) -> set[str]:
    tokens: list[str] = [cfg.folder_token]
    tokens.extend(cfg.source_folder_tokens)
    tokens.extend(folder_tokens_from_registry(cfg))
    return {token for token in tokens if token}


def month_for_source_folder(cfg: Config, folder_token: str) -> str:
    for month, entry in load_folder_registry(cfg).get("months", {}).items():
        if isinstance(entry, dict) and entry.get("source_folder_token") == folder_token:
            return str(month)
    return ""


def get_tenant_access_token(cfg: Config) -> str:
    now = time.time()
    token_key = f"tenant_access_token:{cfg.app_id}"
    expires_key = f"expires_at:{cfg.app_id}"
    cached_token = _token_cache.get(token_key)
    expires_at = float(_token_cache.get(expires_key, 0))
    if cached_token and expires_at - 120 > now:
        return str(cached_token)

    result = request_json(
        cfg,
        "POST",
        "/auth/v3/tenant_access_token/internal",
        body={"app_id": cfg.app_id, "app_secret": cfg.app_secret},
    )
    token = result.get("tenant_access_token")
    if not token:
        raise FeishuApiError("tenant_access_token missing in response")
    expire = int(result.get("expire", 7200))
    _token_cache[token_key] = token
    _token_cache[expires_key] = now + expire
    return str(token)


def subscribe_drive_event(
    cfg: Config,
    file_token: str,
    file_type: str,
    event_type: str | None = None,
) -> None:
    token = get_tenant_access_token(cfg)
    path = f"/drive/v1/files/{urllib.parse.quote(file_token)}/subscribe"
    query = {"file_type": file_type}
    if event_type:
        query["event_type"] = event_type
    request_json(
        cfg,
        "POST",
        path,
        token=token,
        query=query,
    )
    logging.info(
        "Subscribed drive event successfully file_type=%s event_scope=%s",
        file_type,
        event_type or "all",
    )


def subscribe_folder(cfg: Config, folder_token: str | None = None) -> None:
    subscribe_drive_event(
        cfg,
        folder_token or cfg.folder_token,
        "folder",
        FILE_CREATED_SUBSCRIBE_EVENT_TYPE,
    )


def subscribe_source_folders(cfg: Config) -> None:
    for folder_token in sorted(allowed_source_folder_tokens(cfg)):
        subscribe_folder(cfg, folder_token)


def subscribe_bitable_record_changes(cfg: Config) -> None:
    subscribe_drive_event(
        cfg,
        cfg.bitable_app_token,
        "bitable",
    )


def subscribe_configured_events(cfg: Config) -> None:
    subscribe_source_folders(cfg)
    if cfg.pipeline_mode == "unified" or cfg.form_ingress_enabled:
        subscribe_bitable_record_changes(cfg)


def get_bitable_app_meta(cfg: Config) -> dict[str, Any]:
    token = get_tenant_access_token(cfg)
    path = f"/bitable/v1/apps/{urllib.parse.quote(cfg.bitable_app_token)}"
    result = request_json(cfg, "GET", path, token=token)
    return result.get("data", {}).get("app", {})


def load_field_bindings(cfg: Config) -> dict[str, str]:
    """Load logical field keys bound to stable Base field IDs.

    Logical keys are intentionally independent from the live field names.
    Renaming a bound Base field therefore does not change Router behavior or
    produce a warning.
    """
    if not cfg.field_bindings_path:
        return {}
    path = resolve_project_path(cfg.field_bindings_path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("field_bindings_file_invalid")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("field_bindings_file_invalid") from exc
    if not isinstance(payload, Mapping) or payload.get("schema_version") != FIELD_BINDINGS_SCHEMA_VERSION:
        raise ValueError("field_bindings_schema_invalid")
    if payload.get("base_token") != cfg.bitable_app_token or payload.get("table_id") != cfg.bitable_table_id:
        raise ValueError("field_bindings_resource_mismatch")
    raw_fields = payload.get("fields")
    if not isinstance(raw_fields, Mapping) or not raw_fields:
        raise ValueError("field_bindings_fields_invalid")
    bindings: dict[str, str] = {}
    used_ids: set[str] = set()
    for logical_key, raw_field_id in raw_fields.items():
        key = str(logical_key).strip()
        field_id = str(raw_field_id).strip()
        if (
            not key
            or not re.fullmatch(r"fld[A-Za-z0-9_-]{4,}", field_id)
            or field_id in used_ids
        ):
            raise ValueError("field_bindings_fields_invalid")
        bindings[key] = field_id
        used_ids.add(field_id)
    return bindings


def required_field_binding_keys(cfg: Config) -> set[str]:
    required: set[str] = set()
    if cfg.pipeline_mode == "unified":
        required.update(UNIFIED_PIPELINE_REQUIRED_FIELDS)
    if cfg.form_ingress_enabled:
        required.update(FORM_INGRESS_REQUIRED_FIELDS)
        required.add(cfg.form_attachment_field)
    if cfg.meeting_contract_enabled and cfg.pipeline_mode != "unified":
        required.add("文档来源")
    if cfg.structured_metadata_enabled:
        required.update(STRUCTURED_METADATA_FIELDS)
    if cfg.archive_http_enabled:
        required.update(
            {
                cfg.archive_original_time_field,
                cfg.archive_file_link_field,
                cfg.archive_file_name_field,
                cfg.archive_status_field,
                cfg.archive_link_field,
                cfg.archive_time_field,
                cfg.archive_review_field,
                cfg.version_baseline_link_field,
                FIELD_BASELINE_VERSION,
                FIELD_BASELINE_SHA256,
                FIELD_APPROVED_VERSION,
                FIELD_APPROVED_SHA256,
                FIELD_VERSION_DIFF,
                FIELD_VERSION_STATUS,
                FIELD_VERSION_ERROR,
            }
        )
    return required


def _list_bitable_fields_raw(cfg: Config) -> list[dict[str, Any]]:
    token = get_tenant_access_token(cfg)
    path = (
        f"/bitable/v1/apps/{urllib.parse.quote(cfg.bitable_app_token)}"
        f"/tables/{urllib.parse.quote(cfg.bitable_table_id)}/fields"
    )
    fields: list[dict[str, Any]] = []
    page_token = ""
    while True:
        query: dict[str, Any] = {"page_size": 100}
        if page_token:
            query["page_token"] = page_token
        result = request_json(cfg, "GET", path, token=token, query=query)
        data = result.get("data", {})
        fields.extend(data.get("items", []))
        if not data.get("has_more"):
            break
        page_token = data.get("page_token", "")
        if not page_token:
            break
    return fields


def resolve_field_binding_maps(
    cfg: Config,
    raw_fields: list[dict[str, Any]],
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    bindings = load_field_bindings(cfg)
    if not bindings:
        return {}, {}, {}
    missing_keys = sorted(required_field_binding_keys(cfg) - set(bindings))
    if missing_keys:
        raise ValueError("field_bindings_required_keys_missing")
    schema_by_id: dict[str, dict[str, Any]] = {}
    for field in raw_fields:
        if not isinstance(field, Mapping):
            continue
        field_id = str(field.get("field_id") or "")
        if not field_id or field_id in schema_by_id:
            raise ValueError("field_bindings_schema_ambiguous")
        schema_by_id[field_id] = dict(field)
    logical_to_current: dict[str, str] = {}
    current_to_logical: dict[str, str] = {}
    id_to_logical: dict[str, str] = {}
    for logical_key, field_id in bindings.items():
        field = schema_by_id.get(field_id)
        if field is None:
            raise ValueError("field_bindings_field_missing")
        current_name = str(field.get("field_name") or "").strip()
        if not current_name or current_name in current_to_logical:
            raise ValueError("field_bindings_schema_ambiguous")
        logical_to_current[logical_key] = current_name
        current_to_logical[current_name] = logical_key
        id_to_logical[field_id] = logical_key
    return logical_to_current, current_to_logical, id_to_logical


def list_bitable_fields(cfg: Config) -> list[dict[str, Any]]:
    raw_fields = _list_bitable_fields_raw(cfg)
    _logical_to_current, _current_to_logical, id_to_logical = resolve_field_binding_maps(
        cfg, raw_fields
    )
    canonical: list[dict[str, Any]] = []
    for raw_field in raw_fields:
        field = dict(raw_field)
        logical_key = id_to_logical.get(str(field.get("field_id") or ""))
        if logical_key:
            field["field_name"] = logical_key
        canonical.append(field)
    return canonical


def canonicalize_record_fields(
    cfg: Config,
    fields: Mapping[str, Any],
    raw_schemas: list[dict[str, Any]],
) -> dict[str, Any]:
    _logical_to_current, current_to_logical, _id_to_logical = resolve_field_binding_maps(
        cfg, raw_schemas
    )
    canonical: dict[str, Any] = {}
    for current_name, value in fields.items():
        logical_key = current_to_logical.get(str(current_name), str(current_name))
        if logical_key in canonical:
            raise ValueError("field_bindings_record_ambiguous")
        canonical[logical_key] = value
    return canonical


def materialize_record_fields(
    cfg: Config,
    fields: Mapping[str, Any],
    raw_schemas: list[dict[str, Any]],
) -> dict[str, Any]:
    logical_to_current, _current_to_logical, _id_to_logical = resolve_field_binding_maps(
        cfg, raw_schemas
    )
    materialized: dict[str, Any] = {}
    for logical_key, value in fields.items():
        current_name = logical_to_current.get(str(logical_key), str(logical_key))
        if current_name in materialized:
            raise ValueError("field_bindings_write_ambiguous")
        materialized[current_name] = value
    return materialized


def list_bitable_records(cfg: Config) -> list[dict[str, Any]]:
    token = get_tenant_access_token(cfg)
    raw_schemas = _list_bitable_fields_raw(cfg) if cfg.field_bindings_path else []
    path = (
        f"/bitable/v1/apps/{urllib.parse.quote(cfg.bitable_app_token)}"
        f"/tables/{urllib.parse.quote(cfg.bitable_table_id)}/records"
    )
    records: list[dict[str, Any]] = []
    page_token = ""
    while True:
        query: dict[str, Any] = {"page_size": 500, "user_id_type": cfg.user_id_type}
        if page_token:
            query["page_token"] = page_token
        result = request_json(cfg, "GET", path, token=token, query=query)
        data = result.get("data", {})
        items = data.get("items") or []
        if not isinstance(items, list):
            raise FeishuApiError("Bitable record list response is invalid")
        for item in items:
            if not isinstance(item, dict):
                continue
            record = dict(item)
            fields = record.get("fields")
            if isinstance(fields, Mapping) and raw_schemas:
                record["fields"] = canonicalize_record_fields(cfg, fields, raw_schemas)
            records.append(record)
        if not data.get("has_more"):
            break
        page_token = str(data.get("page_token") or "")
        if not page_token:
            raise FeishuApiError("Bitable record pagination token is missing")
    return records


def get_file_meta(cfg: Config, file_token: str, file_type: str) -> dict[str, Any]:
    token = get_tenant_access_token(cfg)
    result = request_json(
        cfg,
        "POST",
        "/drive/v1/metas/batch_query",
        token=token,
        query={"user_id_type": cfg.user_id_type},
        body={
            "request_docs": [{"doc_token": file_token, "doc_type": file_type}],
            "with_url": True,
        },
    )
    data = result.get("data", {})
    failed = data.get("failed_list") or []
    if failed:
        raise FeishuApiError("Failed to get file metadata")
    metas = data.get("metas") or []
    if not metas:
        raise FeishuApiError("File metadata response contained no metas")
    return metas[0]


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
            break
        page_token = str(data.get("next_page_token") or data.get("page_token") or "")
        if not page_token:
            break
    return items


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


def folder_token_from_item(item: dict[str, Any]) -> str:
    return str(item.get("token") or item.get("folder_token") or item.get("file_token") or "")


def ensure_child_folder(cfg: Config, parent_folder_token: str, name: str, dry_run: bool) -> str:
    existing = find_child_folder(cfg, parent_folder_token, name)
    if existing:
        token = folder_token_from_item(existing)
        if not token:
            raise FeishuApiError("Existing folder has no token")
        return token
    if dry_run:
        logging.info("FEISHU_ARCHIVE_DRY_RUN=true; skip creating one child folder")
        return ""
    created = create_drive_folder(cfg, parent_folder_token, name)
    token = folder_token_from_item(created)
    if not token:
        raise FeishuApiError("Create folder response did not include token")
    return token


def copy_drive_file(cfg: Config, file_token: str, file_type: str, name: str, target_folder_token: str) -> dict[str, Any]:
    token = get_tenant_access_token(cfg)
    result = request_json(
        cfg,
        "POST",
        f"/drive/v1/files/{urllib.parse.quote(file_token)}/copy",
        token=token,
        query={"user_id_type": cfg.user_id_type},
        body={"name": name, "type": file_type, "folder_token": target_folder_token},
    )
    return result.get("data", {}).get("file", {}) or result.get("data", {}) or {}


def list_drive_file_versions(cfg: Config, file_token: str) -> list[dict[str, Any]]:
    token = get_tenant_access_token(cfg)
    result = request_json(
        cfg,
        "GET",
        f"/drive/v1/files/{urllib.parse.quote(file_token)}/history",
        token=token,
        query={"only_tag": "true", "page_size": 200},
    )
    data = result.get("data", {})
    versions = data.get("versions") or data.get("file_versions") or data.get("items") or []
    if not isinstance(versions, list):
        raise FeishuApiError("Version history response is invalid")
    return [item for item in versions if isinstance(item, dict) and not item.get("is_deleted")]


def download_drive_file_version(cfg: Config, file_token: str, version: str = "") -> bytes:
    token = get_tenant_access_token(cfg)
    url = f"{cfg.openapi_base.rstrip('/')}/drive/v1/files/{urllib.parse.quote(file_token)}/download"
    if version:
        url += "?" + urllib.parse.urlencode({"version": version})
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise FeishuApiError(f"Drive download failed (http={exc.code})") from exc


def sha256_hex(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def encode_multipart_upload(
    file_name: str,
    parent_node: str,
    content: bytes,
    *,
    content_type: str = "application/octet-stream",
) -> tuple[str, bytes]:
    boundary = "----feishu-version-retention-" + secrets.token_hex(16)
    parts: list[bytes] = []

    def add_field(name: str, value: str) -> None:
        parts.append(f"--{boundary}\r\n".encode("utf-8"))
        parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        parts.append(value.encode("utf-8"))
        parts.append(b"\r\n")

    add_field("file_name", file_name)
    add_field("parent_type", "explorer")
    add_field("parent_node", parent_node)
    add_field("size", str(len(content)))
    safe_name = file_name.replace('"', "")
    parts.append(f"--{boundary}\r\n".encode("utf-8"))
    parts.append(
        (
            f'Content-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("utf-8")
    )
    parts.append(content)
    parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return f"multipart/form-data; boundary={boundary}", b"".join(parts)


def upload_drive_file_bytes(
    cfg: Config,
    target_folder_token: str,
    file_name: str,
    content: bytes,
) -> str:
    token = get_tenant_access_token(cfg)
    multipart_type, body = encode_multipart_upload(
        file_name,
        target_folder_token,
        content,
        content_type="text/markdown" if file_name.lower().endswith(".md") else "application/octet-stream",
    )
    url = f"{cfg.openapi_base.rstrip('/')}/drive/v1/files/upload_all"
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": multipart_type},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise FeishuApiError(f"Drive upload failed (http={exc.code})") from exc
    try:
        result = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise FeishuApiError("Drive upload returned invalid JSON") from exc
    if result.get("code", 0) != 0:
        remote_code = result.get("code")
        safe_code = str(remote_code) if isinstance(remote_code, int) and not isinstance(remote_code, bool) else "unknown"
        raise FeishuApiError(f"Drive upload was rejected (code={safe_code})")
    file_token = str(result.get("data", {}).get("file_token") or "")
    if not file_token:
        raise FeishuApiError("Upload response did not include file_token")
    return file_token


def drive_item_token(item: dict[str, Any]) -> str:
    return str(item.get("token") or item.get("file_token") or "")


def find_drive_file_by_name(cfg: Config, folder_token: str, file_name: str) -> dict[str, Any] | None:
    for item in list_drive_folder_items(cfg, folder_token):
        if item.get("type") == "file" and item.get("name") == file_name:
            return item
    return None


def resolve_drive_file_url(cfg: Config, file_token: str, folder_token: str = "") -> str:
    if folder_token:
        for item in list_drive_folder_items(cfg, folder_token):
            if drive_item_token(item) == file_token and item.get("url"):
                return str(item["url"])
    meta = get_file_meta(cfg, file_token, "file")
    url = str(meta.get("url") or "")
    if not url:
        raise FeishuApiError("Uploaded file URL is missing")
    return url


def upload_version_artifact(
    cfg: Config,
    folder_token: str,
    file_name: str,
    content: bytes,
) -> tuple[str, str]:
    expected_hash = sha256_hex(content)
    existing = find_drive_file_by_name(cfg, folder_token, file_name)
    if existing:
        existing_token = drive_item_token(existing)
        if not existing_token:
            raise FeishuApiError("Existing version artifact has no token")
        existing_hash = sha256_hex(download_drive_file_version(cfg, existing_token))
        if existing_hash != expected_hash:
            raise FeishuApiError("Existing version artifact hash mismatch")
        existing_url = str(existing.get("url") or resolve_drive_file_url(cfg, existing_token, folder_token))
        return existing_token, existing_url
    try:
        uploaded_token = upload_drive_file_bytes(cfg, folder_token, file_name, content)
    except FeishuApiError as upload_error:
        # The upload response may be lost after Drive committed the bytes.
        # Reconcile only by the exact target folder/name and a downloaded hash;
        # never guess from a prefix or the newest file.
        reconciled = find_drive_file_by_name(cfg, folder_token, file_name)
        if reconciled:
            reconciled_token = drive_item_token(reconciled)
            if reconciled_token:
                try:
                    reconciled_hash = sha256_hex(download_drive_file_version(cfg, reconciled_token))
                except Exception:
                    reconciled_hash = ""
                if reconciled_hash == expected_hash:
                    reconciled_url = str(
                        reconciled.get("url")
                        or resolve_drive_file_url(cfg, reconciled_token, folder_token)
                    )
                    return reconciled_token, reconciled_url
        raise upload_error
    uploaded_content = download_drive_file_version(cfg, uploaded_token)
    if sha256_hex(uploaded_content) != expected_hash:
        raise FeishuApiError("Uploaded version artifact hash mismatch")
    return uploaded_token, resolve_drive_file_url(cfg, uploaded_token, folder_token)


def deterministic_uuid4(seed: str) -> str:
    digest = hashlib.sha256(seed.encode("utf-8")).digest()[:16]
    return str(uuid.UUID(bytes=digest, version=4))


def event_to_dict(data: Any) -> dict[str, Any]:
    if isinstance(data, dict):
        return data
    if lark is not None:
        try:
            return json.loads(lark.JSON.marshal(data))
        except Exception:
            pass
    if hasattr(data, "__dict__"):
        return dict(data.__dict__)
    raise TypeError(f"Unsupported event object type: {type(data)!r}")


def get_event_parts(raw_event: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    header = raw_event.get("header") or raw_event.get("Header") or {}
    event = raw_event.get("event") or raw_event.get("Event") or raw_event
    return header, event


def id_from_user_obj(user_obj: dict[str, Any], user_id_type: str) -> str:
    if not isinstance(user_obj, dict):
        return ""
    preferred = user_obj.get(user_id_type)
    return str(preferred or user_obj.get("open_id") or user_obj.get("user_id") or user_obj.get("union_id") or "")


def ms_from_seconds_string(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value) * 1000)
    except (TypeError, ValueError):
        return None


def ms_from_millis_string(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def merged_field_aliases(cfg: Config) -> dict[str, list[str]]:
    aliases = {key: list(value) for key, value in DEFAULT_FIELD_ALIASES.items()}
    if not cfg.field_aliases_json:
        return aliases
    try:
        extra = json.loads(cfg.field_aliases_json)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"FEISHU_FIELD_ALIASES_JSON is not valid JSON: {exc}") from exc
    if not isinstance(extra, dict):
        raise SystemExit("FEISHU_FIELD_ALIASES_JSON must be a JSON object")
    for key, names in extra.items():
        if key not in aliases:
            raise SystemExit(f"Unknown field alias key in FEISHU_FIELD_ALIASES_JSON: {key}")
        if isinstance(names, str):
            aliases[key].append(names)
        elif isinstance(names, list):
            aliases[key].extend(str(name) for name in names)
        else:
            raise SystemExit(f"Aliases for {key} must be a string or list of strings")
    return aliases


def find_field(
    fields: list[dict[str, Any]],
    names: list[str],
    allowed_types: set[int],
    used_field_ids: set[str],
) -> dict[str, Any] | None:
    by_exact = {field.get("field_name", ""): field for field in fields}
    by_normalized = {field.get("field_name", "").strip().lower(): field for field in fields}

    for name in names:
        candidates = [by_exact.get(name), by_normalized.get(name.strip().lower())]
        for field in candidates:
            if not field:
                continue
            field_id = field.get("field_id", "")
            field_type = int(field.get("type", 0))
            if field_id in used_field_ids or field_type in READONLY_TYPES:
                continue
            if field_type in allowed_types:
                used_field_ids.add(field_id)
                return field
    return None


def put_field_value(
    record_fields: dict[str, Any],
    field: dict[str, Any] | None,
    value: Any,
    report: list[str],
    text_for_url: str | None = None,
) -> None:
    if field is None or value in (None, ""):
        return

    name = field.get("field_name")
    field_type = int(field.get("type", 0))
    field_id = field.get("field_id")
    if not name:
        return

    if field_type == TYPE_TEXT:
        record_fields[name] = str(value)
    elif field_type == TYPE_NUMBER:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"Field {name} requires a numeric value")
        record_fields[name] = value
    elif field_type == TYPE_URL:
        record_fields[name] = {"text": text_for_url or str(value), "link": str(value)}
    elif field_type == TYPE_DATE:
        record_fields[name] = int(value)
    elif field_type == TYPE_CHECKBOX:
        if not isinstance(value, bool):
            raise ValueError(f"Field {name} requires a boolean value")
        record_fields[name] = value
    elif field_type == TYPE_USER:
        record_fields[name] = [{"id": str(value)}]
    elif field_type == TYPE_SINGLE_SELECT:
        record_fields[name] = str(value)
    elif field_type == TYPE_MULTI_SELECT:
        record_fields[name] = [str(value)]
    else:
        report.append(f"skip unsupported field {name} ({field_id}) type={field_type}")
        return

    report.append(f"map {name} ({field_id}) type={field_type}")


def display_file_name(title: str) -> str:
    if title.lower().endswith(".md"):
        return title[:-3]
    return title


STRUCTURED_METADATA_FIELDS = {
    "源纪要记录": {TYPE_TEXT},
    "源纪要链接": {TYPE_URL, TYPE_TEXT},
    "观点数": {TYPE_NUMBER},
    "会议日期": {TYPE_DATE},
    "会议系列": {TYPE_SINGLE_SELECT, TYPE_TEXT},
    "会议类型": {TYPE_SINGLE_SELECT, TYPE_TEXT},
    "文档来源": {TYPE_SINGLE_SELECT, TYPE_TEXT},
    "已审核": {TYPE_CHECKBOX},
    "归档状态": {TYPE_SINGLE_SELECT, TYPE_TEXT},
}

SUPPORTED_STRUCTURED_SCHEMA_VERSIONS = {2, 3, 6}


def is_v7_review_markdown(content: bytes) -> bool:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return text.startswith("# 标的观点审阅表\n") and not text.startswith("---\n")


def structured_viewpoint_count(content: bytes) -> int:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Structured Markdown is not UTF-8") from exc
    count = len(re.findall(r"(?m)^## 观点(?:[ \t]+\d+)?[ \t]*$", text))
    if count <= 0:
        raise ValueError("Structured Markdown has no viewpoint cards")
    return count


def parse_structured_frontmatter(content: bytes) -> dict[str, Any]:
    if not content or len(content) > 8 * 1024 * 1024:
        raise ValueError("Structured Markdown is empty or exceeds the metadata parser limit")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Structured Markdown must be UTF-8") from exc
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise ValueError("Structured Markdown is missing frontmatter")
    try:
        boundary = lines.index("---", 1, min(len(lines), 80))
    except ValueError as exc:
        raise ValueError("Structured Markdown frontmatter is not terminated") from exc

    metadata: dict[str, Any] = {}
    for raw_line in lines[1:boundary]:
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"([a-z][a-z0-9_]*)\s*:\s*(.*?)\s*", raw_line)
        if not match:
            raise ValueError("Structured Markdown frontmatter contains an unsupported line")
        key, raw_value = match.groups()
        if key in metadata:
            raise ValueError(f"Structured Markdown frontmatter repeats {key}")
        if raw_value.startswith('"'):
            try:
                value = json.loads(raw_value)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Structured Markdown frontmatter has invalid quoted value for {key}") from exc
        elif re.fullmatch(r"-?\d+", raw_value):
            value = int(raw_value)
        else:
            value = raw_value
        metadata[key] = value

    required = {
        "artifact_stage",
        "schema_version",
        "source_record_id",
        "source_archive_url",
        "source_file_name",
        "viewpoint_count",
    }
    missing = sorted(required - set(metadata))
    if missing:
        raise ValueError("Structured Markdown frontmatter is missing: " + ", ".join(missing))
    if (
        metadata["artifact_stage"] != "structured_review_md"
        or metadata["schema_version"] not in SUPPORTED_STRUCTURED_SCHEMA_VERSIONS
    ):
        raise ValueError("Structured Markdown frontmatter contract is unsupported")
    if not re.fullmatch(r"[A-Za-z0-9_-]{4,100}", str(metadata["source_record_id"])):
        raise ValueError("Structured Markdown source_record_id is invalid")
    source_url = str(metadata["source_archive_url"])
    parsed_url = urllib.parse.urlparse(source_url)
    if parsed_url.scheme != "https" or "/file/" not in parsed_url.path:
        raise ValueError("Structured Markdown source_archive_url is invalid")
    source_file_name = str(metadata["source_file_name"])
    if Path(source_file_name).name != source_file_name or not source_file_name.lower().endswith(".md"):
        raise ValueError("Structured Markdown source_file_name is invalid")
    viewpoint_count = metadata["viewpoint_count"]
    if isinstance(viewpoint_count, bool) or not isinstance(viewpoint_count, int) or viewpoint_count <= 0:
        raise ValueError("Structured Markdown viewpoint_count must be a positive integer")
    return metadata


def structured_title_parts(title: str) -> tuple[str, str]:
    name = display_file_name(title).strip()
    match = re.fullmatch(
        r"(20\d{2}-\d{2}-\d{2})\s+-\s+(.+?)\s+-\s+(?:标的观点|结构化观点)",
        name,
    )
    if not match:
        raise ValueError("Structured Markdown filename does not match the review artifact convention")
    date_text, meeting_series = match.groups()
    try:
        normalized = datetime.strptime(date_text, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise ValueError("Structured Markdown filename contains an invalid meeting date") from exc
    if normalized != date_text or not meeting_series.strip():
        raise ValueError("Structured Markdown filename metadata is invalid")
    return date_text, meeting_series.strip()


def source_meeting_type(source_file_name: str) -> str:
    stem = Path(source_file_name).stem
    parts = [part.strip() for part in re.split(r"\s+-\s+", stem) if part.strip()]
    if len(parts) == 2 and re.fullmatch(r"20\d{2}-\d{2}-\d{2}", parts[0]):
        return "多人复盘会"
    if len(parts) == 3 and re.fullmatch(r"20\d{2}-\d{2}-\d{2}", parts[0]):
        mapping = {
            "上市公司交流": "公司交流",
            "专家交流": "专家交流",
            "多人复盘会": "多人复盘会",
        }
        if parts[-1] in mapping:
            return mapping[parts[-1]]
    raise ValueError("Structured Markdown source_file_name does not match a supported meeting naming contract")


def exact_field(fields: list[dict[str, Any]], name: str, allowed_types: set[int]) -> dict[str, Any]:
    matches = [field for field in fields if field.get("field_name") == name]
    if len(matches) != 1:
        raise ValueError(f"Structured metadata requires exactly one writable {name} field")
    field = matches[0]
    field_type = int(field.get("type", 0))
    if field_type in READONLY_TYPES or field_type not in allowed_types:
        raise ValueError(f"Structured metadata field {name} has an incompatible type")
    return field


def known_select_options(field: dict[str, Any]) -> set[str]:
    prop = field.get("property")
    if not isinstance(prop, dict):
        return set()
    options = prop.get("options")
    if not isinstance(options, list):
        return set()
    return {
        str(option.get("name"))
        for option in options
        if isinstance(option, dict) and option.get("name")
    }


def require_known_select_value(field: dict[str, Any], value: str) -> None:
    if int(field.get("type", 0)) != TYPE_SINGLE_SELECT:
        return
    options = known_select_options(field)
    if not options:
        raise ValueError(f"Structured metadata field {field.get('field_name')} has no configured options")
    if value not in options:
        raise ValueError(f"Structured metadata value is not configured for {field.get('field_name')}")


def validate_meeting_contract_content(cfg: Config, content: bytes) -> None:
    if not cfg.meeting_contract_enabled:
        return
    validator = Path(cfg.meeting_contract_validator)
    expected_hash = cfg.meeting_contract_validator_sha256
    if (
        not validator.is_absolute()
        or validator.is_symlink()
        or not validator.is_file()
        or not re.fullmatch(r"[0-9a-f]{64}", expected_hash)
    ):
        raise ValueError("Meeting minutes contract validator configuration is invalid")
    try:
        actual_hash = hashlib.sha256(validator.read_bytes()).hexdigest()
    except OSError as exc:
        raise ValueError("Meeting minutes contract validator is unreadable") from exc
    if actual_hash != expected_hash:
        raise ValueError("Meeting minutes contract validator hash mismatch")
    try:
        with tempfile.TemporaryDirectory(prefix="meeting-contract-") as temp_dir:
            source = Path(temp_dir) / "meeting.md"
            source.write_bytes(content)
            result = subprocess.run(
                [
                    sys.executable,
                    str(validator),
                    str(source),
                    "--json",
                ],
                capture_output=True,
                check=False,
                timeout=30,
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("Meeting minutes contract validator could not complete") from exc
    try:
        payload = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Meeting minutes contract validator returned invalid output") from exc
    if result.returncode != 0 or not isinstance(payload, dict) or payload.get("ok") is not True:
        raise ValueError("Meeting minutes contract validation failed")


def enrich_meeting_contract_record_fields(
    cfg: Config,
    fields: list[dict[str, Any]],
    *,
    record_fields: dict[str, Any],
    report: list[str],
) -> None:
    if not cfg.meeting_contract_enabled:
        return
    field = exact_field(fields, "文档来源", {TYPE_SINGLE_SELECT})
    require_known_select_value(field, "会议纪要")
    put_field_value(record_fields, field, "会议纪要", report)
    report.append("registered meeting-minutes source without content-shape gating")


def enrich_structured_record_fields(
    cfg: Config,
    fields: list[dict[str, Any]],
    *,
    title: str,
    content: bytes,
    record_fields: dict[str, Any],
    report: list[str],
) -> None:
    if not cfg.structured_metadata_enabled:
        return
    metadata = parse_structured_frontmatter(content)
    meeting_date, meeting_series = structured_title_parts(title)
    source_file_name = str(metadata["source_file_name"])
    source_date_match = re.match(r"^(20\d{2}-\d{2}-\d{2})", source_file_name)
    if not source_date_match or source_date_match.group(1) != meeting_date:
        raise ValueError("Structured Markdown source date does not match the output filename")
    meeting_type = source_meeting_type(source_file_name)
    midnight = datetime.strptime(meeting_date, "%Y-%m-%d").replace(
        tzinfo=timezone(timedelta(hours=cfg.archive_timezone_offset_hours))
    )
    values: dict[str, Any] = {
        "源纪要记录": str(metadata["source_record_id"]),
        "源纪要链接": str(metadata["source_archive_url"]),
        "观点数": int(metadata["viewpoint_count"]),
        "会议日期": int(midnight.timestamp() * 1000),
        "会议系列": meeting_series,
        "会议类型": meeting_type,
        "文档来源": "会议纪要",
        "已审核": False,
        "归档状态": "待归档",
    }
    for name, value in values.items():
        field = exact_field(fields, name, STRUCTURED_METADATA_FIELDS[name])
        if isinstance(value, str):
            require_known_select_value(field, value)
        put_field_value(
            record_fields,
            field,
            value,
            report,
            text_for_url=source_file_name if name == "源纪要链接" else None,
        )
    report.append("validated structured_review_md frontmatter")


def build_record_fields(
    cfg: Config,
    fields: list[dict[str, Any]],
    file_meta: dict[str, Any],
    header: dict[str, Any],
    event: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    aliases = merged_field_aliases(cfg)
    used_field_ids: set[str] = set()
    record_fields: dict[str, Any] = {}
    report: list[str] = []

    file_token = str(event.get("file_token", ""))
    folder_token = str(event.get("folder_token", ""))
    file_type = str(event.get("file_type", ""))
    title = str(file_meta.get("title") or file_token)
    url = str(file_meta.get("url") or "")
    owner_id = str(file_meta.get("owner_id") or "")
    uploader_id = id_from_user_obj(event.get("operator_id", {}), cfg.user_id_type)
    created_ms = ms_from_seconds_string(file_meta.get("create_time"))
    event_ms = ms_from_millis_string(header.get("create_time"))

    put_field_value(
        record_fields,
        find_field(fields, aliases["file_link"], {TYPE_URL, TYPE_TEXT}, used_field_ids),
        url,
        report,
        text_for_url=title,
    )
    put_field_value(
        record_fields,
        find_field(fields, aliases["file_name"], {TYPE_TEXT}, used_field_ids),
        display_file_name(title),
        report,
    )
    put_field_value(
        record_fields,
        find_field(fields, aliases["upload_time"], {TYPE_DATE}, used_field_ids),
        created_ms,
        report,
    )
    put_field_value(
        record_fields,
        find_field(fields, aliases["uploader"], {TYPE_USER, TYPE_TEXT}, used_field_ids),
        uploader_id,
        report,
    )
    put_field_value(
        record_fields,
        find_field(fields, aliases["owner"], {TYPE_USER, TYPE_TEXT}, used_field_ids),
        owner_id,
        report,
    )
    put_field_value(
        record_fields,
        find_field(fields, aliases["file_type"], {TYPE_TEXT, TYPE_SINGLE_SELECT, TYPE_MULTI_SELECT}, used_field_ids),
        file_type,
        report,
    )
    put_field_value(
        record_fields,
        find_field(fields, aliases["file_token"], {TYPE_TEXT}, used_field_ids),
        file_token,
        report,
    )
    put_field_value(
        record_fields,
        find_field(fields, aliases["folder_token"], {TYPE_TEXT}, used_field_ids),
        folder_token,
        report,
    )
    put_field_value(
        record_fields,
        find_field(fields, aliases["source"], {TYPE_TEXT, TYPE_SINGLE_SELECT, TYPE_MULTI_SELECT}, used_field_ids),
        "飞书云盘",
        report,
    )
    put_field_value(
        record_fields,
        find_field(fields, aliases["event_time"], {TYPE_DATE}, used_field_ids),
        event_ms,
        report,
    )

    if not record_fields:
        primary_text = next(
            (
                field
                for field in fields
                if field.get("is_primary") and int(field.get("type", 0)) == TYPE_TEXT
            ),
            None,
        )
        put_field_value(record_fields, primary_text, title, report)
        if primary_text:
            report.append("fallback primary text field used for file title")

    return record_fields, report


def require_reconcile_file_token_field(
    cfg: Config,
    fields: list[dict[str, Any]],
    record_fields: Mapping[str, Any],
    file_token: str,
) -> None:
    aliases = {name.strip().lower() for name in merged_field_aliases(cfg)["file_token"] if name.strip()}
    matches = [
        field
        for field in fields
        if int(field.get("type", 0)) == TYPE_TEXT
        and int(field.get("type", 0)) not in READONLY_TYPES
        and str(field.get("field_name") or "").strip().lower() in aliases
    ]
    if len(matches) != 1:
        raise ValueError("Exactly one writable file-token text field is required before record creation")
    field_name = str(matches[0].get("field_name") or "")
    if plain_field_value(record_fields.get(field_name)).strip() != file_token:
        raise ValueError("The file-token field was not populated before record creation")


def create_bitable_record(
    cfg: Config,
    record_fields: dict[str, Any],
    client_token: str,
) -> dict[str, Any]:
    token = get_tenant_access_token(cfg)
    materialized_fields = (
        materialize_record_fields(cfg, record_fields, _list_bitable_fields_raw(cfg))
        if cfg.field_bindings_path
        else dict(record_fields)
    )
    path = (
        f"/bitable/v1/apps/{urllib.parse.quote(cfg.bitable_app_token)}"
        f"/tables/{urllib.parse.quote(cfg.bitable_table_id)}/records"
    )
    return request_json(
        cfg,
        "POST",
        path,
        token=token,
        query={"user_id_type": cfg.user_id_type, "client_token": client_token},
        body={"fields": materialized_fields},
    )


def record_id_from_create_result(result: Mapping[str, Any]) -> str:
    data = result.get("data", {})
    if not isinstance(data, Mapping):
        return ""
    record = data.get("record", {})
    if not isinstance(record, Mapping):
        return ""
    return str(record.get("record_id") or "").strip()


def find_bitable_records_by_file_token(cfg: Config, file_token: str) -> list[dict[str, Any]]:
    alias_names = {
        name.strip().lower()
        for name in merged_field_aliases(cfg)["file_token"]
        if name.strip()
    }
    matches: list[dict[str, Any]] = []
    for record in list_bitable_records(cfg):
        fields = record.get("fields", {})
        if not isinstance(fields, Mapping):
            continue
        values = [
            plain_field_value(value).strip()
            for name, value in fields.items()
            if str(name).strip().lower() in alias_names
        ]
        if any(value == file_token for value in values):
            matches.append(record)
    return matches


def reconcile_created_record(cfg: Config, file_token: str) -> str:
    matches = find_bitable_records_by_file_token(cfg, file_token)
    if len(matches) != 1:
        code = "record_create_unconfirmed" if not matches else "record_create_ambiguous"
        raise FeishuApiError(code)
    record_id = str(matches[0].get("record_id") or "").strip()
    if not record_id:
        raise FeishuApiError("record_create_unconfirmed")
    return record_id


def create_bitable_record_reconciled(
    cfg: Config,
    record_fields: dict[str, Any],
    client_token: str,
    file_token: str,
) -> str:
    try:
        result = create_bitable_record(cfg, record_fields, client_token)
    except FeishuApiError as create_error:
        try:
            return reconcile_created_record(cfg, file_token)
        except FeishuApiError as reconcile_error:
            if str(reconcile_error) == "record_create_ambiguous":
                raise
            raise create_error
    record_id = record_id_from_create_result(result)
    return record_id or reconcile_created_record(cfg, file_token)


def get_bitable_record(cfg: Config, record_id: str) -> dict[str, Any]:
    token = get_tenant_access_token(cfg)
    path = (
        f"/bitable/v1/apps/{urllib.parse.quote(cfg.bitable_app_token)}"
        f"/tables/{urllib.parse.quote(cfg.bitable_table_id)}"
        f"/records/{urllib.parse.quote(record_id)}"
    )
    result = request_json(cfg, "GET", path, token=token, query={"user_id_type": cfg.user_id_type})
    raw_record = result.get("data", {}).get("record", {}) or {}
    if not isinstance(raw_record, Mapping):
        raise FeishuApiError("Bitable record response is invalid")
    record = dict(raw_record)
    fields = record.get("fields")
    if cfg.field_bindings_path and isinstance(fields, Mapping):
        record["fields"] = canonicalize_record_fields(
            cfg,
            fields,
            _list_bitable_fields_raw(cfg),
        )
    return record


def get_attachments(
    cfg: Config,
    record_id: str,
    field_name: str = "",
) -> list[dict[str, Any]]:
    """Read native Base attachment metadata through Base v3 ``get_attachments``.

    Unlike regular Bitable record APIs this endpoint is rooted at ``/base``
    and accepts a record-id list.  The response carries the attachment
    ``extra`` value required by Drive media download; preserve that value
    exactly instead of synthesising a new JSON object.
    """
    token = get_tenant_access_token(cfg)
    binding_field_id = ""
    current_field_name = field_name
    if cfg.field_bindings_path and field_name:
        raw_schemas = _list_bitable_fields_raw(cfg)
        logical_to_current, _current_to_logical, _id_to_logical = resolve_field_binding_maps(
            cfg, raw_schemas
        )
        binding_field_id = load_field_bindings(cfg).get(field_name, "")
        current_field_name = logical_to_current.get(field_name, field_name)
    path = (
        f"/base/v3/bases/{urllib.parse.quote(cfg.bitable_app_token)}"
        f"/tables/{urllib.parse.quote(cfg.bitable_table_id)}/get_attachments"
    )
    result = request_json(
        cfg,
        "POST",
        path,
        token=token,
        body={"record_id_list": [record_id]},
    )
    data = result.get("data", {})
    if not isinstance(data, (Mapping, list)):
        return []
    values: list[dict[str, Any]] = []

    # Production currently returns a compact map keyed by record and field
    # IDs.  Keep supporting the documented record_attachment_info shape below
    # because both response forms have existed across tenants/clients.
    if isinstance(data, Mapping):
        attachment_map = data.get("attachments")
        record_attachment_map = (
            attachment_map.get(record_id)
            if isinstance(attachment_map, Mapping)
            else None
        )
        if isinstance(record_attachment_map, Mapping):
            for field_id, attachments in record_attachment_map.items():
                if not isinstance(attachments, list):
                    continue
                for item in attachments:
                    if not isinstance(item, Mapping):
                        continue
                    normalized = dict(item)
                    normalized.setdefault("record_id", record_id)
                    normalized.setdefault("field_id", str(field_id))
                    if "extra" not in normalized and normalized.get("extra_info") not in (None, ""):
                        normalized["extra"] = normalized["extra_info"]
                    values.append(normalized)

    if values:
        if binding_field_id:
            return [item for item in values if str(item.get("field_id") or "") == binding_field_id]
        return values

    def walk(node: Any, current_record_id: str = "", current_field_name: str = "") -> None:
        if isinstance(node, list):
            for item in node:
                walk(item, current_record_id, current_field_name)
            return
        if not isinstance(node, Mapping):
            return
        node_record_id = str(
            node.get("record_id") or node.get("recordId") or current_record_id or ""
        )
        node_field_name = str(
            node.get("field_name") or node.get("fieldName") or current_field_name or ""
        )
        attachments = (
            node.get("attachment_list")
            or node.get("attachments")
            or node.get("file_list")
            or node.get("items")
        )
        if isinstance(attachments, list):
            for item in attachments:
                if not isinstance(item, Mapping):
                    continue
                normalized = dict(item)
                if node_record_id:
                    normalized.setdefault("record_id", node_record_id)
                if node_field_name:
                    normalized.setdefault("field_name", node_field_name)
                if "extra" not in normalized and normalized.get("extra_info") not in (None, ""):
                    normalized["extra"] = normalized["extra_info"]
                values.append(normalized)
            return
        for key, child in node.items():
            if key in {"record_id", "recordId", "field_name", "fieldName"}:
                continue
            walk(child, node_record_id, node_field_name or str(key))

    walk(data)
    if current_field_name:
        values = [
            item
            for item in values
            if not item.get("field_name") or str(item.get("field_name")) == current_field_name
        ]
    return values


# Descriptive alias retained for callers/tests that prefer the full name.
get_bitable_record_attachments = get_attachments


def download_drive_media(
    cfg: Config,
    file_token: str,
    *,
    extra: Any = None,
) -> bytes:
    """Download a Base attachment through Drive media with explicit ``extra``.

    Base attachment tokens are not regular Drive ``file`` resources.  The
    media endpoint requires the caller to pass the Base binding in ``extra``;
    keeping it explicit prevents accidentally downloading an unrelated token.
    """
    token = get_tenant_access_token(cfg)
    query: dict[str, Any] = {}
    if extra not in (None, ""):
        # Feishu returns ``extra`` either as an opaque string or a structured
        # object.  Strings must be sent byte-for-byte unchanged for the media
        # binding to remain valid.
        query["extra"] = (
            extra
            if isinstance(extra, str)
            else json.dumps(dict(extra), ensure_ascii=False, separators=(",", ":"))
        )
    url = f"{cfg.openapi_base.rstrip('/')}/drive/v1/medias/{urllib.parse.quote(file_token)}/download"
    if query:
        url += "?" + urllib.parse.urlencode(query)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise FeishuApiError(f"Drive media download failed (http={exc.code})") from exc


# Keep a short alias aligned with the wording used in Feishu's API docs.
download_media_extra = download_drive_media


def update_bitable_record(cfg: Config, record_id: str, record_fields: dict[str, Any]) -> dict[str, Any]:
    token = get_tenant_access_token(cfg)
    materialized_fields = (
        materialize_record_fields(cfg, record_fields, _list_bitable_fields_raw(cfg))
        if cfg.field_bindings_path
        else dict(record_fields)
    )
    path = (
        f"/bitable/v1/apps/{urllib.parse.quote(cfg.bitable_app_token)}"
        f"/tables/{urllib.parse.quote(cfg.bitable_table_id)}"
        f"/records/{urllib.parse.quote(record_id)}"
    )
    return request_json(
        cfg,
        "PUT",
        path,
        token=token,
        query={"user_id_type": cfg.user_id_type},
        body={"fields": materialized_fields},
    )


def value_is_empty(value: Any) -> bool:
    if value in (None, ""):
        return True
    if isinstance(value, (list, tuple, dict)) and not value:
        return True
    return False


def plain_field_value(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        for key in ("text", "link", "name", "value"):
            if value.get(key):
                return str(value[key])
        return ""
    if isinstance(value, list):
        parts = [plain_field_value(item) for item in value]
        return ",".join(part for part in parts if part)
    return str(value)


def checkbox_is_checked(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "y", "checked", "是"}
    if isinstance(value, dict):
        for key in ("checked", "value", "text"):
            if key in value and checkbox_is_checked(value.get(key)):
                return True
        return False
    if isinstance(value, list):
        return any(checkbox_is_checked(item) for item in value)
    return False


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


def month_from_ms(ms: int, offset_hours: int) -> str:
    tz = timezone(timedelta(hours=offset_hours))
    return datetime.fromtimestamp(ms / 1000, tz=tz).strftime("%Y-%m")


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
    raise ValueError("Unsupported or invalid Feishu Drive URL")


def version_settings_ready(cfg: Config) -> bool:
    return bool(cfg.version_root_folder_token and cfg.version_category)


def ensure_version_baseline_folder(cfg: Config, month: str) -> str:
    if not version_settings_ready(cfg):
        raise ValueError("Version retention requires root_folder_token and category")
    category_token = ensure_child_folder(
        cfg,
        cfg.version_root_folder_token,
        cfg.version_category,
        dry_run=False,
    )
    month_token = ensure_child_folder(cfg, category_token, month, dry_run=False)
    return ensure_child_folder(cfg, month_token, "审核前", dry_run=False)


def version_sort_key(item: dict[str, Any]) -> tuple[int, str]:
    raw = item.get("edited_at") or item.get("create_time") or 0
    try:
        edited_at = int(str(raw))
    except ValueError:
        edited_at = 0
    return edited_at, str(item.get("version") or "")


def first_valid_file_version(
    cfg: Config,
    file_token: str,
) -> tuple[dict[str, Any], bytes]:
    versions = sorted(list_drive_file_versions(cfg, file_token), key=version_sort_key)
    if not versions:
        raise FeishuApiError("No version history found")
    for version_info in versions:
        version = str(version_info.get("version") or "")
        if not version:
            continue
        content = download_drive_file_version(cfg, file_token, version)
        if content.strip():
            return version_info, content
    raise ValueError("Source file has no non-empty version; legacy blank templates are not valid review baselines")


def latest_file_version(
    cfg: Config,
    file_token: str,
) -> tuple[dict[str, Any], bytes]:
    versions = sorted(list_drive_file_versions(cfg, file_token), key=version_sort_key, reverse=True)
    if not versions:
        raise FeishuApiError("No version history found")
    version_info = versions[0]
    version = str(version_info.get("version") or "")
    if not version:
        raise FeishuApiError("Latest version is missing version id")
    return version_info, download_drive_file_version(cfg, file_token, version)


def baseline_artifact_name(file_name: str, file_token: str, version_info: dict[str, Any]) -> str:
    path = Path(file_name)
    suffix = path.suffix or ".md"
    stem = path.stem or file_token
    tag = str(version_info.get("tag") or version_info.get("version") or "unknown")
    return f"{stem} - 审核前 - {file_token[-8:]} - v{tag}{suffix}"


def capture_baseline_for_record(
    cfg: Config,
    record_id: str,
    *,
    fields: dict[str, Any] | None = None,
    month_override: str = "",
) -> dict[str, Any]:
    with record_operation_lock(cfg, record_id):
        return _capture_baseline_for_record(
            cfg,
            record_id,
            fields=fields,
            month_override=month_override,
        )


def _capture_baseline_for_record(
    cfg: Config,
    record_id: str,
    *,
    fields: dict[str, Any] | None = None,
    month_override: str = "",
) -> dict[str, Any]:
    if not cfg.version_capture_enabled:
        return {"status": "skipped", "reason": "version_capture_disabled", "record_id": record_id}
    if not version_settings_ready(cfg):
        raise ValueError("Version retention is enabled but root/category settings are incomplete")
    if fields is None:
        record = get_bitable_record(cfg, record_id)
        fields = record.get("fields", {})
    if not isinstance(fields, dict):
        raise FeishuApiError("Record response has no fields")

    baseline_link = url_from_field_value(fields.get(cfg.version_baseline_link_field))
    baseline_version = plain_field_value(fields.get(FIELD_BASELINE_VERSION))
    baseline_hash = plain_field_value(fields.get(FIELD_BASELINE_SHA256))
    if baseline_link and baseline_version and baseline_hash:
        return {
            "status": "baseline_exists",
            "record_id": record_id,
            "version": baseline_version,
            "sha256": baseline_hash,
            "url": baseline_link,
        }

    update_bitable_record(
        cfg,
        record_id,
        {
            FIELD_VERSION_STATUS: VERSION_STATUS_PENDING,
            FIELD_VERSION_DIFF: VERSION_DIFF_PENDING,
            FIELD_VERSION_ERROR: "",
        },
    )
    file_url = url_from_field_value(fields.get(cfg.archive_file_link_field))
    if not file_url:
        raise ValueError("Record file link is empty")
    if month_override:
        if not re.fullmatch(r"\d{4}-\d{2}", month_override):
            raise ValueError(f"Invalid version retention month override: {month_override}")
        month = month_override
    else:
        original_ms = ms_from_record_time(fields.get(cfg.archive_original_time_field))
        if original_ms is None:
            raise ValueError("Record original time is invalid")
        month = month_from_ms(original_ms, cfg.archive_timezone_offset_hours)
    file_token, file_type = parse_drive_url(file_url)
    if file_type != "file":
        raise ValueError(f"Version retention currently supports Drive file links, got {file_type}")
    meta = get_file_meta(cfg, file_token, file_type)
    file_name = str(meta.get("title") or plain_field_value(fields.get(cfg.archive_file_name_field)) or file_token)
    version_info, content = first_valid_file_version(cfg, file_token)
    version = str(version_info.get("version") or "")
    content_hash = sha256_hex(content)
    target_folder = ensure_version_baseline_folder(cfg, month)
    artifact_name = baseline_artifact_name(file_name, file_token, version_info)
    _artifact_token, artifact_url = upload_version_artifact(
        cfg,
        target_folder,
        artifact_name,
        content,
    )
    update_bitable_record(
        cfg,
        record_id,
        {
            cfg.version_baseline_link_field: {"text": artifact_name, "link": artifact_url},
            FIELD_BASELINE_VERSION: version,
            FIELD_BASELINE_SHA256: content_hash,
            FIELD_VERSION_STATUS: VERSION_STATUS_BASELINE,
            FIELD_VERSION_DIFF: VERSION_DIFF_PENDING,
            FIELD_VERSION_ERROR: "",
        },
    )
    return {
        "status": "baseline_captured",
        "record_id": record_id,
        "month": month,
        "version": version,
        "sha256": content_hash,
        "url": artifact_url,
    }


def baseline_terminal_result(cfg: Config, record_id: str) -> dict[str, Any] | None:
    record = get_bitable_record(cfg, record_id)
    fields = record.get("fields", {})
    if not isinstance(fields, dict):
        raise FeishuApiError("Record response has no fields")
    version = plain_field_value(fields.get(FIELD_BASELINE_VERSION))
    content_hash = plain_field_value(fields.get(FIELD_BASELINE_SHA256))
    url = url_from_field_value(fields.get(cfg.version_baseline_link_field))
    if (
        plain_field_value(fields.get(FIELD_VERSION_STATUS))
        in {VERSION_STATUS_BASELINE, VERSION_STATUS_COMPLETE}
        and url
        and version
        and content_hash
    ):
        return {
            "status": "baseline_reconciled",
            "record_id": record_id,
            "version": version,
            "sha256": content_hash,
            "url": url,
        }
    return None


def baseline_terminal_is_complete(cfg: Config, record_id: str) -> bool:
    return baseline_terminal_result(cfg, record_id) is not None


def capture_baseline_for_record_with_failure_status(
    cfg: Config,
    record_id: str,
    *,
    fields: dict[str, Any] | None = None,
    month_override: str = "",
) -> dict[str, Any]:
    with record_operation_lock(cfg, record_id):
        try:
            if fields is None:
                return capture_baseline_for_record(
                    cfg,
                    record_id,
                    month_override=month_override,
                )
            return capture_baseline_for_record(
                cfg,
                record_id,
                fields=fields,
                month_override=month_override,
            )
        except Exception as exc:
            try:
                terminal_result = baseline_terminal_result(cfg, record_id)
            except Exception as recheck_exc:
                terminal_result = None
                logging.error(
                    "baseline_terminal_recheck_failed code=%s",
                    safe_error_code(recheck_exc),
                )
                raise
            if terminal_result is not None:
                logging.info("baseline_commit_reconciled")
                return terminal_result
            else:
                try:
                    update_bitable_record(
                        cfg,
                        record_id,
                        {
                            FIELD_VERSION_STATUS: VERSION_STATUS_FAILED,
                            FIELD_VERSION_DIFF: VERSION_DIFF_FAILED,
                            FIELD_VERSION_ERROR: safe_error_code(exc),
                        },
                    )
                except Exception:
                    logging.error("version_baseline_failure_status_write_failed")
            raise


def migrate_archived_record(cfg: Config, record_id: str) -> dict[str, Any]:
    with record_operation_lock(cfg, record_id):
        return _migrate_archived_record_unlocked(cfg, record_id)


def _migrate_archived_record_unlocked(cfg: Config, record_id: str) -> dict[str, Any]:
    if not version_settings_ready(cfg):
        raise ValueError("Version retention migration requires root/category settings")
    record = get_bitable_record(cfg, record_id)
    fields = record.get("fields", {})
    if not isinstance(fields, dict):
        raise FeishuApiError("Record response has no fields")

    existing_status = plain_field_value(fields.get(FIELD_VERSION_STATUS))
    if (
        existing_status == VERSION_STATUS_COMPLETE
        and url_from_field_value(fields.get(cfg.version_baseline_link_field))
        and plain_field_value(fields.get(FIELD_BASELINE_VERSION))
        and plain_field_value(fields.get(FIELD_BASELINE_SHA256))
        and plain_field_value(fields.get(FIELD_APPROVED_VERSION))
        and plain_field_value(fields.get(FIELD_APPROVED_SHA256))
    ):
        return {"status": "migration_exists", "record_id": record_id}

    source_url = url_from_field_value(fields.get(cfg.archive_file_link_field))
    archive_url = url_from_field_value(fields.get(cfg.archive_link_field))
    if not source_url or not archive_url:
        raise ValueError("Archived migration requires both source and archive links")
    archive_ms = ms_from_record_time(fields.get(cfg.archive_time_field))
    if archive_ms is None:
        raise ValueError("Record archive time is invalid")
    original_ms = ms_from_record_time(fields.get(cfg.archive_original_time_field))
    if original_ms is None:
        raise ValueError("Record original time is invalid")
    month = month_from_ms(original_ms, cfg.archive_timezone_offset_hours)

    source_token, source_type = parse_drive_url(source_url)
    archive_token, archive_type = parse_drive_url(archive_url)
    if source_type != "file" or archive_type != "file":
        raise ValueError("Archived migration currently supports Drive file links only")

    versions = sorted(list_drive_file_versions(cfg, source_token), key=version_sort_key)
    if not versions:
        raise FeishuApiError("No source version history found")
    version_content: dict[str, bytes] = {}

    def content_for(version_info: dict[str, Any]) -> bytes:
        version = str(version_info.get("version") or "")
        if not version:
            raise FeishuApiError("Source version is missing version id")
        if version not in version_content:
            version_content[version] = download_drive_file_version(cfg, source_token, version)
        return version_content[version]

    baseline_info = versions[0]
    baseline_content = content_for(baseline_info)
    if not baseline_content.strip():
        raise ValueError("Legacy blank-template record is not eligible for automatic version migration")
    baseline_version = str(baseline_info.get("version") or "")
    baseline_hash = sha256_hex(baseline_content)

    archive_content = download_drive_file_version(cfg, archive_token)
    approved_hash = sha256_hex(archive_content)
    approved_info: dict[str, Any] | None = None
    for version_info in reversed(versions):
        edited_at, _version = version_sort_key(version_info)
        if edited_at and edited_at > archive_ms:
            continue
        if sha256_hex(content_for(version_info)) == approved_hash:
            approved_info = version_info
            break
    if approved_info is None:
        raise ValueError("No source version before archive time matches the archived file hash")
    approved_version = str(approved_info.get("version") or "")

    source_meta = get_file_meta(cfg, source_token, source_type)
    file_name = str(
        source_meta.get("title")
        or plain_field_value(fields.get(cfg.archive_file_name_field))
        or source_token
    )
    target_folder = ensure_version_baseline_folder(cfg, month)
    artifact_name = baseline_artifact_name(file_name, source_token, baseline_info)
    _artifact_token, artifact_url = upload_version_artifact(
        cfg,
        target_folder,
        artifact_name,
        baseline_content,
    )
    version_diff = VERSION_DIFF_SAME if baseline_hash == approved_hash else VERSION_DIFF_CHANGED
    update_bitable_record(
        cfg,
        record_id,
        {
            cfg.version_baseline_link_field: {"text": artifact_name, "link": artifact_url},
            FIELD_BASELINE_VERSION: baseline_version,
            FIELD_BASELINE_SHA256: baseline_hash,
            FIELD_APPROVED_VERSION: approved_version,
            FIELD_APPROVED_SHA256: approved_hash,
            FIELD_VERSION_DIFF: version_diff,
            FIELD_VERSION_STATUS: VERSION_STATUS_COMPLETE,
            FIELD_VERSION_ERROR: "",
            **(
                {FIELD_VIEWPOINT_COUNT: structured_viewpoint_count(archive_content)}
                if cfg.structured_metadata_enabled
                else {}
            ),
        },
    )
    return {
        "status": "migrated",
        "record_id": record_id,
        "month": month,
        "baseline_version": baseline_version,
        "approved_version": approved_version,
        "version_diff": version_diff,
    }


def migration_terminal_is_complete(cfg: Config, record_id: str) -> bool:
    record = get_bitable_record(cfg, record_id)
    fields = record.get("fields", {})
    if not isinstance(fields, dict):
        raise FeishuApiError("Record response has no fields")
    return bool(
        plain_field_value(fields.get(FIELD_VERSION_STATUS)) == VERSION_STATUS_COMPLETE
        and url_from_field_value(fields.get(cfg.version_baseline_link_field))
        and plain_field_value(fields.get(FIELD_BASELINE_VERSION))
        and plain_field_value(fields.get(FIELD_BASELINE_SHA256))
        and plain_field_value(fields.get(FIELD_APPROVED_VERSION))
        and plain_field_value(fields.get(FIELD_APPROVED_SHA256))
    )


def migrate_archived_record_with_failure_status(cfg: Config, record_id: str) -> dict[str, Any]:
    with record_operation_lock(cfg, record_id):
        try:
            return migrate_archived_record(cfg, record_id)
        except Exception as exc:
            try:
                terminal_complete = migration_terminal_is_complete(cfg, record_id)
            except Exception as recheck_exc:
                terminal_complete = None
                logging.error(
                    "migration_terminal_recheck_failed code=%s",
                    safe_error_code(recheck_exc),
                )
            if terminal_complete is True:
                logging.info("migration_commit_reconciled")
                return {"status": "migration_reconciled", "record_id": record_id}
            if terminal_complete is False:
                try:
                    update_bitable_record(
                        cfg,
                        record_id,
                        {
                            FIELD_VERSION_STATUS: VERSION_STATUS_FAILED,
                            FIELD_VERSION_DIFF: VERSION_DIFF_FAILED,
                            FIELD_VERSION_ERROR: safe_error_code(exc),
                        },
                    )
                except Exception:
                    logging.error("migration_failure_status_write_failed")
            raise


def archive_record(cfg: Config, record_id: str) -> dict[str, Any]:
    with record_operation_lock(cfg, record_id):
        return _archive_record_unlocked(cfg, record_id)


def _archive_record_unlocked(cfg: Config, record_id: str) -> dict[str, Any]:
    record = get_bitable_record(cfg, record_id)
    fields = record.get("fields", {})
    if not isinstance(fields, dict):
        raise FeishuApiError("Record response has no fields")

    if not checkbox_is_checked(fields.get(cfg.archive_review_field)):
        raise ArchivePreconditionError("approval_required")

    archive_link = fields.get(cfg.archive_link_field)
    if not value_is_empty(archive_link):
        return {
            "status": "skipped",
            "reason": "archive_link_exists",
            "record_id": record_id,
            "version_retention_status": plain_field_value(fields.get(FIELD_VERSION_STATUS)),
        }

    status = plain_field_value(fields.get(cfg.archive_status_field))
    if status not in {"待归档", "归档中"}:
        raise ArchivePreconditionError("archive_status_not_ready")

    file_url = url_from_field_value(fields.get(cfg.archive_file_link_field))
    if not file_url:
        raise ValueError("Record file link is empty")

    original_ms = ms_from_record_time(fields.get(cfg.archive_original_time_field))
    if original_ms is None:
        raise ValueError("Record original time is invalid")
    month = month_from_ms(original_ms, cfg.archive_timezone_offset_hours)

    file_token, file_type = parse_drive_url(file_url)
    file_meta = get_file_meta(cfg, file_token, file_type)
    file_name = str(file_meta.get("title") or plain_field_value(fields.get(cfg.archive_file_name_field)) or file_token)

    baseline_result: dict[str, Any] = {}
    if cfg.version_capture_enabled and not cfg.archive_dry_run:
        try:
            baseline_result = capture_baseline_for_record_with_failure_status(
                cfg,
                record_id,
                fields=fields,
            )
        except Exception as exc:
            if cfg.version_capture_enforce:
                raise
            logging.error("version_baseline_non_enforcing_failure code=%s", safe_error_code(exc))
            baseline_result = {"status": "failed", "error": safe_error_code(exc)}
    elif cfg.version_capture_enforce and not cfg.archive_dry_run:
        raise ValueError("Version retention enforcement requires version capture to be enabled")

    version_ready = bool(baseline_result.get("sha256"))
    if cfg.version_capture_enforce and not version_ready:
        raise ValueError("Review baseline is missing; approval archive is blocked")

    if cfg.archive_dry_run:
        target_folder_token = ""
    else:
        update_bitable_record(cfg, record_id, {cfg.archive_status_field: "归档中"})
        target_folder_token = ensure_child_folder(cfg, cfg.archive_root_folder_token, month, dry_run=False)

    if cfg.archive_dry_run:
        logging.info(
            "FEISHU_ARCHIVE_DRY_RUN=true; skip copying file type=%s to archive month=%s",
            file_type,
            month,
        )
        return {
            "status": "dry_run",
            "record_id": record_id,
            "month": month,
            "file_token": file_token,
            "file_type": file_type,
            "target_parent_folder_token": cfg.archive_root_folder_token,
        }

    approved_version = ""
    approved_hash = ""
    version_diff = ""
    if version_ready:
        if file_type != "file":
            raise ValueError(f"Version retention currently supports Drive file links, got {file_type}")
        approved_info, approved_content = latest_file_version(cfg, file_token)
        approved_version = str(approved_info.get("version") or "")
        approved_hash = sha256_hex(approved_content)
        _copied_token, copied_url = upload_version_artifact(
            cfg,
            target_folder_token,
            file_name,
            approved_content,
        )
        copied_name = file_name
        baseline_hash = str(baseline_result.get("sha256") or "")
        version_diff = VERSION_DIFF_SAME if baseline_hash == approved_hash else VERSION_DIFF_CHANGED
    else:
        copied = copy_drive_file(cfg, file_token, file_type, file_name, target_folder_token)
        copied_url = str(copied.get("url") or "")
        copied_name = str(copied.get("name") or file_name)
        if not copied_url:
            raise FeishuApiError("Copy response did not include url")

    update_fields: dict[str, Any] = {
        cfg.archive_status_field: "已归档",
        cfg.archive_link_field: {"text": copied_name, "link": copied_url},
        cfg.archive_time_field: int(time.time() * 1000),
    }
    if version_ready:
        update_fields.update(
            {
                FIELD_APPROVED_VERSION: approved_version,
                FIELD_APPROVED_SHA256: approved_hash,
                FIELD_VERSION_DIFF: version_diff,
                FIELD_VERSION_STATUS: VERSION_STATUS_COMPLETE,
                FIELD_VERSION_ERROR: "",
            }
        )
        if cfg.structured_metadata_enabled:
            update_fields[FIELD_VIEWPOINT_COUNT] = structured_viewpoint_count(approved_content)
    update_bitable_record(
        cfg,
        record_id,
        update_fields,
    )
    return {
        "status": "archived",
        "record_id": record_id,
        "month": month,
        "archive_folder_token": target_folder_token,
        "archive_url": copied_url,
        "approved_version": approved_version,
        "approved_sha256": approved_hash,
        "version_diff": version_diff,
    }


def archive_terminal_is_complete(cfg: Config, record_id: str) -> bool:
    record = get_bitable_record(cfg, record_id)
    fields = record.get("fields", {})
    if not isinstance(fields, dict):
        raise FeishuApiError("Record response has no fields")
    if plain_field_value(fields.get(cfg.archive_status_field)) != "已归档":
        return False
    if value_is_empty(fields.get(cfg.archive_link_field)):
        return False
    if cfg.version_capture_enabled and cfg.version_capture_enforce:
        return bool(
            plain_field_value(fields.get(FIELD_VERSION_STATUS)) == VERSION_STATUS_COMPLETE
            and url_from_field_value(fields.get(cfg.version_baseline_link_field))
            and plain_field_value(fields.get(FIELD_BASELINE_VERSION))
            and plain_field_value(fields.get(FIELD_BASELINE_SHA256))
            and plain_field_value(fields.get(FIELD_APPROVED_VERSION))
            and plain_field_value(fields.get(FIELD_APPROVED_SHA256))
        )
    return True


def archive_record_with_failure_status(cfg: Config, record_id: str) -> dict[str, Any]:
    with record_operation_lock(cfg, record_id):
        try:
            return archive_record(cfg, record_id)
        except ArchivePreconditionError:
            raise
        except Exception as exc:
            if not cfg.archive_dry_run:
                try:
                    terminal_complete = archive_terminal_is_complete(cfg, record_id)
                except Exception as recheck_exc:
                    terminal_complete = None
                    logging.error(
                        "archive_terminal_recheck_failed code=%s",
                        safe_error_code(recheck_exc),
                    )
                if terminal_complete is True:
                    logging.info("archive_commit_reconciled")
                    return {"status": "archive_reconciled", "record_id": record_id}
                if terminal_complete is False:
                    try:
                        failure_fields: dict[str, Any] = {cfg.archive_status_field: "归档失败"}
                        if cfg.version_capture_enabled:
                            try:
                                baseline_complete = baseline_terminal_is_complete(cfg, record_id)
                            except Exception as baseline_recheck_exc:
                                baseline_complete = True
                                logging.error(
                                    "archive_baseline_recheck_failed code=%s",
                                    safe_error_code(baseline_recheck_exc),
                                )
                            if not baseline_complete:
                                failure_fields.update(
                                    {
                                        FIELD_VERSION_STATUS: VERSION_STATUS_FAILED,
                                        FIELD_VERSION_DIFF: VERSION_DIFF_FAILED,
                                        FIELD_VERSION_ERROR: safe_error_code(exc),
                                    }
                                )
                        update_bitable_record(cfg, record_id, failure_fields)
                    except Exception:
                        logging.error("archive_failure_status_write_failed code=%s", safe_error_code(exc))
            raise


def unified_pipeline_schema_ready(fields: list[dict[str, Any]]) -> bool:
    names = {str(field.get("field_name") or "") for field in fields if isinstance(field, dict)}
    return UNIFIED_PIPELINE_REQUIRED_FIELDS.issubset(names)


def url_references_file_token(url: str, file_token: str) -> bool:
    if not url or not file_token:
        return False
    parsed = urllib.parse.urlparse(url)
    segments = [urllib.parse.unquote(item) for item in parsed.path.split("/") if item]
    if file_token in segments:
        return True
    query = urllib.parse.parse_qs(parsed.query)
    return any(file_token == str(value) for values in query.values() for value in values)


def find_unified_records_by_file_token(cfg: Config, file_token: str) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for record in list_bitable_records(cfg):
        fields = record.get("fields", {})
        if not isinstance(fields, dict):
            continue
        if url_references_file_token(url_from_field_value(fields.get("会议纪要MD")), file_token):
            matches.append(record)
    return matches


def write_private_json_once(path: Path, payload: Mapping[str, Any]) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    lock_path = path.with_name(f".{path.name}.lock")
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(lock_fd, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        if path.exists():
            if path.is_symlink() or not path.is_file():
                raise ValueError("unsafe_pipeline_spool_path")
            return False
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temp_path = Path(temp_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fd = -1
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            durable_replace(temp_path, path)
            os.chmod(path, 0o600)
            fsync_directory(path.parent)
        finally:
            if fd >= 0:
                os.close(fd)
            if temp_path.exists():
                temp_path.unlink()
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
    return True


def persist_unregistered_pipeline_file(
    cfg: Config,
    raw_event: Mapping[str, Any],
    *,
    file_token: str,
    folder_token: str,
) -> Path:
    header, _event = get_event_parts(dict(raw_event))
    event_id = str(header.get("event_id") or "")
    key_material = event_id or f"{folder_token}:{file_token}"
    key = hashlib.sha256(key_material.encode("utf-8")).hexdigest()
    root = resolve_project_path(cfg.unregistered_file_spool_dir)
    path = root / f"{key}.json"
    write_private_json_once(
        path,
        {
            "schema_version": 1,
            "reason": "pipeline_binding_pending",
            "event_id": event_id,
            "file_token": file_token,
            "folder_token": folder_token,
            "recorded_at": int(time.time() * 1000),
        },
    )
    return path


def process_unified_file_created_event(
    cfg: Config,
    raw_event: dict[str, Any],
    *,
    fields: list[dict[str, Any]],
    file_token: str,
    folder_token: str,
) -> bool:
    if cfg.form_ingress_enabled and form_ingestion_file_token_bound(cfg, file_token):
        logging.info("Skip Drive file event for a form-ingress-owned upload")
        return True
    if not unified_pipeline_schema_ready(fields):
        raise ValueError("unified_pipeline_schema_not_ready")
    matches = find_unified_records_by_file_token(cfg, file_token)
    if len(matches) > 1:
        raise ValueError("unified_pipeline_file_binding_ambiguous")
    if len(matches) == 1:
        logging.info("Unified pipeline file event matched existing meeting record")
        return True
    persist_unregistered_pipeline_file(
        cfg,
        raw_event,
        file_token=file_token,
        folder_token=folder_token,
    )
    raise PipelineBindingPendingError("pipeline_binding_pending")


def extract_file_token_from_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    segments = [urllib.parse.unquote(item) for item in parsed.path.split("/") if item]
    for index, segment in enumerate(segments[:-1]):
        if segment in {"file", "files"} and segments[index + 1]:
            return segments[index + 1]
    query = urllib.parse.parse_qs(parsed.query)
    for name in ("file_token", "token"):
        values = query.get(name) or []
        if len(values) == 1 and values[0]:
            return str(values[0])
    return ""


PIPELINE_JOB_STATES = ("pending", "processing", "done", "failed", "stale")


def existing_review_job_state(
    cfg: Config,
    *,
    job_id: str,
    payload: Mapping[str, Any],
) -> str | None:
    root = resolve_project_path(cfg.pipeline_review_job_spool_dir)
    name = f"{job_id}.json"
    matches = [
        root / state / name
        for state in PIPELINE_JOB_STATES
        if (root / state / name).exists()
    ]
    if len(matches) > 1:
        raise ValueError("unified_review_job_multiple_states")
    if matches:
        path = matches[0]
        if path.is_symlink() or not path.is_file():
            raise ValueError("unsafe_pipeline_review_job_path")
        existing = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(existing, dict):
            raise ValueError("unified_review_job_invalid")
        comparable = dict(existing)
        comparable["event_time"] = payload["event_time"]
        if comparable != dict(payload):
            raise ValueError("unified_review_job_conflict")
        return path.parent.name

    receipt_name = hashlib.sha256(f"review\0{job_id}".encode("utf-8")).hexdigest()
    receipt_path = (
        resolve_project_path(cfg.pipeline_worker_receipt_dir) / f"{receipt_name}.json"
    )
    if not receipt_path.exists():
        return None
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise ValueError("unsafe_pipeline_review_receipt_path")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if (
        not isinstance(receipt, dict)
        or receipt.get("queue_name") != "review"
        or receipt.get("job_id") != job_id
    ):
        raise ValueError("unified_review_receipt_conflict")
    return "receipt"


def enqueue_unified_review_jobs(
    cfg: Config,
    *,
    record_id: str,
    fields: dict[str, Any],
    event_time: str,
    artifact_types: set[str] | None = None,
) -> list[str]:
    meeting_uid = plain_field_value(fields.get("会议ID")).strip().lower()
    if not re.fullmatch(r"mtg_[0-9a-f]{32}", meeting_uid):
        raise ValueError("unified_review_meeting_uid_invalid")
    try:
        data_version = int(float(plain_field_value(fields.get("数据版本"))))
    except (TypeError, ValueError) as exc:
        raise ValueError("unified_review_data_version_invalid") from exc
    if data_version < 1:
        raise ValueError("unified_review_data_version_invalid")
    queued: list[str] = []
    pending = resolve_project_path(cfg.pipeline_review_job_spool_dir) / "pending"
    for artifact_type, review_field, current_md_field in UNIFIED_REVIEW_BRANCHES:
        if artifact_types is not None and artifact_type not in artifact_types:
            continue
        if plain_field_value(fields.get(review_field)).strip() != "已审核":
            continue
        review_url = url_from_field_value(fields.get(current_md_field))
        review_file_token = extract_file_token_from_url(review_url)
        if not review_file_token:
            raise ValueError(f"unified_review_file_token_missing:{artifact_type}")
        review_content = download_drive_file_version(cfg, review_file_token)
        review_sha256 = hashlib.sha256(review_content).hexdigest()
        identity = f"{record_id}\0{artifact_type}\0{review_sha256}\0review-approved"
        job_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        payload = {
            "job_version": 1,
            "job_type": "review_update",
            "job_id": job_key,
            "record_id": record_id,
            "meeting_uid": meeting_uid,
            "artifact_type": artifact_type,
            "data_version": data_version,
            "review_file_token": review_file_token,
            "review_url": review_url,
            "review_md_sha256": review_sha256,
            "review_action": "approved",
            "event_time": event_time,
        }
        if existing_review_job_state(cfg, job_id=job_key, payload=payload) is not None:
            continue
        path = pending / f"{job_key}.json"
        if not write_private_json_once(path, payload):
            if existing_review_job_state(cfg, job_id=job_key, payload=payload) is None:
                raise ValueError("unified_review_job_write_unconfirmed")
            continue
        queued.append(artifact_type)
    return queued


def changed_unified_review_artifact_types(
    action: Mapping[str, Any],
    field_schemas: list[dict[str, Any]],
) -> set[str]:
    """Return only review branches whose review field changed in this event."""
    if str(action.get("action") or "") != "record_edited":
        return set()
    after_value = action.get("after_value")
    if not isinstance(after_value, list):
        return set()
    changed_field_ids = {
        str(value.get("field_id") or "")
        for value in after_value
        if isinstance(value, Mapping) and str(value.get("field_id") or "")
    }
    if not changed_field_ids:
        return set()

    review_field_to_artifact = {
        review_field: artifact_type
        for artifact_type, review_field, _current_md_field in UNIFIED_REVIEW_BRANCHES
    }
    field_id_to_artifact: dict[str, str] = {}
    seen_review_fields: set[str] = set()
    for field in field_schemas:
        if not isinstance(field, Mapping):
            continue
        field_name = str(field.get("field_name") or "")
        artifact_type = review_field_to_artifact.get(field_name)
        if artifact_type is None:
            continue
        field_id = str(field.get("field_id") or "")
        if not field_id or field_name in seen_review_fields or field_id in field_id_to_artifact:
            raise ValueError("unified_review_field_schema_ambiguous")
        seen_review_fields.add(field_name)
        field_id_to_artifact[field_id] = artifact_type
    if seen_review_fields != set(review_field_to_artifact):
        raise ValueError("unified_review_field_schema_missing")
    return {
        field_id_to_artifact[field_id]
        for field_id in changed_field_ids
        if field_id in field_id_to_artifact
    }


def _attachment_token(value: Mapping[str, Any]) -> str:
    for key in ("file_token", "fileToken", "token", "attachment_token", "attachmentToken"):
        token = str(value.get(key) or "").strip()
        if token:
            return token
    return ""


def _attachment_name(value: Mapping[str, Any]) -> str:
    for key in ("name", "file_name", "fileName", "title"):
        name = str(value.get(key) or "").strip()
        if name:
            return name
    return ""


def _attachment_items_from_field(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _attachment_tokens_from_record(record: Mapping[str, Any], field_name: str) -> set[str]:
    fields = record.get("fields", {})
    if not isinstance(fields, Mapping):
        return set()
    return {
        token
        for token in (_attachment_token(item) for item in _attachment_items_from_field(fields.get(field_name)))
        if token
    }


def validate_form_ingress_schema(
    cfg: Config,
    fields: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Validate the native attachment column and unified writeback columns."""
    by_name: dict[str, dict[str, Any]] = {}
    for field in fields:
        if not isinstance(field, dict):
            continue
        name = str(field.get("field_name") or "").strip()
        if name:
            by_name[name] = field

    attachment = by_name.get(cfg.form_attachment_field)
    if attachment is None:
        raise ValueError("form_attachment_field_missing")
    attachment_type = int(attachment.get("type", 0) or 0)
    if attachment_type != TYPE_ATTACHMENT or attachment_type in READONLY_TYPES:
        raise ValueError("form_attachment_field_type_invalid")

    missing = sorted(FORM_INGRESS_REQUIRED_FIELDS - set(by_name))
    if missing:
        raise ValueError("form_ingress_schema_missing:" + ",".join(missing))
    type_rules: dict[str, set[int]] = {
        "会议ID": {TYPE_TEXT},
        "会议名": {TYPE_TEXT},
        "会议日期": {TYPE_DATE, TYPE_NUMBER},
        "会议系列": {TYPE_TEXT, TYPE_SINGLE_SELECT},
        "会议类型": {TYPE_TEXT, TYPE_SINGLE_SELECT},
        "数据版本": {TYPE_NUMBER},
        "会议纪要MD": {TYPE_URL, TYPE_TEXT},
        "会议纪要审核前MD": {TYPE_URL, TYPE_TEXT},
        "源纪要审核": {TYPE_TEXT, TYPE_SINGLE_SELECT},
        "行业与市场观点审核": {TYPE_TEXT, TYPE_SINGLE_SELECT},
        "标的观点审核": {TYPE_TEXT, TYPE_SINGLE_SELECT},
    }
    for name, allowed in type_rules.items():
        field = by_name[name]
        field_type = int(field.get("type", 0) or 0)
        if field_type in READONLY_TYPES or field_type not in allowed:
            raise ValueError(f"form_ingress_field_type_invalid:{name}")
    return by_name


def parse_form_meeting_filename(file_name: str) -> tuple[str, str, str]:
    """Validate a short Markdown attachment name.

    Meeting metadata is sourced from the Base form fields, never inferred from
    this user-provided filename.  The tuple shape is retained for compatibility
    with local callers; only the first item is meaningful.
    """
    name = Path(file_name).name
    if name != file_name or not name.lower().endswith(".md"):
        raise ValueError("form_attachment_filename_invalid")
    return name, "", ""


def meeting_name_from_filename(
    file_name: str,
    fallback: str = "",
    meeting_date: str = "",
) -> str:
    """Return a compact human label without making it a business identity."""
    name = Path(file_name).name
    if name != file_name:
        raise ValueError("meeting_name_filename_invalid")
    stem = Path(name).stem
    stem = re.sub(r"^20\d{2}-\d{2}-\d{2}\s*[-–—]\s*", "", stem)
    stem = re.sub(r"\s*[-–—]\s*v\d+\s*$", "", stem, flags=re.IGNORECASE)
    stem = re.sub(r"\s*[-–—]\s*会议纪要\s*$", "", stem)
    normalized = unicodedata.normalize("NFKC", stem)
    normalized = " ".join(normalized.split()) or " ".join(
        unicodedata.normalize("NFKC", str(fallback or "")).split()
    )
    if not normalized:
        raise ValueError("meeting_name_missing")
    if len(normalized) > 80:
        raise ValueError("meeting_name_too_long")
    if any(ord(char) < 32 or ord(char) == 127 for char in normalized):
        raise ValueError("meeting_name_invalid")
    if meeting_date:
        if not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", meeting_date):
            raise ValueError("meeting_name_date_invalid")
        normalized = f"{meeting_date} - {normalized}"
    return normalized


def form_meeting_date_from_field(cfg: Config, value: Any) -> str:
    if isinstance(value, Mapping):
        for key in ("timestamp", "value", "date"):
            if value.get(key) not in (None, ""):
                return form_meeting_date_from_field(cfg, value.get(key))
    raw = plain_field_value(value).strip()
    if not raw:
        raise ValueError("form_meeting_date_missing")
    if re.fullmatch(r"20\d{2}-\d{2}-\d{2}", raw):
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date().isoformat()
        except ValueError as exc:
            raise ValueError("form_meeting_date_invalid") from exc
    try:
        number = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("form_meeting_date_invalid") from exc
    # Base date values are milliseconds; tolerate seconds for hand-created
    # fixtures while keeping the resulting date explicit and deterministic.
    if number < 10_000_000_000:
        number *= 1000
    try:
        return datetime.fromtimestamp(number / 1000, timezone(timedelta(hours=cfg.archive_timezone_offset_hours))).date().isoformat()
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("form_meeting_date_invalid") from exc


def form_meeting_metadata_from_fields(cfg: Config, fields: Mapping[str, Any]) -> tuple[str, str, str]:
    date_text = form_meeting_date_from_field(cfg, fields.get("会议日期"))
    meeting_series = normalize_form_path_component(
        plain_field_value(fields.get("会议系列")), "form_meeting_series"
    )
    meeting_type = normalize_form_path_component(
        plain_field_value(fields.get("会议类型")), "form_meeting_type"
    )
    return date_text, meeting_series, meeting_type


def normalize_form_path_component(value: Any, error_code: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    normalized = " ".join(normalized.split())
    if not normalized:
        raise ValueError(f"{error_code}_missing")
    if len(normalized) > 40:
        raise ValueError(f"{error_code}_too_long")
    if any(char in "/\\" or ord(char) < 32 or ord(char) == 127 for char in normalized):
        raise ValueError(f"{error_code}_invalid")
    return normalized


def form_url_field(url: str, name: str) -> dict[str, str]:
    return {"link": url, "text": name}


def _form_review_status(existing_fields: Mapping[str, Any], field_name: str) -> str:
    return "需重审" if plain_field_value(existing_fields.get(field_name)).strip() == "已审核" else "未审核"


def _form_date_millis(cfg: Config, date_text: str) -> int:
    midnight = datetime.strptime(date_text, "%Y-%m-%d").replace(
        tzinfo=timezone(timedelta(hours=cfg.archive_timezone_offset_hours))
    )
    return int(midnight.timestamp() * 1000)


def _form_receipt_root(cfg: Config) -> Path:
    root = resolve_project_path(cfg.form_ingestion_receipt_dir)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    return root


def form_ingestion_receipt_key(record_id: str, attachment_file_token: str, content_sha256: str) -> str:
    material = f"{record_id}\0{attachment_file_token}\0{content_sha256}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def form_ingestion_receipt_path(
    cfg: Config,
    record_id: str,
    attachment_file_token: str,
    content_sha256: str,
) -> Path:
    return _form_receipt_root(cfg) / f"{form_ingestion_receipt_key(record_id, attachment_file_token, content_sha256)}.json"


def load_private_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise ValueError("unsafe_form_ingestion_receipt_path")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("form_ingestion_receipt_invalid")
    return payload


def write_private_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    lock_path = path.with_name(f".{path.name}.lock")
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(lock_fd, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fd = -1
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            durable_replace(temporary, path)
            os.chmod(path, 0o600)
        finally:
            if fd >= 0:
                os.close(fd)
            if temporary.exists():
                temporary.unlink()
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def form_ingestion_receipts_for_record(cfg: Config, record_id: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    root = _form_receipt_root(cfg)
    for path in sorted(root.glob("*.json")):
        receipt = load_private_json(path)
        if receipt and str(receipt.get("record_id") or "") == record_id:
            records.append(receipt)
    return records


def form_ingestion_file_token_bound(cfg: Config, file_token: str) -> bool:
    """Return true for files uploaded by form ingress, suppressing our own event."""
    if not file_token:
        return False
    for receipt in form_ingestion_receipts_for_record_all(cfg):
        if file_token in {
            str(receipt.get("source_file_token") or ""),
            str(receipt.get("review_file_token") or ""),
        }:
            return True
    return False


def form_ingestion_receipts_for_record_all(cfg: Config) -> list[dict[str, Any]]:
    receipts: list[dict[str, Any]] = []
    root = _form_receipt_root(cfg)
    for path in sorted(root.glob("*.json")):
        receipt = load_private_json(path)
        if receipt:
            receipts.append(receipt)
    return receipts


def ensure_form_ingress_folders(cfg: Config, month: str, *, dry_run: bool) -> tuple[str, str]:
    """Ensure source ``root/YYYY-MM`` and the separate version baseline root."""
    month = validate_month(month)
    review_token = ""
    with folder_registry_lock(cfg, exclusive=not dry_run):
        registry = load_folder_registry(cfg)
        months = registry.setdefault("months", {})
        entry = months.setdefault(month, {})
        if not isinstance(entry, dict):
            raise ValueError("form_month_registry_invalid")
        source_token = str(entry.get("source_folder_token") or "")
        if not source_token:
            source_token = ensure_child_folder(cfg, cfg.folder_token, month, dry_run=dry_run)
            if source_token:
                entry["source_folder_token"] = source_token
        review_token = str(entry.get("version_baseline_folder_token") or "")
        entry["updated_at"] = int(time.time() * 1000)
        if not dry_run:
            save_folder_registry(cfg, registry)
    if dry_run:
        return source_token, ""
    if not review_token:
        review_token = ensure_version_baseline_folder(cfg, month)
        with folder_registry_lock(cfg, exclusive=True):
            registry = load_folder_registry(cfg)
            entry = registry.setdefault("months", {}).setdefault(month, {})
            if isinstance(entry, dict):
                entry["version_baseline_folder_token"] = review_token
                entry["updated_at"] = int(time.time() * 1000)
                save_folder_registry(cfg, registry)
    return source_token, review_token


def _generation_job_state(cfg: Config, job_id: str, expected: Mapping[str, Any]) -> str | None:
    root = resolve_project_path(cfg.generation_job_spool_dir)
    states = ("pending", "processing", "done", "failed", "stale")
    matches = [root / state / f"{job_id}.json" for state in states if (root / state / f"{job_id}.json").exists()]
    if len(matches) > 1:
        raise ValueError("generation_job_multiple_states")
    if matches:
        existing = load_private_json(matches[0])
        if existing is None:
            raise ValueError("generation_job_invalid")
        if existing != dict(expected):
            raise ValueError("generation_job_conflict")
        return matches[0].parent.name
    return None


def enqueue_form_generation_jobs(
    cfg: Config,
    *,
    record_id: str,
    meeting_uid: str,
    data_version: int,
    input_file_token: str,
    input_md_sha256: str,
    meeting_date: str,
    meeting_series: str,
    meeting_type: str,
    created_at: str,
) -> list[str]:
    if not MEETING_UID_PATTERN.fullmatch(meeting_uid):
        raise ValueError("generation_meeting_uid_invalid")
    if data_version < 1 or not input_file_token or not re.fullmatch(r"[0-9a-f]{64}", input_md_sha256):
        raise ValueError("generation_identity_invalid")
    pending = resolve_project_path(cfg.generation_job_spool_dir) / "pending"
    pending.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(pending, 0o700)
    queued: list[str] = []
    for artifact_type in GENERATION_ARTIFACT_TYPES:
        job_id = f"{meeting_uid}-v{data_version}-{artifact_type}"
        job = {
            "job_version": 1,
            "job_id": job_id,
            "state": "pending",
            "meeting_uid": meeting_uid,
            "record_id": record_id,
            "artifact_type": artifact_type,
            "data_version": data_version,
            "input_file_token": input_file_token,
            "input_md_sha256": input_md_sha256,
            "meeting_date": meeting_date,
            "meeting_series": meeting_series,
            "meeting_type": meeting_type,
            "source_review_status": "未审核",
            "created_at": created_at,
        }
        path = pending / f"{job_id}.json"
        state = _generation_job_state(cfg, job_id, job)
        if state is None:
            if not write_private_json_once(path, job):
                state = _generation_job_state(cfg, job_id, job)
                if state is None:
                    raise ValueError("generation_job_write_unconfirmed")
        queued.append(artifact_type)
    return queued


def _form_attachment_record_fields(
    cfg: Config,
    *,
    existing_fields: Mapping[str, Any],
    meeting_uid: str,
    meeting_name: str,
    date_text: str,
    meeting_series: str,
    meeting_type: str,
    data_version: int,
    source_url: str,
    source_name: str,
    review_url: str,
    review_name: str,
) -> dict[str, Any]:
    return {
        "会议ID": meeting_uid,
        "会议名": meeting_name,
        "会议日期": _form_date_millis(cfg, date_text),
        "会议系列": meeting_series,
        "会议类型": meeting_type,
        "数据版本": data_version,
        "会议纪要MD": form_url_field(source_url, source_name),
        "会议纪要审核前MD": form_url_field(review_url, review_name),
        "源纪要审核": _form_review_status(existing_fields, "源纪要审核"),
        "行业与市场观点审核": _form_review_status(existing_fields, "行业与市场观点审核"),
        "标的观点审核": _form_review_status(existing_fields, "标的观点审核"),
    }


def _form_fields_match(current: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    for name, value in expected.items():
        observed = current.get(name)
        if isinstance(value, Mapping):
            expected_link = str(value.get("link") or "")
            observed_link = str(observed.get("link") or "") if isinstance(observed, Mapping) else ""
            if expected_link and observed_link:
                if observed_link != expected_link:
                    return False
            elif plain_field_value(observed) != str(value.get("link") or value.get("text") or ""):
                return False
        elif plain_field_value(observed) != plain_field_value(value):
            return False
    return True


def update_bitable_record_reconciled_form(
    cfg: Config,
    record_id: str,
    record_fields: dict[str, Any],
    *,
    attachment_file_token: str,
) -> str:
    try:
        update_bitable_record(cfg, record_id, record_fields)
        return "committed"
    except FeishuApiError as update_error:
        fresh = get_bitable_record(cfg, record_id)
        fresh_fields = fresh.get("fields", {})
        fresh_attachment_tokens = _attachment_tokens_from_record(
            fresh, cfg.form_attachment_field
        )
        if (
            isinstance(fresh_fields, Mapping)
            and _form_fields_match(fresh_fields, record_fields)
            and fresh_attachment_tokens == {attachment_file_token}
        ):
            logging.info("Form ingress Base update reconciled after uncertain response")
            return "committed"
        raise update_error


def process_form_attachment_ingress(
    cfg: Config,
    record_id: str,
    *,
    event_time: str = "",
) -> dict[str, Any]:
    """Ingest exactly one native Base Markdown attachment for one record."""
    if not cfg.form_ingress_enabled:
        return {"status": "disabled"}
    if not record_id:
        raise ValueError("form_record_id_missing")
    with record_operation_lock(cfg, record_id):
        fields = list_bitable_fields(cfg)
        validate_form_ingress_schema(cfg, fields)
        record = get_bitable_record(cfg, record_id)
        record_fields = record.get("fields", {})
        if not isinstance(record_fields, Mapping):
            raise ValueError("form_record_fields_invalid")
        date_text, meeting_series, meeting_type = form_meeting_metadata_from_fields(cfg, record_fields)
        bound_attachment_items = _attachment_items_from_field(record_fields.get(cfg.form_attachment_field))
        if not bound_attachment_items:
            return {"status": "ignored", "reason": "form_attachment_missing"}
        if len(bound_attachment_items) != 1:
            raise ValueError("form_attachment_count_invalid")
        bound_attachment_token = _attachment_token(bound_attachment_items[0])
        if not bound_attachment_token:
            raise ValueError("form_attachment_token_missing")
        metadata_items = get_attachments(cfg, record_id, cfg.form_attachment_field)
        if not metadata_items:
            return {"status": "ignored", "reason": "form_attachment_missing"}
        metadata_items = [
            item for item in metadata_items if _attachment_token(item) == bound_attachment_token
        ]
        if len(metadata_items) != 1:
            raise ValueError("form_attachment_count_invalid")
        metadata = metadata_items[0]
        attachment_name = _attachment_name(metadata) or _attachment_name(bound_attachment_items[0])
        attachment_file_token = _attachment_token(metadata)
        if not attachment_name.lower().endswith(".md"):
            raise ValueError("form_attachment_not_markdown")
        if not attachment_file_token:
            raise ValueError("form_attachment_token_missing")
        parse_form_meeting_filename(attachment_name)
        meeting_name = meeting_name_from_filename(
            attachment_name, meeting_series, date_text
        )
        declared_size = metadata.get("size")
        if declared_size not in (None, ""):
            try:
                if int(declared_size) > cfg.form_attachment_max_bytes:
                    raise ValueError("form_attachment_too_large")
            except TypeError as exc:
                raise ValueError("form_attachment_size_invalid") from exc
            except ValueError as exc:
                if str(exc) == "form_attachment_too_large":
                    raise
                raise ValueError("form_attachment_size_invalid") from exc
        content = download_drive_media(
            cfg,
            attachment_file_token,
            extra=metadata.get("extra"),
        )
        if not content:
            raise ValueError("form_attachment_empty")
        if len(content) > cfg.form_attachment_max_bytes:
            raise ValueError("form_attachment_too_large")
        try:
            content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("form_attachment_not_utf8") from exc
        # Source registration intentionally does not run the meeting-minutes
        # body contract validator.  The upload/source-record boundary remains
        # format/hash-only; downstream generation/review owns that contract.
        content_sha256 = sha256_hex(content)
        receipt_path = form_ingestion_receipt_path(
            cfg, record_id, attachment_file_token, content_sha256
        )
        receipt = load_private_json(receipt_path)
        if receipt is not None:
            compare = {
                "record_id": record_id,
                "attachment_file_token": attachment_file_token,
                "content_sha256": content_sha256,
                "attachment_name": attachment_name,
                "meeting_date": date_text,
                "meeting_series": meeting_series,
                "meeting_type": meeting_type,
            }
            for key, value in compare.items():
                if str(receipt.get(key) or "") != str(value):
                    raise ValueError("form_ingestion_receipt_conflict")
            if receipt.get("status") == "jobs_queued":
                return {"status": "already_ingested", "record_id": record_id}
        else:
            existing_uid = plain_field_value(record_fields.get("会议ID")).strip().lower()
            if existing_uid and not MEETING_UID_PATTERN.fullmatch(existing_uid):
                raise ValueError("form_existing_meeting_uid_invalid")
            meeting_uid = existing_uid or f"mtg_{uuid.uuid4().hex}"
            try:
                current_version = int(float(plain_field_value(record_fields.get("数据版本"))))
            except (TypeError, ValueError):
                current_version = 0
            # A new receipt means a new content identity.  Existing records
            # therefore advance monotonically; repeated events for the same
            # token/hash return above from the terminal receipt instead of
            # reaching this branch.
            data_version = max(1, current_version + 1) if existing_uid else 1
            created_at = datetime.now(timezone.utc).isoformat()
            receipt = {
                "schema_version": 1,
                "status": "pending",
                "record_id": record_id,
                "attachment_file_token": attachment_file_token,
                "attachment_name": attachment_name,
                "content_sha256": content_sha256,
                "meeting_uid": meeting_uid,
                "meeting_name": meeting_name,
                "meeting_date": date_text,
                "meeting_series": meeting_series,
                "meeting_type": meeting_type,
                "data_version": data_version,
                "event_time": event_time,
                "created_at": created_at,
            }
            if not write_private_json_once(receipt_path, receipt):
                receipt = load_private_json(receipt_path)
                if receipt is None:
                    raise ValueError("form_ingestion_receipt_write_unconfirmed")
        if receipt is None:
            raise ValueError("form_ingestion_receipt_invalid")
        if cfg.dry_run:
            return {"status": "dry_run", "record_id": record_id}
        data_version = int(receipt["data_version"])
        status = str(receipt.get("status") or "pending")
        if status not in FORM_RECEIPT_STATES:
            raise ValueError("form_ingestion_receipt_invalid")
        normalized_name = str(
            receipt.get("normalized_file_name")
            or (
                f"{date_text} - {meeting_series} - {receipt['meeting_uid']}"
                f" - 会议纪要 - v{data_version}.md"
            )
        )
        source_token = str(receipt.get("source_file_token") or "")
        source_url = str(receipt.get("source_url") or "")
        review_token = str(receipt.get("review_file_token") or "")
        review_url = str(receipt.get("review_url") or "")
        if status == "pending":
            month_folder, review_folder = ensure_form_ingress_folders(
                cfg, date_text[:7], dry_run=False
            )
            if not month_folder or not review_folder:
                raise FeishuApiError("form_month_folder_unconfirmed")
            source_token, source_url = upload_version_artifact(cfg, month_folder, normalized_name, content)
            source_verify = download_drive_file_version(cfg, source_token)
            if sha256_hex(source_verify) != content_sha256:
                raise FeishuApiError("form_source_hash_mismatch")
            review_token, review_url = upload_version_artifact(cfg, review_folder, normalized_name, content)
            review_verify = download_drive_file_version(cfg, review_token)
            if sha256_hex(review_verify) != content_sha256:
                raise FeishuApiError("form_review_hash_mismatch")
            receipt.update(
                {
                    "status": "uploaded",
                    "source_file_token": source_token,
                    "source_url": source_url,
                    "review_file_token": review_token,
                    "review_url": review_url,
                    "normalized_file_name": normalized_name,
                    "baseline_file_name": normalized_name,
                }
            )
            write_private_json(receipt_path, receipt)

        if status in {"pending", "uploaded"}:
            if not source_token or not source_url or not review_token or not review_url:
                raise FeishuApiError("form_ingestion_receipt_upload_incomplete")
            # Fresh-read immediately before the Base commit. If the native form
            # changed while Drive work was in flight, leave the receipt pending
            # or uploaded and fail closed instead of overwriting newer content.
            fresh_record = get_bitable_record(cfg, record_id)
            fresh_items = _attachment_items_from_field(
                (fresh_record.get("fields", {}) if isinstance(fresh_record, Mapping) else {}).get(
                    cfg.form_attachment_field
                )
            )
            fresh_tokens = {_attachment_token(item) for item in fresh_items if _attachment_token(item)}
            if len(fresh_items) != 1 or fresh_tokens != {attachment_file_token}:
                raise ArchivePreconditionError("form_attachment_changed_before_commit")
            fresh_fields = fresh_record.get("fields", {})
            if not isinstance(fresh_fields, Mapping):
                raise ValueError("form_fresh_record_fields_invalid")
            record_fields_to_write = _form_attachment_record_fields(
                cfg,
                existing_fields=fresh_fields,
                meeting_uid=str(receipt["meeting_uid"]),
                meeting_name=meeting_name,
                date_text=date_text,
                meeting_series=meeting_series,
                meeting_type=meeting_type,
                data_version=data_version,
                source_url=source_url,
                source_name=normalized_name,
                review_url=review_url,
                review_name=normalized_name,
            )
            update_bitable_record_reconciled_form(
                cfg,
                record_id,
                record_fields_to_write,
                attachment_file_token=attachment_file_token,
            )
            receipt.update({"status": "committed", "record_fields": record_fields_to_write})
            write_private_json(receipt_path, receipt)
        queued = enqueue_form_generation_jobs(
            cfg,
            record_id=record_id,
            meeting_uid=str(receipt["meeting_uid"]),
            data_version=data_version,
            input_file_token=source_token,
            input_md_sha256=content_sha256,
            meeting_date=date_text,
            meeting_series=meeting_series,
            meeting_type=meeting_type,
            created_at=str(receipt["created_at"]),
        )
        receipt.update({"status": "jobs_queued", "generation_queued": queued})
        write_private_json(receipt_path, receipt)
        return {"status": "ingested", "record_id": record_id, "data_version": data_version, "generation_queued": queued}


# Stable descriptive aliases for integrations that call the ingress handler
# directly rather than going through ``process_bitable_record_changed_event``.
process_form_attachment_event = process_form_attachment_ingress
ingest_form_attachment = process_form_attachment_ingress


def process_file_created_event(cfg: Config, raw_event: dict[str, Any]) -> None:
    header, event = get_event_parts(raw_event)
    folder_token = str(event.get("folder_token", ""))
    if folder_token not in allowed_source_folder_tokens(cfg):
        logging.info("Ignore event from an unconfigured folder")
        return
    source_month = month_for_source_folder(cfg, folder_token)
    if source_month:
        logging.info("Event matched configured source month=%s", source_month)

    file_token = str(event.get("file_token", ""))
    file_type = str(event.get("file_type", ""))
    if not file_token or not file_type:
        raise ValueError("Event is missing file_token or file_type")

    if cfg.pipeline_mode == "unified":
        fields = list_bitable_fields(cfg)
        process_unified_file_created_event(
            cfg,
            raw_event,
            fields=fields,
            file_token=file_token,
            folder_token=folder_token,
        )
        return

    file_meta = get_file_meta(cfg, file_token, file_type)
    content: bytes | None = None
    if cfg.structured_metadata_enabled:
        if file_type != "file":
            raise ValueError("Structured metadata ingestion only supports Drive files")
        content = download_drive_file_version(cfg, file_token)
    baseline_month = source_month
    if not baseline_month:
        created_ms = ms_from_seconds_string(file_meta.get("create_time"))
        if created_ms is not None:
            baseline_month = month_from_ms(created_ms, cfg.archive_timezone_offset_hours)
    fields = list_bitable_fields(cfg)
    record_fields, report = build_record_fields(cfg, fields, file_meta, header, event)
    enrich_meeting_contract_record_fields(
        cfg,
        fields,
        record_fields=record_fields,
        report=report,
    )
    if cfg.structured_metadata_enabled:
        assert content is not None
        if is_v7_review_markdown(content):
            logging.info(
                "Skip schema-v7 review Markdown event; generation service owns structured Base writeback"
            )
            return
        enrich_structured_record_fields(
            cfg,
            fields,
            title=str(file_meta.get("title") or file_token),
            content=content,
            record_fields=record_fields,
            report=report,
        )

    require_reconcile_file_token_field(cfg, fields, record_fields, file_token)

    if not record_fields:
        raise ValueError("No writable Bitable fields matched; record was not created")

    seed = str(header.get("event_id") or f"{folder_token}:{file_token}:{header.get('create_time', '')}")
    client_token = deterministic_uuid4(seed)

    logging.info("Field mapping: %s", "; ".join(report) if report else "no report")
    logging.info("Prepared record field_count=%s", len(record_fields))

    if cfg.dry_run:
        logging.info("FEISHU_DRY_RUN=true; skip record creation")
        return

    record_id = create_bitable_record_reconciled(
        cfg,
        record_fields,
        client_token,
        file_token,
    )
    logging.info("Bitable record creation confirmed")
    if cfg.version_capture_enabled:
        try:
            baseline = capture_baseline_for_record_with_failure_status(
                cfg,
                record_id,
                month_override=baseline_month,
            )
            logging.info("Version baseline completed status=%s", baseline.get("status", "unknown"))
        except Exception as exc:
            error_code = safe_error_code(exc)
            logging.error("version_baseline_failed code=%s", error_code)
            if cfg.version_capture_enforce:
                raise


def process_bitable_record_changed_event(cfg: Config, raw_event: dict[str, Any]) -> None:
    header, event = get_event_parts(raw_event)
    file_token = str(event.get("file_token") or "")
    table_id = str(event.get("table_id") or "")
    if file_token != cfg.bitable_app_token or table_id != cfg.bitable_table_id:
        logging.info("Ignore Bitable record event for another configured resource")
        return

    action_list = event.get("action_list") or []
    if not isinstance(action_list, list):
        logging.info("Ignore bitable record event with invalid action_list type")
        return
    actions: list[dict[str, Any]] = []
    seen_record_ids: set[str] = set()
    for action in action_list:
        if not isinstance(action, dict):
            continue
        record_id = str(action.get("record_id") or "")
        if not record_id or record_id in seen_record_ids:
            continue
        seen_record_ids.add(record_id)
        actions.append(action)

    if cfg.pipeline_mode == "unified":
        if cfg.pipeline_event_not_before_ms:
            raw_event_time = str(header.get("create_time") or "").strip()
            try:
                event_time_ms = int(raw_event_time)
            except ValueError as exc:
                raise ValueError("unified_event_time_invalid") from exc
            if event_time_ms < cfg.pipeline_event_not_before_ms:
                logging.info("Ignore unified Base event older than cutover watermark")
                return
        field_schemas: list[dict[str, Any]] | None = None
        for action in actions:
            record_id = str(action.get("record_id") or "")
            if str(action.get("action") or "") == "record_deleted":
                logging.info("Ignore deleted Bitable record event")
                continue
            if cfg.form_ingress_enabled:
                process_form_attachment_ingress(
                    cfg,
                    record_id,
                    event_time=str(header.get("create_time") or ""),
                )
            if str(action.get("action") or "") != "record_edited":
                continue
            if field_schemas is None:
                field_schemas = list_bitable_fields(cfg)
            artifact_types = changed_unified_review_artifact_types(action, field_schemas)
            if not artifact_types:
                continue
            record = get_bitable_record(cfg, record_id)
            fields = record.get("fields", {})
            if not isinstance(fields, dict):
                raise ValueError("unified_review_record_fields_invalid")
            queued = enqueue_unified_review_jobs(
                cfg,
                record_id=record_id,
                fields=fields,
                event_time=str(header.get("create_time") or ""),
                artifact_types=artifact_types,
            )
            if queued:
                logging.info("Queued unified review job_count=%s", len(queued))
        return

    for action in actions:
        record_id = str(action.get("record_id") or "")

        record = get_bitable_record(cfg, record_id)
        fields = record.get("fields", {})
        if not isinstance(fields, dict):
            logging.warning("Skip record because record fields are invalid")
            continue
        if not checkbox_is_checked(fields.get(cfg.archive_review_field)):
            logging.info("Skip record because approval field is not checked")
            continue
        if value_is_empty(fields.get(cfg.archive_file_link_field)):
            logging.info("Skip record because the configured file-link field is empty")
            continue
        if not value_is_empty(fields.get(cfg.archive_link_field)):
            logging.info("Skip record because the configured archive-link field already exists")
            continue

        result = archive_record_with_failure_status(cfg, record_id)
        logging.info("Archive event completed status=%s", result.get("status", "unknown"))


def process_event(cfg: Config, raw_event: dict[str, Any]) -> None:
    header, _event = get_event_parts(raw_event)
    event_type = header.get("event_type") or raw_event.get("event_type")
    if event_type == FILE_CREATED_EVENT_TYPE:
        process_file_created_event(cfg, raw_event)
    elif event_type in {BITABLE_RECORD_CHANGED_EVENT_TYPE, BITABLE_RECORD_CHANGED_SUBSCRIBE_EVENT_TYPE}:
        process_bitable_record_changed_event(cfg, raw_event)
    else:
        logging.info("Ignore event_type=%s", event_type)


def process_event_with_retry(cfg: Config, raw_event: dict[str, Any]) -> None:
    transient_errors = (FeishuApiError, urllib.error.URLError, TimeoutError, ConnectionError)
    for attempt in range(1, ROUTER_EVENT_MAX_ATTEMPTS + 1):
        try:
            process_event(cfg, raw_event)
            return
        except transient_errors:
            if attempt >= ROUTER_EVENT_MAX_ATTEMPTS:
                raise
            logging.warning(
                "Retry event after transient failure attempt=%s/%s",
                attempt,
                ROUTER_EVENT_MAX_ATTEMPTS,
            )
            time.sleep(attempt)


@dataclass(frozen=True)
class SpoolItem:
    path: Path
    envelope: dict[str, Any]


class FileEventSpool:
    """Crash-recoverable local spool for sensitive Feishu callback payloads."""

    def __init__(self, root: str | Path):
        self.root = resolve_project_path(str(root))
        self.pending = self.root / "pending"
        self.processing = self.root / "processing"
        self.dead_letter = self.root / "dead-letter"
        for directory in (self.root, self.pending, self.processing, self.dead_letter):
            directory.mkdir(parents=True, exist_ok=True)
            os.chmod(directory, 0o700)

    @contextmanager
    def _transition_lock(self):
        lock_path = self.root / ".transition.lock"
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    @staticmethod
    def event_key(raw_event: Mapping[str, Any]) -> str:
        header = raw_event.get("header", {})
        event_id = ""
        if isinstance(header, Mapping):
            event_id = str(header.get("event_id") or "").strip()
        if not event_id:
            event_id = str(raw_event.get("event_id") or "").strip()
        material = (
            f"event:{event_id}".encode("utf-8")
            if event_id
            else json.dumps(raw_event, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        return hashlib.sha256(material).hexdigest()

    @staticmethod
    def _read_envelope(path: Path) -> dict[str, Any]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("event"), dict):
            raise ValueError("invalid_spool_envelope")
        return payload

    @staticmethod
    def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
        fd, temp_name = tempfile.mkstemp(prefix=".spool-", suffix=".tmp", dir=str(path.parent))
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_path, 0o600)
            durable_replace(temp_path, path)
        finally:
            if temp_path.exists():
                temp_path.unlink()

    def enqueue(self, raw_event: dict[str, Any]) -> str:
        key = self.event_key(raw_event)
        name = f"{key}.json"
        with self._transition_lock():
            if any((directory / name).exists() for directory in (self.pending, self.processing, self.dead_letter)):
                return key
            self._write_atomic(
                self.pending / name,
                {"schema_version": 1, "attempts": 0, "event": raw_event},
            )
        return key

    def recover_processing(self) -> int:
        recovered = 0
        with self._transition_lock():
            for source in sorted(self.processing.glob("*.json")):
                target = self.pending / source.name
                dead = self.dead_letter / source.name
                if target.exists() or dead.exists():
                    durable_unlink(source)
                else:
                    try:
                        envelope = self._read_envelope(source)
                    except Exception:
                        durable_replace(source, dead)
                    else:
                        if envelope.get("last_error_code") or int(envelope.get("attempts") or 0) > 0:
                            durable_replace(source, dead)
                        else:
                            durable_replace(source, target)
                recovered += 1
        return recovered

    def claim(self) -> SpoolItem | None:
        with self._transition_lock():
            for source in sorted(self.pending.glob("*.json")):
                target = self.processing / source.name
                durable_unlink(self.dead_letter / source.name)
                try:
                    durable_replace(source, target)
                except FileNotFoundError:
                    continue
                try:
                    envelope = self._read_envelope(target)
                except Exception:
                    durable_replace(target, self.dead_letter / target.name)
                    logging.error("spool_envelope_invalid")
                    continue
                return SpoolItem(path=target, envelope=envelope)
        return None

    def acknowledge(self, item: SpoolItem) -> None:
        with self._transition_lock():
            durable_unlink(item.path)

    def reject(self, item: SpoolItem, exc: BaseException) -> None:
        envelope = dict(item.envelope)
        envelope["attempts"] = int(envelope.get("attempts") or 0) + 1
        envelope["last_error_code"] = safe_error_code(exc)
        with self._transition_lock():
            self._write_atomic(item.path, envelope)
            durable_replace(item.path, self.dead_letter / item.path.name)

    def replay_dead_letters(self, key: str = "") -> int:
        if key and not re.fullmatch(r"[0-9a-f]{64}", key):
            raise ValueError("invalid_spool_key")
        sources = [self.dead_letter / f"{key}.json"] if key else sorted(self.dead_letter.glob("*.json"))
        replayed = 0
        with self._transition_lock():
            for source in sources:
                if not source.exists():
                    continue
                envelope = self._read_envelope(source)
                envelope["attempts"] = 0
                envelope.pop("last_error_code", None)
                target = self.pending / source.name
                if target.exists() or (self.processing / source.name).exists():
                    durable_unlink(source)
                    replayed += 1
                    continue
                self._write_atomic(source, envelope)
                durable_replace(source, target)
                replayed += 1
        return replayed


def spool_worker_loop(
    spool: FileEventSpool,
    processor: Callable[[dict[str, Any]], None],
    wake_event: threading.Event,
) -> None:
    while True:
        item = spool.claim()
        if item is None:
            wake_event.clear()
            wake_event.wait(timeout=5)
            continue
        try:
            processor(item.envelope["event"])
        except Exception as exc:
            spool.reject(item, exc)
            logging.error("event_moved_to_dead_letter code=%s", safe_error_code(exc))
        else:
            spool.acknowledge(item)


def start_spool_worker(
    spool: FileEventSpool,
    processor: Callable[[dict[str, Any]], None],
) -> threading.Event:
    recovered = spool.recover_processing()
    if recovered:
        logging.warning("Recovered interrupted event_count=%s", recovered)
    wake_event = threading.Event()
    worker = threading.Thread(
        target=spool_worker_loop,
        args=(spool, processor, wake_event),
        daemon=True,
    )
    worker.start()
    wake_event.set()
    return wake_event


def persist_callback_event(
    spool: FileEventSpool,
    wake_event: threading.Event,
    data: Any,
) -> None:
    try:
        spool.enqueue(event_to_dict(data))
    except Exception as exc:
        logging.error("event_spool_write_failed code=%s", safe_error_code(exc))
        raise
    wake_event.set()


def route_env_file_paths() -> list[Path]:
    load_dotenv()
    raw = os.environ.get("FEISHU_ROUTE_ENV_FILES", "").strip()
    if not raw:
        raise SystemExit("Missing FEISHU_ROUTE_ENV_FILES for router mode")
    return [resolve_project_path(item) for item in split_tokens(raw)]


def read_route_configs() -> list[Config]:
    configs = [read_config_from_env_file(path) for path in route_env_file_paths()]
    if not configs:
        raise SystemExit("FEISHU_ROUTE_ENV_FILES did not resolve to any route env files")
    assert_unique_source_folder_routes(configs)
    for index, cfg in enumerate(configs, start=1):
        logging.info(
            "Loaded route config route_index=%s source_folder_count=%s archive_root_configured=%s",
            index,
            len(allowed_source_folder_tokens(cfg)),
            bool(cfg.archive_root_folder_token),
        )
    return configs


def assert_unique_source_folder_routes(configs: list[Config]) -> None:
    owners: dict[str, str] = {}
    for cfg in configs:
        for folder_token in allowed_source_folder_tokens(cfg):
            owner = owners.get(folder_token)
            if owner and owner != cfg.bitable_table_id:
                raise SystemExit("A source folder is configured in multiple route env files")
            owners[folder_token] = cfg.bitable_table_id


def router_credentials(configs: list[Config]) -> tuple[str, str]:
    app_id = first_env("FEISHU_APP_ID", "LARK_APP_ID", default=configs[0].app_id)
    app_secret = first_env("FEISHU_APP_SECRET", "LARK_APP_SECRET", default=configs[0].app_secret)
    if not app_id or not app_secret:
        raise SystemExit("Missing FEISHU_APP_ID/FEISHU_APP_SECRET for router mode")
    for cfg in configs:
        if cfg.app_id != app_id:
            raise SystemExit("Router mode requires all route env files to use the same Feishu app_id")
        if cfg.app_secret != app_secret:
            raise SystemExit("Router mode requires all route env files to use the same Feishu app secret")
    return app_id, app_secret


def find_route_config(configs: list[Config], folder_token: str) -> Config | None:
    matches = [cfg for cfg in configs if folder_token in allowed_source_folder_tokens(cfg)]
    if len(matches) > 1:
        raise RuntimeError("Source folder token matched multiple route configs")
    return matches[0] if matches else None


def find_bitable_route_config(
    configs: list[Config],
    app_token: str,
    table_id: str,
) -> Config | None:
    """Resolve Base record events by the complete app+table binding."""
    matches = [
        cfg
        for cfg in configs
        if cfg.bitable_app_token == app_token and cfg.bitable_table_id == table_id
    ]
    if len(matches) > 1:
        raise RuntimeError("Bitable app/table matched multiple route configs")
    return matches[0] if matches else None


def process_router_event(configs: list[Config], raw_event: dict[str, Any]) -> None:
    header, event = get_event_parts(raw_event)
    event_type = header.get("event_type") or raw_event.get("event_type")
    if event_type in {BITABLE_RECORD_CHANGED_EVENT_TYPE, BITABLE_RECORD_CHANGED_SUBSCRIBE_EVENT_TYPE}:
        app_token = str(event.get("file_token") or event.get("app_token") or "")
        table_id = str(event.get("table_id") or "")
        cfg = find_bitable_route_config(configs, app_token, table_id)
        if cfg is None:
            logging.info("Router ignored Base record event for an unconfigured app/table")
            return
        logging.info("Router dispatched Base record event to a configured route")
        process_bitable_record_changed_event(cfg, raw_event)
        return
    if event_type != FILE_CREATED_EVENT_TYPE:
        logging.info("Router ignored event_type=%s", event_type)
        return

    folder_token = str(event.get("folder_token") or "")
    cfg = find_route_config(configs, folder_token)
    if cfg is None:
        logging.info("Router ignored event from an unconfigured folder")
        return
    logging.info("Router dispatched event to a configured route")
    process_file_created_event(cfg, raw_event)


def process_router_event_with_retry(configs: list[Config], raw_event: dict[str, Any]) -> None:
    transient_errors = (FeishuApiError, urllib.error.URLError, TimeoutError, ConnectionError)
    for attempt in range(1, ROUTER_EVENT_MAX_ATTEMPTS + 1):
        try:
            process_router_event(configs, raw_event)
            return
        except transient_errors as exc:
            if attempt >= ROUTER_EVENT_MAX_ATTEMPTS:
                raise
            logging.warning(
                "Retry router event after transient failure attempt=%s/%s error=%s",
                attempt,
                ROUTER_EVENT_MAX_ATTEMPTS,
                type(exc).__name__,
            )
            time.sleep(attempt)


def subscribe_router_source_folders(configs: list[Config]) -> None:
    subscribed_bases: set[tuple[str, str]] = set()
    for index, cfg in enumerate(configs, start=1):
        logging.info("Subscribe source folders for route_index=%s", index)
        subscribe_source_folders(cfg)
        binding = (cfg.bitable_app_token, cfg.bitable_table_id)
        if (cfg.pipeline_mode == "unified" or cfg.form_ingress_enabled) and binding not in subscribed_bases:
            subscribe_bitable_record_changes(cfg)
            subscribed_bases.add(binding)


def validate_live_field_bindings(configs: list[Config]) -> None:
    for index, cfg in enumerate(configs, start=1):
        fields = list_bitable_fields(cfg)
        binding_count = len(load_field_bindings(cfg))
        logging.info(
            "Validated route field bindings route_index=%s binding_count=%s field_count=%s",
            index,
            binding_count,
            len(fields),
        )


def router_spool_dir(configs: list[Config]) -> str:
    configured = {cfg.event_spool_dir for cfg in configs}
    if len(configured) != 1:
        raise SystemExit("Router route configs must use one shared FEISHU_EVENT_SPOOL_DIR")
    return next(iter(configured))


def run_event_router(configs: list[Config]) -> None:
    if lark is None:
        raise SystemExit("Missing dependency: install with `pip install -r requirements.txt`")

    app_id, app_secret = router_credentials(configs)
    validate_live_field_bindings(configs)
    spool = FileEventSpool(router_spool_dir(configs))
    wake_event = start_spool_worker(
        spool,
        lambda raw_event: process_router_event_with_retry(configs, raw_event),
    )

    def on_created_in_folder(data: Any) -> None:
        persist_callback_event(spool, wake_event, data)

    def on_bitable_record_changed(data: Any) -> None:
        persist_callback_event(spool, wake_event, data)

    event_handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_drive_file_created_in_folder_v1(on_created_in_folder)
        .register_p2_drive_file_bitable_record_changed_v1(on_bitable_record_changed)
        .build()
    )
    client = lark.ws.Client(
        app_id,
        app_secret,
        event_handler=event_handler,
        # The SDK's INFO connection message includes short-lived access_key and
        # ticket query parameters. Keep those out of persisted service logs;
        # explicit DEBUG mode remains available for short interactive diagnosis.
        log_level=lark.LogLevel.DEBUG if logging.getLogger().level <= logging.DEBUG else lark.LogLevel.WARNING,
    )
    logging.info(
        "Starting Feishu event router. route_count=%s source_folder_count=%s",
        len(configs),
        sum(len(allowed_source_folder_tokens(cfg)) for cfg in configs),
    )
    client.start()


def make_archive_handler(cfg: Config) -> type[BaseHTTPRequestHandler]:
    class ArchiveHandler(BaseHTTPRequestHandler):
        server_version = "FeishuArchiveHTTP/1.0"

        def log_message(self, format: str, *args: Any) -> None:
            return

        def write_json(self, status_code: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if urllib.parse.urlparse(self.path).path == "/healthz":
                ready = archive_http_ready(cfg)
                self.write_json(200 if ready else 503, {"ok": ready, "ready": ready})
                return
            self.write_json(404, {"error": "not_found"})

        def do_POST(self) -> None:
            path = urllib.parse.urlparse(self.path).path
            if path not in {"/archive", "/capture-baseline"}:
                self.write_json(404, {"error": "not_found"})
                return
            if cfg.archive_http_token:
                token_values = self.headers.get_all("X-Archive-Token", [])
                if len(token_values) != 1 or not secrets.compare_digest(token_values[0], cfg.archive_http_token):
                    self.write_json(401, {"error": "unauthorized"})
                    return
            elif not cfg.archive_allow_no_token:
                self.write_json(500, {"error": "archive_http_token_not_configured"})
                return

            try:
                if self.headers.get_all("Transfer-Encoding", []):
                    self.write_json(400, {"error": "transfer_encoding_not_supported"})
                    return
                type_values = self.headers.get_all("Content-Type", [])
                if len(type_values) != 1 or type_values[0].split(";", 1)[0].strip().lower() != "application/json":
                    self.write_json(415, {"error": "content_type_must_be_json"})
                    return
                length_values = self.headers.get_all("Content-Length", [])
                if not length_values:
                    self.write_json(411, {"error": "content_length_required"})
                    return
                if len(length_values) != 1:
                    self.write_json(400, {"error": "ambiguous_content_length"})
                    return
                length = int(length_values[0])
                if length <= 0:
                    self.write_json(400, {"error": "empty_body"})
                    return
                if length > cfg.archive_max_body_bytes:
                    self.write_json(413, {"error": "request_too_large"})
                    return
                body = self.rfile.read(length)
                if len(body) != length:
                    raise ValueError("short_read")
                payload = json.loads(body.decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    self.write_json(400, {"error": "json_object_required"})
                    return
                record_id = str(payload.get("record_id") or payload.get("recordId") or "").strip()
                if not re.fullmatch(r"[A-Za-z0-9_-]{4,100}", record_id):
                    self.write_json(400, {"error": "invalid_record_id"})
                    return
                if path == "/capture-baseline":
                    result = capture_baseline_for_record_with_failure_status(cfg, record_id)
                else:
                    result = archive_record_with_failure_status(cfg, record_id)
                self.write_json(200, result)
            except ArchivePreconditionError as exc:
                self.write_json(409, {"error": exc.code})
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                self.write_json(400, {"error": "invalid_request"})
            except FeishuApiError as exc:
                logging.error("archive_http_failed code=%s", safe_error_code(exc))
                self.write_json(502, {"error": "upstream_failure"})
            except Exception as exc:
                logging.error("archive_http_failed code=%s", safe_error_code(exc))
                self.write_json(500, {"error": "internal_error"})

    return ArchiveHandler


def archive_http_ready(cfg: Config) -> bool:
    auth_ready = bool(cfg.archive_http_token or cfg.archive_allow_no_token)
    version_ready = cfg.archive_dry_run or (
        cfg.version_capture_enabled
        and cfg.version_capture_enforce
        and version_settings_ready(cfg)
    )
    return auth_ready and version_ready


def serve_archive_http(cfg: Config) -> None:
    if not archive_http_ready(cfg):
        raise SystemExit("Archive HTTP configuration is not ready")
    server = ThreadingHTTPServer((cfg.archive_http_host, cfg.archive_http_port), make_archive_handler(cfg))
    logging.info(
        "Starting archive HTTP server on %s:%s archive_dry_run=%s",
        cfg.archive_http_host,
        cfg.archive_http_port,
        cfg.archive_dry_run,
    )
    server.serve_forever()


def start_archive_http_thread_if_enabled(cfg: Config) -> None:
    if not cfg.archive_http_enabled:
        return
    thread = threading.Thread(target=serve_archive_http, args=(cfg,), daemon=True)
    thread.start()


def run_long_connection(cfg: Config) -> None:
    if lark is None:
        raise SystemExit("Missing dependency: install with `pip install -r requirements.txt`")

    start_archive_http_thread_if_enabled(cfg)
    spool = FileEventSpool(cfg.event_spool_dir)
    wake_event = start_spool_worker(
        spool,
        lambda raw_event: process_event_with_retry(cfg, raw_event),
    )

    def on_created_in_folder(data: Any) -> None:
        persist_callback_event(spool, wake_event, data)

    def on_bitable_record_changed(data: Any) -> None:
        persist_callback_event(spool, wake_event, data)

    event_handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_drive_file_created_in_folder_v1(on_created_in_folder)
        .register_p2_drive_file_bitable_record_changed_v1(on_bitable_record_changed)
        .build()
    )
    client = lark.ws.Client(
        cfg.app_id,
        cfg.app_secret,
        event_handler=event_handler,
        # The SDK's INFO connection message includes short-lived access_key and
        # ticket query parameters. Keep those out of persisted service logs;
        # explicit DEBUG mode remains available for short interactive diagnosis.
        log_level=lark.LogLevel.DEBUG if logging.getLogger().level <= logging.DEBUG else lark.LogLevel.WARNING,
    )
    logging.info(
        "Starting Feishu long connection. dry_run=%s source_folder_count=%s archive_http_enabled=%s",
        cfg.dry_run,
        len(allowed_source_folder_tokens(cfg)),
        cfg.archive_http_enabled,
    )
    client.start()


def print_fields(cfg: Config) -> None:
    app_meta = get_bitable_app_meta(cfg)
    fields = list_bitable_fields(cfg)
    print(json.dumps({"app": app_meta, "fields": fields}, ensure_ascii=False, indent=2))


def validate_month(value: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}", value):
        raise SystemExit("month must use YYYY-MM format, for example 2032-08")
    try:
        datetime.strptime(value, "%Y-%m")
    except ValueError as exc:
        raise SystemExit("month must be a valid YYYY-MM value") from exc
    return value


def ensure_month_folders(cfg: Config, month: str, subscribe: bool, dry_run: bool) -> dict[str, Any]:
    month = validate_month(month)
    with folder_registry_lock(cfg, exclusive=not dry_run):
        return _ensure_month_folders_locked(cfg, month, subscribe, dry_run)


def _ensure_month_folders_locked(cfg: Config, month: str, subscribe: bool, dry_run: bool) -> dict[str, Any]:
    registry = load_folder_registry(cfg)
    months = registry.setdefault("months", {})
    entry = months.setdefault(month, {})
    if not isinstance(entry, dict):
        raise SystemExit(f"Folder registry entry for {month} must be a JSON object")

    source_token = str(entry.get("source_folder_token") or "")
    archive_token = str(entry.get("archive_folder_token") or "")

    if not source_token:
        source_token = ensure_child_folder(cfg, cfg.folder_token, month, dry_run=dry_run)
        if source_token:
            entry["source_folder_token"] = source_token
    if not archive_token:
        archive_token = ensure_child_folder(cfg, cfg.archive_root_folder_token, month, dry_run=dry_run)
        if archive_token:
            entry["archive_folder_token"] = archive_token

    entry["source_parent_folder_token"] = cfg.folder_token
    entry["archive_parent_folder_token"] = cfg.archive_root_folder_token
    entry["updated_at"] = int(time.time() * 1000)

    if not dry_run:
        save_folder_registry(cfg, registry)
        if subscribe and source_token:
            subscribe_folder(cfg, source_token)

    return {
        "month": month,
        "dry_run": dry_run,
        "source_parent_folder_token": cfg.folder_token,
        "source_folder_token": source_token,
        "archive_parent_folder_token": cfg.archive_root_folder_token,
        "archive_folder_token": archive_token,
        "registry_path": str(resolve_project_path(cfg.folder_registry_path)),
        "subscribed_source_folder": bool(subscribe and source_token and not dry_run),
    }


def env_status() -> dict[str, str]:
    load_dotenv()
    defaults = {
        "FEISHU_SOURCE_FOLDER_TOKENS": "",
        "FEISHU_FOLDER_REGISTRY_PATH": "data/folder_registry.json",
        "FEISHU_USER_ID_TYPE": "open_id",
        "FEISHU_DRY_RUN": "true",
        "FEISHU_ARCHIVE_HTTP_ENABLED": "false",
        "FEISHU_ARCHIVE_DRY_RUN": "true",
        "FEISHU_ARCHIVE_HTTP_HOST": "127.0.0.1",
        "FEISHU_EVENT_SPOOL_DIR": "data/event-spool",
        "FEISHU_FIELD_BINDINGS_PATH": "",
        "FEISHU_PIPELINE_MODE": "legacy",
        "FEISHU_UNREGISTERED_FILE_SPOOL_DIR": "data/unregistered-files",
        "FEISHU_PIPELINE_REVIEW_JOB_SPOOL_DIR": "data/pipeline-review-jobs",
        "FEISHU_PIPELINE_WORKER_RECEIPT_DIR": "data/meeting-pipeline-receipts",
        "FEISHU_PIPELINE_EVENT_NOT_BEFORE_MS": "0",
        "FEISHU_FORM_INGRESS_ENABLED": "false",
        "FEISHU_FORM_ATTACHMENT_FIELD": "会议纪要上传附件",
        "FEISHU_FORM_MAX_ATTACHMENT_BYTES": str(10 * 1024 * 1024),
        "FEISHU_GENERATION_JOB_SPOOL_DIR": "data/meeting-generation-jobs",
        "FEISHU_FORM_INGESTION_RECEIPT_DIR": "data/meeting-ingestion-receipts",
        "FEISHU_ARCHIVE_MAX_BODY_BYTES": "16384",
        "FEISHU_VERSION_CAPTURE_ENABLED": "true",
        "FEISHU_VERSION_CAPTURE_ENFORCE": "true",
    }
    required = [
        "FEISHU_APP_ID",
        "FEISHU_APP_SECRET",
        "FEISHU_FOLDER_TOKEN",
        "FEISHU_ARCHIVE_ROOT_FOLDER_TOKEN",
        "FEISHU_BITABLE_APP_TOKEN",
        "FEISHU_BITABLE_TABLE_ID",
    ]
    keys = [*required, "FEISHU_ARCHIVE_HTTP_TOKEN", *defaults.keys()]
    status: dict[str, str] = {}
    for key in keys:
        if os.environ.get(key):
            status[key] = "set"
        elif key == "FEISHU_APP_ID" and os.environ.get("LARK_APP_ID"):
            status[key] = "set via LARK_APP_ID"
        elif key == "FEISHU_APP_SECRET" and os.environ.get("LARK_APP_SECRET"):
            status[key] = "set via LARK_APP_SECRET"
        elif key in defaults:
            status[key] = "default"
        else:
            status[key] = "missing"
    return status


def doctor(online: bool = False) -> int:
    required = [
        "FEISHU_APP_ID",
        "FEISHU_APP_SECRET",
        "FEISHU_FOLDER_TOKEN",
        "FEISHU_ARCHIVE_ROOT_FOLDER_TOKEN",
        "FEISHU_BITABLE_APP_TOKEN",
        "FEISHU_BITABLE_TABLE_ID",
    ]
    status: dict[str, Any] = {
        "python": sys.version.split()[0],
        "official_sdk_lark_oapi": "set" if lark is not None else "missing",
        "external_cli": {
            "feishu": "set" if shutil.which("feishu") else "missing",
            "lark": "set" if shutil.which("lark") else "missing",
            "lark-cli": "set" if shutil.which("lark-cli") else "missing",
        },
        "env": env_status(),
    }

    status["ready_for_online_calls"] = all(
        status["env"][key].startswith("set")
        for key in required
    )

    if online and status["ready_for_online_calls"]:
        cfg = read_config()
        try:
            app_meta = get_bitable_app_meta(cfg)
            fields = list_bitable_fields(cfg)
            status["online"] = {
                "tenant_token": "ok",
                "base_name": app_meta.get("name"),
                "base_is_advanced": app_meta.get("is_advanced"),
                "field_count": len(fields),
            }
        except FeishuApiError as exc:
            status["online"] = {
                "tenant_token": "failed",
                "error": safe_error_code(exc),
            }
    elif online:
        status["online"] = "skipped: missing required environment variables"

    print(json.dumps(status, ensure_ascii=False, indent=2))
    if online:
        online_status = status.get("online")
        if not isinstance(online_status, dict) or online_status.get("tenant_token") != "ok":
            return 1
    return 0


def init_config(output_name: str = ".env.local.example") -> None:
    service_dir = Path(__file__).resolve().parent
    env_path = (service_dir / output_name).resolve()
    if env_path.parent != service_dir or not env_path.name.endswith(".example"):
        raise SystemExit("init-config output must be a new *.example file in the service directory")
    if env_path.exists():
        raise SystemExit(f"{env_path} already exists; init-config never overwrites configuration files")

    content = f"""# Feishu enterprise self-built app credentials.
# Fill these two values locally. Do not commit or share this file.
FEISHU_APP_ID=
FEISHU_APP_SECRET=

# Required resource identifiers. Fill them locally; no production identifiers
# are embedded in this repository template.
FEISHU_FOLDER_TOKEN=
FEISHU_SOURCE_FOLDER_TOKENS=
FEISHU_ARCHIVE_ROOT_FOLDER_TOKEN=
FEISHU_FOLDER_REGISTRY_PATH=data/folder_registry.json
FEISHU_BITABLE_APP_TOKEN=
FEISHU_BITABLE_TABLE_ID=
# Required for unified mode. This manifest binds Router logical field keys to
# stable Base field IDs; live field names may change without affecting routing.
FEISHU_FIELD_BINDINGS_PATH=

FEISHU_USER_ID_TYPE=open_id
FEISHU_DRY_RUN=true
FEISHU_LOG_LEVEL=INFO

# Archive workflow endpoint. Keep dry-run true until a single-record test is approved.
FEISHU_ARCHIVE_HTTP_ENABLED=false
FEISHU_ARCHIVE_HTTP_HOST=127.0.0.1
FEISHU_ARCHIVE_HTTP_PORT=8787
FEISHU_ARCHIVE_HTTP_TOKEN=
FEISHU_ARCHIVE_DRY_RUN=true
FEISHU_ARCHIVE_REVIEW_FIELD=已审核
FEISHU_ARCHIVE_ORIGINAL_TIME_FIELD=原始记录时间
FEISHU_ARCHIVE_MAX_BODY_BYTES=16384
FEISHU_EVENT_SPOOL_DIR=data/event-spool

# Review before/after version retention. These may also be supplied by
# data/version_retention.json keyed by table ID.
FEISHU_VERSION_CONFIG_PATH=data/version_retention.json
FEISHU_VERSION_CAPTURE_ENABLED=true
FEISHU_VERSION_CAPTURE_ENFORCE=true
FEISHU_VERSION_ROOT_FOLDER_TOKEN=
FEISHU_VERSION_CATEGORY=

# Native Base form attachment ingress (unified pipeline only). The value below
# is a logical key present in the field-binding manifest, not a live field name.
FEISHU_FORM_INGRESS_ENABLED=false
FEISHU_FORM_ATTACHMENT_FIELD={FIELD_FORM_ATTACHMENT}
FEISHU_FORM_MAX_ATTACHMENT_BYTES=10485760
FEISHU_GENERATION_JOB_SPOOL_DIR=data/meeting-generation-jobs
FEISHU_FORM_INGESTION_RECEIPT_DIR=data/meeting-ingestion-receipts
FEISHU_PIPELINE_EVENT_NOT_BEFORE_MS=0

# Optional field aliases.
# FEISHU_FIELD_ALIASES_JSON={{"file_link":["文档链接"],"upload_time":["创建日期"]}}
"""
    env_path.write_text(content, encoding="utf-8")
    print(f"Created placeholder config: {env_path}")


def execute_plan(
    cfg: Config,
    *,
    listen: bool = False,
    subscribe: bool = False,
) -> None:
    if subscribe:
        logging.info("Step 1/3: subscribe source folder events")
        subscribe_configured_events(cfg)
    else:
        logging.info("Step 1/3: skip event subscription by request")

    logging.info("Step 2/3: read Base metadata and table fields")
    app_meta = get_bitable_app_meta(cfg)
    fields = list_bitable_fields(cfg)
    logging.info(
        "Base=%s advanced=%s field_count=%s",
        app_meta.get("name"),
        app_meta.get("is_advanced"),
        len(fields),
    )
    print(json.dumps({"app": app_meta, "fields": fields}, ensure_ascii=False, indent=2))

    if listen:
        logging.info("Step 3/3: start long-connection listener")
        run_long_connection(cfg)
    else:
        logging.info("Step 3/3: listener not started. Use `run` or `execute-plan --listen` after dry-run checks.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Feishu Drive folder to Bitable sync")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run", help="Start long-connection event listener")
    run_parser.add_argument("--apply", action="store_true", help="Required because events can write external records")
    run_router_parser = subparsers.add_parser("run-router", help="Start one long-connection event router for multiple route env files")
    run_router_parser.add_argument("--apply", action="store_true", help="Required because events can write external records")
    subscribe_parser = subparsers.add_parser("subscribe", help="Subscribe configured source folder created-in-folder events")
    subscribe_parser.add_argument("--apply", action="store_true", help="Required to write the external subscription")
    subscribe_router_parser = subparsers.add_parser("subscribe-router", help="Subscribe source folders from all route env files")
    subscribe_router_parser.add_argument("--apply", action="store_true", help="Required to write external subscriptions")
    subparsers.add_parser("fields", help="Print Base metadata and table fields")
    serve_archive_parser = subparsers.add_parser("serve-archive", help="Start archive HTTP endpoint only")
    serve_archive_parser.add_argument("--apply", action="store_true", help="Required because requests can archive external records")
    archive_parser = subparsers.add_parser("archive-record", help="Archive one Bitable record by record_id")
    archive_parser.add_argument("record_id", help="Bitable record_id")
    archive_parser.add_argument("--apply", action="store_true", help="Required to archive the external record")
    baseline_parser = subparsers.add_parser("capture-baseline-record", help="Capture the first valid version for one record")
    baseline_parser.add_argument("record_id", help="Bitable record_id")
    baseline_parser.add_argument("--apply", action="store_true", help="Required to write version evidence")
    migrate_parser = subparsers.add_parser(
        "migrate-archived-record",
        help="Backfill version audit fields for one already archived record",
    )
    migrate_parser.add_argument("record_id", help="Bitable record_id")
    migrate_parser.add_argument("--apply", action="store_true", help="Required to write migration evidence")
    ensure_month_parser = subparsers.add_parser("ensure-month", help="Ensure source/archive YYYY-MM folders exist")
    ensure_month_parser.add_argument("month", help="Month in YYYY-MM format")
    ensure_month_parser.add_argument("--subscribe", action="store_true", help="Subscribe the source month folder after creation")
    ensure_month_parser.add_argument(
        "--apply",
        action="store_true",
        help="Create folders, write registry, and subscribe. Without this flag the command is a dry run.",
    )
    doctor_parser = subparsers.add_parser("doctor", help="Check local runtime, dependency, CLI, and config status")
    doctor_parser.add_argument("--online", action="store_true", help="Also call Feishu APIs when credentials are configured")
    init_parser = subparsers.add_parser("init-config", help="Create a local .env placeholder for this task")
    init_parser.add_argument("--output", default=".env.local.example", help="New *.example file name; existing files are never overwritten")
    execute_parser = subparsers.add_parser("execute-plan", help="Run setup validation steps, optionally then listen")
    execute_parser.add_argument("--listen", action="store_true", help="Start long-connection listener after setup checks")
    execute_parser.add_argument("--subscribe", action="store_true", help="Also create external event subscriptions")
    execute_parser.add_argument("--apply", action="store_true", help="Required with --subscribe or --listen")
    replay_parser = subparsers.add_parser("replay-spool", help="Move local dead-letter events back to pending")
    replay_parser.add_argument("--key", default="", help="Optional 64-character local spool key")
    replay_parser.add_argument("--apply", action="store_true", help="Required to change local spool state")
    replay_router_parser = subparsers.add_parser(
        "replay-router-spool",
        help="Move router dead-letter events back to the shared pending spool",
    )
    replay_router_parser.add_argument("--key", default="", help="Optional 64-character local spool key")
    replay_router_parser.add_argument("--apply", action="store_true", help="Required to change local spool state")
    return parser.parse_args()


def main() -> int:
    configure_logging()
    args = parse_args()
    command = args.command

    if command == "doctor":
        return doctor(online=args.online)
    if command == "init-config":
        init_config(output_name=args.output)
        return 0
    commands_requiring_apply = {
        "run",
        "run-router",
        "subscribe",
        "subscribe-router",
        "serve-archive",
        "archive-record",
        "capture-baseline-record",
        "migrate-archived-record",
        "replay-spool",
        "replay-router-spool",
    }
    if command in commands_requiring_apply and not getattr(args, "apply", False):
        raise SystemExit(f"{command} requires explicit --apply")
    if command == "execute-plan" and (args.subscribe or args.listen) and not args.apply:
        raise SystemExit("execute-plan --subscribe/--listen requires explicit --apply")
    if command in {"run-router", "subscribe-router", "replay-router-spool"}:
        configs = read_route_configs()
        try:
            if command == "run-router":
                run_event_router(configs)
            elif command == "replay-router-spool":
                count = FileEventSpool(router_spool_dir(configs)).replay_dead_letters(args.key)
                print(json.dumps({"replayed": count}, ensure_ascii=False))
            else:
                subscribe_router_source_folders(configs)
        except FeishuApiError as exc:
            logging.error("feishu_api_failed code=%s", safe_error_code(exc))
            return 1
        return 0

    cfg = read_config()

    try:
        if command == "subscribe":
            subscribe_configured_events(cfg)
        elif command == "fields":
            print_fields(cfg)
        elif command == "run":
            run_long_connection(cfg)
        elif command == "serve-archive":
            serve_archive_http(cfg)
        elif command == "replay-spool":
            count = FileEventSpool(cfg.event_spool_dir).replay_dead_letters(args.key)
            print(json.dumps({"replayed": count}, ensure_ascii=False))
        elif command == "archive-record":
            print(
                json.dumps(
                    archive_record_with_failure_status(cfg, args.record_id),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        elif command == "capture-baseline-record":
            print(
                json.dumps(
                    capture_baseline_for_record_with_failure_status(cfg, args.record_id),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        elif command == "migrate-archived-record":
            print(
                json.dumps(
                    migrate_archived_record_with_failure_status(cfg, args.record_id),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        elif command == "ensure-month":
            dry_run = not args.apply
            print(
                json.dumps(
                    ensure_month_folders(cfg, args.month, subscribe=args.subscribe, dry_run=dry_run),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        elif command == "execute-plan":
            execute_plan(cfg, listen=args.listen, subscribe=args.subscribe)
        else:
            raise SystemExit(f"Unknown command: {command}")
    except FeishuApiError as exc:
        logging.error("feishu_api_failed code=%s", safe_error_code(exc))
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nStopped by user.", file=sys.stderr)
        raise SystemExit(130)
