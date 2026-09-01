#!/usr/bin/env python3
"""Retired one-off production environment generator.

Use the current `.implementation/version-retention/feishu-structured-generate`
`.env.example` and Docker Compose definition. This historical entry point is
kept only so old runbooks fail with a clear, non-writing result.
"""


def main() -> int:
    raise SystemExit(
        "retired: create configuration from the current version-retention template; no files were changed"
    )


if __name__ == "__main__":
    raise SystemExit(main())
