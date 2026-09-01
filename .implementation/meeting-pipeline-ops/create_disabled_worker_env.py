#!/usr/bin/env python3
"""Create a private, disabled production environment for the unified worker."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Mapping


class EnvironmentError(RuntimeError):
    pass


def read_dotenv(path: Path) -> dict[str, str]:
    if path.is_symlink() or not path.is_file():
        raise EnvironmentError("source environment file is missing or unsafe")
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise EnvironmentError("source environment contains an invalid key")
        values[key] = value.strip().strip("'").strip('"')
    return values


def read_assets(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EnvironmentError("production assets config is invalid") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise EnvironmentError("production assets config is invalid")
    folders = value.get("folders")
    assets = value.get("assets")
    if not isinstance(folders, dict) or not isinstance(assets, dict):
        raise EnvironmentError("production assets config is incomplete")
    return value


def sha256_file(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise EnvironmentError("packaged runtime asset is missing or unsafe")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_values(
    *,
    router: Mapping[str, str],
    structured: Mapping[str, str],
    config: Mapping[str, object],
    service_root: Path,
    router_data: Path | None = None,
    codex_bin: str = "codex",
) -> dict[str, str]:
    if not service_root.is_absolute() or service_root.is_symlink() or not service_root.is_dir():
        raise EnvironmentError("service root is missing or unsafe")
    app_id = str(router.get("FEISHU_APP_ID") or "")
    app_secret = str(router.get("FEISHU_APP_SECRET") or "")
    if (
        not app_id
        or not app_secret
        or app_id != structured.get("FEISHU_APP_ID")
        or app_secret != structured.get("FEISHU_APP_SECRET")
    ):
        raise EnvironmentError("existing production app credentials do not match")
    folders = config["folders"]
    assets = config["assets"]
    assert isinstance(folders, dict) and isinstance(assets, dict)
    industry = assets.get("industry")
    structured_skill = assets.get("structured")
    if not isinstance(industry, dict) or not isinstance(structured_skill, dict):
        raise EnvironmentError("production Skill assets are incomplete")
    paths = {
        "MEETING_PIPELINE_CONTRACT_PATH": service_root
        / "assets/meeting-pipeline-contract/meeting_pipeline_contract.py",
        "FEISHU_STRUCTURED_SERVICE_ROOT": service_root / "assets/structured-service",
        "INDUSTRY_MARKET_SKILL_ROOT": service_root
        / "skills/industry-market-viewpoints",
        "STRUCTURED_DRAFT_SCRIPT": service_root
        / "assets/structured-draft/scripts/generate_draft_json.py",
        "STRUCTURED_SKILL_ROOT": service_root / "skills/structured-table",
    }
    checks = {
        paths["MEETING_PIPELINE_CONTRACT_PATH"]: assets.get(
            "pipeline_contract_sha256"
        ),
        paths["FEISHU_STRUCTURED_SERVICE_ROOT"]
        / "structured_generate_service.py": assets.get("structured_service_sha256"),
        paths["FEISHU_STRUCTURED_SERVICE_ROOT"]
        / "skill_contract.py": assets.get("structured_service_contract_sha256"),
        paths["INDUSTRY_MARKET_SKILL_ROOT"]
        / "contract/manifest.json": industry.get("manifest_sha256"),
        paths["INDUSTRY_MARKET_SKILL_ROOT"]
        / "scripts/generate_viewpoints.py": industry.get("script_sha256"),
        paths["STRUCTURED_DRAFT_SCRIPT"]: assets.get(
            "structured_draft_script_sha256"
        ),
        paths["STRUCTURED_SKILL_ROOT"]
        / "contract/manifest.json": structured_skill.get("manifest_sha256"),
        paths["STRUCTURED_SKILL_ROOT"]
        / "scripts/generate_table.py": structured_skill.get("script_sha256"),
    }
    for path, expected in checks.items():
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise EnvironmentError("production asset hash is invalid")
        if sha256_file(path) != expected:
            raise EnvironmentError("packaged runtime asset hash mismatch")
    router_data = router_data or (service_root / "router-data")
    if not router_data.is_absolute():
        raise EnvironmentError("router data path must be absolute")
    worker_data = service_root / "data"
    values = {
        "FEISHU_UNIFIED_PIPELINE_ENABLED": "false",
        "FEISHU_APP_ID": app_id,
        "FEISHU_APP_SECRET": app_secret,
        "FEISHU_MEETING_BASE_APP_TOKEN": str(config.get("base_token") or ""),
        "FEISHU_MEETING_BASE_TABLE_ID": str(config.get("table_id") or ""),
        "FEISHU_OUTPUT_OWNER_OPEN_ID": str(
            structured.get("FEISHU_OUTPUT_OWNER_OPEN_ID") or ""
        ),
        "FEISHU_USER_ID_TYPE": str(router.get("FEISHU_USER_ID_TYPE") or "open_id"),
        "FEISHU_OPENAPI_BASE": "https://open.feishu.cn/open-apis",
        "FEISHU_PARENT_FOLDER_TOKEN": str(folders.get("source_current") or ""),
        "FEISHU_PIPELINE_INDUSTRY_MD_FOLDER_TOKEN": str(folders.get("industry_md") or ""),
        "FEISHU_PIPELINE_INDUSTRY_JSON_FOLDER_TOKEN": str(folders.get("industry_json") or ""),
        "FEISHU_PIPELINE_STRUCTURED_MD_FOLDER_TOKEN": str(folders.get("structured_md") or ""),
        "FEISHU_PIPELINE_STRUCTURED_JSON_FOLDER_TOKEN": str(folders.get("structured_json") or ""),
        "FEISHU_PIPELINE_BASELINE_FOLDER_TOKEN": str(folders.get("baseline") or ""),
        "FEISHU_PIPELINE_REVIEWED_FOLDER_TOKEN": str(folders.get("reviewed") or ""),
        "FEISHU_PIPELINE_HISTORY_FOLDER_TOKEN": str(folders.get("history") or ""),
        "FEISHU_GENERATION_JOB_SPOOL_PATH": str(router_data / "meeting-generation-jobs"),
        "FEISHU_PIPELINE_REVIEW_JOB_SPOOL_DIR": str(router_data / "pipeline-review-jobs"),
        "FEISHU_PIPELINE_WORKER_RECEIPT_DIR": str(router_data / "meeting-pipeline-receipts"),
        "FEISHU_PIPELINE_WORKER_LOCK_PATH": str(worker_data / "unified-worker.lock"),
        "FEISHU_PIPELINE_WORK_DIR": str(worker_data / "work"),
        "FEISHU_PIPELINE_ARTIFACT_REGISTRY_PATH": str(worker_data / "artifact-registry.json"),
        "FEISHU_PIPELINE_RECORD_LOCK_DIR": str(worker_data / "record-locks"),
        "FEISHU_PIPELINE_FOLDER_REGISTRY_PATH": str(worker_data / "folder-registry.json"),
        "FEISHU_PIPELINE_OUTPUT_DIR": str(worker_data / "outputs"),
        "MEETING_PIPELINE_CONTRACT_PATH": str(paths["MEETING_PIPELINE_CONTRACT_PATH"]),
        "MEETING_PIPELINE_CONTRACT_SHA256": str(assets["pipeline_contract_sha256"]),
        "MEETING_PIPELINE_CONTRACT_RUNTIME_SHA256": str(
            assets["pipeline_contract_runtime_sha256"]
        ),
        "FEISHU_STRUCTURED_SERVICE_ROOT": str(paths["FEISHU_STRUCTURED_SERVICE_ROOT"]),
        "FEISHU_STRUCTURED_SERVICE_SHA256": str(assets["structured_service_sha256"]),
        "FEISHU_STRUCTURED_SERVICE_CONTRACT_SHA256": str(
            assets["structured_service_contract_sha256"]
        ),
        "INDUSTRY_MARKET_SKILL_ROOT": str(paths["INDUSTRY_MARKET_SKILL_ROOT"]),
        "INDUSTRY_MARKET_SKILL_MANIFEST_SHA256": str(industry["manifest_sha256"]),
        "INDUSTRY_MARKET_SKILL_SCRIPT_SHA256": str(industry["script_sha256"]),
        "INDUSTRY_MARKET_SKILL_RUNTIME_SHA256": str(industry["runtime_sha256"]),
        "STRUCTURED_DRAFT_SCRIPT": str(paths["STRUCTURED_DRAFT_SCRIPT"]),
        "STRUCTURED_DRAFT_SCRIPT_SHA256": str(assets["structured_draft_script_sha256"]),
        "STRUCTURED_SKILL_ROOT": str(paths["STRUCTURED_SKILL_ROOT"]),
        "STRUCTURED_SKILL_MANIFEST_SHA256": str(structured_skill["manifest_sha256"]),
        "STRUCTURED_SKILL_SCRIPT_SHA256": str(structured_skill["script_sha256"]),
        "STRUCTURED_SKILL_RUNTIME_SHA256": str(structured_skill["runtime_sha256"]),
        "FEISHU_PIPELINE_CODEX_BIN": codex_bin,
        "FEISHU_PIPELINE_MODEL": "codex-cli-default",
        "FEISHU_PIPELINE_MODEL_REASONING_EFFORT": "medium",
        "FEISHU_PIPELINE_MODEL_TIMEOUT_SECONDS": "1800",
        "FEISHU_PIPELINE_WORKER_POLL_SECONDS": "2",
        "FEISHU_PIPELINE_WORKER_HTTP_HOST": "127.0.0.1",
        "FEISHU_PIPELINE_WORKER_HTTP_PORT": "8792",
    }
    if any(not value for value in values.values()):
        raise EnvironmentError("production worker environment contains an empty value")
    return values


def write_private_env(path: Path, values: Mapping[str, str]) -> None:
    if path.exists() or path.is_symlink():
        raise EnvironmentError("target environment already exists")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle, temporary = tempfile.mkstemp(prefix=".worker-env.", dir=str(path.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            for key, value in values.items():
                if "\n" in value or "\r" in value:
                    raise EnvironmentError("environment value contains a newline")
                stream.write(f"{key}={value}\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--router-env", required=True)
    parser.add_argument("--structured-env", required=True)
    parser.add_argument("--assets", required=True)
    parser.add_argument("--service-root", required=True)
    parser.add_argument("--router-data", type=Path, help="shared router spool directory; defaults under service root")
    parser.add_argument("--codex-bin", default="codex", help="Codex CLI executable or absolute path")
    parser.add_argument("--target-env", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    values = build_values(
        router=read_dotenv(Path(args.router_env)),
        structured=read_dotenv(Path(args.structured_env)),
        config=read_assets(Path(args.assets)),
        service_root=Path(args.service_root),
        router_data=args.router_data,
        codex_bin=args.codex_bin,
    )
    if args.apply:
        write_private_env(Path(args.target_env), values)
    print(
        json.dumps(
            {
                "status": "created_disabled" if args.apply else "dry_run_ready",
                "unified_enabled": False,
                "field_count": len(values),
                "secret_values_disclosed": False,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except EnvironmentError as exc:
        print(str(exc), file=os.sys.stderr)
        raise SystemExit(2) from None
