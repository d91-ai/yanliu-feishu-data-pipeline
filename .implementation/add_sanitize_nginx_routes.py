#!/usr/bin/env python3
"""Check or install the minute-sanitization Nginx routes.

The script updates both the live Dify Nginx configuration and its persistent
template.  It never prints configuration contents or credentials.
"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import shutil


CONFIG_PATHS: tuple[Path, ...] = ()

ANCHOR = """    location = /feishu-structured/generate-official-json {
      proxy_pass http://host.docker.internal:8790/generate-official-json;
      include proxy.conf;
    }
"""

ROUTES = """

    location = /feishu-sanitize/healthz {
      proxy_pass http://host.docker.internal:8791/healthz;
      include proxy.conf;
    }

    location = /feishu-sanitize/generate-review-md {
      proxy_pass http://host.docker.internal:8791/generate-review-md;
      include proxy.conf;
    }

    location = /feishu-sanitize/archive-review-md {
      proxy_pass http://host.docker.internal:8791/archive-review-md;
      include proxy.conf;
    }
"""

LEGACY_ROUTES = ROUTES + """
    location = /feishu-sanitize/generate-official-json {
      proxy_pass http://host.docker.internal:8791/generate-official-json;
      include proxy.conf;
    }
"""

ROUTE_MARKERS = (
    "/feishu-sanitize/healthz",
    "/feishu-sanitize/generate-review-md",
    "/feishu-sanitize/archive-review-md",
)

LEGACY_ROUTE_MARKER = "/feishu-sanitize/generate-official-json"
SANITIZE_ROUTE_MARKER = "/feishu-sanitize/"
SANITIZE_LOCATION_PREFIX = "    location = /feishu-sanitize/"


def planned_text(current: str) -> tuple[str, str]:
    legacy_count = current.count(LEGACY_ROUTES)
    route_count = current.count(ROUTES)
    sanitize_locations = current.count(SANITIZE_LOCATION_PREFIX)
    sanitize_markers = current.count(SANITIZE_ROUTE_MARKER)

    if legacy_count:
        if legacy_count != 1 or sanitize_locations != 4 or sanitize_markers != 4:
            raise ValueError("ambiguous legacy sanitization route set found")
        return current.replace(LEGACY_ROUTES, ROUTES, 1), "needs_migration"

    if route_count:
        if route_count != 1 or sanitize_locations != 3 or sanitize_markers != 3:
            raise ValueError("ambiguous sanitization route set found")
        return current, "already_present"

    if sanitize_markers:
        raise ValueError("partial or ambiguous sanitization route set found")
    if current.count(ANCHOR) != 1:
        raise ValueError("structured official JSON route anchor is missing or ambiguous")
    return current.replace(ANCHOR, ANCHOR + ROUTES, 1), "needs_install"


def check_paths(paths: tuple[Path, ...] | None = None) -> list[tuple[Path, str, str]]:
    selected = CONFIG_PATHS if paths is None else paths
    if not selected:
        raise ValueError("at least one explicit Nginx configuration path is required")
    checked: list[tuple[Path, str, str]] = []
    for path in selected:
        current = path.read_text(encoding="utf-8")
        updated, status = planned_text(current)
        checked.append((path, updated, status))
    statuses = {status for _, _, status in checked}
    if len(statuses) != 1:
        raise ValueError("live and template route states differ")
    return checked


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        type=Path,
        action="append",
        required=True,
        help="explicit Nginx configuration path; repeat for live and persistent template",
    )
    parser.add_argument("--apply", action="store_true", help="write both configurations after validation")
    args = parser.parse_args()

    checked = check_paths(tuple(path.expanduser().resolve() for path in args.config))
    if not args.apply:
        for path, _, status in checked:
            print(f"{path}: {status}")
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for path, updated, status in checked:
        if status == "already_present":
            print(f"{path}: already_present")
            continue
        backup = path.with_name(path.name + f".bak-sanitize-{stamp}")
        shutil.copy2(path, backup)
        path.write_text(updated, encoding="utf-8")
        print(f"{path}: installed backup={backup.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
