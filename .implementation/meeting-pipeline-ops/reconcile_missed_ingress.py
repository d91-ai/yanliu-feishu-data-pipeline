#!/usr/bin/env python3
"""Recover recent attachment records whose ingress event was missed."""

from __future__ import annotations

import argparse
import importlib.util
import logging
import os
from pathlib import Path
import sys
import time
from typing import Any, Mapping


def load_router(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location("yanliu_router_reconciler", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("router_module_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def record_timestamp_seconds(record: Mapping[str, Any]) -> int:
    raw = (record.get("last_modified_time") or record.get("updated_time")
           or record.get("created_time") or 0)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0
    return value // 1000 if value > 10_000_000_000 else value


def reconcile_once(
    router: Any,
    cfg: Any,
    lookback_hours: int,
    *,
    apply: bool,
    meeting_id_field: str = "会议ID",
) -> tuple[int, int, int]:
    cutoff = int(time.time()) - lookback_hours * 3600
    candidates = recovered = failed = 0
    for record in router.list_bitable_records(cfg):
        if not isinstance(record, Mapping):
            continue
        fields = record.get("fields")
        if not isinstance(fields, Mapping) or record_timestamp_seconds(record) < cutoff:
            continue
        if router.plain_field_value(fields.get(meeting_id_field)).strip():
            continue
        if not router._attachment_items_from_field(fields.get(cfg.form_attachment_field)):
            continue
        record_id = str(record.get("record_id") or record.get("id") or "").strip()
        if not record_id:
            continue
        candidates += 1
        if not apply:
            continue
        try:
            result = router.process_form_attachment_ingress(cfg, record_id)
            if result.get("status") not in {"ignored", "disabled"}:
                recovered += 1
        except Exception as exc:  # One malformed record must not block recovery.
            failed += 1
            logging.error("missed_ingress_record_failed code=%s",
                          router.safe_error_code(exc))
    return candidates, recovered, failed


def main() -> int:
    default_router = (Path(__file__).resolve().parents[1]
                      / "version-retention/feishu-drive-to-bitable/feishu_drive_to_bitable.py")
    parser = argparse.ArgumentParser(description="Recover missed attachment ingress events")
    parser.add_argument("--router-module", type=Path, default=default_router)
    parser.add_argument("--router-env", type=Path, required=True)
    parser.add_argument("--route-env", type=Path, required=True)
    parser.add_argument("--meeting-id-field", default="会议ID")
    parser.add_argument("--lookback-hours", type=int, default=48)
    parser.add_argument("--loop-seconds", type=int, default=300)
    parser.add_argument("--initial-delay-seconds", type=int, default=0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.lookback_hours < 1 or args.loop_seconds < 60:
        raise ValueError("lookback-hours must be positive and loop-seconds must be >= 60")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    router = load_router(args.router_module.resolve())
    os.environ.update(router.parse_dotenv_file(args.router_env.resolve()))
    cfg = router.read_config_from_env_file(args.route_env.resolve())
    if args.initial_delay_seconds > 0:
        time.sleep(args.initial_delay_seconds)
    while True:
        cycle_failed = False
        try:
            candidates, recovered, failed = reconcile_once(
                router, cfg, args.lookback_hours, apply=args.apply,
                meeting_id_field=args.meeting_id_field,
            )
            logging.info(
                "missed_ingress_reconcile_complete candidates=%s recovered=%s failed=%s apply=%s",
                candidates, recovered, failed, args.apply,
            )
        except Exception as exc:
            cycle_failed = True
            logging.error("missed_ingress_reconcile_failed code=%s",
                          router.safe_error_code(exc))
        if args.once:
            return 2 if cycle_failed or failed else 0
        time.sleep(args.loop_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
