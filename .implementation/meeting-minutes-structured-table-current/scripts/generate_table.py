#!/usr/bin/env python3
"""CLI facade for structured viewpoint artifacts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


# Running this file directly puts ``scripts/`` on sys.path, not the Skill root.
# Add the package root without changing the installed or source CLI contract.
_SKILL_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_SECURITY_MASTER = _SKILL_ROOT / "data" / "security_master.csv"
if str(_SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILL_ROOT))

from structured_table.application import (  # noqa: E402
    ReviewMarkdownResult,
    parse_and_normalize_review_markdown,
)
from structured_table.claims import assign_viewpoint_ids, normalize_claim_units  # noqa: E402
from structured_table.common import (  # noqa: E402
    markdown_field,
    normalize_date,
    read_text,
    read_text_with_sha256,
    source_filename_date,
)
from structured_table.contract import MISSING_VALUE  # noqa: E402
from structured_table.official_json import (  # noqa: E402
    build_json_envelope,
    require_meeting_id,
)
from structured_table.review_codec import markdown_document  # noqa: E402
from structured_table.security_master import SecurityMaster  # noqa: E402
from structured_table.speaker_master import SpeakerMaster  # noqa: E402


def load_claim_units(path: str) -> list[dict[str, Any]]:
    label = "claim_units.json"
    try:
        raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
        data = json.loads(raw)
    except FileNotFoundError:
        raise SystemExit(f"{label} not found: {path}") from None
    except UnicodeDecodeError:
        raise SystemExit(f"{label} must be UTF-8 JSON: {path}") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{label} must be valid JSON: {exc.msg} at line {exc.lineno}") from None
    if not isinstance(data, dict) or not isinstance(data.get("claim_units"), list):
        raise SystemExit(f"{label} root must be an object containing claim_units")
    data = data["claim_units"]
    if not all(isinstance(item, dict) for item in data):
        raise SystemExit(f"{label} items must be JSON objects")
    return data


def emit_artifact(content: str, output: str | None) -> None:
    if not output:
        print(content, end="")
        return
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def load_security_master(path: str | None) -> SecurityMaster | None:
    selected = Path(path) if path else _DEFAULT_SECURITY_MASTER
    try:
        return SecurityMaster.from_csv(selected)
    except SystemExit as exc:
        print(
            f"warning: {exc}; continuing without local security-master verification",
            file=sys.stderr,
        )
    return None


def load_speaker_master(path: str | None) -> SpeakerMaster | None:
    if not path:
        return None
    try:
        master = SpeakerMaster.from_csv(Path(path))
    except ValueError as exc:
        print(
            f"warning: {exc}; using original presenter values",
            file=sys.stderr,
        )
        return None
    for warning in master.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return master


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate structured viewpoint Markdown, or regenerate JSON from current Markdown."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--claim-units", help="Model-generated claim_units JSON path, or - for stdin")
    mode.add_argument(
        "--structured-markdown",
        help="Current structured viewpoint Markdown used to regenerate JSON",
    )
    parser.add_argument("--meeting-markdown", help="Confirmed meeting Markdown for --claim-units")
    parser.add_argument("--output")
    parser.add_argument("--meeting-id", required=True, help="Meeting ID supplied by the data pipeline")
    parser.add_argument("--meeting-date", default="")
    parser.add_argument(
        "--security-master",
        help="Override the bundled CSV used for exact, unique security identity resolution",
    )
    parser.add_argument(
        "--speaker-master",
        help="Optional reviewed CSV used to fill the normalized speaker field",
    )
    args = parser.parse_args()

    if args.structured_markdown:
        markdown, markdown_sha256 = read_text_with_sha256(
            args.structured_markdown, "structured markdown"
        )
        security_master = load_security_master(args.security_master)
        result: ReviewMarkdownResult = parse_and_normalize_review_markdown(
            markdown,
            meeting_date=args.meeting_date,
            meeting_id=args.meeting_id,
            security_master=security_master,
            structured_markdown_sha256=markdown_sha256,
        )
        meeting_id = result.meeting_id
        rows = result.rows
        envelope = build_json_envelope(
            rows=rows,
            meeting_id=meeting_id,
            structured_markdown_sha256=result.structured_markdown_sha256,
            security_master_version=(
                security_master.snapshot_version
                if security_master is not None
                else "unavailable"
            ),
        )
        emit_artifact(json.dumps(envelope, ensure_ascii=False, indent=2) + "\n", args.output)
        return 0

    if not args.meeting_markdown:
        raise SystemExit("--claim-units requires --meeting-markdown")
    meeting_markdown = read_text(args.meeting_markdown, "meeting markdown")
    meeting_date = normalize_date(args.meeting_date or markdown_field(meeting_markdown, "会议日期"))
    if meeting_date == MISSING_VALUE:
        meeting_date = source_filename_date(args.meeting_markdown)
    meeting_id = require_meeting_id(args.meeting_id)
    security_master = load_security_master(args.security_master)
    speaker_master = load_speaker_master(args.speaker_master)
    rows: list[dict[str, Any]] = []
    for index, unit in enumerate(load_claim_units(args.claim_units), start=1):
        try:
            unit_rows = normalize_claim_units(
                [unit],
                meeting_date=meeting_date,
                meeting_markdown=meeting_markdown,
                meeting_id=meeting_id,
                security_master=security_master,
                speaker_master=speaker_master,
            )
            for row in unit_rows:
                if any(
                    item["locator"].startswith("Q-")
                    for item in row["source_evidence"]
                ):
                    print(
                        f"warning: claim unit {index}: a source quote was absent or repeated; "
                        "retained with a content locator",
                        file=sys.stderr,
                    )
                rows.append(row)
        except SystemExit as exc:
            print(f"warning: claim unit {index}: {exc}; skipped", file=sys.stderr)
    rows = assign_viewpoint_ids(rows, meeting_id)
    document = markdown_document(rows, {"meeting_date": meeting_date})
    emit_artifact(document, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
