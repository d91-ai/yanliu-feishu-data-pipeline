"""Application use-cases shared by the command-line entry points."""

from __future__ import annotations

from dataclasses import dataclass
import sys
from typing import Any

from .claims import assign_viewpoint_ids, normalize_review_rows
from .common import normalize_date
from .official_json import require_meeting_id, structured_markdown_hash
from .review_codec import parse_review_markdown_rows, parse_review_meeting_date
from .security_master import SecurityMaster


@dataclass(frozen=True)
class ReviewMarkdownResult:
    """Canonical result of the current structured Markdown ingestion boundary."""

    meeting_date: str
    meeting_id: str
    rows: list[dict[str, Any]]
    structured_markdown_sha256: str


def resolve_review_meeting_date(markdown: str, requested_date: str = "") -> str:
    return normalize_date(requested_date or parse_review_meeting_date(markdown))


def parse_and_normalize_review_markdown(
    markdown: str,
    *,
    meeting_date: str = "",
    meeting_id: str,
    security_master: SecurityMaster | None = None,
    label: str = "structured markdown",
    structured_markdown_sha256: str = "",
) -> ReviewMarkdownResult:
    resolved_date = resolve_review_meeting_date(markdown, meeting_date)
    resolved_id = require_meeting_id(meeting_id)
    parsed_rows = parse_review_markdown_rows(markdown, label=label)
    rows: list[dict[str, Any]] = []
    for index, parsed_row in enumerate(parsed_rows, start=1):
        try:
            normalized = normalize_review_rows(
                [parsed_row],
                meeting_date=resolved_date,
                meeting_id=resolved_id,
                security_master=security_master,
                row_number_start=index,
            )
        except SystemExit as exc:
            print(f"warning: structured card {index}: {exc}; skipped", file=sys.stderr)
            continue
        rows.extend(normalized)
    rows = assign_viewpoint_ids(rows, resolved_id)
    return ReviewMarkdownResult(
        meeting_date=resolved_date,
        meeting_id=resolved_id,
        rows=rows,
        structured_markdown_sha256=(
            structured_markdown_sha256 or structured_markdown_hash(markdown)
        ),
    )
