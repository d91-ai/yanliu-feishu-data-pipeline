#!/usr/bin/env python3
"""Retired one-off active-environment updater.

Use the current service `.env.example` and reviewed deployment configuration.
The repository no longer edits a fixed external runtime path.
"""


def main() -> int:
    raise SystemExit("retired: update the reviewed runtime configuration explicitly; no files were changed")


if __name__ == "__main__":
    raise SystemExit(main())
