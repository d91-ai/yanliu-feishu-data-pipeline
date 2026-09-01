#!/usr/bin/env python3
"""Retired one-off Nginx mutation script.

The current repository does not own or mutate an external Nginx installation.
Route changes require a separately reviewed deployment action.
"""


def main() -> int:
    raise SystemExit("retired: external Nginx configuration was not read or changed")


if __name__ == "__main__":
    raise SystemExit(main())
