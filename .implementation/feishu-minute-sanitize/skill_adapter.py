#!/usr/bin/env python3
"""Adapter boundary for the single-Markdown minute-sanitization skill.

The service maps the pinned script CLI to its internal review-Markdown
capability. Sanitization business rules remain entirely inside the skill.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as calendar_date
import hashlib
import logging
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
from typing import Any, Mapping, Protocol, Sequence


APPROVED_SKILL_PINS = {
    "2716eae7d3286abda46f71e9d4e8bbb4712fb32b":
        "ea16a346b3b8639f2b3cc265b694820d77d4bc6e8cf0c7991f9882dbe22d8f3f",
    "919125c568ae5bb5be6369179bf775beab6d5ffe":
        "9a4100025fe35bd2ab409ecc581de26f8e9b0ec4039d5afbd0b789361b2eae28",
}
PROBE_DATE = "2032-07-13"
PROBE_IDENTIFIERS = ("测试甲", "测试研究机构")
PROBE_SOURCE = """会议日期：2032-07-13
会议类型：契约探针
姓名：测试甲
任职公司：测试研究机构
### 发言人：测试甲
【订单｜A公司】
测试甲认为，订单可能增长，仍待公告确认。
### 存疑与待确认
- 订单增幅仍待确认。
""".encode("utf-8")

REQUIRED_SECTION_HEADINGS = (
    "# 脱敏会议纪要",
    "## 一、文档信息",
    "## 二、主题纪要",
)
PENDING_SECTION_HEADING = "## 三、存疑与待确认"
NON_BLOCKING_FORMAT_CODES = {
    "review_structure_invalid",
    "review_pending_invalid",
}


class SkillContractError(RuntimeError):
    """A safe, content-free skill contract failure."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.safe_message = message


@dataclass(frozen=True)
class DoctorReport:
    ready: bool
    contract_version: str
    capabilities: tuple[str, ...]
    reason_code: str = ""
    rules_version: str = ""

    def public_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "contract_version": self.contract_version,
            "capabilities": list(self.capabilities),
            "reason_code": self.reason_code,
            "rules_version": self.rules_version,
        }


@dataclass(frozen=True)
class ReviewArtifact:
    content: bytes
    rules_version: str
    quality_status: str


class SkillAdapter(Protocol):
    def doctor(self, *, force: bool = False) -> DoctorReport: ...

    def generate_review_md(self, source_markdown: bytes, *, meeting_date: str) -> ReviewArtifact: ...


def sha256_hex(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


class CliSkillAdapter:
    """Invoke and validate the pinned single-Markdown sanitizer CLI."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        expected_contract_version: str,
        expected_source_revision: str,
        expected_script_sha256: str,
        timeout_seconds: int = 180,
        doctor_cache_seconds: int = 30,
        approved_skill_pins: Mapping[str, str] | None = None,
    ):
        command_tuple = tuple(str(item) for item in command if str(item))
        if not command_tuple:
            raise ValueError("Skill command must not be empty.")
        revision = expected_source_revision.strip().lower()
        script_sha256 = expected_script_sha256.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ValueError("Skill source revision must be a full Git commit hash.")
        if not re.fullmatch(r"[0-9a-f]{64}", script_sha256):
            raise ValueError("Skill script SHA256 must be a lowercase hexadecimal digest.")
        approved = dict(APPROVED_SKILL_PINS if approved_skill_pins is None else approved_skill_pins)
        if approved.get(revision) != script_sha256:
            raise ValueError("Skill revision and script SHA256 are not an approved pair.")
        self._command = command_tuple
        self._expected_contract_version = expected_contract_version
        self._expected_source_revision = revision
        self._expected_script_sha256 = script_sha256
        self._rules_version = (
            f"meeting-minutes-sanitizer@{revision}#sha256:{script_sha256}"
        )
        self._timeout_seconds = timeout_seconds
        self._doctor_cache_seconds = doctor_cache_seconds
        self._doctor_lock = threading.Lock()
        self._doctor_report: DoctorReport | None = None
        self._doctor_checked_at = 0.0

    def _run(self, args: Sequence[str], *, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                [*self._command, *args],
                text=True,
                capture_output=True,
                timeout=timeout or self._timeout_seconds,
                check=False,
                shell=False,
            )
        except FileNotFoundError as exc:
            raise SkillContractError("skill_command_missing", "Skill command is not available.") from exc
        except subprocess.TimeoutExpired as exc:
            raise SkillContractError("skill_timeout", "Skill invocation timed out.") from exc
        except OSError as exc:
            raise SkillContractError("skill_start_failed", "Skill process could not be started.") from exc

    def _verify_script(self) -> Path:
        if len(self._command) != 2 or Path(self._command[0]).name not in {"python", "python3"}:
            raise SkillContractError(
                "skill_command_invalid",
                "Skill command must be a Python interpreter and one absolute script path.",
            )
        script_path = Path(self._command[1])
        skill_root = script_path.parent.parent
        if (
            not script_path.is_absolute()
            or script_path.is_symlink()
            or script_path.parent.is_symlink()
            or skill_root.is_symlink()
            or not script_path.is_file()
        ):
            raise SkillContractError("skill_script_invalid", "Skill script is not a regular file.")
        try:
            content = script_path.read_bytes()
        except OSError as exc:
            raise SkillContractError("skill_script_unreadable", "Skill script is not readable.") from exc
        if sha256_hex(content) != self._expected_script_sha256:
            raise SkillContractError("skill_script_hash_mismatch", "Skill script hash does not match the pinned version.")
        return script_path

    def doctor(self, *, force: bool = False) -> DoctorReport:
        now = time.monotonic()
        with self._doctor_lock:
            if (
                not force
                and self._doctor_report is not None
                and now - self._doctor_checked_at < self._doctor_cache_seconds
            ):
                return self._doctor_report
            report = self._run_doctor()
            self._doctor_report = report
            self._doctor_checked_at = time.monotonic()
            return report

    def _run_doctor(self) -> DoctorReport:
        try:
            self._verify_script()
            content = self._invoke(
                PROBE_SOURCE,
                meeting_date=PROBE_DATE,
                timeout=min(self._timeout_seconds, 30),
            )
        except SkillContractError as exc:
            return DoctorReport(False, "", (), exc.code)
        if not content:
            return DoctorReport(False, "", (), "review_artifact_empty")
        probe_text = content.decode("utf-8")
        if any(identifier in probe_text for identifier in PROBE_IDENTIFIERS):
            return DoctorReport(False, "", (), "probe_identity_leak")
        return DoctorReport(
            True,
            self._expected_contract_version,
            ("review-md",),
            rules_version=self._rules_version,
        )

    def _require_ready(self) -> DoctorReport:
        report = self.doctor()
        if not report.ready:
            raise SkillContractError("skill_not_ready", f"Skill doctor failed: {report.reason_code or 'unknown'}.")
        return report

    def generate_review_md(self, source_markdown: bytes, *, meeting_date: str) -> ReviewArtifact:
        report = self._require_ready()
        self._verify_script()
        normalized_date = self._normalize_meeting_date(meeting_date)
        content = self._invoke(source_markdown, meeting_date=normalized_date)
        if report.rules_version != self._rules_version:
            raise SkillContractError("rules_version_changed", "Skill rules version changed during invocation.")
        return ReviewArtifact(
            content=content,
            rules_version=self._rules_version,
            quality_status="passed",
        )

    def _invoke(
        self,
        source_markdown: bytes,
        *,
        meeting_date: str,
        timeout: int | None = None,
    ) -> bytes:
        normalized_date = self._normalize_meeting_date(meeting_date)
        with tempfile.TemporaryDirectory(prefix="minute-sanitize-review-") as tmp:
            root = Path(tmp)
            input_path = root / "input.md"
            output_dir = root / "out"
            output_dir.mkdir()
            input_path.write_bytes(source_markdown)
            result = self._run(
                [
                    str(input_path),
                    "--output-dir",
                    str(output_dir),
                    "--meeting-date",
                    normalized_date,
                    "--output-stem",
                    "review",
                ],
                timeout=timeout,
            )
            if result.returncode != 0:
                raise SkillContractError("skill_review_failed", "Sanitizer exited unsuccessfully.")
            output_path = self._assert_only_expected_file(output_dir, "review_sanitized.md")
            return self._validate_markdown(output_path, expected_meeting_date=normalized_date)

    @staticmethod
    def _normalize_meeting_date(value: str) -> str:
        text = value.strip()
        try:
            parsed = calendar_date.fromisoformat(text)
        except ValueError as exc:
            raise SkillContractError("meeting_date_invalid", "Meeting date must use a valid YYYY-MM-DD value.") from exc
        normalized = parsed.isoformat()
        if text != normalized:
            raise SkillContractError("meeting_date_invalid", "Meeting date must use a valid YYYY-MM-DD value.")
        return normalized

    @staticmethod
    def _assert_only_expected_file(directory: Path, expected_name: str) -> Path:
        try:
            entries = list(directory.iterdir())
        except OSError as exc:
            raise SkillContractError("skill_output_unreadable", "Skill output directory is not readable.") from exc
        if len(entries) != 1 or entries[0].name != expected_name:
            raise SkillContractError("skill_extra_artifacts", "Skill emitted unexpected output artifacts.")
        output_path = entries[0]
        if output_path.is_symlink() or not output_path.is_file():
            raise SkillContractError("review_artifact_invalid", "Skill output must be one regular Markdown file.")
        return output_path

    @staticmethod
    def _validate_markdown(path: Path, *, expected_meeting_date: str) -> bytes:
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise SkillContractError("review_artifact_invalid", "Review Markdown is not readable.") from exc
        if not content:
            raise SkillContractError("review_artifact_invalid", "Review Markdown is empty.")
        try:
            text = content.decode("utf-8")
        except UnicodeError as exc:
            raise SkillContractError("review_artifact_invalid", "Review Markdown must be UTF-8.") from exc

        try:
            CliSkillAdapter._validate_markdown_format(text)
        except SkillContractError as exc:
            if exc.code not in NON_BLOCKING_FORMAT_CODES:
                raise
            logging.warning(
                "Ignoring non-blocking post-generation Markdown format warning: %s",
                exc.code,
            )

        metadata_text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
        CliSkillAdapter._validate_markdown_metadata(
            metadata_text,
            expected_meeting_date=expected_meeting_date,
        )
        return content

    @staticmethod
    def _validate_markdown_format(text: str) -> None:
        lines = text.splitlines()
        if not lines or lines[0] != REQUIRED_SECTION_HEADINGS[0]:
            raise SkillContractError("review_structure_invalid", "Review Markdown must start with the canonical title.")
        if "主题：" in text or "待确认业务事项" in text:
            raise SkillContractError("review_structure_invalid", "Review Markdown contains a forbidden internal label.")
        if any(text.count(heading) != 1 for heading in REQUIRED_SECTION_HEADINGS):
            raise SkillContractError("review_structure_invalid", "Review Markdown has an invalid required-section structure.")
        if text.count(PENDING_SECTION_HEADING) > 1:
            raise SkillContractError("review_structure_invalid", "Review Markdown has an invalid pending section.")
        section_headings = list(REQUIRED_SECTION_HEADINGS)
        if PENDING_SECTION_HEADING in text:
            section_headings.append(PENDING_SECTION_HEADING)
        positions = [text.index(heading) for heading in section_headings]
        if positions != sorted(positions):
            raise SkillContractError("review_structure_invalid", "Review Markdown sections are out of order.")
        allowed_fixed = set(section_headings)
        for line_index, line in enumerate(lines):
            if not re.match(r"^#{1,6}\s+", line):
                continue
            if line in allowed_fixed:
                continue
            raise SkillContractError("review_structure_invalid", "Review Markdown contains an unexpected heading.")
        level_two = [line for line in lines if re.match(r"^##(?!#)\s+", line)]
        if level_two != section_headings[1:]:
            raise SkillContractError("review_structure_invalid", "Review Markdown has an invalid section structure.")
        topic_markers = [
            (line_index, line)
            for line_index, line in enumerate(lines)
            if re.fullmatch(r"【[^】\n]+】", line)
        ]
        if not topic_markers:
            raise SkillContractError("review_structure_invalid", "Review Markdown has no topic units.")
        topic_section_start = lines.index(REQUIRED_SECTION_HEADINGS[2])
        pending_section_start = (
            lines.index(PENDING_SECTION_HEADING)
            if PENDING_SECTION_HEADING in lines
            else len(lines)
        )
        if any(
            not topic_section_start < line_index < pending_section_start
            for line_index, _ in topic_markers
        ):
            raise SkillContractError("review_structure_invalid", "Review Markdown topic markers are outside the topic section.")

    @staticmethod
    def _validate_markdown_metadata(text: str, *, expected_meeting_date: str) -> None:
        date_matches = re.findall(r"(?m)^- 会议日期：(20\d{2}-\d{2}-\d{2})$", text)
        if len(date_matches) != 1:
            raise SkillContractError("review_date_invalid", "Review Markdown meeting date is missing or invalid.")
        try:
            parsed_date = calendar_date.fromisoformat(date_matches[0]).isoformat()
        except ValueError as exc:
            raise SkillContractError("review_date_invalid", "Review Markdown meeting date is missing or invalid.") from exc
        if parsed_date != expected_meeting_date:
            raise SkillContractError("review_date_mismatch", "Review Markdown meeting date differs from the source record.")
        if text.count("- 脱敏等级：L2_FACT_PRESERVED") != 1:
            raise SkillContractError("review_level_invalid", "Review Markdown anonymization level is invalid.")
        if PENDING_SECTION_HEADING in text:
            pending = text.split(PENDING_SECTION_HEADING + "\n\n", maxsplit=1)
            if len(pending) != 2 or not pending[1].strip():
                logging.warning(
                    "Ignoring non-blocking post-generation Markdown format warning: review_pending_invalid"
                )
