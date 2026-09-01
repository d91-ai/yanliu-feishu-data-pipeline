"""Small, side-effect-free helpers used by the domain adapters."""

from __future__ import annotations

from datetime import datetime
import hashlib
import re
from pathlib import Path
from typing import Any

from .contract import MISSING_VALUE


def read_text(path: str | None, label: str = "text input") -> str:
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        raise SystemExit(f"{label} not found: {path}") from None
    except UnicodeDecodeError:
        raise SystemExit(f"{label} must be UTF-8 text: {path}") from None


def read_text_with_sha256(path: str, label: str) -> tuple[str, str]:
    """Decode UTF-8 once and hash the exact file bytes consumed."""

    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        raise SystemExit(f"{label} not found: {path}") from None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise SystemExit(f"{label} must be UTF-8 text: {path}") from None
    return text, hashlib.sha256(raw).hexdigest()


def clean_cell(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def clean_evidence_text(value: Any) -> str:
    """Preserve evidence wording and line boundaries while normalizing line endings."""

    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in text.strip().splitlines())


def normalize_date(value: Any) -> str:
    match = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", str(value or ""))
    if not match:
        return MISSING_VALUE
    year, month, day = map(int, match.groups())
    try:
        return datetime(year, month, day).strftime("%Y-%m-%d")
    except ValueError:
        return MISSING_VALUE


def markdown_field(markdown: str, label: str) -> str:
    for pattern in (
        rf"^\s*\*\*{re.escape(label)}\*\*\s*[:：]\s*(.+?)\s*$",
        rf"^\s*{re.escape(label)}\s*[:：]\s*(.+?)\s*$",
    ):
        match = re.search(pattern, markdown, flags=re.M)
        if match:
            return match.group(1).strip().strip("*").strip()
    return ""


def source_filename_date(path: str | None) -> str:
    if not path:
        return MISSING_VALUE
    first_part = Path(path).stem.split(" - ", 1)[0].strip()
    return normalize_date(first_part)
