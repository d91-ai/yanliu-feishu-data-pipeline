from __future__ import annotations

import argparse
import json
from pathlib import Path
import socket
import sys
import urllib.error
import urllib.request


BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
DEFAULT_USERS_PATH = "data/upload_users.json"
LOCAL_HEALTHZ = "http://127.0.0.1:8789/healthz"
SERVICE_NAME = "feishu-minutes-upload"


def load_env(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def check_health(url: str) -> dict[str, object]:
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            body = resp.read().decode("utf-8")
            payload = json.loads(body)
            return {"ok": resp.status == 200, "status": resp.status, "payload": payload}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "status": exc.code}
    except Exception as exc:
        return {"ok": False, "error": exc.__class__.__name__}


def port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(2)
        return sock.connect_ex((host, port)) == 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Check the upload service without printing secret values.")
    parser.add_argument("--public-url", help="Explicit public health URL; omitted by default to avoid external access.")
    args = parser.parse_args()
    env = load_env(ENV_PATH)
    users_path = Path(env.get("FEISHU_UPLOAD_USERS_PATH") or DEFAULT_USERS_PATH)
    if not users_path.is_absolute():
        users_path = BASE_DIR / users_path
    app_id_set = bool(env.get("FEISHU_APP_ID", "").strip())
    app_secret_set = bool(env.get("FEISHU_APP_SECRET", "").strip())
    parent_folder_token_set = bool(env.get("FEISHU_PARENT_FOLDER_TOKEN", "").strip())
    user_count = 0
    if users_path.exists():
        try:
            user_count = len(json.loads(users_path.read_text(encoding="utf-8")).get("users", []))
        except Exception:
            user_count = -1
    local_healthz = check_health(LOCAL_HEALTHZ)
    public_healthz = check_health(args.public_url) if args.public_url else {"ok": None, "status": "not_requested"}
    status = {
        "env_file_exists": ENV_PATH.exists(),
        "app_id_set": app_id_set,
        "app_secret_set": app_secret_set,
        "parent_folder_token_set": parent_folder_token_set,
        "upload_users_path": str(users_path),
        "upload_users_readable": users_path.exists(),
        "upload_user_count": user_count,
        "local_port_8789_open": port_open("127.0.0.1", 8789),
        "local_healthz": local_healthz,
        "public_healthz": public_healthz,
    }
    print(json.dumps(status, ensure_ascii=False, indent=2))
    health_ok = bool(local_healthz["ok"] and (public_healthz["ok"] if args.public_url else True))
    service_ok = (
        local_healthz.get("payload", {}).get("service") == SERVICE_NAME
        and (not args.public_url or public_healthz.get("payload", {}).get("service") == SERVICE_NAME)
    )
    config_ok = bool(app_id_set and app_secret_set and parent_folder_token_set and users_path.exists() and user_count > 0)
    return 0 if health_ok and service_ok and config_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
