#!/usr/bin/env python3
"""Create cutover-ready Router env files without exposing credentials."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re

from create_disabled_worker_env import (
    EnvironmentError,
    read_assets,
    read_dotenv,
    write_private_env,
)


def build_router_values(current: dict[str, str]) -> dict[str, str]:
    app_id = str(current.get("FEISHU_APP_ID") or "")
    app_secret = str(current.get("FEISHU_APP_SECRET") or "")
    if not app_id or not app_secret:
        raise EnvironmentError("current Router credentials are missing")
    return {
        "FEISHU_APP_ID": app_id,
        "FEISHU_APP_SECRET": app_secret,
        "FEISHU_ROUTE_ENV_FILES": "/app/.env.meeting-minutes",
        "FEISHU_LOG_LEVEL": str(current.get("FEISHU_LOG_LEVEL") or "INFO"),
    }


def build_route_values(
    current: dict[str, str],
    config: dict[str, object],
    *,
    watermark_ms: int,
    form_ingress_enabled: bool,
) -> dict[str, str]:
    if watermark_ms <= 0:
        raise EnvironmentError("cutover watermark must be positive")
    folders = config.get("folders")
    if not isinstance(folders, dict):
        raise EnvironmentError("production folder assets are invalid")
    contract_enabled = str(
        current.get("FEISHU_MEETING_CONTRACT_ENABLED") or "false"
    ).lower() in {"1", "true", "yes"}
    validator = str(current.get("FEISHU_MEETING_CONTRACT_VALIDATOR") or "")
    validator_hash = str(
        current.get("FEISHU_MEETING_CONTRACT_VALIDATOR_SHA256") or ""
    ).lower()
    if contract_enabled and (
        not validator or not re.fullmatch(r"[0-9a-f]{64}", validator_hash)
    ):
        raise EnvironmentError("meeting-minutes contract pin is incomplete")
    values = {
        "FEISHU_FOLDER_TOKEN": str(folders.get("source_current") or ""),
        "FEISHU_ARCHIVE_ROOT_FOLDER_TOKEN": str(folders.get("history") or ""),
        "FEISHU_FOLDER_REGISTRY_PATH": "data/folder_registry_meeting_minutes.json",
        "FEISHU_BITABLE_APP_TOKEN": str(config.get("base_token") or ""),
        "FEISHU_BITABLE_TABLE_ID": str(config.get("table_id") or ""),
        "FEISHU_USER_ID_TYPE": str(current.get("FEISHU_USER_ID_TYPE") or "open_id"),
        "FEISHU_DRY_RUN": "false",
        "FEISHU_LOG_LEVEL": str(current.get("FEISHU_LOG_LEVEL") or "INFO"),
        "FEISHU_ARCHIVE_HTTP_ENABLED": "false",
        "FEISHU_ARCHIVE_DRY_RUN": "true",
        "FEISHU_EVENT_SPOOL_DIR": "data/event-spool",
        "FEISHU_PIPELINE_MODE": "unified",
        "FEISHU_UNREGISTERED_FILE_SPOOL_DIR": "data/unregistered-files",
        "FEISHU_PIPELINE_REVIEW_JOB_SPOOL_DIR": "data/pipeline-review-jobs",
        "FEISHU_PIPELINE_WORKER_RECEIPT_DIR": "data/meeting-pipeline-receipts",
        "FEISHU_PIPELINE_EVENT_NOT_BEFORE_MS": str(watermark_ms),
        "FEISHU_FORM_INGRESS_ENABLED": (
            "true" if form_ingress_enabled else "false"
        ),
        "FEISHU_FORM_ATTACHMENT_FIELD": "会议纪要上传附件",
        "FEISHU_FORM_MAX_ATTACHMENT_BYTES": str(10 * 1024 * 1024),
        "FEISHU_GENERATION_JOB_SPOOL_DIR": "data/meeting-generation-jobs",
        "FEISHU_FORM_INGESTION_RECEIPT_DIR": "data/meeting-ingestion-receipts",
        "FEISHU_VERSION_CONFIG_PATH": "",
        "FEISHU_VERSION_CAPTURE_ENABLED": "true",
        "FEISHU_VERSION_CAPTURE_ENFORCE": "true",
        "FEISHU_VERSION_ROOT_FOLDER_TOKEN": str(folders.get("baseline") or ""),
        "FEISHU_VERSION_CATEGORY": "会议纪要",
        "FEISHU_MEETING_CONTRACT_ENABLED": "true" if contract_enabled else "false",
        "FEISHU_MEETING_CONTRACT_VALIDATOR": validator,
        "FEISHU_MEETING_CONTRACT_VALIDATOR_SHA256": validator_hash,
    }
    if any(not value for key, value in values.items() if key != "FEISHU_VERSION_CONFIG_PATH"):
        raise EnvironmentError("unified Router environment contains an empty value")
    return values


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--current-router-env", required=True)
    parser.add_argument("--current-route-env", required=True)
    parser.add_argument("--assets", required=True)
    parser.add_argument("--router-output", required=True)
    parser.add_argument("--route-output", required=True)
    parser.add_argument("--watermark-ms", type=int, required=True)
    parser.add_argument("--enable-form-ingress", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    router_values = build_router_values(read_dotenv(Path(args.current_router_env)))
    route_values = build_route_values(
        read_dotenv(Path(args.current_route_env)),
        read_assets(Path(args.assets)),
        watermark_ms=args.watermark_ms,
        form_ingress_enabled=args.enable_form_ingress,
    )
    outputs = [Path(args.router_output), Path(args.route_output)]
    if any(path.exists() or path.is_symlink() for path in outputs):
        raise EnvironmentError("unified Router output already exists")
    if args.apply:
        write_private_env(outputs[0], router_values)
        try:
            write_private_env(outputs[1], route_values)
        except Exception:
            outputs[0].unlink(missing_ok=True)
            raise
    print(
        json.dumps(
            {
                "status": "created" if args.apply else "dry_run_ready",
                "pipeline_mode": "unified",
                "form_ingress_enabled": args.enable_form_ingress,
                "watermark_ms": args.watermark_ms,
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
        print(str(exc), file=__import__("sys").stderr)
        raise SystemExit(2) from None
