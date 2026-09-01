#!/usr/bin/env python3
"""Fetch Skill source mirrors without ever modifying promoted runtime trees."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any, Mapping


class TrackerError(RuntimeError):
    pass


def read_config(path: Path) -> list[dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TrackerError("invalid Skill repository config") from exc
    repositories = value.get("repositories") if isinstance(value, dict) else None
    if value.get("schema_version") != 1 or not isinstance(repositories, list) or not repositories:
        raise TrackerError("invalid Skill repository config")
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for raw in repositories:
        if not isinstance(raw, dict):
            raise TrackerError("invalid Skill repository entry")
        item = dict(raw)
        name = str(item.get("name") or "")
        url = str(item.get("url") or "")
        branch = str(item.get("branch") or "")
        promoted = str(item.get("promoted_commit") or "").lower()
        enabled = item.get("runtime_enabled")
        if (
            not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,80}", name)
            or name in seen
            or not url.startswith("https://github.com/")
            or not url.endswith(".git")
            or not re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", branch)
            or enabled not in {True, False}
            or (promoted and not re.fullmatch(r"[0-9a-f]{40}", promoted))
            or (enabled and not promoted)
        ):
            raise TrackerError("invalid Skill repository entry")
        seen.add(name)
        result.append(item)
    return result


def git(args: list[str]) -> str:
    environment = dict(os.environ)
    environment.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "never",
            "GIT_HTTP_LOW_SPEED_LIMIT": "1",
            "GIT_HTTP_LOW_SPEED_TIME": "60",
        }
    )
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
            env=environment,
        )
    except subprocess.TimeoutExpired as exc:
        raise TrackerError("git command timed out") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise TrackerError(f"git command failed:{detail[:500]}")
    return result.stdout.strip()


def remote_head(url: str, branch: str) -> str:
    output = git(["ls-remote", url, f"refs/heads/{branch}"])
    lines = [line.split() for line in output.splitlines() if line.strip()]
    if len(lines) != 1 or len(lines[0]) != 2 or not re.fullmatch(r"[0-9a-f]{40}", lines[0][0]):
        raise TrackerError("Skill repository branch identity is ambiguous")
    return lines[0][0]


def status_for(repository: Mapping[str, Any], head: str) -> dict[str, Any]:
    promoted = str(repository.get("promoted_commit") or "")
    enabled = bool(repository.get("runtime_enabled"))
    return {
        "name": repository["name"],
        "branch": repository["branch"],
        "status": "ok",
        "remote_head": head,
        "promoted_commit": promoted or None,
        "runtime_enabled": enabled,
        "update_available": not promoted or head != promoted,
        "promotion_required": enabled and head != promoted,
    }


def update_mirror(root: Path, repository: Mapping[str, Any]) -> None:
    if not root.is_absolute() or root.is_symlink():
        raise TrackerError("mirror root must be an absolute non-symlink path")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = root / f"{repository['name']}.git"
    if target.exists():
        if target.is_symlink() or not target.is_dir():
            raise TrackerError("Skill mirror target is unsafe")
        origin = git(["--git-dir", str(target), "remote", "get-url", "origin"])
        if origin != repository["url"]:
            raise TrackerError("Skill mirror origin conflict")
        git(["--git-dir", str(target), "fetch", "--prune", "origin"])
    else:
        git(["clone", "--mirror", str(repository["url"]), str(target)])


def mirror_head(root: Path, repository: Mapping[str, Any]) -> str:
    target = root / f"{repository['name']}.git"
    head = git(
        [
            "--git-dir",
            str(target),
            "rev-parse",
            "--verify",
            f"refs/heads/{repository['branch']}",
        ]
    )
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        raise TrackerError("Skill mirror branch identity is invalid")
    return head


def write_private(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Track Skill repositories without auto-promotion")
    parser.add_argument("--config", required=True)
    parser.add_argument("--mirror-root")
    parser.add_argument("--status-output")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    repositories = read_config(Path(args.config))
    if args.apply and (not args.mirror_root or not args.status_output):
        raise TrackerError("--apply requires --mirror-root and --status-output")
    results = []
    failures = 0
    mirror_root = Path(args.mirror_root) if args.mirror_root else None
    for repository in repositories:
        try:
            if args.apply:
                assert mirror_root is not None
                update_mirror(mirror_root, repository)
                head = mirror_head(mirror_root, repository)
            else:
                head = remote_head(
                    str(repository["url"]), str(repository["branch"])
                )
            results.append(status_for(repository, head))
        except TrackerError:
            failures += 1
            results.append(
                {
                    "name": repository["name"],
                    "branch": repository["branch"],
                    "status": "error",
                    "remote_head": None,
                    "promoted_commit": repository.get("promoted_commit") or None,
                    "runtime_enabled": bool(repository.get("runtime_enabled")),
                    "update_available": None,
                    "promotion_required": None,
                    "error_code": "repository_check_failed",
                }
            )
    payload = {
        "schema_version": 1,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "mode": "mirror_updated" if args.apply else "dry_run",
        "status": "ok" if not failures else "partial_failure",
        "repositories": results,
    }
    if args.apply:
        write_private(Path(args.status_output), payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if not failures else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except TrackerError as exc:
        print(str(exc), file=os.sys.stderr)
        raise SystemExit(2) from None
