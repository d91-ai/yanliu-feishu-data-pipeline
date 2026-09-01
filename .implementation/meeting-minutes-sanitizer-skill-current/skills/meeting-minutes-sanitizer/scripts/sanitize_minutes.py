#!/usr/bin/env python3
"""Sanitize Chinese investment meeting minutes into one Markdown output."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable


ANONYMIZATION_LEVEL = "L2_FACT_PRESERVED"
SUPPORTED_SUFFIXES = {".md", ".txt"}

FILLER_PATTERNS = [
    r"(?<![\u4e00-\u9fffA-Za-z0-9])(?:OK|ok|嗯+|呃+|啊+|哈{2,})(?![\u4e00-\u9fffA-Za-z0-9])",
    r"怎么说呢",
    r"坦白讲",
    r"老实说",
]

GENERIC_SPEAKER_WORDS = [
    "某某",
    "某人",
    "发言人",
    "发言人A",
    "发言人B",
    "嘉宾",
    "专家",
    "老师",
    "主持人",
    "主讲人",
    "分享人",
    "报告人",
    "参会嘉宾",
]

IDENTITY_VALUE_LABEL_PATTERN = (
    r"(?:发言人(?![A-Z])|姓名|身份|发言人别名|发言人称谓|别名|称谓|参会嘉宾|主讲人|分享人|报告人|"
    r"发言机构|任职机构|发言公司|任职公司|所在部门|所在地|"
    r"会议预定人|预定人|主持人)"
)
MARKDOWN_FIELD_WRAPPER = r"[*_`]{0,3}"
MARKDOWN_FIELD_PREFIX = r"(?:>\s*)?(?:(?:[-*+]\s*)|(?:\d{1,3}[.)]\s+))?"
SPEAKER_VALUE_RE = re.compile(
    rf"^\s*{MARKDOWN_FIELD_PREFIX}{MARKDOWN_FIELD_WRAPPER}{IDENTITY_VALUE_LABEL_PATTERN}"
    rf"{MARKDOWN_FIELD_WRAPPER}\s*[:：]\s*(.+?)\s*$",
    re.MULTILINE,
)
IDENTITY_LINE_RE = re.compile(
    rf"^\s*{MARKDOWN_FIELD_PREFIX}{MARKDOWN_FIELD_WRAPPER}"
    rf"(?:{IDENTITY_VALUE_LABEL_PATTERN}|职位|职务|履历|个人背景|个人介绍)"
    rf"{MARKDOWN_FIELD_WRAPPER}\s*[:：]"
)
DOCUMENT_METADATA_LINE_RE = re.compile(
    rf"^\s*{MARKDOWN_FIELD_PREFIX}{MARKDOWN_FIELD_WRAPPER}"
    rf"(?:会议日期|会议时间|会议类型|脱敏等级|处理说明)"
    rf"{MARKDOWN_FIELD_WRAPPER}\s*[:：]"
)
HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", re.MULTILINE)
EXPLICIT_SPEAKER_HEADING_RE = re.compile(
    r"^(?:发言人[A-Z]?|姓名|主持人|主讲人|分享人|报告人|参会嘉宾|嘉宾|专家|老师)(?:\s*[:：]\s*(.+?))?$"
)
INLINE_IDENTITY_VALUE_RE = re.compile(
    rf"(?:{IDENTITY_VALUE_LABEL_PATTERN}|职位|职务)\s*[:：]\s*([^；;,，|｜（）()]+)"
)
TOPIC_MARKER_RE = re.compile(r"(?m)^\s*【([^】\n]*)】\s*$")
UNSUPPORTED_SOURCE_HEADING_ROOTS = (
    "原始纪要",
    "外部核验",
    "外部验证",
    "用户修正",
    "人工修正",
    "模型推断",
    "本地候选",
    "候选项",
    "用户确认",
    "人工确认",
    "确认项",
    "已确认",
    "已否决",
    "主源",
)
DECISION_TABLE_MARKERS = frozenset(
    {"原始表述", "当前判断", "模型推断", "本地候选", "候选项", "用户修正", "人工修正", "用户确认", "人工确认", "确认项"}
)

COMMON_SINGLE_SURNAMES = frozenset(
    "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜谢邹喻柏窦章云苏潘葛范彭郎鲁韦昌马苗方俞任袁柳鲍史唐费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余顾孟平黄穆萧尹姚邵汪祁毛禹狄米贝戴谈宋茅庞熊纪舒屈项祝董梁杜阮蓝闵席季贾路娄江童颜郭梅盛林刁钟徐邱骆高夏蔡田樊胡凌霍虞万柯卢莫房解应宗丁宣邓郁单杭洪包左石崔吉龚程邢裴陆荣翁荀羊甄曲封储靳段富巫乌焦巴牧山谷车侯全班仰秋仲伊宫宁栾甘厉祖武符刘景詹束龙叶幸韶黎白怀蒲鄂索籍赖卓蔺屠蒙池乔翟谭姬申冉牛寿通边燕浦尚农温庄晏柴瞿阎连茹习艾容向古易慎廖庾居衡步都耿满弘匡国文寇广东欧沃利蔚越隆师巩聂辛简饶曾沙鞠丰关查游权盖益桓"
)
COMMON_COMPOUND_SURNAMES = (
    "欧阳",
    "司马",
    "上官",
    "诸葛",
    "夏侯",
    "东方",
    "皇甫",
    "尉迟",
    "公孙",
    "慕容",
    "令狐",
    "宇文",
    "长孙",
)

TIMESTAMP_RES = [
    re.compile(r"[（(]\s*录音约\s*\d{1,2}:\d{2}(?::\d{2})?\s*[）)]"),
    re.compile(r"\[\s*\d{1,2}:\d{2}(?::\d{2})?\s*\]"),
    re.compile(
        r"(?m)^\s*\d{1,2}:\d{2}(?::\d{2})?\s+(?=(?:发言人[A-Z]?|主持人|主讲人|分享人|报告人|参会嘉宾|嘉宾|专家|老师)\s*[:：])"
    ),
]

ATTRIBUTION_VERBS = "认为|表示|说|讲|提到|指出|判断|反馈|强调|分享|补充|称|介绍"
GENERIC_ATTRIBUTION_RE = re.compile(
    rf"(?m)^\s*(?:{'|'.join(map(re.escape, GENERIC_SPEAKER_WORDS))})\s*(?:{ATTRIBUTION_VERBS})[，,：:\s]*"
)
MEETING_ROLE_ATTRIBUTION_RE = re.compile(
    rf"(?:据\s*)?(?:发言人[A-Z]?|主持人|主讲人|分享人|报告人|参会嘉宾)\s*(?:{ATTRIBUTION_VERBS})[，,：:\s]*"
)
FIRST_PERSON_SPEECH_RE = re.compile(
    r"(?:我|我们|咱们|个人)\s*(认为|判断|觉得|感觉|看|预计|建议|倾向(?:于)?|推测|观察到|注意到|了解到|了解|认同|明白)"
)
PERSON_REFERENCE_VERBS = (
    ATTRIBUTION_VERBS + "|负责|担任|任职|加入|离任|参与|主导|联系|对接|建议|预计|推测"
)
PERSON_LIKE_REFERENCE_RE = re.compile(
    rf"(?:^|[，。；：、,\s]|据|由|与|向|和|及|的)(?P<name>(?:[{''.join(sorted(COMMON_SINGLE_SURNAMES))}][\u4e00-\u9fff]{{0,2}}(?:总|老师|博士|经理)|"
    rf"[{''.join(sorted(COMMON_SINGLE_SURNAMES))}][\u4e00-\u9fff]{{1,2}}|"
    rf"(?:{'|'.join(COMMON_COMPOUND_SURNAMES)})[\u4e00-\u9fff]{{1,2}}))\s*(?=(?:在[^，。；]{{0,12}})?(?:{PERSON_REFERENCE_VERBS})|的(?:观点|判断|看法|反馈|建议|履历|经历))",
    re.MULTILINE,
)
ENGLISH_PERSON_REFERENCE_RE = re.compile(
    rf"(?<![A-Za-z])(?P<name>[A-Z][A-Za-z.'-]+(?:\s+[A-Z][A-Za-z.'-]+)+)\s*"
    rf"(?=(?:(?:在|于)[^，。；\n]{{0,12}})?(?:{PERSON_REFERENCE_VERBS})|"
    rf"的(?:观点|判断|看法|反馈|建议|履历|经历))"
)
ENGLISH_SINGLE_PERSON_REFERENCE_RE = re.compile(
    rf"(?<![A-Za-z])(?P<name>[A-Z][A-Za-z.'-]{{1,30}})\s*"
    rf"(?=(?:(?:在|于)[^，。；\n]{{0,12}})?(?:{PERSON_REFERENCE_VERBS})|"
    rf"的(?:观点|判断|看法|反馈|建议|履历|经历))"
)
TITLED_ALIAS_REFERENCE_RE = re.compile(
    rf"(?P<name>[\u4e00-\u9fff]{{1,4}}(?:哥|姐|董|总|老师|博士|经理))\s*"
    rf"(?=(?:(?:在|于)[^，。；\n]{{0,12}})?(?:{PERSON_REFERENCE_VERBS})|"
    rf"的(?:观点|判断|看法|反馈|建议|履历|经历))"
)
CHINESE_PERSON_NAME_RE_FRAGMENT = (
    rf"(?:[{''.join(sorted(COMMON_SINGLE_SURNAMES))}][\u4e00-\u9fff]{{1,2}}|"
    rf"(?:{'|'.join(COMMON_COMPOUND_SURNAMES)})[\u4e00-\u9fff]{{1,2}})"
)
IDENTIFYING_ROLE_REFERENCE_RE = re.compile(
    rf"(?:董事长|总经理|副总裁|首席执行官|首席财务官|首席技术官|CEO|CFO|CTO|分析师|研究员)"
    rf"\s*(?:{CHINESE_PERSON_NAME_RE_FRAGMENT}\s*)?(?:(?:在|于)[^，。；]{{0,12}})?"
    rf"(?:{PERSON_REFERENCE_VERBS})",
    re.I,
)
PERSON_TOPIC_REFERENCE_RE = re.compile(
    rf"(?P<name>{CHINESE_PERSON_NAME_RE_FRAGMENT})(?:的)?(?:观点|判断|看法|反馈|建议|履历|经历|分享)$"
)
DIRECT_IDENTIFIER_RES = (
    re.compile(r"(?<!\d)(?:\+?86[ -]?)?1[3-9](?:[ -]?\d){9}(?!\d)"),
    re.compile(r"(?<!\d)0\d{2,3}[ -]?\d{7,8}(?!\d)"),
    re.compile(r"(?<!\d)\d{17}[0-9Xx](?!\d)"),
    re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![A-Za-z0-9.-])"),
    re.compile(r"(?i)https?://|www\."),
    re.compile(
        r"(?:企业微信(?:号|账号|帐号|\s*ID)?|微信(?:号|账号|帐号|\s*ID)?|"
        r"WeChat(?:\s*(?:ID|Account))?|手机号|手机号码|联系电话|电话|电话号码|办公电话|座机|"
        r"邮箱|身份证号)"
        r"\s*(?:[:：]|为|是)",
        re.I,
    ),
    re.compile(r"(?i)(?<![A-Za-z0-9])wxid_?[A-Za-z0-9_-]{3,}"),
    re.compile(r"(?:联系人|对接人)\s*(?:[:：]|为)\s*[^，。；\n]{1,64}"),
)
SOURCE_LOCATOR_RES = (
    re.compile(
        r"(?:原文位置|来源文件|源文件|录音文件|音频文件|附件位置|记录\s*ID|文档\s*ID|(?:source|record|document)[_ -]?id)\s*(?:[:：]|为|是)",
        re.I,
    ),
    re.compile(
        r"(?:原文|来源|源文档|文档|材料|纪要|附件)[^，。；：:,\n]{0,24}"
        r"(?:[，,:：]\s*)?第\s*\d+\s*页"
        r"(?:[^，。；：:,\n]{0,12}(?:[，,:：]\s*)?第\s*\d+\s*段)?"
    ),
    re.compile(r"(?:录音|音频)[^，。；\n]{0,12}\d{1,2}:\d{2}(?::\d{2})?"),
    re.compile(r"(?i)(?:\.docx?|\.pdf|\.md|\.txt|\.wav|\.mp3|\.m4a)(?![A-Za-z0-9])"),
)


@dataclass
class TopicUnit:
    full_topic: str
    topic: str
    target: str
    text: str
    entities: list[str]


def read_input(path: Path) -> str:
    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise SystemExit("Only .md and .txt input is supported. Convert .docx to Markdown or plain text first.")
    try:
        return path.read_text(encoding="utf-8-sig")
    except FileNotFoundError as exc:
        raise SystemExit(f"Input file not found: {path.name}") from exc
    except PermissionError as exc:
        raise SystemExit(f"Input file is not readable: {path.name}") from exc
    except UnicodeDecodeError as exc:
        raise SystemExit(f"Input file is not valid UTF-8 text: {path.name}") from exc
    except OSError as exc:
        raise SystemExit(f"Failed to read input file {path.name}: {exc.strerror or exc}") from exc


def parse_meeting_date(text: str, override: str | None = None) -> str:
    if override:
        return normalize_date(override, strict=True, error_context="--meeting-date")

    plain_text = strip_markdown_emphasis(text)
    labeled = re.search(
        rf"(?m)^\s*{MARKDOWN_FIELD_PREFIX}(?:会议日期|会议时间)\s*[:：]\s*([^\n\r]+?)\s*$",
        plain_text,
    )
    if labeled:
        return normalize_date(labeled.group(1), strict=True, error_context="meeting date metadata")

    return "unknown"


def normalize_date(raw: str, strict: bool = False, error_context: str = "meeting date") -> str:
    match = re.fullmatch(r"\s*(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})日?\s*", raw)
    if not match:
        if strict:
            raise SystemExit(f"Invalid {error_context}. Use a real calendar date in YYYY-MM-DD format.")
        return "unknown"
    year, month, day = match.groups()
    try:
        return date(int(year), int(month), int(day)).isoformat()
    except ValueError as exc:
        if strict:
            raise SystemExit(f"Invalid {error_context}. Use a real calendar date in YYYY-MM-DD format.") from exc
        return "unknown"


def parse_meeting_type(text: str, speaker_names: list[str]) -> str:
    match = re.search(
        rf"(?m)^\s*{MARKDOWN_FIELD_PREFIX}会议类型\s*[:：]\s*([^\n\r]+?)\s*$",
        strip_markdown_emphasis(text),
    )
    if not match:
        return "未识别"
    value = neutralize_text(match.group(1), speaker_names)
    for identity_value in speaker_variants(speaker_names):
        value = re.sub(re.escape(identity_value), "", value)
    return normalize_sentences(value).rstrip("。") or "未识别"


def strip_markdown_emphasis(text: str) -> str:
    return re.sub(r"[*_`]+", "", text)


def collect_speaker_names(text: str) -> list[str]:
    names: list[str] = []

    for match in SPEAKER_VALUE_RE.finditer(text):
        for candidate in re.split(r"[、,，;/；|｜]+", match.group(1)):
            add_speaker_candidate(names, candidate, explicit=True)

    for match in HEADING_RE.finditer(text):
        label = clean_heading_label(match.group(1))
        explicit_match = EXPLICIT_SPEAKER_HEADING_RE.fullmatch(label)
        if explicit_match:
            candidate = explicit_match.group(1)
            if candidate:
                add_speaker_candidate(names, candidate, explicit=True)
    return dedupe(names)


def add_speaker_candidate(names: list[str], raw_name: str, explicit: bool) -> None:
    name = clean_heading_label(raw_name)
    for annotation in re.findall(r"[（(]([^（）()]*)[）)]", name):
        labeled_values = INLINE_IDENTITY_VALUE_RE.findall(annotation)
        values = labeled_values or [annotation]
        for value in values:
            for candidate in re.split(r"[、,，;/；|｜]+", value):
                add_explicit_identity_value(names, candidate)
    name = re.sub(r"[（(].*?[）)]", "", name).strip()
    if not name or name in GENERIC_SPEAKER_WORDS or len(name) > 80:
        return
    if explicit or is_probable_person_speaker(name):
        names.append(name)


def add_explicit_identity_value(names: list[str], raw_value: str) -> None:
    value = clean_heading_label(raw_value).strip(" \t:：")
    if value and value not in GENERIC_SPEAKER_WORDS and len(value) <= 80:
        names.append(value)


def clean_heading_label(label: str) -> str:
    return re.sub(r"[#*`_>]+", "", label).strip()


def is_probable_person_speaker(name: str) -> bool:
    if not name:
        return False
    compact = re.sub(r"\s+", "", name)
    titled = re.fullmatch(r"([\u4e00-\u9fff]{1,4})(?:老师|博士|经理|总)", compact)
    if titled:
        base = titled.group(1)
        if base[0] in COMMON_SINGLE_SURNAMES or base.startswith(COMMON_COMPOUND_SURNAMES):
            return True
    compact = re.sub(r"(?:老师|博士|经理|总)$", "", compact)
    if not compact:
        return False
    if re.fullmatch(r"[\u4e00-\u9fff]{2,3}", compact) and compact[0] in COMMON_SINGLE_SURNAMES:
        return True
    if re.fullmatch(r"[\u4e00-\u9fff]{3,4}", compact) and compact.startswith(COMMON_COMPOUND_SURNAMES):
        return True
    if re.fullmatch(r"[A-Za-z][A-Za-z.'-]+(?:\s+[A-Za-z][A-Za-z.'-]+)+", name.strip()):
        return True
    return False


def is_ambiguous_person_value(value: str) -> bool:
    compact = re.sub(r"\s+", "", value)
    if is_probable_person_speaker(value):
        return True
    if re.fullmatch(r"[\u4e00-\u9fff]{1,4}(?:哥|姐|董|总|老师|博士|经理)", compact):
        return True
    return bool(re.fullmatch(r"[A-Z][A-Za-z.'-]{1,30}", value.strip()))


def validate_source_and_heading_modes(text: str, speaker_names: list[str]) -> None:
    for match in HEADING_RE.finditer(text):
        label = clean_heading_label(match.group(1))
        if is_unsupported_source_heading(label):
            raise SystemExit(
                "Mixed-source, evidence-layer, candidate, or correction sections are not supported. Resolve them into one reviewed source before sanitization."
            )
        if EXPLICIT_SPEAKER_HEADING_RE.fullmatch(label):
            continue
        if label in speaker_names:
            continue
        if is_probable_person_speaker(label):
            raise SystemExit(
                "Ambiguous person-like Markdown heading. Label it explicitly as a speaker or as a business topic before sanitization."
            )

    for line in text.splitlines():
        cells = [clean_heading_label(cell).strip() for cell in re.split(r"[|｜]", line) if cell.strip()]
        if len(set(cells) & DECISION_TABLE_MARKERS) >= 2:
            raise SystemExit(
                "Candidate/confirmation decision tables are not supported. Resolve decisions into one reviewed source before sanitization."
            )


def is_unsupported_source_heading(label: str) -> bool:
    normalized = clean_heading_label(label).strip(" \t:：")
    normalized = re.sub(
        r"^(?:(?:第?[一二三四五六七八九十百0-9]+(?:章|节|部分)?)|(?:[（(][一二三四五六七八九十0-9]+[）)]))\s*[、.．:：-]?\s*",
        "",
        normalized,
    )
    candidate = re.split(r"[:：]", normalized, maxsplit=1)[0].strip()
    for root in UNSUPPORTED_SOURCE_HEADING_ROOTS:
        if re.fullmatch(rf"{re.escape(root)}(?:结果|说明|内容)?(?:[（(][^（）()]{{1,20}}[）)])?", candidate):
            return True
    return False


def remove_timestamps(text: str, speaker_names: list[str] | None = None) -> str:
    cleaned = text
    for pattern in TIMESTAMP_RES:
        cleaned = pattern.sub("", cleaned)
    for speaker in speaker_variants(speaker_names or []):
        cleaned = re.sub(
            rf"(?m)^\s*\d{{1,2}}:\d{{2}}(?::\d{{2}})?\s+(?={re.escape(speaker)}\s*[:：])",
            "",
            cleaned,
        )
    return cleaned


def strip_speaker_headings_and_identity(text: str, speaker_names: list[str]) -> str:
    kept_lines: list[str] = []
    for raw_line in text.splitlines():
        line = remove_timestamps(raw_line, speaker_names).strip()
        if not line:
            kept_lines.append("")
            continue
        heading_match = HEADING_RE.fullmatch(line)
        if heading_match:
            label = clean_heading_label(heading_match.group(1))
            if EXPLICIT_SPEAKER_HEADING_RE.fullmatch(label) or label in speaker_names:
                continue
            kept_lines.append(line)
            continue
        if IDENTITY_LINE_RE.match(line):
            continue
        if DOCUMENT_METADATA_LINE_RE.match(line):
            continue
        line = re.sub(r"^\s*(?:发言人[A-Z]?|嘉宾|专家|老师|主持人|[\u4e00-\u9fff]{1,4}(?:总|老师|博士|经理))\s*[:：]\s*", "", line)
        kept_lines.append(line)
    return "\n".join(kept_lines)


def split_pending_section(text: str) -> tuple[str, list[str]]:
    pattern = re.compile(r"^\s{0,3}#{0,6}\s*(?:[一二三四五六七八九十]、)?(?:存疑与待确认|待确认业务事项|业务存疑事项|存疑事项)\s*$", re.MULTILINE)
    match = pattern.search(text)
    if not match:
        return text, []

    tail = text[match.end() :]
    boundary = re.search(r"(?m)^\s*(?:#{1,6}\s+.+|【[^】\n]*】)\s*$", tail)
    if boundary:
        pending_text = tail[: boundary.start()]
        main_text = text[: match.start()] + "\n" + tail[boundary.start() :]
    else:
        pending_text = tail
        main_text = text[: match.start()]

    pending_items: list[str] = []
    for line in pending_text.splitlines():
        item = line.strip()
        if not item or re.match(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?$", item):
            continue
        item = re.sub(r"^\s*[-*]\s*", "", item)
        item = re.sub(r"^\|", "", item).rstrip("|")
        item = "；".join(part.strip() for part in item.split("|") if part.strip())
        compact_item = re.sub(r"[\s；;|]", "", item)
        if "发言人" in compact_item and ("事项" in compact_item or "时间" in compact_item):
            continue
        if "原始表述" in compact_item and "当前判断" in compact_item:
            continue
        pending_items.append(item)
    return main_text, pending_items


def split_topic_units(text: str, speaker_names: list[str]) -> list[TopicUnit]:
    matches = list(TOPIC_MARKER_RE.finditer(text))
    if not matches:
        cleaned = neutralize_text(text, speaker_names)
        return [make_topic_unit("未分主题", cleaned, speaker_names)] if cleaned else []

    units: list[TopicUnit] = []
    prefix = neutralize_text(text[: matches[0].start()], speaker_names)
    if prefix:
        units.append(make_topic_unit("概览", prefix, speaker_names))

    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        topic_label = match.group(1).strip()
        body = neutralize_text(text[start:end], speaker_names)
        if body:
            units.append(make_topic_unit(topic_label, body, speaker_names))
    return units


def make_topic_unit(topic_label: str, body: str, speaker_names: list[str] | None = None) -> TopicUnit:
    topic_label = sanitize_topic_label(topic_label, speaker_names or [])
    topic, target = split_topic_label(topic_label)
    return TopicUnit(
        full_topic=topic_label,
        topic=topic,
        target=target,
        text=body,
        entities=extract_entities(topic, target, body),
    )


def sanitize_topic_label(topic_label: str, speaker_names: list[str]) -> str:
    label = strip_quote_marks(strip_inline_markup(remove_timestamps(topic_label, speaker_names)))
    label = MEETING_ROLE_ATTRIBUTION_RE.sub("", label)
    label = GENERIC_ATTRIBUTION_RE.sub("", label)
    label = re.sub(r"\s+", " ", label).strip(" ，,；;：:。")
    return label or "未分主题"


def split_topic_label(label: str) -> tuple[str, str]:
    parts = re.split(r"[｜|]", label, maxsplit=1)
    topic = parts[0].strip() if parts else label.strip()
    target = parts[1].strip() if len(parts) > 1 else ""
    return topic, target


def neutralize_text(text: str, speaker_names: list[str]) -> str:
    text = remove_timestamps(text, speaker_names)
    text = strip_inline_markup(text)
    text = strip_quote_marks(text)
    text = MEETING_ROLE_ATTRIBUTION_RE.sub("", text)
    text = GENERIC_ATTRIBUTION_RE.sub("", text)

    for speaker in speaker_variants(speaker_names):
        escaped = re.escape(speaker)
        text = re.sub(rf"{escaped}\s*(?:{ATTRIBUTION_VERBS})[，,：:\s]*", "", text)
        text = re.sub(rf"据\s*{escaped}\s*(?:{ATTRIBUTION_VERBS})[，,：:\s]*", "", text)
        text = re.sub(rf"{escaped}\s*[:：]", "", text)

    text = re.sub(r"(?:我|个人|我们|咱们)\s*(?:认为|判断)\s*[，,：:]*", "判断：", text)
    text = re.sub(r"(?:我|个人|我们|咱们)\s*(?:觉得|感觉|看)\s*[，,：:]*", "观点：", text)
    text = re.sub(
        r"(?:我|个人|我们|咱们)\s*(预计|建议|倾向(?:于)?|推测|观察到|注意到)\s*",
        r"\1",
        text,
    )
    text = re.sub(r"(?:我的|我们的|个人的)\s*(?:观点|判断|感觉)\s*(?:是|为)?\s*[，,：:]*", "观点：", text)
    text = re.sub(r"不要传出去|以我为准", "", text)
    text = re.sub(r"我不确定", "不确定", text)
    text = re.sub(r"我了解到", "了解到", text)
    text = re.sub(r"我了解的", "了解的", text)
    text = re.sub(r"我并不认同", "不认同", text)
    text = re.sub(r"我不明白", "尚不明确", text)
    text = re.sub(r"我(?:都)?有点不能理解", "存在疑问", text)
    text = re.sub(r"我这边", "", text)
    text = re.sub(r"我这里", "", text)
    text = re.sub(r"我自己", "", text)
    text = re.sub(r"结合我对", "结合对", text)
    text = re.sub(r"咱们|咱", "", text)
    text = re.sub(r"其他老师还有补充吗[^。！？]*[。！？]?", "", text)
    text = re.sub(r"[^。！？]*会议就结束[^。！？]*[。！？]?", "", text)
    text = re.sub(r"拜拜[。！？]?", "", text)
    text = re.sub(r"发言人\s*未在", "原文未在", text)
    text = re.sub(r"发言人\s*未", "原文未", text)

    for pattern in FILLER_PATTERNS:
        text = re.sub(pattern, "", text)

    lines = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if re.search(r"(会议就结束|拜拜|其他老师还有补充吗)", line):
            continue
        if re.search(r"谢谢.+接下来.+(?:开始分享|分享)", line):
            continue
        line = re.sub(r"^#{1,6}\s+", "", line)
        if IDENTITY_LINE_RE.match(line):
            continue
        if DOCUMENT_METADATA_LINE_RE.match(line):
            continue
        line = re.sub(r"^\s*[-*]\s*", "", line)
        line = re.sub(r"\s+", " ", line)
        line = re.sub(r"\s*([，。；：、,.!?！？])\s*", r"\1", line)
        line = re.sub(r"[，,；;：:]+$", "。", line)
        lines.append(line)

    return normalize_sentences(" ".join(lines))


def speaker_variants(speaker_names: list[str]) -> list[str]:
    variants: list[str] = []
    for name in speaker_names:
        if name and name not in variants:
            variants.append(name)
        for suffix in ("老师", "博士", "经理", "总"):
            if name.endswith(suffix) and len(name) > len(suffix) + 1:
                base = name[: -len(suffix)]
                if base and base not in variants:
                    variants.append(base)
    return sorted(variants, key=len, reverse=True)


def strip_inline_markup(text: str) -> str:
    text = re.sub(r"</?u>", "", text, flags=re.I)
    text = re.sub(r"</?strong>", "", text, flags=re.I)
    text = re.sub(r"</?b>", "", text, flags=re.I)
    return re.sub(r"[*_`]+", "", text)


def strip_quote_marks(text: str) -> str:
    replacements = {
        "“": "",
        "”": "",
        "‘": "",
        "’": "",
        "「": "",
        "」": "",
        "『": "",
        "』": "",
        '"': "",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


def normalize_sentences(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"也[，,]\s*从", "从", text)
    text = re.sub(r"([。！？]){2,}", r"\1", text)
    text = re.sub(r"([，,]){2,}", r"\1", text)
    text = text.strip(" ，,；;：:")
    if text and text[-1] not in "。！？.!?":
        text += "。"
    return text


def extract_entities(topic: str, target: str, body: str) -> list[str]:
    entities: list[str] = []
    target_without_codes = target
    for match in re.finditer(r"([\u4e00-\u9fffA-Za-z0-9·]{2,20})[（(](\d{5,6}\.(?:SZ|SH|BJ|HK|US))[）)]", target, flags=re.I):
        add_entity(entities, match.group(1))
        add_entity(entities, match.group(2).upper())
        target_without_codes = target_without_codes.replace(match.group(0), " ")

    for part in re.split(r"[、,，/；;\s]+", target_without_codes):
        add_entity(entities, part)

    for match in re.finditer(r"([\u4e00-\u9fffA-Za-z0-9·]{2,20})[（(](\d{5,6}\.(?:SZ|SH|BJ|HK|US))[）)]", body, flags=re.I):
        add_entity(entities, match.group(1))
        add_entity(entities, match.group(2).upper())

    if not entities and topic:
        add_entity(entities, topic)
    return entities


def add_entity(entities: list[str], value: str) -> None:
    value = value.strip(" 。，,；;：:（）()[]【】")
    value = re.sub(r"^[和与及对像给把在从看]+", "", value)
    if not value or value in entities:
        return
    stopwords = {"行业", "公司", "客户", "市场", "业务", "价格", "材料", "产品", "订单", "产能", "良率", "上游", "下游", "显示"}
    if value in stopwords:
        return
    entities.append(value)


def build_document_id(raw_text: str) -> str:
    return hashlib.sha256(raw_text.encode("utf-8")).hexdigest()[:12]


def build_output_stem(raw_text: str, meeting_date: str, override: str | None = None) -> str:
    if override:
        value = override.strip()
        if (
            not value
            or value in {".", ".."}
            or value.startswith(".")
            or len(value) > 120
            or any(character in value for character in "/\\\r\n\t")
            or Path(value).name != value
            or bool(Path(value).suffix)
        ):
            raise SystemExit("Invalid --output-stem. Provide one reviewed filename stem without a path or extension.")
        return value
    return f"{meeting_date}_脱敏会议纪要_{build_document_id(raw_text)}"


def coded_business_names(unit: TopicUnit) -> set[str]:
    names: set[str] = set()
    source = f"{unit.full_topic}\n{unit.text}"
    for match in re.finditer(
        r"([\u4e00-\u9fffA-Za-z0-9·]{2,20})[（(](\d{5,6}\.(?:SZ|SH|BJ|HK|US))[）)]",
        source,
        flags=re.I,
    ):
        names.add(match.group(1))
    return names


def quality_check(
    units: list[TopicUnit],
    pending_items: list[str],
    speaker_names: list[str],
    meeting_type: str,
    output_stem: str,
) -> None:
    combined = "\n".join(
        [meeting_type, output_stem]
        + [unit.full_topic + "\n" + unit.text for unit in units]
        + pending_items
    )
    issues: list[str] = []

    if re.search(r"^\s*###\s+", combined, flags=re.MULTILINE):
        issues.append("Markdown speaker heading remains.")
    if GENERIC_ATTRIBUTION_RE.search(combined) or MEETING_ROLE_ATTRIBUTION_RE.search(combined):
        issues.append("Generic meeting-speaker attribution remains.")
    if IDENTIFYING_ROLE_REFERENCE_RE.search(combined):
        issues.append("Potentially identifying role attribution remains.")
    if re.search(r"发言人[A-Z]?", combined):
        issues.append("Speaker marker remains.")
    if re.search(r"[“\"「『][^”\"」』]{20,}[”\"」』]", combined):
        issues.append("Long direct quote remains.")
    if remove_timestamps(combined, speaker_names) != combined:
        issues.append("Recognized recording offset remains.")
    if FIRST_PERSON_SPEECH_RE.search(combined):
        issues.append("Recognized first-person speaking style remains.")

    for identity_value in speaker_variants(speaker_names):
        if identity_value and identity_value in combined:
            issues.append("A collected identity value remains.")

    for pattern in DIRECT_IDENTIFIER_RES:
        if pattern.search(combined):
            issues.append("A direct identifier or URL remains.")
            break

    for pattern in SOURCE_LOCATOR_RES:
        if pattern.search(combined):
            issues.append("A source locator remains.")
            break

    for pattern in (
        PERSON_LIKE_REFERENCE_RE,
        ENGLISH_PERSON_REFERENCE_RE,
        ENGLISH_SINGLE_PERSON_REFERENCE_RE,
        TITLED_ALIAS_REFERENCE_RE,
    ):
        if pattern.search(combined):
            issues.append("An unregistered person-like reference remains.")
            break

    for unit in units:
        explicitly_coded_names = coded_business_names(unit)
        for value in [unit.target, *unit.entities]:
            if value and is_ambiguous_person_value(value) and value not in explicitly_coded_names:
                issues.append("An ambiguous person-like topic target or entity remains.")
                break
        if is_ambiguous_person_value(unit.topic):
            issues.append("An ambiguous person-like topic remains.")
        if PERSON_TOPIC_REFERENCE_RE.search(unit.topic):
            issues.append("A person-like topic reference remains.")

        unit_source = (unit.full_topic + "\n" + unit.text).lower()
        for entity in unit.entities:
            if entity.lower() not in unit_source:
                issues.append("An extracted entity is not grounded in the sanitized topic text.")

    if issues:
        raise SystemExit("Quality check failed:\n- " + "\n- ".join(dedupe(issues)))


def render_markdown(
    units: list[TopicUnit],
    pending_items: list[str],
    meeting_date: str,
    meeting_type: str,
) -> str:
    lines = [
        "# 脱敏会议纪要",
        "",
        "## 一、文档信息",
        "",
        f"- 会议日期：{meeting_date}",
        f"- 会议类型：{meeting_type}",
        f"- 脱敏等级：{ANONYMIZATION_LEVEL}",
        "- 处理说明：仅删除有限规则明确识别到的发言人身份值，并对发言风格执行规则化处理；"
        "以保留业务事实为目标，未执行外部事实核验，交付前必须人工复核",
        "",
        "## 二、主题纪要",
        "",
    ]
    for unit in units:
        lines.extend([f"【{unit.full_topic}】", "", unit.text, ""])

    if pending_items:
        lines.extend(["## 三、存疑与待确认", ""])
        lines.extend(f"- {item}" for item in pending_items)
    return "\n".join(lines).rstrip() + "\n"


def write_markdown(text: str, path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def validate_written_markdown(
    path: Path,
    expected_text: str,
    units: list[TopicUnit],
    pending_items: list[str],
) -> None:
    try:
        raw = path.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            raise SystemExit("Generated Markdown unexpectedly contains a UTF-8 BOM.")
        actual = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SystemExit("Generated Markdown is not valid UTF-8.") from exc
    except OSError as exc:
        raise SystemExit(f"Generated Markdown validation failed: {exc}") from exc

    if "\r" in actual:
        raise SystemExit("Generated Markdown contains non-canonical line endings.")
    if not actual.endswith("\n"):
        raise SystemExit("Generated Markdown does not end with a newline.")
    if actual != expected_text:
        raise SystemExit("Generated Markdown content differs from the validated in-memory result.")

    section_headings = [
        "# 脱敏会议纪要",
        "## 一、文档信息",
        "## 二、主题纪要",
    ]
    if any(actual.count(heading) != 1 for heading in section_headings):
        raise SystemExit("Generated Markdown has an invalid fixed-section structure.")
    pending_heading = "## 三、存疑与待确认"
    if "主题：" in actual or "待确认业务事项" in actual:
        raise SystemExit("Generated Markdown contains a forbidden internal label.")
    if actual.count(pending_heading) != (1 if pending_items else 0):
        raise SystemExit("Generated Markdown has an invalid pending-section structure.")
    if pending_items:
        section_headings.append(pending_heading)
    positions = [actual.index(heading) for heading in section_headings]
    if positions != sorted(positions):
        raise SystemExit("Generated Markdown sections are out of order.")

    actual_topic_markers = [
        line for line in actual.splitlines() if re.fullmatch(r"【[^】\n]+】", line)
    ]
    expected_topic_markers = [f"【{unit.full_topic}】" for unit in units]
    if actual_topic_markers != expected_topic_markers:
        raise SystemExit("Generated Markdown topic markers differ from sanitized topic units.")

    if pending_items:
        pending_section = actual.split(pending_heading + "\n\n", maxsplit=1)[1]
        expected_pending = "\n".join(f"- {item}" for item in pending_items) + "\n"
        if pending_section != expected_pending:
            raise SystemExit("Generated Markdown pending items differ from sanitized pending items.")


def create_temp_path(output_dir: Path) -> Path:
    handle = tempfile.NamedTemporaryFile(
        prefix=".sanitizer-",
        suffix=".md",
        dir=output_dir,
        delete=False,
    )
    handle.close()
    return Path(handle.name)


def cleanup_paths(paths: Iterable[Path]) -> list[str]:
    errors: list[str] = []
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            errors.append(f"{path.name}: {exc}")
    return errors


def publish_markdown(temp_path: Path, final_path: Path, force: bool) -> None:
    if force:
        try:
            temp_path.replace(final_path)
        except OSError as exc:
            cleanup_errors = cleanup_paths([temp_path])
            detail = f" Cleanup failed: {'; '.join(cleanup_errors)}." if cleanup_errors else ""
            raise SystemExit(f"Failed to publish sanitized Markdown: {exc}.{detail}") from exc
        return

    try:
        os.link(temp_path, final_path)
    except FileExistsError as exc:
        cleanup_errors = cleanup_paths([temp_path])
        detail = f" Cleanup failed: {'; '.join(cleanup_errors)}." if cleanup_errors else ""
        raise SystemExit(
            f"Refusing to overwrite existing output: {final_path.name}. "
            f"Use --force only after review.{detail}"
        ) from exc
    except OSError as exc:
        cleanup_errors = cleanup_paths([temp_path])
        detail = f" Cleanup failed: {'; '.join(cleanup_errors)}." if cleanup_errors else ""
        raise SystemExit(f"Failed to publish sanitized Markdown: {exc}.{detail}") from exc

    cleanup_errors = cleanup_paths([temp_path])
    if cleanup_errors:
        raise SystemExit(
            "Sanitized Markdown was published successfully, but temporary-file cleanup failed: "
            + "; ".join(cleanup_errors)
        )


def write_markdown_output(
    output_dir: Path,
    output_stem: str,
    force: bool,
    input_path: Path,
    units: list[TopicUnit],
    pending_items: list[str],
    meeting_date: str,
    meeting_type: str,
) -> Path:
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SystemExit(f"Failed to create output directory: {exc}") from exc

    final_path = output_dir / f"{output_stem}_sanitized.md"
    if final_path.resolve() == input_path.resolve():
        raise SystemExit("Refusing to overwrite the input source file, even with --force.")
    if final_path.exists() and not force:
        raise SystemExit(
            f"Refusing to overwrite existing output: {final_path.name}. "
            "Use --force only after review."
        )

    rendered = render_markdown(units, pending_items, meeting_date, meeting_type)
    temp_path: Path | None = None
    try:
        temp_path = create_temp_path(output_dir)
        write_markdown(rendered, temp_path)
        validate_written_markdown(temp_path, rendered, units, pending_items)
        publish_path = temp_path
        temp_path = None
        publish_markdown(publish_path, final_path, force=force)
    except OSError as exc:
        cleanup_errors = cleanup_paths([temp_path]) if temp_path is not None else []
        detail = f" Cleanup failed: {'; '.join(cleanup_errors)}." if cleanup_errors else ""
        raise SystemExit(f"Failed to write sanitized Markdown: {exc}.{detail}") from exc
    except BaseException as exc:
        cleanup_errors = cleanup_paths([temp_path]) if temp_path is not None else []
        if cleanup_errors:
            raise SystemExit(
                f"{exc}\nTemporary-file cleanup failed: {'; '.join(cleanup_errors)}"
            ) from exc
        raise
    return final_path


def dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sanitize Chinese investment meeting minutes into one Markdown file requiring human review."
    )
    parser.add_argument("input_file", help="Input .md or .txt meeting-minutes file")
    parser.add_argument("--output-dir", default="outputs", help="Output directory, default: outputs")
    parser.add_argument("--meeting-date", help="Override meeting date, format YYYY-MM-DD")
    parser.add_argument("--output-stem", help="Reviewed identity-free output filename stem; default: safe date/hash stem")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Atomically replace an existing Markdown output after temporary-file validation",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_path = Path(args.input_file).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()

    raw_text = read_input(input_path)
    speaker_names = collect_speaker_names(raw_text)
    validate_source_and_heading_modes(raw_text, speaker_names)
    meeting_date = parse_meeting_date(raw_text, args.meeting_date)
    meeting_type = parse_meeting_type(raw_text, speaker_names)

    topic_raw, pending_raw = split_pending_section(raw_text)
    topic_text = strip_speaker_headings_and_identity(topic_raw, speaker_names)
    units = split_topic_units(topic_text, speaker_names)
    pending_items = [neutralize_text(item, speaker_names) for item in pending_raw]
    pending_items = [item for item in pending_items if item]

    if not units:
        raise SystemExit("No usable topic content found after sanitization.")

    output_stem = build_output_stem(raw_text, meeting_date, args.output_stem)
    quality_check(units, pending_items, speaker_names, meeting_type, output_stem)
    written_path = write_markdown_output(
        output_dir=output_dir,
        output_stem=output_stem,
        force=args.force,
        input_path=input_path,
        units=units,
        pending_items=pending_items,
        meeting_date=meeting_date,
        meeting_type=meeting_type,
    )

    print(f"Wrote {written_path}")
    print(f"Topic chunks: {len(units)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
