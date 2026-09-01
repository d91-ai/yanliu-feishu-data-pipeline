#!/usr/bin/env python3
"""
Export a finalized meeting note to the user's Obsidian workflow as Markdown.
"""

from __future__ import annotations

import argparse
import os
import os
import re
import shutil
import sys
import tempfile
import warnings
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from validate_meeting_minutes_contract import validate_contract

DEFAULT_WORKSPACE_ROOT = (
    Path(os.environ["INVESTMENT_MINUTES_WORKSPACE"]).expanduser()
    if os.environ.get("INVESTMENT_MINUTES_WORKSPACE")
    else Path.home() / "Documents/会议纪要整理"
)
DEFAULT_EXPORT_DIR = DEFAULT_WORKSPACE_ROOT / "01 Projects/会议纪要"
INVALID_FILENAME_CHARS = r'[\\/:*?"<>|]+'
CJK_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
KNOWN_REVIEW_SERIES = tuple(
    item.strip()
    for item in os.environ.get("MEETING_REVIEW_SERIES", "").split("|")
    if item.strip()
)
MEETING_TYPE_ALIASES = {"上市公司交流": "公司交流"}
FILENAME_PLACEHOLDERS = {"", "会议系列", "会议类型", "未命名会议", "待确认"}


def validate_utf8_text_file(path: Path, *, require_cjk: bool = False) -> tuple[bool, str]:
    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        return False, f"{path}: 不是有效 UTF-8: {exc}"
    if "\ufffd" in text:
        return False, f"{path}: 检测到 Unicode 替换字符 U+FFFD，疑似编码损坏"
    if require_cjk and not CJK_PATTERN.search(text):
        return False, f"{path}: 未检测到中文字符"
    return True, "ok"


@dataclass
class ExportResult:
    md_path: Path
    md_created: bool
    md_message: str


def sanitize_filename(name: str) -> str:
    cleaned = re.sub(INVALID_FILENAME_CHARS, "-", name).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or "未命名会议"


def markdown_field(markdown: str, field: str, fallback: str = "") -> str:
    pattern = re.compile(rf"^\*\*{re.escape(field)}\*\*[:：]\s*(.+?)\s*$", re.MULTILINE)
    match = pattern.search(markdown)
    return match.group(1).strip() if match else fallback


def strip_suffix(value: str, suffixes: tuple[str, ...]) -> str:
    cleaned = value.strip()
    for suffix in sorted(suffixes, key=len, reverse=True):
        if cleaned.endswith(suffix):
            return cleaned[: -len(suffix)].strip(" -_—–｜|")
    return cleaned


def infer_review_series(content: str, source_name: str) -> str:
    explicit = markdown_field(content, "会议系列", "").strip()
    if explicit not in FILENAME_PLACEHOLDERS:
        return sanitize_filename(explicit)

    meeting_title = markdown_field(content, "会议标题", "").strip()
    haystacks = (meeting_title, source_name)
    matches = [series for series in KNOWN_REVIEW_SERIES if any(series in value for value in haystacks)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(f"多人复盘会匹配到多个会议系列: {', '.join(matches)}；请向用户确认")
    raise ValueError("无法确定多人复盘会的会议系列；请从原始文件名匹配或向用户确认")


def detect_filename_title(content: str, source_name: str) -> str:
    meeting_type_raw = markdown_field(content, "会议类型", "").strip()
    meeting_type = MEETING_TYPE_ALIASES.get(meeting_type_raw, meeting_type_raw)
    meeting_title = markdown_field(content, "会议标题", "").strip()

    if meeting_type == "多人复盘会":
        return infer_review_series(content, source_name)
    if meeting_type == "公司交流":
        company_name = strip_suffix(meeting_title, ("上市公司交流会议", "上市公司交流", "交流会议", "交流"))
        if not company_name or company_name in FILENAME_PLACEHOLDERS:
            raise ValueError("无法从会议标题确定公司名；请向用户确认")
        return sanitize_filename(f"{company_name} - 上市公司交流")
    if meeting_type == "专家交流":
        topic = strip_suffix(meeting_title, ("专家交流会议", "专家交流"))
        if not topic or topic in FILENAME_PLACEHOLDERS:
            raise ValueError("无法从会议标题确定专家交流主题；请向用户确认")
        return sanitize_filename(f"{topic} - 专家交流")
    raise ValueError("会议类型必须是多人复盘会、公司交流或专家交流")


def parse_meeting_date(raw: str, label: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        raise ValueError(f"{label} 必须为 YYYY-MM-DD 且为合法日期: {raw}")
    try:
        return datetime.strptime(raw, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"{label} 必须为 YYYY-MM-DD 且为合法日期: {raw}") from exc


def normalize_meeting_date(date_override: str | None, content: str = "") -> str:
    override = (date_override or "").strip()
    if override:
        return parse_meeting_date(override, "--meeting-date")
    metadata_date = markdown_field(content, "会议日期", "").strip()
    if metadata_date:
        return parse_meeting_date(metadata_date, "会议日期")
    return datetime.now().strftime("%Y-%m-%d")


def output_path_candidates(export_dir: Path, filename_base: str) -> list[Path]:
    candidates = [export_dir / f"{filename_base}.md"]
    stamp = datetime.now().strftime("%H%M%S")
    for idx in range(1, 1000):
        suffix = f"-{stamp}" if idx == 1 else f"-{stamp}-{idx}"
        candidates.append(export_dir / f"{filename_base}{suffix}.md")
    return candidates


def publish_completed_file(part_path: Path, export_dir: Path, filename_base: str) -> Path:
    """Atomically publish a completed file without exposing partial Markdown."""
    for candidate in output_path_candidates(export_dir, filename_base):
        try:
            os.link(part_path, candidate)
        except FileExistsError:
            continue
        except OSError as exc:
            raise OSError(
                exc.errno,
                "目标文件系统不支持安全的原子无覆盖发布；未采用可能覆盖或暴露半文件的降级方案",
                str(candidate),
            ) from exc
        return candidate
    raise FileExistsError(f"无法为 {filename_base} 原子发布未占用的输出文件名")


def cleanup_part_file(part_path: Path | None) -> None:
    if part_path is None:
        return
    try:
        part_path.unlink(missing_ok=True)
    except OSError as exc:
        warnings.warn(f"已完成 Markdown 发布，但临时 part 文件清理失败: {part_path}: {exc}", RuntimeWarning)


def export_note(source_file: Path, export_dir: Path, date_override: str | None) -> ExportResult:
    raw_content = source_file.read_text(encoding="utf-8")
    source_encoding_ok, source_encoding_message = validate_utf8_text_file(source_file, require_cjk=True)
    if not source_encoding_ok:
        raise UnicodeError(source_encoding_message)
    meeting_date = normalize_meeting_date(date_override, raw_content)
    title = detect_filename_title(raw_content, source_file.name)
    contract_result = validate_contract(raw_content)
    contract_errors = list(contract_result["errors"])
    if markdown_field(raw_content, "会议类型", "").strip() == "多人复盘会" and title:
        contract_errors = [
            error for error in contract_errors if error != "缺少会议元信息字段: 会议系列"
        ]
    if contract_errors:
        preview = "；".join(str(error) for error in contract_errors[:6])
        raise ValueError(f"会议纪要输出契约校验失败: {preview}")
    filename_base = f"{meeting_date} - {title}"
    export_dir = export_dir / meeting_date
    export_dir.mkdir(parents=True, exist_ok=True)

    md_path = export_dir / f"{filename_base}.md"
    part_path: Path | None = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{filename_base}.",
            suffix=".part",
            dir=export_dir,
            delete=False,
        ) as destination:
            part_path = Path(destination.name)
            with source_file.open("rb") as source:
                shutil.copyfileobj(source, destination)
        shutil.copystat(source_file, part_path)
        md_ok, md_message = validate_utf8_text_file(part_path, require_cjk=True)
        if not md_ok:
            raise UnicodeError(md_message)
        md_path = publish_completed_file(part_path, export_dir, filename_base)
    except Exception as exc:
        md_ok = False
        md_message = str(exc)
    finally:
        cleanup_part_file(part_path)

    return ExportResult(
        md_path=md_path,
        md_created=md_ok,
        md_message=md_message,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="导出投资会议纪要到 Obsidian 目录（仅 Markdown）")
    parser.add_argument("input_file", help="已整理完成的 Markdown 文件")
    parser.add_argument("--export-dir", default=str(DEFAULT_EXPORT_DIR), help=f"导出目录，默认 {DEFAULT_EXPORT_DIR}")
    parser.add_argument("--meeting-date", help="覆盖系统日期，格式 YYYY-MM-DD")
    args = parser.parse_args()

    source_file = Path(args.input_file).expanduser().resolve()
    if not source_file.exists():
        print(f"输入文件不存在: {source_file}", file=sys.stderr)
        return 1

    export_dir = Path(args.export_dir).expanduser().resolve()
    try:
        result = export_note(source_file, export_dir, args.meeting_date)
    except Exception as exc:
        print(f"Markdown: 未生成 ({exc})", file=sys.stderr)
        return 1

    if result.md_created:
        print(f"Markdown: {result.md_path}")
    else:
        print(f"Markdown: 未生成 ({result.md_message})")
    return 0 if result.md_created else 1


if __name__ == "__main__":
    raise SystemExit(main())
