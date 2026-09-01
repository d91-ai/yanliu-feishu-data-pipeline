#!/usr/bin/env python3
"""Disabled compatibility entry point for record-specific baseline repair.

Use the manifest-driven baseline tools with explicit external-write
authorization; this entry point intentionally contains no record identifiers.
"""


def main() -> int:
    raise SystemExit("disabled: use a reviewed manifest-driven repair; no writes occurred")


if __name__ == "__main__":
    raise SystemExit(main())
