#!/usr/bin/env python3
"""Plan or remove exact local pipeline state for deleted test meetings."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any


UID_PATTERN = re.compile(r"mtg_[0-9a-f]{32}")
ALLOWED_ROUTER_PREFIXES = (
    "meeting-generation-jobs/",
    "meeting-pipeline-receipts/",
    "meeting-ingestion-receipts/",
    "event-spool/pending/",
    "event-spool/processing/",
    "event-spool/dead-letter/",
)


class CleanupError(ValueError):
    pass


def load_registry(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CleanupError("artifact_registry_invalid") from exc
    if not isinstance(value, dict) or value.get("version") != 1:
        raise CleanupError("artifact_registry_invalid")
    if not isinstance(value.get("artifacts"), dict) or not isinstance(
        value.get("review_receipts"), dict
    ):
        raise CleanupError("artifact_registry_invalid")
    return value


def prune_registry(value: dict[str, Any], meeting_uids: set[str]) -> tuple[dict[str, Any], list[str]]:
    removed: list[str] = []
    for section in ("artifacts", "review_receipts"):
        current = value[section]
        for key in list(current):
            if any(key == uid or key.startswith(uid + ":") for uid in meeting_uids):
                removed.append(f"{section}:{key}")
                del current[key]
    return value, sorted(removed)


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    encoded = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    fd, temporary_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def matching_files(root: Path, identities: set[str]) -> list[Path]:
    matches: list[Path] = []
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        if path.name.endswith(".log") or path.name == "artifact-registry.json":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if any(identity in text for identity in identities):
            matches.append(path)
    return sorted(matches)


def execute(
    router_root: Path,
    worker_root: Path,
    *,
    record_ids: set[str],
    meeting_uids: set[str],
    apply: bool,
) -> dict[str, Any]:
    if not record_ids or not meeting_uids or any(not UID_PATTERN.fullmatch(uid) for uid in meeting_uids):
        raise CleanupError("cleanup_identity_invalid")
    for root in (router_root, worker_root):
        if not root.is_absolute() or not root.is_dir() or root.is_symlink():
            raise CleanupError("cleanup_root_invalid")
    identities = record_ids | meeting_uids
    router_files = matching_files(router_root, identities)
    for path in router_files:
        relative = path.relative_to(router_root).as_posix()
        if not relative.startswith(ALLOWED_ROUTER_PREFIXES):
            raise CleanupError(f"unexpected_router_state_path:{relative}")
    worker_files = matching_files(worker_root, identities)
    work_dirs = sorted(
        {
            path.relative_to(worker_root).parts[1]
            for path in worker_files
            if len(path.relative_to(worker_root).parts) >= 3
            and path.relative_to(worker_root).parts[0] == "work"
        }
    )
    unexpected_worker = [
        path.relative_to(worker_root).as_posix()
        for path in worker_files
        if not path.relative_to(worker_root).as_posix().startswith("work/")
    ]
    if unexpected_worker:
        raise CleanupError("unexpected_worker_state_path:" + ",".join(unexpected_worker))
    registry_path = worker_root / "artifact-registry.json"
    lock_path = worker_root / "artifact-registry.json.lock"
    if not registry_path.is_file() or registry_path.is_symlink() or lock_path.is_symlink():
        raise CleanupError("artifact_registry_path_invalid")
    with lock_path.open("a+") as handle:
        os.chmod(lock_path, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        registry, removed_registry = prune_registry(load_registry(registry_path), meeting_uids)
        if apply:
            atomic_write_json(registry_path, registry)
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    if apply:
        for path in router_files:
            path.unlink()
        for name in work_dirs:
            directory = worker_root / "work" / name
            if directory.is_symlink() or directory.parent != worker_root / "work":
                raise CleanupError("work_directory_invalid")
            shutil.rmtree(directory)
    return {
        "ok": True,
        "apply": apply,
        "router_files": [path.relative_to(router_root).as_posix() for path in router_files],
        "worker_work_dirs": work_dirs,
        "registry_entries": removed_registry,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--router-data-root", type=Path, required=True)
    parser.add_argument("--worker-data-root", type=Path, required=True)
    parser.add_argument("--record-id", action="append", required=True)
    parser.add_argument("--meeting-uid", action="append", required=True)
    parser.add_argument("--apply", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    result = execute(
        args.router_data_root.resolve(),
        args.worker_data_root.resolve(),
        record_ids=set(args.record_id),
        meeting_uids=set(args.meeting_uid),
        apply=args.apply,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
