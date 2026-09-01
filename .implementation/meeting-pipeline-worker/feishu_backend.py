#!/usr/bin/env python3
"""Disabled-by-default Feishu backend candidate for the unified worker."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
from types import SimpleNamespace
from typing import Any, Iterator, Mapping
import urllib.parse


MODULE_ROOT = Path(__file__).resolve().parent
REPOSITORY_ROOT = MODULE_ROOT.parents[1]
if str(MODULE_ROOT) not in sys.path:
    sys.path.insert(0, str(MODULE_ROOT))

from unified_pipeline_worker import (  # noqa: E402
    GENERATION_ARTIFACT_TYPES,
    GeneratedArtifact,
    PipelineJobError,
    StaleJob,
    atomic_private_json,
    ensure_private_directory,
    sha256_bytes,
    utc_now,
)


FIELD_URLS = {
    "meeting_minutes": {
        "current_md": "会议纪要MD",
        "baseline_md": "会议纪要审核前MD",
        "reviewed_md": "会议纪要审核后MD",
        "review": "源纪要审核",
    },
    "industry_market_viewpoints": {
        "current_md": "行业与市场观点MD",
        "baseline_md": "行业与市场观点审核前MD",
        "reviewed_md": "行业与市场观点审核后MD",
        "json": "行业与市场观点JSON",
        "review": "行业与市场观点审核",
    },
    "structured_viewpoints": {
        "current_md": "标的观点MD",
        "baseline_md": "标的观点审核前MD",
        "reviewed_md": "标的观点审核后MD",
        "json": "标的观点JSON",
        "review": "标的观点审核",
    },
}


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_runtime_modules(
    service_root: Path | None = None,
    contract_path: Path | None = None,
):
    service_root = service_root or (
        REPOSITORY_ROOT
        / ".implementation"
        / "version-retention"
        / "feishu-structured-generate"
    )
    contract_path = contract_path or (
        REPOSITORY_ROOT
        / ".implementation"
        / "meeting-pipeline-contract"
        / "meeting_pipeline_contract.py"
    )
    if str(service_root) not in sys.path:
        sys.path.insert(0, str(service_root))
    service = _load_module(
        "unified_worker_structured_service",
        service_root / "structured_generate_service.py",
    )
    contract = _load_module(
        "unified_worker_pipeline_contract",
        contract_path,
    )
    return service, contract


@dataclass(frozen=True)
class FeishuBackendConfig:
    app_id: str
    app_secret: str
    base_token: str
    table_id: str
    source_current_parent: str
    industry_md_parent: str
    industry_json_parent: str
    structured_md_parent: str
    structured_json_parent: str
    baseline_parent: str
    reviewed_parent: str
    history_parent: str
    generation_job_root: Path
    registry_path: Path
    lock_root: Path
    folder_registry_path: Path
    output_dir: Path
    output_owner_open_id: str = ""
    openapi_base: str = "https://open.feishu.cn/open-apis"
    user_id_type: str = "open_id"
    unified_enabled: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "FeishuBackendConfig":
        values = dict(os.environ if env is None else env)
        required = {
            "FEISHU_APP_ID": "app_id",
            "FEISHU_APP_SECRET": "app_secret",
            "FEISHU_MEETING_BASE_APP_TOKEN": "base_token",
            "FEISHU_MEETING_BASE_TABLE_ID": "table_id",
            "FEISHU_PARENT_FOLDER_TOKEN": "source_current_parent",
            "FEISHU_PIPELINE_INDUSTRY_MD_FOLDER_TOKEN": "industry_md_parent",
            "FEISHU_PIPELINE_INDUSTRY_JSON_FOLDER_TOKEN": "industry_json_parent",
            "FEISHU_PIPELINE_STRUCTURED_MD_FOLDER_TOKEN": "structured_md_parent",
            "FEISHU_PIPELINE_STRUCTURED_JSON_FOLDER_TOKEN": "structured_json_parent",
            "FEISHU_PIPELINE_BASELINE_FOLDER_TOKEN": "baseline_parent",
            "FEISHU_PIPELINE_REVIEWED_FOLDER_TOKEN": "reviewed_parent",
            "FEISHU_PIPELINE_HISTORY_FOLDER_TOKEN": "history_parent",
        }
        missing = [name for name in required if not str(values.get(name) or "").strip()]
        if missing:
            raise PipelineJobError("feishu_backend_config_missing", ",".join(missing))
        root = MODULE_ROOT

        def path_value(name: str, default: str) -> Path:
            value = Path(str(values.get(name) or default)).expanduser()
            return value if value.is_absolute() else root / value

        kwargs = {field: str(values[name]).strip() for name, field in required.items()}
        return cls(
            **kwargs,
            generation_job_root=path_value(
                "FEISHU_GENERATION_JOB_SPOOL_PATH", "data/meeting-generation-jobs"
            ),
            registry_path=path_value(
                "FEISHU_PIPELINE_ARTIFACT_REGISTRY_PATH", "data/artifact-registry.json"
            ),
            lock_root=path_value("FEISHU_PIPELINE_RECORD_LOCK_DIR", "data/record-locks"),
            folder_registry_path=path_value(
                "FEISHU_PIPELINE_FOLDER_REGISTRY_PATH", "data/folder-registry.json"
            ),
            output_dir=path_value("FEISHU_PIPELINE_OUTPUT_DIR", "data/outputs"),
            output_owner_open_id=str(values.get("FEISHU_OUTPUT_OWNER_OPEN_ID") or "").strip(),
            openapi_base=str(
                values.get("FEISHU_OPENAPI_BASE") or "https://open.feishu.cn/open-apis"
            ).rstrip("/"),
            user_id_type=str(values.get("FEISHU_USER_ID_TYPE") or "open_id").strip(),
            unified_enabled=str(values.get("FEISHU_UNIFIED_PIPELINE_ENABLED") or "").lower()
            in {"1", "true", "yes"},
        )


class FeishuPipelineBackend:
    def __init__(
        self,
        config: FeishuBackendConfig,
        *,
        apply: bool = False,
        service: Any | None = None,
        contract: Any | None = None,
    ):
        self.config = config
        if service is None or contract is None:
            loaded_service, loaded_contract = load_runtime_modules()
            service = service or loaded_service
            contract = contract or loaded_contract
        self.service = service
        self.contract = contract
        self.apply = bool(apply)
        self.api_cfg = SimpleNamespace(
            app_id=config.app_id,
            app_secret=config.app_secret,
            source_base_token=config.base_token,
            source_table_id=config.table_id,
            openapi_base=config.openapi_base,
            user_id_type=config.user_id_type,
            output_owner_open_id=config.output_owner_open_id,
            folder_registry_path=config.folder_registry_path,
            output_dir=config.output_dir,
            semantic_job_dir=config.lock_root,
        )

    def _require_apply(self) -> None:
        if not self.apply or not self.config.unified_enabled:
            raise PipelineJobError("production_backend_disabled")

    @contextmanager
    def record_lock(self, record_id: str) -> Iterator[None]:
        ensure_private_directory(self.config.lock_root)
        digest = hashlib.sha256(record_id.encode("utf-8")).hexdigest()
        path = self.config.lock_root / f"record-{digest}.lock"
        with path.open("a+") as handle:
            os.chmod(path, 0o600)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _url(self, value: Any) -> str:
        return self.service.url_from_field_value(value)

    def _file_token(self, value: Any) -> str:
        url = self._url(value)
        if not url:
            return ""
        try:
            token, file_type = self.service.parse_drive_url(url)
        except Exception as exc:
            raise PipelineJobError("drive_url_invalid") from exc
        if file_type != "file":
            raise PipelineJobError("drive_file_type_invalid")
        return token

    def _date(self, value: Any) -> str:
        milliseconds = self.service.ms_from_record_time(value)
        if milliseconds is not None:
            return self.service.date_text_from_ms(milliseconds)
        text = self.service.plain_field_value(value).strip()
        try:
            return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
        except ValueError as exc:
            raise PipelineJobError("meeting_date_invalid") from exc

    def get_record(self, record_id: str) -> dict[str, Any]:
        raw = self.service.get_bitable_record_from(
            self.api_cfg, self.config.base_token, self.config.table_id, record_id
        )
        fields = raw.get("fields") or {}
        if not isinstance(fields, dict):
            raise PipelineJobError("base_record_fields_invalid")
        source_token = self._file_token(fields.get("会议纪要MD"))
        if not source_token:
            raise PipelineJobError("source_file_token_missing")
        source_content = self.service.download_drive_file(self.api_cfg, source_token)
        version = self.service.number_field_value(fields.get("数据版本"))
        if version is None or version < 1:
            raise PipelineJobError("data_version_invalid")
        review_tokens = {
            artifact_type: self._file_token(fields.get(spec["current_md"]))
            for artifact_type, spec in FIELD_URLS.items()
        }
        current_file_tokens = {
            "meeting_minutes": {"md": source_token, "json": ""},
            **{
                artifact_type: {
                    "md": review_tokens[artifact_type],
                    "json": self._file_token(fields.get(spec.get("json", ""))),
                }
                for artifact_type, spec in FIELD_URLS.items()
                if artifact_type != "meeting_minutes"
            },
        }
        return {
            "record_id": record_id,
            "meeting_uid": self.service.plain_field_value(fields.get("会议ID")).strip().lower(),
            "meeting_date": self._date(fields.get("会议日期")),
            "meeting_series": self.service.plain_field_value(fields.get("会议系列")).strip(),
            "meeting_type": self.service.plain_field_value(fields.get("会议类型")).strip(),
            "data_version": version,
            "source_file_token": source_token,
            "source_md_sha256": sha256_bytes(source_content),
            "source_review_status": self.service.plain_field_value(
                fields.get("源纪要审核")
            ).strip(),
            "review_file_tokens": review_tokens,
            "current_file_tokens": current_file_tokens,
            "artifact_review_statuses": {
                artifact_type: self.service.plain_field_value(fields.get(spec["review"])).strip()
                for artifact_type, spec in FIELD_URLS.items()
                if artifact_type != "meeting_minutes"
            },
            "raw_fields": fields,
        }

    def download_file(self, file_token: str) -> bytes:
        return self.service.download_drive_file(self.api_cfg, file_token)

    def _registry_lock_path(self) -> Path:
        return self.config.registry_path.with_suffix(self.config.registry_path.suffix + ".lock")

    @contextmanager
    def _registry_lock(self) -> Iterator[None]:
        path = self._registry_lock_path()
        ensure_private_directory(path.parent)
        with path.open("a+") as handle:
            os.chmod(path, 0o600)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _load_registry_unlocked(self) -> dict[str, Any]:
        path = self.config.registry_path
        if not path.exists():
            return {"version": 1, "review_receipts": {}, "artifacts": {}}
        if path.is_symlink() or not path.is_file():
            raise PipelineJobError("artifact_registry_unsafe")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PipelineJobError("artifact_registry_invalid") from exc
        if not isinstance(value, dict) or value.get("version") != 1:
            raise PipelineJobError("artifact_registry_invalid")
        value.setdefault("review_receipts", {})
        value.setdefault("artifacts", {})
        return value

    def _save_review_receipt_only(
        self, *, meeting_uid: str, artifact_type: str, review_md_sha256: str, data_version: int
    ) -> dict[str, Any]:
        receipt = {
            "meeting_uid": meeting_uid,
            "artifact_type": artifact_type,
            "data_version": data_version,
            "updated_at": utc_now(),
        }
        with self._registry_lock():
            registry = self._load_registry_unlocked()
            registry["review_receipts"][
                f"{meeting_uid}:{artifact_type}:{review_md_sha256}"
            ] = receipt
            atomic_private_json(self.config.registry_path, registry)
        return receipt

    def review_receipt(self, job: dict[str, Any]) -> dict[str, Any] | None:
        meeting_uid = str(job["meeting_uid"])
        artifact_type = str(job["artifact_type"])
        review_md_sha256 = str(job["review_md_sha256"])
        key = f"{meeting_uid}:{artifact_type}:{review_md_sha256}"
        with self._registry_lock():
            value = self._load_registry_unlocked().get("review_receipts", {}).get(key)
        if isinstance(value, dict):
            return dict(value)

        # Recover the terminal state if Base committed but the local receipt
        # write or HTTP response was lost.  A hash match on the immutable
        # reviewed file is required; status/version alone are insufficient.
        record = self.get_record(str(job["record_id"]))
        if (
            record["meeting_uid"] != meeting_uid
            or record["data_version"] != int(job["data_version"]) + 1
        ):
            return None
        spec = FIELD_URLS[artifact_type]
        fields = record["raw_fields"]
        if self.service.plain_field_value(fields.get(spec["review"])).strip() != "已审核":
            return None
        reviewed_token = self._file_token(fields.get(spec["reviewed_md"]))
        if not reviewed_token:
            return None
        if sha256_bytes(self.download_file(reviewed_token)) != review_md_sha256:
            return None
        return self._save_review_receipt_only(
            meeting_uid=meeting_uid,
            artifact_type=artifact_type,
            review_md_sha256=review_md_sha256,
            data_version=record["data_version"],
        )

    def _save_registry_entry(
        self,
        *,
        meeting_uid: str,
        artifact_type: str,
        data_version: int,
        links: dict[str, Any],
        review_md_sha256: str = "",
    ) -> None:
        with self._registry_lock():
            registry = self._load_registry_unlocked()
            artifact_key = f"{meeting_uid}:{artifact_type}"
            registry["artifacts"][artifact_key] = {
                "meeting_uid": meeting_uid,
                "artifact_type": artifact_type,
                "data_version": data_version,
                "links": links,
                "updated_at": utc_now(),
            }
            if review_md_sha256:
                registry["review_receipts"][
                    f"{meeting_uid}:{artifact_type}:{review_md_sha256}"
                ] = {
                    "meeting_uid": meeting_uid,
                    "artifact_type": artifact_type,
                    "data_version": data_version,
                    "updated_at": utc_now(),
                }
            atomic_private_json(self.config.registry_path, registry)

    def _month_folder(self, parent: str, meeting_date: str, artifact_type: str = "") -> str:
        current = parent
        if artifact_type:
            display = self.contract.CONTRACT.artifacts[artifact_type].display_name
            current = self.service.ensure_child_folder(self.api_cfg, current, display)
        return self.service.ensure_child_folder(self.api_cfg, current, meeting_date[:7])

    def _parent_for(self, artifact_type: str, extension: str) -> str:
        if artifact_type == "meeting_minutes":
            return self.config.source_current_parent
        if artifact_type == "industry_market_viewpoints":
            return (
                self.config.industry_md_parent
                if extension == "md"
                else self.config.industry_json_parent
            )
        return (
            self.config.structured_md_parent
            if extension == "md"
            else self.config.structured_json_parent
        )

    def _history_folder(self, artifact_type: str, meeting_date: str) -> str:
        display = self.contract.CONTRACT.artifacts[artifact_type].display_name
        category = self.service.ensure_child_folder(
            self.api_cfg, self.config.history_parent, display
        )
        month = self.service.ensure_child_folder(self.api_cfg, category, meeting_date[:7])
        return self.service.ensure_child_folder(self.api_cfg, month, "历史")

    def _file_is_in_folder(self, folder_token: str, file_token: str) -> bool:
        return any(
            str(item.get("token") or item.get("file_token") or "") == file_token
            for item in self.service.list_drive_folder_items(self.api_cfg, folder_token)
        )

    def _move_to_history(
        self, file_token: str, artifact_type: str, meeting_date: str
    ) -> dict[str, str]:
        target = self._history_folder(artifact_type, meeting_date)
        if self._file_is_in_folder(target, file_token):
            return {"file_token": file_token, "status": "already_archived"}
        try:
            self.service.request_json(
                self.api_cfg,
                "POST",
                f"/drive/v1/files/{urllib.parse.quote(file_token, safe='')}/move",
                token=self.service.get_tenant_access_token(self.api_cfg),
                body={"folder_token": target, "type": "file"},
            )
        except Exception:
            if not self._file_is_in_folder(target, file_token):
                raise
        if not self._file_is_in_folder(target, file_token):
            raise PipelineJobError("drive_move_unconfirmed")
        return {"file_token": file_token, "status": "archived"}

    def _cleanup_old_files(
        self,
        *,
        old_tokens: dict[str, str],
        new_tokens: dict[str, str],
        artifact_type: str,
        meeting_date: str,
    ) -> dict[str, Any]:
        archived: list[dict[str, str]] = []
        pending: list[dict[str, str]] = []
        seen: set[str] = set()
        for extension, token in old_tokens.items():
            token = str(token or "")
            if (
                not token
                or token in seen
                or token == str(new_tokens.get(extension) or "")
            ):
                continue
            seen.add(token)
            try:
                archived.append(
                    self._move_to_history(token, artifact_type, meeting_date)
                )
            except Exception as exc:
                pending.append(
                    {
                        "file_token": token,
                        "extension": extension,
                        "error_code": getattr(exc, "error_code", "")
                        or getattr(exc, "code", "")
                        or exc.__class__.__name__,
                    }
                )
        return {
            "status": "cleanup_pending" if pending else "cleanup_complete",
            "archived": archived,
            "pending": pending,
        }

    def _find_exact_files(self, folder_token: str, file_name: str) -> list[dict[str, Any]]:
        return [
            item
            for item in self.service.list_drive_folder_items(self.api_cfg, folder_token)
            if item.get("type") == "file" and item.get("name") == file_name
        ]

    def _match_exact_file(
        self, folder_token: str, file_name: str, content_sha256: str
    ) -> tuple[str, str] | None:
        matches = self._find_exact_files(folder_token, file_name)
        if len(matches) > 1:
            raise PipelineJobError("drive_exact_name_ambiguous")
        if not matches:
            return None
        item = matches[0]
        token = str(item.get("token") or item.get("file_token") or "")
        if not token or sha256_bytes(self.download_file(token)) != content_sha256:
            raise PipelineJobError("drive_exact_name_hash_conflict")
        url = str(item.get("url") or "")
        if not url:
            url = self.service.resolve_uploaded_file_url(
                self.api_cfg, folder_token, token, file_name
            )
        return token, url

    def _publish(
        self,
        *,
        parent: str,
        meeting_date: str,
        meeting_series: str,
        meeting_uid: str,
        artifact_type: str,
        data_version: int,
        extension: str,
        content: bytes,
        category: str = "",
    ) -> dict[str, Any]:
        folder = self._month_folder(parent, meeting_date, category)
        # Date, series and version are not a unique meeting identity.  Build the
        # contract name first so the complete human-readable series is kept,
        # then add the anonymous pipeline UID as its own filename component.
        file_name = self.contract.build_artifact_filename(
            meeting_date=meeting_date,
            meeting_series=meeting_series,
            artifact_type=artifact_type,
            data_version=data_version,
            extension=extension,
        )
        contract_settings = getattr(self.contract, "CONTRACT", None)
        separator = str(getattr(contract_settings, "filename_separator", " - "))
        series_prefix = separator.join((meeting_date, meeting_series))
        unique_prefix = separator.join((series_prefix, meeting_uid))
        if not file_name.startswith(series_prefix + separator):
            raise PipelineJobError("artifact_filename_contract_mismatch")
        file_name = unique_prefix + file_name[len(series_prefix) :]
        maximum_filename_length = int(
            getattr(contract_settings, "maximum_filename_length", 120)
        )
        if len(file_name) > maximum_filename_length:
            raise PipelineJobError("artifact_filename_too_long_with_uid")
        content_sha256 = sha256_bytes(content)
        existing = self._match_exact_file(folder, file_name, content_sha256)
        if existing is None:
            try:
                token = self.service.upload_drive_file(
                    self.api_cfg,
                    folder,
                    file_name,
                    content,
                    content_type=(
                        "text/markdown" if extension == "md" else "application/json"
                    ),
                )
            except Exception as exc:
                if getattr(exc, "error_code", "") == "owner_transfer_failed":
                    raise
                reconciled = self._match_exact_file(folder, file_name, content_sha256)
                if reconciled is None:
                    raise
                token, url = reconciled
                self.service.transfer_output_owner(self.api_cfg, token)
            else:
                url = self.service.resolve_uploaded_file_url(
                    self.api_cfg, folder, token, file_name
                )
            existing = (token, url)
        return {
            "file_token": existing[0],
            "url": existing[1],
            "file_name": file_name,
            "sha256": content_sha256,
        }

    def _url_cell(self, field_map: dict[str, dict[str, Any]], name: str, value: dict[str, Any]):
        return self.service.url_cell_value(field_map, name, value["url"], value["file_name"])

    def _update_and_confirm(
        self,
        record_id: str,
        fields: dict[str, Any],
        expected: dict[str, str],
        expected_version: int,
    ) -> dict[str, Any]:
        try:
            self.service.update_bitable_record_in(
                self.api_cfg,
                self.config.base_token,
                self.config.table_id,
                record_id,
                fields,
            )
        except Exception:
            pass
        record = self.service.get_bitable_record_from(
            self.api_cfg, self.config.base_token, self.config.table_id, record_id
        )
        current = record.get("fields") or {}
        version = self.service.number_field_value(current.get("数据版本"))
        if version != expected_version:
            raise PipelineJobError("base_commit_unconfirmed")
        for name, value in expected.items():
            actual = (
                self._url(current.get(name))
                if name.endswith("MD") or name.endswith("JSON")
                else self.service.plain_field_value(current.get(name)).strip()
            )
            if actual != value:
                raise PipelineJobError("base_commit_unconfirmed", name)
        return {"record_id": record_id, "data_version": version}

    def _validate_artifact(self, job: dict[str, Any], artifact: GeneratedArtifact) -> dict[str, Any]:
        if artifact.json_artifact is None:
            raise PipelineJobError("artifact_json_missing")
        metadata = self.contract.validate_artifact_metadata(
            artifact.json_artifact.get("metadata")
        )
        if (
            metadata["meeting_uid"] != job["meeting_uid"]
            or metadata["artifact_type"] != job["artifact_type"]
        ):
            raise PipelineJobError("artifact_identity_mismatch")
        return metadata

    def _validate_structured_json(
        self,
        job: dict[str, Any],
        artifact: GeneratedArtifact,
        review_bytes: bytes,
    ) -> dict[str, Any]:
        value = artifact.json_artifact
        if not isinstance(value, dict) or set(value) != {"metadata", "rows"}:
            raise PipelineJobError("artifact_json_invalid")
        metadata = value.get("metadata")
        rows = value.get("rows")
        required = {
            "meeting_id",
            "structured_markdown_sha256",
            "schema_version",
            "security_master_version",
        }
        if not isinstance(metadata, dict) or set(metadata) != required or not isinstance(rows, list):
            raise PipelineJobError("artifact_json_invalid")
        if (
            metadata.get("meeting_id") != job["meeting_uid"]
            or metadata.get("schema_version") != 9
            or metadata.get("structured_markdown_sha256") != sha256_bytes(review_bytes)
        ):
            raise PipelineJobError("artifact_identity_mismatch")
        return metadata

    def commit_generation(
        self,
        job: dict[str, Any],
        artifact: GeneratedArtifact,
        *,
        expected_version: int,
    ) -> dict[str, Any]:
        self._require_apply()
        review_bytes = str(artifact.review_markdown or "").encode("utf-8")
        record = self.get_record(job["record_id"])
        structured = job["artifact_type"] == "structured_viewpoints"
        if structured:
            self._validate_structured_json(job, artifact, review_bytes)
            previous_status = record["artifact_review_statuses"].get(job["artifact_type"]) or "未审核"
            review_status = "需重审" if previous_status == "已审核" else "未审核"
        else:
            metadata = self._validate_artifact(job, artifact)
            if metadata["data_version"] != expected_version or metadata["quality_status"] != "unreviewed":
                raise PipelineJobError("artifact_version_or_quality_mismatch")
            if sha256_bytes(review_bytes) != metadata["review_md_sha256"]:
                raise PipelineJobError("artifact_review_hash_mismatch")
            review_status = metadata["artifact_review_status"]
        if record["data_version"] != expected_version or record["source_md_sha256"] != job["input_md_sha256"]:
            raise StaleJob("generation_commit_stale")
        current = self._publish(
            parent=self._parent_for(job["artifact_type"], "md"),
            meeting_date=record["meeting_date"],
            meeting_series=record["meeting_series"],
            meeting_uid=job["meeting_uid"],
            artifact_type=job["artifact_type"],
            data_version=expected_version,
            extension="md",
            content=review_bytes,
        )
        baseline = self._publish(
            parent=self.config.baseline_parent,
            meeting_date=record["meeting_date"],
            meeting_series=record["meeting_series"],
            meeting_uid=job["meeting_uid"],
            artifact_type=job["artifact_type"],
            data_version=expected_version,
            extension="md",
            content=review_bytes,
            category=job["artifact_type"],
        )
        json_bytes = (
            json.dumps(artifact.json_artifact, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        json_file = self._publish(
            parent=self._parent_for(job["artifact_type"], "json"),
            meeting_date=record["meeting_date"],
            meeting_series=record["meeting_series"],
            meeting_uid=job["meeting_uid"],
            artifact_type=job["artifact_type"],
            data_version=expected_version,
            extension="json",
            content=json_bytes,
        )
        fresh = self.get_record(job["record_id"])
        if fresh["data_version"] != expected_version or fresh["source_md_sha256"] != job["input_md_sha256"]:
            raise StaleJob("generation_publish_stale")
        spec = FIELD_URLS[job["artifact_type"]]
        field_defs = self.service.fields_by_name(
            self.service.list_bitable_fields(
                self.api_cfg, self.config.base_token, self.config.table_id
            )
        )
        fields = {
            spec["current_md"]: self._url_cell(field_defs, spec["current_md"], current),
            spec["baseline_md"]: self._url_cell(field_defs, spec["baseline_md"], baseline),
            spec["review"]: review_status,
        }
        fields[spec["json"]] = self._url_cell(field_defs, spec["json"], json_file)
        expected_fields = {
            spec["current_md"]: current["url"],
            spec["baseline_md"]: baseline["url"],
            spec["json"]: json_file["url"],
            spec["review"]: review_status,
        }
        result = self._update_and_confirm(
            job["record_id"],
            fields,
            expected_fields,
            expected_version,
        )
        links = {"current_md": current, "baseline_md": baseline, "json": json_file}
        cleanup = self._cleanup_old_files(
            old_tokens=record["current_file_tokens"].get(job["artifact_type"], {}),
            new_tokens={
                "md": current["file_token"],
                "json": json_file["file_token"],
            },
            artifact_type=job["artifact_type"],
            meeting_date=record["meeting_date"],
        )
        links["cleanup"] = cleanup
        self._save_registry_entry(
            meeting_uid=job["meeting_uid"],
            artifact_type=job["artifact_type"],
            data_version=expected_version,
            links=links,
        )
        return {**result, "links": links, "status": cleanup["status"]}

    def commit_review(
        self,
        job: dict[str, Any],
        artifact: GeneratedArtifact,
        *,
        expected_version: int,
    ) -> dict[str, Any]:
        self._require_apply()
        new_version = expected_version + 1
        review_bytes = self.download_file(job["review_file_token"])
        if sha256_bytes(review_bytes) != job["review_md_sha256"]:
            raise StaleJob("review_content_stale")
        if job["artifact_type"] == "structured_viewpoints":
            self._validate_structured_json(job, artifact, review_bytes)
        else:
            metadata = self._validate_artifact(job, artifact)
            if metadata["data_version"] != new_version or metadata["quality_status"] != "reviewed":
                raise PipelineJobError("artifact_version_or_quality_mismatch")
        json_bytes = (json.dumps(artifact.json_artifact, ensure_ascii=False, indent=2) + "\n").encode(
            "utf-8"
        )
        record = self.get_record(job["record_id"])
        if record["data_version"] != expected_version:
            raise StaleJob("review_commit_stale")
        current = self._publish(
            parent=self._parent_for(job["artifact_type"], "md"),
            meeting_date=record["meeting_date"],
            meeting_series=record["meeting_series"],
            meeting_uid=job["meeting_uid"],
            artifact_type=job["artifact_type"],
            data_version=new_version,
            extension="md",
            content=review_bytes,
        )
        reviewed = self._publish(
            parent=self.config.reviewed_parent,
            meeting_date=record["meeting_date"],
            meeting_series=record["meeting_series"],
            meeting_uid=job["meeting_uid"],
            artifact_type=job["artifact_type"],
            data_version=new_version,
            extension="md",
            content=review_bytes,
            category=job["artifact_type"],
        )
        json_file = self._publish(
            parent=self._parent_for(job["artifact_type"], "json"),
            meeting_date=record["meeting_date"],
            meeting_series=record["meeting_series"],
            meeting_uid=job["meeting_uid"],
            artifact_type=job["artifact_type"],
            data_version=new_version,
            extension="json",
            content=json_bytes,
        )
        fresh = self.get_record(job["record_id"])
        if fresh["data_version"] != expected_version:
            raise StaleJob("review_publish_stale")
        spec = FIELD_URLS[job["artifact_type"]]
        field_defs = self.service.fields_by_name(
            self.service.list_bitable_fields(
                self.api_cfg, self.config.base_token, self.config.table_id
            )
        )
        fields = {
            "数据版本": new_version,
            spec["current_md"]: self._url_cell(field_defs, spec["current_md"], current),
            spec["reviewed_md"]: self._url_cell(field_defs, spec["reviewed_md"], reviewed),
            spec["json"]: self._url_cell(field_defs, spec["json"], json_file),
            spec["review"]: "已审核",
        }
        result = self._update_and_confirm(
            job["record_id"],
            fields,
            {
                spec["current_md"]: current["url"],
                spec["reviewed_md"]: reviewed["url"],
                spec["json"]: json_file["url"],
                spec["review"]: "已审核",
            },
            new_version,
        )
        links = {"current_md": current, "reviewed_md": reviewed, "json": json_file}
        cleanup = self._cleanup_old_files(
            old_tokens=record["current_file_tokens"].get(job["artifact_type"], {}),
            new_tokens={"md": current["file_token"], "json": json_file["file_token"]},
            artifact_type=job["artifact_type"],
            meeting_date=record["meeting_date"],
        )
        links["cleanup"] = cleanup
        self._save_registry_entry(
            meeting_uid=job["meeting_uid"],
            artifact_type=job["artifact_type"],
            data_version=new_version,
            links=links,
            review_md_sha256=job["review_md_sha256"],
        )
        return {
            **result,
            **record,
            "data_version": new_version,
            "links": links,
            "status": cleanup["status"],
        }

    def commit_source_review(
        self,
        job: dict[str, Any],
        source_content: bytes,
        *,
        expected_version: int,
    ) -> dict[str, Any]:
        self._require_apply()
        new_version = expected_version + 1
        record = self.get_record(job["record_id"])
        if record["data_version"] != expected_version:
            raise StaleJob("source_review_commit_stale")
        current = self._publish(
            parent=self.config.source_current_parent,
            meeting_date=record["meeting_date"],
            meeting_series=record["meeting_series"],
            meeting_uid=job["meeting_uid"],
            artifact_type="meeting_minutes",
            data_version=new_version,
            extension="md",
            content=source_content,
        )
        reviewed = self._publish(
            parent=self.config.reviewed_parent,
            meeting_date=record["meeting_date"],
            meeting_series=record["meeting_series"],
            meeting_uid=job["meeting_uid"],
            artifact_type="meeting_minutes",
            data_version=new_version,
            extension="md",
            content=source_content,
            category="meeting_minutes",
        )
        fresh = self.get_record(job["record_id"])
        if fresh["data_version"] != expected_version:
            raise StaleJob("source_review_publish_stale")
        field_defs = self.service.fields_by_name(
            self.service.list_bitable_fields(
                self.api_cfg, self.config.base_token, self.config.table_id
            )
        )
        fields: dict[str, Any] = {
            "数据版本": new_version,
            "会议纪要MD": self._url_cell(field_defs, "会议纪要MD", current),
            "会议纪要审核后MD": self._url_cell(field_defs, "会议纪要审核后MD", reviewed),
            "源纪要审核": "已审核",
        }
        expected = {
            "会议纪要MD": current["url"],
            "会议纪要审核后MD": reviewed["url"],
            "源纪要审核": "已审核",
        }
        for artifact_type in GENERATION_ARTIFACT_TYPES:
            spec = FIELD_URLS[artifact_type]
            previous = record["artifact_review_statuses"].get(artifact_type) or "未审核"
            status = "需重审" if previous == "已审核" else "未审核"
            fields[spec["review"]] = status
            expected[spec["review"]] = status
        self._update_and_confirm(job["record_id"], fields, expected, new_version)
        links = {"current_md": current, "reviewed_md": reviewed}
        cleanup = self._cleanup_old_files(
            old_tokens=record["current_file_tokens"].get("meeting_minutes", {}),
            new_tokens={"md": current["file_token"], "json": ""},
            artifact_type="meeting_minutes",
            meeting_date=record["meeting_date"],
        )
        links["cleanup"] = cleanup
        self._save_registry_entry(
            meeting_uid=job["meeting_uid"],
            artifact_type="meeting_minutes",
            data_version=new_version,
            links=links,
            review_md_sha256=job["review_md_sha256"],
        )
        updated = self.get_record(job["record_id"])
        return updated

    def enqueue_generation_jobs(self, record: dict[str, Any]) -> list[str]:
        self._require_apply()
        pending = self.config.generation_job_root / "pending"
        ensure_private_directory(pending)
        queued: list[str] = []
        for artifact_type in GENERATION_ARTIFACT_TYPES:
            job_id = f"{record['meeting_uid']}-v{record['data_version']}-{artifact_type}"
            job = {
                "job_version": 1,
                "job_id": job_id,
                "state": "pending",
                "meeting_uid": record["meeting_uid"],
                "record_id": record["record_id"],
                "artifact_type": artifact_type,
                "data_version": record["data_version"],
                "input_file_token": record["source_file_token"],
                "input_md_sha256": record["source_md_sha256"],
                "meeting_date": record["meeting_date"],
                "meeting_series": record["meeting_series"],
                "meeting_type": record["meeting_type"],
                "source_review_status": record["source_review_status"],
                "created_at": utc_now(),
            }
            path = pending / f"{job_id}.json"
            if path.exists():
                existing = json.loads(path.read_text(encoding="utf-8"))
                existing["created_at"] = job["created_at"]
                if existing != job:
                    raise PipelineJobError("generation_job_conflict")
            else:
                atomic_private_json(path, job)
            queued.append(job_id)
        return queued
