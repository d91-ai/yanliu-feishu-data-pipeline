#!/usr/bin/env python3
"""
Validate that text artifacts are UTF-8 readable and, optionally, contain Chinese text.
"""

from __future__ import annotations

import argparse
import re
import sys
import zipfile
from pathlib import Path

TEXT_SUFFIXES = {
    ".csv",
    ".json",
    ".log",
    ".md",
    ".srt",
    ".tsv",
    ".txt",
    ".vtt",
    ".yaml",
    ".yml",
}

DOCX_XML_NAMES = {
    "word/document.xml",
    "word/footnotes.xml",
    "word/endnotes.xml",
    "word/comments.xml",
    "word/header1.xml",
    "word/footer1.xml",
}

CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
BOM_UTF8 = b"\xef\xbb\xbf"
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _iter_input_files(paths: list[Path], recursive: bool) -> list[Path]:
    files: list[Path] = []
    for item in paths:
        resolved = item.expanduser().resolve()
        if resolved.is_dir():
            pattern = "**/*" if recursive else "*"
            for child in resolved.glob(pattern):
                if child.is_file() and (child.suffix.lower() in TEXT_SUFFIXES or child.suffix.lower() == ".docx"):
                    files.append(child)
        elif resolved.is_file():
            files.append(resolved)
        else:
            raise FileNotFoundError(f"输入不存在: {item}")
    return files


def _decode_utf8_bytes(path: Path, reject_bom: bool = False) -> str:
    data = path.read_bytes()
    if reject_bom and data.startswith(BOM_UTF8):
        raise UnicodeError(f"{path}: 检测到 UTF-8 BOM；skill frontmatter 必须以 --- 直接开头")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UnicodeError(f"{path}: 不是有效 UTF-8: {exc}") from exc
    if "\ufffd" in text:
        raise UnicodeError(f"{path}: 检测到 Unicode 替换字符 U+FFFD，疑似编码损坏")
    return text


def _decode_docx_xml(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            xml_parts: list[str] = []
            for name in archive.namelist():
                if name in DOCX_XML_NAMES or (name.startswith("word/header") and name.endswith(".xml")) or (name.startswith("word/footer") and name.endswith(".xml")):
                    try:
                        xml_parts.append(archive.read(name).decode("utf-8"))
                    except UnicodeDecodeError as exc:
                        raise UnicodeError(f"{path}:{name}: DOCX XML 不是有效 UTF-8: {exc}") from exc
    except zipfile.BadZipFile as exc:
        raise UnicodeError(f"{path}: 不是有效 DOCX/ZIP 文件: {exc}") from exc
    text = "\n".join(xml_parts)
    if "\ufffd" in text:
        raise UnicodeError(f"{path}: DOCX XML 检测到 Unicode 替换字符 U+FFFD，疑似编码损坏")
    return text


def validate_file(path: Path, require_cjk: bool, portable_skill: bool = False) -> tuple[bool, str]:
    suffix = path.suffix.lower()
    if suffix == ".docx":
        text = _decode_docx_xml(path)
    else:
        text = _decode_utf8_bytes(path, reject_bom=portable_skill and path.name == "SKILL.md")

    if require_cjk and not CJK_RE.search(text):
        return False, f"{path}: 未检测到中文字符"
    if portable_skill and path.name == "SKILL.md":
        if not text.startswith("---\n"):
            return False, f"{path}: skill frontmatter 必须以 '---' + LF 开头，不能有 BOM、空行或 CRLF 前缀"
        if "\r\n" in text or "\r" in text:
            return False, f"{path}: 检测到 CRLF/CR 换行；为跨平台导入稳定性，SKILL.md 应使用 LF"
        if CONTROL_RE.search(text):
            return False, f"{path}: 检测到不可见控制字符，可能导致导入解析异常"
    return True, f"{path}: ok"


def main() -> int:
    parser = argparse.ArgumentParser(description="校验文本/Word 文件是否能按 UTF-8 正常读取")
    parser.add_argument("paths", nargs="+", help="要校验的文件或目录")
    parser.add_argument("--recursive", action="store_true", help="目录递归校验")
    parser.add_argument("--require-cjk", action="store_true", help="要求文件内容包含中文字符")
    parser.add_argument("--portable-skill", action="store_true", help="额外校验 SKILL.md 跨平台导入约束：UTF-8 无 BOM、LF 换行、frontmatter 直接开头")
    args = parser.parse_args()

    try:
        files = _iter_input_files([Path(item) for item in args.paths], args.recursive)
    except Exception as exc:
        print(f"UTF-8 校验失败: {exc}", file=sys.stderr)
        return 1

    failed = False
    for path in files:
        try:
            ok, message = validate_file(path, args.require_cjk, args.portable_skill)
        except Exception as exc:
            ok, message = False, str(exc)
        print(message)
        failed = failed or not ok
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
