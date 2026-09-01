#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any


ROOT = Path("outputs/structured-regeneration")
MANIFEST = ROOT / "manifest.json"
SYMBOL_UNIVERSE = ROOT / "symbol_universe.csv"
OUT_DIR = ROOT / "new_structured"
ARTIFACT_DIR = ROOT / "artifacts"
EXPORTER: Path | None = None

GENERIC_TERMS = {
    "市场",
    "市场情绪",
    "市场位置",
    "市场反弹",
    "市场节奏",
    "策略",
    "配置方向",
    "短期判断",
    "短期共振调整",
    "国产算力",
    "国产替代",
    "半导体设备",
    "半导体",
    "AI",
    "AI云",
    "AI叙事",
    "AI应用",
    "机器人",
    "生猪",
    "宠物",
    "商业航天",
    "传统军工",
    "食品饮料",
    "科技",
    "科技成长",
    "存储",
    "存储链",
    "光模块",
    "光芯片",
    "PCB",
    "锂盐",
    "锂资源",
    "锂矿",
    "资产配置",
    "宏观流动性",
    "交易节奏",
    "科技持仓",
    "海外算力",
    "国产半导体和封装",
    "低估值业绩线",
    "中报业绩",
}


def clean(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text.replace("|", "｜")


def safe_file_stem(value: str) -> str:
    text = re.sub(r'[\\/:*?"<>|]+', " ", value).strip()
    return re.sub(r"\s+", " ", text) or "untitled"


def normalize_speaker(value: str) -> str:
    text = re.sub(r"^#+\s*", "", clean(value))
    text = text.strip(" -")
    return text or "待确认"


def market_from_code(code: str, fallback: str = "待确认") -> str:
    upper = code.upper()
    if upper.endswith((".SH", ".SZ", ".BJ")):
        return "A股"
    if upper.endswith(".HK"):
        return "港股"
    if upper.endswith(".US") or re.fullmatch(r"[A-Z]{1,8}", upper):
        return "美股"
    return fallback if fallback in {"A股", "港股", "美股", "其他", "待确认"} else "其他"


def split_codes(code: str) -> list[str]:
    parts = [item.strip() for item in re.split(r"[/,，;；\s]+", code) if item.strip()]
    return parts or []


def normalize_code(code: str) -> str:
    text = clean(code).upper()
    text = re.sub(r"^0+(\d{4}\.HK)$", r"\1", text)
    return text


def load_symbols(path: Path) -> tuple[dict[str, dict[str, str]], dict[str, list[dict[str, str]]]]:
    by_alias: dict[str, list[dict[str, str]]] = {}
    by_code: dict[str, dict[str, str]] = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            target = clean(row.get("target_name"))
            code = normalize_code(row.get("stock_code", ""))
            market = clean(row.get("market")) or market_from_code(code)
            if not target or not code:
                continue
            symbol = {"target_name": target, "stock_code": code, "market": market_from_code(code, market)}
            by_code[code] = symbol
            aliases = [target, code]
            aliases.extend(part.strip() for part in str(row.get("aliases") or "").split("|") if part.strip())
            for alias in dict.fromkeys(clean(item) for item in aliases if clean(item)):
                by_alias.setdefault(alias.casefold(), []).append(symbol)
    return by_code, by_alias


def lookup_alias(alias: str, by_alias: dict[str, list[dict[str, str]]]) -> dict[str, str] | None:
    key = clean(alias).casefold()
    if not key:
        return None
    rows = by_alias.get(key) or []
    if len(rows) == 1:
        return rows[0]
    return None


def normalized_symbol_for_code(code: str, by_alias: dict[str, list[dict[str, str]]]) -> dict[str, str] | None:
    direct = lookup_alias(code, by_alias)
    if direct and "/" not in direct["stock_code"]:
        return direct
    for row in by_alias.get(clean(code).casefold(), []) or []:
        if row["stock_code"] == code:
            return row
    return None


def strip_markdown_link_code(text: str) -> str:
    return re.sub(r"\[([A-Za-z0-9./]+)\]\([^)]*\)", r"\1", text)


def sector_from_bracket(content: str) -> str:
    first = clean(re.split(r"[|｜]", content, maxsplit=1)[0])
    if not first or re.search(r"\d{4,6}\.|[A-Z]{2,8}\.", first):
        return "待确认"
    return first


def parse_explicit_targets(content: str) -> list[tuple[str, str]]:
    text = strip_markdown_link_code(content)
    targets: list[tuple[str, str]] = []
    pattern = re.compile(
        r"([\u4e00-\u9fffA-Za-z0-9·&（）() -]{2,40}?)"
        r"[（(]"
        r"([A-Za-z0-9.]+(?:/[A-Za-z0-9.]+)*)"
        r"[）)]"
    )
    for match in pattern.finditer(text):
        name = clean(match.group(1)).strip(" |｜、，,;；")
        code = normalize_code(match.group(2))
        if name and code and not is_generic(name):
            targets.append((name, code))
    return targets


def split_plain_candidates(content: str) -> list[str]:
    text = strip_markdown_link_code(content)
    text = re.sub(r"[\u4e00-\u9fffA-Za-z0-9·&（）() -]{2,40}?[（(][A-Za-z0-9.]+(?:/[A-Za-z0-9.]+)*[）)]", " ", text)
    parts = re.split(r"[|｜、，,;；]", text)
    candidates: list[str] = []
    for part in parts:
        part = clean(part).strip(" -")
        if not part or is_generic(part):
            continue
        if len(part) > 30:
            continue
        candidates.append(part)
    return candidates


def is_generic(value: str) -> bool:
    text = clean(value)
    if not text or text in GENERIC_TERMS:
        return True
    if re.search(r"(方向|策略|节奏|配置|宏观|流动性|市场|赛道|风格|产业|板块|需求|业绩|扩产|交易|调整|行情|观察|机会)$", text):
        return True
    return False


def targets_from_content(content: str, by_alias: dict[str, list[dict[str, str]]]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for name, code in parse_explicit_targets(content):
        codes = split_codes(code) or [code]
        chosen_code = codes[0]
        symbol = normalized_symbol_for_code(chosen_code, by_alias)
        target_name = symbol["target_name"] if symbol else name
        market = market_from_code(chosen_code, symbol["market"] if symbol else "待确认")
        key = (target_name, chosen_code)
        if key not in seen:
            rows.append({"target_name": target_name, "stock_code": chosen_code, "market": market})
            seen.add(key)
    for candidate in split_plain_candidates(content):
        symbol = lookup_alias(candidate, by_alias)
        if not symbol:
            continue
        key = (symbol["target_name"], symbol["stock_code"])
        if key not in seen:
            rows.append(dict(symbol))
            seen.add(key)
    return rows


def collect_sections(markdown: str) -> list[dict[str, str]]:
    lines = markdown.splitlines()
    speaker = "待确认"
    sections: list[dict[str, str]] = []
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        speaker_match = re.match(r"^#{1,3}\s*(.+?)\s*$", line)
        if speaker_match and "【" not in line:
            maybe = normalize_speaker(speaker_match.group(1))
            if maybe and not maybe.startswith(("一、", "二、", "三、")):
                speaker = maybe
            index += 1
            continue
        bracket_match = re.search(r"【([^】]{1,160})】", line)
        if not bracket_match:
            index += 1
            continue
        heading = bracket_match.group(1)
        body_parts = [line[bracket_match.end() :].strip()]
        next_index = index + 1
        while next_index < len(lines):
            nxt = lines[next_index].strip()
            if re.match(r"^#{1,3}\s+", nxt) and "【" not in nxt:
                break
            if re.search(r"^#{0,6}\s*【[^】]{1,160}】", nxt):
                break
            body_parts.append(nxt)
            next_index += 1
        body = clean(" ".join(part for part in body_parts if part and not part.startswith("|")))
        sections.append({"speaker": speaker, "heading": heading, "body": body})
        index = next_index
    return sections


def evidence_for(target: str, code: str, heading: str, body: str) -> str:
    text = clean(f"【{heading}】 {body}")
    needles = [target]
    if code and code != "待确认":
        needles.extend(split_codes(code))
    parts = re.split(r"(?<=[。！？!?；;])\s*", text)
    for part in parts:
        if any(needle and needle in part for needle in needles):
            return clean(part)[:220]
    return text[:220]


def direction_from_text(text: str) -> str:
    if re.search(r"看空|偏空|减仓|卖出|回避|不看好|悲观|下行|miss|不及预期|风险.*较大|谨慎|清仓", text, re.I):
        return "看空"
    if re.search(r"首推|强烈推荐|坚定|看好|推荐|买入|加仓|关注|机会|受益|超预期|积极|首配|空间|低估|可以看|可以参与|继续推荐|有望|赔率", text):
        return "看多"
    if re.search(r"观望|等待|跟踪|没有太多|不确定|再看|观察", text):
        return "中性"
    return "信息不足"


def conviction_from_text(text: str, direction: str) -> str:
    if direction in {"中性", "信息不足"}:
        return "低" if direction == "中性" else "未说明"
    if re.search(r"首推|强烈推荐|坚定|确信|核心逻辑|非常|至少|确定|99%|100%|空间.*倍|首配", text):
        return "高"
    if re.search(r"可以看|可以关注|可能|观察|跟踪|还不错|适当|小仓位", text):
        return "低"
    return "中"


def horizon_from_text(text: str) -> str:
    if re.search(r"明天|本周|短期|这周|下周|近期|7月|月底|二季报|中报", text):
        return "短期"
    if re.search(r"下半年|三季度|四季度|年底|明年|未来[一二三四五六七八九十0-9-]+个?季度|1-2 年|一年|中期", text):
        return "中期"
    if re.search(r"长期|未来[三四五六七八九十0-9]+年|2029|2030", text):
        return "长期"
    return "未说明"


def viewpoint_sentence(target: str, direction: str, evidence: str) -> str:
    phrase = {
        "看多": "看多",
        "看空": "看空",
        "中性": "中性观察",
        "信息不足": "信息不足",
    }[direction]
    reason = evidence
    reason = re.sub(r"^【[^】]+】\s*", "", reason)
    reason = re.sub(r"\s+", " ", reason).strip(" ，,。")
    if len(reason) > 90:
        reason = reason[:90].rstrip(" ，,。") + "..."
    return f"对{target}{phrase}，依据是{reason}。"


def build_rows_for_note(item: dict[str, Any], by_alias: dict[str, list[dict[str, str]]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    markdown = Path(item["source_path"]).read_text(encoding="utf-8")
    meeting_date = item["meeting_date"] or "待确认"
    semantic_rows: list[dict[str, Any]] = []
    identified: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for section in collect_sections(markdown):
        heading = section["heading"]
        body = section["body"]
        section_text = clean(f"【{heading}】 {body}")
        sector = sector_from_bracket(heading)
        for target in targets_from_content(heading, by_alias):
            evidence = evidence_for(target["target_name"], target["stock_code"], heading, body)
            key = (section["speaker"], target["target_name"], target["stock_code"], evidence[:80])
            if key in seen:
                continue
            seen.add(key)
            direction = direction_from_text(section_text)
            conviction = conviction_from_text(section_text, direction)
            horizon = horizon_from_text(section_text)
            semantic_rows.append(
                {
                    "meeting_date": meeting_date,
                    "viewpoint_date": meeting_date,
                    "target_name": target["target_name"],
                    "stock_code": target["stock_code"],
                    "market": target["market"],
                    "sector_name": sector,
                    "presenter": section["speaker"],
                    "presenter_normalized": section["speaker"],
                    "direction": direction,
                    "conviction": conviction,
                    "time_horizon": horizon,
                    "core_viewpoint": viewpoint_sentence(target["target_name"], direction, evidence),
                    "evidence": evidence,
                    "rationale": "由已审核纪要的发言人标题、标的标题和相邻正文判断。明确代码或本地别名唯一匹配后确认标的；方向按正文语义判断。",
                }
            )
            identified.append(
                {
                    "target_name": target["target_name"],
                    "stock_code": target["stock_code"],
                    "market": target["market"],
                    "candidate_status": "confirmed",
                    "matched_alias": target["target_name"],
                    "needs_alias_review": False,
                    "alias_review_reason": "",
                    "evidence": evidence,
                    "rationale": "标题或正文出现明确标的，且代码显式给出或本地别名库唯一匹配。",
                }
            )
    # Deduplicate identified rows by target/code while preserving first evidence.
    deduped: list[dict[str, Any]] = []
    identified_seen: set[tuple[str, str]] = set()
    for row in identified:
        key = (row["target_name"], row["stock_code"])
        if key in identified_seen:
            continue
        identified_seen.add(key)
        deduped.append(row)
    return semantic_rows, deduped


def run_exporter(item: dict[str, Any], semantic_path: Path, identified_path: Path, output_path: Path, json_output_path: Path) -> None:
    if EXPORTER is None or not EXPORTER.is_file():
        raise RuntimeError("explicit structured-table exporter is unavailable")
    cmd = [
        sys.executable,
        str(EXPORTER),
        "--semantic-rows",
        str(semantic_path),
        "--identified-targets",
        str(identified_path),
        "--meeting-markdown",
        item["source_path"],
        "--output",
        str(output_path),
        "--json-output",
        str(json_output_path),
        "--source-record-id",
        item["source_record_id"],
        "--source-archive-url",
        item["source_archive_url"],
        "--source-file-name",
        item["source_remote_name"],
        "--meeting-date",
        item["meeting_date"],
        "--generated-at",
        datetime.now(timezone(timedelta(hours=8))).replace(microsecond=0).isoformat(),
        "--model-version",
        "codex-heading-semantic-v1",
        "--symbol-universe-version",
        "configured-symbol-universe",
    ]
    result = subprocess.run(cmd, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"export failed for {item['source_name']}: {(result.stderr or result.stdout).strip()}")


def main() -> int:
    global EXPORTER, ROOT, MANIFEST, SYMBOL_UNIVERSE, OUT_DIR, ARTIFACT_DIR
    parser = argparse.ArgumentParser(description="Rebuild local structured viewpoint artifacts from an explicit batch manifest.")
    parser.add_argument("--exporter", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--symbol-universe", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="write local regeneration artifacts")
    args = parser.parse_args()
    ROOT = args.work_dir.expanduser().resolve()
    MANIFEST = ROOT / "manifest.json"
    SYMBOL_UNIVERSE = args.symbol_universe.expanduser().resolve()
    OUT_DIR = ROOT / "new_structured"
    ARTIFACT_DIR = ROOT / "artifacts"
    if not args.apply:
        print(json.dumps({"ok": True, "dry_run": True, "output_root": str(ROOT)}))
        return 0
    EXPORTER = args.exporter.expanduser().resolve()
    _by_code, by_alias = load_symbols(SYMBOL_UNIVERSE)
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    summary: list[dict[str, Any]] = []
    for item in manifest:
        stem = safe_file_stem(item["source_name"])
        note_dir = ARTIFACT_DIR / stem
        note_dir.mkdir(parents=True, exist_ok=True)
        semantic_rows, identified_rows = build_rows_for_note(item, by_alias)
        semantic_path = note_dir / "semantic_rows.json"
        identified_path = note_dir / "identified_targets.json"
        output_path = OUT_DIR / f"{stem} - 结构化表格.md"
        json_output_path = OUT_DIR / f"{stem} - 结构化表格.json"
        semantic_path.write_text(json.dumps(semantic_rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        identified_path.write_text(json.dumps(identified_rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        run_exporter(item, semantic_path, identified_path, output_path, json_output_path)
        exported_rows = json.loads(json_output_path.read_text(encoding="utf-8"))
        summary.append(
            {
                "source_record_id": item["source_record_id"],
                "source_name": item["source_name"],
                "rows": len(exported_rows),
                "reviewable_rows": sum(1 for row in exported_rows if row.get("reviewable_prediction")),
                "output_path": str(output_path),
                "json_output_path": str(json_output_path),
                "semantic_rows": str(semantic_path),
                "identified_targets": str(identified_path),
                "structured_record_id": item.get("structured_record_id", ""),
                "structured_file_token": item.get("structured_file_token", ""),
                "structured_file_url": item.get("structured_file_url", ""),
            }
        )
    (ROOT / "generation_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"generated": len(summary), "total_rows": sum(item["rows"] for item in summary), "summary_path": str(ROOT / "generation_summary.json")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
