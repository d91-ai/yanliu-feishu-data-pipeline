#!/usr/bin/env python3
"""Independent Feishu workflow service for reviewed minute sanitization."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import re
import secrets
import tempfile
import threading
import time
from typing import Any, Iterator, Mapping, Sequence
import urllib.parse

from feishu_gateway import (
    FeishuOpenApiGateway,
    FeishuSettings,
    GatewayError,
    WorkflowGateway,
    deterministic_client_token,
)
from skill_adapter import CliSkillAdapter, DoctorReport, SkillAdapter, SkillContractError, sha256_hex
from source_contract_adapter import (
    SourceContractError,
    adapt_source_contract,
    pipeline_rules_version,
)


SERVICE_NAME = "feishu-minute-sanitize"
SERVICE_VERSION = "0.4.3"

SOURCE_STATUS = "脱敏生成状态"
SOURCE_LINK = "脱敏MD链接"
SOURCE_TIME = "脱敏生成时间"
SOURCE_ERROR = "脱敏生成错误"

STATUS_PENDING = "待生成"
STATUS_RUNNING = "生成中"
STATUS_GENERATED = "已生成"
STATUS_FAILED = "生成失败"

FIELD_PRIMARY = "脱敏纪要"
FIELD_SOURCE_ID = "来源记录ID"
FIELD_SOURCE_LINK = "来源归档链接"
FIELD_SOURCE_SHA = "来源审核后SHA256"
FIELD_MEETING_DATE = "会议日期"
FIELD_IDEMPOTENCY = "幂等键"
FIELD_RULES_VERSION = "脱敏规则版本"
FIELD_MD_STATUS = "MD生成状态"
FIELD_MD_LINK = "脱敏MD链接"
FIELD_MD_TIME = "MD生成时间"
FIELD_QUALITY = "质量检查状态"
FIELD_REVIEW = "审核状态"
FIELD_ARCHIVE_STATUS = "归档状态"
FIELD_ARCHIVE_LINK = "归档链接"
FIELD_ARCHIVE_TIME = "归档时间"
FIELD_BASELINE_LINK = "审核前版本链接"
FIELD_BASELINE_VERSION = "审核前文件版本号"
FIELD_BASELINE_SHA = "审核前内容SHA256"
FIELD_APPROVED_VERSION = "审核后文件版本号"
FIELD_APPROVED_SHA = "审核后内容SHA256"
FIELD_VERSION_DIFF = "版本差异"
FIELD_VERSION_STATUS = "版本留存状态"
FIELD_VERSION_ERROR = "版本留存错误"
FIELD_ERROR_STAGE = "错误阶段"
FIELD_ERROR = "错误信息"

TARGET_CREATE_RECONCILE_DELAYS = (0.0, 0.2, 0.5, 1.0)


class WorkflowError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        http_status: int = 409,
        *,
        response_status: str = "",
    ):
        super().__init__(message)
        self.code = code
        self.safe_message = message
        self.http_status = http_status
        self.response_status = response_status


@dataclass(frozen=True)
class ServiceConfig:
    pending_root_token: str
    archive_root_token: str
    version_root_token: str
    contract_version: str = "minute-sanitization/v2"
    source_cutoff_ms: int = 0
    state_dir: Path = Path("data/state")
    max_input_bytes: int = 10 * 1024 * 1024
    max_error_chars: int = 300


@dataclass(frozen=True)
class RuntimeConfig:
    service: ServiceConfig
    feishu: FeishuSettings
    skill_command: tuple[str, ...]
    skill_source_revision: str
    skill_script_sha256: str
    skill_timeout_seconds: int
    http_token: str
    http_host: str
    http_port: int


def plain_value(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return " ".join(filter(None, (plain_value(item) for item in value))).strip()
    if isinstance(value, dict):
        for key in ("text", "name", "value", "label"):
            if value.get(key) not in (None, ""):
                return plain_value(value[key])
    return ""


def url_value(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("link") or value.get("url") or "").strip()
    if isinstance(value, list):
        for item in value:
            result = url_value(item)
            if result:
                return result
        return ""
    text = plain_value(value)
    return text if text.startswith(("https://", "http://")) else ""


def checked(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return plain_value(value).lower() in {"true", "1", "yes", "checked", "是"}


def record_fields(record: dict[str, Any]) -> dict[str, Any]:
    fields = record.get("fields")
    if not isinstance(fields, dict):
        raise WorkflowError("record_fields_missing", "Bitable record has no fields.", 500)
    return fields


def valid_sha(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F]{64}", value))


SHANGHAI = timezone(timedelta(hours=8))


def meeting_date(value: Any) -> tuple[str, int]:
    if isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(float(value) / 1000, tz=SHANGHAI)
    else:
        text = plain_value(value)
        match = re.search(r"(20\d{2})[-/.\u5e74](\d{1,2})[-/.\u6708](\d{1,2})", text)
        if not match:
            raise WorkflowError("meeting_date_missing", "Meeting date is missing or invalid.")
        dt = datetime(*(int(item) for item in match.groups()), tzinfo=SHANGHAI)
    normalized = f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}"
    start = datetime(dt.year, dt.month, dt.day, tzinfo=SHANGHAI)
    return normalized, int(start.timestamp() * 1000)


def field_time_ms(value: Any) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    text = plain_value(value)
    if text.isdigit():
        return int(text)
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(text, pattern).replace(tzinfo=SHANGHAI).timestamp() * 1000)
        except ValueError:
            continue
    return 0


def cutoff_time_ms(value: str) -> int:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d %H:%M").replace(tzinfo=SHANGHAI)
    except ValueError as exc:
        raise WorkflowError("invalid_config", "FEISHU_SANITIZE_SOURCE_CUTOFF must use YYYY-MM-DD HH:MM.", 500) from exc
    return int(parsed.timestamp() * 1000)


def safe_record_suffix(record_id: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9]", "", record_id)
    return clean[-6:] if len(clean) >= 6 else hashlib.sha256(record_id.encode()).hexdigest()[:6]


def record_id_hash(record_id: str) -> str:
    return hashlib.sha256(record_id.encode("utf-8")).hexdigest()[:12]


def link_field(text: str, url: str) -> dict[str, str]:
    return {"text": text, "link": url}


def normalize_review_markdown(content: bytes) -> bytes:
    """Best-effort legacy normalization without blocking a generated artifact."""

    try:
        text = content.decode("utf-8")
    except UnicodeError as exc:
        raise WorkflowError("review_encoding_invalid", "Review Markdown must be UTF-8.", 500) from exc

    text = re.sub(r"(?m)^### 主题：([^\r\n]+)$", r"【\1】", text)
    topic_count = len(re.findall(r"(?m)^【[^】\r\n]+】$", text))
    if topic_count == 0:
        logging.warning(
            "Ignoring non-blocking post-generation Markdown format warning: review_structure_invalid"
        )

    old_marker = "## 三、待确认业务事项"
    canonical_marker = "## 三、存疑与待确认"
    marker_count = text.count(old_marker) + text.count(canonical_marker)
    if marker_count > 1:
        logging.warning(
            "Ignoring non-blocking post-generation Markdown format warning: review_pending_invalid"
        )
        normalized = text.rstrip() + "\n"
    else:
        marker = old_marker if old_marker in text else canonical_marker if canonical_marker in text else ""
        if marker:
            topic_text, pending_text = text.split(marker, maxsplit=1)
            pending_text = pending_text.strip()
            if pending_text in {"", "- 无。"}:
                normalized = topic_text.rstrip() + "\n"
            else:
                normalized = (
                    topic_text.rstrip()
                    + "\n\n## 三、存疑与待确认\n\n"
                    + pending_text
                    + "\n"
                )
        else:
            normalized = text.rstrip() + "\n"

    if "主题：" in normalized or "待确认业务事项" in normalized:
        logging.warning(
            "Ignoring non-blocking post-generation Markdown format warning: review_structure_invalid"
        )
    return normalized.encode("utf-8")


def safe_error(exc: BaseException, max_chars: int) -> tuple[str, str]:
    code = str(getattr(exc, "code", "internal_error"))
    if isinstance(exc, (WorkflowError, SkillContractError, GatewayError, SourceContractError)):
        message = str(getattr(exc, "safe_message", "Processing failed."))
    else:
        message = "Internal processing error."
    message = re.sub(r"\s+", " ", message).strip()[:max_chars]
    return code, message


class ManifestStore:
    """Persist retry evidence; values are restricted to non-content metadata."""

    ALLOWED = {
        "stage",
        "record_id",
        "target_record_id",
        "idempotency_key",
        "source_sha256",
        "artifact_sha256",
        "file_token",
        "updated_at",
    }

    def __init__(self, root: Path):
        self.root = root

    def write(self, key: str, **values: Any) -> None:
        unknown = set(values) - self.ALLOWED
        if unknown:
            raise ValueError(f"Unsupported manifest keys: {sorted(unknown)}")
        payload = {name: values[name] for name in sorted(values)}
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        path = self.root / f"{hashlib.sha256(key.encode()).hexdigest()}.json"
        if path.is_symlink():
            raise ValueError("Refusing to replace a symbolic-link manifest")
        fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=self.root)
        temporary = Path(temporary_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fd = -1
                handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
            directory_fd = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


class RecordLocks:
    def __init__(self):
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    @contextmanager
    def acquire(self, key: str) -> Iterator[None]:
        with self._guard:
            lock = self._locks.setdefault(key, threading.Lock())
        if not lock.acquire(blocking=False):
            raise WorkflowError("already_running", "This record is already being processed.", 409)
        try:
            yield
        finally:
            lock.release()


class MinuteSanitizeOrchestrator:
    def __init__(self, cfg: ServiceConfig, gateway: WorkflowGateway, skill: SkillAdapter):
        self.cfg = cfg
        self.gateway = gateway
        self.skill = skill
        self.manifests = ManifestStore(cfg.state_dir)
        self.locks = RecordLocks()

    def health(self) -> dict[str, Any]:
        try:
            doctor = self.skill.doctor()
        except Exception:
            doctor = DoctorReport(False, "", (), "doctor_exception")
        return {
            "ok": True,
            "service": SERVICE_NAME,
            "version": SERVICE_VERSION,
            "live": True,
            "skill_ready": doctor.ready,
            "skill": doctor.public_dict(),
        }

    def _require_skill(self) -> DoctorReport:
        doctor = self.skill.doctor()
        if not doctor.ready:
            raise WorkflowError("skill_not_ready", f"Skill doctor failed: {doctor.reason_code or 'unknown'}.", 503)
        if doctor.contract_version != self.cfg.contract_version:
            raise WorkflowError("skill_contract_mismatch", "Skill contract version is incompatible.", 503)
        if not doctor.rules_version:
            raise WorkflowError("skill_rules_version_missing", "Skill rules version is unavailable.", 503)
        return doctor

    def _reconcile_repeated_target_create(
        self,
        source_record_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        for delay in TARGET_CREATE_RECONCILE_DELAYS:
            if delay:
                time.sleep(delay)
            target = self.gateway.find_target_by_source_id(source_record_id)
            if target is None:
                continue
            fields = record_fields(target)
            if (
                plain_value(fields.get(FIELD_SOURCE_ID)) != source_record_id
                or plain_value(fields.get(FIELD_IDEMPOTENCY)) != idempotency_key
            ):
                raise WorkflowError(
                    "target_create_conflict",
                    "Repeated target creation resolved to inconsistent target evidence.",
                )
            if not target.get("record_id"):
                raise WorkflowError(
                    "target_id_missing",
                    "Reconciled target record has no record ID.",
                    500,
                )
            return target
        raise WorkflowError(
            "target_create_ambiguous",
            "Repeated target creation could not be reconciled to a target record.",
            503,
        )

    def _review_terminal_is_complete(
        self,
        source_record_id: str,
        target_record_id: str,
        *,
        idempotency_key: str,
        rules_version: str,
        md_url: str,
        baseline_url: str,
        baseline_version: str,
        baseline_sha: str,
    ) -> bool:
        source = record_fields(self.gateway.get_source_record(source_record_id))
        target = record_fields(self.gateway.get_target_record(target_record_id))
        return (
            bool(md_url)
            and bool(baseline_url)
            and bool(baseline_version)
            and valid_sha(baseline_sha)
            and plain_value(source.get(SOURCE_STATUS)) == STATUS_GENERATED
            and url_value(source.get(SOURCE_LINK)) == md_url
            and plain_value(target.get(FIELD_SOURCE_ID)) == source_record_id
            and plain_value(target.get(FIELD_IDEMPOTENCY)) == idempotency_key
            and plain_value(target.get(FIELD_RULES_VERSION)) == rules_version
            and plain_value(target.get(FIELD_MD_STATUS)) == STATUS_GENERATED
            and plain_value(target.get(FIELD_QUALITY)) == "已通过"
            and url_value(target.get(FIELD_MD_LINK)) == md_url
            and url_value(target.get(FIELD_BASELINE_LINK)) == baseline_url
            and plain_value(target.get(FIELD_BASELINE_VERSION)) == baseline_version
            and plain_value(target.get(FIELD_BASELINE_SHA)).lower() == baseline_sha
            and plain_value(target.get(FIELD_VERSION_STATUS)) in {"基线已留存", "已完成"}
        )

    def _archive_terminal_is_complete(
        self,
        target_record_id: str,
        *,
        archive_url: str,
        approved_version: str,
        approved_sha: str,
        version_diff: str,
    ) -> bool:
        target = record_fields(self.gateway.get_target_record(target_record_id))
        return (
            plain_value(target.get(FIELD_ARCHIVE_STATUS)) == "已归档"
            and plain_value(target.get(FIELD_VERSION_STATUS)) == "已完成"
            and url_value(target.get(FIELD_ARCHIVE_LINK)) == archive_url
            and plain_value(target.get(FIELD_APPROVED_VERSION)) == approved_version
            and plain_value(target.get(FIELD_APPROVED_SHA)).lower() == approved_sha
            and plain_value(target.get(FIELD_VERSION_DIFF)) == version_diff
        )

    @staticmethod
    def _outcome_uncertain(code: str, message: str) -> WorkflowError:
        return WorkflowError(
            code,
            message,
            503,
            response_status="outcome_uncertain",
        )

    def generate_review_md(self, source_record_id: str) -> dict[str, Any]:
        doctor = self._require_skill()  # Must happen before any Feishu read/write.
        with self.locks.acquire(f"source:{source_record_id}"):
            source = self.gateway.get_source_record(source_record_id)
            fields = record_fields(source)
            existing_target = self.gateway.find_target_by_source_id(source_record_id)
            source_state = plain_value(fields.get(SOURCE_STATUS))
            if source_state == STATUS_GENERATED:
                if existing_target and self._review_complete(record_fields(existing_target)):
                    return {"ok": True, "status": "skipped_existing", "record_id": source_record_id}
                raise WorkflowError("source_target_inconsistent", "Source is complete but target evidence is missing.")
            if source_state not in {"", STATUS_PENDING, STATUS_RUNNING, STATUS_FAILED}:
                raise WorkflowError("source_state_blocked", "Source sanitization status cannot be safely resumed.")
            self._check_source_gate(fields)
            source_url = url_value(fields.get("归档链接"))
            expected_sha = plain_value(fields.get("审核后内容SHA256")).lower()
            date_text, date_ms = meeting_date(fields.get(FIELD_MEETING_DATE))
            month = date_text[:7]
            source_file = self.gateway.fetch_file(source_url)
            self._check_bytes(source_file.content, "source_markdown")
            if not source_file.name.lower().endswith(".md"):
                raise WorkflowError("source_type_invalid", "Source archive must be a Markdown file.")
            if source_file.sha256 != expected_sha:
                raise WorkflowError("source_hash_mismatch", "Source archive hash does not match Bitable.")
            try:
                adapted_source = adapt_source_contract(
                    source_file.content,
                    expected_meeting_date=date_text,
                )
            except SourceContractError as exc:
                raise WorkflowError(exc.code, exc.safe_message) from exc
            rules_version = pipeline_rules_version(doctor.rules_version)
            idempotency_key = sha256_hex(
                f"{source_record_id}|{expected_sha}|{rules_version}".encode("utf-8")
            )
            target_id = ""
            if existing_target:
                target_fields = record_fields(existing_target)
                if plain_value(target_fields.get(FIELD_IDEMPOTENCY)) != idempotency_key:
                    raise WorkflowError("idempotency_conflict", "Existing target uses a different source hash or contract.")
                target_id = str(existing_target.get("record_id") or "")
                if self._review_complete(target_fields):
                    md_url = url_value(target_fields.get(FIELD_MD_LINK))
                    baseline_url = url_value(target_fields.get(FIELD_BASELINE_LINK))
                    baseline_version = plain_value(target_fields.get(FIELD_BASELINE_VERSION))
                    baseline_sha = plain_value(target_fields.get(FIELD_BASELINE_SHA)).lower()
                    try:
                        self.gateway.update_source_record(
                            source_record_id,
                            {
                                SOURCE_STATUS: STATUS_GENERATED,
                                SOURCE_LINK: target_fields[FIELD_MD_LINK],
                                SOURCE_ERROR: "",
                            },
                        )
                    except Exception as exc:
                        try:
                            confirmed = self._review_terminal_is_complete(
                                source_record_id,
                                target_id,
                                idempotency_key=idempotency_key,
                                rules_version=rules_version,
                                md_url=md_url,
                                baseline_url=baseline_url,
                                baseline_version=baseline_version,
                                baseline_sha=baseline_sha,
                            )
                        except Exception:
                            confirmed = False
                        if confirmed:
                            return {
                                "ok": True,
                                "status": "skipped_existing",
                                "record_id": source_record_id,
                                "reconciled": True,
                            }
                        raise self._outcome_uncertain(
                            "review_commit_outcome_uncertain",
                            "Review commit outcome could not be confirmed.",
                        ) from exc
                    return {"ok": True, "status": "skipped_existing", "record_id": source_record_id}
            safe_name = f"{date_text} - 脱敏会议纪要 - {safe_record_suffix(source_record_id)}.md"
            self.gateway.update_source_record(
                source_record_id,
                {SOURCE_STATUS: STATUS_RUNNING, SOURCE_ERROR: ""},
            )
            try:
                if not target_id:
                    initial_fields = {
                        FIELD_PRIMARY: safe_name,
                        FIELD_SOURCE_ID: source_record_id,
                        FIELD_SOURCE_LINK: link_field("来源归档", source_url),
                        FIELD_SOURCE_SHA: expected_sha,
                        FIELD_MEETING_DATE: date_ms,
                        FIELD_IDEMPOTENCY: idempotency_key,
                        FIELD_RULES_VERSION: rules_version,
                        FIELD_MD_STATUS: STATUS_RUNNING,
                        FIELD_QUALITY: "未检查",
                        FIELD_REVIEW: False,
                        FIELD_ARCHIVE_STATUS: "待归档",
                        FIELD_VERSION_DIFF: "未比较",
                        FIELD_VERSION_STATUS: "待留存",
                        FIELD_ERROR_STAGE: "",
                        FIELD_ERROR: "",
                    }
                    try:
                        created = self.gateway.create_target_record(
                            initial_fields,
                            client_token=deterministic_client_token(f"sanitize:{idempotency_key}"),
                        )
                    except GatewayError as exc:
                        if exc.remote_code != "1254608":
                            raise
                        created = self._reconcile_repeated_target_create(
                            source_record_id,
                            idempotency_key,
                        )
                    target_id = str(created.get("record_id") or "")
                    if not target_id:
                        raise WorkflowError("target_id_missing", "Created target record has no record ID.", 500)
                if target_id:
                    self.gateway.update_target_record(
                        target_id,
                        {FIELD_MD_STATUS: STATUS_RUNNING, FIELD_ERROR_STAGE: "", FIELD_ERROR: ""},
                    )
                artifact = self.skill.generate_review_md(adapted_source, meeting_date=date_text)
                if artifact.rules_version != doctor.rules_version:
                    raise WorkflowError("skill_rules_changed", "Skill rules changed during generation.", 503)
                review_content = normalize_review_markdown(artifact.content)
                self._check_bytes(review_content, "review_markdown")
                try:
                    review_content.decode("utf-8")
                except UnicodeError as exc:
                    raise WorkflowError("review_encoding_invalid", "Review Markdown must be UTF-8.", 500) from exc
                content_sha = sha256_hex(review_content)
                pending_folder = self.gateway.ensure_month_folder(self.cfg.pending_root_token, month)
                baseline_folder = self.gateway.ensure_baseline_folder(self.cfg.version_root_token, month)
                pending = self.gateway.upload_or_reuse(
                    pending_folder, safe_name, review_content, content_type="text/markdown; charset=utf-8"
                )
                versioned_pending = self.gateway.ensure_auditable_version(
                    pending,
                    content_type="text/markdown; charset=utf-8",
                )
                if versioned_pending.sha256 != content_sha:
                    raise WorkflowError("pending_hash_mismatch", "Pending Markdown failed remote hash verification.", 500)
                baseline_name = f"{Path(safe_name).stem} - 审核前 - {idempotency_key[:8]}.md"
                baseline = self.gateway.upload_or_reuse(
                    baseline_folder, baseline_name, review_content, content_type="text/markdown; charset=utf-8"
                )
                self.manifests.write(
                    f"review:{source_record_id}",
                    stage="review_uploaded",
                    record_id=source_record_id,
                    target_record_id=target_id,
                    idempotency_key=idempotency_key,
                    source_sha256=expected_sha,
                    artifact_sha256=content_sha,
                    file_token=pending.token,
                    updated_at=self.gateway.now_ms(),
                )
                final_fields = {
                    FIELD_RULES_VERSION: rules_version,
                    FIELD_MD_STATUS: STATUS_GENERATED,
                    FIELD_MD_LINK: link_field(safe_name, pending.url),
                    FIELD_MD_TIME: self.gateway.now_ms(),
                    FIELD_QUALITY: "已通过",
                    FIELD_ARCHIVE_STATUS: "待归档",
                    FIELD_BASELINE_LINK: link_field(baseline_name, baseline.url),
                    FIELD_BASELINE_VERSION: versioned_pending.version,
                    FIELD_BASELINE_SHA: content_sha,
                    FIELD_VERSION_DIFF: "未比较",
                    FIELD_VERSION_STATUS: "基线已留存",
                    FIELD_VERSION_ERROR: "",
                    FIELD_ERROR_STAGE: "",
                    FIELD_ERROR: "",
                }
                try:
                    self.gateway.update_target_record(target_id, final_fields)
                    self.gateway.update_source_record(
                        source_record_id,
                        {
                            SOURCE_STATUS: STATUS_GENERATED,
                            SOURCE_LINK: link_field(safe_name, pending.url),
                            SOURCE_TIME: self.gateway.now_ms(),
                            SOURCE_ERROR: "",
                        },
                    )
                except Exception as exc:
                    try:
                        confirmed = self._review_terminal_is_complete(
                            source_record_id,
                            target_id,
                            idempotency_key=idempotency_key,
                            rules_version=rules_version,
                            md_url=pending.url,
                            baseline_url=baseline.url,
                            baseline_version=versioned_pending.version,
                            baseline_sha=content_sha,
                        )
                    except Exception:
                        confirmed = False
                    if confirmed:
                        return {
                            "ok": True,
                            "status": "generated",
                            "record_id": source_record_id,
                            "target_record_id": target_id,
                            "reconciled": True,
                        }
                    raise self._outcome_uncertain(
                        "review_commit_outcome_uncertain",
                        "Review commit outcome could not be confirmed.",
                    ) from exc
                return {"ok": True, "status": "generated", "record_id": source_record_id, "target_record_id": target_id}
            except Exception as exc:
                if isinstance(exc, WorkflowError) and exc.response_status == "outcome_uncertain":
                    raise
                self._mark_review_failure(source_record_id, target_id, exc)
                raise

    def archive_review_md(self, target_record_id: str) -> dict[str, Any]:
        with self.locks.acquire(f"target:{target_record_id}"):
            record = self.gateway.get_target_record(target_record_id)
            fields = record_fields(record)
            terminal = (
                plain_value(fields.get(FIELD_ARCHIVE_STATUS)) == "已归档"
                and plain_value(fields.get(FIELD_VERSION_STATUS)) == "已完成"
            )
            if terminal:
                archive_url = url_value(fields.get(FIELD_ARCHIVE_LINK))
                approved_sha = plain_value(fields.get(FIELD_APPROVED_SHA)).lower()
                if not archive_url or not valid_sha(approved_sha):
                    raise WorkflowError(
                        "terminal_archive_evidence_missing",
                        "Archived Markdown terminal evidence is incomplete.",
                    )
                archived = self.gateway.fetch_file(archive_url)
                self._check_bytes(archived.content, "archived_markdown")
                if not archived.name.lower().endswith(".md"):
                    raise WorkflowError("terminal_archive_type_invalid", "Archived artifact must be Markdown.")
                try:
                    archived.content.decode("utf-8")
                except UnicodeError as exc:
                    raise WorkflowError("terminal_archive_encoding_invalid", "Archived Markdown must be UTF-8.") from exc
                if archived.sha256 != approved_sha:
                    raise WorkflowError(
                        "terminal_archive_hash_mismatch",
                        "Archived Markdown no longer matches its approved SHA256.",
                    )
                return {"ok": True, "status": "skipped_existing", "record_id": target_record_id}
            self._check_archive_gate(fields)
            source_id = plain_value(fields.get(FIELD_SOURCE_ID))
            date_text, _ = meeting_date(fields.get(FIELD_MEETING_DATE))
            safe_name = f"{date_text} - 脱敏会议纪要 - {safe_record_suffix(source_id)}.md"
            try:
                pending = self.gateway.fetch_file(url_value(fields.get(FIELD_MD_LINK)), require_version=True)
                self._check_bytes(pending.content, "approved_markdown")
                pending.content.decode("utf-8")
            except Exception as exc:
                version_exc: BaseException = exc
                if isinstance(exc, UnicodeError):
                    version_exc = WorkflowError("approved_encoding_invalid", "Approved Markdown must be UTF-8.")
                code, message = safe_error(version_exc, self.cfg.max_error_chars)
                try:
                    self.gateway.update_target_record(
                        target_record_id,
                        {
                            FIELD_ARCHIVE_STATUS: "归档失败",
                            FIELD_VERSION_STATUS: "留存失败",
                            FIELD_VERSION_DIFF: "比较失败",
                            FIELD_VERSION_ERROR: message,
                            FIELD_ERROR_STAGE: "archive-version-read",
                            FIELD_ERROR: message,
                        },
                    )
                except Exception:
                    logging.error(
                        "failure_status_write_failed stage=archive-version record_id_hash=%s code=%s",
                        record_id_hash(target_record_id),
                        code,
                    )
                raise version_exc
            approved_sha = pending.sha256
            baseline_sha = plain_value(fields.get(FIELD_BASELINE_SHA)).lower()
            self.gateway.update_target_record(
                target_record_id,
                {FIELD_ARCHIVE_STATUS: "归档中", FIELD_ERROR_STAGE: "", FIELD_ERROR: ""},
            )
            try:
                archive_folder = self.gateway.ensure_month_folder(self.cfg.archive_root_token, date_text[:7])
                archived = self.gateway.upload_or_reuse(
                    archive_folder, safe_name, pending.content, content_type="text/markdown; charset=utf-8"
                )
                roundtrip = self.gateway.fetch_file(archived.url)
                if roundtrip.sha256 != approved_sha:
                    raise WorkflowError("archive_hash_mismatch", "Archived Markdown failed remote hash verification.", 500)
                diff = "无修改" if baseline_sha == approved_sha else "有修改"
                self.manifests.write(
                    f"archive:{target_record_id}",
                    stage="review_archived",
                    record_id=target_record_id,
                    source_sha256=baseline_sha,
                    artifact_sha256=approved_sha,
                    file_token=archived.token,
                    updated_at=self.gateway.now_ms(),
                )
                try:
                    self.gateway.update_target_record(
                        target_record_id,
                        {
                            FIELD_ARCHIVE_STATUS: "已归档",
                            FIELD_ARCHIVE_LINK: link_field(safe_name, archived.url),
                            FIELD_ARCHIVE_TIME: self.gateway.now_ms(),
                            FIELD_APPROVED_VERSION: pending.version,
                            FIELD_APPROVED_SHA: approved_sha,
                            FIELD_VERSION_DIFF: diff,
                            FIELD_VERSION_STATUS: "已完成",
                            FIELD_VERSION_ERROR: "",
                            FIELD_ERROR_STAGE: "",
                            FIELD_ERROR: "",
                        },
                    )
                except Exception as exc:
                    try:
                        confirmed = self._archive_terminal_is_complete(
                            target_record_id,
                            archive_url=archived.url,
                            approved_version=pending.version,
                            approved_sha=approved_sha,
                            version_diff=diff,
                        )
                    except Exception:
                        confirmed = False
                    if confirmed:
                        return {
                            "ok": True,
                            "status": "archived",
                            "record_id": target_record_id,
                            "version_diff": diff,
                            "reconciled": True,
                        }
                    raise self._outcome_uncertain(
                        "archive_commit_outcome_uncertain",
                        "Archive commit outcome could not be confirmed.",
                    ) from exc
                return {"ok": True, "status": "archived", "record_id": target_record_id, "version_diff": diff}
            except Exception as exc:
                if isinstance(exc, WorkflowError) and exc.response_status == "outcome_uncertain":
                    raise
                code, message = safe_error(exc, self.cfg.max_error_chars)
                try:
                    self.gateway.update_target_record(
                        target_record_id,
                        {
                            FIELD_ARCHIVE_STATUS: "归档失败",
                            FIELD_VERSION_STATUS: "基线已留存",
                            FIELD_VERSION_DIFF: "未比较",
                            FIELD_VERSION_ERROR: "",
                            FIELD_ERROR_STAGE: "archive-review-md",
                            FIELD_ERROR: message,
                        },
                    )
                except Exception:
                    logging.error(
                        "failure_status_write_failed stage=archive record_id_hash=%s code=%s",
                        record_id_hash(target_record_id),
                        code,
                    )
                raise

    def _check_bytes(self, content: bytes, stage: str) -> None:
        if not content or not content.strip():
            raise WorkflowError(f"{stage}_empty", f"{stage} artifact is empty.")
        if len(content) > self.cfg.max_input_bytes:
            raise WorkflowError(f"{stage}_too_large", f"{stage} artifact exceeds the configured limit.", 413)

    @staticmethod
    def _review_complete(fields: dict[str, Any]) -> bool:
        return (
            plain_value(fields.get(FIELD_MD_STATUS)) == STATUS_GENERATED
            and bool(url_value(fields.get(FIELD_MD_LINK)))
            and plain_value(fields.get(FIELD_VERSION_STATUS)) in {"基线已留存", "已完成"}
            and valid_sha(plain_value(fields.get(FIELD_BASELINE_SHA)))
        )

    def _check_source_gate(self, fields: dict[str, Any]) -> None:
        if not checked(fields.get(FIELD_REVIEW)):
            raise WorkflowError("source_not_reviewed", "Source meeting minutes are not approved.")
        if plain_value(fields.get(FIELD_ARCHIVE_STATUS)) != "已归档":
            raise WorkflowError("source_not_archived", "Source meeting minutes are not archived.")
        if plain_value(fields.get(FIELD_VERSION_STATUS)) != "已完成":
            raise WorkflowError("source_version_incomplete", "Source version retention is incomplete.")
        if not url_value(fields.get(FIELD_ARCHIVE_LINK)):
            raise WorkflowError("source_archive_link_missing", "Source archive link is missing.")
        if not valid_sha(plain_value(fields.get(FIELD_APPROVED_SHA))):
            raise WorkflowError("source_sha_missing", "Source approved SHA256 is missing or invalid.")
        if self.cfg.source_cutoff_ms:
            archived_at = field_time_ms(fields.get(FIELD_ARCHIVE_TIME))
            if not archived_at or archived_at < self.cfg.source_cutoff_ms:
                raise WorkflowError("source_before_cutoff", "Source archive predates the sanitization workflow cutoff.")

    @staticmethod
    def _check_archive_gate(fields: dict[str, Any]) -> None:
        if plain_value(fields.get(FIELD_MD_STATUS)) != STATUS_GENERATED:
            raise WorkflowError("review_md_incomplete", "Review Markdown is not generated.")
        if not checked(fields.get(FIELD_REVIEW)):
            raise WorkflowError("review_not_approved", "Sanitized Markdown is not approved.")
        if plain_value(fields.get(FIELD_ARCHIVE_STATUS)) not in {"待归档", "归档中", "归档失败"}:
            raise WorkflowError("archive_state_blocked", "Archive status cannot be safely resumed.")
        if url_value(fields.get(FIELD_ARCHIVE_LINK)):
            raise WorkflowError("archive_link_conflict", "Archive link already exists while status is pending.")
        if not url_value(fields.get(FIELD_MD_LINK)):
            raise WorkflowError("review_md_link_missing", "Review Markdown link is missing.")
        if plain_value(fields.get(FIELD_VERSION_STATUS)) not in {"基线已留存", "留存失败"}:
            raise WorkflowError("baseline_incomplete", "Review baseline is incomplete.")
        if not url_value(fields.get(FIELD_BASELINE_LINK)) or not valid_sha(plain_value(fields.get(FIELD_BASELINE_SHA))):
            raise WorkflowError("baseline_evidence_missing", "Review baseline evidence is incomplete.")

    def _mark_review_failure(self, source_id: str, target_id: str, exc: BaseException) -> None:
        code, message = safe_error(exc, self.cfg.max_error_chars)
        try:
            self.gateway.update_source_record(source_id, {SOURCE_STATUS: STATUS_FAILED, SOURCE_ERROR: message})
        except Exception:
            logging.error(
                "failure_status_write_failed stage=review-source record_id_hash=%s code=%s",
                record_id_hash(source_id),
                code,
            )
        if target_id:
            try:
                self.gateway.update_target_record(
                    target_id,
                    {FIELD_MD_STATUS: STATUS_FAILED, FIELD_ERROR_STAGE: "review-md", FIELD_ERROR: message},
                )
            except Exception:
                logging.error(
                    "failure_status_write_failed stage=review-target record_id_hash=%s code=%s",
                    record_id_hash(target_id),
                    code,
                )


def parse_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'").strip('"')
    return values


def read_secret(env: Mapping[str, str], name: str, *, required: bool = True) -> str:
    file_value = str(env.get(f"{name}_FILE", "")).strip()
    direct = str(env.get(name, "")).strip()
    if file_value:
        try:
            value = Path(file_value).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise WorkflowError("invalid_config", f"Could not read {name}_FILE.", 500) from exc
    else:
        value = direct
    if required and not value:
        raise WorkflowError("invalid_config", f"Missing required configuration: {name}.", 500)
    return value


def read_runtime_config(env_override: Mapping[str, str] | None = None) -> RuntimeConfig:
    base = Path(__file__).resolve().parent
    env: dict[str, str] = {}
    explicit = os.environ.get("FEISHU_SANITIZE_ENV_FILE", "").strip()
    for candidate in ([Path(explicit)] if explicit else [Path.cwd() / ".env", base / ".env"]):
        if candidate.exists():
            env.update(parse_dotenv(candidate))
            break
    env.update(os.environ)
    if env_override:
        env.update(env_override)

    def required(name: str) -> str:
        value = str(env.get(name, "")).strip()
        if not value:
            raise WorkflowError("invalid_config", f"Missing required configuration: {name}.", 500)
        return value

    try:
        command_value = json.loads(
            str(
                env.get(
                    "SANITIZE_SKILL_COMMAND_JSON",
                    '["python3","/skills/meeting-minutes-sanitizer/scripts/sanitize_minutes.py"]',
                )
            )
        )
    except json.JSONDecodeError as exc:
        raise WorkflowError("invalid_config", "SANITIZE_SKILL_COMMAND_JSON must be a JSON array.", 500) from exc
    if not isinstance(command_value, list) or not command_value or not all(isinstance(item, str) and item for item in command_value):
        raise WorkflowError("invalid_config", "SANITIZE_SKILL_COMMAND_JSON must be a non-empty string array.", 500)
    service = ServiceConfig(
        pending_root_token=required("FEISHU_SANITIZE_PENDING_ROOT_FOLDER_TOKEN"),
        archive_root_token=required("FEISHU_SANITIZE_ARCHIVE_ROOT_FOLDER_TOKEN"),
        version_root_token=required("FEISHU_SANITIZE_VERSION_ROOT_FOLDER_TOKEN"),
        contract_version=str(env.get("SANITIZE_SKILL_CONTRACT_VERSION", "minute-sanitization/v2")).strip(),
        source_cutoff_ms=cutoff_time_ms(required("FEISHU_SANITIZE_SOURCE_CUTOFF")),
        state_dir=Path(str(env.get("SANITIZE_STATE_DIR", base / "data/state"))).expanduser(),
        max_input_bytes=int(str(env.get("SANITIZE_MAX_INPUT_BYTES", 10 * 1024 * 1024))),
        max_error_chars=int(str(env.get("SANITIZE_MAX_ERROR_CHARS", 300))),
    )
    feishu = FeishuSettings(
        app_id=read_secret(env, "FEISHU_APP_ID"),
        app_secret=read_secret(env, "FEISHU_APP_SECRET"),
        bitable_app_token=required("FEISHU_SANITIZE_BITABLE_APP_TOKEN"),
        source_table_id=required("FEISHU_SANITIZE_SOURCE_TABLE_ID"),
        target_table_id=required("FEISHU_SANITIZE_TARGET_TABLE_ID"),
        openapi_base=str(env.get("FEISHU_OPENAPI_BASE", "https://open.feishu.cn/open-apis")).rstrip("/"),
        user_id_type=str(env.get("FEISHU_USER_ID_TYPE", "open_id")),
    )
    return RuntimeConfig(
        service=service,
        feishu=feishu,
        skill_command=tuple(command_value),
        skill_source_revision=required("SANITIZE_SKILL_SOURCE_REVISION").lower(),
        skill_script_sha256=required("SANITIZE_SKILL_SCRIPT_SHA256").lower(),
        skill_timeout_seconds=int(str(env.get("SANITIZE_SKILL_TIMEOUT_SECONDS", 180))),
        http_token=read_secret(env, "FEISHU_SANITIZE_HTTP_TOKEN"),
        http_host=str(env.get("FEISHU_SANITIZE_HTTP_HOST", "127.0.0.1")),
        http_port=int(str(env.get("FEISHU_SANITIZE_HTTP_PORT", 8791))),
    )


def make_handler(orchestrator: MinuteSanitizeOrchestrator, http_token: str) -> type[BaseHTTPRequestHandler]:
    routes = {
        "/generate-review-md": orchestrator.generate_review_md,
        "/archive-review-md": orchestrator.archive_review_md,
    }

    class Handler(BaseHTTPRequestHandler):
        server_version = f"FeishuMinuteSanitize/{SERVICE_VERSION}"

        def log_message(self, format: str, *args: Any) -> None:
            return

        def write_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if urllib.parse.urlparse(self.path).path == "/healthz":
                self.write_json(200, orchestrator.health())
            else:
                self.write_json(404, {"ok": False, "error_code": "not_found"})

        def do_POST(self) -> None:
            path = urllib.parse.urlparse(self.path).path
            action = routes.get(path)
            if action is None:
                self.write_json(404, {"ok": False, "error_code": "not_found"})
                return
            auth_values = self.headers.get_all("Authorization", [])
            if len(auth_values) != 1:
                self.write_json(401, {"ok": False, "error_code": "unauthorized"})
                return
            header = auth_values[0]
            supplied = header[7:] if header.startswith("Bearer ") else ""
            if not supplied or not secrets.compare_digest(supplied, http_token):
                self.write_json(401, {"ok": False, "error_code": "unauthorized"})
                return
            if self.headers.get_all("Transfer-Encoding", []):
                self.write_json(400, {"ok": False, "error_code": "transfer_encoding_not_supported"})
                return
            length_values = self.headers.get_all("Content-Length", [])
            if len(length_values) != 1:
                self.write_json(400, {"ok": False, "error_code": "invalid_content_length"})
                return
            try:
                length = int(length_values[0])
            except ValueError:
                self.write_json(400, {"ok": False, "error_code": "invalid_content_length"})
                return
            if length <= 0 or length > 16 * 1024:
                self.write_json(400, {"ok": False, "error_code": "invalid_body_size"})
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
            except (UnicodeError, json.JSONDecodeError, ValueError):
                self.write_json(400, {"ok": False, "error_code": "invalid_json"})
                return
            if not isinstance(payload, dict) or set(payload) - {"record_id", "recordId"}:
                self.write_json(400, {"ok": False, "error_code": "invalid_payload"})
                return
            record_id = str(payload.get("record_id") or payload.get("recordId") or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]{4,100}", record_id):
                self.write_json(400, {"ok": False, "error_code": "invalid_record_id"})
                return
            try:
                result = action(record_id)
                self.write_json(200, result)
            except Exception as exc:
                code, message = safe_error(exc, orchestrator.cfg.max_error_chars)
                status = int(getattr(exc, "http_status", None) or 500)
                logging.error(
                    "request_failed record_id_hash=%s code=%s",
                    record_id_hash(record_id),
                    code,
                )
                response = {"ok": False, "error_code": code, "message": message}
                response_status = str(getattr(exc, "response_status", "") or "")
                if response_status:
                    response["status"] = response_status
                self.write_json(status, response)

    return Handler


def build_orchestrator(runtime: RuntimeConfig) -> MinuteSanitizeOrchestrator:
    gateway = FeishuOpenApiGateway(runtime.feishu)
    skill = CliSkillAdapter(
        runtime.skill_command,
        expected_contract_version=runtime.service.contract_version,
        expected_source_revision=runtime.skill_source_revision,
        expected_script_sha256=runtime.skill_script_sha256,
        timeout_seconds=runtime.skill_timeout_seconds,
    )
    return MinuteSanitizeOrchestrator(runtime.service, gateway, skill)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve_parser = subparsers.add_parser("serve")
    serve_parser.add_argument("--apply", action="store_true", help="required because requests can write Drive/Base")
    subparsers.add_parser("doctor")
    args = parser.parse_args(argv)
    logging.basicConfig(level=os.environ.get("FEISHU_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    if args.command == "serve" and not args.apply:
        raise SystemExit("serve requires explicit --apply")
    runtime = read_runtime_config()
    orchestrator = build_orchestrator(runtime)
    if args.command == "doctor":
        payload = orchestrator.health()
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload["skill_ready"] else 1
    server = ThreadingHTTPServer((runtime.http_host, runtime.http_port), make_handler(orchestrator, runtime.http_token))
    logging.info("service_start name=%s host=%s port=%s", SERVICE_NAME, runtime.http_host, runtime.http_port)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
