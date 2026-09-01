"""Deterministic hashing and structured JSON envelope adapters."""

from __future__ import annotations

import hashlib
from typing import Any

from .common import clean_cell
from .contract import SCHEMA_VERSION


def require_meeting_id(value: Any) -> str:
    meeting_id = clean_cell(value)
    if not meeting_id:
        raise SystemExit("meeting_id is required and must be supplied by the data pipeline")
    return meeting_id


def structured_markdown_hash(markdown: str) -> str:
    """Hash the exact decoded Markdown text consumed by the exporter."""

    return hashlib.sha256(markdown.encode("utf-8")).hexdigest()


def build_json_envelope(
    *,
    rows: list[dict[str, Any]],
    meeting_id: str,
    structured_markdown_sha256: str,
    security_master_version: str = "unavailable",
) -> dict[str, Any]:
    return {
        "metadata": {
            "meeting_id": require_meeting_id(meeting_id),
            "structured_markdown_sha256": structured_markdown_sha256,
            "schema_version": SCHEMA_VERSION,
            "security_master_version": clean_cell(security_master_version) or "unavailable",
        },
        "rows": rows,
    }
