#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from industry_market_viewpoints import (  # noqa: E402
    SkillContractError,
    export_reviewed_artifact,
    generate_draft_artifacts,
    source_fragments,
    validate_artifact,
)


def _read_text(path: str, label: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        raise SkillContractError(f"{label} not found: {path}") from None
    except UnicodeDecodeError:
        raise SkillContractError(f"{label} must be UTF-8: {path}") from None


def _read_json(path: str, label: str) -> Any:
    try:
        return json.loads(_read_text(path, label))
    except json.JSONDecodeError as exc:
        raise SkillContractError(
            f"{label} must be valid JSON: {exc.msg} at line {exc.lineno}"
        ) from None


def _write_text(path: str, value: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(value, encoding="utf-8")


def _write_json(path: str, value: Any) -> None:
    _write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate and validate industry/market viewpoint artifacts."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    fragments_parser = subparsers.add_parser("source-fragments")
    fragments_parser.add_argument("--meeting-markdown", required=True)
    fragments_parser.add_argument("--output", required=True)

    generate_parser = subparsers.add_parser("generate")
    generate_parser.add_argument("--meeting-markdown", required=True)
    generate_parser.add_argument("--claim-units", required=True)
    generate_parser.add_argument("--context", required=True)
    generate_parser.add_argument("--review-output", required=True)
    generate_parser.add_argument("--json-output", required=True)

    export_parser = subparsers.add_parser("export-reviewed")
    export_parser.add_argument("--review-markdown", required=True)
    export_parser.add_argument("--context", required=True)
    export_parser.add_argument("--json-output", required=True)

    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("--artifact-json", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "source-fragments":
        fragments = source_fragments(_read_text(args.meeting_markdown, "meeting Markdown"))
        _write_json(args.output, fragments)
        result = {"status": "generated", "fragment_count": len(fragments)}
    elif args.command == "generate":
        review_markdown, artifact = generate_draft_artifacts(
            _read_text(args.meeting_markdown, "meeting Markdown"),
            _read_json(args.claim_units, "claim units"),
            _read_json(args.context, "context"),
        )
        _write_text(args.review_output, review_markdown)
        _write_json(args.json_output, artifact)
        result = {"status": "generated", "item_count": len(artifact["items"])}
    elif args.command == "export-reviewed":
        artifact = export_reviewed_artifact(
            _read_text(args.review_markdown, "review Markdown"),
            _read_json(args.context, "context"),
        )
        _write_json(args.json_output, artifact)
        result = {"status": "exported", "item_count": len(artifact["items"])}
    else:
        artifact = validate_artifact(_read_json(args.artifact_json, "artifact JSON"))
        result = {"status": "valid", "item_count": len(artifact["items"])}
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SkillContractError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from None
