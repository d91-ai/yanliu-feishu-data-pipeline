from __future__ import annotations

from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import re
from typing import Any
import unicodedata


SKILL_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = SKILL_ROOT / "contract" / "manifest.json"
UID_PATTERN = re.compile(r"^mtg_[0-9a-f]{32}$")
HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
CLAIM_REF_PATTERN = re.compile(r"^c[0-9]{3,}$")
SOURCE_REF_PATTERN = re.compile(r"^L[0-9]{3,}$")
VIEW_SCOPES = ("market", "industry")
VIEW_TYPES = ("看多", "看空", "中性")
REVIEW_STATUSES = ("未审核", "已审核", "需重审")
QUALITY_STATUSES = ("unreviewed", "reviewed")
SECTION_TITLES = {"market": "市场观点", "industry": "行业观点"}
FIELD_LABELS = ("日期", "主题", "发言人", "观点类型", "观点")


class SkillContractError(ValueError):
    """Raised when input or output violates this Skill contract."""


def _json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Invalid {label}: {path}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} must contain an object")
    return value


MANIFEST = _json_object(MANIFEST_PATH, "Skill manifest")
if MANIFEST.get("contract_version") != 3 or MANIFEST.get("schema_version") != 1:
    raise RuntimeError("Unsupported industry/market Skill contract version")


def _clean_text(value: Any, label: str, *, maximum: int = 10000) -> str:
    # Preserve reviewer-authored full-width punctuation. NFC only normalizes
    # equivalent Unicode composition without rewriting human business text.
    text = unicodedata.normalize("NFC", str(value or ""))
    text = " ".join(text.split())
    if not text:
        raise SkillContractError(f"{label} is required")
    if len(text) > maximum:
        raise SkillContractError(f"{label} exceeds {maximum} characters")
    return text


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _validate_timestamp(value: Any) -> str:
    text = str(value or "").strip()
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise SkillContractError("generated_at must be an RFC 3339 date-time") from exc
    if parsed.tzinfo is None:
        raise SkillContractError("generated_at must include a timezone")
    return text


def _validate_meeting_date(value: Any, label: str = "meeting_date") -> str:
    text = str(value or "").strip()
    try:
        parsed_date = date.fromisoformat(text)
    except ValueError as exc:
        raise SkillContractError(f"{label} must use YYYY-MM-DD") from exc
    if parsed_date.isoformat() != text:
        raise SkillContractError(f"{label} must use canonical YYYY-MM-DD")
    return text


def _validate_context(
    value: Any,
    *,
    reviewed: bool,
    source_md_sha256: str | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SkillContractError("context must contain an object")
    required = {
        "meeting_uid",
        "meeting_date",
        "meeting_series",
        "meeting_type",
        "data_version",
        "source_review_status",
        "artifact_review_status",
        "generated_at",
    }
    allowed = required | {"source_md_sha256"}
    missing = sorted(required - set(value))
    extra = sorted(set(value) - allowed)
    if missing:
        raise SkillContractError(f"context missing fields: {', '.join(missing)}")
    if extra:
        raise SkillContractError(f"context has unknown fields: {', '.join(extra)}")

    meeting_uid = str(value.get("meeting_uid") or "").strip().lower()
    if not UID_PATTERN.fullmatch(meeting_uid):
        raise SkillContractError("meeting_uid is invalid")
    meeting_date = _validate_meeting_date(value.get("meeting_date"))
    meeting_series = _clean_text(value.get("meeting_series"), "meeting_series", maximum=40)
    meeting_type = _clean_text(value.get("meeting_type"), "meeting_type", maximum=40)
    data_version = value.get("data_version")
    if isinstance(data_version, bool) or not isinstance(data_version, int) or data_version < 1:
        raise SkillContractError("data_version must be a positive integer")
    source_review_status = str(value.get("source_review_status") or "").strip()
    artifact_review_status = str(value.get("artifact_review_status") or "").strip()
    if source_review_status not in REVIEW_STATUSES:
        raise SkillContractError("source_review_status is invalid")
    if artifact_review_status not in REVIEW_STATUSES:
        raise SkillContractError("artifact_review_status is invalid")
    if reviewed and artifact_review_status != "已审核":
        raise SkillContractError("reviewed export requires artifact_review_status=已审核")
    if not reviewed and artifact_review_status == "已审核":
        raise SkillContractError("draft generation cannot claim artifact_review_status=已审核")

    supplied_hash = str(value.get("source_md_sha256") or "").strip()
    if source_md_sha256 is not None:
        if supplied_hash and supplied_hash != source_md_sha256:
            raise SkillContractError("context source_md_sha256 does not match meeting Markdown")
        supplied_hash = source_md_sha256
    if reviewed and not supplied_hash:
        raise SkillContractError("reviewed export requires source_md_sha256")
    if supplied_hash and not HASH_PATTERN.fullmatch(supplied_hash):
        raise SkillContractError("source_md_sha256 must be lowercase SHA-256")

    return {
        "meeting_uid": meeting_uid,
        "meeting_date": meeting_date,
        "meeting_series": meeting_series,
        "meeting_type": meeting_type,
        "data_version": data_version,
        "source_review_status": source_review_status,
        "artifact_review_status": artifact_review_status,
        "generated_at": _validate_timestamp(value.get("generated_at")),
        "source_md_sha256": supplied_hash,
    }


def source_fragments(markdown: str) -> list[dict[str, str]]:
    if not str(markdown or "").strip():
        raise SkillContractError("meeting Markdown is empty")
    fragments: list[dict[str, str]] = []
    in_frontmatter = False
    frontmatter_closed = False
    for raw_line in markdown.splitlines():
        stripped = raw_line.strip()
        if not fragments and not frontmatter_closed and stripped == "---":
            in_frontmatter = not in_frontmatter
            if not in_frontmatter:
                frontmatter_closed = True
            continue
        if in_frontmatter or not stripped or stripped.startswith("```"):
            continue
        fragments.append({"source_ref": f"L{len(fragments) + 1:03d}", "text": stripped})
    if not fragments:
        raise SkillContractError("meeting Markdown has no usable source fragments")
    return fragments


def _normalize_claim_units(value: Any, fragments: list[dict[str, str]]) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise SkillContractError("claim units must contain an array")
    fragment_by_ref = {item["source_ref"]: item["text"] for item in fragments}
    fragment_order = {item["source_ref"]: index for index, item in enumerate(fragments)}
    normalized: list[dict[str, str]] = []
    claim_refs: set[str] = set()
    signatures: set[tuple[str, str, str, str, str]] = set()
    expected_fields = {
        "claim_ref",
        "source_refs",
        "view_scope",
        "subject",
        "presenter",
        "view_type",
        "viewpoint_text",
    }
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise SkillContractError(f"claim unit {index + 1} must be an object")
        if set(raw) != expected_fields:
            raise SkillContractError(f"claim unit {index + 1} fields do not match the contract")
        claim_ref = str(raw.get("claim_ref") or "").strip()
        if not CLAIM_REF_PATTERN.fullmatch(claim_ref) or claim_ref in claim_refs:
            raise SkillContractError(f"claim unit {index + 1} has invalid or duplicate claim_ref")
        claim_refs.add(claim_ref)
        source_refs = raw.get("source_refs")
        if not isinstance(source_refs, list) or not source_refs:
            raise SkillContractError(f"claim unit {claim_ref} requires source_refs")
        normalized_refs = [str(item or "").strip() for item in source_refs]
        if (
            len(set(normalized_refs)) != len(normalized_refs)
            or any(not SOURCE_REF_PATTERN.fullmatch(item) for item in normalized_refs)
            or any(item not in fragment_by_ref for item in normalized_refs)
        ):
            raise SkillContractError(f"claim unit {claim_ref} has invalid source_refs")
        if normalized_refs != sorted(normalized_refs, key=fragment_order.__getitem__):
            raise SkillContractError(f"claim unit {claim_ref} source_refs are out of source order")

        view_scope = str(raw.get("view_scope") or "").strip()
        if view_scope not in VIEW_SCOPES:
            raise SkillContractError(f"claim unit {claim_ref} has invalid view_scope")
        subject = _clean_text(raw.get("subject"), f"claim unit {claim_ref} subject", maximum=200)
        presenter = _clean_text(
            raw.get("presenter"), f"claim unit {claim_ref} presenter", maximum=200
        )
        view_type = str(raw.get("view_type") or "").strip()
        if view_type not in VIEW_TYPES:
            raise SkillContractError(f"claim unit {claim_ref} has invalid view_type")
        viewpoint_text = _clean_text(
            raw.get("viewpoint_text"),
            f"claim unit {claim_ref} viewpoint_text",
            maximum=300,
        )
        signature = (
            view_scope,
            subject.casefold(),
            presenter.casefold(),
            view_type,
            viewpoint_text.casefold(),
        )
        if signature in signatures:
            raise SkillContractError(f"claim unit {claim_ref} duplicates another viewpoint")
        signatures.add(signature)
        normalized.append(
            {
                "view_scope": view_scope,
                "subject": subject,
                "presenter": presenter,
                "view_type": view_type,
                "viewpoint_text": viewpoint_text,
            }
        )
    return normalized


def _escape_markdown_cell(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\n", "<br>")


def _split_markdown_row(line: str) -> list[str]:
    text = line.strip()
    if not text.startswith("|") or not text.endswith("|"):
        return []
    cells = re.split(r"(?<!\\)\|", text)[1:-1]
    return [cell.strip().replace("\\|", "|").replace("\\\\", "\\").replace("<br>", "\n") for cell in cells]


def render_review_markdown(items: list[dict[str, str]], meeting_date: str) -> str:
    meeting_date = _validate_meeting_date(meeting_date)
    lines = ["# 行业与市场观点", ""]
    for scope in VIEW_SCOPES:
        lines.extend([f"## {SECTION_TITLES[scope]}", ""])
        scoped = [item for item in items if item.get("view_scope") == scope]
        if not scoped:
            lines.extend(["（未抽取到观点）", ""])
            continue
        for number, item in enumerate(scoped, start=1):
            lines.extend(
                [
                    f"### 观点 {number}",
                    "",
                    "| 字段 | 内容 |",
                    "| --- | --- |",
                    f"| 日期 | {_escape_markdown_cell(meeting_date)} |",
                    f"| 主题 | {_escape_markdown_cell(item['subject'])} |",
                    f"| 发言人 | {_escape_markdown_cell(item['presenter'])} |",
                    f"| 观点类型 | {_escape_markdown_cell(item['view_type'])} |",
                    f"| 观点 | {_escape_markdown_cell(item['viewpoint_text'])} |",
                    "",
                ]
            )
    return "\n".join(lines).rstrip() + "\n"


def parse_review_markdown(markdown: str) -> list[dict[str, str]]:
    if not str(markdown or "").strip():
        raise SkillContractError("review Markdown is empty")
    current_scope: str | None = None
    current_fields: dict[str, str] | None = None
    items: list[dict[str, str]] = []

    def finish_card() -> None:
        nonlocal current_fields
        if current_fields is None:
            return
        missing = [field for field in FIELD_LABELS if not current_fields.get(field)]
        if missing:
            raise SkillContractError(f"review card missing fields: {', '.join(missing)}")
        if tuple(current_fields) != FIELD_LABELS:
            raise SkillContractError("review card fields are out of contract order")
        assert current_scope is not None
        view_type = str(current_fields["观点类型"]).strip()
        if view_type not in VIEW_TYPES:
            raise SkillContractError("观点类型 is invalid")
        items.append(
            {
                "meeting_date": _validate_meeting_date(current_fields["日期"], "日期"),
                "view_scope": current_scope,
                "subject": _clean_text(current_fields["主题"], "主题", maximum=200),
                "viewpoint_text": _clean_text(current_fields["观点"], "观点"),
                "presenter": _clean_text(current_fields["发言人"], "发言人", maximum=200),
                "view_type": view_type,
            }
        )
        current_fields = None

    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped == "## 市场观点":
            finish_card()
            current_scope = "market"
            continue
        if stripped == "## 行业观点":
            finish_card()
            current_scope = "industry"
            continue
        if stripped.startswith("## ") and stripped not in ("## 市场观点", "## 行业观点"):
            finish_card()
            current_scope = None
            continue
        if stripped.startswith("### 观点"):
            finish_card()
            if current_scope is None:
                raise SkillContractError("review card appears outside a recognized section")
            current_fields = {}
            continue
        if current_fields is None:
            continue
        cells = _split_markdown_row(stripped)
        if len(cells) != 2 or cells[0] in ("字段", "---"):
            continue
        label, content = cells
        if label not in FIELD_LABELS:
            raise SkillContractError(f"unknown review card field: {label}")
        if label in current_fields:
            raise SkillContractError(f"duplicate review card field: {label}")
        current_fields[label] = content
    finish_card()

    signatures: set[tuple[str, str, str, str, str]] = set()
    for item in items:
        signature = (
            item["view_scope"],
            item["subject"].casefold(),
            item["presenter"].casefold(),
            item["view_type"],
            item["viewpoint_text"].casefold(),
        )
        if signature in signatures:
            raise SkillContractError("review Markdown contains duplicate viewpoint cards")
        signatures.add(signature)
    return items


def _viewpoint_id(meeting_uid: str, item: dict[str, str]) -> str:
    identity = "\x1f".join(
        (
            meeting_uid,
            item["view_scope"],
            item["subject"].casefold(),
            item["presenter"].casefold(),
            item["view_type"],
            item["viewpoint_text"].casefold(),
        )
    )
    return "ivp_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]


def build_artifact(
    *,
    items: list[dict[str, str]],
    context: dict[str, Any],
    review_markdown: str,
    quality_status: str,
    source_md_sha256: str | None = None,
) -> dict[str, Any]:
    if quality_status not in QUALITY_STATUSES:
        raise SkillContractError("quality_status is invalid")
    normalized_context = _validate_context(
        context,
        reviewed=quality_status == "reviewed",
        source_md_sha256=source_md_sha256,
    )
    normalized_items: list[dict[str, str]] = []
    ids: set[str] = set()
    for item in items:
        expected = {
            "meeting_date",
            "view_scope",
            "subject",
            "presenter",
            "view_type",
            "viewpoint_text",
        }
        if set(item) != expected or item.get("view_scope") not in VIEW_SCOPES:
            raise SkillContractError("artifact item fields do not match the contract")
        item_date = _validate_meeting_date(item["meeting_date"])
        if item_date != normalized_context["meeting_date"]:
            raise SkillContractError("artifact item meeting_date does not match context")
        view_type = str(item["view_type"] or "").strip()
        if view_type not in VIEW_TYPES:
            raise SkillContractError("artifact item view_type is invalid")
        normalized = {
            "meeting_date": item_date,
            "view_scope": str(item["view_scope"]),
            "subject": _clean_text(item["subject"], "subject", maximum=200),
            "presenter": _clean_text(item["presenter"], "presenter", maximum=200),
            "view_type": view_type,
            "viewpoint_text": _clean_text(
                item["viewpoint_text"], "viewpoint_text", maximum=300
            ),
        }
        viewpoint_id = _viewpoint_id(normalized_context["meeting_uid"], normalized)
        if viewpoint_id in ids:
            raise SkillContractError("artifact contains duplicate viewpoint IDs")
        ids.add(viewpoint_id)
        normalized_items.append({"viewpoint_id": viewpoint_id, **normalized})

    artifact = {
        "metadata": {
            "schema_version": 1,
            "meeting_uid": normalized_context["meeting_uid"],
            "meeting_date": normalized_context["meeting_date"],
            "meeting_series": normalized_context["meeting_series"],
            "meeting_type": normalized_context["meeting_type"],
            "artifact_type": "industry_market_viewpoints",
            "data_version": normalized_context["data_version"],
            "quality_status": quality_status,
            "source_review_status": normalized_context["source_review_status"],
            "artifact_review_status": normalized_context["artifact_review_status"],
            "source_md_sha256": normalized_context["source_md_sha256"],
            "review_md_sha256": _sha256_text(review_markdown),
            "item_count": len(normalized_items),
            "generated_at": normalized_context["generated_at"],
        },
        "items": normalized_items,
    }
    validate_artifact(artifact)
    return artifact


def generate_draft_artifacts(
    meeting_markdown: str,
    claim_units: Any,
    context: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    source_hash = _sha256_text(meeting_markdown)
    items = _normalize_claim_units(claim_units, source_fragments(meeting_markdown))
    normalized_context = _validate_context(
        context,
        reviewed=False,
        source_md_sha256=source_hash,
    )
    dated_items = [
        {"meeting_date": normalized_context["meeting_date"], **item}
        for item in items
    ]
    review_markdown = render_review_markdown(items, normalized_context["meeting_date"])
    artifact = build_artifact(
        items=dated_items,
        context=context,
        review_markdown=review_markdown,
        quality_status="unreviewed",
        source_md_sha256=source_hash,
    )
    return review_markdown, artifact


def export_reviewed_artifact(
    review_markdown: str,
    context: dict[str, Any],
) -> dict[str, Any]:
    return build_artifact(
        items=parse_review_markdown(review_markdown),
        context=context,
        review_markdown=review_markdown,
        quality_status="reviewed",
    )


def validate_artifact(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"metadata", "items"}:
        raise SkillContractError("artifact must contain metadata and items")
    metadata = value.get("metadata")
    items = value.get("items")
    if not isinstance(metadata, dict) or not isinstance(items, list):
        raise SkillContractError("artifact metadata/items have invalid types")
    required_metadata = {
        "schema_version",
        "meeting_uid",
        "meeting_date",
        "meeting_series",
        "meeting_type",
        "artifact_type",
        "data_version",
        "quality_status",
        "source_review_status",
        "artifact_review_status",
        "source_md_sha256",
        "review_md_sha256",
        "item_count",
        "generated_at",
    }
    if set(metadata) != required_metadata:
        raise SkillContractError("artifact metadata fields do not match the contract")
    if metadata.get("schema_version") != 1:
        raise SkillContractError("artifact schema_version is invalid")
    if metadata.get("artifact_type") != "industry_market_viewpoints":
        raise SkillContractError("artifact_type is invalid")
    quality_status = str(metadata.get("quality_status") or "")
    context = {
        key: metadata[key]
        for key in (
            "meeting_uid",
            "meeting_date",
            "meeting_series",
            "meeting_type",
            "data_version",
            "source_review_status",
            "artifact_review_status",
            "generated_at",
            "source_md_sha256",
        )
    }
    _validate_context(context, reviewed=quality_status == "reviewed")
    if quality_status not in QUALITY_STATUSES:
        raise SkillContractError("quality_status is invalid")
    if not HASH_PATTERN.fullmatch(str(metadata.get("review_md_sha256") or "")):
        raise SkillContractError("review_md_sha256 is invalid")
    if metadata.get("item_count") != len(items):
        raise SkillContractError("item_count does not match items")
    ids: set[str] = set()
    for item in items:
        if not isinstance(item, dict) or set(item) != {
            "viewpoint_id",
            "meeting_date",
            "view_scope",
            "subject",
            "presenter",
            "view_type",
            "viewpoint_text",
        }:
            raise SkillContractError("artifact item fields do not match the contract")
        viewpoint_id = str(item.get("viewpoint_id") or "")
        if not re.fullmatch(r"ivp_[0-9a-f]{32}", viewpoint_id) or viewpoint_id in ids:
            raise SkillContractError("artifact viewpoint_id is invalid or duplicate")
        ids.add(viewpoint_id)
        if item.get("view_scope") not in VIEW_SCOPES:
            raise SkillContractError("artifact view_scope is invalid")
        if _validate_meeting_date(item.get("meeting_date")) != metadata["meeting_date"]:
            raise SkillContractError("artifact item meeting_date does not match metadata")
        if item.get("view_type") not in VIEW_TYPES:
            raise SkillContractError("artifact view_type is invalid")
        for field in ("subject", "presenter", "viewpoint_text"):
            _clean_text(item.get(field), field)
    return value
