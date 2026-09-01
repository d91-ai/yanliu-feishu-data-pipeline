#!/usr/bin/env python3
"""
Organize historical raw meeting archives into date/meeting-session folders.
"""

from __future__ import annotations

import argparse
import re
import shutil
from collections import defaultdict
from pathlib import Path

from archive_raw_inputs import (
    DEFAULT_ARCHIVE_ROOT,
    material_label,
    meeting_folder_name,
    sanitize_filename,
    standardized_filename,
    unique_target_path,
)

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
STANDARD_RE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2}) - (?P<title>.+?) - 原始\d{2}-.+$")
STANDARD_FOLDER_RE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2}) - (?P<title>.+)$")
TYPE_ORDER = {
    "文稿": 0,
    "录音": 1,
    "录像": 2,
    "附件": 3,
}


def strip_date_tokens(value: str) -> str:
    cleaned = value
    cleaned = re.sub(r"20\d{2}[-_. ]?\d{1,2}[-_. ]?\d{1,2}", " ", cleaned)
    cleaned = re.sub(r"20\d{6}", " ", cleaned)
    cleaned = re.sub(r"^\s*\d{1,2}月\d{1,2}日\s*\d{1,2}[_:：]\d{2}\s*(录音|记录)?", " ", cleaned)
    cleaned = re.sub(r"^\s*0?\d{1,2}0?\d{1,2}\s+", " ", cleaned)
    cleaned = re.sub(r"^\s*\d{1,2}[-_. ]\d{1,2}\s*", " ", cleaned)
    cleaned = re.sub(r"\b\d{1,2}[_:：]\d{2}\b", " ", cleaned)
    return cleaned


def infer_title_from_name(path: Path, archive_date: str) -> str:
    standard = STANDARD_RE.match(path.name)
    if standard and standard.group("date") == archive_date:
        return sanitize_filename(standard.group("title"))

    stem = path.stem
    stem = re.sub(r"\.preprocessed$", "", stem, flags=re.IGNORECASE)
    stem = re.sub(r"(_原文|_笔记|_sensevoice.*|_transcript.*)$", "", stem, flags=re.IGNORECASE)
    stem = re.sub(r"(-root-duplicate|-duplicate)$", "", stem, flags=re.IGNORECASE)
    stem = strip_date_tokens(stem)
    stem = re.sub(r"[-_ ]\d{6}$", "", stem)
    stem = re.sub(r"[-_ ]\d{1,2}$", "", stem)
    stem = re.sub(r"[_\-]+", " ", stem)
    stem = re.sub(r"\s+", " ", stem).strip()
    return sanitize_filename(stem, fallback=path.stem)


def session_sort_key(path: Path) -> tuple[int, str, str]:
    label = material_label(path)
    return (TYPE_ORDER.get(label, 9), path.suffix.lower(), path.name)


def collect_files(archive_root: Path, only_date: str | None) -> dict[tuple[str, str], list[Path]]:
    groups: dict[tuple[str, str], list[Path]] = defaultdict(list)
    for date_dir in sorted(item for item in archive_root.iterdir() if item.is_dir() and DATE_RE.match(item.name)):
        if only_date and date_dir.name != only_date:
            continue
        for path in sorted(date_dir.rglob("*")):
            if not path.is_file() or path.name == ".DS_Store":
                continue
            rel = path.relative_to(date_dir)
            title = infer_title_from_name(path, date_dir.name)
            if len(rel.parts) > 1:
                folder_match = STANDARD_FOLDER_RE.match(rel.parts[0])
                if folder_match and folder_match.group("date") == date_dir.name:
                    title = sanitize_filename(folder_match.group("title"))
            groups[(date_dir.name, title)].append(path)
    return groups


def same_path(left: Path, right: Path) -> bool:
    return left.resolve().as_posix().casefold() == right.resolve().as_posix().casefold()


def organize(archive_root: Path, only_date: str | None, apply: bool) -> list[tuple[Path, Path]]:
    moves: list[tuple[Path, Path]] = []
    groups = collect_files(archive_root, only_date)

    for (archive_date, title), files in sorted(groups.items()):
        target_dir = archive_root / archive_date / meeting_folder_name(archive_date, title)
        for index, source in enumerate(sorted(files, key=session_sort_key), 1):
            filename = standardized_filename(source, archive_date, index, title)
            target = target_dir / filename
            if same_path(source, target):
                continue
            if apply:
                target_dir.mkdir(parents=True, exist_ok=True)
                final_target = unique_target_path(target_dir, filename)
                shutil.move(str(source), str(final_target))
                moves.append((source, final_target))
            else:
                moves.append((source, target))
    return moves


def remove_empty_dirs(archive_root: Path) -> int:
    removed = 0
    for date_dir in sorted((item for item in archive_root.iterdir() if item.is_dir() and DATE_RE.match(item.name)), reverse=True):
        for directory in sorted((item for item in date_dir.rglob("*") if item.is_dir()), key=lambda item: len(item.parts), reverse=True):
            try:
                directory.rmdir()
                removed += 1
            except OSError:
                pass
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description="把历史会议原始记录整理为 日期/场次/规范文件名 结构")
    parser.add_argument("--archive-root", default=str(DEFAULT_ARCHIVE_ROOT), help=f"归档根目录，默认 {DEFAULT_ARCHIVE_ROOT}")
    parser.add_argument("--date", help="只整理指定日期，格式 YYYY-MM-DD")
    parser.add_argument("--apply", action="store_true", help="实际移动文件；不加时只预览")
    parser.add_argument("--remove-empty-dirs", action="store_true", help="整理后删除空的旧目录")
    args = parser.parse_args()

    archive_root = Path(args.archive_root).expanduser().resolve()
    moves = organize(archive_root, args.date, args.apply)
    for source, target in moves:
        action = "MOVE" if args.apply else "DRY-RUN"
        print(f"{action}\t{source}\t=>\t{target}")

    if args.apply and args.remove_empty_dirs:
        removed = remove_empty_dirs(archive_root)
        print(f"REMOVED_EMPTY_DIRS\t{removed}")
    print(f"TOTAL\t{len(moves)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
