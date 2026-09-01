#!/usr/bin/env python3
"""Atomically update only the sanitizer skill revision and script hash."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", type=Path, required=True)
    parser.add_argument("--source-repo", type=Path, required=True)
    parser.add_argument("--installed-script", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--apply", action="store_true", help="atomically update the explicit env file")
    args = parser.parse_args()

    revision = args.revision.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise SystemExit("revision must be a full Git commit hash")
    actual_revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=args.source_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if actual_revision != revision:
        raise SystemExit("source repository revision mismatch")
    if subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=args.source_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip():
        raise SystemExit("source repository is not clean")

    script_hash = hashlib.sha256(args.installed_script.read_bytes()).hexdigest()
    env_text = args.env.read_text(encoding="utf-8")
    revision_pattern = r"(?m)^SANITIZE_SKILL_SOURCE_REVISION=[^\r\n]*$"
    hash_pattern = r"(?m)^SANITIZE_SKILL_SCRIPT_SHA256=[^\r\n]*$"
    if len(re.findall(revision_pattern, env_text)) != 1 or len(re.findall(hash_pattern, env_text)) != 1:
        raise SystemExit("runtime environment has an invalid skill pin shape")
    updated = re.sub(revision_pattern, f"SANITIZE_SKILL_SOURCE_REVISION={revision}", env_text)
    updated = re.sub(hash_pattern, f"SANITIZE_SKILL_SCRIPT_SHA256={script_hash}", updated)

    if not args.apply:
        print(json.dumps({"ok": True, "dry_run": True, "revision": revision, "script_sha256": script_hash}))
        return 0

    mode = args.env.stat().st_mode & 0o777
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=args.env.parent,
        prefix=f".{args.env.name}.",
        delete=False,
    ) as handle:
        handle.write(updated)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.chmod(temporary, mode)
    temporary.replace(args.env)
    print(json.dumps({"ok": True, "dry_run": False, "revision": revision, "script_sha256": script_hash}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
