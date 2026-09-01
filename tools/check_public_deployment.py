#!/usr/bin/env python3
"""Read-only check that the public deployment bundle is internally complete."""

from __future__ import annotations

import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]


def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid JSON: {path.relative_to(ROOT)}") from exc


def main() -> int:
    required = (
        "README.md",
        "docs/公开部署配置.md",
        "docs/多维表格Workflow搭建.md",
        "docs/端到端快速验收.md",
        ".implementation/meeting-pipeline-contract/contract/unified-base.schema.json",
        ".implementation/meeting-pipeline-worker/unified_worker_service.py",
        ".implementation/meeting-pipeline-ops/prepare_public_worker_env.py",
        ".implementation/meeting-pipeline-ops/provision_collaboration_workflows.py",
        ".implementation/meeting-pipeline-ops/reconcile_missed_ingress.py",
        ".implementation/meeting-pipeline-ops/deployment/yanliu-router.service.example",
        ".implementation/meeting-pipeline-ops/deployment/yanliu-worker.service.example",
        ".implementation/meeting-pipeline-ops/deployment/yanliu-reconciler.service.example",
        ".implementation/meeting-pipeline-ops/deployment/start-wsl-pipeline.ps1.example",
        ".implementation/meeting-minutes-industry-market-viewpoints/SKILL.md",
        ".implementation/meeting-minutes-structured-table-current/SKILL.md",
        ".implementation/meeting-minutes-structured-table-current/contract/manifest.json",
        ".implementation/meeting-minutes-structured-table-current/scripts/generate_table.py",
        ".implementation/version-retention/feishu-drive-to-bitable/.field-bindings.meeting-minutes.example.json",
    )
    missing = [relative for relative in required if not (ROOT / relative).is_file()]
    if missing:
        raise RuntimeError("missing public deployment files: " + ", ".join(missing))

    schema = load_json(
        ROOT
        / ".implementation/meeting-pipeline-contract/contract/unified-base.schema.json"
    )
    schema_fields = {str(item["name"]) for item in schema["fields"]}
    bindings = load_json(
        ROOT
        / ".implementation/version-retention/feishu-drive-to-bitable/.field-bindings.meeting-minutes.example.json"
    )
    binding_fields = bindings.get("fields")
    if not isinstance(binding_fields, dict):
        raise RuntimeError("field-binding template has no fields object")
    expected = schema_fields | {"会议纪要上传附件"}
    if set(binding_fields) != expected or len(set(binding_fields.values())) != len(expected):
        raise RuntimeError("field-binding template does not match the public Base schema")

    skill_root = ROOT / ".implementation/meeting-minutes-structured-table-current"
    manifest = load_json(skill_root / "contract/manifest.json")
    runtime_paths = manifest.get("runtime_paths")
    if not isinstance(runtime_paths, list) or not runtime_paths:
        raise RuntimeError("structured Skill manifest has no runtime paths")
    missing_runtime = [str(path) for path in runtime_paths if not (skill_root / path).exists()]
    if missing_runtime:
        raise RuntimeError("structured Skill runtime is incomplete: " + ", ".join(missing_runtime))

    security_master = skill_root / "data/security_master.csv"
    if security_master.read_text(encoding="utf-8-sig").strip() != (
        "target_name,stock_code,market,aliases"
    ):
        raise RuntimeError("public security master must contain only the safe header")

    print(
        json.dumps(
            {
                "ok": True,
                "deployment_docs": 3,
                "collaboration_workflows": 5,
                "restart_recovery_templates": 4,
                "base_fields": len(schema_fields),
                "binding_fields": len(binding_fields),
                "structured_skill_runtime_paths": len(runtime_paths),
                "external_writes": False,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from None
