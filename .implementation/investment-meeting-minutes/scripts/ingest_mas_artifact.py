#!/usr/bin/env python3
"""Ingest one MAS subagent artifact return into a dispatch directory."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from collect_mas_artifacts import PHASE_ORDER
from mas_task_lock import mas_task_lock
from validate_mas_artifacts import (
    MAIN_OWNED_ARTIFACTS,
    artifact_mapping,
    read_json,
    validate_dispatch_identity,
    validate_payload,
)

TRANSACTION_PREFIX = ".mas-ingest-txn-"


class ArtifactTransactionError(OSError):
    def __init__(
        self,
        message: str,
        *,
        recovery_required: bool,
        recovery_dir: Path | None = None,
    ) -> None:
        super().__init__(message)
        self.recovery_required = recovery_required
        self.recovery_dir = recovery_dir


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def artifact_envelope(payload: dict[str, Any], artifact_type: str, artifact: Any) -> dict[str, Any]:
    return {
        "run_id": payload.get("run_id"),
        "task_id": payload.get("task_id"),
        "dispatch_phase": payload.get("dispatch_phase"),
        "artifact_owner": payload.get("artifact_owner"),
        "artifact_type": artifact_type,
        "artifact": artifact,
    }


def transaction_target(
    entry: dict[str, Any],
    artifact_dir: Path,
    repair_dir: Path,
) -> tuple[Path, str]:
    scope = str(entry.get("scope") or "")
    filename = str(entry.get("filename") or "")
    backup_name = str(entry.get("backup_name") or "")
    if scope not in {"artifact", "repair"}:
        raise ValueError(f"unknown transaction target scope: {scope}")
    if not filename or Path(filename).name != filename:
        raise ValueError(f"unsafe transaction target filename: {filename}")
    if not backup_name or Path(backup_name).name != backup_name:
        raise ValueError(f"unsafe transaction backup filename: {backup_name}")
    target_dir = artifact_dir if scope == "artifact" else repair_dir
    return target_dir / filename, backup_name


def remove_transaction_dir(transaction_dir: Path) -> list[str]:
    try:
        shutil.rmtree(transaction_dir)
    except OSError as exc:
        return [f"cannot remove transaction directory: {exc}"]
    return []


def rollback_transaction(
    transaction_dir: Path,
    artifact_dir: Path,
    repair_dir: Path,
) -> list[str]:
    manifest_path = transaction_dir / "manifest.json"
    if not manifest_path.exists():
        return remove_transaction_dir(transaction_dir)
    try:
        manifest = read_json(manifest_path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return [f"cannot read transaction manifest: {exc}"]
    if not isinstance(manifest, dict):
        return ["transaction manifest must be a JSON object"]
    if manifest.get("state") == "committed":
        return remove_transaction_dir(transaction_dir)
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        return ["transaction manifest entries must be a JSON array"]

    resolved: list[tuple[dict[str, Any], Path, Path]] = []
    errors: list[str] = []
    for raw_entry in entries:
        if not isinstance(raw_entry, dict):
            errors.append("transaction entry must be a JSON object")
            continue
        try:
            target, backup_name = transaction_target(raw_entry, artifact_dir, repair_dir)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        resolved.append((raw_entry, target, transaction_dir / "backup" / backup_name))
    if errors:
        return errors

    for entry, target, backup in reversed(resolved):
        had_existing = entry.get("had_existing")
        if not isinstance(had_existing, bool):
            errors.append(f"transaction entry had_existing must be boolean: {target.name}")
            continue
        try:
            if backup.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(backup, target)
            elif had_existing:
                if not target.exists():
                    errors.append(f"original target and backup are both missing: {target}")
            else:
                target.unlink(missing_ok=True)
        except OSError as exc:
            errors.append(f"cannot restore transaction target {target}: {exc}")

    for entry, target, _ in resolved:
        had_existing = entry.get("had_existing")
        if had_existing is True and not target.exists():
            errors.append(f"restored target is missing: {target}")
        if had_existing is False and target.exists():
            errors.append(f"new transaction target remains after rollback: {target}")
    if errors:
        return errors
    return remove_transaction_dir(transaction_dir)


def recover_pending_transactions(task_dir: Path) -> list[str]:
    artifact_dir = task_dir / "artifacts"
    repair_dir = task_dir / "repair_history"
    if not artifact_dir.exists():
        return []
    recovered: list[str] = []
    recovery_errors: list[str] = []
    failed_dir: Path | None = None
    for transaction_dir in sorted(artifact_dir.glob(f"{TRANSACTION_PREFIX}*")):
        if not transaction_dir.is_dir():
            continue
        errors = rollback_transaction(transaction_dir, artifact_dir, repair_dir)
        if errors:
            failed_dir = failed_dir or transaction_dir
            recovery_errors.extend(f"{transaction_dir.name}: {error}" for error in errors)
        else:
            recovered.append(str(transaction_dir))
    if recovery_errors:
        raise ArtifactTransactionError(
            "unfinished MAS artifact transaction cannot be recovered automatically: "
            + "; ".join(recovery_errors),
            recovery_required=True,
            recovery_dir=failed_dir,
        )
    return recovered


def commit_artifact_set(
    task_dir: Path,
    payload: dict[str, Any],
    artifacts: dict[str, Any],
    repair_records: list[tuple[Path, dict[str, Any]]] | None = None,
) -> list[dict[str, str]]:
    """Commit task artifacts and replacement history as one recoverable set."""
    artifact_dir = task_dir / "artifacts"
    repair_dir = task_dir / "repair_history"
    artifact_types = sorted(str(artifact_type) for artifact_type in artifacts)
    repair_records = repair_records or []
    for repair_path, _ in repair_records:
        if repair_path.parent != repair_dir or Path(repair_path.name).name != repair_path.name:
            raise ValueError(f"repair transaction target must be inside repair_history: {repair_path}")
    artifact_dir.mkdir(parents=True, exist_ok=True)
    transaction_dir = Path(tempfile.mkdtemp(prefix=TRANSACTION_PREFIX, dir=artifact_dir))
    stage_dir = transaction_dir / "stage"
    backup_dir = transaction_dir / "backup"
    stage_dir.mkdir()
    backup_dir.mkdir()
    entries: list[dict[str, Any]] = []
    staged_payloads: list[tuple[str, dict[str, Any]]] = []
    for artifact_type in artifact_types:
        target = artifact_dir / f"{artifact_type}.json"
        stage_name = f"artifact-{len(entries):03d}.json"
        entries.append(
            {
                "scope": "artifact",
                "filename": target.name,
                "stage_name": stage_name,
                "backup_name": f"{len(entries):03d}.json",
                "had_existing": target.exists(),
            }
        )
        staged_payloads.append((stage_name, artifact_envelope(payload, artifact_type, artifacts[artifact_type])))
    for repair_path, repair_payload in repair_records:
        stage_name = f"repair-{len(entries):03d}.json"
        entries.append(
            {
                "scope": "repair",
                "filename": repair_path.name,
                "stage_name": stage_name,
                "backup_name": f"{len(entries):03d}.json",
                "had_existing": repair_path.exists(),
            }
        )
        staged_payloads.append((stage_name, repair_payload))

    manifest = {
        "schema_version": "1.0",
        "state": "prepared",
        "entries": entries,
    }
    manifest_written = False
    try:
        for stage_name, staged_payload in staged_payloads:
            write_json(stage_dir / stage_name, staged_payload)
        write_json(transaction_dir / "manifest.json", manifest)
        manifest_written = True
        for entry in entries:
            target, backup_name = transaction_target(entry, artifact_dir, repair_dir)
            target.parent.mkdir(parents=True, exist_ok=True)
            if entry["had_existing"]:
                backup = backup_dir / backup_name
                os.replace(target, backup)
        for entry in entries:
            target, _ = transaction_target(entry, artifact_dir, repair_dir)
            os.replace(stage_dir / str(entry["stage_name"]), target)
        manifest["state"] = "committed"
        write_json(transaction_dir / "manifest.json", manifest)
        shutil.rmtree(transaction_dir, ignore_errors=True)
    except BaseException as exc:
        recovery_errors = (
            rollback_transaction(transaction_dir, artifact_dir, repair_dir)
            if manifest_written
            else remove_transaction_dir(transaction_dir)
        )
        if isinstance(exc, (KeyboardInterrupt, SystemExit)) and not recovery_errors:
            raise
        detail = f"MAS artifact transaction failed: {exc}"
        if recovery_errors:
            detail += "; manual recovery required at " + str(transaction_dir)
            detail += "; " + "; ".join(recovery_errors)
        else:
            detail += "; transaction rolled back"
        raise ArtifactTransactionError(
            detail,
            recovery_required=bool(recovery_errors),
            recovery_dir=transaction_dir if recovery_errors else None,
        ) from exc
    return [
        {"artifact_type": artifact_type, "path": str(artifact_dir / f"{artifact_type}.json")}
        for artifact_type in artifact_types
    ]


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip())
    return cleaned.strip("-") or "artifact"


def load_json_input(input_path: Path | None) -> tuple[Any | None, str, str, list[str]]:
    if input_path is None:
        source = "stdin"
        try:
            raw_text = sys.stdin.read()
        except OSError as exc:
            return None, source, "", [f"无法读取 subagent artifact JSON: {exc}"]
    else:
        source = str(input_path)
        try:
            raw_text = input_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return None, source, "", [f"无法读取 subagent artifact JSON: {exc}"]
    try:
        return json.loads(raw_text), source, raw_text, []
    except json.JSONDecodeError as exc:
        return None, source, raw_text, [f"无法解析 subagent artifact JSON: {exc}"]


def collector_command(task_dir: Path, through_phase: str | None = None) -> str:
    script_path = Path(__file__).with_name("collect_mas_artifacts.py")
    command = f"python3 {shlex.quote(str(script_path))} {shlex.quote(str(task_dir))} --json"
    if through_phase:
        command = (
            f"python3 {shlex.quote(str(script_path))} {shlex.quote(str(task_dir))} "
            f"--through-phase {shlex.quote(through_phase)} --json"
        )
    return command


def repair_file_path(repair_dir: Path, reason: str, artifact_types: list[str], input_source: str) -> Path:
    source_name = "stdin" if input_source == "stdin" else Path(input_source).stem
    type_part = "-".join(safe_name(artifact_type) for artifact_type in artifact_types) if artifact_types else "unknown"
    import uuid

    return repair_dir / f"{utc_stamp()}-{uuid.uuid4().hex[:12]}-{type_part}-{safe_name(reason)}-{safe_name(source_name)}.json"


def repair_record_payload(
    reason: str,
    input_source: str,
    payload: Any,
    errors: list[str],
    warnings: list[str],
    artifact_types: list[str],
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "ingest_status": reason,
        "input_source": input_source,
        "artifact_types": artifact_types,
        "errors": errors,
        "warnings": warnings,
        "payload": payload,
    }


def write_repair_record(
    repair_dir: Path,
    reason: str,
    input_source: str,
    payload: Any,
    errors: list[str],
    warnings: list[str],
    artifact_types: list[str],
) -> Path:
    path = repair_file_path(repair_dir, reason, artifact_types, input_source)
    write_json(
        path,
        repair_record_payload(
            reason,
            input_source,
            payload,
            errors,
            warnings,
            artifact_types,
        ),
    )
    return path


def ingest_mas_artifact(
    payload: Any,
    task_dir: Path,
    input_source: str = "stdin",
    through_phase: str | None = None,
    replace_existing: bool = False,
) -> dict[str, Any]:
    task_dir = task_dir.expanduser()
    artifact_dir = task_dir / "artifacts"
    repair_dir = task_dir / "repair_history"
    artifacts, mapping_errors = artifact_mapping(payload)
    validation = validate_payload(payload)
    errors = [str(error) for error in mapping_errors]
    for error in validation.get("errors", []):
        if str(error) not in errors:
            errors.append(str(error))
    warnings = [str(warning) for warning in validation.get("warnings", [])]
    artifact_types = sorted(str(artifact_type) for artifact_type in artifacts)
    main_owned_types = sorted(set(artifact_types) & MAIN_OWNED_ARTIFACTS)
    if main_owned_types:
        errors.append(
            "subagent ingest 不接受 Main Orchestrator 自有 artifact: " + ", ".join(main_owned_types)
        )
    written_artifacts: list[dict[str, str]] = []
    repair_history_file = ""
    ingest_status = "written"

    if errors:
        with mas_task_lock(task_dir, exclusive=False):
            try:
                bundle = read_json(task_dir / "mas_task_bundle.json")
                manifest = read_json(task_dir / "dispatch_manifest.json")
                if not isinstance(bundle, dict) or not isinstance(manifest, dict):
                    raise ValueError("MAS bundle and dispatch manifest must be JSON objects")
                for error in validate_dispatch_identity(
                    payload,
                    bundle,
                    manifest,
                    through_phase=through_phase,
                    phase_order=PHASE_ORDER,
                ):
                    if str(error) not in errors:
                        errors.append(str(error))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                errors.append(f"无法校验 MAS artifact dispatch identity: {exc}")
        ingest_status = "invalid_artifact_not_written"
        repair_history_file = str(
            write_repair_record(
                repair_dir,
                "invalid",
                input_source,
                payload,
                errors,
                warnings,
                artifact_types,
            )
        )
    else:
        with mas_task_lock(task_dir, exclusive=True):
            try:
                recovered_transactions = recover_pending_transactions(task_dir)
                if recovered_transactions:
                    warnings.append(
                        f"recovered {len(recovered_transactions)} unfinished MAS artifact transaction(s)"
                    )
            except ArtifactTransactionError as exc:
                errors.append(str(exc))
                ingest_status = "artifact_transaction_recovery_required"

            if not errors:
                try:
                    bundle = read_json(task_dir / "mas_task_bundle.json")
                    manifest = read_json(task_dir / "dispatch_manifest.json")
                    if not isinstance(bundle, dict) or not isinstance(manifest, dict):
                        raise ValueError("MAS bundle and dispatch manifest must be JSON objects")
                    errors.extend(
                        validate_dispatch_identity(
                            payload,
                            bundle,
                            manifest,
                            through_phase=through_phase,
                            phase_order=PHASE_ORDER,
                        )
                    )
                except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                    errors.append(f"无法校验 MAS artifact dispatch identity: {exc}")

            if errors:
                reason = "recovery_required" if ingest_status == "artifact_transaction_recovery_required" else "invalid"
                if reason == "invalid":
                    ingest_status = "invalid_artifact_not_written"
                repair_history_file = str(
                    write_repair_record(
                        repair_dir,
                        reason,
                        input_source,
                        payload,
                        errors,
                        warnings,
                        artifact_types,
                    )
                )
            else:
                duplicate_paths = [
                    artifact_dir / f"{artifact_type}.json"
                    for artifact_type in artifact_types
                    if (artifact_dir / f"{artifact_type}.json").exists()
                ]
                if duplicate_paths and not replace_existing:
                    ingest_status = "duplicate_artifact_not_written"
                    errors.extend(f"artifact already exists: {path}" for path in duplicate_paths)
                    repair_history_file = str(
                        write_repair_record(
                            repair_dir,
                            "duplicate",
                            input_source,
                            payload,
                            errors,
                            warnings,
                            artifact_types,
                        )
                    )
                else:
                    superseded_records: list[tuple[Path, dict[str, Any]]] = []
                    for path in duplicate_paths:
                        try:
                            previous_payload = read_json(path)
                        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                            errors.append(f"无法归档待替换 MAS artifact: {path}: {exc}")
                            continue
                        archived = repair_file_path(repair_dir, "superseded", artifact_types, str(path))
                        superseded_records.append(
                            (
                                archived,
                                repair_record_payload(
                                    "superseded",
                                    str(path),
                                    previous_payload,
                                    [],
                                    ["explicit replacement requested by Main Orchestrator"],
                                    artifact_types,
                                ),
                            )
                        )
                    if errors:
                        ingest_status = "replacement_archive_failed_not_written"
                        repair_history_file = str(
                            write_repair_record(
                                repair_dir,
                                "replacement_archive_failed",
                                input_source,
                                payload,
                                errors,
                                warnings,
                                artifact_types,
                            )
                        )
                    else:
                        try:
                            written_artifacts = commit_artifact_set(
                                task_dir,
                                payload,
                                artifacts,
                                repair_records=superseded_records,
                            )
                        except ArtifactTransactionError as exc:
                            errors.append(str(exc))
                            if exc.recovery_required:
                                ingest_status = "artifact_transaction_recovery_required"
                                reason = "recovery_required"
                            else:
                                ingest_status = "artifact_transaction_failed_not_written"
                                reason = "transaction_failed"
                            repair_history_file = str(
                                write_repair_record(
                                    repair_dir,
                                    reason,
                                    input_source,
                                    payload,
                                    errors,
                                    warnings,
                                    artifact_types,
                                )
                            )
                        except (OSError, ValueError) as exc:
                            errors.append(f"MAS artifact set 事务准备失败，未写入: {exc}")
                            ingest_status = "artifact_transaction_failed_not_written"
                            repair_history_file = str(
                                write_repair_record(
                                    repair_dir,
                                    "transaction_failed",
                                    input_source,
                                    payload,
                                    errors,
                                    warnings,
                                    artifact_types,
                                )
                            )
                        else:
                            ingest_status = "replaced" if duplicate_paths else "written"
                            if superseded_records:
                                repair_history_file = str(superseded_records[-1][0])

    ok = ingest_status in {"written", "replaced"}
    return {
        "schema_version": "1.0",
        "ok": ok,
        "ingest_status": ingest_status,
        "input_source": input_source,
        "task_dir": str(task_dir),
        "artifact_dir": str(artifact_dir),
        "repair_history_dir": str(repair_dir),
        "artifact_types": artifact_types,
        "written_artifacts": written_artifacts,
        "repair_history_file": repair_history_file,
        "validation": validation,
        "next_collector_command": collector_command(task_dir, through_phase=through_phase),
        "errors": errors,
        "warnings": warnings,
    }


def ingest_mas_artifact_file(
    input_path: Path | None,
    task_dir: Path,
    through_phase: str | None = None,
    replace_existing: bool = False,
) -> dict[str, Any]:
    payload, input_source, raw_text, parse_errors = load_json_input(input_path)
    if parse_errors:
        task_dir = task_dir.expanduser()
        repair_dir = task_dir / "repair_history"
        repair_path = write_repair_record(
            repair_dir,
            "parse_error",
            input_source,
            {"raw_text": raw_text},
            parse_errors,
            [],
            [],
        )
        return {
            "schema_version": "1.0",
            "ok": False,
            "ingest_status": "parse_error_not_written",
            "input_source": input_source,
            "task_dir": str(task_dir),
            "artifact_dir": str(task_dir / "artifacts"),
            "repair_history_dir": str(repair_dir),
            "artifact_types": [],
            "written_artifacts": [],
            "repair_history_file": str(repair_path),
            "validation": {"ok": False, "errors": parse_errors, "warnings": []},
            "next_collector_command": collector_command(task_dir, through_phase=through_phase),
            "errors": parse_errors,
            "warnings": [],
        }
    return ingest_mas_artifact(
        payload,
        task_dir,
        input_source=input_source,
        through_phase=through_phase,
        replace_existing=replace_existing,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="接收并校验一个 MAS subagent artifact JSON 返回")
    parser.add_argument("artifact_file", nargs="?", help="subagent 返回的 JSON 文件；省略时从 stdin 读取")
    parser.add_argument("--task-dir", required=True, help="包含 MAS dispatch files 的任务目录")
    parser.add_argument("--through-phase", choices=sorted(PHASE_ORDER), help="建议下一次 collector 校验到的 phase")
    parser.add_argument("--replace-existing", action="store_true", help="显式替换同 run/task 的已有 artifact，并先归档旧值")
    parser.add_argument("--json", action="store_true", help="输出 JSON；默认也是 JSON")
    args = parser.parse_args()

    input_path = Path(args.artifact_file) if args.artifact_file else None
    result = ingest_mas_artifact_file(
        input_path,
        Path(args.task_dir),
        through_phase=args.through_phase,
        replace_existing=bool(args.replace_existing),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
