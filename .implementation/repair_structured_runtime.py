#!/usr/bin/env python3
"""Disabled compatibility entry point for superseded structured runtimes.

The active implementation is under `.implementation/version-retention/` and
must be deployed through an explicitly reviewed process.
"""


def main() -> int:
    raise SystemExit("disabled: compatibility runtime repair performs no changes")


if __name__ == "__main__":
    raise SystemExit(main())
