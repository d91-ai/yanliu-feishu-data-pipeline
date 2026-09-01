from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import json
from pathlib import Path
import re
import secrets
import unicodedata
from typing import Any, Mapping


CONTRACT_ROOT = Path(__file__).resolve().parent / "contract"
MANIFEST_PATH = CONTRACT_ROOT / "manifest.json"
METADATA_SCHEMA_PATH = CONTRACT_ROOT / "artifact-metadata.schema.json"
BASE_SCHEMA_PATH = CONTRACT_ROOT / "unified-base.schema.json"


class ContractError(ValueError):
    """Raised when pipeline metadata violates the shared contract."""


@dataclass(frozen=True)
class ArtifactSpec:
    display_name: str
    extensions: tuple[str, ...]
    generation_enabled: bool


@dataclass(frozen=True)
class PipelineContract:
    contract_version: int
    metadata_schema_version: int
    meeting_uid_pattern: re.Pattern[str]
    sha256_pattern: re.Pattern[str]
    minimum_data_version: int
    review_statuses: tuple[str, ...]
    quality_statuses: tuple[str, ...]
    upload_required_fields: tuple[str, ...]
    filename_separator: str
    filename_version_prefix: str
    maximum_series_length: int
    maximum_filename_length: int
    artifacts: Mapping[str, ArtifactSpec]
    business_fields: tuple[Mapping[str, Any], ...]


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Invalid {label}: {path}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} must contain an object: {path}")
    return value


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise RuntimeError(f"{label} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{label} must be a positive integer") from exc
    if parsed <= 0:
        raise RuntimeError(f"{label} must be a positive integer")
    return parsed


def _text_tuple(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise RuntimeError(f"{label} must be a non-empty list")
    result = tuple(str(item).strip() for item in value)
    if any(not item for item in result) or len(set(result)) != len(result):
        raise RuntimeError(f"{label} must contain unique non-empty strings")
    return result


def load_contract(path: Path = MANIFEST_PATH) -> PipelineContract:
    manifest = _load_json_object(path, "meeting pipeline manifest")
    filename = manifest.get("filename")
    artifacts_value = manifest.get("artifacts")
    business_fields = manifest.get("business_fields")
    if not isinstance(filename, dict):
        raise RuntimeError("Meeting pipeline manifest missing filename settings")
    if not isinstance(artifacts_value, dict) or not artifacts_value:
        raise RuntimeError("Meeting pipeline manifest missing artifacts")
    if not isinstance(business_fields, list) or len(business_fields) != 21:
        raise RuntimeError("Meeting pipeline manifest must define exactly 21 business fields")

    artifacts: dict[str, ArtifactSpec] = {}
    for artifact_type, raw_spec in artifacts_value.items():
        if not isinstance(raw_spec, dict):
            raise RuntimeError(f"Invalid artifact spec: {artifact_type}")
        display_name = str(raw_spec.get("display_name") or "").strip()
        extensions = _text_tuple(raw_spec.get("extensions"), f"{artifact_type}.extensions")
        enabled = raw_spec.get("generation_enabled")
        if not display_name or not isinstance(enabled, bool):
            raise RuntimeError(f"Invalid artifact spec: {artifact_type}")
        artifacts[str(artifact_type)] = ArtifactSpec(display_name, extensions, enabled)

    try:
        uid_pattern = re.compile(str(manifest["meeting_uid_pattern"]))
        sha_pattern = re.compile(str(manifest["sha256_pattern"]))
    except (KeyError, re.error) as exc:
        raise RuntimeError("Meeting pipeline manifest has invalid identity patterns") from exc

    return PipelineContract(
        contract_version=_positive_int(manifest.get("contract_version"), "contract_version"),
        metadata_schema_version=_positive_int(
            manifest.get("metadata_schema_version"), "metadata_schema_version"
        ),
        meeting_uid_pattern=uid_pattern,
        sha256_pattern=sha_pattern,
        minimum_data_version=_positive_int(
            manifest.get("minimum_data_version"), "minimum_data_version"
        ),
        review_statuses=_text_tuple(manifest.get("review_statuses"), "review_statuses"),
        quality_statuses=_text_tuple(manifest.get("quality_statuses"), "quality_statuses"),
        upload_required_fields=_text_tuple(
            manifest.get("upload_required_fields"), "upload_required_fields"
        ),
        filename_separator=str(filename.get("separator") or ""),
        filename_version_prefix=str(filename.get("version_prefix") or ""),
        maximum_series_length=_positive_int(
            filename.get("maximum_series_length"), "maximum_series_length"
        ),
        maximum_filename_length=_positive_int(
            filename.get("maximum_filename_length"), "maximum_filename_length"
        ),
        artifacts=artifacts,
        business_fields=tuple(business_fields),
    )


CONTRACT = load_contract()
ARTIFACT_TYPES = tuple(CONTRACT.artifacts)
REVIEW_STATUSES = CONTRACT.review_statuses
QUALITY_STATUSES = CONTRACT.quality_statuses


def new_meeting_uid() -> str:
    meeting_uid = f"mtg_{secrets.token_hex(16)}"
    if not CONTRACT.meeting_uid_pattern.fullmatch(meeting_uid):
        raise RuntimeError("Generated meeting UID violates the loaded contract")
    return meeting_uid


def validate_meeting_uid(value: Any) -> str:
    meeting_uid = str(value or "").strip().lower()
    if not CONTRACT.meeting_uid_pattern.fullmatch(meeting_uid):
        raise ContractError("meeting_uid must match mtg_ followed by 32 lowercase hex characters")
    return meeting_uid


def validate_meeting_date(value: Any) -> str:
    text = str(value or "").strip()
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ContractError("meeting_date must use YYYY-MM-DD and be a real date") from exc
    if parsed.isoformat() != text:
        raise ContractError("meeting_date must use canonical YYYY-MM-DD")
    return text


def validate_data_version(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError("data_version must be an integer")
    if value < CONTRACT.minimum_data_version:
        raise ContractError(
            f"data_version must be at least {CONTRACT.minimum_data_version}"
        )
    return value


def validate_metadata_text(value: Any, label: str, maximum_length: int = 40) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = " ".join(text.split())
    if not text:
        raise ContractError(f"{label} is required")
    if len(text) > maximum_length:
        raise ContractError(f"{label} must not exceed {maximum_length} characters")
    if any(ord(character) < 32 for character in text) or "/" in text or "\\" in text:
        raise ContractError(f"{label} contains an unsafe path character")
    return text


def validate_artifact_type(value: Any, *, require_json: bool = False) -> str:
    artifact_type = str(value or "").strip()
    spec = CONTRACT.artifacts.get(artifact_type)
    if spec is None:
        raise ContractError(f"Unsupported artifact_type: {artifact_type}")
    if require_json and "json" not in spec.extensions:
        raise ContractError(f"Artifact type does not produce JSON: {artifact_type}")
    return artifact_type


def build_artifact_filename(
    *,
    meeting_date: Any,
    meeting_series: Any,
    artifact_type: Any,
    data_version: Any,
    extension: str,
) -> str:
    normalized_date = validate_meeting_date(meeting_date)
    normalized_series = validate_metadata_text(
        meeting_series, "meeting_series", CONTRACT.maximum_series_length
    )
    normalized_artifact_type = validate_artifact_type(artifact_type)
    normalized_version = validate_data_version(data_version)
    normalized_extension = str(extension or "").strip().lower().lstrip(".")
    spec = CONTRACT.artifacts[normalized_artifact_type]
    if normalized_extension not in spec.extensions:
        raise ContractError(
            f"Extension {normalized_extension!r} is not valid for {normalized_artifact_type}"
        )
    parts = (
        normalized_date,
        normalized_series,
        spec.display_name,
        f"{CONTRACT.filename_version_prefix}{normalized_version}",
    )
    file_name = CONTRACT.filename_separator.join(parts) + f".{normalized_extension}"
    if len(file_name) > CONTRACT.maximum_filename_length:
        raise ContractError(
            f"Normalized filename must not exceed {CONTRACT.maximum_filename_length} characters"
        )
    return file_name


def _validate_rfc3339(value: Any, label: str) -> str:
    text = str(value or "").strip()
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ContractError(f"{label} must be an RFC 3339 date-time") from exc
    if parsed.tzinfo is None:
        raise ContractError(f"{label} must include a timezone")
    return text


def validate_artifact_metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError("artifact metadata must be an object")
    required = {
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
    missing = sorted(required - set(value))
    extra = sorted(set(value) - required)
    if missing:
        raise ContractError(f"artifact metadata missing fields: {', '.join(missing)}")
    if extra:
        raise ContractError(f"artifact metadata has unknown fields: {', '.join(extra)}")

    schema_version = value.get("schema_version")
    if isinstance(schema_version, bool) or schema_version != CONTRACT.metadata_schema_version:
        raise ContractError(
            f"schema_version must equal {CONTRACT.metadata_schema_version}"
        )
    meeting_uid = validate_meeting_uid(value.get("meeting_uid"))
    meeting_date = validate_meeting_date(value.get("meeting_date"))
    meeting_series = validate_metadata_text(
        value.get("meeting_series"), "meeting_series", CONTRACT.maximum_series_length
    )
    meeting_type = validate_metadata_text(
        value.get("meeting_type"), "meeting_type", CONTRACT.maximum_series_length
    )
    artifact_type = validate_artifact_type(value.get("artifact_type"), require_json=True)
    data_version = validate_data_version(value.get("data_version"))

    quality_status = str(value.get("quality_status") or "").strip()
    source_review_status = str(value.get("source_review_status") or "").strip()
    artifact_review_status = str(value.get("artifact_review_status") or "").strip()
    if quality_status not in CONTRACT.quality_statuses:
        raise ContractError(f"Unsupported quality_status: {quality_status}")
    if source_review_status not in CONTRACT.review_statuses:
        raise ContractError(f"Unsupported source_review_status: {source_review_status}")
    if artifact_review_status not in CONTRACT.review_statuses:
        raise ContractError(f"Unsupported artifact_review_status: {artifact_review_status}")
    if quality_status == "reviewed" and artifact_review_status != "已审核":
        raise ContractError("reviewed JSON requires artifact_review_status=已审核")
    if quality_status == "unreviewed" and artifact_review_status == "已审核":
        raise ContractError("unreviewed JSON cannot claim artifact_review_status=已审核")

    hashes: dict[str, str] = {}
    for field in ("source_md_sha256", "review_md_sha256"):
        hash_value = str(value.get(field) or "").strip()
        if not CONTRACT.sha256_pattern.fullmatch(hash_value):
            raise ContractError(f"{field} must be 64 lowercase hex characters")
        hashes[field] = hash_value

    item_count = value.get("item_count")
    if isinstance(item_count, bool) or not isinstance(item_count, int) or item_count < 0:
        raise ContractError("item_count must be a non-negative integer")
    generated_at = _validate_rfc3339(value.get("generated_at"), "generated_at")

    return {
        "schema_version": schema_version,
        "meeting_uid": meeting_uid,
        "meeting_date": meeting_date,
        "meeting_series": meeting_series,
        "meeting_type": meeting_type,
        "artifact_type": artifact_type,
        "data_version": data_version,
        "quality_status": quality_status,
        "source_review_status": source_review_status,
        "artifact_review_status": artifact_review_status,
        **hashes,
        "item_count": item_count,
        "generated_at": generated_at,
    }


def validate_contract_assets() -> None:
    schema = _load_json_object(METADATA_SCHEMA_PATH, "artifact metadata schema")
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise RuntimeError("Artifact metadata schema missing properties")
    if properties.get("schema_version", {}).get("const") != CONTRACT.metadata_schema_version:
        raise RuntimeError("Metadata schema version differs from manifest")
    if properties.get("quality_status", {}).get("enum") != list(CONTRACT.quality_statuses):
        raise RuntimeError("Metadata quality statuses differ from manifest")
    expected_review_statuses = list(CONTRACT.review_statuses)
    for field in ("source_review_status", "artifact_review_status"):
        if properties.get(field, {}).get("enum") != expected_review_statuses:
            raise RuntimeError(f"Metadata {field} values differ from manifest")
    expected_json_artifacts = [
        artifact_type
        for artifact_type, spec in CONTRACT.artifacts.items()
        if "json" in spec.extensions
    ]
    if properties.get("artifact_type", {}).get("enum") != expected_json_artifacts:
        raise RuntimeError("Metadata artifact types differ from manifest")
    base_schema = _load_json_object(BASE_SCHEMA_PATH, "unified Base schema")
    base_fields = base_schema.get("fields")
    if not isinstance(base_fields, list):
        raise RuntimeError("Unified Base schema missing fields")
    manifest_fields = [field.get("name") for field in CONTRACT.business_fields]
    schema_fields = [field.get("name") for field in base_fields if isinstance(field, dict)]
    if schema_fields != manifest_fields:
        raise RuntimeError("Unified Base fields differ from manifest")
    views = base_schema.get("views")
    if not isinstance(views, list) or [item.get("name") for item in views] != [
        "会议总览",
        "源纪要审核",
        "行业与市场观点审核",
        "标的观点审核",
    ]:
        raise RuntimeError("Unified Base views differ from the contract")
