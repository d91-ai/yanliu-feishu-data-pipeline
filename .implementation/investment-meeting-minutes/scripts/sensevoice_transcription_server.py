#!/usr/bin/env python3
"""Tiny local HTTP bridge for SenseVoice transcription."""

from __future__ import annotations

import argparse
import email.message
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from email.parser import BytesParser
from email.policy import default as email_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
SCRIPT_DIR = Path(__file__).resolve().parent
TRANSCRIBE_SCRIPT = SCRIPT_DIR / "transcribe_audio.py"
DEFAULT_PRIMARY_ENGINE = "sensevoice"
DEFAULT_PRIMARY_MODEL = "iic/SenseVoiceSmall"
DEFAULT_AUXILIARY_ENGINE = "paraformer"
DEFAULT_AUXILIARY_MODEL = "iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch"
DEFAULT_VAD_MODEL = "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch"
DEFAULT_MODEL_CACHE = os.environ.get(
    "SENSEVOICE_MODEL_CACHE",
    os.environ.get(
        "FUNASR_MODEL_CACHE",
        str(Path.home() / "Documents/Codex/asr-model-cache"),
    ),
)
DEFAULT_TRANSCRIBE_PYTHON = os.environ.get("SENSEVOICE_PYTHON", "")


def _positive_env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


MAX_UPLOAD_BYTES = _positive_env_int("SENSEVOICE_MAX_UPLOAD_BYTES", 64 * 1024 * 1024)
MAX_CONCURRENT_TRANSCRIPTIONS = _positive_env_int("SENSEVOICE_MAX_CONCURRENT_TRANSCRIPTIONS", 1)
TRANSCRIBE_SEMAPHORE = threading.BoundedSemaphore(MAX_CONCURRENT_TRANSCRIPTIONS)

LOG_DIR = Path(os.environ.get("RESEARCHPIPELINE_SYNC_LOG_DIR", str(Path.home() / "Library/Logs/researchpipeline-sync")))
MODEL_REQUIREMENTS = {
    "sensevoice": ("iic/SenseVoiceSmall", ("config.yaml", "model.pt")),
    "paraformer": (DEFAULT_AUXILIARY_MODEL, ("config.yaml", "model.pt")),
    "vad": (DEFAULT_VAD_MODEL, ("config.yaml", "model.pt")),
}


class UploadedFormFile:
    def __init__(self, filename: str, data: bytes) -> None:
        self.filename = filename
        self.file = io.BytesIO(data)


class MultipartForm:
    def __init__(self) -> None:
        self.fields: dict[str, object] = {}
        self.files: dict[str, object] = {}

    def add_field(self, key: str, value: str) -> None:
        _append_form_value(self.fields, key, value)

    def add_file(self, key: str, value: UploadedFormFile) -> None:
        _append_form_value(self.files, key, value)

    def __contains__(self, key: str) -> bool:
        return key in self.fields or key in self.files

    def __getitem__(self, key: str) -> object:
        value = self.files[key] if key in self.files else self.fields[key]
        return value[0] if isinstance(value, list) else value

    def getvalue(self, key: str) -> object:
        return self.fields.get(key, "")


def _append_form_value(store: dict[str, object], key: str, value: object) -> None:
    current = store.get(key)
    if current is None:
        store[key] = value
    elif isinstance(current, list):
        current.append(value)
    else:
        store[key] = [current, value]


def _parse_multipart_form(content_type: str, body: bytes) -> MultipartForm:
    type_header = email.message.Message(policy=email_policy)
    type_header["Content-Type"] = content_type
    if type_header.get_content_type().lower() != "multipart/form-data" or not type_header.get_param(
        "boundary", header="content-type"
    ):
        raise ValueError("invalid_multipart")
    message = BytesParser(policy=email_policy).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8") + body
    )
    form = MultipartForm()
    if not message.is_multipart():
        return form
    for part in message.iter_parts():
        if part.get_content_disposition() != "form-data":
            continue
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        payload = part.get_payload(decode=True) or b""
        filename = part.get_filename()
        if filename:
            if name != "audio" or name in form.files:
                raise ValueError("exactly_one_audio_file_required")
            form.add_file(name, UploadedFormFile(filename, payload))
            continue
        if name in form.fields:
            raise ValueError("duplicate_form_field")
        charset = part.get_content_charset() or "utf-8"
        form.add_field(name, payload.decode(charset, errors="replace"))
    return form


def require_audio_form_file(form: MultipartForm) -> UploadedFormFile:
    file_item = form["audio"] if "audio" in form else None
    if isinstance(file_item, list) or file_item is None or not getattr(file_item, "filename", None):
        raise ValueError("audio field required")
    if not hasattr(file_item, "file"):
        raise ValueError("audio field must be a file upload")
    return file_item


def _model_cache_status() -> dict:
    root = Path(DEFAULT_MODEL_CACHE).expanduser()
    models = {}
    for name, (relative_path, required_files) in MODEL_REQUIREMENTS.items():
        candidates = [
            root / "modelscope" / "models" / relative_path,
            root / "modelscope" / relative_path,
            root / "models" / relative_path,
            root / relative_path,
            root / "hub" / "models" / relative_path,
            root / "hub" / relative_path,
        ]
        path = next(
            (
                item
                for item in candidates
                if item.exists() and all((item / required).exists() for required in required_files)
            ),
            candidates[0],
        )
        missing = [item for item in required_files if not (path / item).exists()]
        models[name] = {"complete": path.exists() and not missing}
    return {
        "ready": TRANSCRIBE_SCRIPT.is_file() and all(item["complete"] for item in models.values()),
        "transcribe_script": TRANSCRIBE_SCRIPT.is_file(),
        "models": models,
    }


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _clean_filename(filename: str | None) -> str:
    if not filename:
        return "audio_upload"
    keep = []
    for char in filename:
        keep.append(char if char.isalnum() or char in ".-_ " else "_")
    return "".join(keep).strip() or "audio_upload"


def _field_text(form: MultipartForm, key: str, default: str = "") -> str:
    if key not in form:
        return default
    value = form.getvalue(key)
    if isinstance(value, list):
        value = value[0] if value else default
    return str(value or default).strip()


def _read_transcript_txt(output_dir: Path, stem: str) -> str:
    text_path = output_dir / f"{stem}.txt"
    return text_path.read_text(encoding="utf-8", errors="replace").strip() if text_path.exists() else ""


def _read_transcript_json(output_dir: Path, stem: str) -> dict:
    json_path = output_dir / f"{stem}.json"
    if not json_path.exists():
        return {}
    try:
        data = json.loads(json_path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _run_transcribe(command: list[str], output_dir: Path, stem: str, timeout: int = 7200) -> tuple[bool, str, str]:
    env = dict(os.environ)
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("SENSEVOICE_MODEL_CACHE", DEFAULT_MODEL_CACHE)
    env.setdefault("FUNASR_MODEL_CACHE", DEFAULT_MODEL_CACHE)
    env.setdefault("MODELSCOPE_CACHE", str(Path(DEFAULT_MODEL_CACHE).expanduser() / "modelscope"))
    env.setdefault("HF_HOME", str(Path(DEFAULT_MODEL_CACHE).expanduser() / "huggingface"))
    try:
        completed = subprocess.run(command, text=True, capture_output=True, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "", "transcription_timeout"
    except OSError:
        return False, "", "transcription_process_unavailable"
    text = _read_transcript_txt(output_dir, stem)
    error = ""
    if completed.returncode != 0:
        error = "transcription_process_failed"
    elif not text:
        error = "transcription_output_missing"
    return completed.returncode == 0 and bool(text), text, error


class SenseVoiceHandler(BaseHTTPRequestHandler):
    server_version = "SenseVoiceBridge/1.0"

    def log_message(self, fmt: str, *args) -> None:
        return

    def do_GET(self) -> None:
        if self.path.rstrip("/") == "/health":
            model_cache = _model_cache_status()
            _json_response(
                self,
                200 if model_cache["ready"] else 503,
                {
                    "ok": model_cache["ready"],
                    "ready": model_cache["ready"],
                    "engine": DEFAULT_PRIMARY_ENGINE,
                    "model": DEFAULT_PRIMARY_MODEL,
                    "primary_engine": DEFAULT_PRIMARY_ENGINE,
                    "primary_model": DEFAULT_PRIMARY_MODEL,
                    "auxiliary_engine": DEFAULT_AUXILIARY_ENGINE,
                    "auxiliary_model": DEFAULT_AUXILIARY_MODEL,
                    "max_upload_bytes": MAX_UPLOAD_BYTES,
                    "max_concurrent_transcriptions": MAX_CONCURRENT_TRANSCRIPTIONS,
                    "model_cache": model_cache,
                },
            )
            return
        _json_response(self, 404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        if self.path.rstrip("/") != "/transcribe":
            _json_response(self, 404, {"ok": False, "error": "not found"})
            return
        if not TRANSCRIBE_SEMAPHORE.acquire(blocking=False):
            _json_response(self, 503, {"ok": False, "error": "transcription service is busy", "text": ""})
            return
        try:
            self._handle_transcribe()
        finally:
            TRANSCRIBE_SEMAPHORE.release()

    def _handle_transcribe(self) -> None:
        if self.headers.get_all("Transfer-Encoding", []):
            _json_response(self, 400, {"ok": False, "error": "transfer_encoding_not_supported", "text": ""})
            return
        length_values = self.headers.get_all("Content-Length", [])
        if len(length_values) != 1:
            _json_response(self, 400, {"ok": False, "error": "invalid_content_length", "text": ""})
            return
        try:
            content_length = int(length_values[0])
        except ValueError:
            _json_response(self, 400, {"ok": False, "error": "invalid_content_length", "text": ""})
            return
        if content_length <= 0:
            _json_response(self, 400, {"ok": False, "error": "invalid_content_length", "text": ""})
            return
        if content_length > MAX_UPLOAD_BYTES + 1024 * 1024:
            _json_response(
                self,
                413,
                {"ok": False, "error": "request_too_large", "text": ""},
            )
            return

        type_values = self.headers.get_all("Content-Type", [])
        if len(type_values) != 1:
            _json_response(self, 415, {"ok": False, "error": "invalid_content_type", "text": ""})
            return
        content_type = type_values[0]

        try:
            body = self.rfile.read(content_length)
            if len(body) != content_length:
                raise ValueError("incomplete_request_body")
            form = _parse_multipart_form(content_type, body)
        except ValueError as exc:
            code = str(exc) if str(exc) in {
                "incomplete_request_body",
                "invalid_multipart",
                "exactly_one_audio_file_required",
                "duplicate_form_field",
            } else "invalid_multipart"
            _json_response(self, 400, {"ok": False, "error": code, "text": ""})
            return
        try:
            file_item = require_audio_form_file(form)
        except ValueError as exc:
            _json_response(self, 400, {"ok": False, "error": str(exc), "text": ""})
            return

        filename = _clean_filename(file_item.filename)
        if len(file_item.file.getbuffer()) > MAX_UPLOAD_BYTES:
            _json_response(self, 413, {"ok": False, "error": "audio_too_large", "text": ""})
            return
        suffix = Path(filename).suffix or ".audio"
        with tempfile.TemporaryDirectory(prefix="sensevoice-local-") as tmp:
            tmp_dir = Path(tmp)
            audio_path = tmp_dir / f"input{suffix}"
            with audio_path.open("wb") as audio_handle:
                shutil.copyfileobj(file_item.file, audio_handle, length=1024 * 1024)
            file_item.file.close()
            del file_item
            del form
            primary_dir = tmp_dir / "primary"
            primary_dir.mkdir()

            command = [
                DEFAULT_TRANSCRIBE_PYTHON
                if DEFAULT_TRANSCRIBE_PYTHON and Path(DEFAULT_TRANSCRIBE_PYTHON).exists()
                else sys.executable,
                str(TRANSCRIBE_SCRIPT),
                str(audio_path),
                "--engine",
                "sensevoice",
                "--output-dir",
                str(primary_dir),
                "--output-format",
                "all",
                "--language",
                "zh",
            ]
            if DEFAULT_MODEL_CACHE:
                command.extend(["--cache-dir", DEFAULT_MODEL_CACHE])
            primary_ok, text, primary_error = _run_transcribe(command, primary_dir, "input")
            primary_json = _read_transcript_json(primary_dir, "input")
            timestamp_segments = primary_json.get("sentence_info") if isinstance(primary_json.get("sentence_info"), list) else []
            speaker_segments = [item for item in timestamp_segments if isinstance(item, dict) and item.get("speaker")]
            payload = {
                "ok": primary_ok,
                "engine": "sensevoice",
                "model": DEFAULT_PRIMARY_MODEL,
                "primary_engine": "sensevoice",
                "primary_model": DEFAULT_PRIMARY_MODEL,
                "filename": filename,
                "text": text,
                "timestamp_detected": bool(primary_json.get("timestamp_detected")),
                "speakers": primary_json.get("speakers") or [],
                "speaker_segments": speaker_segments,
                "timestamp_segments": timestamp_segments,
                "sentence_info": timestamp_segments,
                "timestamp_index": primary_json.get("timestamp_index") or [],
                # The bridge temp directory is removed before the response is
                # consumed; callers must use the inline timestamp_index.
                "timestamp_index_path": "",
                "timestamp_index_source": primary_json.get("timestamp_index_source") or "",
                "auxiliary_engine": primary_json.get("auxiliary_engine") or "",
                "auxiliary_model": primary_json.get("auxiliary_model") or "",
                "auxiliary_text": primary_json.get("auxiliary_text") or "",
                "auxiliary_ok": bool(primary_json.get("auxiliary_ok")),
                "auxiliary_status": primary_json.get("auxiliary_status") or "",
                "asr_comparison_diff": primary_json.get("asr_comparison_diff") or "",
                "auxiliary_transcripts": {
                    "paraformer": primary_json.get("auxiliary_text") or "",
                }
                if primary_json.get("auxiliary_text")
                else {},
            }
            if not primary_ok:
                payload["error"] = primary_error or "SenseVoice transcription failed"
                _json_response(self, 500, payload)
                return
            _json_response(self, 200, payload)


def main() -> int:
    parser = argparse.ArgumentParser(description="启动本地 SenseVoice 转写 HTTP bridge")
    parser.add_argument("--host", default=os.environ.get("SENSEVOICE_BRIDGE_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SENSEVOICE_BRIDGE_PORT", str(DEFAULT_PORT))))
    parser.add_argument(
        "--allow-non-loopback",
        action="store_true",
        help="explicitly allow a non-loopback bind; the bridge has no HTTP authentication",
    )
    args = parser.parse_args()

    host = args.host
    port = args.port
    if host not in {"127.0.0.1", "::1", "localhost"} and not args.allow_non_loopback:
        raise SystemExit("non-loopback bind requires explicit --allow-non-loopback")
    server = ThreadingHTTPServer((host, port), SenseVoiceHandler)
    print(f"SenseVoice bridge listening on http://{host}:{port}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
