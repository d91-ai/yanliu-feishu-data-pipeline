#!/usr/bin/env python3
"""Create a reviewable Worker env that changes only the unified enable flag."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from create_disabled_worker_env import EnvironmentError, read_dotenv, write_private_env


def enabled_values(source: dict[str, str]) -> dict[str, str]:
    if source.get("FEISHU_UNIFIED_PIPELINE_ENABLED") != "false":
        raise EnvironmentError("source Worker environment is not explicitly disabled")
    result = dict(source)
    result["FEISHU_UNIFIED_PIPELINE_ENABLED"] = "true"
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-env", required=True)
    parser.add_argument("--target-env", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    source = read_dotenv(Path(args.source_env))
    target = enabled_values(source)
    target_path = Path(args.target_env)
    if target_path.exists() or target_path.is_symlink():
        raise EnvironmentError("enabled Worker environment already exists")
    if args.apply:
        write_private_env(target_path, target)
    changed = [key for key in target if target.get(key) != source.get(key)]
    if changed != ["FEISHU_UNIFIED_PIPELINE_ENABLED"]:
        raise EnvironmentError("enabled Worker environment changed unexpected keys")
    print(
        json.dumps(
            {
                "status": "created" if args.apply else "dry_run_ready",
                "changed_keys": changed,
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
        print(str(exc), file=__import__("sys").stderr)
        raise SystemExit(2) from None
