#!/usr/bin/env python3
from __future__ import annotations

import argparse
from contextlib import contextmanager
import datetime as dt
import email.message
import email.parser
import email.policy
import fcntl
import hashlib
import hmac
import http.server
import importlib.util
import json
import logging
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import Any
import urllib.error
import urllib.parse
import urllib.request
import uuid


SERVICE_NAME = "feishu-minutes-upload"
DEFAULT_PARENT_FOLDER_TOKEN = ""
DEFAULT_USERS_PATH = "data/upload_users.json"
DEFAULT_MAX_UPLOAD_BYTES = 10 * 1024 * 1024
DEFAULT_FILE_URL_BASE = "https://feishu.cn/file/"
FEISHU_OPENAPI_BASE = "https://open.feishu.cn/open-apis"
IDEMPOTENCY_KEY_RE = re.compile(r"[A-Za-z0-9._:-]{16,128}")
IDEMPOTENCY_RECORD_VERSION = 1
INGESTION_RECEIPT_VERSION = 1
MEETING_REGISTRY_VERSION = 1
DEFAULT_PIPELINE_CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / ".implementation"
    / "meeting-pipeline-contract"
    / "meeting_pipeline_contract.py"
)
GENERATION_ARTIFACT_TYPES = (
    "industry_market_viewpoints",
    "structured_viewpoints",
)


class UploadError(Exception):
    def __init__(self, error_code: str, message: str, http_status: int = 400):
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.http_status = http_status


class AtomicCommitUncertain(OSError):
    """Raised when replacement happened but directory durability is unconfirmed."""


@dataclass
class Config:
    app_id: str
    app_secret: str
    parent_folder_token: str
    user_db_path: Path
    max_upload_bytes: int
    file_url_base: str
    openapi_base: str = FEISHU_OPENAPI_BASE
    meeting_contract_enabled: bool = True
    meeting_contract_validator: str = "/skills/investment-meeting-minutes/scripts/validate_meeting_minutes_contract.py"
    meeting_contract_validator_sha256: str = ""
    output_owner_open_id: str = ""
    pipeline_contract_path: Path = DEFAULT_PIPELINE_CONTRACT_PATH
    baseline_parent_folder_token: str = ""
    meeting_base_app_token: str = ""
    meeting_base_table_id: str = ""
    generation_job_spool_path: Path = Path("data/meeting-generation-jobs")
    meeting_registry_path: Path = Path("data/meeting-registry")

    @classmethod
    def from_env(cls, users_path_override: str | None = None) -> "Config":
        load_env_files()
        users_path = users_path_override or os.environ.get("FEISHU_UPLOAD_USERS_PATH", DEFAULT_USERS_PATH)
        max_upload_bytes = parse_int_env("FEISHU_MAX_UPLOAD_BYTES", DEFAULT_MAX_UPLOAD_BYTES)
        return cls(
            app_id=os.environ.get("FEISHU_APP_ID", ""),
            app_secret=os.environ.get("FEISHU_APP_SECRET", ""),
            parent_folder_token=os.environ.get("FEISHU_PARENT_FOLDER_TOKEN", DEFAULT_PARENT_FOLDER_TOKEN),
            user_db_path=resolve_path(users_path),
            max_upload_bytes=max_upload_bytes,
            file_url_base=os.environ.get("FEISHU_FILE_URL_BASE", DEFAULT_FILE_URL_BASE),
            openapi_base=os.environ.get("FEISHU_OPENAPI_BASE", FEISHU_OPENAPI_BASE).rstrip("/"),
            meeting_contract_enabled=parse_bool_env("FEISHU_MEETING_CONTRACT_ENABLED", True),
            meeting_contract_validator=os.environ.get(
                "FEISHU_MEETING_CONTRACT_VALIDATOR",
                "/skills/investment-meeting-minutes/scripts/validate_meeting_minutes_contract.py",
            ).strip(),
            meeting_contract_validator_sha256=os.environ.get(
                "FEISHU_MEETING_CONTRACT_VALIDATOR_SHA256",
                "",
            ).strip().lower(),
            output_owner_open_id=os.environ.get("FEISHU_OUTPUT_OWNER_OPEN_ID", "").strip(),
            pipeline_contract_path=resolve_path(
                os.environ.get("FEISHU_PIPELINE_CONTRACT_PATH", str(DEFAULT_PIPELINE_CONTRACT_PATH))
            ),
            baseline_parent_folder_token=os.environ.get(
                "FEISHU_BASELINE_PARENT_FOLDER_TOKEN", ""
            ).strip(),
            meeting_base_app_token=os.environ.get(
                "FEISHU_MEETING_BASE_APP_TOKEN", ""
            ).strip(),
            meeting_base_table_id=os.environ.get(
                "FEISHU_MEETING_BASE_TABLE_ID", ""
            ).strip(),
            generation_job_spool_path=resolve_path(
                os.environ.get(
                    "FEISHU_GENERATION_JOB_SPOOL_PATH", "data/meeting-generation-jobs"
                )
            ),
            meeting_registry_path=resolve_path(
                os.environ.get("FEISHU_MEETING_REGISTRY_PATH", "data/meeting-registry")
            ),
        )


@dataclass
class UploadedFile:
    field_name: str
    file_name: str
    content_type: str
    data: bytes


@dataclass(frozen=True)
class IngestionMetadata:
    meeting_uid: str
    meeting_date: str
    meeting_series: str
    meeting_type: str
    data_version: int
    normalized_file_name: str
    month: str


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_pipeline_contract(path: Path):
    resolved = path.expanduser().resolve()
    if resolved.is_symlink() or not resolved.is_file():
        raise UploadError(
            "pipeline_contract_invalid",
            "Meeting pipeline contract is not configured.",
            500,
        )
    spec = importlib.util.spec_from_file_location(
        "meeting_pipeline_contract_for_upload", resolved
    )
    if spec is None or spec.loader is None:
        raise UploadError("pipeline_contract_invalid", "Meeting pipeline contract cannot be loaded.", 500)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        module.validate_contract_assets()
    except Exception as exc:
        raise UploadError(
            "pipeline_contract_invalid",
            "Meeting pipeline contract validation failed.",
            500,
        ) from exc
    if module.CONTRACT.contract_version != 1:
        raise UploadError("pipeline_contract_invalid", "Unsupported meeting pipeline contract.", 500)
    return module


def request_metadata_fields(
    fields: dict[str, str],
    query: dict[str, list[str]],
) -> dict[str, str]:
    allowed = {
        "meeting_date",
        "meeting_series",
        "meeting_type",
        "meeting_uid",
        "dry_run",
    }
    unknown = sorted(set(fields) - allowed)
    if unknown:
        raise UploadError(
            "unexpected_field",
            f"Unsupported multipart fields: {', '.join(unknown)}.",
        )
    unknown_query = sorted(set(query) - allowed)
    if unknown_query:
        raise UploadError(
            "unexpected_query_parameter",
            f"Unsupported query parameters: {', '.join(unknown_query)}.",
        )
    result: dict[str, str] = {}
    for name in ("meeting_date", "meeting_series", "meeting_type", "meeting_uid"):
        query_values = query.get(name, [])
        if len(query_values) > 1:
            raise UploadError(
                "ambiguous_meeting_metadata",
                f"Query parameter {name} must appear at most once.",
            )
        query_value = str(query_values[0]) if query_values else ""
        field_value = str(fields.get(name) or "")
        if query_value and field_value and query_value != field_value:
            raise UploadError(
                "ambiguous_meeting_metadata",
                f"Conflicting query and multipart values for {name}.",
            )
        result[name] = query_value or field_value
    dry_query_values = query.get("dry_run", [])
    if len(dry_query_values) > 1:
        raise UploadError(
            "ambiguous_meeting_metadata",
            "Query parameter dry_run must appear at most once.",
        )
    dry_query = str(dry_query_values[0]) if dry_query_values else ""
    dry_field = str(fields.get("dry_run") or "")
    if dry_query and dry_field and dry_query != dry_field:
        raise UploadError(
            "ambiguous_meeting_metadata",
            "Conflicting query and multipart values for dry_run.",
        )
    return result


def validate_ingestion_metadata(
    config: Config,
    raw: dict[str, str],
    *,
    data_version: int,
    generate_uid: bool,
) -> IngestionMetadata:
    contract = load_pipeline_contract(config.pipeline_contract_path)
    try:
        meeting_date = contract.validate_meeting_date(raw.get("meeting_date"))
        meeting_series = contract.validate_metadata_text(
            raw.get("meeting_series"),
            "meeting_series",
            contract.CONTRACT.maximum_series_length,
        )
        meeting_type = contract.validate_metadata_text(
            raw.get("meeting_type"),
            "meeting_type",
            contract.CONTRACT.maximum_series_length,
        )
        raw_uid = str(raw.get("meeting_uid") or "").strip()
        meeting_uid = (
            contract.validate_meeting_uid(raw_uid)
            if raw_uid
            else contract.new_meeting_uid() if generate_uid else ""
        )
        normalized_version = contract.validate_data_version(data_version)
        normalized_file_name = contract.build_artifact_filename(
            meeting_date=meeting_date,
            meeting_series=meeting_series,
            artifact_type="meeting_minutes",
            data_version=normalized_version,
            extension="md",
        )
    except (ValueError, RuntimeError) as exc:
        raise UploadError("invalid_meeting_metadata", str(exc), 422) from exc
    return IngestionMetadata(
        meeting_uid=meeting_uid,
        meeting_date=meeting_date,
        meeting_series=meeting_series,
        meeting_type=meeting_type,
        data_version=normalized_version,
        normalized_file_name=normalized_file_name,
        month=meeting_date[:7],
    )


def load_env_files() -> None:
    candidates = [
        Path.cwd() / ".env",
        Path(__file__).resolve().parent / ".env",
    ]
    seen: set[Path] = set()
    for path in candidates:
        if path in seen or not path.exists():
            continue
        seen.add(path)
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip("'").strip('"')
            if key and key not in os.environ:
                os.environ[key] = value


def resolve_path(value: str) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return Path.cwd() / path


def parse_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise UploadError("invalid_config", f"{name} must be an integer.", 500) from exc
    if value <= 0:
        raise UploadError("invalid_config", f"{name} must be positive.", 500)
    return value


def parse_bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def meeting_contract_config_ready(config: Config) -> bool:
    if not config.meeting_contract_enabled:
        return False
    validator = Path(config.meeting_contract_validator)
    expected_hash = config.meeting_contract_validator_sha256
    if (
        not validator.is_absolute()
        or validator.is_symlink()
        or not validator.is_file()
        or not re.fullmatch(r"[0-9a-f]{64}", expected_hash)
    ):
        return False
    try:
        return hashlib.sha256(validator.read_bytes()).hexdigest() == expected_hash
    except OSError:
        return False


def print_json(data: dict[str, Any]) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def tail_token(value: str) -> str:
    if not value:
        return ""
    return "..." + value[-4:]


def _load_user_db_unlocked(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "users": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UploadError("user_db_invalid", "Upload user database is invalid JSON.", 500) from exc
    if not isinstance(data, dict) or not isinstance(data.get("users"), list):
        raise UploadError("user_db_invalid", "Upload user database has an invalid structure.", 500)
    return data


def ensure_private_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)


def fsync_directory(path: Path) -> None:
    directory_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


@contextmanager
def user_db_lock(path: Path, *, exclusive: bool):
    ensure_private_parent(path)
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
def upload_month_lock(config: Config, month: str):
    root = config.user_db_path.parent / "upload-operation-locks"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    digest = hashlib.sha256(f"{config.parent_folder_token}\0{month}".encode("utf-8")).hexdigest()
    path = root / f"{digest}.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "r+", encoding="utf-8") as handle:
            fd = -1
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        if fd >= 0:
            os.close(fd)


def extract_idempotency_key(headers: http.client.HTTPMessage) -> str:
    values = headers.get_all("Idempotency-Key", [])
    if len(values) > 1:
        raise UploadError(
            "ambiguous_idempotency_key",
            "At most one Idempotency-Key header is allowed.",
            400,
        )
    if not values:
        return ""
    value = values[0].strip()
    if not IDEMPOTENCY_KEY_RE.fullmatch(value):
        raise UploadError(
            "invalid_idempotency_key",
            "Idempotency-Key must be 16-128 ASCII letters, digits, dot, underscore, colon, or hyphen.",
            400,
        )
    return value


def idempotency_record_path(config: Config, user_id: str, key: str) -> Path:
    digest = hashlib.sha256(f"{user_id}\0{key}".encode("utf-8")).hexdigest()
    return config.user_db_path.parent / "upload-idempotency" / f"{digest}.json"


@contextmanager
def idempotency_lock(config: Config, user_id: str, key: str):
    record_path = idempotency_record_path(config, user_id, key)
    ensure_private_parent(record_path)
    lock_path = record_path.with_suffix(".lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "r+", encoding="utf-8") as handle:
            fd = -1
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield record_path
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        if fd >= 0:
            os.close(fd)


def ingestion_receipt_path(config: Config, user_id: str, key: str) -> Path:
    digest = hashlib.sha256(f"{user_id}\0{key}".encode("utf-8")).hexdigest()
    return config.user_db_path.parent / "meeting-ingestion-receipts" / f"{digest}.json"


@contextmanager
def ingestion_receipt_lock(config: Config, user_id: str, key: str):
    receipt_path = ingestion_receipt_path(config, user_id, key)
    ensure_private_parent(receipt_path)
    lock_path = receipt_path.with_suffix(".lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "r+", encoding="utf-8") as handle:
            fd = -1
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield receipt_path
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        if fd >= 0:
            os.close(fd)


@contextmanager
def meeting_uid_lock(config: Config, meeting_uid: str):
    root = config.meeting_registry_path / ".locks"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    digest = hashlib.sha256(meeting_uid.encode("utf-8")).hexdigest()
    path = root / f"{digest}.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "r+", encoding="utf-8") as handle:
            fd = -1
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        if fd >= 0:
            os.close(fd)


def _load_private_json(path: Path, label: str) -> dict[str, Any] | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise UploadError(f"{label}_invalid", f"{label.replace('_', ' ').title()} path is unsafe.", 500)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UploadError(f"{label}_invalid", f"{label.replace('_', ' ').title()} is invalid.", 500) from exc
    if not isinstance(value, dict):
        raise UploadError(f"{label}_invalid", f"{label.replace('_', ' ').title()} must contain an object.", 500)
    return value


def load_ingestion_receipt(path: Path) -> dict[str, Any] | None:
    value = _load_private_json(path, "ingestion_receipt")
    if value is None:
        return None
    allowed_statuses = {
        "prepared",
        "drive_uploaded",
        "baseline_captured",
        "base_committed",
        "jobs_queued",
        "completed",
    }
    if (
        value.get("version") != INGESTION_RECEIPT_VERSION
        or value.get("status") not in allowed_statuses
        or not re.fullmatch(r"[0-9a-f]{64}", str(value.get("fingerprint") or ""))
    ):
        raise UploadError("ingestion_receipt_invalid", "Ingestion receipt has an invalid structure.", 500)
    return value


def save_ingestion_receipt(path: Path, value: dict[str, Any]) -> None:
    try:
        atomic_private_write(path, json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    except AtomicCommitUncertain as exc:
        raise UploadError(
            "ingestion_outcome_uncertain",
            "Ingestion receipt durability is uncertain; retry with the same Idempotency-Key.",
            503,
        ) from exc
    except OSError as exc:
        raise UploadError("ingestion_receipt_failed", "Ingestion receipt could not be saved.", 500) from exc


def save_post_effect_ingestion_receipt(path: Path, value: dict[str, Any]) -> None:
    """Persist a stage after Drive/Base/job effects without reporting a safe failure."""
    try:
        save_ingestion_receipt(path, value)
    except UploadError as exc:
        if exc.error_code == "ingestion_outcome_uncertain":
            raise
        raise UploadError(
            "ingestion_outcome_uncertain",
            "An ingestion effect completed, but its receipt is not durable; retry with the same Idempotency-Key.",
            503,
        ) from exc


def meeting_registry_file(config: Config, meeting_uid: str) -> Path:
    if not re.fullmatch(r"mtg_[0-9a-f]{32}", meeting_uid):
        raise UploadError("invalid_meeting_uid", "Meeting UID is invalid.", 422)
    return config.meeting_registry_path / f"{meeting_uid}.json"


def load_meeting_registry(config: Config, meeting_uid: str) -> dict[str, Any] | None:
    value = _load_private_json(meeting_registry_file(config, meeting_uid), "meeting_registry")
    if value is None:
        return None
    required = {
        "version",
        "meeting_uid",
        "meeting_date",
        "meeting_series",
        "meeting_type",
        "data_version",
        "normalized_file_name",
        "source_md_sha256",
        "file_token",
        "url",
        "baseline_file_token",
        "baseline_url",
        "record_id",
        "updated_at",
    }
    if (
        not required.issubset(value)
        or value.get("version") != MEETING_REGISTRY_VERSION
        or value.get("meeting_uid") != meeting_uid
        or not isinstance(value.get("data_version"), int)
        or int(value.get("data_version")) < 1
        or not re.fullmatch(r"[0-9a-f]{64}", str(value.get("source_md_sha256") or ""))
    ):
        raise UploadError("meeting_registry_invalid", "Meeting registry has an invalid structure.", 500)
    return value


def save_meeting_registry(config: Config, value: dict[str, Any]) -> None:
    path = meeting_registry_file(config, str(value.get("meeting_uid") or ""))
    try:
        atomic_private_write(path, json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    except (AtomicCommitUncertain, OSError) as exc:
        raise UploadError(
            "ingestion_outcome_uncertain",
            "Meeting registry update is uncertain; retry with the same Idempotency-Key.",
            503,
        ) from exc


def ingestion_fingerprint(
    *,
    user_id: str,
    original_file_name: str,
    content_sha256: str,
    raw_metadata: dict[str, str],
    config: Config,
) -> str:
    material = {
        "content_sha256": content_sha256,
        "meeting_date": raw_metadata.get("meeting_date", ""),
        "meeting_series": raw_metadata.get("meeting_series", ""),
        "meeting_type": raw_metadata.get("meeting_type", ""),
        "meeting_uid": raw_metadata.get("meeting_uid", "").strip().lower(),
        "original_file_name": original_file_name,
        "resource_sha256": hashlib.sha256(
            "\0".join(
                (
                    config.parent_folder_token,
                    config.baseline_parent_folder_token,
                    config.meeting_base_app_token,
                    config.meeting_base_table_id,
                )
            ).encode("utf-8")
        ).hexdigest(),
        "user_id": user_id,
        "v": INGESTION_RECEIPT_VERSION,
    }
    canonical = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_idempotency_record(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise UploadError("idempotency_record_invalid", "Idempotency record path is unsafe.", 500)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UploadError("idempotency_record_invalid", "Idempotency record is invalid.", 500) from exc
    if (
        not isinstance(record, dict)
        or record.get("version") != IDEMPOTENCY_RECORD_VERSION
        or record.get("status") not in {"in_flight", "completed"}
        or not re.fullmatch(r"[0-9a-f]{64}", str(record.get("fingerprint") or ""))
    ):
        raise UploadError("idempotency_record_invalid", "Idempotency record has an invalid structure.", 500)
    return record


def save_idempotency_record(path: Path, record: dict[str, Any]) -> None:
    text = json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    try:
        atomic_private_write(path, text)
    except AtomicCommitUncertain as exc:
        raise UploadError(
            "idempotency_store_uncertain",
            "Idempotency evidence could not be durably confirmed; retry with the same key.",
            503,
        ) from exc
    except UploadError:
        raise
    except OSError as exc:
        raise UploadError(
            "idempotency_store_failed",
            "Idempotency evidence could not be saved.",
            500,
        ) from exc


def save_completed_idempotency_record(path: Path, record: dict[str, Any]) -> None:
    """Persist a post-upload receipt without ever reporting a remote write as failed."""
    try:
        save_idempotency_record(path, record)
    except UploadError as exc:
        if exc.error_code == "idempotency_store_uncertain":
            raise
        raise UploadError(
            "idempotency_store_uncertain",
            "Upload completed, but its idempotency receipt could not be durably confirmed; retry with the same key.",
            503,
        ) from exc


def idempotency_fingerprint(
    *,
    user_id: str,
    file_name: str,
    content_sha256: str,
    month: str,
    parent_folder_token: str,
) -> str:
    material = {
        "content_sha256": content_sha256,
        "file_name": file_name,
        "month": month,
        "parent_folder_sha256": hashlib.sha256(parent_folder_token.encode("utf-8")).hexdigest(),
        "user_id": user_id,
        "v": 1,
    }
    canonical = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def atomic_private_write(path: Path, text: str) -> None:
    ensure_private_parent(path)
    if path.is_symlink():
        raise UploadError("unsafe_output_path", "Refusing to replace a symbolic link.", 500)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    replaced = False
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        replaced = True
        os.chmod(path, 0o600)
        fsync_directory(path.parent)
    except Exception as exc:
        if replaced:
            raise AtomicCommitUncertain(f"commit outcome is uncertain for {path}") from exc
        raise
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def _save_user_db_unlocked(path: Path, data: dict[str, Any]) -> None:
    atomic_private_write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def load_user_db(path: Path) -> dict[str, Any]:
    with user_db_lock(path, exclusive=False):
        return _load_user_db_unlocked(path)


def save_user_db(path: Path, data: dict[str, Any]) -> None:
    with user_db_lock(path, exclusive=True):
        _save_user_db_unlocked(path, data)


@contextmanager
def update_user_db(path: Path):
    with user_db_lock(path, exclusive=True):
        data = _load_user_db_unlocked(path)
        yield data
        _save_user_db_unlocked(path, data)


def slugify_user_id(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "user"


def make_token() -> str:
    return "fmu_" + secrets.token_urlsafe(32)


def hash_token(token: str, salt_hex: str | None = None) -> str:
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
    digest = hashlib.sha256(salt + token.encode("utf-8")).hexdigest()
    return f"sha256:{salt.hex()}:{digest}"


def verify_token_hash(token: str, stored_hash: str) -> bool:
    try:
        algo, salt_hex, expected = stored_hash.split(":", 2)
    except ValueError:
        return False
    if algo != "sha256":
        return False
    try:
        actual = hash_token(token, salt_hex).split(":", 2)[2]
    except ValueError:
        return False
    return hmac.compare_digest(actual, expected)


def authenticate_upload_token(token: str, user_db_path: Path) -> dict[str, Any]:
    if not token:
        raise UploadError("unauthorized", "Missing upload token.", 401)
    data = load_user_db(user_db_path)
    for user in data.get("users", []):
        if not user.get("enabled", True):
            continue
        if verify_token_hash(token, str(user.get("token_hash", ""))):
            return user
    raise UploadError("unauthorized", "Invalid upload token.", 401)


def write_secret_file(path_value: str, token: str) -> None:
    path = resolve_path(path_value)
    atomic_private_write(path, token + "\n")


def stage_secret_file(path_value: str, token: str) -> tuple[Path, Path]:
    final_path = resolve_path(path_value)
    ensure_private_parent(final_path)
    if final_path.is_symlink():
        raise UploadError("unsafe_output_path", "Refusing to replace a symbolic link.", 500)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{final_path.name}.pending.",
        suffix=".token",
        dir=final_path.parent,
    )
    staged_path = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(token + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        discard_staged_secret(staged_path)
        raise
    finally:
        if fd >= 0:
            os.close(fd)
    fsync_directory(staged_path.parent)
    return final_path, staged_path


def discard_staged_secret(staged_path: Path | None) -> None:
    if staged_path is None:
        return
    try:
        staged_path.unlink()
    except FileNotFoundError:
        return
    fsync_directory(staged_path.parent)


def commit_staged_secret(final_path: Path, staged_path: Path) -> None:
    try:
        if final_path.is_symlink():
            raise OSError("symbolic link target")
        os.replace(staged_path, final_path)
        os.chmod(final_path, 0o600)
        fsync_directory(final_path.parent)
    except OSError as exc:
        recovery_path = staged_path if staged_path.exists() else final_path
        raise UploadError(
            "token_file_commit_failed",
            f"Token is active; recover it from the private token file: {recovery_path}",
            500,
        ) from exc


def save_credential_update(
    user_db_path: Path,
    data: dict[str, Any],
    *,
    final_path: Path | None,
    staged_path: Path | None,
) -> None:
    try:
        _save_user_db_unlocked(user_db_path, data)
    except AtomicCommitUncertain as exc:
        recovery = str(staged_path) if staged_path is not None else "unavailable (--write-token-file omitted)"
        raise UploadError(
            "user_db_commit_uncertain",
            f"User DB may have activated the new token; recovery token file: {recovery}",
            500,
        ) from exc
    except Exception:
        discard_staged_secret(staged_path)
        raise
    if final_path is not None and staged_path is not None:
        commit_staged_secret(final_path, staged_path)


def create_user(args: argparse.Namespace) -> int:
    config = Config.from_env(args.users_path)
    token = make_token()
    now = utc_now()
    final_path: Path | None = None
    staged_path: Path | None = None
    if args.write_token_file:
        final_path, staged_path = stage_secret_file(args.write_token_file, token)
    with user_db_lock(config.user_db_path, exclusive=True):
        data = _load_user_db_unlocked(config.user_db_path)
        try:
            existing_ids = {str(user.get("user_id")) for user in data.get("users", [])}
            base_id = slugify_user_id(args.name)
            user_id = base_id
            index = 2
            while user_id in existing_ids:
                user_id = f"{base_id}-{index}"
                index += 1
            data["users"].append(
                {
                    "user_id": user_id,
                    "name": args.name,
                    "source": args.source,
                    "enabled": True,
                    "token_hash": hash_token(token),
                    "created_at": now,
                    "updated_at": now,
                }
            )
            save_credential_update(
                config.user_db_path,
                data,
                final_path=final_path,
                staged_path=staged_path,
            )
        except UploadError:
            raise
        except Exception:
            discard_staged_secret(staged_path)
            raise

    print(f"created_user_id: {user_id}")
    print(f"upload_token: {token}")
    if args.write_token_file:
        print(f"token_file: {args.write_token_file}")
    print("notice: the upload token is shown once; store it securely.")
    return 0


def list_users(args: argparse.Namespace) -> int:
    config = Config.from_env(args.users_path)
    data = load_user_db(config.user_db_path)
    rows = []
    for user in data.get("users", []):
        rows.append(
            {
                "user_id": user.get("user_id"),
                "name": user.get("name"),
                "source": user.get("source"),
                "enabled": bool(user.get("enabled", True)),
                "created_at": user.get("created_at"),
                "updated_at": user.get("updated_at"),
            }
        )
    print_json({"ok": True, "users": rows})
    return 0


def disable_user(args: argparse.Namespace) -> int:
    config = Config.from_env(args.users_path)
    with update_user_db(config.user_db_path) as data:
        for user in data.get("users", []):
            if user.get("user_id") == args.user_id:
                user["enabled"] = False
                user["updated_at"] = utc_now()
                break
        else:
            print_json({"ok": False, "error_code": "user_not_found", "message": "User was not found."})
            return 1
    print_json({"ok": True, "disabled_user_id": args.user_id})
    return 0


def rotate_user(args: argparse.Namespace) -> int:
    config = Config.from_env(args.users_path)
    token = make_token()
    final_path: Path | None = None
    staged_path: Path | None = None
    if args.write_token_file:
        final_path, staged_path = stage_secret_file(args.write_token_file, token)
    with user_db_lock(config.user_db_path, exclusive=True):
        data = _load_user_db_unlocked(config.user_db_path)
        try:
            for user in data.get("users", []):
                if user.get("user_id") == args.user_id:
                    user["token_hash"] = hash_token(token)
                    user["updated_at"] = utc_now()
                    break
            else:
                discard_staged_secret(staged_path)
                print_json({"ok": False, "error_code": "user_not_found", "message": "User was not found."})
                return 1
            save_credential_update(
                config.user_db_path,
                data,
                final_path=final_path,
                staged_path=staged_path,
            )
        except UploadError:
            raise
        except Exception:
            discard_staged_secret(staged_path)
            raise
    print(f"rotated_user_id: {args.user_id}")
    print(f"upload_token: {token}")
    if args.write_token_file:
        print(f"token_file: {args.write_token_file}")
    print("notice: the upload token is shown once; store it securely.")
    return 0


def doctor(args: argparse.Namespace) -> int:
    config = Config.from_env(args.users_path)
    readiness = local_readiness(config)
    checks = {
        "python": sys.version.split()[0],
        "FEISHU_APP_ID": "set" if config.app_id else "missing",
        "FEISHU_APP_SECRET": "set" if config.app_secret else "missing",
        "FEISHU_PARENT_FOLDER_TOKEN": "set" if config.parent_folder_token else "missing",
        "FEISHU_BASELINE_PARENT_FOLDER_TOKEN": (
            "set" if config.baseline_parent_folder_token else "missing"
        ),
        "FEISHU_MEETING_BASE_APP_TOKEN": (
            "set" if config.meeting_base_app_token else "missing"
        ),
        "FEISHU_MEETING_BASE_TABLE_ID": (
            "set" if config.meeting_base_table_id else "missing"
        ),
        "FEISHU_OUTPUT_OWNER_OPEN_ID": "set" if config.output_owner_open_id else "missing",
        "upload_users_ready": readiness["user_db"],
        "max_upload_bytes": config.max_upload_bytes,
        "meeting_contract_enabled": config.meeting_contract_enabled,
        "meeting_contract_validator": "set" if config.meeting_contract_validator else "missing",
        "meeting_contract_validator_sha256": (
            "set" if re.fullmatch(r"[0-9a-f]{64}", config.meeting_contract_validator_sha256) else "missing"
        ),
        "pipeline_contract": readiness["pipeline"],
        "generation_job_spool_path": str(config.generation_job_spool_path),
        "meeting_registry_path": str(config.meeting_registry_path),
    }
    ok = bool(readiness["ready"])
    print_json({"ok": ok, "service": SERVICE_NAME, "checks": checks})
    return 0 if ok else 1


def local_readiness(config: Config) -> dict[str, bool]:
    credentials_ready = bool(config.app_id and config.app_secret)
    pipeline_ready = pipeline_resources_ready(config)
    resource_ready = bool(config.parent_folder_token) and pipeline_ready
    # Uploaded Markdown is registered regardless of its document structure.
    # Keep the legacy health key compatible, but do not make an optional
    # content linter a readiness or upload precondition.
    contract_ready = True
    user_db_ready = False
    if config.user_db_path.is_file():
        try:
            data = load_user_db(config.user_db_path)
            user_db_ready = any(
                isinstance(user, dict)
                and user.get("enabled", True)
                and bool(str(user.get("token_hash", "")))
                for user in data.get("users", [])
            )
        except UploadError:
            user_db_ready = False
    return {
        "ready": credentials_ready and resource_ready and contract_ready and user_db_ready,
        "credentials": credentials_ready,
        "resource": resource_ready,
        "contract": contract_ready,
        "user_db": user_db_ready,
        "pipeline": pipeline_ready,
    }


def parse_bool(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def sanitize_file_name(raw_name: str) -> str:
    name = Path(raw_name.replace("\x00", "")).name
    if not name or name in {".", ".."}:
        raise UploadError("invalid_file_name", "File name is invalid.")
    return name


def validate_markdown_file(file_name: str, data: bytes, max_bytes: int) -> None:
    if Path(file_name).suffix.lower() != ".md":
        raise UploadError("unsupported_file_type", "Only .md files are allowed.", 415)
    if not data:
        raise UploadError("empty_file", "File is empty.")
    if len(data) > max_bytes:
        raise UploadError("file_too_large", "File exceeds the configured size limit.", 413)
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UploadError("invalid_markdown_encoding", "Markdown file must be UTF-8.", 422) from exc


def validate_meeting_contract(data: bytes, config: Config) -> None:
    if not config.meeting_contract_enabled:
        raise UploadError(
            "meeting_contract_config_invalid",
            "Meeting-minutes contract validation cannot be disabled.",
            500,
        )
    validator = Path(config.meeting_contract_validator)
    if not meeting_contract_config_ready(config):
        raise UploadError(
            "meeting_contract_config_invalid",
            "Meeting-minutes contract validation is not configured.",
            500,
        )
    try:
        with tempfile.TemporaryDirectory(prefix="meeting-upload-contract-") as temp_dir:
            source = Path(temp_dir) / "meeting.md"
            source.write_bytes(data)
            result = subprocess.run(
                [sys.executable, str(validator), str(source), "--json"],
                capture_output=True,
                check=False,
                timeout=30,
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UploadError(
            "meeting_contract_unavailable",
            "Meeting-minutes contract validation could not complete.",
            503,
        ) from exc
    try:
        payload = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise UploadError(
            "meeting_contract_unavailable",
            "Meeting-minutes contract validator returned invalid output.",
            503,
        ) from exc
    if result.returncode != 0 or not isinstance(payload, dict) or payload.get("ok") is not True:
        raise UploadError(
            "meeting_contract_invalid",
            "Meeting minutes do not satisfy the required output contract.",
            422,
        )


def resolve_month(file_name: str, date_param: str | None) -> str:
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", file_name)
    if match:
        date_value = match.group(1)
    elif date_param:
        date_value = date_param
    else:
        date_value = dt.date.today().isoformat()
    try:
        parsed = dt.date.fromisoformat(date_value)
    except ValueError as exc:
        raise UploadError("invalid_date", "Date must be YYYY-MM-DD.") from exc
    return f"{parsed.year:04d}-{parsed.month:02d}"


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
    raise UploadError("duplicate_name_exhausted", "Could not generate a unique file name.", 409)


def feishu_request_json(
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: int = 20,
) -> dict[str, Any]:
    request = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        try:
            data = json.loads(payload.decode("utf-8"))
        except Exception:
            data = {}
        if exc.code in {401, 403}:
            raise UploadError("feishu_permission_denied", "Feishu OpenAPI denied the request.", 502) from exc
        raise UploadError("feishu_api_error", "Feishu OpenAPI rejected the request.", 502) from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        raise UploadError("feishu_network_error", "Could not reach Feishu OpenAPI.", 502) from exc

    try:
        data = json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise UploadError("feishu_bad_response", "Feishu OpenAPI returned non-JSON data.", 502) from exc
    code = data.get("code", 0)
    if code != 0:
        raise UploadError("feishu_api_error", "Feishu OpenAPI rejected the request.", 502)
    return data


def get_tenant_access_token(config: Config) -> str:
    if not config.app_id or not config.app_secret:
        raise UploadError("config_missing", "Feishu app credentials are not configured.", 500)
    body = json.dumps(
        {"app_id": config.app_id, "app_secret": config.app_secret},
        ensure_ascii=False,
    ).encode("utf-8")
    data = feishu_request_json(
        "POST",
        f"{config.openapi_base}/auth/v3/tenant_access_token/internal",
        headers={"Content-Type": "application/json; charset=utf-8"},
        body=body,
    )
    token = data.get("tenant_access_token") or data.get("data", {}).get("tenant_access_token")
    if not token:
        raise UploadError("feishu_bad_response", "Feishu did not return tenant access token.", 502)
    return str(token)


def list_folder(config: Config, tenant_access_token: str, folder_token: str) -> list[dict[str, Any]]:
    headers = {"Authorization": f"Bearer {tenant_access_token}"}
    files: list[dict[str, Any]] = []
    page_token = ""
    while True:
        params = {"folder_token": folder_token, "page_size": "200"}
        if page_token:
            params["page_token"] = page_token
        url = f"{config.openapi_base}/drive/v1/files?{urllib.parse.urlencode(params)}"
        data = feishu_request_json("GET", url, headers=headers)
        payload = data.get("data", {})
        page_files = payload.get("files", [])
        if isinstance(page_files, list):
            files.extend(item for item in page_files if isinstance(item, dict))
        if not payload.get("has_more"):
            return files
        page_token = str(payload.get("next_page_token") or "")
        if not page_token:
            return files


def find_month_folder(items: list[dict[str, Any]], month: str) -> dict[str, Any]:
    for item in items:
        if item.get("name") == month and item.get("type") == "folder" and item.get("token"):
            return item
    raise UploadError("month_folder_not_prepared", "Month folder is not prepared.", 409)


def encode_multipart_upload(file_name: str, parent_node: str, data: bytes) -> tuple[str, bytes]:
    boundary = "----feishu-minutes-upload-" + secrets.token_hex(16)
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


def upload_file(config: Config, tenant_access_token: str, folder_token: str, file_name: str, data: bytes) -> str:
    content_type, body = encode_multipart_upload(file_name, folder_token, data)
    response = feishu_request_json(
        "POST",
        f"{config.openapi_base}/drive/v1/files/upload_all",
        headers={
            "Authorization": f"Bearer {tenant_access_token}",
            "Content-Type": content_type,
        },
        body=body,
        timeout=60,
    )
    file_token = response.get("data", {}).get("file_token")
    if not file_token:
        raise UploadError("feishu_bad_response", "Feishu did not return uploaded file token.", 502)
    return str(file_token)


def transfer_output_owner(config: Config, tenant_access_token: str, file_token: str) -> None:
    owner_open_id = config.output_owner_open_id.strip()
    if not owner_open_id:
        return
    token_path = urllib.parse.quote(file_token, safe="")
    query = urllib.parse.urlencode(
        {
            "type": "file",
            "need_notification": "false",
            "remove_old_owner": "false",
            "old_owner_perm": "full_access",
            "stay_put": "true",
        }
    )
    body = json.dumps(
        {"member_id": owner_open_id, "member_type": "openid"},
        ensure_ascii=False,
    ).encode("utf-8")
    try:
        feishu_request_json(
            "POST",
            f"{config.openapi_base}/drive/v1/permissions/{token_path}/members/transfer_owner?{query}",
            headers={
                "Authorization": f"Bearer {tenant_access_token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            body=body,
        )
    except UploadError as exc:
        raise UploadError(
            "owner_transfer_failed",
            "File was uploaded, but ownership could not be transferred.",
            502,
        ) from exc


def resolve_uploaded_url(
    config: Config,
    tenant_access_token: str,
    month_folder_token: str,
    uploaded_file_name: str,
    file_token: str,
) -> str:
    try:
        items = list_folder(config, tenant_access_token, month_folder_token)
    except UploadError:
        return config.file_url_base.rstrip("/") + "/" + file_token
    for item in items:
        if item.get("token") == file_token and item.get("url"):
            return str(item.get("url"))
    for item in items:
        if item.get("name") == uploaded_file_name and item.get("url"):
            return str(item.get("url"))
    return config.file_url_base.rstrip("/") + "/" + file_token


def download_drive_file(config: Config, tenant_access_token: str, file_token: str) -> bytes:
    token_path = urllib.parse.quote(file_token, safe="")
    request = urllib.request.Request(
        f"{config.openapi_base}/drive/v1/files/{token_path}/download",
        headers={"Authorization": f"Bearer {tenant_access_token}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read(config.max_upload_bytes + 1)
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            raise UploadError("feishu_permission_denied", "Feishu OpenAPI denied the request.", 502) from exc
        raise UploadError("feishu_api_error", "Feishu OpenAPI rejected the request.", 502) from exc
    except urllib.error.URLError as exc:
        raise UploadError("feishu_network_error", "Could not reach Feishu OpenAPI.", 502) from exc
    if len(data) > config.max_upload_bytes:
        raise UploadError(
            "idempotency_reconciliation_conflict",
            "Remote file exceeds the expected upload limit.",
            409,
        )
    return data


def _file_url(config: Config, item: dict[str, Any], file_token: str) -> str:
    return str(item.get("url") or config.file_url_base.rstrip("/") + "/" + file_token)


def find_drive_file_by_name_and_hash(
    config: Config,
    tenant_access_token: str,
    folder_token: str,
    file_name: str,
    expected_sha256: str,
) -> tuple[str, str]:
    matches: list[tuple[str, str]] = []
    for item in list_folder(config, tenant_access_token, folder_token):
        if item.get("type") != "file" or item.get("name") != file_name or not item.get("token"):
            continue
        file_token = str(item["token"])
        try:
            actual_sha = hashlib.sha256(
                download_drive_file(config, tenant_access_token, file_token)
            ).hexdigest()
        except UploadError:
            continue
        if hmac.compare_digest(actual_sha, expected_sha256):
            matches.append((file_token, _file_url(config, item, file_token)))
    if not matches:
        raise UploadError(
            "ingestion_outcome_uncertain",
            "Drive file outcome is not visible; retry with the same Idempotency-Key.",
            503,
        )
    if len(matches) != 1:
        raise UploadError(
            "ingestion_drive_ambiguous",
            "Multiple Drive files have the same display name and content hash.",
            409,
        )
    return matches[0]


def upload_file_confirmed(
    config: Config,
    tenant_access_token: str,
    folder_token: str,
    file_name: str,
    data: bytes,
) -> tuple[str, str]:
    expected_sha = hashlib.sha256(data).hexdigest()
    try:
        file_token = upload_file(config, tenant_access_token, folder_token, file_name, data)
    except Exception:
        file_token, url = find_drive_file_by_name_and_hash(
            config,
            tenant_access_token,
            folder_token,
            file_name,
            expected_sha,
        )
    else:
        try:
            actual = download_drive_file(config, tenant_access_token, file_token)
        except Exception:
            file_token, url = find_drive_file_by_name_and_hash(
                config,
                tenant_access_token,
                folder_token,
                file_name,
                expected_sha,
            )
        else:
            if not hmac.compare_digest(hashlib.sha256(actual).hexdigest(), expected_sha):
                raise UploadError(
                    "ingestion_drive_conflict",
                    "Uploaded Drive file hash does not match the source request.",
                    409,
                )
            url = resolve_uploaded_url(
                config, tenant_access_token, folder_token, file_name, file_token
            )
    # Ownership is a separate required terminal effect. Never convert its
    # deterministic failure into a successful upload reconciliation.
    transfer_output_owner(config, tenant_access_token, file_token)
    return file_token, url


def copy_drive_file(
    config: Config,
    tenant_access_token: str,
    file_token: str,
    target_folder_token: str,
    target_name: str,
) -> dict[str, Any]:
    body = json.dumps(
        {"name": target_name, "type": "file", "folder_token": target_folder_token},
        ensure_ascii=False,
    ).encode("utf-8")
    result = feishu_request_json(
        "POST",
        f"{config.openapi_base}/drive/v1/files/{urllib.parse.quote(file_token, safe='')}/copy",
        headers={
            "Authorization": f"Bearer {tenant_access_token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        body=body,
    )
    data = result.get("data", {})
    file_value = data.get("file") if isinstance(data, dict) else None
    return file_value if isinstance(file_value, dict) else data if isinstance(data, dict) else {}


def copy_file_confirmed(
    config: Config,
    tenant_access_token: str,
    source_file_token: str,
    target_folder_token: str,
    target_name: str,
    expected_sha256: str,
) -> tuple[str, str]:
    try:
        copied = copy_drive_file(
            config,
            tenant_access_token,
            source_file_token,
            target_folder_token,
            target_name,
        )
        copied_token = str(copied.get("token") or copied.get("file_token") or "")
        if not copied_token:
            raise UploadError("ingestion_outcome_uncertain", "Drive copy token is missing.", 503)
    except Exception:
        copied_token, copied_url = find_drive_file_by_name_and_hash(
            config,
            tenant_access_token,
            target_folder_token,
            target_name,
            expected_sha256,
        )
    else:
        try:
            actual_sha = hashlib.sha256(
                download_drive_file(config, tenant_access_token, copied_token)
            ).hexdigest()
        except Exception:
            copied_token, copied_url = find_drive_file_by_name_and_hash(
                config,
                tenant_access_token,
                target_folder_token,
                target_name,
                expected_sha256,
            )
        else:
            if not hmac.compare_digest(actual_sha, expected_sha256):
                raise UploadError(
                    "ingestion_baseline_conflict",
                    "Review baseline hash does not match the uploaded source.",
                    409,
                )
            copied_url = _file_url(config, copied, copied_token)
    transfer_output_owner(config, tenant_access_token, copied_token)
    return copied_token, copied_url


def list_meeting_records(config: Config, tenant_access_token: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    page_token = ""
    while True:
        params = {"page_size": "500"}
        if page_token:
            params["page_token"] = page_token
        url = (
            f"{config.openapi_base}/bitable/v1/apps/"
            f"{urllib.parse.quote(config.meeting_base_app_token, safe='')}/tables/"
            f"{urllib.parse.quote(config.meeting_base_table_id, safe='')}/records?"
            f"{urllib.parse.urlencode(params)}"
        )
        result = feishu_request_json(
            "GET", url, headers={"Authorization": f"Bearer {tenant_access_token}"}
        )
        data = result.get("data", {})
        items = data.get("items", []) if isinstance(data, dict) else []
        if isinstance(items, list):
            records.extend(item for item in items if isinstance(item, dict))
        if not isinstance(data, dict) or not data.get("has_more"):
            return records
        page_token = str(data.get("page_token") or data.get("next_page_token") or "")
        if not page_token:
            raise UploadError("meeting_base_bad_response", "Base pagination token is missing.", 502)


def plain_bitable_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        for key in ("link", "text", "value", "name"):
            if value.get(key) is not None:
                return str(value[key]).strip()
        return ""
    if isinstance(value, list):
        return ",".join(item for item in (plain_bitable_value(item) for item in value) if item)
    return str(value).strip()


def find_meeting_record(
    config: Config,
    tenant_access_token: str,
    meeting_uid: str,
) -> dict[str, Any] | None:
    matches = []
    for record in list_meeting_records(config, tenant_access_token):
        fields = record.get("fields", {})
        if isinstance(fields, dict) and plain_bitable_value(fields.get("会议ID")) == meeting_uid:
            matches.append(record)
    if len(matches) > 1:
        raise UploadError(
            "meeting_record_ambiguous",
            "Multiple Base records have the same meeting UID.",
            409,
        )
    return matches[0] if matches else None


def meeting_date_millis(meeting_date: str) -> int:
    value = dt.datetime.strptime(meeting_date, "%Y-%m-%d").replace(
        tzinfo=dt.timezone(dt.timedelta(hours=8))
    )
    return int(value.timestamp() * 1000)


def base_url_value(url: str, text: str) -> dict[str, str]:
    return {"link": url, "text": text}


def source_review_status_for_update(existing_fields: dict[str, Any], name: str) -> str:
    current = plain_bitable_value(existing_fields.get(name))
    return "需重审" if current == "已审核" else "未审核"


def meeting_record_fields(receipt: dict[str, Any], existing: dict[str, Any] | None) -> dict[str, Any]:
    existing_fields = existing.get("fields", {}) if isinstance(existing, dict) else {}
    if not isinstance(existing_fields, dict):
        existing_fields = {}
    file_name = str(receipt["normalized_file_name"])
    baseline_name = str(receipt["baseline_file_name"])
    fields: dict[str, Any] = {
        "会议ID": str(receipt["meeting_uid"]),
        "会议日期": meeting_date_millis(str(receipt["meeting_date"])),
        "会议系列": str(receipt["meeting_series"]),
        "会议类型": str(receipt["meeting_type"]),
        "数据版本": int(receipt["data_version"]),
        "会议纪要MD": base_url_value(str(receipt["url"]), file_name),
        "会议纪要审核前MD": base_url_value(str(receipt["baseline_url"]), baseline_name),
        "源纪要审核": source_review_status_for_update(existing_fields, "源纪要审核"),
        "行业与市场观点审核": source_review_status_for_update(
            existing_fields, "行业与市场观点审核"
        ),
        "标的观点审核": source_review_status_for_update(existing_fields, "标的观点审核"),
    }
    if not existing:
        fields["源纪要审核"] = "未审核"
        fields["行业与市场观点审核"] = "未审核"
        fields["标的观点审核"] = "未审核"
    return fields


def deterministic_client_token(seed: str) -> str:
    digest = hashlib.sha256(seed.encode("utf-8")).digest()[:16]
    return str(uuid.UUID(bytes=digest, version=4))


def create_meeting_record(
    config: Config,
    tenant_access_token: str,
    fields: dict[str, Any],
    meeting_uid: str,
) -> str:
    params = {
        "client_token": deterministic_client_token(
            f"meeting-ingestion:{config.meeting_base_app_token}:{config.meeting_base_table_id}:{meeting_uid}"
        )
    }
    url = (
        f"{config.openapi_base}/bitable/v1/apps/"
        f"{urllib.parse.quote(config.meeting_base_app_token, safe='')}/tables/"
        f"{urllib.parse.quote(config.meeting_base_table_id, safe='')}/records?"
        f"{urllib.parse.urlencode(params)}"
    )
    try:
        result = feishu_request_json(
            "POST",
            url,
            headers={
                "Authorization": f"Bearer {tenant_access_token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            body=json.dumps({"fields": fields}, ensure_ascii=False).encode("utf-8"),
        )
        record = result.get("data", {}).get("record", {})
        record_id = str(record.get("record_id") or "") if isinstance(record, dict) else ""
        if record_id:
            return record_id
    except UploadError:
        pass
    reconciled = find_meeting_record(config, tenant_access_token, meeting_uid)
    record_id = str(reconciled.get("record_id") or "") if reconciled else ""
    if not record_id:
        raise UploadError(
            "ingestion_outcome_uncertain",
            "Meeting Base create outcome is not confirmed; retry with the same Idempotency-Key.",
            503,
        )
    return record_id


def update_meeting_record(
    config: Config,
    tenant_access_token: str,
    record_id: str,
    fields: dict[str, Any],
    meeting_uid: str,
) -> None:
    url = (
        f"{config.openapi_base}/bitable/v1/apps/"
        f"{urllib.parse.quote(config.meeting_base_app_token, safe='')}/tables/"
        f"{urllib.parse.quote(config.meeting_base_table_id, safe='')}/records/"
        f"{urllib.parse.quote(record_id, safe='')}"
    )
    try:
        feishu_request_json(
            "PUT",
            url,
            headers={
                "Authorization": f"Bearer {tenant_access_token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            body=json.dumps({"fields": fields}, ensure_ascii=False).encode("utf-8"),
        )
    except UploadError:
        reconciled = find_meeting_record(config, tenant_access_token, meeting_uid)
        if not reconciled or str(reconciled.get("record_id") or "") != record_id:
            raise UploadError(
                "ingestion_outcome_uncertain",
                "Meeting Base update outcome is not confirmed; retry with the same Idempotency-Key.",
                503,
            ) from None
        current_fields = reconciled.get("fields", {})
        if not isinstance(current_fields, dict) or plain_bitable_value(
            current_fields.get("数据版本")
        ) != str(fields["数据版本"]) or plain_bitable_value(
            current_fields.get("会议纪要MD")
        ) != str(fields["会议纪要MD"]["link"]):
            raise UploadError(
                "ingestion_outcome_uncertain",
                "Meeting Base update is not yet visible; retry with the same Idempotency-Key.",
                503,
            ) from None


def commit_meeting_record(
    config: Config,
    tenant_access_token: str,
    receipt: dict[str, Any],
    registry: dict[str, Any] | None,
) -> str:
    existing = find_meeting_record(config, tenant_access_token, str(receipt["meeting_uid"]))
    fields = meeting_record_fields(receipt, existing)
    if registry is None:
        if existing is not None:
            existing_fields = existing.get("fields", {})
            record_id = str(existing.get("record_id") or "")
            if (
                not record_id
                or not isinstance(existing_fields, dict)
                or plain_bitable_value(existing_fields.get("数据版本"))
                != str(receipt["data_version"])
                or plain_bitable_value(existing_fields.get("会议纪要MD"))
                != str(receipt["url"])
            ):
                raise UploadError(
                    "meeting_record_conflict",
                    "Meeting UID already exists in Base with conflicting content.",
                    409,
                )
            return record_id
        return create_meeting_record(
            config, tenant_access_token, fields, str(receipt["meeting_uid"])
        )
    expected_record_id = str(registry.get("record_id") or "")
    if not expected_record_id or not existing:
        raise UploadError("meeting_registry_conflict", "Meeting registry/Base binding is missing.", 409)
    if str(existing.get("record_id") or "") != expected_record_id:
        raise UploadError("meeting_registry_conflict", "Meeting registry/Base binding conflicts.", 409)
    update_meeting_record(
        config,
        tenant_access_token,
        expected_record_id,
        fields,
        str(receipt["meeting_uid"]),
    )
    return expected_record_id


def enqueue_generation_jobs(config: Config, receipt: dict[str, Any]) -> list[str]:
    pending = config.generation_job_spool_path / "pending"
    pending.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(pending, 0o700)
    queued: list[str] = []
    for artifact_type in GENERATION_ARTIFACT_TYPES:
        job_id = (
            f"{receipt['meeting_uid']}-v{receipt['data_version']}-{artifact_type}"
        )
        job = {
            "job_version": 1,
            "job_id": job_id,
            "state": "pending",
            "meeting_uid": receipt["meeting_uid"],
            "record_id": receipt["record_id"],
            "artifact_type": artifact_type,
            "data_version": receipt["data_version"],
            "input_file_token": receipt["file_token"],
            "input_md_sha256": receipt["content_sha256"],
            "meeting_date": receipt["meeting_date"],
            "meeting_series": receipt["meeting_series"],
            "meeting_type": receipt["meeting_type"],
            "source_review_status": "未审核",
            "created_at": receipt["created_at"],
        }
        path = pending / f"{job_id}.json"
        existing = _load_private_json(path, "generation_job")
        if existing is not None:
            if existing != job:
                raise UploadError(
                    "generation_job_conflict",
                    "Existing generation job does not match the ingestion receipt.",
                    409,
                )
        else:
            try:
                atomic_private_write(
                    path, json.dumps(job, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
                )
            except (AtomicCommitUncertain, OSError) as exc:
                raise UploadError(
                    "ingestion_outcome_uncertain",
                    "Generation job durability is uncertain; retry with the same Idempotency-Key.",
                    503,
                ) from exc
        queued.append(artifact_type)
    return queued


def completed_payload(
    record: dict[str, Any],
    *,
    request_id: str,
    idempotency_status: str,
) -> dict[str, Any]:
    required = (
        "uploader",
        "original_file_name",
        "uploaded_file_name",
        "month",
        "month_folder_token",
        "file_token",
        "url",
    )
    if any(not isinstance(record.get(key), str) for key in required):
        raise UploadError("idempotency_record_invalid", "Completed idempotency record is incomplete.", 500)
    return success_payload(
        status="uploaded",
        uploader=str(record["uploader"]),
        original_file_name=str(record["original_file_name"]),
        uploaded_file_name=str(record["uploaded_file_name"]),
        month=str(record["month"]),
        parent_folder_token=str(record.get("parent_folder_token") or ""),
        month_folder_token=str(record["month_folder_token"]),
        file_token=str(record["file_token"]),
        url=str(record["url"]),
        request_id=request_id,
        idempotency_status=idempotency_status,
    )


def reconcile_in_flight_upload(
    config: Config,
    tenant_access_token: str,
    record_path: Path,
    record: dict[str, Any],
    *,
    request_id: str,
) -> dict[str, Any]:
    month_folder_token = str(record.get("month_folder_token") or "")
    uploaded_file_name = str(record.get("uploaded_file_name") or "")
    expected_sha = str(record.get("content_sha256") or "")
    if (
        not month_folder_token
        or not uploaded_file_name
        or not re.fullmatch(r"[0-9a-f]{64}", expected_sha)
    ):
        raise UploadError("idempotency_record_invalid", "In-flight idempotency record is incomplete.", 500)
    try:
        items = list_folder(config, tenant_access_token, month_folder_token)
    except Exception as exc:
        raise UploadError(
            "idempotency_outcome_uncertain",
            "Upload result is not confirmed; retry with the same Idempotency-Key.",
            503,
        ) from exc
    matches = [
        item
        for item in items
        if item.get("type") == "file"
        and item.get("name") == uploaded_file_name
        and item.get("token")
    ]
    if not matches:
        raise UploadError(
            "idempotency_outcome_uncertain",
            "Upload result is not yet visible; retry with the same Idempotency-Key.",
            503,
        )
    if len(matches) > 1:
        raise UploadError(
            "idempotency_reconciliation_ambiguous",
            "Multiple remote files match the persisted upload intent.",
            409,
        )
    match = matches[0]
    file_token = str(match["token"])
    try:
        content = download_drive_file(config, tenant_access_token, file_token)
    except Exception as exc:
        if isinstance(exc, UploadError) and exc.error_code == "idempotency_reconciliation_conflict":
            raise
        raise UploadError(
            "idempotency_outcome_uncertain",
            "Remote upload content could not be verified; retry with the same Idempotency-Key.",
            503,
        ) from exc
    if hashlib.sha256(content).hexdigest() != expected_sha:
        raise UploadError(
            "idempotency_reconciliation_conflict",
            "Remote file content does not match the persisted upload intent.",
            409,
        )
    transfer_output_owner(config, tenant_access_token, file_token)
    url = str(match.get("url") or config.file_url_base.rstrip("/") + "/" + file_token)
    completed = dict(record)
    completed.update(
        {
            "status": "completed",
            "file_token": file_token,
            "url": url,
            "completed_at": utc_now(),
        }
    )
    save_completed_idempotency_record(record_path, completed)
    return completed_payload(completed, request_id=request_id, idempotency_status="reconciled")


def idempotent_upload(
    config: Config,
    user: dict[str, Any],
    key: str,
    tenant_access_token: str,
    month: str,
    month_folder_token: str,
    upload: UploadedFile,
    request_id: str,
) -> dict[str, Any]:
    user_id = str(user.get("user_id") or "").strip()
    if not user_id:
        raise UploadError("user_db_invalid", "Authenticated upload user has no stable user ID.", 500)
    uploader = str(user.get("name") or user_id)
    content_sha256 = hashlib.sha256(upload.data).hexdigest()
    fingerprint = idempotency_fingerprint(
        user_id=user_id,
        file_name=upload.file_name,
        content_sha256=content_sha256,
        month=month,
        parent_folder_token=config.parent_folder_token,
    )

    with idempotency_lock(config, user_id, key) as record_path:
        record = load_idempotency_record(record_path)
        if record is not None:
            if not hmac.compare_digest(str(record.get("fingerprint") or ""), fingerprint):
                raise UploadError(
                    "idempotency_key_conflict",
                    "Idempotency-Key was already used for a different upload request.",
                    409,
                )
            if record["status"] == "completed":
                return completed_payload(record, request_id=request_id, idempotency_status="replayed")
            return reconcile_in_flight_upload(
                config,
                tenant_access_token,
                record_path,
                record,
                request_id=request_id,
            )

        with upload_month_lock(config, month):
            month_items = list_folder(config, tenant_access_token, month_folder_token)
            existing_names = {str(item.get("name")) for item in month_items if item.get("name")}
            uploaded_file_name = unique_upload_name(upload.file_name, existing_names)
            record = {
                "version": IDEMPOTENCY_RECORD_VERSION,
                "status": "in_flight",
                "fingerprint": fingerprint,
                "content_sha256": content_sha256,
                "uploader": uploader,
                "original_file_name": upload.file_name,
                "uploaded_file_name": uploaded_file_name,
                "month": month,
                "parent_folder_token": tail_token(config.parent_folder_token),
                "month_folder_token": month_folder_token,
                "first_request_id": request_id,
                "created_at": utc_now(),
            }
            save_idempotency_record(record_path, record)
            try:
                file_token = upload_file(
                    config,
                    tenant_access_token,
                    month_folder_token,
                    uploaded_file_name,
                    upload.data,
                )
                url = resolve_uploaded_url(
                    config,
                    tenant_access_token,
                    month_folder_token,
                    uploaded_file_name,
                    file_token,
                )
                transfer_output_owner(config, tenant_access_token, file_token)
            except Exception:
                return reconcile_in_flight_upload(
                    config,
                    tenant_access_token,
                    record_path,
                    record,
                    request_id=request_id,
                )
            completed = dict(record)
            completed.update(
                {
                    "status": "completed",
                    "file_token": file_token,
                    "url": url,
                    "completed_at": utc_now(),
                }
            )
            save_completed_idempotency_record(record_path, completed)
            return completed_payload(completed, request_id=request_id, idempotency_status="created")


def pipeline_resources_ready(config: Config) -> bool:
    try:
        load_pipeline_contract(config.pipeline_contract_path)
    except UploadError:
        return False
    return bool(
        config.parent_folder_token
        and config.baseline_parent_folder_token
        and config.meeting_base_app_token
        and config.meeting_base_table_id
        and not config.generation_job_spool_path.is_symlink()
        and not config.meeting_registry_path.is_symlink()
    )


def ingestion_success_payload(
    receipt: dict[str, Any],
    *,
    request_id: str,
    idempotency_status: str,
) -> dict[str, Any]:
    required = (
        "meeting_uid",
        "record_id",
        "data_version",
        "original_file_name",
        "normalized_file_name",
        "file_token",
        "url",
        "operation",
    )
    if any(key not in receipt for key in required):
        raise UploadError("ingestion_receipt_invalid", "Completed ingestion receipt is incomplete.", 500)
    operation = str(receipt["operation"])
    status = "unchanged" if operation == "skipped_unchanged" else operation
    return {
        "ok": True,
        "status": status,
        "meeting_uid": str(receipt["meeting_uid"]),
        "record_id": str(receipt["record_id"]),
        "data_version": int(receipt["data_version"]),
        "original_file_name": str(receipt["original_file_name"]),
        "normalized_file_name": str(receipt["normalized_file_name"]),
        "file_token": str(receipt["file_token"]),
        "url": str(receipt["url"]),
        "generation_queued": list(receipt.get("generation_queued") or []),
        "idempotency_status": idempotency_status,
        "request_id": request_id,
    }


def _validate_registry_identity(registry: dict[str, Any], metadata: IngestionMetadata) -> None:
    expected = {
        "meeting_date": metadata.meeting_date,
        "meeting_series": metadata.meeting_series,
        "meeting_type": metadata.meeting_type,
    }
    conflicts = [name for name, value in expected.items() if registry.get(name) != value]
    if conflicts:
        raise UploadError(
            "meeting_metadata_conflict",
            f"Existing meeting metadata differs: {', '.join(conflicts)}.",
            409,
        )


def _completed_unchanged_receipt(
    *,
    receipt_path: Path,
    fingerprint: str,
    user: dict[str, Any],
    upload: UploadedFile,
    registry: dict[str, Any],
    request_id: str,
) -> dict[str, Any]:
    receipt = {
        "version": INGESTION_RECEIPT_VERSION,
        "status": "completed",
        "fingerprint": fingerprint,
        "operation": "skipped_unchanged",
        "uploader": str(user.get("name") or user.get("user_id") or ""),
        "original_file_name": upload.file_name,
        "meeting_uid": registry["meeting_uid"],
        "meeting_date": registry["meeting_date"],
        "meeting_series": registry["meeting_series"],
        "meeting_type": registry["meeting_type"],
        "data_version": registry["data_version"],
        "normalized_file_name": registry["normalized_file_name"],
        "content_sha256": registry["source_md_sha256"],
        "file_token": registry["file_token"],
        "url": registry["url"],
        "baseline_file_token": registry["baseline_file_token"],
        "baseline_url": registry["baseline_url"],
        "record_id": registry["record_id"],
        "generation_queued": [],
        "first_request_id": request_id,
        "created_at": utc_now(),
        "completed_at": utc_now(),
    }
    save_ingestion_receipt(receipt_path, receipt)
    return receipt


def _prepare_ingestion_receipt(
    *,
    config: Config,
    tenant_access_token: str,
    receipt_path: Path,
    user: dict[str, Any],
    upload: UploadedFile,
    raw_metadata: dict[str, str],
    fingerprint: str,
    request_id: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    provisional = validate_ingestion_metadata(
        config,
        raw_metadata,
        data_version=1,
        generate_uid=True,
    )
    registry = load_meeting_registry(config, provisional.meeting_uid)
    content_sha256 = hashlib.sha256(upload.data).hexdigest()
    if registry is not None:
        _validate_registry_identity(registry, provisional)
        if hmac.compare_digest(str(registry["source_md_sha256"]), content_sha256):
            return (
                _completed_unchanged_receipt(
                    receipt_path=receipt_path,
                    fingerprint=fingerprint,
                    user=user,
                    upload=upload,
                    registry=registry,
                    request_id=request_id,
                ),
                registry,
            )
        data_version = int(registry["data_version"]) + 1
        operation = "updated"
    else:
        data_version = 1
        operation = "created"
    metadata = validate_ingestion_metadata(
        config,
        {
            **raw_metadata,
            "meeting_uid": provisional.meeting_uid,
        },
        data_version=data_version,
        generate_uid=False,
    )
    parent_items = list_folder(config, tenant_access_token, config.parent_folder_token)
    month_folder = find_month_folder(parent_items, metadata.month)
    baseline_parent_items = list_folder(
        config, tenant_access_token, config.baseline_parent_folder_token
    )
    baseline_month_folder = find_month_folder(baseline_parent_items, metadata.month)
    baseline_file_name = f"{Path(metadata.normalized_file_name).stem} - 审核前.md"
    receipt = {
        "version": INGESTION_RECEIPT_VERSION,
        "status": "prepared",
        "fingerprint": fingerprint,
        "operation": operation,
        "uploader": str(user.get("name") or user.get("user_id") or ""),
        "original_file_name": upload.file_name,
        "content_sha256": content_sha256,
        "meeting_uid": metadata.meeting_uid,
        "meeting_date": metadata.meeting_date,
        "meeting_series": metadata.meeting_series,
        "meeting_type": metadata.meeting_type,
        "data_version": metadata.data_version,
        "month": metadata.month,
        "normalized_file_name": metadata.normalized_file_name,
        "month_folder_token": str(month_folder["token"]),
        "baseline_month_folder_token": str(baseline_month_folder["token"]),
        "baseline_file_name": baseline_file_name,
        "first_request_id": request_id,
        "created_at": utc_now(),
    }
    save_ingestion_receipt(receipt_path, receipt)
    return receipt, registry


def _registry_from_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": MEETING_REGISTRY_VERSION,
        "meeting_uid": receipt["meeting_uid"],
        "meeting_date": receipt["meeting_date"],
        "meeting_series": receipt["meeting_series"],
        "meeting_type": receipt["meeting_type"],
        "data_version": receipt["data_version"],
        "normalized_file_name": receipt["normalized_file_name"],
        "source_md_sha256": receipt["content_sha256"],
        "file_token": receipt["file_token"],
        "url": receipt["url"],
        "baseline_file_token": receipt["baseline_file_token"],
        "baseline_url": receipt["baseline_url"],
        "record_id": receipt["record_id"],
        "updated_at": utc_now(),
    }


def _continue_ingestion(
    *,
    config: Config,
    tenant_access_token: str,
    receipt_path: Path,
    receipt: dict[str, Any],
    upload: UploadedFile,
) -> dict[str, Any]:
    registry = load_meeting_registry(config, str(receipt["meeting_uid"]))
    if receipt["status"] == "prepared":
        if receipt.get("drive_attempted_at"):
            file_token, url = find_drive_file_by_name_and_hash(
                config,
                tenant_access_token,
                str(receipt["month_folder_token"]),
                str(receipt["normalized_file_name"]),
                str(receipt["content_sha256"]),
            )
        else:
            receipt["drive_attempted_at"] = utc_now()
            save_ingestion_receipt(receipt_path, receipt)
            file_token, url = upload_file_confirmed(
                config,
                tenant_access_token,
                str(receipt["month_folder_token"]),
                str(receipt["normalized_file_name"]),
                upload.data,
            )
        receipt.update(
            {"status": "drive_uploaded", "file_token": file_token, "url": url}
        )
        save_post_effect_ingestion_receipt(receipt_path, receipt)

    if receipt["status"] == "drive_uploaded":
        if receipt.get("baseline_attempted_at"):
            baseline_token, baseline_url = find_drive_file_by_name_and_hash(
                config,
                tenant_access_token,
                str(receipt["baseline_month_folder_token"]),
                str(receipt["baseline_file_name"]),
                str(receipt["content_sha256"]),
            )
        else:
            receipt["baseline_attempted_at"] = utc_now()
            save_ingestion_receipt(receipt_path, receipt)
            baseline_token, baseline_url = copy_file_confirmed(
                config,
                tenant_access_token,
                str(receipt["file_token"]),
                str(receipt["baseline_month_folder_token"]),
                str(receipt["baseline_file_name"]),
                str(receipt["content_sha256"]),
            )
        receipt.update(
            {
                "status": "baseline_captured",
                "baseline_file_token": baseline_token,
                "baseline_url": baseline_url,
            }
        )
        save_post_effect_ingestion_receipt(receipt_path, receipt)

    if receipt["status"] == "baseline_captured":
        record_id = commit_meeting_record(config, tenant_access_token, receipt, registry)
        receipt.update({"status": "base_committed", "record_id": record_id})
        save_post_effect_ingestion_receipt(receipt_path, receipt)

    if receipt["status"] == "base_committed":
        queued = enqueue_generation_jobs(config, receipt)
        receipt.update({"status": "jobs_queued", "generation_queued": queued})
        save_post_effect_ingestion_receipt(receipt_path, receipt)

    if receipt["status"] == "jobs_queued":
        new_registry = _registry_from_receipt(receipt)
        current_registry = load_meeting_registry(config, str(receipt["meeting_uid"]))
        if current_registry is not None and int(current_registry["data_version"]) > int(
            new_registry["data_version"]
        ):
            raise UploadError(
                "stale_ingestion",
                "A newer meeting version already exists; stale ingestion cannot become current.",
                409,
            )
        save_meeting_registry(config, new_registry)
        receipt.update({"status": "completed", "completed_at": utc_now()})
        save_post_effect_ingestion_receipt(receipt_path, receipt)
    return receipt


def idempotent_ingestion(
    config: Config,
    user: dict[str, Any],
    key: str,
    tenant_access_token: str,
    raw_metadata: dict[str, str],
    upload: UploadedFile,
    request_id: str,
) -> dict[str, Any]:
    if not pipeline_resources_ready(config):
        raise UploadError(
            "pipeline_config_invalid",
            "Meeting ingestion pipeline resources are not configured.",
            500,
        )
    user_id = str(user.get("user_id") or "").strip()
    if not user_id:
        raise UploadError("user_db_invalid", "Authenticated upload user has no stable user ID.", 500)
    preliminary = validate_ingestion_metadata(
        config, raw_metadata, data_version=1, generate_uid=False
    )
    fingerprint = ingestion_fingerprint(
        user_id=user_id,
        original_file_name=upload.file_name,
        content_sha256=hashlib.sha256(upload.data).hexdigest(),
        raw_metadata={
            **raw_metadata,
            "meeting_date": preliminary.meeting_date,
            "meeting_series": preliminary.meeting_series,
            "meeting_type": preliminary.meeting_type,
            "meeting_uid": preliminary.meeting_uid,
        },
        config=config,
    )
    with ingestion_receipt_lock(config, user_id, key) as receipt_path:
        receipt = load_ingestion_receipt(receipt_path)
        resumed = receipt is not None
        if receipt is not None:
            if not hmac.compare_digest(str(receipt["fingerprint"]), fingerprint):
                raise UploadError(
                    "idempotency_key_conflict",
                    "Idempotency-Key was already used for a different ingestion request.",
                    409,
                )
            if receipt["status"] == "completed":
                return ingestion_success_payload(
                    receipt, request_id=request_id, idempotency_status="replayed"
                )
            meeting_uid = str(receipt["meeting_uid"])
        else:
            meeting_uid = preliminary.meeting_uid or load_pipeline_contract(
                config.pipeline_contract_path
            ).new_meeting_uid()
        with meeting_uid_lock(config, meeting_uid):
            if receipt is None:
                receipt, _registry = _prepare_ingestion_receipt(
                    config=config,
                    tenant_access_token=tenant_access_token,
                    receipt_path=receipt_path,
                    user=user,
                    upload=upload,
                    raw_metadata={**raw_metadata, "meeting_uid": meeting_uid},
                    fingerprint=fingerprint,
                    request_id=request_id,
                )
                if receipt["status"] == "completed":
                    return ingestion_success_payload(
                        receipt, request_id=request_id, idempotency_status="created"
                    )
            completed = _continue_ingestion(
                config=config,
                tenant_access_token=tenant_access_token,
                receipt_path=receipt_path,
                receipt=receipt,
                upload=upload,
            )
        return ingestion_success_payload(
            completed,
            request_id=request_id,
            idempotency_status="reconciled" if resumed else "created",
        )


def parse_multipart_form(content_type: str, body: bytes) -> tuple[dict[str, str], UploadedFile]:
    content_type_header = email.message.Message(policy=email.policy.default)
    content_type_header["Content-Type"] = content_type
    if content_type_header.get_content_type().lower() != "multipart/form-data":
        raise UploadError("invalid_content_type", "Content-Type must be multipart/form-data.", 415)
    if not content_type_header.get_param("boundary", header="content-type"):
        raise UploadError("invalid_multipart", "Multipart boundary is required.")
    header = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8")
    message = email.parser.BytesParser(policy=email.policy.default).parsebytes(header + body)
    if not message.is_multipart():
        raise UploadError("invalid_multipart", "Request body is not valid multipart data.")

    fields: dict[str, str] = {}
    upload_file_value: UploadedFile | None = None
    for part in message.iter_parts():
        if part.is_multipart():
            raise UploadError("invalid_multipart", "Nested multipart data is not allowed.")
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if filename is not None:
            if name != "file":
                raise UploadError("unexpected_file_field", "Only multipart field 'file' may contain a file.")
            if upload_file_value is not None:
                raise UploadError("multiple_files", "Exactly one file is allowed.")
            upload_file_value = UploadedFile(
                field_name=name,
                file_name=sanitize_file_name(filename),
                content_type=part.get_content_type(),
                data=payload,
            )
        else:
            if name in fields:
                raise UploadError("duplicate_field", "Duplicate multipart fields are not allowed.")
            fields[name] = payload.decode(part.get_content_charset() or "utf-8", errors="replace")

    if upload_file_value is None:
        raise UploadError("missing_file", "Multipart field 'file' is required.")
    if upload_file_value.field_name != "file":
        raise UploadError("missing_file", "Multipart field 'file' is required.")
    return fields, upload_file_value


def request_body_metadata(headers: http.client.HTTPMessage, max_upload_bytes: int) -> tuple[int, str]:
    if headers.get_all("Transfer-Encoding", []):
        raise UploadError("unsupported_transfer_encoding", "Transfer-Encoding is not supported.", 400)
    length_values = headers.get_all("Content-Length", [])
    if not length_values:
        raise UploadError("missing_content_length", "Content-Length is required.", 411)
    if len(length_values) != 1:
        raise UploadError("ambiguous_content_length", "Exactly one Content-Length header is required.", 400)
    try:
        request_size = int(length_values[0])
    except ValueError as exc:
        raise UploadError("invalid_content_length", "Content-Length is invalid.") from exc
    if request_size <= 0:
        raise UploadError("invalid_content_length", "Content-Length must be positive.")
    if request_size > max_upload_bytes + 1024 * 1024:
        raise UploadError("file_too_large", "Request exceeds the configured size limit.", 413)

    type_values = headers.get_all("Content-Type", [])
    if len(type_values) != 1:
        raise UploadError("invalid_content_type", "Exactly one Content-Type header is required.", 415)
    return request_size, type_values[0]


def read_exact_body(stream: Any, request_size: int) -> bytes:
    body = stream.read(request_size)
    if len(body) != request_size:
        raise UploadError("incomplete_request_body", "Request body ended before Content-Length bytes.", 400)
    return body


def extract_upload_token(headers: http.client.HTTPMessage) -> str:
    authorization_values = headers.get_all("Authorization", [])
    legacy_values = headers.get_all("X-Upload-Token", [])
    if len(authorization_values) > 1 or len(legacy_values) > 1 or (authorization_values and legacy_values):
        raise UploadError(
            "ambiguous_authentication",
            "Exactly one upload authentication header is required.",
            400,
        )
    if authorization_values:
        auth = authorization_values[0].strip()
        if not auth.lower().startswith("bearer "):
            return ""
        return auth[7:].strip()
    return legacy_values[0].strip() if legacy_values else ""


class UploadHandler(http.server.BaseHTTPRequestHandler):
    server_version = "FeishuMinutesUpload/0.3.0"
    config: Config

    def log_message(self, format: str, *args: Any) -> None:
        # Request paths, query strings, client addresses, and header values are
        # intentionally excluded from normal logs.
        return

    def send_json(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        request_id = uuid.uuid4().hex
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path not in {"/healthz", "/readyz"}:
            self.send_json(404, error_payload("not_found", "Not found.", request_id))
            return
        readiness = local_readiness(self.config)
        self.send_json(
            200 if readiness["ready"] else 503,
            {
                "ok": readiness["ready"],
                "ready": readiness["ready"],
                "service": SERVICE_NAME,
                "checks": {
                    "credentials": readiness["credentials"],
                    "resource": readiness["resource"],
                    "meeting_contract": readiness["contract"],
                    "meeting_pipeline": readiness["pipeline"],
                    "upload_users": readiness["user_db"],
                    "output_owner_configured": bool(self.config.output_owner_open_id),
                },
                "request_id": request_id,
            },
        )

    def do_POST(self) -> None:
        request_id = uuid.uuid4().hex
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/api/upload":
            self.send_json(404, error_payload("not_found", "Not found.", request_id))
            return
        try:
            payload = self.handle_upload(parsed, request_id)
            self.send_json(200, payload)
            logging.info("request_id=%s status=%s", request_id, payload.get("status"))
        except UploadError as exc:
            payload = error_payload(exc.error_code, exc.message, request_id)
            if exc.error_code in {
                "idempotency_outcome_uncertain",
                "idempotency_store_uncertain",
                "ingestion_outcome_uncertain",
            }:
                payload["status"] = "outcome_uncertain"
            self.send_json(exc.http_status, payload)
            logging.warning("request_id=%s error_code=%s", request_id, exc.error_code)
        except Exception:
            self.send_json(500, error_payload("internal_error", "Internal server error.", request_id))
            logging.error("request_id=%s error_code=internal_error", request_id)

    def handle_upload(self, parsed: urllib.parse.ParseResult, request_id: str) -> dict[str, Any]:
        config = self.config
        token = extract_upload_token(self.headers)
        user = authenticate_upload_token(token, config.user_db_path)
        idempotency_key = extract_idempotency_key(self.headers)

        request_size, content_type = request_body_metadata(self.headers, config.max_upload_bytes)
        body = read_exact_body(self.rfile, request_size)
        fields, upload = parse_multipart_form(content_type, body)
        validate_markdown_file(upload.file_name, upload.data, config.max_upload_bytes)

        query = urllib.parse.parse_qs(parsed.query)
        dry_run = parse_bool(first(query.get("dry_run")) or fields.get("dry_run"))
        raw_metadata = request_metadata_fields(fields, query)
        preview = validate_ingestion_metadata(
            config,
            raw_metadata,
            data_version=1,
            generate_uid=False,
        )

        if not dry_run and not idempotency_key:
            raise UploadError(
                "missing_idempotency_key",
                "Idempotency-Key is required for every ingestion write.",
                400,
            )

        tenant_access_token = get_tenant_access_token(config)
        if dry_run:
            if not pipeline_resources_ready(config):
                raise UploadError(
                    "pipeline_config_invalid",
                    "Meeting ingestion pipeline resources are not configured.",
                    500,
                )
            current_month = find_month_folder(
                list_folder(config, tenant_access_token, config.parent_folder_token),
                preview.month,
            )
            baseline_month = find_month_folder(
                list_folder(config, tenant_access_token, config.baseline_parent_folder_token),
                preview.month,
            )
            return {
                "ok": True,
                "status": "dry_run",
                "meeting_uid": preview.meeting_uid,
                "data_version": preview.data_version,
                "original_file_name": upload.file_name,
                "normalized_file_name": preview.normalized_file_name,
                "month": preview.month,
                "month_folder_token_tail": tail_token(str(current_month["token"])),
                "baseline_month_folder_token_tail": tail_token(str(baseline_month["token"])),
                "idempotency_status": "dry_run" if idempotency_key else "",
                "request_id": request_id,
            }
        return idempotent_ingestion(
            config,
            user,
            idempotency_key,
            tenant_access_token,
            raw_metadata,
            upload,
            request_id,
        )


def first(values: list[str] | None) -> str | None:
    if not values:
        return None
    return values[0]


def success_payload(
    status: str,
    uploader: str,
    original_file_name: str,
    uploaded_file_name: str,
    month: str,
    parent_folder_token: str,
    month_folder_token: str,
    file_token: str,
    url: str,
    request_id: str,
    idempotency_status: str = "",
) -> dict[str, Any]:
    payload = {
        "ok": True,
        "status": status,
        "uploader": uploader,
        "original_file_name": original_file_name,
        "uploaded_file_name": uploaded_file_name,
        "duplicate_strategy": "rename",
        "month": month,
        "parent_folder_token_tail": tail_token(parent_folder_token),
        "month_folder_token_tail": tail_token(month_folder_token),
        "file_token": file_token,
        "url": url,
        "request_id": request_id,
    }
    if idempotency_status:
        payload["idempotency_status"] = idempotency_status
    return payload


def error_payload(error_code: str, message: str, request_id: str) -> dict[str, Any]:
    return {"ok": False, "error_code": error_code, "message": message, "request_id": request_id}


def make_handler(config: Config) -> type[UploadHandler]:
    class ConfiguredUploadHandler(UploadHandler):
        pass

    ConfiguredUploadHandler.config = config
    return ConfiguredUploadHandler


def serve(args: argparse.Namespace) -> int:
    config = Config.from_env(args.users_path)
    if not local_readiness(config)["ready"]:
        raise UploadError(
            "service_not_ready",
            "Local service configuration is incomplete.",
            500,
        )
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    server = http.server.ThreadingHTTPServer((args.host, args.port), make_handler(config))
    logging.info("service=%s host=%s port=%s", SERVICE_NAME, args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logging.info("service=%s shutdown=keyboard_interrupt", SERVICE_NAME)
    finally:
        server.server_close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="feishu_upload_service.py")
    parser.add_argument("--users-path", help="Override upload user database path.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor_parser = subparsers.add_parser("doctor", help="Check local configuration without printing secrets.")
    doctor_parser.set_defaults(func=doctor)

    users_parser = subparsers.add_parser("users", help="Manage upload users.")
    users_sub = users_parser.add_subparsers(dest="users_command", required=True)

    add_parser = users_sub.add_parser("add", help="Create an upload user and token.")
    add_parser.add_argument("--name", required=True)
    add_parser.add_argument("--source", required=True)
    add_parser.add_argument("--write-token-file")
    add_parser.set_defaults(func=create_user)

    list_parser = users_sub.add_parser("list", help="List upload users without token material.")
    list_parser.set_defaults(func=list_users)

    disable_parser = users_sub.add_parser("disable", help="Disable an upload user.")
    disable_parser.add_argument("user_id")
    disable_parser.set_defaults(func=disable_user)

    rotate_parser = users_sub.add_parser("rotate", help="Rotate an upload user's token.")
    rotate_parser.add_argument("user_id")
    rotate_parser.add_argument("--write-token-file")
    rotate_parser.set_defaults(func=rotate_user)

    serve_parser = subparsers.add_parser("serve", help="Run the upload API server.")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8789)
    serve_parser.add_argument("--apply", action="store_true", help="required because uploads write Drive")
    serve_parser.set_defaults(func=serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        argv = ["serve"]
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "serve" and not args.apply:
        raise SystemExit("serve requires explicit --apply")
    started = time.time()
    try:
        return int(args.func(args))
    except UploadError as exc:
        print_json({"ok": False, "error_code": exc.error_code, "message": exc.message})
        return 1
    finally:
        _ = started


if __name__ == "__main__":
    raise SystemExit(main())
