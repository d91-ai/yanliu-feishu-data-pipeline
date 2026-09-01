#!/usr/bin/env python3
"""
Copy raw meeting input files into the dated investment-meeting inbox archive.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import warnings
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_WORKSPACE_ROOT = (
    Path(os.environ["INVESTMENT_MINUTES_WORKSPACE"]).expanduser()
    if os.environ.get("INVESTMENT_MINUTES_WORKSPACE")
    else Path.home() / "Documents/会议纪要整理"
)
DEFAULT_ARCHIVE_ROOT = DEFAULT_WORKSPACE_ROOT / "00 Inbox/会议原始记录"
INVALID_FILENAME_CHARS = r'[\\/:*?"<>|]+'
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TYPE_LABELS = {
    ".docx": "文稿",
    ".doc": "文稿",
    ".txt": "文稿",
    ".md": "文稿",
    ".pdf": "附件",
    ".mp3": "录音",
    ".m4a": "录音",
    ".wav": "录音",
    ".aac": "录音",
    ".flac": "录音",
    ".ogg": "录音",
    ".mp4": "录像",
    ".mov": "录像",
}


def sanitize_filename(value: str, *, fallback: str = "未命名会议", max_length: int = 80) -> str:
    cleaned = re.sub(INVALID_FILENAME_CHARS, "-", value or "").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = cleaned.strip(". ")
    if not cleaned:
        cleaned = fallback
    return cleaned[:max_length].rstrip(". ")


def material_label(path: Path) -> str:
    return TYPE_LABELS.get(path.suffix.lower(), "附件")


def normalize_archive_date(value: str) -> str:
    raw = (value or "").strip()
    if not DATE_PATTERN.match(raw):
        raise ValueError(f"归档日期必须使用 YYYY-MM-DD: {value}")
    try:
        return datetime.strptime(raw, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"归档日期无效: {value}") from exc


def standardized_filename(source: Path, archive_date: str, index: int, meeting_title: str | None) -> str:
    title = sanitize_filename(meeting_title or source.stem)
    label = material_label(source)
    return f"{archive_date} - {title} - 原始{index:02d}-{label}{source.suffix.lower()}"


def meeting_folder_name(archive_date: str, meeting_title: str | None) -> str:
    title = sanitize_filename(meeting_title)
    return f"{archive_date} - {title}"


def infer_meeting_title(files: list[Path]) -> str | None:
    for source in files:
        suffix = source.suffix.lower()
        if suffix == ".docx":
            try:
                from docx import Document  # type: ignore

                doc = Document(source)
                for paragraph in doc.paragraphs:
                    text = paragraph.text.strip()
                    if text:
                        return text
            except Exception:
                continue
        if suffix in {".txt", ".md"}:
            try:
                for line in source.read_text(encoding="utf-8").splitlines():
                    text = line.strip().lstrip("#").strip()
                    if text:
                        return text
            except Exception:
                continue
    return None


def unique_target_path(target_dir: Path, filename: str, reserved: set[Path] | None = None) -> Path:
    reserved = reserved or set()
    target = target_dir / filename
    if not target.exists() and target not in reserved:
        return target

    stem = target.stem
    suffix = target.suffix
    timestamp = datetime.now().strftime("%H%M%S")
    candidate = target_dir / f"{stem}-{timestamp}{suffix}"
    counter = 2
    while candidate.exists() or candidate in reserved:
        candidate = target_dir / f"{stem}-{timestamp}-{counter}{suffix}"
        counter += 1
    return candidate


def plan_archive_files(
    files: list[Path],
    archive_root: Path,
    archive_date: str,
    meeting_title: str | None = None,
) -> dict[str, Any]:
    archive_date = normalize_archive_date(archive_date)
    resolved_files = [source.expanduser().resolve() for source in files]
    for resolved in resolved_files:
        if not resolved.exists():
            raise FileNotFoundError(f"输入文件不存在: {resolved}")
        if not resolved.is_file():
            raise IsADirectoryError(f"输入路径不是文件: {resolved}")

    resolved_title = meeting_title or infer_meeting_title(resolved_files)
    if not resolved_title and resolved_files:
        resolved_title = resolved_files[0].stem
    target_dir = archive_root / archive_date / meeting_folder_name(archive_date, resolved_title)

    reserved: set[Path] = set()
    planned_files: list[dict[str, str]] = []
    for index, resolved in enumerate(resolved_files, 1):
        target_name = standardized_filename(resolved, archive_date, index, resolved_title)
        target = unique_target_path(target_dir, target_name, reserved)
        reserved.add(target)
        planned_files.append(
            {
                "source": str(resolved),
                "target": str(target),
                "filename": target.name,
                "material_type": material_label(resolved),
            }
        )
    return {
        "archive_root": str(archive_root),
        "archive_date": archive_date,
        "meeting_title": sanitize_filename(resolved_title or ""),
        "target_dir": str(target_dir),
        "files": planned_files,
    }


def archive_target_candidates(target: Path) -> list[Path]:
    timestamp = datetime.now().strftime("%H%M%S")
    candidates = [target]
    candidates.extend(
        target.with_name(
            f"{target.stem}{f'-{timestamp}' if index == 1 else f'-{timestamp}-{index}'}{target.suffix}"
        )
        for index in range(1, 1000)
    )
    return candidates


def publish_archive_file(part_path: Path, target: Path) -> Path:
    """Atomically publish a completed archive file without overwriting."""
    for candidate in archive_target_candidates(target):
        try:
            os.link(part_path, candidate)
        except FileExistsError:
            continue
        except OSError as exc:
            raise OSError(
                exc.errno,
                "目标文件系统不支持安全的原子无覆盖归档；未采用可能覆盖或暴露半文件的降级方案",
                str(candidate),
            ) from exc
        return candidate
    raise FileExistsError(f"无法为归档文件原子发布未占用名称: {target.name}")


def cleanup_archive_part_file(part_path: Path | None) -> None:
    if part_path is None:
        return
    try:
        part_path.unlink(missing_ok=True)
    except OSError as exc:
        warnings.warn(f"已完成原始文件归档，但临时 part 文件清理失败: {part_path}: {exc}", RuntimeWarning)


def archive_files(
    files: list[Path],
    archive_root: Path,
    archive_date: str,
    meeting_title: str | None = None,
) -> list[Path]:
    plan = plan_archive_files(files, archive_root, archive_date, meeting_title)
    target_dir = Path(str(plan["target_dir"]))
    target_dir.mkdir(parents=True, exist_ok=True)

    copied: list[Path] = []
    try:
        for item in plan["files"]:
            source = Path(str(item["source"]))
            target = Path(str(item["target"]))
            part_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    prefix=f".{target.name}.",
                    suffix=".part",
                    dir=target_dir,
                    delete=False,
                ) as target_handle:
                    part_path = Path(target_handle.name)
                    with source.open("rb") as source_handle:
                        shutil.copyfileobj(source_handle, target_handle)
                shutil.copystat(source, part_path)
                target = publish_archive_file(part_path, target)
                copied.append(target)
            except Exception:
                raise
            finally:
                cleanup_archive_part_file(part_path)
    except Exception:
        for target in copied:
            target.unlink(missing_ok=True)
        raise
    return copied


def main() -> int:
    parser = argparse.ArgumentParser(description="按日期归档投资会议原始文件")
    parser.add_argument("files", nargs="+", help="用于整理会议纪要的原始文件")
    parser.add_argument("--archive-root", default=str(DEFAULT_ARCHIVE_ROOT), help=f"归档根目录，默认 {DEFAULT_ARCHIVE_ROOT}")
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"), help="归档日期，格式 YYYY-MM-DD")
    parser.add_argument("--title", help="会议标题，用于生成规范归档文件名")
    parser.add_argument("--dry-run", action="store_true", help="只预览目标路径，不复制文件")
    parser.add_argument("--json", action="store_true", help="输出 JSON，便于自动化调用")
    args = parser.parse_args()

    try:
        archive_root = Path(args.archive_root).expanduser().resolve()
        input_files = [Path(item) for item in args.files]
        if args.dry_run:
            plan = plan_archive_files(input_files, archive_root, args.date, args.title)
            payload = {"ok": True, "dry_run": True, **plan}
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                for item in plan["files"]:
                    print(item["target"])
            return 0

        copied = archive_files(input_files, archive_root, args.date, args.title)
    except Exception as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        else:
            print(f"归档失败: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(
            json.dumps(
                {
                    "ok": True,
                    "dry_run": False,
                    "archived_count": len(copied),
                    "archived_paths": [str(path) for path in copied],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    for path in copied:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
