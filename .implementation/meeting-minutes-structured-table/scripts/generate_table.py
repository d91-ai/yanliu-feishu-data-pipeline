#!/usr/bin/env python3
"""Generate a fixed-schema structured table from confirmed meeting minutes."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import io
import json
import re
from pathlib import Path
from typing import Any


FIELDS = [
    "meeting_date",
    "row_index",
    "target_name",
    "sector_name",
    "stock_code",
    "core_viewpoint",
    "presenter",
    "comment_time",
    "investment_judgment",
]

DISPLAY_FIELDS = [
    ("target_name", "标的名称"),
    ("meeting_date", "会议日期"),
    ("row_index", "标的汇总表中的行号"),
    ("sector_name", "板块"),
    ("stock_code", "股票代码"),
    ("core_viewpoint", "核心观点"),
    ("presenter", "发言人"),
    ("comment_time", "评论时间"),
    ("investment_judgment", "投资判断"),
]

FRONTMATTER_FIELDS = [
    "source_record_id",
    "source_archive_url",
    "source_file_name",
    "meeting_date",
    "generated_at",
    "generator",
    "row_count",
    "schema_version",
]

TARGET_STOPWORDS = {
    "会议日期",
    "整理时间",
    "会议标题",
    "会议类型",
    "会议系列",
    "存疑",
    "待确认",
    "表述存疑",
    "疑似",
    "行业术语",
    "公司名",
    "机构名",
    "产品名",
    "鹏华",
    "彭华",
    "景顺",
    "秦顺",
    "MPV",
    "CPU",
    "AI",
    "LED",
    "NV",
    "GPU",
    "KEG",
    "PCB",
}

KNOWN_TARGETS = [
    "甲辰科技",
    "云岭光电",
    "星桥智能",
    "海岳材料",
    "远景设备",
    "青云通信",
]

SECTOR_KEYWORDS = [
    ("PCB", "PCB"),
    ("覆铜板", "PCB"),
    ("复铜板", "PCB"),
    ("铜箔", "PCB"),
    ("球硅", "PCB材料"),
    ("光芯片", "光通信"),
    ("CPO", "光通信"),
    ("CPU", "AI算力"),
    ("AI", "AI"),
    ("端侧", "端侧AI"),
    ("眼镜", "AR/AI眼镜"),
    ("AR", "AR/AI眼镜"),
    ("MicroLED", "显示/短距通信"),
    ("液冷", "液冷"),
    ("卫星", "卫星通信"),
    ("具身智能", "机器人/具身智能"),
    ("半导体设备", "半导体设备"),
    ("创新药", "医药"),
    ("医药", "医药"),
    ("锂矿", "锂矿"),
    ("化工", "化工"),
    ("航运", "航运"),
    ("南油", "航运"),
]

TARGET_SPLIT_RE = re.compile(r"\s*(?:/|、|，|,|；|;|｜|\||和|及|与)\s*")


def read_text(path: str | None) -> str:
    if not path:
        return ""
    return Path(path).read_text(encoding="utf-8")


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
        return "待确认"
    year, month, day = match.groups()
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"


def clean_cell(value: str) -> str:
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    return value.replace("|", "｜")


def normalize_target_name(value: str) -> str:
    text = clean_cell(value)
    text = re.sub(r"^\*\*|\*\*$", "", text).strip()
    text = re.sub(r"(表述存疑|疑似公司名|疑似机构名|疑似产品名|行业术语|转写错误|ASR.*)$", "", text).strip(" /｜、，。；;:")
    embedded = [item for item in KNOWN_TARGETS if item in text]
    if embedded and "/" not in text:
        return max(embedded, key=len)
    if "/" in text:
        parts = [part.strip() for part in text.split("/") if part.strip()]
        concrete = [part for part in parts if not re.search(r"表述|存疑|其他|相关|环节|内容", part)]
        concrete = [max([known for known in KNOWN_TARGETS if known in part] or [part], key=len) for part in concrete]
        text = "/".join(concrete or parts)
    return text.strip()


def clean_stock_name_candidate(value: str) -> str:
    text = normalize_target_name(value)
    text = re.sub(r"^(我觉得|我认为|我感觉|对我来说|看好|关注|强推|买入|加仓|增持|提到|提及|提的是|标的是|公司是|股票是)", "", text).strip()
    text = re.sub(r"^.*?[：:，,。；;\s]", "", text).strip()
    return normalize_target_name(text)


def stock_name_code_pairs(markdown: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    code_pattern = r"(?:\d{6}(?:\.[A-Z]{2})?|[A-Z]{2,8}(?:\.[A-Z]{1,4})?)"
    patterns = [
        rf"([\u4e00-\u9fffA-Za-z0-9][\u4e00-\u9fffA-Za-z0-9·]{{1,20}})\s*[（(]\[({code_pattern})\]\([^)]*\)[）)]",
        rf"([\u4e00-\u9fffA-Za-z0-9][\u4e00-\u9fffA-Za-z0-9·]{{1,20}})\s*[（(]({code_pattern})[）)]",
        rf"([\u4e00-\u9fffA-Za-z0-9][\u4e00-\u9fffA-Za-z0-9·]{{1,20}})\s*【({code_pattern})】",
        r"([\u4e00-\u9fffA-Za-z0-9][\u4e00-\u9fffA-Za-z0-9·]{1,20})\s+(\d{6}(?:\.[A-Z]{2})?)",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, markdown):
            name = clean_stock_name_candidate(match.group(1))
            code = match.group(2).strip()
            if is_target_like(name) and (code not in pairs or len(name) > len(pairs[code])):
                pairs[code] = name
    return pairs


def normalize_stock_code(value: str) -> str:
    match = re.search(r"\d{6}(?:\.[A-Z]{2})?", str(value or "").upper())
    return match.group(0) if match else ""


def stock_sector_hints(markdown: str) -> dict[str, str]:
    hints: dict[str, str] = {}
    pattern = re.compile(
        r"[-*]\s*([\u4e00-\u9fffA-Za-z0-9·]{2,24})\s*[（(]"
        r"(\d{6}(?:\.[A-Z]{2})?)"
        r"[）)](?=[^\n]*?(?:板块|行业)[:：])([^\n]*)"
    )
    for match in pattern.finditer(markdown):
        name = clean_stock_name_candidate(match.group(1))
        code = normalize_stock_code(match.group(2))
        tail = match.group(3)
        sector_match = re.search(r"(?:板块|行业)[:：]\s*([^；;，,\n]+)", tail)
        sector = clean_cell(sector_match.group(1)) if sector_match else ""
        if not sector or sector in {"-", "待确认"}:
            continue
        if code:
            hints[code] = sector
        if name:
            hints[name] = sector
    return hints


def resolved_sector_for_target(
    *,
    text_sector: str,
    canonical_target: str,
    confirmed_code: str,
    sector_hints: dict[str, str],
) -> str:
    cleaned_text_sector = clean_cell(text_sector)
    if cleaned_text_sector and cleaned_text_sector != "待确认":
        return cleaned_text_sector
    code = normalize_stock_code(confirmed_code)
    if code and sector_hints.get(code):
        return sector_hints[code]
    if canonical_target and sector_hints.get(canonical_target):
        return sector_hints[canonical_target]
    return cleaned_text_sector or "待确认"


def canonical_target_name(target: str, code: str, stock_names_by_code: dict[str, str]) -> str:
    normalized = normalize_target_name(target)
    if code and stock_names_by_code.get(code):
        return stock_names_by_code[code]
    return normalized


def confirmed_code_for_target(target: str, stock_names_by_code: dict[str, str]) -> str:
    normalized = normalize_target_name(target)
    if not normalized:
        return ""
    for code, name in stock_names_by_code.items():
        canonical = normalize_target_name(name)
        if normalized == canonical or normalized in canonical or canonical in normalized:
            return code
    return ""


def confirmed_target_fields(target: str, code: str, stock_names_by_code: dict[str, str]) -> tuple[str, str]:
    confirmed_code = clean_cell(code) or confirmed_code_for_target(target, stock_names_by_code)
    if not confirmed_code:
        return "", ""
    return canonical_target_name(target, confirmed_code, stock_names_by_code), confirmed_code


def split_combined_targets(target: str, code: str = "") -> list[tuple[str, str]]:
    target = clean_cell(target)
    if not target:
        return []
    target_for_split = re.sub(r"\[([A-Za-z0-9.]{3,32})\]\([^)]*\)", r"\1", target)
    parts = [part.strip() for part in TARGET_SPLIT_RE.split(target_for_split) if part.strip()]
    if len(parts) <= 1:
        return [(normalize_target_name(target), code)]
    explicit_codes = [part.strip() for part in re.split(r"\s*(?:/|、|，|,|；|;|｜|\|)\s*", code) if part.strip()]
    split_parts: list[tuple[str, str]] = []
    for part in parts:
        clean_part, part_code = strip_code(part)
        clean_part = normalize_target_name(clean_part)
        if clean_part:
            split_parts.append((clean_part, part_code))
    if len(split_parts) <= 1:
        return [(normalize_target_name(target), code)]
    if not any(part_code for _, part_code in split_parts) and not all(is_target_like(part) for part, _ in split_parts):
        return [(normalize_target_name(target), code)]
    if len(explicit_codes) == len(split_parts):
        return [(part, explicit_codes[index] or part_code) for index, (part, part_code) in enumerate(split_parts)]
    if len(explicit_codes) == 1 and split_parts and not split_parts[0][1]:
        first_part, _ = split_parts[0]
        split_parts[0] = (first_part, explicit_codes[0])
    return split_parts


def is_target_like(value: str) -> bool:
    text = normalize_target_name(value)
    if not text or text in TARGET_STOPWORDS:
        return False
    if len(text) > 28:
        return False
    if any(word in text for word in TARGET_STOPWORDS):
        return False
    if re.search(r"(另一处|当前判断|候选|给出|未给|需确认|同一标的|代码缺失|识别仍需核验)", text):
        return False
    if re.search(r"(那个|受益于|预期|事情|内容|题材|风格|产业|环节|连接件|cage|case)", text, flags=re.I) and not any(known in text for known in KNOWN_TARGETS):
        return False
    if text in KNOWN_TARGETS:
        return True
    if re.fullmatch(r"[A-Za-z]{1,2}", text):
        return False
    if re.search(r"(科技|股份|新材|材料|精工|电气|数控|智能|光电|通信|芯片|设备)", text):
        return True
    if re.fullmatch(r"[A-Z]{2,8}", text) and text not in TARGET_STOPWORDS:
        return True
    if 2 <= len(re.findall(r"[\u4e00-\u9fff]", text)) <= 8 and not re.search(r"(方向|赛道|逻辑|订单|产能|市值|赔率|机会|客户|产品|产业|板块|公司|股东|会议|目标)", text):
        return True
    return False


def infer_sector(text: str, fallback: str = "待确认") -> str:
    for keyword, sector in SECTOR_KEYWORDS:
        if keyword in text:
            return sector
    return fallback or "待确认"


def sentence_for_target(text: str, target: str) -> str:
    cleaned = clean_cell(text)
    pieces = re.split(r"(?<=[。！？!?；;])\s*", cleaned)
    normalized = normalize_target_name(target)
    target_tokens = [normalized]
    if "/" in normalized:
        target_tokens.extend(part.strip() for part in normalized.split("/") if part.strip())
    for piece in pieces:
        if any(token and token in piece for token in target_tokens):
            return piece[:260]
    return cleaned[:260]


def compact_reason(text: str, target: str) -> str:
    reason = sentence_for_target(text, target)
    reason = re.sub(r"^就?除了[^，。；;]*以外[，,]\s*", "", reason)
    reason = re.sub(r"^(就是|那个|其实|反正|今天|后面|个股方面[，,]?)", "", reason).strip()
    reason = re.sub(r"^(我觉得|我认为|我感觉|对我来说)[，,]?", "", reason).strip()
    reason = re.sub(r"^(第[一二三四五六七八九十]+个(是|就是)?)[，,]?", "", reason).strip()
    reason = re.sub(r"^(看好|最看好|强推)\s*", "", reason).strip()
    normalized = normalize_target_name(target)
    candidates = [normalized] + [part.strip() for part in normalized.split("/") if part.strip()]
    for candidate in sorted({item for item in candidates if item}, key=len, reverse=True):
        reason = re.sub(rf"^\**{re.escape(candidate)}\**(的话)?(是)?[，,：: ]*", "", reason).strip()
    reason = re.sub(r"^[/、，,；;和及与 ]+", "", reason)
    reason = re.sub(r"(我觉得|我感觉|我认为|对我来说|其实|反正|就是|那个|这个票|大家|我刚才看群里|老是)", "", reason)
    reason = reason.replace("如果说", "如果").replace("的话", "")
    reason = re.sub(r"\s+", " ", reason).strip()
    reason = re.sub(r"^[，,、 ]+", "", reason)
    reason = re.sub(r"[。！？!?；;]+", "，", reason).strip(" ，,")
    if len(reason) > 120:
        reason = reason[:120].rstrip(" ，,") + "..."
    return reason or "原文未给出更细理由"


def summarize_viewpoint(target: str, text: str) -> str:
    reason = compact_reason(text, target)
    judgment = judgment_from_text(text)
    if judgment == "积极":
        if reason.startswith(("看好", "关注")):
            return f"看好{target}，原文同时提及{reason[2:].strip() or '相关标的'}。"
        return f"看好{target}，核心理由是{reason}。"
    if judgment == "消极":
        return f"对{target}持消极判断，核心理由是{reason}。"
    return f"对{target}保持中性观察，核心信息是{reason}。"


def bold_targets(text: str) -> list[str]:
    targets: list[str] = []
    for match in re.finditer(r"\*\*([^*\n]{1,40})\*\*", text):
        candidate = normalize_target_name(match.group(1))
        if is_target_like(candidate) and candidate not in targets:
            targets.append(candidate)
    return targets


def inline_targets(text: str) -> list[str]:
    candidates: list[str] = []
    for known in KNOWN_TARGETS:
        if known in text and known not in candidates:
            candidates.append(known)
    patterns = [
        r"([A-Z]{2,8})",
        r"([\u4e00-\u9fff]{2,12}(?:科技|股份|新材|材料|精工|南油|华创|电气|数控|维格))",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            candidate = normalize_target_name(match.group(1))
            if is_target_like(candidate) and candidate not in candidates:
                candidates.append(candidate)
    return candidates


def section_paragraphs(markdown: str) -> list[tuple[str, str]]:
    body = markdown
    match = re.search(r"##\s*一、逐发言人原文整理\s*(.*?)(?=\n##\s*二、存疑与待确认|\Z)", markdown, flags=re.S)
    if match:
        body = match.group(1)
    speaker = "待确认"
    paragraphs: list[tuple[str, str]] = []
    buffer: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        heading = re.match(r"^#{3,4}\s*(.+?)\s*$", stripped)
        if heading:
            if buffer:
                paragraphs.append((speaker, "\n".join(buffer).strip()))
                buffer = []
            speaker = heading.group(1).strip() or speaker
            continue
        if not stripped:
            if buffer:
                paragraphs.append((speaker, "\n".join(buffer).strip()))
                buffer = []
            continue
        if stripped.startswith("|"):
            continue
        buffer.append(stripped)
    if buffer:
        paragraphs.append((speaker, "\n".join(buffer).strip()))
    return [(spk, text) for spk, text in paragraphs if len(clean_cell(text)) >= 12]


def strip_code(value: str) -> tuple[str, str]:
    text = str(value or "").strip()
    code = ""
    markdown_link_match = re.search(r"[\(（]\[([A-Za-z0-9.]{3,32})\]\([^)]*\)[\)）]", text)
    if markdown_link_match:
        code = markdown_link_match.group(1).strip()
        text = (text[: markdown_link_match.start()] + text[markdown_link_match.end() :]).strip()
    code_match = re.search(r"[\(（]([A-Za-z0-9.]{3,32})[\)）]", text)
    if code_match and not code:
        code = code_match.group(1).strip()
        text = (text[: code_match.start()] + text[code_match.end() :]).strip()
    bracket_match = re.search(r"【([A-Za-z0-9.]{3,32})】", text)
    if bracket_match and not code:
        code = bracket_match.group(1).strip()
        text = text.replace(bracket_match.group(0), "").strip()
    return text.strip(" -｜|"), code


def parse_block_heading(line: str) -> tuple[str, str, str] | None:
    match = re.match(r"^\s*【([^】｜|]+)[｜|](.+)】", line)
    if not match:
        return None
    sector = match.group(1).strip() or "待确认"
    target_raw = match.group(2).strip()
    target, code = strip_code(target_raw)
    if not target or target == "-":
        target = sector or "待确认"
    return sector, target, code


def block_heading_remainder(line: str) -> str:
    match = re.match(r"^\s*【[^】]+】\s*(.*)$", line)
    if not match:
        return ""
    return clean_cell(match.group(1))


def speaker_from_heading(line: str, current: str) -> str:
    match = re.match(r"^\s*#{2,4}\s*(.+?)\s*$", line)
    if not match:
        return current
    text = match.group(1).strip()
    if text.startswith("【"):
        return current
    if text.startswith(("一、", "二、", "三、")):
        return current
    if "发言人" in text or len(text) <= 20:
        return text
    return current


def paragraph_text(lines: list[str], start: int) -> str:
    collected: list[str] = []
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#") or stripped.startswith("【") or parse_block_heading(stripped):
            break
        if re.match(r"^\|.+\|$", stripped):
            break
        collected.append(stripped)
    return clean_cell(" ".join(collected))


def judgment_from_text(text: str) -> str:
    if re.search(r"看空|偏空|减仓|卖出|下行|不建议|回避|不及预期|不如预期|风险|不确定|谨慎", text):
        return "消极"
    if re.search(r"看多|偏多|积极|买入|加仓|增持|机会|受益|强推|看好|最看好|赔率|逻辑.*好|订单.*好|比较想买|还可以|没走完|突破", text):
        return "积极"
    return "中性"


def extract_rows(meeting_markdown: str, source_markdown: str = "") -> list[dict[str, Any]]:
    meeting_date = normalize_date(markdown_field(meeting_markdown, "会议日期"))
    combined_markdown = "\n".join([meeting_markdown, source_markdown])
    stock_names_by_code = stock_name_code_pairs(combined_markdown)
    sector_hints = stock_sector_hints(combined_markdown)
    lines = meeting_markdown.splitlines()
    rows: list[dict[str, Any]] = []
    current_speaker = "待确认"
    seen_keys: set[tuple[str, str, str, str]] = set()
    for index, line in enumerate(lines):
        current_speaker = speaker_from_heading(line, current_speaker)
        block_line = re.sub(r"^\s*#{2,6}\s*", "", line.strip())
        parsed = parse_block_heading(block_line)
        if not parsed:
            continue
        sector, target, code = parsed
        viewpoint = paragraph_text(lines, index)
        if not viewpoint:
            viewpoint = block_heading_remainder(block_line)
        if not viewpoint:
            continue
        for target_part, code_part in split_combined_targets(target, code):
            canonical_target, confirmed_code = confirmed_target_fields(target_part, code_part, stock_names_by_code)
            if not canonical_target or not confirmed_code:
                continue
            summary_target = canonical_target or normalize_target_name(target_part) or sector
            summary = summarize_viewpoint(summary_target, viewpoint)
            resolved_sector = resolved_sector_for_target(
                text_sector=sector,
                canonical_target=canonical_target,
                confirmed_code=confirmed_code,
                sector_hints=sector_hints,
            )
            key = (canonical_target, resolved_sector, confirmed_code, summary[:80])
            if key in seen_keys:
                continue
            seen_keys.add(key)
            rows.append(
                {
                    "meeting_date": meeting_date,
                    "row_index": len(rows) + 1,
                    "target_name": clean_cell(canonical_target),
                    "sector_name": resolved_sector,
                    "stock_code": clean_cell(confirmed_code),
                    "core_viewpoint": summary,
                    "presenter": clean_cell(current_speaker) or "待确认",
                    "comment_time": meeting_date,
                    "investment_judgment": judgment_from_text(viewpoint),
                }
            )

    if rows:
        return rows

    for speaker, paragraph in section_paragraphs(meeting_markdown):
        bold = bold_targets(paragraph)
        inline = []
        for item in inline_targets(paragraph):
            if item in bold:
                continue
            if any(item in existing for existing in bold):
                continue
            inline.append(item)
        targets = bold + inline
        if not targets and re.search(r"(锂矿|化工|医药|创新药|半导体设备|端侧AI|端侧 AI|AI眼镜|液冷|铜箔|PCB|覆铜板|球硅|光芯片)", paragraph):
            targets = [infer_sector(paragraph)]
        for target in targets[:8]:
            sector = infer_sector(paragraph)
            if target == sector:
                sector = target
            canonical_target, confirmed_code = confirmed_target_fields(target, "", stock_names_by_code)
            if not canonical_target or not confirmed_code:
                continue
            viewpoint = sentence_for_target(paragraph, target)
            summary = summarize_viewpoint(target, viewpoint)
            resolved_sector = resolved_sector_for_target(
                text_sector=sector,
                canonical_target=canonical_target,
                confirmed_code=confirmed_code,
                sector_hints=sector_hints,
            )
            key = (canonical_target, resolved_sector, confirmed_code, summary[:80])
            if key in seen_keys:
                continue
            seen_keys.add(key)
            rows.append(
                {
                    "meeting_date": meeting_date,
                    "row_index": len(rows) + 1,
                    "target_name": clean_cell(canonical_target),
                    "sector_name": resolved_sector,
                    "stock_code": clean_cell(confirmed_code),
                    "core_viewpoint": summary,
                    "presenter": clean_cell(speaker) or "待确认",
                    "comment_time": meeting_date,
                    "investment_judgment": judgment_from_text(viewpoint),
                }
            )

    if rows:
        return rows

    return []


def first_meaningful_paragraph(markdown: str) -> str:
    for paragraph in re.split(r"\n\s*\n", markdown):
        text = clean_cell(re.sub(r"^#+\s*", "", paragraph.strip()))
        if len(text) >= 12 and not text.startswith("|"):
            return text
    return ""


def markdown_table(rows: list[dict[str, Any]]) -> str:
    output = io.StringIO()
    output.write("# 结构化表格\n\n")
    output.write("| " + " | ".join(label for _, label in DISPLAY_FIELDS) + " |\n")
    output.write("| " + " | ".join("---" for _ in DISPLAY_FIELDS) + " |\n")
    for row in rows:
        output.write("| " + " | ".join(clean_cell(str(row.get(field, ""))) for field, _ in DISPLAY_FIELDS) + " |\n")
    return output.getvalue()


def yaml_scalar(value: Any) -> str:
    if value is None:
        return '""'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def frontmatter_text(metadata: dict[str, Any]) -> str:
    output = io.StringIO()
    output.write("---\n")
    for field in FRONTMATTER_FIELDS:
        output.write(f"{field}: {yaml_scalar(metadata.get(field, ''))}\n")
    output.write("---\n\n")
    return output.getvalue()


def markdown_document(rows: list[dict[str, Any]], metadata: dict[str, Any] | None = None) -> str:
    body = markdown_table(rows)
    if not metadata:
        return body
    return frontmatter_text(metadata) + body


def build_frontmatter_metadata(
    *,
    rows: list[dict[str, Any]],
    meeting_markdown: str,
    source_record_id: str,
    source_archive_url: str,
    source_file_name: str,
    meeting_date_override: str,
    generated_at: str,
    schema_version: int,
) -> dict[str, Any] | None:
    if not any([source_record_id, source_archive_url, source_file_name, generated_at]):
        return None
    if not generated_at:
        generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    meeting_date = meeting_date_override or (rows[0].get("meeting_date") if rows else normalize_date(markdown_field(meeting_markdown, "会议日期")))
    return {
        "source_record_id": source_record_id,
        "source_archive_url": source_archive_url,
        "source_file_name": source_file_name,
        "meeting_date": meeting_date,
        "generated_at": generated_at,
        "generator": "meeting-minutes-structured-table",
        "row_count": len(rows),
        "schema_version": schema_version,
    }


def csv_text(rows: list[dict[str, Any]]) -> str:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=[label for _, label in DISPLAY_FIELDS])
    writer.writeheader()
    for row in rows:
        writer.writerow({label: row.get(field, "") for field, label in DISPLAY_FIELDS})
    return output.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate structured table rows from confirmed meeting minutes.")
    parser.add_argument("--meeting-markdown", required=True, help="Second-confirmed meeting note Markdown")
    parser.add_argument("--source-markdown", help="First-confirmed source text Markdown")
    parser.add_argument("--output", help="Write Markdown table to this path")
    parser.add_argument("--json-output", help="Write JSON rows to this path")
    parser.add_argument("--csv-output", help="Write CSV rows to this path")
    parser.add_argument("--format", choices=["markdown", "json", "csv"], default="markdown")
    parser.add_argument("--source-record-id", default="", help="Optional source Bitable record ID for Markdown frontmatter")
    parser.add_argument("--source-archive-url", default="", help="Optional source archive URL for Markdown frontmatter")
    parser.add_argument("--source-file-name", default="", help="Optional source file name for Markdown frontmatter")
    parser.add_argument("--meeting-date", default="", help="Optional confirmed meeting date override in YYYY-MM-DD")
    parser.add_argument("--generated-at", default="", help="Optional ISO timestamp for Markdown frontmatter")
    parser.add_argument("--schema-version", type=int, default=1, help="Structured table schema version for Markdown frontmatter")
    parser.add_argument(
        "--target-lexicon",
        help="Optional deployer-owned UTF-8 CSV (target_name/aliases) or one-target-per-line file",
    )
    args = parser.parse_args()

    if args.target_lexicon:
        global KNOWN_TARGETS
        lexicon_path = Path(args.target_lexicon)
        raw = lexicon_path.read_text(encoding="utf-8-sig")
        if lexicon_path.suffix.lower() == ".csv":
            entries: list[str] = []
            for row in csv.DictReader(io.StringIO(raw)):
                entries.extend(
                    item.strip()
                    for item in [row.get("target_name", ""), *row.get("aliases", "").split("|")]
                    if item.strip()
                )
            KNOWN_TARGETS = list(dict.fromkeys(entries))
        else:
            KNOWN_TARGETS = list(dict.fromkeys(line.strip() for line in raw.splitlines() if line.strip()))

    meeting_markdown = read_text(args.meeting_markdown)
    rows = extract_rows(meeting_markdown, read_text(args.source_markdown))
    meeting_date_override = normalize_date(args.meeting_date)
    if args.meeting_date and not meeting_date_override:
        raise SystemExit("--meeting-date must contain a valid YYYY-MM-DD date")
    if meeting_date_override:
        for row in rows:
            old_meeting_date = row.get("meeting_date")
            row["meeting_date"] = meeting_date_override
            if not row.get("comment_time") or row.get("comment_time") == old_meeting_date:
                row["comment_time"] = meeting_date_override
    metadata = build_frontmatter_metadata(
        rows=rows,
        meeting_markdown=meeting_markdown,
        source_record_id=args.source_record_id,
        source_archive_url=args.source_archive_url,
        source_file_name=args.source_file_name,
        meeting_date_override=meeting_date_override,
        generated_at=args.generated_at,
        schema_version=args.schema_version,
    )
    if args.output:
        Path(args.output).write_text(markdown_document(rows, metadata), encoding="utf-8")
    if args.json_output:
        Path(args.json_output).write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.csv_output:
        Path(args.csv_output).write_text(csv_text(rows), encoding="utf-8")

    if args.format == "json":
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    elif args.format == "csv":
        print(csv_text(rows), end="")
    else:
        print(markdown_document(rows, metadata), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
