#!/usr/bin/env python3
"""Sync the pinned meeting-minutes contract validator across live runtimes."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import urllib.error
import urllib.request
from typing import Any


DEFAULT_SKILL_DIR = Path.home() / ".codex/skills/投资会议纪要整理"
DEFAULT_ROUTER_SERVICE_DIR = Path.home() / "services/feishu-drive-to-bitable"
DEFAULT_UPLOAD_ENV = Path.home() / "services/feishu-upload-service/.env"
DEFAULT_UPLOAD_CONTAINER = "feishu-upload-service"
DEFAULT_ROUTER_CONTAINER = "feishu-drive-to-bitable-router"
DEFAULT_UPLOAD_HEALTHZ = "http://127.0.0.1:8789/healthz"
DEFAULT_MINUTES_ARCHIVE_HEALTHZ = "http://127.0.0.1:8787/healthz"

HELPER_PATH = Path(__file__).with_name("update_meeting_contract_router_config.py")
ENV_SHA_KEY = "FEISHU_MEETING_CONTRACT_VALIDATOR_SHA256"


def load_update_helper():
    spec = importlib.util.spec_from_file_location("update_meeting_contract_router_config", HELPER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load helper: {HELPER_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_env_value(path: Path, key: str) -> str:
    if not path.is_file() or path.is_symlink():
        return ""
    match = re.search(rf"(?m)^{re.escape(key)}=([^\r\n]*)$", path.read_text(encoding="utf-8"))
    return match.group(1).strip() if match else ""


def healthz(url: str) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return {"ok": 200 <= response.status < 300, "status": response.status, "payload": payload}
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except Exception:
            payload = {}
        return {"ok": False, "status": exc.code, "payload": payload}
    except Exception as exc:
        return {"ok": False, "status": "unreachable", "error": type(exc).__name__}


def safe_health_summary(result: dict[str, Any]) -> dict[str, Any]:
    payload = result.get("payload")
    if not isinstance(payload, dict):
        return {key: result.get(key) for key in ("ok", "status", "error") if key in result}
    checks = payload.get("checks")
    return {
        "ok": bool(result.get("ok")),
        "status": result.get("status"),
        "service": payload.get("service"),
        "ready": payload.get("ready"),
        "checks": checks if isinstance(checks, dict) else None,
        "archive_dry_run": payload.get("archive_dry_run"),
        "version_settings_ready": payload.get("version_settings_ready"),
    }


def run_command(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, check=False, capture_output=True, text=True)


def restart_container(name: str) -> dict[str, Any]:
    result = run_command(["docker", "restart", name])
    return {
        "ok": result.returncode == 0,
        "container": name,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip()[:500],
    }


def docker_env_hash_match(container: str, env_path: str, validator_path: str) -> dict[str, Any]:
    code = (
        "import hashlib,json,re; "
        f"env_path={env_path!r}; validator={validator_path!r}; "
        "text=open(env_path, encoding='utf-8').read(); "
        f"m=re.search(r'(?m)^{re.escape(ENV_SHA_KEY)}=([^\\r\\n]*)$', text); "
        "expected=m.group(1).strip() if m else ''; "
        "actual=hashlib.sha256(open(validator,'rb').read()).hexdigest(); "
        "print(json.dumps({'expected':expected,'actual':actual,'match':expected==actual}))"
    )
    result = run_command(["docker", "exec", container, "python", "-c", code])
    if result.returncode != 0:
        return {
            "ok": False,
            "container": container,
            "error": result.stderr.strip()[:500] or result.stdout.strip()[:500],
        }
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "container": container, "error": "invalid_json"}
    return {
        "ok": bool(payload.get("match")),
        "container": container,
        "expected": payload.get("expected"),
        "actual": payload.get("actual"),
        "match": bool(payload.get("match")),
    }


def sync_env_files(*, service_dir: Path, upload_env: Path, skill_dir: Path, apply: bool) -> dict[str, Any]:
    helper = load_update_helper()
    validator = skill_dir / "scripts" / "validate_meeting_minutes_contract.py"
    if validator.is_symlink() or not validator.is_file():
        raise SystemExit("installed meeting-minutes validator is unavailable")
    digest = sha256_file(validator)
    host_env = service_dir / ".env"
    meeting_env = service_dir / ".env.meeting-minutes"
    structured_env = service_dir / ".env.structured"
    planned_files = [host_env, meeting_env, structured_env, upload_env]

    before = {
        str(meeting_env): read_env_value(meeting_env, ENV_SHA_KEY),
        str(upload_env): read_env_value(upload_env, ENV_SHA_KEY),
    }
    if apply:
        helper.update_files_with_rollback(
            [
                (host_env, {"MEETING_MINUTES_SKILL_HOST_DIR": str(skill_dir)}),
                (
                    meeting_env,
                    {
                        "FEISHU_MEETING_CONTRACT_ENABLED": "true",
                        "FEISHU_MEETING_CONTRACT_VALIDATOR": helper.CONTAINER_VALIDATOR,
                        ENV_SHA_KEY: digest,
                    },
                ),
                (structured_env, {"FEISHU_MEETING_CONTRACT_ENABLED": "false"}),
                (
                    upload_env,
                    {
                        "FEISHU_MEETING_CONTRACT_ENABLED": "true",
                        "FEISHU_MEETING_CONTRACT_VALIDATOR": helper.CONTAINER_VALIDATOR,
                        ENV_SHA_KEY: digest,
                    },
                ),
            ]
        )
    after = {
        str(meeting_env): read_env_value(meeting_env, ENV_SHA_KEY),
        str(upload_env): read_env_value(upload_env, ENV_SHA_KEY),
    }
    return {
        "ok": True,
        "dry_run": not apply,
        "validator_sha256": digest,
        "planned_files": [str(path) for path in planned_files],
        "before": before,
        "after": after,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill-dir", type=Path, default=DEFAULT_SKILL_DIR)
    parser.add_argument("--router-service-dir", type=Path, default=DEFAULT_ROUTER_SERVICE_DIR)
    parser.add_argument("--upload-env", type=Path, default=DEFAULT_UPLOAD_ENV)
    parser.add_argument("--upload-container", default=DEFAULT_UPLOAD_CONTAINER)
    parser.add_argument("--router-container", default=DEFAULT_ROUTER_CONTAINER)
    parser.add_argument("--upload-healthz", default=DEFAULT_UPLOAD_HEALTHZ)
    parser.add_argument("--minutes-archive-healthz", default=DEFAULT_MINUTES_ARCHIVE_HEALTHZ)
    parser.add_argument("--apply", action="store_true", help="update runtime env files")
    parser.add_argument("--restart", action="store_true", help="restart upload and router containers")
    parser.add_argument("--check", action="store_true", help="run health and container hash-match checks")
    args = parser.parse_args()

    if args.restart and not args.apply:
        raise SystemExit("--restart requires --apply")

    service_dir = args.router_service_dir.expanduser().resolve()
    upload_env = args.upload_env.expanduser().resolve()
    skill_dir = args.skill_dir.expanduser().resolve()

    report: dict[str, Any] = {
        "sync": sync_env_files(
            service_dir=service_dir,
            upload_env=upload_env,
            skill_dir=skill_dir,
            apply=args.apply,
        )
    }
    report["ok"] = bool(report["sync"]["ok"])

    if args.restart:
        report["restart"] = [
            restart_container(args.upload_container),
            restart_container(args.router_container),
        ]
        report["ok"] = report["ok"] and all(item["ok"] for item in report["restart"])

    if args.check:
        helper = load_update_helper()
        report["checks"] = {
            "upload_healthz": safe_health_summary(healthz(args.upload_healthz)),
            "minutes_archive_healthz": safe_health_summary(healthz(args.minutes_archive_healthz)),
            "upload_hash_match": docker_env_hash_match(
                args.upload_container,
                "/app-upload/.env",
                helper.CONTAINER_VALIDATOR,
            ),
            "router_hash_match": docker_env_hash_match(
                args.router_container,
                "/app/.env.meeting-minutes",
                helper.CONTAINER_VALIDATOR,
            ),
        }
        report["ok"] = report["ok"] and all(
            bool(item.get("ok")) for item in report["checks"].values()
        )

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
