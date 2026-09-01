#!/usr/bin/env python3
"""Create the isolated runtime configuration without printing secret values."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import threading
from typing import Any, Mapping, Sequence

from skill_adapter import APPROVED_SKILL_PINS


RESOURCE_KEYS = (
    "bitable_app_token",
    "source_table_id",
    "target_table_id",
    "pending_root_folder_token",
    "archive_root_folder_token",
    "version_root_folder_token",
)
_THREAD_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.Lock] = {}


class ConfigError(RuntimeError):
    pass


def parse_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'").strip('"')
    return values


def ensure_runtime_secrets(source_env: Path, output_dir: Path) -> dict[str, Any]:
    # Use the same bundle lock as a full apply so init-only and full applies
    # cannot race while reading or creating the shared HTTP token.
    paths = runtime_bundle_paths(output_dir)
    with exclusive_runtime_lock(paths):
        plan = plan_runtime_secrets(source_env, output_dir)
        app_secret_path = Path(plan["app_secret_path"])
        http_token_path = Path(plan["http_token_path"])
        files = [
            (app_secret_path, str(plan["app_secret"]) + "\n", 0o600),
            (http_token_path, str(plan["http_token"]) + "\n", 0o600),
        ]
        _commit_runtime_files_unlocked(files)
        return {
            "app_id": str(plan["app_id"]),
            "app_secret_path": app_secret_path,
            "http_token_path": http_token_path,
        }


def plan_runtime_secrets(source_env: Path, output_dir: Path) -> dict[str, Any]:
    source = parse_dotenv(source_env)
    app_id = source.get("FEISHU_APP_ID", "").strip()
    app_secret = source.get("FEISHU_APP_SECRET", "").strip()
    if not app_id or not app_secret:
        raise ConfigError("source environment is missing FEISHU_APP_ID or FEISHU_APP_SECRET")
    secret_dir = output_dir / "secrets"
    http_token_path = secret_dir / "workflow-http-token.txt"
    if http_token_path.exists():
        if http_token_path.is_symlink():
            raise ConfigError("existing workflow HTTP token path must not be a symbolic link")
        http_token = http_token_path.read_text(encoding="utf-8").strip()
        if len(http_token) < 32:
            raise ConfigError("existing workflow HTTP token is invalid; refusing to replace it implicitly")
        write_http_token = False
    else:
        http_token = secrets.token_urlsafe(48)
        write_http_token = True
    return {
        "app_id": app_id,
        "app_secret": app_secret,
        "app_secret_path": secret_dir / "feishu-app-secret.txt",
        "http_token": http_token,
        "http_token_path": http_token_path,
        "write_http_token": write_http_token,
    }


def fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def runtime_bundle_paths(output_dir: Path, *, include_env: bool = True) -> list[Path]:
    secret_dir = output_dir / "secrets"
    paths = [secret_dir / "feishu-app-secret.txt", secret_dir / "workflow-http-token.txt"]
    if include_env:
        paths.append(output_dir / ".env")
    return paths


def runtime_lock_path(paths: list[Path]) -> Path:
    material = "\0".join(sorted(str(path.expanduser().resolve(strict=False)) for path in paths))
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return Path(tempfile.gettempdir()) / f"sanitize-runtime-config-{digest}.lock"


@contextmanager
def exclusive_runtime_lock(paths: list[Path]):
    lock_path = runtime_lock_path(paths)
    key = str(lock_path)
    with _THREAD_LOCKS_GUARD:
        thread_lock = _THREAD_LOCKS.setdefault(key, threading.Lock())
    with thread_lock:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def stage_private_text(path: Path, content: str, mode: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    if path.is_symlink():
        raise ConfigError("refusing to replace a symbolic-link runtime file")
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        if fd >= 0:
            os.close(fd)
    return temporary


def commit_runtime_files(files: list[tuple[Path, str, int]]) -> None:
    with exclusive_runtime_lock([path for path, _content, _mode in files]):
        _commit_runtime_files_unlocked(files)


def _commit_runtime_files_unlocked(files: list[tuple[Path, str, int]]) -> None:
    snapshots: list[tuple[Path, str | None, int | None]] = []
    staged: list[tuple[Path, Path]] = []
    try:
        for path, content, mode in files:
            if path.is_symlink():
                raise ConfigError("refusing to replace a symbolic-link runtime file")
            if path.exists():
                snapshots.append((path, path.read_text(encoding="utf-8"), path.stat().st_mode & 0o777))
            else:
                snapshots.append((path, None, None))
            staged.append((path, stage_private_text(path, content, mode)))
    except Exception:
        for _path, temporary in staged:
            temporary.unlink(missing_ok=True)
        raise

    committed = 0
    try:
        for path, temporary in staged:
            os.replace(temporary, path)
            committed += 1
            fsync_directory(path.parent)
    except Exception as commit_exc:
        rollback_failed = False
        for path, original, original_mode in reversed(snapshots[:committed]):
            rollback: Path | None = None
            try:
                if original is None:
                    path.unlink(missing_ok=True)
                    fsync_directory(path.parent)
                else:
                    rollback = stage_private_text(path, original, int(original_mode or 0o600))
                    os.replace(rollback, path)
                    fsync_directory(path.parent)
            except Exception:
                rollback_failed = True
            finally:
                if rollback is not None:
                    rollback.unlink(missing_ok=True)
        for _path, temporary in staged:
            temporary.unlink(missing_ok=True)
        if rollback_failed:
            raise ConfigError("runtime configuration commit and rollback both failed") from commit_exc
        raise


def clean_value(name: str, value: str) -> str:
    normalized = value.strip()
    if not normalized or "\n" in normalized or "\r" in normalized:
        raise ConfigError(f"{name} is missing or invalid")
    return normalized


def validate_cutoff(value: str) -> str:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise ConfigError("source cutoff must use a valid YYYY-MM-DD HH:MM value") from exc
    normalized = parsed.strftime("%Y-%m-%d %H:%M")
    if normalized != value:
        raise ConfigError("source cutoff must use a valid YYYY-MM-DD HH:MM value")
    return normalized


def render_env(
    *,
    app_id: str,
    output_dir: Path,
    skill_host_dir: Path,
    skill_source_revision: str,
    source_cutoff: str,
    resources: Mapping[str, str],
) -> str:
    values = {key: clean_value(key, str(resources.get(key, ""))) for key in RESOURCE_KEYS}
    cutoff = validate_cutoff(source_cutoff)
    untrusted_skill_dir = skill_host_dir.expanduser()
    if untrusted_skill_dir.is_symlink() or not untrusted_skill_dir.is_dir():
        raise ConfigError("skill host directory does not exist")
    if not untrusted_skill_dir.is_absolute():
        untrusted_skill_dir = (Path.cwd() / untrusted_skill_dir).absolute()
    skill_dir_path = untrusted_skill_dir.resolve()
    skill_dir = str(skill_dir_path)
    revision = skill_source_revision.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ConfigError("skill source revision must be a full Git commit hash")
    skill_script = skill_dir_path / "scripts" / "sanitize_minutes.py"
    if skill_script.parent.is_symlink() or skill_script.is_symlink() or not skill_script.is_file():
        raise ConfigError("skill script does not exist or is not a regular file")
    try:
        script_sha256 = hashlib.sha256(skill_script.read_bytes()).hexdigest()
    except OSError as exc:
        raise ConfigError("skill script is not readable") from exc
    if APPROVED_SKILL_PINS.get(revision) != script_sha256:
        raise ConfigError("skill revision and script SHA256 are not an approved pair")
    secret_dir = output_dir.resolve() / "secrets"
    lines = [
        f"FEISHU_APP_ID={clean_value('FEISHU_APP_ID', app_id)}",
        f"FEISHU_SANITIZE_BITABLE_APP_TOKEN={values['bitable_app_token']}",
        f"FEISHU_SANITIZE_SOURCE_TABLE_ID={values['source_table_id']}",
        f"FEISHU_SANITIZE_TARGET_TABLE_ID={values['target_table_id']}",
        f"FEISHU_SANITIZE_PENDING_ROOT_FOLDER_TOKEN={values['pending_root_folder_token']}",
        f"FEISHU_SANITIZE_ARCHIVE_ROOT_FOLDER_TOKEN={values['archive_root_folder_token']}",
        f"FEISHU_SANITIZE_VERSION_ROOT_FOLDER_TOKEN={values['version_root_folder_token']}",
        f"FEISHU_SANITIZE_SOURCE_CUTOFF={cutoff}",
        f"FEISHU_APP_SECRET_HOST_FILE={secret_dir / 'feishu-app-secret.txt'}",
        f"FEISHU_SANITIZE_HTTP_TOKEN_HOST_FILE={secret_dir / 'workflow-http-token.txt'}",
        f"SANITIZE_SKILL_HOST_DIR={skill_dir}",
        'SANITIZE_SKILL_COMMAND_JSON=["python","/skills/meeting-minutes-sanitizer/scripts/sanitize_minutes.py"]',
        "SANITIZE_SKILL_CONTRACT_VERSION=minute-sanitization/v2",
        f"SANITIZE_SKILL_SOURCE_REVISION={revision}",
        f"SANITIZE_SKILL_SCRIPT_SHA256={script_sha256}",
        "SANITIZE_SKILL_TIMEOUT_SECONDS=180",
        "SANITIZE_MAX_INPUT_BYTES=10485760",
        "FEISHU_SANITIZE_HTTP_HOST=127.0.0.1",
        "FEISHU_SANITIZE_HTTP_PORT=8791",
        "FEISHU_LOG_LEVEL=INFO",
    ]
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-env", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--init-secrets-only", action="store_true")
    parser.add_argument("--skill-host-dir", type=Path)
    parser.add_argument("--skill-source-revision")
    parser.add_argument("--source-cutoff")
    parser.add_argument("--apply", action="store_true", help="create secrets and runtime env; default is a local plan")
    for key in RESOURCE_KEYS:
        parser.add_argument("--" + key.replace("_", "-"), dest=key)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        source_env = args.source_env.expanduser().resolve()
        output_dir = args.output_dir.expanduser().resolve()
        if not args.apply:
            print(
                json.dumps(
                    {
                        "ok": True,
                        "dry_run": True,
                        "source_env": str(source_env),
                        "output_dir": str(output_dir),
                        "secrets_initialized": False,
                        "env_written": False,
                    }
                )
            )
            return 0
        if args.init_secrets_only:
            ensure_runtime_secrets(source_env, output_dir)
            print(json.dumps({"ok": True, "dry_run": False, "secrets_initialized": True, "env_written": False}))
            return 0
        resources = {key: getattr(args, key) or "" for key in RESOURCE_KEYS}
        if args.skill_host_dir is None or not args.skill_source_revision or not args.source_cutoff:
            raise ConfigError("skill host directory, source revision, and source cutoff are required")
        with exclusive_runtime_lock(runtime_bundle_paths(output_dir)):
            secrets_state = plan_runtime_secrets(source_env, output_dir)
            content = render_env(
                app_id=str(secrets_state["app_id"]),
                output_dir=output_dir,
                skill_host_dir=args.skill_host_dir,
                skill_source_revision=args.skill_source_revision,
                source_cutoff=args.source_cutoff,
                resources=resources,
            )
            files = [
                (Path(secrets_state["app_secret_path"]), str(secrets_state["app_secret"]) + "\n", 0o600),
                (Path(secrets_state["http_token_path"]), str(secrets_state["http_token"]) + "\n", 0o600),
                (output_dir / ".env", content, 0o600),
            ]
            _commit_runtime_files_unlocked(files)
        print(json.dumps({"ok": True, "dry_run": False, "secrets_initialized": True, "env_written": True}))
        return 0
    except (OSError, UnicodeError, ConfigError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
