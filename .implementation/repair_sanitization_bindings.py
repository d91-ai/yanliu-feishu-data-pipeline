#!/usr/bin/env python3
"""Apply hash-bound, manifest-driven text repairs to a local UTF-8 artifact.

The tool contains no meeting-specific record IDs, hashes, names, or correction
rules.  A deployer creates a private manifest for an approved repair, keeps that
manifest outside Git, previews the result, and must pass ``--apply`` to write it.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import tempfile
import os
from typing import Any


class RepairError(RuntimeError):
    pass


@dataclass(frozen=True)
class Replacement:
    kind: str
    before: str
    after: str
    expected_count: int


@dataclass(frozen=True)
class RepairManifest:
    source_sha256: str
    output_sha256: str
    replacements: tuple[Replacement, ...]


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def required_hash(value: Any, label: str) -> str:
    text = str(value or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", text):
        raise RepairError(f"{label} must be a SHA-256 hex digest")
    return text


def load_manifest(path: Path) -> RepairManifest:
    if path.is_symlink() or not path.is_file():
        raise RepairError("repair manifest is missing or unsafe")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RepairError("repair manifest is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise RepairError("unsupported repair manifest schema")
    raw_replacements = payload.get("replacements")
    if not isinstance(raw_replacements, list) or not raw_replacements:
        raise RepairError("repair manifest requires replacements")
    replacements: list[Replacement] = []
    for index, item in enumerate(raw_replacements):
        if not isinstance(item, dict):
            raise RepairError(f"replacement {index} must be an object")
        kind = str(item.get("kind") or "literal")
        before = str(item.get("before") or "")
        after = str(item.get("after") or "")
        expected_count = item.get("expected_count")
        if kind not in {"literal", "regex"} or not before:
            raise RepairError(f"replacement {index} is invalid")
        if not isinstance(expected_count, int) or expected_count < 1:
            raise RepairError(f"replacement {index} requires a positive expected_count")
        replacements.append(Replacement(kind, before, after, expected_count))
    return RepairManifest(
        source_sha256=required_hash(payload.get("source_sha256"), "source_sha256"),
        output_sha256=required_hash(payload.get("output_sha256"), "output_sha256"),
        replacements=tuple(replacements),
    )


def normalize_source(content: bytes, manifest: RepairManifest) -> bytes:
    if sha256_bytes(content) != manifest.source_sha256:
        raise RepairError("source bytes do not match the approved hash")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RepairError("source is not UTF-8") from exc
    for item in manifest.replacements:
        if item.kind == "literal":
            count = text.count(item.before)
            if count != item.expected_count:
                raise RepairError("reviewed literal replacement count changed")
            text = text.replace(item.before, item.after)
        else:
            text, count = re.subn(item.before, item.after, text)
            if count != item.expected_count:
                raise RepairError("reviewed regex replacement count changed")
    output = text.encode("utf-8")
    if sha256_bytes(output) != manifest.output_sha256:
        raise RepairError("output bytes do not match the approved hash")
    return output


def write_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".repair-", dir=str(path.parent))
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    manifest = load_manifest(args.manifest)
    source = args.input.read_bytes()
    output = normalize_source(source, manifest)
    if args.apply:
        write_atomic(args.output, output)
    print(json.dumps({
        "ok": True,
        "dry_run": not args.apply,
        "source_sha256": sha256_bytes(source),
        "output_sha256": sha256_bytes(output),
        "replacement_count": len(manifest.replacements),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RepairError as exc:
        raise SystemExit(str(exc)) from None
