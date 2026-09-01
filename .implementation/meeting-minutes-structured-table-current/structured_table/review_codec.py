"""Human-editable structured-viewpoint Markdown codec."""

from __future__ import annotations

import io
import html
import re
import sys
from typing import Any

from .common import clean_cell, clean_evidence_text
from .contract import SUMMARY_FIELDS, SUMMARY_LABELS


CANONICAL_LABEL_BY_FIELD = {field: label for field, label in SUMMARY_FIELDS}
BUSINESS_FIELDS = {
    "viewpoint_date",
    "target_name",
    "stock_code",
    "market",
    "presenter",
    "presenter_normalized",
    "direction",
    "time_horizon",
    "position_context",
}


def markdown_cell(value: Any) -> str:
    return clean_cell(value).replace("|", "&#124;")


def split_markdown_row(line: str) -> list[str]:
    text = line.strip().removeprefix("|").removesuffix("|")
    return [clean_cell(html.unescape(cell)) for cell in text.split("|")]


def is_separator(cells: list[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)


def claim_markdown_cards(rows: list[dict[str, Any]]) -> str:
    output = io.StringIO()
    output.write("# 标的观点结构化表\n\n")
    output.write("> 观点周期：短期=一个月以内；中期=超过一个月至十二个月；长期=超过十二个月；无明确时间依据=未说明。\n\n")
    for index, row in enumerate(rows, start=1):
        output.write(f"## 观点 {index}\n\n")
        output.write("| " + " | ".join(label for _, label in SUMMARY_FIELDS) + " |\n")
        output.write("| " + " | ".join("---" for _ in SUMMARY_FIELDS) + " |\n")
        output.write("| " + " | ".join(markdown_cell(row.get(field)) for field, _ in SUMMARY_FIELDS) + " |\n\n")
        output.write("### 原文限定条件\n\n")
        output.write("| 原文条件 | 条件类型 |\n| --- | --- |\n")
        conditions = row.get("conditions", [])
        if conditions:
            for condition in conditions:
                output.write(
                    f"| {markdown_cell(condition.get('text'))} | "
                    f"{'、'.join(markdown_cell(value) for value in condition.get('types', []))} |\n"
                )
        else:
            output.write("| 无 | 无 |\n")
        evidence = row.get("source_evidence", [])
        for evidence_index, item in enumerate(evidence, start=1):
            output.write(f"\n### 原文依据 {evidence_index}\n\n")
            output.write(f"- 原文定位：{markdown_cell(item.get('locator'))}\n\n")
            text = clean_evidence_text(item.get("text"))
            for line in text.split("\n"):
                output.write(f"> {line}\n")
            output.write("\n")
    return output.getvalue()


def markdown_document(rows: list[dict[str, Any]], metadata: dict[str, Any]) -> str:
    meeting_date = markdown_cell(metadata.get("meeting_date"))
    date_line = f"- 会议日期：{meeting_date}\n\n" if meeting_date else ""
    cards = claim_markdown_cards(rows)
    heading, body = cards.split("\n", 1)
    return f"{heading}\n\n{date_line}{body.lstrip()}"


def parse_review_meeting_date(markdown: str) -> str:
    match = re.search(r"(?m)^-\s*会议日期[：:]\s*(.+?)\s*$", markdown)
    return clean_cell(match.group(1)) if match else ""


def parse_condition_table(lines: list[str], start: int, card_index: int) -> list[dict[str, Any]]:
    table_index = start
    while table_index < len(lines) and not lines[table_index].strip().startswith("|"):
        table_index += 1
    if table_index + 1 >= len(lines):
        raise SystemExit(f"structured markdown card {card_index}: missing condition table")
    headers = split_markdown_row(lines[table_index])
    if (
        "原文条件" not in headers
        or "条件类型" not in headers
        or not is_separator(split_markdown_row(lines[table_index + 1]))
    ):
        raise SystemExit(f"structured markdown card {card_index}: invalid condition table")
    text_index = headers.index("原文条件")
    types_index = headers.index("条件类型")
    conditions: list[dict[str, Any]] = []
    row_index = table_index + 2
    while row_index < len(lines) and lines[row_index].strip().startswith("|"):
        cells = split_markdown_row(lines[row_index])
        if len(cells) > max(text_index, types_index):
            text = cells[text_index]
            raw_types = cells[types_index]
            if text != "无" or raw_types != "无":
                types = [clean_cell(value) for value in raw_types.split("、") if clean_cell(value)]
                conditions.append({"text": text, "types": types})
        row_index += 1
    return conditions


def parse_source_evidence(block: str, card_index: int) -> list[dict[str, str]]:
    pattern = re.compile(
        r"(?ms)^### 原文依据(?:\s+\d+)?\s*$\n(.*?)(?=^### 原文依据(?:\s+\d+)?\s*$|\Z)"
    )
    evidence: list[dict[str, str]] = []
    for evidence_index, match in enumerate(pattern.finditer(block), start=1):
        section = match.group(1)
        locator_match = re.search(r"(?m)^-\s*原文定位[：:]\s*(.*?)\s*$", section)
        quote_lines = re.findall(r"(?m)^>\s?(.*)$", section)
        text = clean_evidence_text("\n".join(quote_lines))
        if not text:
            print(
                f"warning: structured markdown card {card_index} evidence "
                f"{evidence_index}: missing text; skipped",
                file=sys.stderr,
            )
            continue
        locator = clean_cell(locator_match.group(1)) if locator_match else ""
        evidence.append({"text": text, "locator": locator})
    return evidence


def parse_review_markdown_rows(markdown: str, label: str = "structured markdown") -> list[dict[str, Any]]:
    blocks = re.split(r"(?m)^## 观点(?:\s+\d+)?\s*$", markdown)[1:]
    if not blocks:
        print(f"warning: {label}: no viewpoint cards found", file=sys.stderr)
        return []
    rows: list[dict[str, Any]] = []
    for card_index, block in enumerate(blocks, start=1):
        lines = block.splitlines()
        table_index = next((i for i, line in enumerate(lines) if line.strip().startswith("|")), -1)
        if table_index < 0 or table_index + 2 >= len(lines):
            print(f"warning: {label} card {card_index}: missing summary table; skipped", file=sys.stderr)
            continue
        headers = split_markdown_row(lines[table_index])
        values = split_markdown_row(lines[table_index + 2])
        if not is_separator(split_markdown_row(lines[table_index + 1])) or len(headers) != len(values):
            print(f"warning: {label} card {card_index}: invalid summary table; skipped", file=sys.stderr)
            continue
        row: dict[str, Any] = {}
        selected_labels: dict[str, str] = {}
        for header, value in zip(headers, values):
            field = SUMMARY_LABELS.get(header)
            if not field:
                print(
                    f"warning: {label} card {card_index}: unknown summary header: {header}; ignored",
                    file=sys.stderr,
                )
                continue
            previous = selected_labels.get(field)
            if previous:
                canonical = CANONICAL_LABEL_BY_FIELD.get(field)
                if header == canonical and previous != canonical:
                    row[field] = value
                    selected_labels[field] = header
                print(
                    f"warning: {label} card {card_index}: duplicate summary field: {field}; "
                    f"using {selected_labels[field]}",
                    file=sys.stderr,
                )
                continue
            row[field] = value
            selected_labels[field] = header
        missing_defaultable = sorted((BUSINESS_FIELDS - {"target_name"}) - row.keys())
        if missing_defaultable:
            missing_labels = [CANONICAL_LABEL_BY_FIELD[field] for field in missing_defaultable]
            print(
                f"warning: {label} card {card_index}: missing summary fields: "
                f"{', '.join(missing_labels)}; conservative defaults will be used",
                file=sys.stderr,
            )
        row["source_evidence"] = parse_source_evidence(block, card_index)
        if not row["source_evidence"]:
            print(f"warning: {label} card {card_index}: missing source evidence; skipped", file=sys.stderr)
            continue
        condition_heading = next((i for i, line in enumerate(lines) if line.strip() == "### 原文限定条件"), -1)
        if condition_heading < 0:
            print(f"warning: {label} card {card_index}: missing condition section; using empty conditions", file=sys.stderr)
            row["conditions"] = []
        else:
            try:
                row["conditions"] = parse_condition_table(lines, condition_heading + 1, card_index)
            except SystemExit as exc:
                print(f"warning: {exc}; using empty conditions", file=sys.stderr)
                row["conditions"] = []
        required = {"target_name"}
        missing = sorted(required - row.keys())
        if missing:
            print(f"warning: {label} card {card_index}: missing fields: {', '.join(missing)}; skipped", file=sys.stderr)
            continue
        rows.append(row)
    return rows
