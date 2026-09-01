#!/usr/bin/env python3
"""Turn a read-only publication audit into a non-executing reconcile plan."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any


class ReconcilePlanError(ValueError):
    pass


def read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReconcilePlanError("invalid audit JSON") from exc
    if not isinstance(value, dict) or not isinstance(value.get("issues"), list):
        raise ReconcilePlanError("audit JSON must contain issues array")
    return value


def build_plan(audit: dict[str, Any]) -> dict[str, Any]:
    actions: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for issue in audit.get("issues", []):
        if not isinstance(issue, dict):
            blocked.append({"code": "invalid_audit_issue"})
            continue
        code = str(issue.get("code") or "")
        if code == "orphan_json" and issue.get("file_token"):
            actions.append(
                {
                    "action": "quarantine_file",
                    "file_token": issue["file_token"],
                    "meeting_uid": issue.get("meeting_uid", ""),
                    "artifact_type": issue.get("artifact_type", ""),
                    "precondition": "fresh_audit_still_reports_same_orphan",
                    "destructive": False,
                }
            )
        elif code == "base_current_file_missing_or_invalid":
            actions.append(
                {
                    "action": "rebuild_from_artifact_registry",
                    "record_id": issue.get("record_id", ""),
                    "field": issue.get("field", ""),
                    "file_token": issue.get("file_token", ""),
                    "precondition": "registry_identity_version_hash_unique",
                    "destructive": False,
                }
            )
        else:
            blocked.append(issue)
    canonical = json.dumps(
        {"actions": actions, "blocked": blocked},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "schema_version": 1,
        "mode": "plan-only",
        "action_count": len(actions),
        "blocked_count": len(blocked),
        "actions": actions,
        "blocked": blocked,
        "plan_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a publication reconcile plan")
    parser.add_argument("--audit-json", required=True)
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    plan = build_plan(read_object(Path(args.audit_json)))
    output = json.dumps(plan, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        target = Path(args.output)
        if target.exists() and target.read_text(encoding="utf-8") != output:
            raise ReconcilePlanError("output exists with different content")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(output, encoding="utf-8")
    print(output, end="")
    return 0 if not plan["blocked_count"] else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReconcilePlanError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from None
