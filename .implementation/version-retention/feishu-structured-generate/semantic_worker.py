#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any
from typing import Callable, TypeVar

from skill_contract import load_skill_contract


T = TypeVar("T")


@dataclass(frozen=True)
class WorkerConfig:
    job_root: Path
    lock_path: Path
    skill_script: Path
    codex_bin: str
    docker_bin: str
    container_name: str
    container_job_root: Path
    poll_seconds: int
    command_timeout_seconds: int
    model_version: str
    contract_version: int
    schema_version: int
    claim_schema_path: Path
    semantic_prompt: str
    contract_manifest_path: Path
    prompt_path: Path
    security_master_path: Path
    security_master_cli_flag: str
    skill_runtime_sha256: str


def read_config() -> WorkerConfig:
    required = {
        name: os.environ.get(name, "").strip()
        for name in (
            "STRUCTURED_SEMANTIC_JOB_DIR_HOST",
            "STRUCTURED_TABLE_SKILL_SCRIPT_HOST",
        )
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError("Missing required worker configuration: " + ", ".join(missing))
    model_version = os.environ.get("STRUCTURED_SEMANTIC_MODEL_VERSION", "codex-cli-default").strip()
    if model_version.startswith("REPLACE_"):
        raise RuntimeError("STRUCTURED_SEMANTIC_MODEL_VERSION must be an actual model ID or codex-cli-default")
    job_root = Path(required["STRUCTURED_SEMANTIC_JOB_DIR_HOST"]).expanduser()
    skill_script = Path(required["STRUCTURED_TABLE_SKILL_SCRIPT_HOST"]).expanduser()
    contract = load_skill_contract(skill_script)
    if contract.contract_version != 9 or contract.schema_version != 9:
        raise RuntimeError("Persistent deployment source requires Skill contract v9 / schema v9")
    return WorkerConfig(
        job_root=job_root,
        lock_path=Path(
            os.environ.get("STRUCTURED_SEMANTIC_LOCK_PATH", str(job_root / ".worker.lock"))
        ).expanduser(),
        skill_script=skill_script,
        codex_bin=os.environ.get("STRUCTURED_CODEX_BIN", shutil.which("codex") or "codex"),
        docker_bin=os.environ.get("STRUCTURED_DOCKER_BIN", shutil.which("docker") or "docker"),
        container_name=os.environ.get("STRUCTURED_CONTAINER_NAME", "feishu-structured-generate"),
        container_job_root=Path(
            os.environ.get("STRUCTURED_SEMANTIC_JOB_DIR_CONTAINER", "/app/semantic-jobs")
        ),
        poll_seconds=max(2, int(os.environ.get("STRUCTURED_WORKER_POLL_SECONDS", "15"))),
        command_timeout_seconds=max(
            60, int(os.environ.get("STRUCTURED_WORKER_TIMEOUT_SECONDS", "900"))
        ),
        model_version=model_version or "codex-cli-default",
        contract_version=contract.contract_version,
        schema_version=contract.schema_version,
        claim_schema_path=contract.claim_schema_path,
        semantic_prompt=contract.prompt,
        contract_manifest_path=contract.manifest_path,
        prompt_path=contract.prompt_path,
        security_master_path=contract.security_master_path,
        security_master_cli_flag=contract.security_master_cli_flag,
        skill_runtime_sha256=contract.runtime_sha256,
    )


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_command(cmd: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout, check=False)
    if result.returncode != 0:
        detail = " ".join((result.stderr or result.stdout or "command failed").split())[-1200:]
        raise RuntimeError(detail)
    return result


def build_codex_command(
    *,
    codex_bin: str,
    job_dir: Path,
    schema_path: Path,
    output_path: Path,
    prompt: str,
    model: str,
) -> list[str]:
    command = [
        codex_bin,
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "--cd",
        str(job_dir),
        "--output-schema",
        str(schema_path),
        "--output-last-message",
        str(output_path),
        "--color",
        "never",
        "--config",
        'model_reasoning_effort="high"',
    ]
    if model != "codex-cli-default":
        command.extend(["--model", model])
    command.append(prompt)
    return command


def load_stage_output(path: Path, key: str) -> list[dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Codex stage output is not valid JSON: {path.name}") from exc
    rows = value.get(key) if isinstance(value, dict) else None
    if not isinstance(rows, list) or any(not isinstance(item, dict) for item in rows):
        raise RuntimeError(f"Codex stage output must contain object array: {key}")
    return rows


def ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def durable_replace(source: Path, target: Path) -> None:
    os.replace(source, target)
    fsync_directory(target.parent)
    if source.parent != target.parent:
        fsync_directory(source.parent)


def atomic_private_text(path: Path, content: str) -> None:
    ensure_private_directory(path.parent)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        durable_replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if fd >= 0:
            os.close(fd)
        temporary.unlink(missing_ok=True)


def write_json(path: Path, value: Any) -> None:
    atomic_private_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def write_provider_claim_schema(source_path: Path, output_path: Path) -> None:
    try:
        schema = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Invalid canonical claim schema: {source_path}") from exc
    if not isinstance(schema, dict):
        raise RuntimeError("Canonical claim schema must contain an object")

    def require_all_object_properties(node: Any) -> None:
        if isinstance(node, dict):
            properties = node.get("properties")
            if node.get("type") == "object" and isinstance(properties, dict):
                node["required"] = list(properties)
            for value in node.values():
                require_all_object_properties(value)
        elif isinstance(node, list):
            for value in node:
                require_all_object_properties(value)

    require_all_object_properties(schema)
    write_json(output_path, schema)


def retry_interrupted(operation: Callable[[], T], *, attempts: int = 5, delay_seconds: float = 1.0) -> T:
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except InterruptedError:
            if attempt == attempts:
                raise
            time.sleep(delay_seconds)
    raise RuntimeError("interrupted operation exhausted retries")


def claim_next_job(cfg: WorkerConfig) -> Path | None:
    pending = cfg.job_root / "pending"
    processing = cfg.job_root / "processing"
    ensure_private_directory(pending)
    ensure_private_directory(processing)
    pending_items = retry_interrupted(lambda: list(pending.iterdir()))
    for source in sorted(path for path in pending_items if path.is_dir()):
        target = processing / source.name
        try:
            retry_interrupted(lambda: durable_replace(source, target))
        except FileNotFoundError:
            continue
        return target
    return None


def move_job(cfg: WorkerConfig, job_dir: Path, state: str) -> Path:
    target_root = cfg.job_root / state
    ensure_private_directory(target_root)
    target = target_root / job_dir.name
    if target.exists():
        target = target_root / f"{job_dir.name}-{int(time.time())}"
    durable_replace(job_dir, target)
    return target


def run_claim_unit_stage(cfg: WorkerConfig, job_dir: Path) -> None:
    claim_raw = job_dir / "claim_units.response.json"
    provider_schema = job_dir / "claim_units.provider.schema.json"
    write_provider_claim_schema(cfg.claim_schema_path, provider_schema)
    run_command(
        build_codex_command(
            codex_bin=cfg.codex_bin,
            job_dir=job_dir,
            schema_path=provider_schema,
            output_path=claim_raw,
            prompt=cfg.semantic_prompt,
            model=cfg.model_version,
        ),
        timeout=cfg.command_timeout_seconds,
    )
    write_json(
        job_dir / "claim_units.json",
        {"claim_units": load_stage_output(claim_raw, "claim_units")},
    )


def container_command(cfg: WorkerConfig, command: str, job_id: str) -> list[str]:
    return [
        cfg.docker_bin,
        "exec",
        cfg.container_name,
        "python",
        "structured_generate_service.py",
        command,
        job_id,
        "--apply",
    ]


def job_id_hash(job_id: str) -> str:
    return hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:12]


def process_job(cfg: WorkerConfig, job_dir: Path) -> None:
    required_files = (
        cfg.skill_script,
        cfg.contract_manifest_path,
        cfg.prompt_path,
        cfg.claim_schema_path,
        cfg.security_master_path,
    )
    for path in required_files:
        if not path.is_file():
            raise RuntimeError(f"Skill contract file not found: {path}")
    run_claim_unit_stage(cfg, job_dir)
    write_json(
        job_dir / "model_metadata.json",
        {
            "model_version": cfg.model_version,
            "contract_version": cfg.contract_version,
            "schema_version": cfg.schema_version,
            "skill_script_sha256": sha256_file(cfg.skill_script),
            "skill_contract_sha256": sha256_file(cfg.contract_manifest_path),
            "semantic_prompt_sha256": sha256_file(cfg.prompt_path),
            "claim_units_schema_sha256": sha256_file(cfg.claim_schema_path),
            "security_master_sha256": sha256_file(cfg.security_master_path),
            "security_master_path": str(cfg.security_master_path),
            "security_master_cli_flag": cfg.security_master_cli_flag,
            "skill_runtime_sha256": cfg.skill_runtime_sha256,
            "worker": "codex-local-agent",
            "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        },
    )
    run_command(container_command(cfg, "complete-job", job_dir.name), timeout=cfg.command_timeout_seconds)
    for name in (
        "claim_units.json",
        "claim_units.response.json",
        "claim_units.provider.schema.json",
    ):
        (job_dir / name).unlink(missing_ok=True)


def mark_failed(cfg: WorkerConfig, job_dir: Path, exc: Exception) -> bool:
    detail = " ".join(str(exc).split())[:2000] or exc.__class__.__name__
    atomic_private_text(job_dir / "error.txt", detail + "\n")
    try:
        result = run_command(container_command(cfg, "fail-job", job_dir.name), timeout=120)
        try:
            payload = json.loads(result.stdout)
        except (TypeError, json.JSONDecodeError):
            payload = {}
        return isinstance(payload, dict) and payload.get("status") == "skipped_terminal"
    except Exception:
        logging.error("Could not write failure status for job_id_hash=%s", job_id_hash(job_dir.name))
        return False


def recover_processing_jobs(cfg: WorkerConfig) -> None:
    processing = cfg.job_root / "processing"
    pending = cfg.job_root / "pending"
    done = cfg.job_root / "done"
    failed = cfg.job_root / "failed"
    for directory in (processing, pending, done, failed):
        ensure_private_directory(directory)
    for source in sorted(path for path in processing.iterdir() if path.is_dir()):
        if (source / "result.json").is_file():
            target = done / source.name
        elif (source / "failure.json").is_file():
            target = failed / source.name
        else:
            target = pending / source.name
        if target.exists():
            raise RuntimeError("semantic_queue_recovery_conflict")
        durable_replace(source, target)


def run_once(cfg: WorkerConfig) -> bool:
    job_dir = claim_next_job(cfg)
    if job_dir is None:
        return False
    logging.info("Processing semantic job_id_hash=%s", job_id_hash(job_dir.name))
    try:
        process_job(cfg, job_dir)
    except Exception as exc:
        logging.error(
            "Semantic job failed job_id_hash=%s code=%s",
            job_id_hash(job_dir.name),
            exc.__class__.__name__,
        )
        terminal_success = mark_failed(cfg, job_dir, exc)
        move_job(cfg, job_dir, "done" if terminal_success else "failed")
        return True
    move_job(cfg, job_dir, "done")
    logging.info("Semantic job completed job_id_hash=%s", job_id_hash(job_dir.name))
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="semantic_worker.py")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    configure_logging()
    cfg = read_config()
    ensure_private_directory(cfg.job_root)
    ensure_private_directory(cfg.lock_path.parent)
    with cfg.lock_path.open("a+") as lock_handle:
        os.chmod(cfg.lock_path, 0o600)
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logging.info("Another semantic worker already owns the queue lock")
            return 0
        recover_processing_jobs(cfg)
        if args.once:
            run_once(cfg)
            return 0
        while True:
            try:
                processed = run_once(cfg)
            except InterruptedError:
                logging.warning("Queue filesystem operation was interrupted; retrying after poll interval")
                time.sleep(cfg.poll_seconds)
                continue
            if not processed:
                time.sleep(cfg.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
