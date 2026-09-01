#!/usr/bin/env python3
"""Local readiness checks for the investment meeting-minutes workflow."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import struct
import subprocess
import sys
import tempfile
import wave
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_WORKSPACE_ROOT = (
    Path(os.environ["INVESTMENT_MINUTES_WORKSPACE"]).expanduser()
    if os.environ.get("INVESTMENT_MINUTES_WORKSPACE")
    else Path.home() / "Documents/会议纪要整理"
)
VAULT_DIR = DEFAULT_WORKSPACE_ROOT
RAW_DIR = VAULT_DIR / "00 Inbox/会议原始记录"
MINUTES_DIR = VAULT_DIR / "01 Projects/会议纪要"
DEFAULT_ASR_RUNTIME_PYTHON_VALUE = os.environ.get("SENSEVOICE_PYTHON", "").strip()
DEFAULT_DOCUMENT_PYTHON_VALUE = os.environ.get("INVESTMENT_MINUTES_PYTHON", "").strip()

ASR_RUNTIME_MODULES = [
    ("Python 依赖: funasr", "funasr"),
    ("Python 依赖: modelscope", "modelscope"),
    ("Python 依赖: soundfile", "soundfile"),
    ("Python 依赖: librosa", "librosa"),
]


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def check(status: str, name: str, message: str, **details: Any) -> dict[str, Any]:
    item: dict[str, Any] = {"status": status, "name": name, "message": message}
    if details:
        item["details"] = details
    return item


def run_command(args: list[str], *, cwd: Path | None = None, timeout: int = 20) -> tuple[int, str, str]:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        result = subprocess.run(args, cwd=cwd, text=True, capture_output=True, timeout=timeout, env=env)
    except FileNotFoundError as exc:
        return 127, "", str(exc)
    except subprocess.TimeoutExpired as exc:
        return 124, exc.stdout or "", exc.stderr or "command timed out"
    return result.returncode, result.stdout, result.stderr


def path_check(name: str, path: Path, *, writable: bool = False, create_if_missing: bool = False) -> dict[str, Any]:
    try:
        if create_if_missing:
            path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return check("error", name, f"无法创建目录: {exc}", path=str(path))
    if not path.exists():
        return check(
            "error",
            name,
            "路径不存在；请设置 INVESTMENT_MINUTES_WORKSPACE 指向实际工作区，或确认路径后使用 --prepare-local-dirs 创建",
            path=str(path),
            workspace_root=str(VAULT_DIR),
        )
    if writable and not os.access(path, os.W_OK):
        return check("error", name, "路径不可写", path=str(path))
    if path.is_dir():
        return check("ok", name, "目录存在" + ("且可写" if writable else ""), path=str(path))
    return check("ok", name, "文件存在", path=str(path), size=path.stat().st_size)


def asr_runtime_python() -> Path:
    candidates = [
        Path(DEFAULT_ASR_RUNTIME_PYTHON_VALUE).expanduser() if DEFAULT_ASR_RUNTIME_PYTHON_VALUE else None,
        Path.home() / "Documents/会议纪要整理/.transcribe-venv/bin/python",
        Path.home() / "Documents/会议纪要整理/.transcribe-venv/bin/python3",
        Path.home() / "Documents/Codex/asr-runtimes/funasr-speaker-venv/bin/python",
    ]
    for candidate in candidates:
        if candidate and candidate.exists():
            return candidate
    return Path(sys.executable)


def document_runtime_python() -> Path:
    if DEFAULT_DOCUMENT_PYTHON_VALUE:
        candidate = Path(DEFAULT_DOCUMENT_PYTHON_VALUE).expanduser()
        if candidate.exists():
            return candidate
    return Path(sys.executable)


def python_module_check(
    name: str,
    module: str,
    *,
    strict: bool,
    python_executable: Path | None = None,
    optional: bool = False,
) -> dict[str, Any]:
    python_path = python_executable or Path(sys.executable)
    if python_path == Path(sys.executable) and importlib.util.find_spec(module):
        return check("ok", name, "模块可导入", module=module, python=str(python_path))
    probe = "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec(sys.argv[1]) else 1)"
    returncode, stdout, stderr = run_command([str(python_path), "-c", probe, module], timeout=10)
    if returncode == 0:
        return check("ok", name, "模块可导入", module=module, python=str(python_path))
    status = "warning" if optional or not strict else "error"
    return check(
        status,
        name,
        "模块不可导入；运行期禁止临时安装依赖",
        module=module,
        python=str(python_path),
        stderr=stderr.strip() or stdout.strip(),
    )


def asr_model_cache_check(*, strict: bool) -> dict[str, Any]:
    script = SCRIPT_DIR / "transcribe_audio.py"
    if not script.exists():
        return check("error", "ASR 模型缓存", "转写脚本不存在", path=str(script))
    returncode, stdout, stderr = run_command([sys.executable, str(script), "--check-model-cache"], timeout=30)
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        status = "error" if strict else "warning"
        return check(
            status,
            "ASR 模型缓存",
            f"模型缓存检查输出不是 JSON: {exc}",
            stdout=stdout[-2000:],
            stderr=stderr[-2000:],
        )

    models = payload.get("models") if isinstance(payload.get("models"), dict) else {}
    required_model_names = {"sensevoice", "paraformer", "vad"}
    required_incomplete = {
        name: model if isinstance(model, dict) else {"complete": False, "missing": ["not reported"]}
        for name in required_model_names
        for model in [models.get(name)]
        if not (isinstance(model, dict) and model.get("complete"))
    }
    if returncode == 0 and not required_incomplete:
        return check(
            "ok",
            "ASR 模型缓存",
            "SenseVoice 主模型、Paraformer 辅助模型和 fsmn-vad 模型缓存完整",
            cache_root=payload.get("cache_root"),
            models=models,
        )
    status = "error" if strict else "warning"
    return check(
        status,
        "ASR 模型缓存",
        "SenseVoice/Paraformer/fsmn-vad 模型缓存不完整；运行期禁止远程查找或下载模型",
        cache_root=payload.get("cache_root"),
        incomplete=required_incomplete,
        stderr=stderr.strip(),
    )


def sensevoice_service_model_cache_check(*, strict: bool) -> dict[str, Any]:
    name = "SenseVoice 服务模型缓存"
    request = Request("http://127.0.0.1:8765/health", headers={"User-Agent": "investment-workflow-health/1.0"})
    try:
        with urlopen(request, timeout=5) as response:
            payload = json.loads(response.read(128 * 1024).decode("utf-8", errors="replace"))
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        status = "error" if strict else "warning"
        return check(status, name, f"无法读取服务健康状态: {exc}", url="http://127.0.0.1:8765/health")

    model_cache = payload.get("model_cache") if isinstance(payload.get("model_cache"), dict) else {}
    models = model_cache.get("models") if isinstance(model_cache.get("models"), dict) else {}
    required_model_names = {"sensevoice", "paraformer", "vad"}
    required_incomplete = {
        model_name: model if isinstance(model, dict) else {"complete": False, "missing": ["not reported"]}
        for model_name in required_model_names
        for model in [models.get(model_name)]
        if not (isinstance(model, dict) and model.get("complete"))
    }
    if payload.get("ok") is True and models and not required_incomplete:
        return check(
            "ok",
            name,
            "服务使用的 SenseVoice 主模型、Paraformer 辅助模型和 fsmn-vad 模型缓存完整",
            cache_root=model_cache.get("cache_root"),
            python=model_cache.get("python"),
        )
    status = "error" if strict else "warning"
    return check(
        status,
        name,
        "服务使用的 SenseVoice/Paraformer/fsmn-vad 模型缓存不完整；运行期禁止远程下载模型",
        cache_root=model_cache.get("cache_root"),
        python=model_cache.get("python"),
        incomplete=required_incomplete,
    )


def write_smoke_wav(path: Path, *, seconds: float = 0.6, sample_rate: int = 16000) -> None:
    frame_count = int(seconds * sample_rate)
    amplitude = 8000
    frequency = 440.0
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        frames = bytearray()
        for index in range(frame_count):
            sample = int(amplitude * math.sin(2 * math.pi * frequency * index / sample_rate))
            frames.extend(struct.pack("<h", sample))
        handle.writeframes(bytes(frames))


def sensevoice_service_smoke_check(*, strict: bool) -> dict[str, Any]:
    name = "SenseVoice 服务转写 smoke"
    boundary = "----investment-meeting-smoke-boundary"
    with tempfile.TemporaryDirectory(prefix="meeting-asr-smoke-") as tmp:
        audio_path = Path(tmp) / "smoke.wav"
        write_smoke_wav(audio_path)
        audio_bytes = audio_path.read_bytes()
        body = b"".join(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                b'Content-Disposition: form-data; name="audio"; filename="smoke.wav"\r\n',
                b"Content-Type: audio/wav\r\n\r\n",
                audio_bytes,
                f"\r\n--{boundary}--\r\n".encode("utf-8"),
            ]
        )
    request = Request(
        "http://127.0.0.1:8765/transcribe",
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": "investment-workflow-health/1.0",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=120) as response:
            payload = json.loads(response.read(256 * 1024).decode("utf-8", errors="replace"))
    except HTTPError as exc:
        try:
            payload = json.loads(exc.read(256 * 1024).decode("utf-8", errors="replace"))
        except Exception:
            payload = {"error": str(exc)}
        status = "error" if strict else "warning"
        return check(status, name, f"HTTP {exc.code}", url="http://127.0.0.1:8765/transcribe", payload=payload)
    except Exception as exc:  # noqa: BLE001
        status = "error" if strict else "warning"
        return check(status, name, f"转写 smoke 失败: {exc}", url="http://127.0.0.1:8765/transcribe")

    if payload.get("ok") is True:
        return check(
            "ok",
            name,
            "服务完成真实转写调用",
            engine=payload.get("engine"),
            model=payload.get("model"),
            timestamp_detected=payload.get("timestamp_detected"),
            auxiliary_ok=payload.get("auxiliary_ok"),
            auxiliary_status=payload.get("auxiliary_status"),
            text_preview=str(payload.get("text") or "")[:80],
        )
    status = "error" if strict else "warning"
    return check(status, name, "服务返回 ok=false", payload=payload)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def utf8_roundtrip_check() -> dict[str, Any]:
    text = "# UTF-8 检查\n\n中文内容\n"
    with tempfile.TemporaryDirectory(prefix="meeting-doc-ready-") as tmp:
        path = Path(tmp) / "utf8.md"
        path.write_text(text, encoding="utf-8", newline="\n")
        read_back = path.read_text(encoding="utf-8")
    if read_back == text:
        return check("ok", "TXT/MD UTF-8", "UTF-8 读写正常", sha256=sha256_text(read_back))
    return check("error", "TXT/MD UTF-8", "UTF-8 往返内容不一致")


def temp_dir_check() -> dict[str, Any]:
    try:
        with tempfile.TemporaryDirectory(prefix="meeting-minutes-ready-") as tmp:
            path = Path(tmp)
            probe = path / "probe.txt"
            probe.write_text("ok\n", encoding="utf-8")
            if probe.read_text(encoding="utf-8") == "ok\n":
                return check("ok", "临时目录权限", "可创建并读写临时文件", path=str(path))
    except OSError as exc:
        return check("error", "临时目录权限", f"临时目录不可用: {exc}")
    return check("error", "临时目录权限", "临时目录读写结果异常")


def markdown_validator_check() -> dict[str, Any]:
    script = SCRIPT_DIR / "validate_meeting_minutes_contract.py"
    if not script.exists():
        return check("error", "Markdown validator", "校验脚本不存在", path=str(script))
    returncode, stdout, stderr = run_command([sys.executable, str(script), "--help"], timeout=10)
    if returncode == 0:
        return check("ok", "Markdown validator", "校验脚本可运行", path=str(script))
    return check("error", "Markdown validator", "校验脚本 --help 失败", path=str(script), stderr=stderr.strip() or stdout.strip())


def local_exporter_check() -> dict[str, Any]:
    script = SCRIPT_DIR / "export_to_obsidian.py"
    if not script.exists():
        return check("error", "本地 Markdown 导出器", "导出脚本不存在", path=str(script))
    returncode, stdout, stderr = run_command([sys.executable, str(script), "--help"], timeout=10)
    if returncode == 0:
        return check("ok", "本地 Markdown 导出器", "导出脚本可运行", path=str(script))
    return check("error", "本地 Markdown 导出器", "导出脚本 --help 失败", path=str(script), stderr=stderr.strip() or stdout.strip())


def atomic_no_clobber_check(path: Path) -> dict[str, Any]:
    """Verify the destination supports same-directory hard-link publication."""
    if not path.is_dir():
        return check("error", "安全原子发布", "本地输出目录不存在或不是目录", path=str(path))
    source_path: Path | None = None
    target_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=".meeting-minutes-link-probe-",
            suffix=".part",
            dir=path,
            delete=False,
        ) as source:
            source.write(b"atomic-link-probe\n")
            source_path = Path(source.name)
        target_path = source_path.with_name(f"{source_path.name}.published")
        os.link(source_path, target_path)
        if target_path.read_bytes() != b"atomic-link-probe\n":
            return check("error", "安全原子发布", "hard-link 探针内容不一致", path=str(path))
        return check("ok", "安全原子发布", "目标目录支持同目录 hard-link 原子无覆盖发布", path=str(path))
    except OSError as exc:
        return check(
            "error",
            "安全原子发布",
            f"目标目录不支持安全的 hard-link 原子无覆盖发布: {exc}",
            path=str(path),
        )
    finally:
        for probe_path in (target_path, source_path):
            if probe_path is not None:
                try:
                    probe_path.unlink(missing_ok=True)
                except OSError:
                    pass


def asr_checks(strict: bool, *, runtime_smoke: bool) -> list[dict[str, Any]]:
    runtime_python = asr_runtime_python()
    checks = [
        python_module_check(name, module, strict=strict, python_executable=runtime_python)
        for name, module in ASR_RUNTIME_MODULES
    ]
    checks.append(asr_model_cache_check(strict=strict))
    checks.append(sensevoice_service_model_cache_check(strict=strict))
    if runtime_smoke:
        checks.append(sensevoice_service_smoke_check(strict=strict))
    return checks


def document_checks(strict: bool, *, prepare_local_dirs: bool = False) -> list[dict[str, Any]]:
    document_python = document_runtime_python()
    checks = [
        utf8_roundtrip_check(),
        python_module_check("Python 依赖: python-docx", "docx", strict=strict, python_executable=document_python),
        temp_dir_check(),
        path_check("原始输入目录", RAW_DIR, writable=True, create_if_missing=prepare_local_dirs),
        path_check("本地输出目录", MINUTES_DIR, writable=True, create_if_missing=prepare_local_dirs),
    ]
    return checks


def export_checks(strict: bool, *, prepare_local_dirs: bool = False) -> list[dict[str, Any]]:
    return [
        path_check("本地输出目录", MINUTES_DIR, writable=True, create_if_missing=prepare_local_dirs),
        atomic_no_clobber_check(MINUTES_DIR),
        markdown_validator_check(),
        local_exporter_check(),
    ]


def collect_checks(
    *,
    strict: bool = False,
    runtime_smoke: bool = False,
    profile: str = "full",
    prepare_local_dirs: bool = False,
) -> list[dict[str, Any]]:
    profile = (profile or "full").strip().lower()
    if profile == "asr":
        return asr_checks(strict=strict, runtime_smoke=runtime_smoke)
    if profile == "document":
        return document_checks(strict=strict, prepare_local_dirs=prepare_local_dirs)
    if profile == "export":
        return export_checks(strict=strict, prepare_local_dirs=prepare_local_dirs)
    checks: list[dict[str, Any]] = []
    checks.extend(asr_checks(strict=strict, runtime_smoke=runtime_smoke))
    checks.extend(document_checks(strict=strict, prepare_local_dirs=prepare_local_dirs))
    checks.extend(export_checks(strict=strict, prepare_local_dirs=prepare_local_dirs))
    return checks


def summarize(checks: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {"ok": 0, "warning": 0, "error": 0}
    for item in checks:
        status = str(item.get("status") or "warning")
        counts[status] = counts.get(status, 0) + 1
    if counts.get("error", 0):
        overall = "error"
    elif counts.get("warning", 0):
        overall = "warning"
    else:
        overall = "ok"
    return {"overall": overall, "counts": counts}


def print_text_report(report: dict[str, Any]) -> None:
    summary = report["summary"]
    print(f"整体状态: {summary['overall'].upper()}")
    print(f"生成时间: {report['generated_at']}")
    print(f"检查项: OK {summary['counts'].get('ok', 0)} / WARNING {summary['counts'].get('warning', 0)} / ERROR {summary['counts'].get('error', 0)}")
    print()
    for item in report["checks"]:
        label = str(item["status"]).upper()
        print(f"[{label}] {item['name']}: {item['message']}")
        details = item.get("details") or {}
        for key in ("path", "url", "cache_root", "python", "stderr"):
            if details.get(key):
                print(f"  - {key}: {details[key]}")


def main() -> int:
    global VAULT_DIR, RAW_DIR, MINUTES_DIR

    parser = argparse.ArgumentParser(description="检查投资会议纪要本地整理、校验和导出 readiness")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument(
        "--workspace-root",
        default=str(DEFAULT_WORKSPACE_ROOT),
        help=f"本地会议纪要工作区根目录，默认 {DEFAULT_WORKSPACE_ROOT}",
    )
    parser.add_argument(
        "--profile",
        choices=["asr", "document", "export", "full"],
        default="full",
        help="检查范围；默认 full。日常音频建议 asr，文稿处理建议 document，导出前建议 export",
    )
    parser.add_argument("--strict", action="store_true", help="严格检查本地依赖和 ASR 模型缓存；缺失即失败，不触发下载")
    parser.add_argument("--runtime-smoke", action="store_true", help="额外调用 SenseVoice 服务执行真实短音频转写；耗时较长")
    parser.add_argument("--prepare-local-dirs", action="store_true", help="首次部署时显式创建本地输入/输出目录；默认只检查不创建")
    args = parser.parse_args()

    VAULT_DIR = Path(args.workspace_root).expanduser().resolve()
    RAW_DIR = VAULT_DIR / "00 Inbox/会议原始记录"
    MINUTES_DIR = VAULT_DIR / "01 Projects/会议纪要"

    checks = collect_checks(
        strict=args.strict,
        runtime_smoke=args.runtime_smoke,
        profile=args.profile,
        prepare_local_dirs=args.prepare_local_dirs,
    )
    report = {
        "generated_at": now_iso(),
        "profile": args.profile,
        "strict": bool(args.strict),
        "runtime_smoke": bool(args.runtime_smoke),
        "prepare_local_dirs": bool(args.prepare_local_dirs),
        "workspace_root": str(VAULT_DIR),
        "summary": summarize(checks),
        "checks": checks,
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_text_report(report)
    return 1 if report["summary"]["overall"] == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
