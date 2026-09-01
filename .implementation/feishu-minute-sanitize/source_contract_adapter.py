#!/usr/bin/env python3
"""Lossless structural adapter for reviewed meeting-minutes Markdown.

The active meeting-minutes contract uses speaker/stage headings while the
pinned sanitizer accepts explicit speaker headings and bare topic markers.
This module performs only deterministic structural rewrites in memory.  It
does not sanitize identities and never writes the adapted source to disk.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
import re


ADAPTER_VERSION = "meeting-minutes-source-adapter/v1"
RESTRICTED_DISTRIBUTION_PATTERNS = (
    re.compile(r"不\s*要\s*传\s*出\s*去"),
    re.compile(r"以\s*我\s*为\s*准"),
)
CANONICAL_TITLE = "# 投资会议纪要"
BODY_HEADING = "## 一、发言整理"
PENDING_HEADING = "## 二、存疑与待确认"


class SourceContractError(ValueError):
    """Content-free, stable source-contract failure."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.safe_message = message


def adapter_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def pipeline_rules_version(skill_rules_version: str) -> str:
    value = skill_rules_version.strip()
    if not value:
        raise SourceContractError("skill_rules_version_missing", "Skill rules version is unavailable.")
    return f"{value}+{ADAPTER_VERSION}#sha256:{adapter_sha256()}"


def adapt_source_contract(content: bytes, *, expected_meeting_date: str = "") -> bytes:
    try:
        text = content.decode("utf-8")
    except UnicodeError as exc:
        raise SourceContractError("source_encoding_invalid", "Source archive must be UTF-8 Markdown.") from exc

    if any(pattern.search(text) for pattern in RESTRICTED_DISTRIBUTION_PATTERNS):
        raise SourceContractError(
            "restricted_distribution_language",
            "Source contains distribution or attribution restrictions and requires human resolution.",
        )

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    stripped_lines = [line.strip() for line in normalized.splitlines()]
    title_count = stripped_lines.count(CANONICAL_TITLE)
    body_heading_count = stripped_lines.count(BODY_HEADING)
    pending_heading_count = stripped_lines.count(PENDING_HEADING)
    if title_count == 0 and body_heading_count == 0:
        # Preserve the pinned skill's existing standalone input contract.
        return content
    if title_count != 1 or body_heading_count != 1 or pending_heading_count > 1:
        raise SourceContractError(
            "canonical_structure_invalid",
            "Canonical source has missing or duplicate top-level structure.",
        )

    before_body, after_body = normalized.split(BODY_HEADING, 1)
    if PENDING_HEADING in after_body:
        body, pending = after_body.split(PENDING_HEADING, 1)
    else:
        body, pending = after_body, ""

    metadata = _parse_metadata(before_body)
    meeting_date = metadata.get("会议日期", "")
    if not meeting_date:
        raise SourceContractError("meeting_date_missing", "Canonical source has no meeting date.")
    try:
        normalized_date = datetime.strptime(meeting_date, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise SourceContractError("meeting_date_invalid", "Canonical source meeting date is invalid.") from exc
    if normalized_date != meeting_date:
        raise SourceContractError("meeting_date_invalid", "Canonical source meeting date is invalid.")
    if expected_meeting_date and meeting_date != expected_meeting_date:
        raise SourceContractError(
            "meeting_date_mismatch",
            "Canonical source meeting date does not match the reviewed record.",
        )
    meeting_type = metadata.get("会议类型", "")
    if meeting_type not in {"多人复盘会", "公司交流", "专家交流"}:
        raise SourceContractError("meeting_type_invalid", "Canonical source has an unsupported meeting type.")

    out: list[str] = ["# 已审核投资会议纪要", ""]
    for name in ("会议日期", "会议类型"):
        value = metadata.get(name)
        if value:
            out.append(f"{name}：{value}")
    info = [(name, metadata.get(name, "")) for name in ("会议标题", "会议系列", "会议标的")]
    info = [(name, value) for name, value in info if value]
    if info:
        out.extend(["", "【会议信息】"])
        out.extend(f"原{name}：{value}" for name, value in info)

    if meeting_type == "多人复盘会":
        adapted_body = _adapt_review_body(body)
    else:
        adapted_body = _adapt_stage_body(body)
    out.extend(["", *adapted_body])

    adapted_pending = _adapt_pending_table(pending)
    if adapted_pending:
        out.extend(["", "## 存疑与待确认", "", *adapted_pending])
    return ("\n".join(out).rstrip() + "\n").encode("utf-8")


def _parse_metadata(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    pattern = re.compile(r"(?m)^\s*\*\*(会议日期|会议类型|会议标题|会议系列|会议标的)\*\*\s*[:：]\s*(.*?)\s*$")
    for match in pattern.finditer(text):
        name, value = match.groups()
        value = value.strip()
        if name in result:
            raise SourceContractError("metadata_duplicate", "Canonical source repeats meeting metadata.")
        if value:
            result[name] = value
    return result


def _clean_body_lines(text: str) -> list[str]:
    lines = text.splitlines()
    while lines and (not lines[0].strip() or lines[0].strip() == "---"):
        lines.pop(0)
    while lines and (not lines[-1].strip() or lines[-1].strip() == "---"):
        lines.pop()
    return lines


def _adapt_review_body(text: str) -> list[str]:
    out: list[str] = []
    speaker_seen = False
    topic_seen_for_speaker = False
    for raw in _clean_body_lines(text):
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped == "---":
            if out and out[-1] != "":
                out.append("")
            continue
        speaker = re.fullmatch(r"###\s+(.+?)\s*", stripped)
        if speaker:
            label = speaker.group(1).strip()
            if not label or label.startswith("【"):
                raise SourceContractError("speaker_heading_invalid", "Canonical speaker heading is invalid.")
            out.extend([f"### 发言人：{label}", ""])
            speaker_seen = True
            topic_seen_for_speaker = False
            continue
        topic = re.fullmatch(r"####\s+【([^】]+)】\s*", stripped)
        if topic:
            if not speaker_seen:
                raise SourceContractError("speaker_heading_missing", "Topic appears before a canonical speaker heading.")
            out.extend([f"【{topic.group(1).strip()}】", ""])
            topic_seen_for_speaker = True
            continue
        target = re.fullmatch(r"#####\s+【([^】]+)】\s*", stripped)
        if target:
            if not topic_seen_for_speaker:
                raise SourceContractError("target_without_topic", "Canonical target appears without a topic.")
            out.extend([f"证券标的：{target.group(1).strip()}", ""])
            continue
        if stripped.startswith("#"):
            raise SourceContractError("body_heading_unsupported", "Canonical body contains an unsupported heading level.")
        if not speaker_seen:
            raise SourceContractError("speaker_heading_missing", "Canonical review body has content before a speaker heading.")
        if not topic_seen_for_speaker:
            out.extend(["【未分主题】", ""])
            topic_seen_for_speaker = True
        out.append(line)
    if not speaker_seen:
        raise SourceContractError("speaker_heading_missing", "Canonical review body has no speaker heading.")
    return _compact_blank_lines(out)


_KNOWN_STAGE_LABELS = {
    "管理层介绍",
    "公司介绍",
    "业务介绍",
    "投资者问答",
    "问答环节",
    "主持人/专家",
    "主持人与专家",
    "专家问答",
}
_COMMON_SURNAMES = frozenset("赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜谢邹喻柏窦章云苏潘葛范彭郎鲁韦昌马苗方俞任袁柳鲍史唐费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余顾孟平黄穆萧尹姚邵汪祁毛禹狄米贝戴谈宋茅庞熊纪舒屈项祝董梁杜阮蓝闵席季贾路娄江童颜郭梅盛林刁钟徐邱骆高夏蔡田樊胡凌霍虞万柯卢莫房解应宗丁宣邓郁单杭洪包左石崔吉龚程邢裴陆荣翁")


def _looks_like_person_stage(label: str) -> bool:
    compact = re.sub(r"\s+", "", label)
    if compact in _KNOWN_STAGE_LABELS:
        return False
    if re.fullmatch(r"发言人\d+", compact) or re.fullmatch(r"[\u4e00-\u9fff]{1,4}(?:老师|博士|经理|总)", compact):
        return True
    return bool(re.fullmatch(r"[\u4e00-\u9fff]{2,3}", compact) and compact[0] in _COMMON_SURNAMES)


def _adapt_stage_body(text: str) -> list[str]:
    out: list[str] = []
    stage = ""
    marker_emitted = False
    for raw in _clean_body_lines(text):
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped == "---":
            if out and out[-1] != "":
                out.append("")
            continue
        heading = re.fullmatch(r"###\s+(.+?)\s*", stripped)
        if heading:
            stage = heading.group(1).strip()
            if _looks_like_person_stage(stage):
                raise SourceContractError(
                    "ambiguous_person_stage",
                    "Company or expert source contains a person-like stage heading that requires human resolution.",
                )
            marker_emitted = False
            continue
        question = re.fullmatch(r"\*\*【([^】]+)】\*\*", stripped)
        if question:
            if not stage:
                raise SourceContractError("stage_heading_missing", "Canonical question appears before a stage heading.")
            out.extend([f"【{stage}·问题：{question.group(1).strip()}】", ""])
            marker_emitted = True
            continue
        if stripped.startswith("#"):
            raise SourceContractError("body_heading_unsupported", "Canonical body contains an unsupported heading level.")
        if not stage:
            raise SourceContractError("stage_heading_missing", "Canonical body has content before a stage heading.")
        if not marker_emitted:
            out.extend([f"【{stage}】", ""])
            marker_emitted = True
        out.append(line)
    if not stage:
        raise SourceContractError("stage_heading_missing", "Canonical body has no stage heading.")
    return _compact_blank_lines(out)


def _table_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _is_separator(cells: list[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in cells)


def _adapt_pending_table(text: str) -> list[str]:
    lines = [line for line in _clean_body_lines(text) if line.strip() and line.strip() != "---"]
    if not lines:
        return []
    table_lines = [line for line in lines if "|" in line]
    if len(table_lines) != len(lines) or len(table_lines) < 2:
        raise SourceContractError("pending_table_invalid", "Canonical pending section must contain only the fixed table.")
    header = _table_cells(table_lines[0])
    separator = _table_cells(table_lines[1])
    allowed = ["原始表述", "当前判断", "候选项", "人工确认"]
    if header not in (allowed, ["时间戳", *allowed]) or len(separator) != len(header) or not _is_separator(separator):
        raise SourceContractError("pending_columns_invalid", "Canonical pending table has unsupported columns.")
    result: list[str] = []
    for raw in table_lines[2:]:
        cells = _table_cells(raw)
        if len(cells) != len(header):
            raise SourceContractError("pending_row_invalid", "Canonical pending table has an invalid row.")
        values = dict(zip(header, cells))
        if not values.get("原始表述"):
            raise SourceContractError("pending_row_invalid", "Canonical pending row is missing the original expression.")
        # Recording timestamps are source locators and are intentionally not
        # forwarded; every business column remains explicit and ordered.
        result.append("- " + "；".join(f"{name}：{values.get(name, '')}" for name in allowed))
    return result


def _compact_blank_lines(lines: list[str]) -> list[str]:
    result: list[str] = []
    for line in lines:
        if line == "" and (not result or result[-1] == ""):
            continue
        result.append(line)
    while result and result[-1] == "":
        result.pop()
    return result
