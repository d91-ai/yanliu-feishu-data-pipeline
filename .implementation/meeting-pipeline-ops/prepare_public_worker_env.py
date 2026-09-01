#!/usr/bin/env python3
"""Fill public-repository runtime paths and hashes in a disabled Worker env."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

from create_disabled_worker_env import EnvironmentError, read_dotenv, write_private_env


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WORKER_ROOT = REPOSITORY_ROOT / ".implementation" / "meeting-pipeline-worker"
PIPELINE_CONTRACT_RUNTIME_PATHS = (
    "meeting_pipeline_contract.py",
    "contract/manifest.json",
    "contract/artifact-metadata.schema.json",
    "contract/unified-base.schema.json",
)


def sha256_file(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise EnvironmentError(f"runtime file is missing or unsafe: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fixed_runtime_tree_sha256(root: Path, relative_paths: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    resolved_root = root.resolve()
    for relative in sorted(relative_paths):
        path = root / relative
        if path.is_symlink() or not path.is_file():
            raise EnvironmentError(f"runtime file is missing or unsafe: {path}")
        try:
            path.resolve().relative_to(resolved_root)
        except ValueError as exc:
            raise EnvironmentError(f"runtime path escapes its root: {path}") from exc
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
        digest.update(b"\n")
    return digest.hexdigest()


def runtime_tree_sha256(root: Path, manifest_path: Path) -> str:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EnvironmentError(f"invalid Skill manifest: {manifest_path}") from exc
    runtime_paths = manifest.get("runtime_paths") if isinstance(manifest, dict) else None
    if not isinstance(runtime_paths, list) or not runtime_paths:
        raise EnvironmentError(f"Skill manifest has no runtime paths: {manifest_path}")
    resolved_root = root.resolve()
    files: dict[str, Path] = {}
    for raw in runtime_paths:
        relative = Path(str(raw))
        if relative.is_absolute() or ".." in relative.parts:
            raise EnvironmentError(f"unsafe Skill runtime path: {raw}")
        candidate = root / relative
        candidates = [candidate] if candidate.is_file() else sorted(candidate.rglob("*"))
        for path in candidates:
            if path.is_dir() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            if path.is_symlink() or not path.is_file():
                raise EnvironmentError(f"runtime file is missing or unsafe: {path}")
            try:
                relative_name = path.resolve().relative_to(resolved_root).as_posix()
            except ValueError as exc:
                raise EnvironmentError(f"runtime path escapes its root: {path}") from exc
            files[relative_name] = path
    if not files:
        raise EnvironmentError(f"Skill runtime tree is empty: {manifest_path}")
    digest = hashlib.sha256()
    for relative_name, path in sorted(files.items()):
        digest.update(relative_name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
        digest.update(b"\n")
    return digest.hexdigest()


def prepared_values(source: dict[str, str], router_data: Path) -> dict[str, str]:
    contract_root = REPOSITORY_ROOT / ".implementation" / "meeting-pipeline-contract"
    contract_entry = contract_root / "meeting_pipeline_contract.py"
    structured_service = (
        REPOSITORY_ROOT
        / ".implementation"
        / "version-retention"
        / "feishu-structured-generate"
    )
    industry_skill = (
        REPOSITORY_ROOT
        / ".implementation"
        / "meeting-minutes-industry-market-viewpoints"
    )
    structured_skill = (
        REPOSITORY_ROOT
        / ".implementation"
        / "meeting-minutes-structured-table-current"
    )
    values = dict(source)
    values.update(
        {
            "FEISHU_UNIFIED_PIPELINE_ENABLED": "false",
            "FEISHU_GENERATION_JOB_SPOOL_PATH": str(
                router_data / "meeting-generation-jobs"
            ),
            "FEISHU_PIPELINE_REVIEW_JOB_SPOOL_DIR": str(
                router_data / "pipeline-review-jobs"
            ),
            "FEISHU_PIPELINE_WORKER_RECEIPT_DIR": str(
                router_data / "meeting-pipeline-receipts"
            ),
            "MEETING_PIPELINE_CONTRACT_PATH": str(contract_entry),
            "MEETING_PIPELINE_CONTRACT_SHA256": sha256_file(contract_entry),
            "MEETING_PIPELINE_CONTRACT_RUNTIME_SHA256": fixed_runtime_tree_sha256(
                contract_root, PIPELINE_CONTRACT_RUNTIME_PATHS
            ),
            "FEISHU_STRUCTURED_SERVICE_ROOT": str(structured_service),
            "FEISHU_STRUCTURED_SERVICE_SHA256": sha256_file(
                structured_service / "structured_generate_service.py"
            ),
            "FEISHU_STRUCTURED_SERVICE_CONTRACT_SHA256": sha256_file(
                structured_service / "skill_contract.py"
            ),
            "INDUSTRY_MARKET_SKILL_ROOT": str(industry_skill),
            "INDUSTRY_MARKET_SKILL_MANIFEST_SHA256": sha256_file(
                industry_skill / "contract" / "manifest.json"
            ),
            "INDUSTRY_MARKET_SKILL_SCRIPT_SHA256": sha256_file(
                industry_skill / "scripts" / "generate_viewpoints.py"
            ),
            "INDUSTRY_MARKET_SKILL_RUNTIME_SHA256": runtime_tree_sha256(
                industry_skill, industry_skill / "contract" / "manifest.json"
            ),
            "STRUCTURED_SKILL_ROOT": str(structured_skill),
            "STRUCTURED_SKILL_MANIFEST_SHA256": sha256_file(
                structured_skill / "contract" / "manifest.json"
            ),
            "STRUCTURED_SKILL_SCRIPT_SHA256": sha256_file(
                structured_skill / "scripts" / "generate_table.py"
            ),
            "STRUCTURED_SKILL_RUNTIME_SHA256": runtime_tree_sha256(
                structured_skill, structured_skill / "contract" / "manifest.json"
            ),
        }
    )
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-env", required=True)
    parser.add_argument("--target-env", required=True)
    parser.add_argument(
        "--router-data",
        default=str(
            REPOSITORY_ROOT
            / ".implementation"
            / "version-retention"
            / "feishu-drive-to-bitable"
            / "data"
        ),
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    router_data = Path(args.router_data).expanduser().resolve()
    values = prepared_values(read_dotenv(Path(args.source_env)), router_data)
    target = Path(args.target_env)
    if target.exists() or target.is_symlink():
        raise EnvironmentError("target environment already exists")
    if args.apply:
        write_private_env(target, values)
    print(
        json.dumps(
            {
            "status": "created" if args.apply else "dry_run_ready",
            "unified_enabled": values["FEISHU_UNIFIED_PIPELINE_ENABLED"],
            "runtime_assets": "embedded-public-repository",
            "secret_values_disclosed": False,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except EnvironmentError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from None
