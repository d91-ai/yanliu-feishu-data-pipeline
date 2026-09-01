#!/usr/bin/env python3
"""Candidate draft exporter for the current structured-table Skill.

This script deliberately starts from source-grounded claim units. It does not
call the approved-only official JSON envelope and cannot publish reviewed data.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import re
import sys
from typing import Any


class DraftExportError(ValueError):
    """Raised when the candidate contract or input is invalid."""


@dataclass(frozen=True)
class StructuredRuntime:
    normalize_claim_units: Any
    parse_and_normalize_approved_markdown: Any
    build_markdown_metadata: Any
    markdown_document: Any
    source_document_class: Any
    security_master_class: Any
    schema_version: int
    security_master_path: Path


def _read_json(path: Path, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise DraftExportError(f"{label} not found: {path}") from None
    except UnicodeDecodeError:
        raise DraftExportError(f"{label} must be UTF-8: {path}") from None
    except json.JSONDecodeError as exc:
        raise DraftExportError(
            f"{label} must be valid JSON: {exc.msg} at line {exc.lineno}"
        ) from None


def _read_text(path: Path, label: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise DraftExportError(f"{label} not found: {path}") from None
    except UnicodeDecodeError:
        raise DraftExportError(f"{label} must be UTF-8: {path}") from None


def _load_pipeline_contract(module_path: Path):
    module_path = module_path.expanduser().resolve()
    spec = importlib.util.spec_from_file_location(
        "meeting_pipeline_contract_for_structured_draft", module_path
    )
    if spec is None or spec.loader is None:
        raise DraftExportError(f"Cannot load pipeline contract: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except (OSError, RuntimeError, ValueError) as exc:
        raise DraftExportError(f"Invalid pipeline contract: {exc}") from exc
    if module.CONTRACT.contract_version != 1:
        raise DraftExportError("Pipeline contract version must be 1")
    return module


def _load_structured_runtime(skill_root: Path) -> StructuredRuntime:
    skill_root = skill_root.expanduser().resolve()
    manifest_path = skill_root / "contract" / "manifest.json"
    manifest = _read_json(manifest_path, "structured Skill manifest")
    if not isinstance(manifest, dict):
        raise DraftExportError("structured Skill manifest must contain an object")
    if manifest.get("contract_version") != 4 or manifest.get("schema_version") != 7:
        raise DraftExportError("structured Skill must be contract v4/schema v7")
    security_master_value = manifest.get("security_master")
    if not isinstance(security_master_value, dict):
        raise DraftExportError("structured Skill manifest missing security_master")
    security_master_path = (skill_root / str(security_master_value.get("default_path") or "")).resolve()
    if skill_root not in security_master_path.parents or not security_master_path.is_file():
        raise DraftExportError("structured Skill security master is missing or unsafe")

    if str(skill_root) not in sys.path:
        sys.path.insert(0, str(skill_root))
    try:
        claims = importlib.import_module("structured_table.claims")
        official_json = importlib.import_module("structured_table.official_json")
        review_codec = importlib.import_module("structured_table.review_codec")
        source_document = importlib.import_module("structured_table.source_document")
        security_master = importlib.import_module("structured_table.security_master")
    except ImportError as exc:
        raise DraftExportError(f"Cannot import structured Skill runtime: {exc}") from exc
    return StructuredRuntime(
        normalize_claim_units=claims.normalize_claim_units,
        parse_and_normalize_approved_markdown=importlib.import_module(
            "structured_table.application"
        ).parse_and_normalize_approved_markdown,
        build_markdown_metadata=official_json.build_markdown_metadata,
        markdown_document=review_codec.markdown_document,
        source_document_class=source_document.SourceDocument,
        security_master_class=security_master.SecurityMaster,
        schema_version=7,
        security_master_path=security_master_path,
    )


def _claim_units_array(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        value = value.get("claim_units")
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise DraftExportError("claim units must be an array or an object with claim_units array")
    return value


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def generate_draft(
    *,
    meeting_markdown: str,
    claim_units: Any,
    context: dict[str, Any],
    skill_root: Path,
    pipeline_contract_path: Path,
) -> tuple[str, dict[str, Any]]:
    pipeline_contract = _load_pipeline_contract(pipeline_contract_path)
    runtime = _load_structured_runtime(skill_root)
    if not isinstance(context, dict):
        raise DraftExportError("context must contain an object")
    required_context = {
        "meeting_uid",
        "meeting_date",
        "meeting_series",
        "meeting_type",
        "data_version",
        "source_review_status",
        "artifact_review_status",
        "generated_at",
    }
    if set(context) != required_context:
        raise DraftExportError("context fields do not match the draft contract")
    if context.get("artifact_review_status") == "已审核":
        raise DraftExportError("draft generation cannot claim artifact_review_status=已审核")

    source_document = runtime.source_document_class.from_markdown(meeting_markdown)
    security_master = runtime.security_master_class.from_csv(runtime.security_master_path)
    try:
        rows = runtime.normalize_claim_units(
            _claim_units_array(claim_units),
            meeting_date=context.get("meeting_date"),
            source_fragments=source_document.fragments,
            source_speakers=source_document.speakers,
            meeting_uid=pipeline_contract.validate_meeting_uid(context.get("meeting_uid")),
            security_master=security_master,
        )
        markdown_metadata = runtime.build_markdown_metadata(
            rows=rows,
            meeting_uid=context.get("meeting_uid"),
            meeting_date=context.get("meeting_date"),
            source_record_id="",
            source_archive_url="",
            source_file_name="",
            generated_at=context.get("generated_at"),
            schema_version=runtime.schema_version,
            model_version="",
        )
        review_markdown = runtime.markdown_document(rows, markdown_metadata)
    except SystemExit as exc:
        raise DraftExportError(str(exc)) from None

    metadata = pipeline_contract.validate_artifact_metadata(
        {
            "schema_version": pipeline_contract.CONTRACT.metadata_schema_version,
            "meeting_uid": context.get("meeting_uid"),
            "meeting_date": context.get("meeting_date"),
            "meeting_series": context.get("meeting_series"),
            "meeting_type": context.get("meeting_type"),
            "artifact_type": "structured_viewpoints",
            "data_version": context.get("data_version"),
            "quality_status": "unreviewed",
            "source_review_status": context.get("source_review_status"),
            "artifact_review_status": context.get("artifact_review_status"),
            "source_md_sha256": _sha256_text(meeting_markdown),
            "review_md_sha256": _sha256_text(review_markdown),
            "item_count": len(rows),
            "generated_at": context.get("generated_at"),
        }
    )
    viewpoint_ids = [str(row.get("viewpoint_id") or "") for row in rows]
    if not all(viewpoint_ids) or len(viewpoint_ids) != len(set(viewpoint_ids)):
        raise DraftExportError("structured draft contains missing or duplicate viewpoint IDs")
    return review_markdown, {"metadata": metadata, "rows": rows}


def generate_reviewed(
    *,
    review_markdown: str,
    context: dict[str, Any],
    skill_root: Path,
    pipeline_contract_path: Path,
) -> dict[str, Any]:
    """Export reviewed Markdown through the current schema-v7 parser.

    The reviewed Markdown remains the semantic authority.  This adapter adds
    only the shared meeting-pipeline metadata envelope; it does not re-run a
    model or restore rows removed by the reviewer.
    """

    pipeline_contract = _load_pipeline_contract(pipeline_contract_path)
    runtime = _load_structured_runtime(skill_root)
    if not isinstance(context, dict):
        raise DraftExportError("context must contain an object")
    required_context = {
        "meeting_uid",
        "meeting_date",
        "meeting_series",
        "meeting_type",
        "data_version",
        "source_review_status",
        "artifact_review_status",
        "source_md_sha256",
        "generated_at",
    }
    if set(context) != required_context:
        raise DraftExportError("context fields do not match the reviewed contract")
    if context.get("artifact_review_status") != "已审核":
        raise DraftExportError("reviewed generation requires artifact_review_status=已审核")
    meeting_uid = pipeline_contract.validate_meeting_uid(context.get("meeting_uid"))
    if not re.search(r"^## 观点 [0-9]+\s*$", review_markdown, flags=re.M):
        if not review_markdown.lstrip().startswith("# 标的观点审阅表"):
            raise DraftExportError("reviewed Markdown is not a structured viewpoint document")
        rows: list[dict[str, Any]] = []
    else:
        try:
            result = runtime.parse_and_normalize_approved_markdown(
                review_markdown,
                meeting_date=str(context.get("meeting_date") or ""),
                meeting_uid=meeting_uid,
                security_master=runtime.security_master_class.from_csv(runtime.security_master_path),
            )
        except SystemExit as exc:
            raise DraftExportError(str(exc)) from None
        rows = result.rows

    metadata = pipeline_contract.validate_artifact_metadata(
        {
            "schema_version": pipeline_contract.CONTRACT.metadata_schema_version,
            "meeting_uid": context.get("meeting_uid"),
            "meeting_date": context.get("meeting_date"),
            "meeting_series": context.get("meeting_series"),
            "meeting_type": context.get("meeting_type"),
            "artifact_type": "structured_viewpoints",
            "data_version": context.get("data_version"),
            "quality_status": "reviewed",
            "source_review_status": context.get("source_review_status"),
            "artifact_review_status": context.get("artifact_review_status"),
            "source_md_sha256": context.get("source_md_sha256"),
            "review_md_sha256": _sha256_text(review_markdown),
            "item_count": len(rows),
            "generated_at": context.get("generated_at"),
        }
    )
    viewpoint_ids = [str(row.get("viewpoint_id") or "") for row in rows]
    if not all(viewpoint_ids) or len(viewpoint_ids) != len(set(viewpoint_ids)):
        raise DraftExportError("structured reviewed output contains missing or duplicate viewpoint IDs")
    return {"metadata": metadata, "rows": rows}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate a structured draft JSON candidate.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--meeting-markdown")
    source.add_argument("--reviewed-markdown")
    parser.add_argument("--claim-units")
    parser.add_argument("--context", required=True)
    parser.add_argument("--skill-root", required=True)
    parser.add_argument("--pipeline-contract", required=True)
    parser.add_argument("--review-output")
    parser.add_argument("--json-output", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    context = _read_json(Path(args.context), "context")
    if args.reviewed_markdown:
        if args.claim_units or args.review_output:
            raise DraftExportError(
                "--reviewed-markdown cannot be combined with --claim-units or --review-output"
            )
        artifact = generate_reviewed(
            review_markdown=_read_text(Path(args.reviewed_markdown), "reviewed Markdown"),
            context=context,
            skill_root=Path(args.skill_root),
            pipeline_contract_path=Path(args.pipeline_contract),
        )
        review_markdown = ""
    else:
        if not args.claim_units or not args.review_output:
            raise DraftExportError(
                "draft generation requires --claim-units and --review-output"
            )
        review_markdown, artifact = generate_draft(
            meeting_markdown=_read_text(Path(args.meeting_markdown), "meeting Markdown"),
            claim_units=_read_json(Path(args.claim_units), "claim units"),
            context=context,
            skill_root=Path(args.skill_root),
            pipeline_contract_path=Path(args.pipeline_contract),
        )
    json_path = Path(args.json_output)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    if args.review_output:
        review_path = Path(args.review_output)
        review_path.parent.mkdir(parents=True, exist_ok=True)
        review_path.write_text(review_markdown, encoding="utf-8")
    json_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "status": "exported" if args.reviewed_markdown else "generated",
                "row_count": len(artifact["rows"]),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DraftExportError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from None
