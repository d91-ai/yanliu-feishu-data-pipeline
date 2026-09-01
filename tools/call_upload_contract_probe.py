#!/usr/bin/env python3
"""Call the local upload API without exposing the per-user bearer token."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request
import uuid


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8789/api/upload")
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--date", default="2032-07-13")
    args = parser.parse_args()

    token = args.token_file.read_text(encoding="utf-8").strip()
    content = args.input.read_bytes()
    boundary = "codex-" + uuid.uuid4().hex
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{args.input.name}"\r\n'
        "Content-Type: text/markdown\r\n\r\n"
    ).encode("utf-8") + content + f"\r\n--{boundary}--\r\n".encode("utf-8")
    query = urllib.parse.urlencode({"dry_run": "true", "date": args.date})
    request = urllib.request.Request(
        args.url + "?" + query,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status = response.status
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        status = exc.code
        payload = json.loads(exc.read().decode("utf-8"))
    safe = {
        "http_status": status,
        "ok": bool(payload.get("ok")),
        "status": payload.get("status"),
        "error_code": payload.get("error_code"),
    }
    print(json.dumps(safe, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
