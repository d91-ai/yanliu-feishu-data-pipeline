#!/usr/bin/env python3
"""Enable the pinned meeting contract only on the meeting-minutes router."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading


CONTAINER_VALIDATOR = "/skills/investment-meeting-minutes/scripts/validate_meeting_minutes_contract.py"
_THREAD_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.Lock] = {}


def transaction_lock_path(paths: list[Path]) -> Path:
    material = "\0".join(sorted(str(path.expanduser().resolve(strict=False)) for path in paths))
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return Path(tempfile.gettempdir()) / f"meeting-contract-config-{digest}.lock"


@contextmanager
def exclusive_transaction_lock(paths: list[Path]):
    lock_path = transaction_lock_path(paths)
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


def replace_or_append(text: str, key: str, value: str) -> str:
    pattern = rf"(?m)^{re.escape(key)}=[^\r\n]*$"
    line = f"{key}={value}"
    if re.search(pattern, text):
        return re.sub(pattern, line, text)
    return text.rstrip() + "\n" + line + "\n"


def render_update(path: Path, updates: dict[str, str]) -> tuple[str, str, int]:
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError(f"configuration file is missing or unsafe: {path}")
    original = path.read_text(encoding="utf-8")
    updated = original
    for key, value in updates.items():
        updated = replace_or_append(updated, key, value)
    mode = path.stat().st_mode & 0o777
    return original, updated, mode


def stage_content(path: Path, content: str, mode: int) -> Path:
    fd = -1
    temporary: Path | None = None
    try:
        fd, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        return temporary
    except Exception:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise
    finally:
        if fd >= 0:
            os.close(fd)


def fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def update_files_with_rollback(updates_by_path: list[tuple[Path, dict[str, str]]]) -> None:
    with exclusive_transaction_lock([path for path, _updates in updates_by_path]):
        _update_files_with_rollback_unlocked(updates_by_path)


def _update_files_with_rollback_unlocked(updates_by_path: list[tuple[Path, dict[str, str]]]) -> None:
    plans = [(path, *render_update(path, updates)) for path, updates in updates_by_path]
    staged: list[tuple[Path, Path, str, int]] = []
    try:
        for path, original, updated, mode in plans:
            staged.append((path, stage_content(path, updated, mode), original, mode))
    except Exception:
        for _path, temporary, _original, _mode in staged:
            temporary.unlink(missing_ok=True)
        raise

    committed: list[tuple[Path, str, int]] = []
    try:
        for path, temporary, original, mode in staged:
            os.replace(temporary, path)
            committed.append((path, original, mode))
            fsync_directory(path.parent)
    except Exception as commit_exc:
        rollback_errors: list[str] = []
        for path, original, mode in reversed(committed):
            rollback: Path | None = None
            try:
                rollback = stage_content(path, original, mode)
                os.replace(rollback, path)
                fsync_directory(path.parent)
            except Exception:
                rollback_errors.append(str(path))
            finally:
                if rollback is not None:
                    rollback.unlink(missing_ok=True)
        for _path, temporary, _original, _mode in staged:
            temporary.unlink(missing_ok=True)
        if rollback_errors:
            raise RuntimeError("configuration rollback failed for prevalidated files") from commit_exc
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service-dir", type=Path, required=True)
    parser.add_argument("--upload-env", type=Path, required=True)
    parser.add_argument("--skill-dir", type=Path, required=True)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="prevalidate and update four explicit env files with rollback on a reported commit failure",
    )
    args = parser.parse_args()

    service_dir = args.service_dir.expanduser().resolve()
    upload_env = args.upload_env.expanduser().resolve()
    skill_dir = args.skill_dir.expanduser().resolve()
    host_env = service_dir / ".env"
    meeting_env = service_dir / ".env.meeting-minutes"
    structured_env = service_dir / ".env.structured"
    validator = skill_dir / "scripts/validate_meeting_minutes_contract.py"
    if not args.apply:
        print(
            json.dumps(
                {
                    "ok": True,
                    "dry_run": True,
                    "planned_files": [str(host_env), str(meeting_env), str(structured_env), str(upload_env)],
                }
            )
        )
        return 0

    if skill_dir.is_symlink() or validator.is_symlink() or not validator.is_file():
        raise SystemExit("installed meeting-minutes validator is unavailable")
    digest = hashlib.sha256(validator.read_bytes()).hexdigest()
    update_files_with_rollback(
        [
            (host_env, {"MEETING_MINUTES_SKILL_HOST_DIR": str(skill_dir)}),
            (
                meeting_env,
                {
                    "FEISHU_MEETING_CONTRACT_ENABLED": "true",
                    "FEISHU_MEETING_CONTRACT_VALIDATOR": CONTAINER_VALIDATOR,
                    "FEISHU_MEETING_CONTRACT_VALIDATOR_SHA256": digest,
                },
            ),
            (structured_env, {"FEISHU_MEETING_CONTRACT_ENABLED": "false"}),
            (
                upload_env,
                {
                    "FEISHU_MEETING_CONTRACT_ENABLED": "true",
                    "FEISHU_MEETING_CONTRACT_VALIDATOR": CONTAINER_VALIDATOR,
                    "FEISHU_MEETING_CONTRACT_VALIDATOR_SHA256": digest,
                },
            ),
        ]
    )
    print(
        json.dumps(
            {
                "ok": True,
                "validator_sha256": digest,
                "meeting_route": True,
                "structured_route": False,
                "upload_service": True,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
