#!/usr/bin/env python3
"""Run an explicitly configured structured-regeneration batch."""

from __future__ import annotations

from datetime import datetime
import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import time
from typing import Any


WORKSPACE = Path(__file__).resolve().parents[1]
ROOT = WORKSPACE / "outputs/structured-regeneration"
SUMMARY = ROOT / "generation_summary.json"
MANIFEST = ROOT / "manifest.json"

svc: Any = None


def load_service_module(runtime_dir: Path) -> Any:
    path = runtime_dir / "structured_generate_service.py"
    spec = importlib.util.spec_from_file_location("structured_regeneration_service", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("structured runtime module is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def run_cli(args: list[str], *, dry_run: bool) -> dict[str, Any]:
    cmd = ["lark-cli", *args]
    if dry_run and "--dry-run" not in cmd:
        cmd.append("--dry-run")
    result = subprocess.run(cmd, cwd=WORKSPACE, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"command failed: {' '.join(cmd[:3])}: {(result.stderr or result.stdout).strip()[:800]}")
    output = result.stdout.strip()
    if dry_run:
        return {"dry_run": True, "stdout": output[:1200]}
    if not output:
        return {}
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return {"stdout": output}


def rel(path: str | Path) -> str:
    return str(Path(path).resolve().relative_to(WORKSPACE))


def now_cell() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def get_month_folder(cfg: svc.Config, meeting_date: str) -> str:
    month = meeting_date[:7]
    registry = svc.load_folder_registry(cfg.folder_registry_path)
    entry = registry.get("months", {}).get(month, {})
    token = entry.get("source_folder_token") if isinstance(entry, dict) else ""
    if not token:
        raise RuntimeError(f"missing structured source folder token for month {month}")
    return str(token)


def resolve_file_url(cfg: svc.Config, folder_token: str, file_token: str) -> str:
    for _ in range(12):
        for item in svc.list_drive_folder_items(cfg, folder_token):
            token = str(item.get("token") or item.get("file_token") or "")
            if token == file_token and item.get("url"):
                return str(item["url"])
        time.sleep(2)
    return f"https://example-tenant.feishu.cn/file/{file_token}"


def list_records(cfg: svc.Config, base_token: str, table_id: str) -> list[dict[str, Any]]:
    token = svc.get_tenant_access_token(cfg)
    records: list[dict[str, Any]] = []
    page_token = ""
    path = f"/bitable/v1/apps/{base_token}/tables/{table_id}/records"
    while True:
        query: dict[str, Any] = {"page_size": 500, "user_id_type": cfg.user_id_type}
        if page_token:
            query["page_token"] = page_token
        result = svc.request_json(cfg, "GET", path, token=token, query=query)
        data = result.get("data", {})
        records.extend(data.get("items") or [])
        if not data.get("has_more"):
            return records
        page_token = str(data.get("page_token") or data.get("next_page_token") or "")
        if not page_token:
            return records


def find_structured_record(cfg: svc.Config, source_name: str, file_url: str) -> str:
    for _ in range(18):
        for record in list_records(cfg, cfg.structured_base_token, cfg.structured_table_id):
            fields = record.get("fields") or {}
            name = svc.plain_field_value(fields.get("表格名"))
            url = svc.url_from_field_value(fields.get("待审核MD链接"))
            if name == source_name or (file_url and url == file_url):
                return str(record.get("record_id") or "")
        time.sleep(2)
    return ""


def record_upsert_args(base_token: str, table_id: str, payload: dict[str, Any], record_id: str = "") -> list[str]:
    args = [
        "base",
        "+record-upsert",
        "--base-token",
        base_token,
        "--table-id",
        table_id,
        "--json",
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        "--as",
        "bot",
        "--format",
        "json",
    ]
    if record_id:
        args.extend(["--record-id", record_id])
    return args


def markdown_overwrite_args(file_token: str, file_path: str, name: str) -> list[str]:
    return [
        "markdown",
        "+overwrite",
        "--as",
        "bot",
        "--file-token",
        file_token,
        "--file",
        rel(file_path),
        "--name",
        name,
        "--json",
    ]


def markdown_create_args(folder_token: str, file_path: str, name: str) -> list[str]:
    return [
        "markdown",
        "+create",
        "--as",
        "bot",
        "--folder-token",
        folder_token,
        "--file",
        rel(file_path),
        "--name",
        name,
        "--json",
    ]


def main() -> int:
    global svc, ROOT, SUMMARY, MANIFEST
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, default=ROOT, help="batch manifest and output directory")
    parser.add_argument(
        "--online-read-only",
        action="store_true",
        help="explicitly allow runtime/Feishu reads while keeping lark-cli writes in dry-run",
    )
    parser.add_argument("--apply", action="store_true", help="Perform writes. Without this flag, only dry-run command payloads.")
    args = parser.parse_args()
    ROOT = args.work_dir.expanduser().resolve()
    SUMMARY = ROOT / "generation_summary.json"
    MANIFEST = ROOT / "manifest.json"
    if not args.apply and not args.online_read_only:
        print(
            json.dumps(
                {
                    "ok": True,
                    "dry_run": True,
                    "external_access": False,
                    "work_dir": str(ROOT),
                }
            )
        )
        return 0
    svc = load_service_module(args.runtime_dir.expanduser().resolve())
    dry_run = not args.apply

    cfg = svc.read_config()
    summary = load_json(SUMMARY)
    manifest_by_record = {item["source_record_id"]: item for item in load_json(MANIFEST)}
    results: list[dict[str, Any]] = []

    for item in summary:
        manifest = manifest_by_record[item["source_record_id"]]
        output_path = WORKSPACE / item["output_path"]
        output_name = f"{item['source_name']} - 结构化表格.md"
        row_count = int(item["rows"])
        file_url = item.get("structured_file_url") or ""
        file_token = item.get("structured_file_token") or ""
        structured_record_id = item.get("structured_record_id") or ""
        action = "overwrite"

        if file_token:
            run_cli(markdown_overwrite_args(file_token, str(output_path), output_name), dry_run=dry_run)
        else:
            action = "create"
            folder_token = get_month_folder(cfg, manifest["meeting_date"])
            create_result = run_cli(markdown_create_args(folder_token, str(output_path), output_name), dry_run=dry_run)
            if not dry_run:
                data = create_result.get("data", create_result)
                file_token = str(data.get("file_token") or data.get("token") or "")
                if not file_token:
                    raise RuntimeError(f"create returned no file_token for {item['source_name']}: {create_result}")
                file_url = resolve_file_url(cfg, folder_token, file_token)
                structured_record_id = find_structured_record(cfg, item["source_name"], file_url)

        source_payload = {
            "表格生成状态": "已生成" if row_count else "无可结构化标的",
            "表格行数": row_count,
            "待审核MD链接": file_url or item.get("structured_file_url") or "",
            "生成时间": now_cell(),
            "表格生成错误": "",
        }
        run_cli(
            record_upsert_args(cfg.source_base_token, cfg.source_table_id, source_payload, item["source_record_id"]),
            dry_run=dry_run,
        )

        structured_payload = {
            "表格名": item["source_name"],
            "会议日期": f"{manifest['meeting_date']} 00:00:00",
            "表格链接": file_url or item.get("structured_file_url") or "",
            "生成时间": now_cell(),
            "文档来源": "会议纪要",
            "源纪要链接": manifest["source_archive_url"],
        }
        if manifest.get("meeting_series"):
            structured_payload["会议系列"] = manifest["meeting_series"]
        if manifest.get("meeting_type"):
            structured_payload["会议类型"] = manifest["meeting_type"]

        if not structured_record_id and not dry_run:
            created = run_cli(
                record_upsert_args(cfg.structured_base_token, cfg.structured_table_id, structured_payload),
                dry_run=False,
            )
            record = created.get("data", {}).get("record") or created.get("record") or {}
            structured_record_id = str(record.get("record_id") or "")
        else:
            run_cli(
                record_upsert_args(cfg.structured_base_token, cfg.structured_table_id, structured_payload, structured_record_id),
                dry_run=dry_run,
            )

        results.append(
            {
                "source_record_id": item["source_record_id"],
                "source_name": item["source_name"],
                "action": action,
                "rows": row_count,
                "file_token": file_token,
                "file_url": file_url or item.get("structured_file_url") or "",
                "structured_record_id": structured_record_id,
            }
        )

    result_path = ROOT / ("apply_results.json" if args.apply else "dry_run_results.json")
    result_path.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"dry_run": dry_run, "processed": len(results), "result_path": str(result_path)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
