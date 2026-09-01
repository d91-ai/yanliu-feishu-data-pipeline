#!/usr/bin/env python3
"""Disabled-by-default service entrypoint for the unified meeting worker."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import signal
import threading
from typing import Any, Mapping

from feishu_backend import (
    FeishuBackendConfig,
    FeishuPipelineBackend,
    load_runtime_modules,
)
from unified_pipeline_worker import (
    CandidateArtifactGenerator,
    PipelineJobError,
)
from unified_pipeline_worker import QueueWorker, durable_replace


MODULE_ROOT = Path(__file__).resolve().parent
REPOSITORY_ROOT = MODULE_ROOT.parents[1]
QUEUE_STATES = ("pending", "processing", "done", "failed", "stale")
RETRYABLE_STATES = ("failed", "stale")
PIPELINE_CONTRACT_RUNTIME_PATHS = (
    "meeting_pipeline_contract.py",
    "contract/manifest.json",
    "contract/artifact-metadata.schema.json",
    "contract/unified-base.schema.json",
)


def _path(value: str, default: Path) -> Path:
    path = Path(value).expanduser() if value else default
    return path if path.is_absolute() else MODULE_ROOT / path


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    if path.is_symlink() or not path.is_file():
        raise PipelineJobError("worker_env_file_invalid")
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise PipelineJobError("worker_env_key_invalid")
        os.environ.setdefault(key, value.strip().strip("'").strip('"'))


@dataclass(frozen=True)
class WorkerServiceConfig:
    review_job_root: Path
    receipt_root: Path
    worker_lock_path: Path
    work_root: Path
    pipeline_contract_path: Path
    structured_service_root: Path
    industry_skill_root: Path
    structured_skill_root: Path
    speaker_master_path: Path
    pipeline_contract_sha256: str
    pipeline_contract_runtime_sha256: str
    structured_service_sha256: str
    structured_service_contract_sha256: str
    industry_manifest_sha256: str
    industry_script_sha256: str
    industry_runtime_sha256: str
    structured_manifest_sha256: str
    structured_script_sha256: str
    structured_runtime_sha256: str
    codex_bin: str
    model: str
    reasoning_effort: str
    timeout_seconds: int
    poll_seconds: float
    http_host: str
    http_port: int

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "WorkerServiceConfig":
        values = dict(os.environ if env is None else env)

        def integer(name: str, default: int, minimum: int, maximum: int) -> int:
            try:
                value = int(str(values.get(name) or default))
            except ValueError as exc:
                raise PipelineJobError("worker_config_invalid", name) from exc
            if value < minimum or value > maximum:
                raise PipelineJobError("worker_config_invalid", name)
            return value

        def digest(name: str) -> str:
            value = str(values.get(name) or "").strip().lower()
            if not re.fullmatch(r"[0-9a-f]{64}", value):
                raise PipelineJobError("worker_config_invalid", name)
            return value

        reasoning_effort = str(
            values.get("FEISHU_PIPELINE_MODEL_REASONING_EFFORT") or "medium"
        ).strip().lower()
        if reasoning_effort not in {"low", "medium", "high", "xhigh", "max", "ultra"}:
            raise PipelineJobError(
                "worker_config_invalid", "FEISHU_PIPELINE_MODEL_REASONING_EFFORT"
            )

        try:
            poll = float(str(values.get("FEISHU_PIPELINE_WORKER_POLL_SECONDS") or "2"))
        except ValueError as exc:
            raise PipelineJobError(
                "worker_config_invalid", "FEISHU_PIPELINE_WORKER_POLL_SECONDS"
            ) from exc
        if poll < 0.1 or poll > 300:
            raise PipelineJobError(
                "worker_config_invalid", "FEISHU_PIPELINE_WORKER_POLL_SECONDS"
            )

        return cls(
            review_job_root=_path(
                str(values.get("FEISHU_PIPELINE_REVIEW_JOB_SPOOL_DIR") or ""),
                MODULE_ROOT / "data" / "pipeline-review-jobs",
            ),
            receipt_root=_path(
                str(values.get("FEISHU_PIPELINE_WORKER_RECEIPT_DIR") or ""),
                MODULE_ROOT / "data" / "meeting-pipeline-receipts",
            ),
            worker_lock_path=_path(
                str(values.get("FEISHU_PIPELINE_WORKER_LOCK_PATH") or ""),
                MODULE_ROOT / "data" / "unified-worker.lock",
            ),
            work_root=_path(
                str(values.get("FEISHU_PIPELINE_WORK_DIR") or ""),
                MODULE_ROOT / "data" / "work",
            ),
            pipeline_contract_path=_path(
                str(values.get("MEETING_PIPELINE_CONTRACT_PATH") or ""),
                REPOSITORY_ROOT
                / ".implementation"
                / "meeting-pipeline-contract"
                / "meeting_pipeline_contract.py",
            ),
            structured_service_root=_path(
                str(values.get("FEISHU_STRUCTURED_SERVICE_ROOT") or ""),
                REPOSITORY_ROOT
                / ".implementation"
                / "version-retention"
                / "feishu-structured-generate",
            ),
            industry_skill_root=_path(
                str(values.get("INDUSTRY_MARKET_SKILL_ROOT") or ""),
                REPOSITORY_ROOT
                / ".implementation"
                / "meeting-minutes-industry-market-viewpoints",
            ),
            structured_skill_root=_path(
                str(values.get("STRUCTURED_SKILL_ROOT") or ""),
                REPOSITORY_ROOT
                / ".implementation"
                / "meeting-minutes-structured-table-current",
            ),
            speaker_master_path=_path(
                str(values.get("SPEAKER_MASTER_PATH") or ""),
                REPOSITORY_ROOT / "data" / "speaker_identity" / "speaker_master.csv",
            ),
            pipeline_contract_sha256=digest("MEETING_PIPELINE_CONTRACT_SHA256"),
            pipeline_contract_runtime_sha256=digest(
                "MEETING_PIPELINE_CONTRACT_RUNTIME_SHA256"
            ),
            structured_service_sha256=digest(
                "FEISHU_STRUCTURED_SERVICE_SHA256"
            ),
            structured_service_contract_sha256=digest(
                "FEISHU_STRUCTURED_SERVICE_CONTRACT_SHA256"
            ),
            industry_manifest_sha256=digest(
                "INDUSTRY_MARKET_SKILL_MANIFEST_SHA256"
            ),
            industry_script_sha256=digest("INDUSTRY_MARKET_SKILL_SCRIPT_SHA256"),
            industry_runtime_sha256=digest(
                "INDUSTRY_MARKET_SKILL_RUNTIME_SHA256"
            ),
            structured_manifest_sha256=digest("STRUCTURED_SKILL_MANIFEST_SHA256"),
            structured_script_sha256=digest("STRUCTURED_SKILL_SCRIPT_SHA256"),
            structured_runtime_sha256=digest("STRUCTURED_SKILL_RUNTIME_SHA256"),
            codex_bin=str(values.get("FEISHU_PIPELINE_CODEX_BIN") or "codex").strip(),
            model=str(
                values.get("FEISHU_PIPELINE_MODEL") or "codex-cli-default"
            ).strip(),
            reasoning_effort=reasoning_effort,
            timeout_seconds=integer(
                "FEISHU_PIPELINE_MODEL_TIMEOUT_SECONDS", 1800, 30, 3600
            ),
            poll_seconds=poll,
            http_host=str(
                values.get("FEISHU_PIPELINE_WORKER_HTTP_HOST") or "127.0.0.1"
            ).strip(),
            http_port=integer("FEISHU_PIPELINE_WORKER_HTTP_PORT", 8792, 1, 65535),
        )

    def validate_assets(self) -> None:
        pipeline_contract_root = self.pipeline_contract_path.parent
        pipeline_contract_files = tuple(
            pipeline_contract_root / relative
            for relative in PIPELINE_CONTRACT_RUNTIME_PATHS
        )
        structured_service = (
            self.structured_service_root / "structured_generate_service.py"
        )
        structured_service_contract = self.structured_service_root / "skill_contract.py"
        industry_script = self.industry_skill_root / "scripts" / "generate_viewpoints.py"
        industry_manifest = self.industry_skill_root / "contract" / "manifest.json"
        structured_manifest = self.structured_skill_root / "contract" / "manifest.json"
        structured_script = self.structured_skill_root / "scripts" / "generate_table.py"
        files = (
            *pipeline_contract_files,
            structured_service,
            structured_service_contract,
            industry_script,
            industry_manifest,
            self.industry_skill_root / "contract" / "semantic_prompt.md",
            self.industry_skill_root / "contract" / "claim_units.schema.json",
            structured_manifest,
            structured_script,
        )
        missing = [str(path) for path in files if not path.is_file() or path.is_symlink()]
        if missing:
            raise PipelineJobError("worker_asset_missing", ",".join(missing))
        expected = (
            (self.pipeline_contract_path, self.pipeline_contract_sha256),
            (structured_service, self.structured_service_sha256),
            (
                structured_service_contract,
                self.structured_service_contract_sha256,
            ),
            (industry_manifest, self.industry_manifest_sha256),
            (industry_script, self.industry_script_sha256),
            (structured_manifest, self.structured_manifest_sha256),
            (structured_script, self.structured_script_sha256),
        )
        drifted = [
            str(path)
            for path, digest in expected
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest
        ]
        if drifted:
            raise PipelineJobError("worker_asset_hash_mismatch", ",".join(drifted))
        if (
            fixed_runtime_tree_sha256(
                pipeline_contract_root, PIPELINE_CONTRACT_RUNTIME_PATHS
            )
            != self.pipeline_contract_runtime_sha256
        ):
            raise PipelineJobError(
                "worker_contract_runtime_hash_mismatch",
                str(pipeline_contract_root),
            )
        runtime_drifted = []
        for root, manifest, digest in (
            (
                self.industry_skill_root,
                industry_manifest,
                self.industry_runtime_sha256,
            ),
            (
                self.structured_skill_root,
                structured_manifest,
                self.structured_runtime_sha256,
            ),
        ):
            if runtime_tree_sha256(root, manifest) != digest:
                runtime_drifted.append(str(root))
        if runtime_drifted:
            raise PipelineJobError(
                "worker_skill_runtime_hash_mismatch", ",".join(runtime_drifted)
            )
        if "/" in self.codex_bin:
            codex = Path(self.codex_bin)
            available = codex.is_file() and os.access(codex, os.X_OK)
        else:
            available = shutil.which(self.codex_bin) is not None
        if not available:
            raise PipelineJobError("worker_codex_missing")


def fixed_runtime_tree_sha256(root: Path, relative_paths: tuple[str, ...]) -> str:
    resolved_root = root.resolve()
    digest = hashlib.sha256()
    for relative in sorted(relative_paths):
        path = root / relative
        if path.is_symlink() or not path.is_file():
            raise PipelineJobError("worker_asset_missing", str(path))
        try:
            path.resolve().relative_to(resolved_root)
        except ValueError as exc:
            raise PipelineJobError("worker_asset_missing", str(path)) from exc
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
        digest.update(b"\n")
    return digest.hexdigest()


def runtime_tree_sha256(skill_root: Path, manifest_path: Path) -> str:
    """Hash every file declared by manifest.runtime_paths, including imports/data."""
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PipelineJobError("worker_skill_manifest_invalid") from exc
    runtime_paths = manifest.get("runtime_paths") if isinstance(manifest, dict) else None
    if (
        not isinstance(runtime_paths, list)
        or not runtime_paths
        or any(not isinstance(item, str) or not item.strip() for item in runtime_paths)
    ):
        raise PipelineJobError("worker_skill_manifest_invalid")
    root = skill_root.resolve()
    files: dict[str, Path] = {}
    for raw in runtime_paths:
        relative = Path(raw)
        if relative.is_absolute() or ".." in relative.parts:
            raise PipelineJobError("worker_skill_manifest_invalid")
        candidate = skill_root / relative
        if candidate.is_symlink() or not candidate.exists():
            raise PipelineJobError("worker_asset_missing", str(candidate))
        candidates = [candidate] if candidate.is_file() else sorted(candidate.rglob("*"))
        for path in candidates:
            if path.is_dir():
                continue
            if path.is_symlink() or not path.is_file():
                raise PipelineJobError("worker_skill_runtime_file_invalid", str(path))
            resolved = path.resolve()
            try:
                rel = resolved.relative_to(root).as_posix()
            except ValueError as exc:
                raise PipelineJobError("worker_skill_runtime_file_invalid", str(path)) from exc
            if "__pycache__" in Path(rel).parts or rel.endswith((".pyc", ".pyo")):
                continue
            files[rel] = path
    if not files:
        raise PipelineJobError("worker_skill_manifest_invalid")
    digest = hashlib.sha256()
    for relative, path in sorted(files.items()):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
        digest.update(b"\n")
    return digest.hexdigest()


class RuntimeStatus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.ready = False
        self.active = False
        self.last_error = ""
        self.last_receipt: dict[str, Any] | None = None

    def update(self, **values: Any) -> None:
        with self._lock:
            for name, value in values.items():
                setattr(self, name, value)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "ready": self.ready,
                "active": self.active,
                "last_error": self.last_error or None,
                "last_job_status": (
                    (self.last_receipt or {}).get("result", {}).get("status")
                    if isinstance((self.last_receipt or {}).get("result"), dict)
                    else None
                ),
            }


def queue_counts(generation_root: Path, review_root: Path) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    for name, root in (("generation", generation_root), ("review", review_root)):
        result[name] = {
            state: len(list((root / state).glob("*.json")))
            if (root / state).is_dir()
            else 0
            for state in QUEUE_STATES
        }
    return result


def build_runtime(
    service_config: WorkerServiceConfig,
    backend_config: FeishuBackendConfig,
    *,
    apply: bool,
) -> QueueWorker:
    service_config.validate_assets()
    structured_service, pipeline_contract = load_runtime_modules(
        service_config.structured_service_root,
        service_config.pipeline_contract_path,
    )
    backend = FeishuPipelineBackend(
        backend_config,
        apply=apply,
        service=structured_service,
        contract=pipeline_contract,
    )
    backend._require_apply()
    generator = CandidateArtifactGenerator(
        work_root=service_config.work_root,
        industry_skill_root=service_config.industry_skill_root,
        structured_skill_root=service_config.structured_skill_root,
        speaker_master_path=service_config.speaker_master_path,
        codex_bin=service_config.codex_bin,
        model=service_config.model,
        reasoning_effort=service_config.reasoning_effort,
        timeout_seconds=service_config.timeout_seconds,
    )
    worker = QueueWorker(
        generation_root=backend_config.generation_job_root,
        review_root=service_config.review_job_root,
        receipt_root=service_config.receipt_root,
        lock_path=service_config.worker_lock_path,
        backend=backend,
        generator=generator,
    )
    worker.prepare()
    worker.recover()
    return worker


def run_worker_loop(
    worker: QueueWorker,
    *,
    poll_seconds: float,
    stop: threading.Event,
    status: RuntimeStatus,
) -> None:
    status.update(ready=True, last_error="")
    while not stop.is_set():
        try:
            status.update(active=True)
            receipt = worker.run_once()
            values: dict[str, Any] = {
                "active": False,
                "ready": True,
                "last_error": "",
            }
            if receipt is not None:
                values["last_receipt"] = receipt
            status.update(**values)
            if receipt is None:
                stop.wait(poll_seconds)
        except Exception as exc:
            code = exc.code if isinstance(exc, PipelineJobError) else exc.__class__.__name__
            logging.exception("unified worker loop failed code=%s", code)
            status.update(active=False, ready=False, last_error=code)
            stop.wait(poll_seconds)


class HealthServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        status: RuntimeStatus,
        generation_root: Path,
        review_root: Path,
    ):
        self.runtime_status = status
        self.generation_root = generation_root
        self.review_root = review_root
        super().__init__(address, HealthHandler)


class HealthHandler(BaseHTTPRequestHandler):
    server: HealthServer

    def _reply(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._reply(200, {"live": True})
            return
        if self.path == "/readyz":
            snapshot = self.server.runtime_status.snapshot()
            snapshot["queues"] = queue_counts(
                self.server.generation_root, self.server.review_root
            )
            self._reply(200 if snapshot["ready"] else 503, snapshot)
            return
        self._reply(404, {"error": "not_found"})

    def log_message(self, format: str, *args: Any) -> None:
        logging.debug("worker health request " + format, *args)


def serve(
    worker: QueueWorker,
    service_config: WorkerServiceConfig,
    backend_config: FeishuBackendConfig,
) -> int:
    stop = threading.Event()
    status = RuntimeStatus()
    thread = threading.Thread(
        target=run_worker_loop,
        kwargs={
            "worker": worker,
            "poll_seconds": service_config.poll_seconds,
            "stop": stop,
            "status": status,
        },
        name="unified-worker",
    )
    server = HealthServer(
        (service_config.http_host, service_config.http_port),
        status,
        backend_config.generation_job_root,
        service_config.review_job_root,
    )
    thread.start()

    def request_stop(_signum: int, _frame: Any) -> None:
        stop.set()

    previous = {
        sig: signal.signal(sig, request_stop) for sig in (signal.SIGINT, signal.SIGTERM)
    }
    server.timeout = 1
    try:
        while not stop.is_set():
            server.handle_request()
    finally:
        stop.set()
        server.server_close()
        thread.join()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 0


def retry_job(
    root: Path,
    *,
    job_id: str,
    from_state: str,
    apply: bool,
) -> dict[str, str]:
    if not apply:
        raise PipelineJobError("retry_requires_apply")
    if from_state not in RETRYABLE_STATES:
        raise PipelineJobError("retry_state_invalid")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,200}", job_id):
        raise PipelineJobError("retry_job_id_invalid")
    name = f"{job_id}.json"
    source = root / from_state / name
    if source.is_symlink() or not source.is_file():
        raise PipelineJobError("retry_source_missing")
    conflicts = [
        state
        for state in QUEUE_STATES
        if state != from_state and (root / state / name).exists()
    ]
    if conflicts:
        raise PipelineJobError("retry_job_state_conflict", ",".join(conflicts))
    target = root / "pending" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(target.parent, 0o700)
    durable_replace(source, target)
    return {"job_id": job_id, "from": from_state, "to": "pending"}


def check_config(
    service_config: WorkerServiceConfig, backend_config: FeishuBackendConfig
) -> dict[str, Any]:
    service_config.validate_assets()
    return {
        "ready": bool(backend_config.unified_enabled),
        "unified_enabled": bool(backend_config.unified_enabled),
        "assets_valid": True,
        "queues": queue_counts(
            backend_config.generation_job_root, service_config.review_job_root
        ),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Unified meeting-pipeline worker service")
    parser.add_argument(
        "--env-file",
        default=os.environ.get("FEISHU_UNIFIED_WORKER_ENV_FILE", ".env"),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check")
    once = subparsers.add_parser("run-once")
    once.add_argument("--apply", action="store_true")
    server = subparsers.add_parser("serve")
    server.add_argument("--apply", action="store_true")
    retry = subparsers.add_parser("retry")
    retry.add_argument("--queue", choices=("generation", "review"), required=True)
    retry.add_argument("--job-id", required=True)
    retry.add_argument("--from-state", choices=RETRYABLE_STATES, required=True)
    retry.add_argument("--apply", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_dotenv(Path(args.env_file).expanduser())
    service_config = WorkerServiceConfig.from_env()
    backend_config = FeishuBackendConfig.from_env()
    if args.command == "check":
        print(json.dumps(check_config(service_config, backend_config), ensure_ascii=False))
        return 0
    if args.command == "retry":
        service_config.validate_assets()
        if not backend_config.unified_enabled:
            raise PipelineJobError("production_backend_disabled")
        root = (
            backend_config.generation_job_root
            if args.queue == "generation"
            else service_config.review_job_root
        )
        print(
            json.dumps(
                retry_job(
                    root,
                    job_id=args.job_id,
                    from_state=args.from_state,
                    apply=args.apply,
                ),
                ensure_ascii=False,
            )
        )
        return 0
    worker = build_runtime(service_config, backend_config, apply=args.apply)
    if args.command == "run-once":
        print(json.dumps(worker.run_once(), ensure_ascii=False))
        return 0
    return serve(worker, service_config, backend_config)


if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("FEISHU_LOG_LEVEL", "INFO"))
    raise SystemExit(main())
