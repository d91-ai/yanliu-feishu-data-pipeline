#!/usr/bin/env python3
"""Run fixed local regression checks for the meeting-minutes contract."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import contextlib
import errno
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
import warnings as py_warnings
from pathlib import Path
from typing import Any
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
DEFAULT_CASES_PATH = SKILL_DIR / "references/regression_samples/cases.json"

sys.path.insert(0, str(SCRIPT_DIR))
from validate_meeting_minutes_contract import (  # noqa: E402
    validate_contract,
    validate_timestamp_index_file,
    validate_verification_sidecar,
)
from validate_mas_artifacts import (  # noqa: E402
    file_sha256,
    validate_file as validate_mas_artifacts_file,
    validate_payload as validate_mas_artifacts_payload,
)
from build_mas_task_bundle import (  # noqa: E402
    build_bundle_from_request as build_mas_task_bundle_from_request,
    validate_bundle as validate_mas_task_bundle,
    write_dispatch_files as write_mas_dispatch_files,
)
from create_mas_source_manifest import create_source_manifest, source_manifest_artifact  # noqa: E402
from summarize_mas_decisions import summarize_file as summarize_mas_decision_file  # noqa: E402
from collect_mas_artifacts import collect_mas_run  # noqa: E402
from ingest_mas_artifact import ingest_mas_artifact_file  # noqa: E402
import ingest_mas_artifact as ingest_mas_artifact_module  # noqa: E402
from plan_mas_next_action import plan_from_summary  # noqa: E402
from run_mas_phase_operator import run_mas_phase_operator  # noqa: E402
from run_mas_dry_run import (  # noqa: E402
    run_mas_dry_run,
    synthetic_final_markdown,
    synthetic_verification_payload,
)
from record_mas_main_actions import record_main_actions  # noqa: E402
from archive_raw_inputs import archive_files  # noqa: E402
from export_to_obsidian import export_note  # noqa: E402
import archive_raw_inputs as archive_module  # noqa: E402
import export_to_obsidian as export_module  # noqa: E402
from process_transcript import build_output, detect_segments  # noqa: E402
from sensevoice_transcription_server import (  # noqa: E402
    MultipartForm,
    UploadedFormFile,
    _run_transcribe as run_sensevoice_subprocess,
    require_audio_form_file,
)
import transcribe_audio as transcribe_audio_module  # noqa: E402


def read_cases(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = payload.get("cases")
    if not isinstance(cases, list):
        raise ValueError(f"回归样例格式错误: {path}")
    return cases


def dispatch_context(task_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    bundle = json.loads((task_dir / "mas_task_bundle.json").read_text(encoding="utf-8"))
    manifest = json.loads((task_dir / "dispatch_manifest.json").read_text(encoding="utf-8"))
    if not isinstance(bundle, dict) or not isinstance(manifest, dict):
        raise ValueError("MAS regression dispatch context must contain JSON objects")
    return bundle, manifest


def fixture_identity(manifest: dict[str, Any], artifact_type: str) -> dict[str, Any]:
    run_id = str(manifest.get("run_id") or "")
    if artifact_type == "source_manifest":
        return {
            "run_id": run_id,
            "task_id": f"{run_id}:main:source_manifest",
            "dispatch_phase": "pre_draft",
            "artifact_owner": "Main Orchestrator",
        }
    for task in manifest.get("task_files", []):
        if not isinstance(task, dict):
            continue
        produced = {str(task.get("artifact_type") or "")}
        produced.update(str(item) for item in task.get("secondary_artifacts", []))
        if artifact_type in produced:
            return {
                "run_id": run_id,
                "task_id": str(task.get("task_id") or ""),
                "dispatch_phase": str(task.get("dispatch_phase") or ""),
                "artifact_owner": str(task.get("artifact_owner") or task.get("role") or ""),
            }
    raise ValueError(f"MAS regression cannot resolve artifact task identity: {artifact_type}")


def fixture_payload(
    manifest: dict[str, Any],
    artifact_type: str,
    artifact: Any,
    markdown_path: Path | None = None,
) -> dict[str, Any]:
    artifact_value = json.loads(json.dumps(artifact, ensure_ascii=False))
    if artifact_type == "export_manifest" and isinstance(artifact_value, dict) and markdown_path is not None:
        artifact_value["markdown_path"] = str(markdown_path)
        artifact_value["markdown_sha256"] = file_sha256(markdown_path)
        artifact_value["main_actions_verified"] = True
    return {
        **fixture_identity(manifest, artifact_type),
        "artifact_type": artifact_type,
        "artifact": artifact_value,
    }


def bind_fixture_return(source_path: Path, destination: Path, manifest: dict[str, Any]) -> Path:
    payload = json.loads(source_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"MAS fixture return must be a JSON object: {source_path}")
    if isinstance(payload.get("artifacts"), dict):
        artifact_types = [str(item) for item in payload["artifacts"]]
    else:
        artifact_types = [str(payload.get("artifact_type") or "")]
    primary = next((item for item in artifact_types if item and item != "doubtful_items"), artifact_types[0])
    identity = fixture_identity(manifest, primary)
    identity.pop("task_artifact_set", None)
    identity.pop("ingested_split", None)
    bound = {**identity, **payload}
    destination.write_text(json.dumps(bound, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return destination


def fixture_return_payload(
    manifest: dict[str, Any],
    primary_artifact: str,
    fixture_artifacts: dict[str, Any],
    markdown_path: Path | None = None,
) -> dict[str, Any]:
    identity = fixture_identity(manifest, primary_artifact)
    task_artifact_set = [primary_artifact]
    for task in manifest.get("task_files", []):
        if isinstance(task, dict) and str(task.get("task_id") or "") == str(identity.get("task_id") or ""):
            task_artifact_set = [str(task.get("artifact_type") or "")]
            task_artifact_set.extend(str(item) for item in task.get("secondary_artifacts", []))
            break
    values: dict[str, Any] = {}
    for artifact_type in task_artifact_set:
        if artifact_type not in fixture_artifacts:
            raise ValueError(f"MAS return fixture missing task artifact: {artifact_type}")
        artifact_value = json.loads(json.dumps(fixture_artifacts[artifact_type], ensure_ascii=False))
        if artifact_type == "export_manifest" and isinstance(artifact_value, dict) and markdown_path is not None:
            artifact_value["markdown_path"] = str(markdown_path)
            artifact_value["markdown_sha256"] = file_sha256(markdown_path)
            artifact_value["main_actions_verified"] = True
        values[artifact_type] = artifact_value
    if len(values) == 1:
        artifact_type, artifact_value = next(iter(values.items()))
        return {**identity, "artifact_type": artifact_type, "artifact": artifact_value}
    return {**identity, "artifacts": values}


def run_case(case: dict[str, Any], base_dir: Path) -> dict[str, Any]:
    file_path = base_dir / str(case["file"])
    if case.get("check") == "text_contains":
        text = file_path.read_text(encoding="utf-8")
        errors: list[str] = []
        warnings: list[str] = []
        for term in [str(term) for term in case.get("required_terms", [])]:
            if term not in text:
                errors.append(f"缺少文本检查锚点: {term}")
        for term in [str(term) for term in case.get("forbidden_terms", [])]:
            if term in text:
                errors.append(f"包含文本检查禁止锚点: {term}")
        result: dict[str, Any] = {
            "ok": not errors,
            "errors": errors,
            "warnings": warnings,
        }
    elif case.get("check") == "export_filename":
        with tempfile.TemporaryDirectory(prefix="meeting-minutes-export-") as tmpdir:
            errors = []
            warnings = []
            md_path = ""
            try:
                configured_series = tuple(str(item) for item in case.get("review_series", []))
                with patch.object(export_module, "KNOWN_REVIEW_SERIES", configured_series):
                    result_export = export_note(
                        file_path,
                        Path(tmpdir),
                        str(case["meeting_date_override"]) if case.get("meeting_date_override") else None,
                    )
                expected_stem = str(case["expected_stem"])
                actual_stem = result_export.md_path.stem
                md_path = str(result_export.md_path)
                if actual_stem != expected_stem:
                    errors.append(f"导出文件名不符合预期: expected={expected_stem} actual={actual_stem}")
                if result_export.md_path.suffix != ".md":
                    errors.append(f"Markdown 后缀错误: {result_export.md_path.name}")
                if not result_export.md_created:
                    errors.append(f"Markdown 未生成: {result_export.md_message}")
            except Exception as exc:
                errors.append(f"导出失败: {exc}")
            result = {
                "ok": not errors,
                "errors": errors,
                "warnings": warnings,
                "md_path": md_path,
            }
    elif case.get("check") == "export_concurrent":
        with tempfile.TemporaryDirectory(prefix="meeting-minutes-export-concurrent-") as tmpdir:
            errors = []
            warnings = []
            count = int(case.get("count") or 8)
            with ThreadPoolExecutor(max_workers=count) as executor:
                exports = list(
                    executor.map(
                        lambda _: export_note(file_path, Path(tmpdir), str(case.get("meeting_date_override") or "")),
                        range(count),
                    )
                )
            paths = [item.md_path for item in exports]
            if not all(item.md_created for item in exports):
                errors.append("并发导出存在未成功结果")
            if len(set(paths)) != count:
                errors.append(f"并发导出路径不唯一: expected={count} actual={len(set(paths))}")
            source_bytes = file_path.read_bytes()
            for path in paths:
                if not path.is_file() or path.read_bytes() != source_bytes:
                    errors.append(f"并发导出文件缺失或内容不一致: {path}")
            if list(Path(tmpdir).rglob("*.part")):
                errors.append("并发导出后残留隐藏 part 文件")
            result = {
                "ok": not errors,
                "errors": errors,
                "warnings": warnings,
                "export_count": count,
                "unique_path_count": len(set(paths)),
            }
    elif case.get("check") == "export_part_cleanup_failure":
        errors = []
        warnings = []
        with tempfile.TemporaryDirectory(prefix="meeting-minutes-export-cleanup-") as tmpdir:
            original_unlink = Path.unlink

            def fail_part_unlink(path: Path, *args: Any, **kwargs: Any) -> None:
                if path.suffix == ".part":
                    raise PermissionError("synthetic part cleanup failure")
                original_unlink(path, *args, **kwargs)

            with patch.object(Path, "unlink", fail_part_unlink), py_warnings.catch_warnings(record=True) as caught:
                py_warnings.simplefilter("always")
                exported = export_note(file_path, Path(tmpdir), str(case.get("meeting_date_override") or ""))
            if not exported.md_created or not exported.md_path.is_file():
                errors.append("part 清理失败不应反转已完成的 Markdown 发布")
            elif exported.md_path.read_bytes() != file_path.read_bytes():
                errors.append("part 清理失败后的 Markdown 内容不完整")
            part_files = list(Path(tmpdir).rglob("*.part"))
            if not part_files or not any("part 文件清理失败" in str(item.message) for item in caught):
                errors.append("part 清理失败缺少明确告警或故障注入未生效")
            for part_file in part_files:
                original_unlink(part_file, missing_ok=True)
        result = {"ok": not errors, "errors": errors, "warnings": warnings}
    elif case.get("check") == "archive_part_cleanup_failure":
        errors = []
        warnings = []
        with tempfile.TemporaryDirectory(prefix="meeting-minutes-archive-cleanup-") as tmpdir:
            original_unlink = Path.unlink

            def fail_part_unlink(path: Path, *args: Any, **kwargs: Any) -> None:
                if path.suffix == ".part":
                    raise PermissionError("synthetic part cleanup failure")
                original_unlink(path, *args, **kwargs)

            with patch.object(Path, "unlink", fail_part_unlink), py_warnings.catch_warnings(record=True) as caught:
                py_warnings.simplefilter("always")
                archived = archive_files(
                    [file_path],
                    Path(tmpdir),
                    str(case.get("archive_date") or "2032-07-01"),
                    str(case.get("meeting_title") or "合成归档"),
                )
            if len(archived) != 1 or not archived[0].is_file():
                errors.append("part 清理失败不应反转已完成的原始文件归档")
            elif archived[0].read_bytes() != file_path.read_bytes():
                errors.append("part 清理失败后的归档内容不完整")
            part_files = list(Path(tmpdir).rglob("*.part"))
            if not part_files or not any("part 文件清理失败" in str(item.message) for item in caught):
                errors.append("归档 part 清理失败缺少明确告警或故障注入未生效")
            for part_file in part_files:
                original_unlink(part_file, missing_ok=True)
        result = {"ok": not errors, "errors": errors, "warnings": warnings}
    elif case.get("check") == "atomic_publish_unsupported":
        errors = []
        warnings = []
        unsupported = OSError(errno.EOPNOTSUPP, "synthetic hard-link unsupported")
        with tempfile.TemporaryDirectory(prefix="meeting-minutes-atomic-unsupported-") as tmpdir:
            export_dir = Path(tmpdir) / "export"
            archive_dir = Path(tmpdir) / "archive"
            with patch.object(export_module.os, "link", side_effect=unsupported):
                exported = export_note(file_path, export_dir, str(case.get("meeting_date_override") or ""))
            if exported.md_created or "不支持安全的原子无覆盖发布" not in exported.md_message:
                errors.append("不支持 hard-link 时 Markdown 导出未明确 fail closed")
            if list(export_dir.rglob("*.md")) or list(export_dir.rglob("*.part")):
                errors.append("Markdown 原子发布失败后残留 final 或 part 文件")
            archive_error = ""
            try:
                with patch.object(archive_module.os, "link", side_effect=unsupported):
                    archive_files(
                        [file_path],
                        archive_dir,
                        str(case.get("archive_date") or "2032-07-01"),
                        str(case.get("meeting_title") or "合成归档"),
                    )
            except OSError as exc:
                archive_error = str(exc)
            if "不支持安全的原子无覆盖归档" not in archive_error:
                errors.append("不支持 hard-link 时原始文件归档未明确 fail closed")
            if list(archive_dir.rglob("*.md")) or list(archive_dir.rglob("*.part")):
                errors.append("原始文件原子归档失败后残留 final 或 part 文件")
        result = {"ok": not errors, "errors": errors, "warnings": warnings}
    elif case.get("check") == "archive_concurrent":
        with tempfile.TemporaryDirectory(prefix="meeting-minutes-archive-concurrent-") as tmpdir:
            errors = []
            count = int(case.get("count") or 8)
            with ThreadPoolExecutor(max_workers=count) as executor:
                archived_batches = list(
                    executor.map(
                        lambda _: archive_files(
                            [file_path],
                            Path(tmpdir),
                            str(case.get("archive_date") or "2032-07-01"),
                            str(case.get("meeting_title") or "synthetic concurrent archive"),
                        ),
                        range(count),
                    )
                )
            paths = [batch[0] for batch in archived_batches]
            if len(set(paths)) != count:
                errors.append(f"并发归档路径不唯一: expected={count} actual={len(set(paths))}")
            source_bytes = file_path.read_bytes()
            for path in paths:
                if not path.is_file() or path.read_bytes() != source_bytes:
                    errors.append(f"并发归档文件缺失或内容不一致: {path}")
            if list(Path(tmpdir).rglob("*.part")):
                errors.append("并发归档后残留隐藏 part 文件")
            result = {
                "ok": not errors,
                "errors": errors,
                "warnings": [],
                "archive_count": count,
                "unique_path_count": len(set(paths)),
            }
    elif case.get("check") == "process_transcript":
        input_text = str(case.get("input_text") or "")
        errors = []
        warnings = []
        segments: list[tuple[str, str]] = []
        output = ""
        if not input_text.strip():
            errors.append("输入文本为空，无法预处理会议转录")
        else:
            preferred_speakers = [
                str(item)
                for item in case.get("preferred_speakers", [])
                if str(item).strip()
            ]
            segments = detect_segments(input_text, preferred_speakers or None)
            output = build_output(segments, input_text)
            expected_segment_count = case.get("expected_segment_count")
            if expected_segment_count is not None and len(segments) != int(expected_segment_count):
                errors.append(f"预处理发言段数不符合预期: expected={expected_segment_count} actual={len(segments)}")
            for term in [str(term) for term in case.get("required_terms", [])]:
                if term not in output:
                    errors.append(f"缺少预处理输出锚点: {term}")
            for term in [str(term) for term in case.get("forbidden_terms", [])]:
                if term in output:
                    errors.append(f"包含预处理禁止锚点: {term}")
        result = {
            "ok": not errors,
            "errors": errors,
            "warnings": warnings,
            "segment_count": len(segments),
        }
    elif case.get("check") == "sensevoice_bridge_form":
        errors = []
        warnings = []
        form = MultipartForm()
        if case.get("include_audio"):
            form.add_file("audio", UploadedFormFile(str(case.get("filename") or "sample.wav"), b""))
        try:
            require_audio_form_file(form)
        except ValueError as exc:
            errors.append(str(exc))
        result = {
            "ok": not errors,
            "errors": errors,
            "warnings": warnings,
        }
    elif case.get("check") == "sensevoice_empty_result":
        with tempfile.TemporaryDirectory(prefix="sensevoice-empty-result-") as tmpdir:
            ok, text, error = run_sensevoice_subprocess(
                [sys.executable, "-c", "pass"],
                Path(tmpdir),
                "input",
                timeout=5,
            )
        errors = []
        if ok or text or error != "transcription_output_missing":
            errors.append(f"SenseVoice bridge 未拒绝空转写: ok={ok} text={text!r} error={error!r}")
        result = {"ok": not errors, "errors": errors, "warnings": []}
    elif case.get("check") == "sensevoice_managed_outputs":
        errors = []
        with tempfile.TemporaryDirectory(prefix="sensevoice-managed-outputs-") as tmpdir:
            output_dir = Path(tmpdir) / "out"
            output_dir.mkdir()
            input_file = Path(tmpdir) / "input.wav"
            input_file.write_bytes(b"synthetic")
            managed_paths = [
                output_dir / f"input{suffix}"
                for suffix in transcribe_audio_module.SENSEVOICE_MANAGED_OUTPUT_SUFFIXES
            ]
            for path in managed_paths:
                path.write_text("OLD OUTPUT\n", encoding="utf-8")
            fake_funasr = types.ModuleType("funasr")
            fake_funasr.AutoModel = lambda **_: object()  # type: ignore[attr-defined]
            shared_patches = (
                patch.dict(sys.modules, {"funasr": fake_funasr}),
                patch.object(transcribe_audio_module, "_ensure_ffmpeg_for_current_process", lambda: None),
                patch.object(
                    transcribe_audio_module,
                    "_resolve_model_ref",
                    lambda model_name, **_: model_name,
                ),
                patch.object(transcribe_audio_module, "_select_device", lambda _: "cpu"),
            )
            with shared_patches[0], shared_patches[1], shared_patches[2], shared_patches[3], patch.object(
                transcribe_audio_module,
                "_run_sensevoice_vad_segments",
                return_value={"text": "", "sentence_info": [], "timestamp_index": [], "raw": []},
            ), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                empty_code = transcribe_audio_module._run_sensevoice(
                    input_file,
                    output_dir,
                    "iic/SenseVoiceSmall",
                    "zh",
                    "all",
                    False,
                    False,
                    "none",
                    "",
                    False,
                )
            if empty_code != 1 or any(path.read_text(encoding="utf-8") != "OLD OUTPUT\n" for path in managed_paths):
                errors.append("SenseVoice 空结果失败后未保留上一轮完整输出")

            for path in managed_paths:
                path.write_text("OLD OUTPUT\n", encoding="utf-8")
            with patch.dict(sys.modules, {"funasr": fake_funasr}), patch.object(
                transcribe_audio_module,
                "_ensure_ffmpeg_for_current_process",
                lambda: None,
            ), patch.object(
                transcribe_audio_module,
                "_resolve_model_ref",
                lambda model_name, **_: model_name,
            ), patch.object(
                transcribe_audio_module,
                "_select_device",
                lambda _: "cpu",
            ), patch.object(
                transcribe_audio_module,
                "_run_sensevoice_vad_segments",
                return_value={"text": "新转写", "sentence_info": [], "timestamp_index": [], "raw": []},
            ), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                success_code = transcribe_audio_module._run_sensevoice(
                    input_file,
                    output_dir,
                    "iic/SenseVoiceSmall",
                    "zh",
                    "all",
                    False,
                    False,
                    "none",
                    "",
                    False,
                )
            expected_present = {output_dir / "input.txt", output_dir / "input.json"}
            expected_absent = set(managed_paths) - expected_present
            if (
                success_code != 0
                or any(not path.is_file() for path in expected_present)
                or any(path.exists() for path in expected_absent)
                or (output_dir / "input.txt").read_text(encoding="utf-8").strip() != "新转写"
            ):
                errors.append("SenseVoice 成功结果未清除本轮未产生的旧 side outputs")
        result = {"ok": not errors, "errors": errors, "warnings": []}
    elif case.get("check") == "sensevoice_output_transaction":
        errors = []
        with tempfile.TemporaryDirectory(prefix="sensevoice-output-transaction-") as tmpdir:
            output_dir = Path(tmpdir)
            stem = "input"
            old_contents = {
                suffix: f"OLD {suffix}\n"
                for suffix in transcribe_audio_module.SENSEVOICE_MANAGED_OUTPUT_SUFFIXES
            }
            for suffix, content in old_contents.items():
                (output_dir / f"{stem}{suffix}").write_text(content, encoding="utf-8")
            original_replace = os.replace

            def fail_second_publish(source: Any, destination: Any) -> None:
                source_path = Path(source)
                destination_path = Path(destination)
                if source_path.parent.name == "stage" and destination_path.name == f"{stem}.json":
                    raise OSError("synthetic commit failure")
                original_replace(source, destination)

            try:
                with patch.object(transcribe_audio_module.os, "replace", fail_second_publish):
                    transcribe_audio_module._commit_sensevoice_outputs(
                        output_dir,
                        stem,
                        {".txt": "NEW TXT\n", ".json": "NEW JSON\n"},
                    )
            except OSError:
                pass
            else:
                errors.append("SenseVoice 事务提交故障注入未触发")
            for suffix, content in old_contents.items():
                path = output_dir / f"{stem}{suffix}"
                if not path.is_file() or path.read_text(encoding="utf-8") != content:
                    errors.append(f"SenseVoice 提交失败后未恢复旧输出: {suffix}")
            if list(output_dir.glob(f".{stem}.sensevoice-txn-*")):
                errors.append("SenseVoice 提交失败后残留 transaction 目录")

            child_code = "\n".join(
                [
                    "import os, sys",
                    "from pathlib import Path",
                    "import transcribe_audio as target",
                    "output_dir = Path(sys.argv[1])",
                    "stem = sys.argv[2]",
                    "original_replace = target.os.replace",
                    "def abrupt_replace(source, destination):",
                    "    original_replace(source, destination)",
                    "    if Path(source).parent.name == 'stage':",
                    "        os._exit(77)",
                    "target.os.replace = abrupt_replace",
                    "target._commit_sensevoice_outputs(output_dir, stem, {'.txt': 'CRASH TXT\\n', '.json': 'CRASH JSON\\n'})",
                ]
            )
            child_env = os.environ.copy()
            child_env["PYTHONPATH"] = str(SCRIPT_DIR)
            crashed = subprocess.run(
                [sys.executable, "-c", child_code, str(output_dir), stem],
                capture_output=True,
                text=True,
                env=child_env,
                timeout=10,
            )
            if crashed.returncode != 77:
                errors.append(
                    f"SenseVoice abrupt-exit 故障注入未按预期退出: returncode={crashed.returncode} stderr={crashed.stderr}"
                )
            with transcribe_audio_module._sensevoice_stem_lock(output_dir, stem):
                transcribe_audio_module._recover_sensevoice_transactions(output_dir, stem)
            for suffix, content in old_contents.items():
                path = output_dir / f"{stem}{suffix}"
                if not path.is_file() or path.read_text(encoding="utf-8") != content:
                    errors.append(f"SenseVoice abrupt-exit 恢复后旧输出不完整: {suffix}")
            if list(output_dir.glob(f".{stem}.sensevoice-txn-*")):
                errors.append("SenseVoice abrupt-exit 恢复后残留 transaction 目录")

            victim_stem = "foobar"
            metachar_stem = "foo*"
            for suffix, content in old_contents.items():
                (output_dir / f"{victim_stem}{suffix}").write_text(content, encoding="utf-8")
            victim_crash = subprocess.run(
                [sys.executable, "-c", child_code, str(output_dir), victim_stem],
                capture_output=True,
                text=True,
                env=child_env,
                timeout=10,
            )
            if victim_crash.returncode != 77:
                errors.append("SenseVoice metachar stem 隔离故障注入未按预期退出")
            with transcribe_audio_module._sensevoice_stem_lock(output_dir, metachar_stem):
                transcribe_audio_module._recover_sensevoice_transactions(output_dir, metachar_stem)
            if not list(output_dir.glob(f".{victim_stem}.sensevoice-txn-*")):
                errors.append("含 glob 元字符的 stem 错误消费了其他 stem 的遗留事务")
            if any((output_dir / f"{metachar_stem}{suffix}").exists() for suffix in old_contents):
                errors.append("含 glob 元字符的 stem 错误生成了跨 stem 恢复输出")
            with transcribe_audio_module._sensevoice_stem_lock(output_dir, victim_stem):
                transcribe_audio_module._recover_sensevoice_transactions(output_dir, victim_stem)
            for suffix, content in old_contents.items():
                path = output_dir / f"{victim_stem}{suffix}"
                if not path.is_file() or path.read_text(encoding="utf-8") != content:
                    errors.append(f"SenseVoice metachar stem 隔离后 victim 恢复失败: {suffix}")

            active = 0
            max_active = 0
            counter_lock = threading.Lock()

            def lock_probe() -> None:
                nonlocal active, max_active
                with transcribe_audio_module._sensevoice_stem_lock(output_dir, stem):
                    with counter_lock:
                        active += 1
                        max_active = max(max_active, active)
                    time.sleep(0.02)
                    with counter_lock:
                        active -= 1

            with ThreadPoolExecutor(max_workers=2) as executor:
                list(executor.map(lambda _: lock_probe(), range(2)))
            if max_active != 1:
                errors.append(f"同 stem SenseVoice 运行未串行化: max_active={max_active}")
        result = {"ok": not errors, "errors": errors, "warnings": []}
    elif case.get("check") == "mas_artifacts":
        result = validate_mas_artifacts_file(
            file_path,
            required_artifacts=[str(item) for item in case.get("require_artifacts", [])],
        )
    elif case.get("check") == "mas_source_manifest":
        request_payload = json.loads(file_path.read_text(encoding="utf-8"))
        if not isinstance(request_payload, dict):
            raise ValueError(f"MAS source_manifest request 必须是 JSON object: {file_path}")
        manifest, warnings = create_source_manifest(
            request_payload,
            archive_allowed=bool(case.get("archive_allowed", False)),
            archive_status=str(case.get("archive_status") or "not_started"),
            skipped_reason=str(case.get("skipped_reason") or ""),
        )
        artifact = source_manifest_artifact(manifest, "regression-source-manifest")
        result = validate_mas_artifacts_payload(artifact, required_artifacts=["source_manifest"])
        result["warnings"] = [str(warning) for warning in warnings] + result["warnings"]
        expected_source_mode = case.get("expect_source_mode")
        if expected_source_mode and manifest.get("source_mode") != expected_source_mode:
            result["errors"].append(
                f"MAS source_manifest source_mode 不符合预期: expected={expected_source_mode} "
                f"actual={manifest.get('source_mode')}"
            )
            result["ok"] = False
        material_kinds = {
            str(item.get("kind") or "")
            for item in manifest.get("materials", [])
            if isinstance(item, dict)
        }
        for kind in [str(item) for item in case.get("expect_material_kinds", [])]:
            if kind not in material_kinds:
                result["errors"].append(f"MAS source_manifest 缺少 material kind: {kind}")
                result["ok"] = False
        if "expect_archive_allowed" in case and manifest.get("archive_allowed") != bool(case["expect_archive_allowed"]):
            result["errors"].append(
                "MAS source_manifest archive_allowed 不符合预期: "
                f"expected={bool(case['expect_archive_allowed'])} actual={manifest.get('archive_allowed')}"
            )
            result["ok"] = False
        manifest_text = json.dumps(artifact, ensure_ascii=False, sort_keys=True)
        for term in [str(item) for item in case.get("required_terms", [])]:
            if term not in manifest_text:
                result["errors"].append(f"MAS source_manifest 缺少文本锚点: {term}")
                result["ok"] = False
    elif case.get("check") == "mas_source_manifest_cli_binding":
        request_payload = json.loads(file_path.read_text(encoding="utf-8"))
        if not isinstance(request_payload, dict):
            raise ValueError(f"MAS source_manifest request 必须是 JSON object: {file_path}")
        bundle = build_mas_task_bundle_from_request(request_payload)
        conflicting_request = base_dir / str(case["conflicting_request_file"])
        with tempfile.TemporaryDirectory(prefix="mas-source-manifest-cli-") as tmpdir:
            task_dir = Path(tmpdir)
            write_mas_dispatch_files(bundle, task_dir)
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_DIR / "create_mas_source_manifest.py"),
                    "--task-dir",
                    str(task_dir),
                    "--request-json",
                    str(conflicting_request),
                    "--json",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=20,
                check=False,
            )
            errors = []
            artifact_path = task_dir / "artifacts" / "source_manifest.json"
            if completed.returncode != 0 or not artifact_path.is_file():
                errors.append(
                    "MAS source_manifest CLI 未成功生成绑定 artifact: "
                    f"returncode={completed.returncode} output={completed.stdout}{completed.stderr}"
                )
            else:
                bound_bundle = json.loads((task_dir / "mas_task_bundle.json").read_text(encoding="utf-8"))
                artifact_payload = json.loads(artifact_path.read_text(encoding="utf-8"))
                artifact_manifest = artifact_payload.get("artifact", {})
                if artifact_payload.get("run_id") != bound_bundle.get("run_id"):
                    errors.append("MAS source_manifest CLI 写入了非当前 dispatch run_id")
                expected_materials = {
                    str(item.get("name") or "")
                    for item in create_source_manifest(bound_bundle)[0].get("materials", [])
                    if isinstance(item, dict)
                }
                actual_materials = {
                    str(item.get("name") or "")
                    for item in artifact_manifest.get("materials", [])
                    if isinstance(item, dict)
                }
                if actual_materials != expected_materials:
                    errors.append(
                        "MAS source_manifest CLI 材料未绑定当前 dispatch: "
                        f"expected={sorted(expected_materials)} actual={sorted(actual_materials)}"
                    )
            result = {"ok": not errors, "errors": errors, "warnings": []}
    elif case.get("check") == "mas_task_bundle":
        request_payload = json.loads(file_path.read_text(encoding="utf-8"))
        if not isinstance(request_payload, dict):
            raise ValueError(f"MAS task request 必须是 JSON object: {file_path}")
        bundle = build_mas_task_bundle_from_request(request_payload)
        errors = validate_mas_task_bundle(bundle)
        warnings: list[str] = []
        expected_artifacts = [str(item) for item in bundle.get("expected_artifacts", [])]
        artifact_owners = {
            str(key): str(value)
            for key, value in dict(bundle.get("artifact_owners", {})).items()
        }
        roles = [str(task.get("role") or "") for task in bundle.get("tasks", []) if isinstance(task, dict)]
        bundle_text = json.dumps(bundle, ensure_ascii=False, sort_keys=True)
        if "expect_mas_required" in case and bool(bundle.get("mas_required")) != bool(case["expect_mas_required"]):
            errors.append(
                f"MAS task bundle mas_required 不符合预期: expected={bool(case['expect_mas_required'])} "
                f"actual={bool(bundle.get('mas_required'))}"
            )
        for artifact in [str(item) for item in case.get("require_artifacts", [])]:
            if artifact not in expected_artifacts:
                errors.append(f"MAS task bundle 缺少 expected_artifact: {artifact}")
        for artifact in [str(item) for item in case.get("forbid_artifacts", [])]:
            if artifact in expected_artifacts:
                errors.append(f"MAS task bundle 不应包含 expected_artifact: {artifact}")
        for role in [str(item) for item in case.get("require_roles", [])]:
            if role not in roles:
                errors.append(f"MAS task bundle 缺少 role: {role}")
        tasks_by_artifact = {
            str(task.get("artifact_type") or ""): task
            for task in bundle.get("tasks", [])
            if isinstance(task, dict)
        }
        for artifact_type, required_inputs in dict(case.get("require_task_inputs", {})).items():
            task = tasks_by_artifact.get(str(artifact_type))
            if task is None:
                errors.append(f"MAS task bundle 缺少用于检查 inputs 的 task: {artifact_type}")
                continue
            actual_inputs = {str(item) for item in task.get("inputs", [])}
            for required_input in [str(item) for item in required_inputs]:
                if required_input not in actual_inputs:
                    errors.append(
                        f"MAS task bundle {artifact_type} inputs 缺少: {required_input}"
                    )
        for artifact, owner in dict(case.get("require_artifact_owners", {})).items():
            if artifact_owners.get(str(artifact)) != str(owner):
                errors.append(
                    f"MAS task bundle artifact owner 不符合预期: {artifact} "
                    f"expected={owner} actual={artifact_owners.get(str(artifact))}"
                )
        for term in [str(term) for term in case.get("required_terms", [])]:
            if term not in bundle_text:
                errors.append(f"MAS task bundle 缺少文本锚点: {term}")
        for term in [str(term) for term in case.get("forbidden_terms", [])]:
            if term in bundle_text:
                errors.append(f"MAS task bundle 包含禁止锚点: {term}")
        result = {
            "ok": not errors,
            "errors": errors,
            "warnings": warnings,
            "mas_required": bool(bundle.get("mas_required")),
            "expected_artifacts": expected_artifacts,
            "artifact_owners": artifact_owners,
            "roles": roles,
        }
    elif case.get("check") == "mas_task_bundle_reject":
        request_payload = json.loads(file_path.read_text(encoding="utf-8"))
        errors = []
        try:
            bundle = build_mas_task_bundle_from_request(request_payload)
            validation_errors = validate_mas_task_bundle(bundle)
            if validation_errors:
                raise ValueError("; ".join(validation_errors))
        except ValueError as exc:
            errors.append(str(exc))
        result = {
            "ok": not errors,
            "errors": errors,
            "warnings": [],
        }
    elif case.get("check") == "mas_task_bundle_cli":
        command = [
            sys.executable,
            str(SCRIPT_DIR / "build_mas_task_bundle.py"),
            *[str(item) for item in case.get("cli_args", [])],
        ]
        expected_returncode = int(case.get("expect_returncode", 0))
        with tempfile.TemporaryDirectory(prefix="mas-task-bundle-cli-") as tmpdir:
            if case.get("with_task_dir"):
                command.extend(["--task-dir", tmpdir])
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=20,
                check=False,
            )
            errors = []
            if completed.returncode != expected_returncode:
                errors.append(
                    "MAS task bundle CLI returncode 不符合预期: "
                    f"expected={expected_returncode} actual={completed.returncode}"
                )
            output_text = completed.stdout + completed.stderr
            for term in [str(item) for item in case.get("required_terms", [])]:
                if term not in output_text:
                    errors.append(f"MAS task bundle CLI 缺少文本锚点: {term}")
            for filename in [str(item) for item in case.get("require_task_files", [])]:
                if not (Path(tmpdir) / filename).is_file():
                    errors.append(f"MAS task bundle CLI 缺少派发文件: {filename}")
            if case.get("check_repeat_requires_overwrite") and completed.returncode == 0:
                repeated = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=20,
                    check=False,
                )
                repeated_text = repeated.stdout + repeated.stderr
                if repeated.returncode != 1 or "already contains dispatch files" not in repeated_text:
                    errors.append("MAS task bundle CLI 重复派发未要求显式覆盖授权")
                overwritten = subprocess.run(
                    [*command, "--overwrite-dispatch"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=20,
                    check=False,
                )
                if overwritten.returncode != 0:
                    errors.append(
                        "MAS task bundle CLI 显式覆盖派发失败: "
                        + overwritten.stdout
                        + overwritten.stderr
                    )
            result = {
                "ok": not errors,
                "errors": errors,
                "warnings": [],
                "returncode": completed.returncode,
            }
    elif case.get("check") == "mas_task_bundle_mutation_reject":
        request_payload = json.loads(file_path.read_text(encoding="utf-8"))
        bundle = build_mas_task_bundle_from_request(request_payload)
        mutation = str(case.get("mutation") or "")
        if mutation == "audio_profile":
            bundle["run_profile"] = "standard"
        elif mutation == "drop_entity_secondary":
            for task in bundle.get("tasks", []):
                if isinstance(task, dict) and task.get("artifact_type") == "entity_verification_report":
                    task["secondary_artifacts"] = []
                    task.pop("secondary_required_fields", None)
                    break
        elif mutation == "drop_entity_scope":
            removed = {"entity_verification_report", "doubtful_items"}
            bundle["expected_artifacts"] = [
                item for item in bundle.get("expected_artifacts", []) if str(item) not in removed
            ]
            bundle["tasks"] = [
                task
                for task in bundle.get("tasks", [])
                if not isinstance(task, dict) or str(task.get("artifact_type") or "") != "entity_verification_report"
            ]
            for artifact_type in removed:
                bundle.get("artifact_owners", {}).pop(artifact_type, None)
            if isinstance(bundle.get("validation"), dict):
                bundle["validation"]["required_artifacts"] = list(bundle["expected_artifacts"])
        elif mutation == "duplicate_task":
            entity_task = next(
                task
                for task in bundle.get("tasks", [])
                if isinstance(task, dict) and task.get("artifact_type") == "entity_verification_report"
            )
            bundle["tasks"].append(json.loads(json.dumps(entity_task, ensure_ascii=False)))
        elif mutation == "duplicate_expected_artifact":
            bundle["expected_artifacts"].append(bundle["expected_artifacts"][0])
        else:
            raise ValueError(f"未知 MAS bundle mutation: {mutation}")
        mutation_errors = validate_mas_task_bundle(bundle)
        result = {
            "ok": not mutation_errors,
            "errors": mutation_errors,
            "warnings": [],
        }
    elif case.get("check") == "mas_task_dispatch_files":
        request_payload = json.loads(file_path.read_text(encoding="utf-8"))
        if not isinstance(request_payload, dict):
            raise ValueError(f"MAS task request 必须是 JSON object: {file_path}")
        bundle = build_mas_task_bundle_from_request(request_payload)
        errors = validate_mas_task_bundle(bundle)
        warnings: list[str] = []
        with tempfile.TemporaryDirectory(prefix="mas-task-dispatch-") as tmpdir:
            dispatch_result = write_mas_dispatch_files(bundle, Path(tmpdir))
            manifest_path = Path(dispatch_result["manifest_file"])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            bound_bundle = json.loads(Path(dispatch_result["bundle_file"]).read_text(encoding="utf-8"))
            task_files = [Path(path) for path in dispatch_result["task_files"]]
            if manifest.get("schema_version") != "1.0":
                errors.append(f"dispatch_manifest schema_version 不符合预期: {manifest.get('schema_version')}")
            bundle_file = Path(tmpdir) / str(manifest.get("bundle_file") or "")
            if not bundle_file.exists():
                errors.append(f"dispatch_manifest bundle_file 不存在: {manifest.get('bundle_file')}")
            if int(manifest.get("task_count", -1)) != len(task_files):
                errors.append(
                    f"dispatch_manifest task_count 不符合实际文件数: {manifest.get('task_count')} != {len(task_files)}"
                )
            manifest_task_files = manifest.get("task_files")
            if not isinstance(manifest_task_files, list):
                errors.append("dispatch_manifest task_files 必须是 JSON array")
                manifest_task_files = []
            manifest_paths = []
            for item in manifest_task_files:
                if not isinstance(item, dict):
                    errors.append("dispatch_manifest task_files item 必须是 JSON object")
                    continue
                path_name = str(item.get("path") or "")
                manifest_paths.append(path_name)
                if not (Path(tmpdir) / path_name).exists():
                    errors.append(f"dispatch_manifest task file path 不存在: {path_name}")
                if str(item.get("dispatch_phase") or "") not in {"pre_draft", "draft_review", "final_verification"}:
                    errors.append(f"dispatch_manifest task dispatch_phase 不合法: {item.get('dispatch_phase')}")
            actual_names = [path.name for path in task_files]
            if manifest_paths != actual_names:
                errors.append(f"dispatch_manifest task_files 顺序不符合实际生成文件: {manifest_paths} != {actual_names}")
            for filename in [str(item) for item in case.get("require_task_files", [])]:
                if not (Path(tmpdir) / filename).exists():
                    errors.append(f"缺少 MAS dispatch task file: {filename}")
            combined_task_text = "\n".join(path.read_text(encoding="utf-8") for path in task_files)
            manifest_text = json.dumps(manifest, ensure_ascii=False, sort_keys=True)
            for task in bound_bundle.get("tasks", []):
                if not isinstance(task, dict):
                    continue
                shape_result = validate_mas_artifacts_payload(task.get("expected_output_shape"))
                if not shape_result.get("ok"):
                    errors.append(
                        f"MAS task expected_output_shape 无法通过自身 schema: {task.get('artifact_type')}: "
                        + "; ".join(str(item) for item in shape_result.get("errors", []))
                    )
            expected_primary = str(case.get("expect_source_reconciliation_primary") or "")
            if expected_primary:
                source_tasks = [
                    task
                    for task in bound_bundle.get("tasks", [])
                    if isinstance(task, dict) and task.get("artifact_type") == "source_reconciliation"
                ]
                actual_primary = ""
                if len(source_tasks) == 1:
                    shape = source_tasks[0].get("expected_output_shape")
                    if isinstance(shape, dict):
                        artifact = shape.get("artifact")
                        if isinstance(artifact, dict):
                            actual_primary = str(artifact.get("primary_body_source") or "")
                if actual_primary != expected_primary:
                    errors.append(
                        "MAS source_reconciliation expected_output_shape 主源不符合 source_mode: "
                        f"expected={expected_primary} actual={actual_primary}"
                    )
            for term in [str(term) for term in case.get("required_terms", [])]:
                if term not in combined_task_text and term not in manifest_text:
                    errors.append(f"MAS dispatch files 缺少文本锚点: {term}")
            for term in [str(term) for term in case.get("forbidden_terms", [])]:
                if term in combined_task_text or term in manifest_text:
                    errors.append(f"MAS dispatch files 包含禁止锚点: {term}")
            if case.get("check_overwrite_prompt_cleanup"):
                stale_prompt = Path(tmpdir) / "99-stale.prompt.md"
                stale_prompt.write_text("stale prompt\n", encoding="utf-8")
                write_mas_dispatch_files(bundle, Path(tmpdir), overwrite_prompts=True)
                if stale_prompt.exists():
                    errors.append("MAS dispatch 显式覆盖后未清理旧 prompt")
                dispatch_before_reject = {
                    path.name: path.read_bytes()
                    for path in Path(tmpdir).glob("*")
                    if path.is_file() and (
                        path.name in {"mas_task_bundle.json", "dispatch_manifest.json"}
                        or path.name.endswith(".prompt.md")
                    )
                }
                try:
                    write_mas_dispatch_files(bundle, Path(tmpdir), overwrite_prompts=False)
                except ValueError as exc:
                    if "already contains dispatch files" not in str(exc):
                        errors.append(f"MAS dispatch 非覆盖模式错误不符合预期: {exc}")
                else:
                    errors.append("MAS dispatch 非覆盖模式未拒绝既有派发目录")
                dispatch_after_reject = {
                    path.name: path.read_bytes()
                    for path in Path(tmpdir).glob("*")
                    if path.is_file() and (
                        path.name in {"mas_task_bundle.json", "dispatch_manifest.json"}
                        or path.name.endswith(".prompt.md")
                    )
                }
                if dispatch_after_reject != dispatch_before_reject:
                    errors.append("MAS dispatch 非覆盖模式拒绝时仍改写了派发文件")
            result = {
                "ok": not errors,
                "errors": errors,
                "warnings": warnings,
                "task_count": len(task_files),
                "task_file_names": [path.name for path in task_files],
            }
    elif case.get("check") == "mas_collector_corrupt_control":
        request_payload = json.loads(file_path.read_text(encoding="utf-8"))
        if not isinstance(request_payload, dict):
            raise ValueError(f"MAS task request 必须是 JSON object: {file_path}")
        bundle = build_mas_task_bundle_from_request(request_payload)
        with tempfile.TemporaryDirectory(prefix="mas-corrupt-control-") as tmpdir:
            task_dir = Path(tmpdir)
            write_mas_dispatch_files(bundle, task_dir)
            observed: list[dict[str, Any]] = []
            (task_dir / "mas_task_bundle.json").write_text("{invalid-json\n", encoding="utf-8")
            corrupt_bundle = collect_mas_run(task_dir)
            bundle_error_text = "\n".join(str(item) for item in corrupt_bundle.get("errors", []))
            if corrupt_bundle.get("ok") or "无法读取 MAS task bundle" not in bundle_error_text:
                result = {
                    "ok": False,
                    "errors": ["MAS collector 未将损坏 bundle 转成结构化失败"],
                    "warnings": corrupt_bundle.get("warnings", []),
                }
            else:
                observed.append({"case": "corrupt_bundle", "errors": corrupt_bundle.get("errors", [])})
                for path in (task_dir / "artifacts").glob("*.json"):
                    path.unlink()
                write_mas_dispatch_files(bundle, task_dir, overwrite_prompts=True)

                manifest_path = task_dir / "dispatch_manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["task_count"] = int(manifest.get("task_count", 0)) + 1
                manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                corrupt_manifest = collect_mas_run(task_dir)
                manifest_error_text = "\n".join(str(item) for item in corrupt_manifest.get("errors", []))
                if corrupt_manifest.get("ok") or "task_count" not in manifest_error_text:
                    result = {
                        "ok": False,
                        "errors": ["MAS collector 未拦截 task_count 与 task_files 不一致"],
                        "warnings": corrupt_manifest.get("warnings", []),
                    }
                else:
                    observed.append({"case": "corrupt_manifest_task_count", "errors": corrupt_manifest.get("errors", [])})
                    for path in (task_dir / "artifacts").glob("*.json"):
                        path.unlink()
                    write_mas_dispatch_files(bundle, task_dir, overwrite_prompts=True)
                    _, dispatch_manifest = dispatch_context(task_dir)
                    (task_dir / "artifacts").mkdir(parents=True, exist_ok=True)
                    fixture_payload_data = json.loads(
                        (base_dir / "mas_artifacts_valid.json").read_text(encoding="utf-8")
                    )
                    source_manifest = fixture_payload_data["artifacts"]["source_manifest"]
                    reserved_payload = fixture_payload(dispatch_manifest, "source_manifest", source_manifest)
                    reserved_payload["task_artifact_set"] = ["source_manifest"]
                    reserved_payload["ingested_split"] = True
                    (task_dir / "artifacts" / "source_manifest.json").write_text(
                        json.dumps(reserved_payload, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
                    reserved_result = collect_mas_run(task_dir, through_phase="pre_draft")
                    reserved_error_text = "\n".join(str(item) for item in reserved_result.get("errors", []))
                    if reserved_result.get("ok") or "内部拆分字段" not in reserved_error_text:
                        result = {
                            "ok": False,
                            "errors": ["MAS collector 未拒绝直接落盘的内部拆分字段"],
                            "warnings": reserved_result.get("warnings", []),
                        }
                    else:
                        observed.append({"case": "reserved_split_direct_collection", "errors": reserved_result.get("errors", [])})
                        result = {
                            "ok": True,
                            "errors": [],
                            "warnings": [],
                            "observed_structured_errors": observed,
                        }
    elif case.get("check") == "mas_collect_artifacts":
        request_payload = json.loads(file_path.read_text(encoding="utf-8"))
        if not isinstance(request_payload, dict):
            raise ValueError(f"MAS task request 必须是 JSON object: {file_path}")
        artifact_fixture_path = base_dir / str(case["artifact_file"])
        artifact_payload = json.loads(artifact_fixture_path.read_text(encoding="utf-8"))
        fixture_artifacts = artifact_payload.get("artifacts")
        if not isinstance(fixture_artifacts, dict):
            raise ValueError(f"MAS artifact fixture 必须包含 artifacts object: {artifact_fixture_path}")
        bundle = build_mas_task_bundle_from_request(request_payload)
        errors = validate_mas_task_bundle(bundle)
        warnings: list[str] = []
        omitted = {str(item) for item in case.get("omit_artifacts", [])}
        duplicated = {str(item) for item in case.get("duplicate_artifacts", [])}
        with tempfile.TemporaryDirectory(prefix="mas-collect-artifacts-") as tmpdir:
            task_dir = Path(tmpdir)
            write_mas_dispatch_files(bundle, task_dir)
            _, dispatch_manifest = dispatch_context(task_dir)
            synthetic_markdown = task_dir / "synthetic-final.md"
            synthetic_markdown.write_text(synthetic_final_markdown(fixture_artifacts), encoding="utf-8")
            if case.get("invalid_markdown_claimed_valid"):
                synthetic_markdown.write_text("# fake validator pass\n", encoding="utf-8")
            verification_payload = synthetic_verification_payload(fixture_artifacts)
            tamper_sidecar_field = case.get("tamper_sidecar_field")
            if isinstance(tamper_sidecar_field, dict):
                records = verification_payload.get("records")
                if isinstance(records, list) and records and isinstance(records[0], dict):
                    records[0][str(tamper_sidecar_field.get("field") or "")] = tamper_sidecar_field.get("value")
            (task_dir / "synthetic.verification.json").write_text(
                json.dumps(verification_payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            artifact_dir = task_dir / "artifacts"
            artifact_dir.mkdir(parents=True, exist_ok=True)
            deferred_export: tuple[str, Any] | None = None
            for artifact_type, artifact in fixture_artifacts.items():
                artifact_type = str(artifact_type)
                if artifact_type in omitted or artifact_type not in bundle.get("expected_artifacts", []):
                    continue
                if artifact_type == "source_manifest" and case.get("source_manifest_first_material_name"):
                    artifact = json.loads(json.dumps(artifact, ensure_ascii=False))
                    materials = artifact.get("materials") if isinstance(artifact, dict) else None
                    if isinstance(materials, list) and materials and isinstance(materials[0], dict):
                        materials[0]["name"] = str(case["source_manifest_first_material_name"])
                if artifact_type == "source_manifest" and case.get("use_generated_source_manifest"):
                    artifact, _ = create_source_manifest(request_payload, archive_allowed=False)
                if artifact_type == "source_reconciliation" and (
                    "source_reconciliation_primary" in case or "source_reconciliation_cross_check" in case
                ):
                    artifact = json.loads(json.dumps(artifact, ensure_ascii=False))
                    if "source_reconciliation_primary" in case:
                        artifact["primary_body_source"] = case.get("source_reconciliation_primary")
                    if "source_reconciliation_cross_check" in case:
                        artifact["cross_check_source"] = case.get("source_reconciliation_cross_check")
                if case.get("record_main_actions") and artifact_type == "export_manifest":
                    deferred_export = (artifact_type, artifact)
                    continue
                payload = fixture_payload(dispatch_manifest, artifact_type, artifact, synthetic_markdown)
                if artifact_type == str(case.get("top_level_final_field_artifact") or ""):
                    payload["final_markdown"] = "# forbidden direct collector field"
                (artifact_dir / f"{artifact_type}.json").write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                if artifact_type in duplicated:
                    (artifact_dir / f"duplicate-{artifact_type}.json").write_text(
                        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
            if case.get("record_main_actions"):
                result = collect_mas_run(task_dir, through_phase="draft_review")
                summary_path = task_dir / "mas_run_summary.json"
                summary_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                record_main_actions(task_dir, synthetic_markdown, summary_path=summary_path)
                if deferred_export:
                    artifact_type, artifact = deferred_export
                    payload = fixture_payload(dispatch_manifest, artifact_type, artifact, synthetic_markdown)
                    (artifact_dir / f"{artifact_type}.json").write_text(
                        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
                if case.get("tamper_markdown_after_receipt"):
                    synthetic_markdown.write_text(
                        synthetic_markdown.read_text(encoding="utf-8") + "tampered after verification\n",
                        encoding="utf-8",
                    )
            result = collect_mas_run(
                task_dir,
                through_phase=str(case["through_phase"]) if case.get("through_phase") else None,
            )
            result["errors"] = errors + result["errors"]
            result["warnings"] = warnings + result["warnings"]
            result["ok"] = not result["errors"] and bool(result["ok"])
        expected_decision = case.get("expect_decision")
        if expected_decision and result.get("decision", {}).get("decision") != expected_decision:
            result["errors"].append(
                f"MAS collected decision 不符合预期: expected={expected_decision} "
                f"actual={result.get('decision', {}).get('decision')}"
            )
            result["ok"] = False
        for artifact in [str(item) for item in case.get("require_artifacts", [])]:
            if artifact not in result.get("artifact_types", []):
                result["errors"].append(f"MAS collected artifacts 缺少 artifact: {artifact}")
                result["ok"] = False
        expected_next_action = case.get("expect_next_action_type")
        if expected_next_action and result.get("next_action", {}).get("type") != expected_next_action:
            result["errors"].append(
                f"MAS next_action 不符合预期: expected={expected_next_action} "
                f"actual={result.get('next_action', {}).get('type')}"
            )
            result["ok"] = False
        expected_next_phase = case.get("expect_next_phase")
        if expected_next_phase and result.get("next_action", {}).get("phase") != expected_next_phase:
            result["errors"].append(
                f"MAS next_action phase 不符合预期: expected={expected_next_phase} "
                f"actual={result.get('next_action', {}).get('phase')}"
            )
            result["ok"] = False
        next_action_text = json.dumps(result.get("next_action", {}), ensure_ascii=False, sort_keys=True)
        for term in [str(item) for item in case.get("require_next_action_terms", [])]:
            if term not in next_action_text:
                result["errors"].append(f"MAS next_action 缺少文本锚点: {term}")
                result["ok"] = False
        duplicate_types = {
            str(item.get("artifact_type") or "")
            for item in result.get("duplicate_artifacts", [])
            if isinstance(item, dict)
        }
        for artifact_type in [str(item) for item in case.get("require_duplicate_artifacts", [])]:
            if artifact_type not in duplicate_types:
                result["errors"].append(f"MAS duplicate_artifacts 缺少 artifact: {artifact_type}")
                result["ok"] = False
    elif case.get("check") == "mas_dry_run":
        artifact_fixture_path = base_dir / str(case["artifact_file"])
        with tempfile.TemporaryDirectory(prefix="mas-dry-run-") as tmpdir:
            result = run_mas_dry_run(file_path, artifact_fixture_path, Path(tmpdir))
            combined_payload = json.loads(
                Path(str(result.get("combined_artifacts_file") or "")).read_text(encoding="utf-8")
            )
            combined_artifacts = combined_payload.get("artifacts", {})
            result["combined_artifact_types"] = sorted(combined_artifacts) if isinstance(combined_artifacts, dict) else []
            if isinstance(combined_artifacts, dict):
                export_manifest = combined_artifacts.get("export_manifest", {})
                markdown_path = Path(str(export_manifest.get("markdown_path") or "")) if isinstance(export_manifest, dict) else Path()
                result["combined_export_hash_matches"] = bool(
                    isinstance(export_manifest, dict)
                    and markdown_path.is_file()
                    and export_manifest.get("markdown_sha256") == file_sha256(markdown_path)
                )
        expected_phase_order = [str(item) for item in case.get("expect_phase_order", [])]
        if expected_phase_order and result.get("phase_order") != expected_phase_order:
            result["errors"].append(
                f"MAS dry-run phase_order 不符合预期: expected={expected_phase_order} "
                f"actual={result.get('phase_order')}"
            )
            result["ok"] = False
        expected_completed_phase_order = [str(item) for item in case.get("expect_completed_phase_order", [])]
        if expected_completed_phase_order and result.get("completed_phase_order") != expected_completed_phase_order:
            result["errors"].append(
                f"MAS dry-run completed_phase_order 不符合预期: expected={expected_completed_phase_order} "
                f"actual={result.get('completed_phase_order')}"
            )
            result["ok"] = False
        expected_stop_reason = case.get("expect_stop_reason")
        if expected_stop_reason and result.get("stop_reason") != expected_stop_reason:
            result["errors"].append(
                f"MAS dry-run stop_reason 不符合预期: expected={expected_stop_reason} "
                f"actual={result.get('stop_reason')}"
            )
            result["ok"] = False
        phase_results = {
            str(item.get("phase") or ""): item
            for item in result.get("phases", [])
            if isinstance(item, dict)
        }
        for expected_phase_action in case.get("expect_phase_next_actions", []):
            if not isinstance(expected_phase_action, dict):
                result["errors"].append("MAS dry-run expect_phase_next_actions item 必须是 JSON object")
                result["ok"] = False
                continue
            phase = str(expected_phase_action.get("phase") or "")
            phase_result = phase_results.get(phase)
            if not phase_result:
                result["errors"].append(f"MAS dry-run 缺少 phase 结果: {phase}")
                result["ok"] = False
                continue
            next_action = phase_result.get("next_action", {})
            expected_type = expected_phase_action.get("type")
            if expected_type and next_action.get("type") != expected_type:
                result["errors"].append(
                    f"MAS dry-run {phase} next_action 不符合预期: expected={expected_type} "
                    f"actual={next_action.get('type')}"
                )
                result["ok"] = False
            expected_phase = expected_phase_action.get("next_phase")
            if expected_phase and next_action.get("phase") != expected_phase:
                result["errors"].append(
                    f"MAS dry-run {phase} next_action phase 不符合预期: expected={expected_phase} "
                    f"actual={next_action.get('phase')}"
                )
                result["ok"] = False
        expected_next_action = case.get("expect_final_next_action_type")
        if expected_next_action and result.get("final_next_action", {}).get("type") != expected_next_action:
            result["errors"].append(
                f"MAS dry-run final_next_action 不符合预期: expected={expected_next_action} "
                f"actual={result.get('final_next_action', {}).get('type')}"
            )
            result["ok"] = False
        expected_next_phase = case.get("expect_final_next_phase")
        if expected_next_phase and result.get("final_next_action", {}).get("phase") != expected_next_phase:
            result["errors"].append(
                f"MAS dry-run final_next_action phase 不符合预期: expected={expected_next_phase} "
                f"actual={result.get('final_next_action', {}).get('phase')}"
            )
            result["ok"] = False
        for artifact_type in [str(item) for item in case.get("expect_combined_artifacts", [])]:
            if artifact_type not in result.get("combined_artifact_types", []):
                result["errors"].append(f"MAS dry-run combined artifacts 缺少: {artifact_type}")
                result["ok"] = False
        if case.get("expect_combined_export_hash_matches") and not result.get("combined_export_hash_matches"):
            result["errors"].append("MAS dry-run combined export_manifest 哈希未绑定实际 Markdown")
            result["ok"] = False
        trace_text = json.dumps(result, ensure_ascii=False, sort_keys=True)
        for term in [str(item) for item in case.get("require_trace_terms", [])]:
            if term not in trace_text:
                result["errors"].append(f"MAS dry-run trace 缺少文本锚点: {term}")
                result["ok"] = False
    elif case.get("check") == "mas_ingest_artifact":
        request_payload = json.loads(file_path.read_text(encoding="utf-8"))
        if not isinstance(request_payload, dict):
            raise ValueError(f"MAS task request 必须是 JSON object: {file_path}")
        bundle = build_mas_task_bundle_from_request(request_payload)
        errors = validate_mas_task_bundle(bundle)
        warnings: list[str] = []
        artifact_input_path = base_dir / str(case["artifact_file"])
        with tempfile.TemporaryDirectory(prefix="mas-ingest-artifact-") as tmpdir:
            task_dir = Path(tmpdir)
            write_mas_dispatch_files(bundle, task_dir)
            _, dispatch_manifest = dispatch_context(task_dir)
            bound_return_path = bind_fixture_return(
                artifact_input_path,
                task_dir / "returned-artifact.json",
                dispatch_manifest,
            )
            result = ingest_mas_artifact_file(
                bound_return_path,
                task_dir,
                through_phase=str(case["through_phase"]) if case.get("through_phase") else None,
            )
            result["errors"] = errors + result["errors"]
            result["warnings"] = warnings + result["warnings"]
            result["ok"] = not result["errors"] and bool(result["ok"])
            written_types = {
                str(item.get("artifact_type") or "")
                for item in result.get("written_artifacts", [])
                if isinstance(item, dict)
            }
            for artifact in [str(item) for item in case.get("expect_written_artifacts", [])]:
                if artifact not in written_types:
                    result["errors"].append(f"MAS ingest 缺少写入 artifact: {artifact}")
                    result["ok"] = False
            if case.get("expect_repair_history") and not result.get("repair_history_file"):
                result["errors"].append("MAS ingest 未写入 repair_history_file")
                result["ok"] = False
            if case.get("expect_next_collector_term"):
                term = str(case["expect_next_collector_term"])
                if term not in str(result.get("next_collector_command") or ""):
                    result["errors"].append(f"MAS ingest collector command 缺少锚点: {term}")
                    result["ok"] = False
            if case.get("invalid_artifact_file"):
                invalid_return_path = bind_fixture_return(
                    base_dir / str(case["invalid_artifact_file"]),
                    task_dir / "invalid-returned-artifact.json",
                    dispatch_manifest,
                )
                invalid_result = ingest_mas_artifact_file(
                    invalid_return_path,
                    task_dir,
                    through_phase=str(case["through_phase"]) if case.get("through_phase") else None,
                )
                result["invalid_result"] = invalid_result
                if invalid_result.get("ok"):
                    result["errors"].append("MAS ingest 无效 artifact 应失败但实际通过")
                    result["ok"] = False
                if invalid_result.get("ingest_status") != "invalid_artifact_not_written":
                    result["errors"].append(
                        "MAS ingest 无效 artifact 状态不符合预期: "
                        f"{invalid_result.get('ingest_status')}"
                    )
                    result["ok"] = False
                if not invalid_result.get("repair_history_file"):
                    result["errors"].append("MAS ingest 无效 artifact 未写入 repair_history_file")
                    result["ok"] = False
                if case.get("expect_reserved_field_repair"):
                    reserved_errors = "\n".join(str(item) for item in invalid_result.get("errors", []))
                    if "内部拆分字段" not in reserved_errors:
                        result["errors"].append("MAS ingest 未拒绝 subagent 伪造内部拆分字段")
                        result["ok"] = False
            if case.get("expect_duplicate_repair"):
                duplicate_result = ingest_mas_artifact_file(
                    bound_return_path,
                    task_dir,
                    through_phase=str(case["through_phase"]) if case.get("through_phase") else None,
                )
                result["duplicate_result"] = duplicate_result
                if duplicate_result.get("ok"):
                    result["errors"].append("MAS ingest 重复 artifact 应失败但实际通过")
                    result["ok"] = False
                if duplicate_result.get("ingest_status") != "duplicate_artifact_not_written":
                    result["errors"].append(
                        "MAS ingest 重复 artifact 状态不符合预期: "
                        f"{duplicate_result.get('ingest_status')}"
                    )
                    result["ok"] = False
                if not duplicate_result.get("repair_history_file"):
                    result["errors"].append("MAS ingest 重复 artifact 未写入 repair_history_file")
                    result["ok"] = False
            if case.get("expect_transaction_rollback"):
                with tempfile.TemporaryDirectory(prefix="mas-ingest-transaction-") as transaction_tmpdir:
                    transaction_task_dir = Path(transaction_tmpdir)
                    write_mas_dispatch_files(bundle, transaction_task_dir)
                    _, transaction_manifest = dispatch_context(transaction_task_dir)
                    transaction_return = bind_fixture_return(
                        artifact_input_path,
                        transaction_task_dir / "returned-artifact.json",
                        transaction_manifest,
                    )
                    transaction_artifact_dir = transaction_task_dir / "artifacts"
                    real_replace = ingest_mas_artifact_module.os.replace
                    publish_count = 0

                    def fail_second_artifact_publish(source: Any, destination: Any) -> None:
                        nonlocal publish_count
                        source_path = Path(source)
                        destination_path = Path(destination)
                        if source_path.parent.name == "stage" and destination_path.parent == transaction_artifact_dir:
                            publish_count += 1
                            if publish_count == 2:
                                raise OSError(errno.EIO, "synthetic second artifact publish failure")
                        real_replace(source, destination)

                    with patch.object(
                        ingest_mas_artifact_module.os,
                        "replace",
                        side_effect=fail_second_artifact_publish,
                    ):
                        failed_transaction = ingest_mas_artifact_file(
                            transaction_return,
                            transaction_task_dir,
                            through_phase=str(case["through_phase"]) if case.get("through_phase") else None,
                        )
                    result["transaction_failure_result"] = failed_transaction
                    if failed_transaction.get("ok") or failed_transaction.get("ingest_status") != "artifact_transaction_failed_not_written":
                        result["errors"].append("MAS ingest 多 artifact 故障未按事务失败")
                        result["ok"] = False
                    residual_artifacts = sorted(transaction_artifact_dir.glob("*.json"))
                    residual_transactions = sorted(transaction_artifact_dir.glob(".mas-ingest-txn-*"))
                    if residual_artifacts or residual_transactions:
                        result["errors"].append(
                            "MAS ingest 事务失败后残留 artifact 或事务目录: "
                            + ", ".join(str(path) for path in residual_artifacts + residual_transactions)
                        )
                        result["ok"] = False
                    retry_result = ingest_mas_artifact_file(
                        transaction_return,
                        transaction_task_dir,
                        through_phase=str(case["through_phase"]) if case.get("through_phase") else None,
                    )
                    result["transaction_retry_result"] = retry_result
                    retry_types = {
                        str(item.get("artifact_type") or "")
                        for item in retry_result.get("written_artifacts", [])
                        if isinstance(item, dict)
                    }
                    expected_retry_types = {str(item) for item in case.get("expect_written_artifacts", [])}
                    if not retry_result.get("ok") or not expected_retry_types <= retry_types:
                        result["errors"].append("MAS ingest 事务回滚后原返回无法干净重试")
                        result["ok"] = False

                    crash_task_dir = transaction_task_dir / "hard-crash-recovery"
                    write_mas_dispatch_files(bundle, crash_task_dir)
                    _, crash_manifest = dispatch_context(crash_task_dir)
                    crash_return = bind_fixture_return(
                        artifact_input_path,
                        crash_task_dir / "returned-artifact.json",
                        crash_manifest,
                    )
                    crash_script = "\n".join(
                        [
                            "import os, sys",
                            "from pathlib import Path",
                            "sys.path.insert(0, sys.argv[1])",
                            "import ingest_mas_artifact as module",
                            "artifact_dir = Path(sys.argv[3]) / 'artifacts'",
                            "real_replace = module.os.replace",
                            "state = {'publish_count': 0}",
                            "def crash_during_publish(source, destination):",
                            "    source_path = Path(source)",
                            "    destination_path = Path(destination)",
                            "    if source_path.parent.name == 'stage' and destination_path.parent == artifact_dir:",
                            "        state['publish_count'] += 1",
                            "        if state['publish_count'] == 2:",
                            "            os._exit(77)",
                            "    real_replace(source, destination)",
                            "module.os.replace = crash_during_publish",
                            "module.ingest_mas_artifact_file(Path(sys.argv[2]), Path(sys.argv[3]), through_phase=sys.argv[4] or None)",
                        ]
                    )
                    crashed_ingest = subprocess.run(
                        [
                            sys.executable,
                            "-c",
                            crash_script,
                            str(SCRIPT_DIR),
                            str(crash_return),
                            str(crash_task_dir),
                            str(case.get("through_phase") or ""),
                        ],
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        timeout=20,
                        check=False,
                    )
                    if crashed_ingest.returncode != 77:
                        result["errors"].append(
                            "MAS ingest 硬退出注入未生效: "
                            f"returncode={crashed_ingest.returncode}"
                        )
                        result["ok"] = False
                    crash_transactions = list((crash_task_dir / "artifacts").glob(".mas-ingest-txn-*"))
                    if not crash_transactions:
                        result["errors"].append("MAS ingest 硬退出后未保留可恢复事务")
                        result["ok"] = False
                    blocked_collector = collect_mas_run(
                        crash_task_dir,
                        through_phase=str(case["through_phase"]) if case.get("through_phase") else None,
                    )
                    result["hard_crash_collector_result"] = blocked_collector
                    blocked_error_text = "\n".join(
                        str(item) for item in blocked_collector.get("errors", [])
                    )
                    if blocked_collector.get("ok") or "存在未完成 MAS artifact 事务" not in blocked_error_text:
                        result["errors"].append("MAS collector 未阻断硬退出后的半发布 artifact 集合")
                        result["ok"] = False
                    crash_recovery_result = ingest_mas_artifact_file(
                        crash_return,
                        crash_task_dir,
                        through_phase=str(case["through_phase"]) if case.get("through_phase") else None,
                    )
                    result["hard_crash_recovery_result"] = crash_recovery_result
                    crash_warning_text = "\n".join(
                        str(item) for item in crash_recovery_result.get("warnings", [])
                    )
                    if (
                        not crash_recovery_result.get("ok")
                        or "recovered 1 unfinished MAS artifact transaction" not in crash_warning_text
                        or list((crash_task_dir / "artifacts").glob(".mas-ingest-txn-*"))
                    ):
                        result["errors"].append("MAS ingest 硬退出事务未在下次 ingest 自动恢复")
                        result["ok"] = False

                    before_replacement = {
                        path.name: path.read_bytes()
                        for path in transaction_artifact_dir.glob("*.json")
                    }
                    publish_count = 0
                    with patch.object(
                        ingest_mas_artifact_module.os,
                        "replace",
                        side_effect=fail_second_artifact_publish,
                    ):
                        failed_replacement = ingest_mas_artifact_file(
                            transaction_return,
                            transaction_task_dir,
                            through_phase=str(case["through_phase"]) if case.get("through_phase") else None,
                            replace_existing=True,
                        )
                    result["transaction_replacement_failure_result"] = failed_replacement
                    after_failed_replacement = {
                        path.name: path.read_bytes()
                        for path in transaction_artifact_dir.glob("*.json")
                    }
                    if (
                        failed_replacement.get("ok")
                        or failed_replacement.get("ingest_status") != "artifact_transaction_failed_not_written"
                        or after_failed_replacement != before_replacement
                    ):
                        result["errors"].append("MAS ingest 替换事务失败后未完整恢复旧 artifact set")
                        result["ok"] = False

                    real_write_json = ingest_mas_artifact_module.write_json
                    repair_stage_count = 0

                    def fail_second_repair_stage(path: Path, staged_payload: Any) -> None:
                        nonlocal repair_stage_count
                        if path.parent.name == "stage" and path.name.startswith("repair-"):
                            repair_stage_count += 1
                            if repair_stage_count == 2:
                                raise OSError(errno.EIO, "synthetic replacement archive staging failure")
                        real_write_json(path, staged_payload)

                    superseded_before = set((transaction_task_dir / "repair_history").glob("*superseded*.json"))
                    with patch.object(
                        ingest_mas_artifact_module,
                        "write_json",
                        side_effect=fail_second_repair_stage,
                    ):
                        failed_archive_stage = ingest_mas_artifact_file(
                            transaction_return,
                            transaction_task_dir,
                            through_phase=str(case["through_phase"]) if case.get("through_phase") else None,
                            replace_existing=True,
                        )
                    result["replacement_archive_stage_failure_result"] = failed_archive_stage
                    superseded_after = set((transaction_task_dir / "repair_history").glob("*superseded*.json"))
                    after_archive_stage_failure = {
                        path.name: path.read_bytes()
                        for path in transaction_artifact_dir.glob("*.json")
                    }
                    if (
                        failed_archive_stage.get("ok")
                        or failed_archive_stage.get("ingest_status") != "artifact_transaction_failed_not_written"
                        or after_archive_stage_failure != before_replacement
                        or superseded_after != superseded_before
                    ):
                        result["errors"].append("MAS ingest 替换归档预备失败后留下半提交记录")
                        result["ok"] = False

                    publish_count = 0
                    restore_failure_count = 0

                    def fail_publish_and_first_restore(source: Any, destination: Any) -> None:
                        nonlocal publish_count, restore_failure_count
                        source_path = Path(source)
                        destination_path = Path(destination)
                        if source_path.parent.name == "stage" and destination_path.parent == transaction_artifact_dir:
                            publish_count += 1
                            if publish_count == 2:
                                raise OSError(errno.EIO, "synthetic second artifact publish failure")
                        if source_path.parent.name == "backup" and destination_path.parent == transaction_artifact_dir:
                            restore_failure_count += 1
                            if restore_failure_count == 1:
                                raise OSError(errno.EIO, "synthetic backup restore failure")
                        real_replace(source, destination)

                    with patch.object(
                        ingest_mas_artifact_module.os,
                        "replace",
                        side_effect=fail_publish_and_first_restore,
                    ):
                        recovery_required_result = ingest_mas_artifact_file(
                            transaction_return,
                            transaction_task_dir,
                            through_phase=str(case["through_phase"]) if case.get("through_phase") else None,
                            replace_existing=True,
                        )
                    result["transaction_recovery_required_result"] = recovery_required_result
                    pending_transactions = list(transaction_artifact_dir.glob(".mas-ingest-txn-*"))
                    if (
                        recovery_required_result.get("ok")
                        or recovery_required_result.get("ingest_status") != "artifact_transaction_recovery_required"
                        or not pending_transactions
                    ):
                        result["errors"].append("MAS ingest 回滚失败后未保留可重试恢复状态")
                        result["ok"] = False

                    replacement_result = ingest_mas_artifact_file(
                        transaction_return,
                        transaction_task_dir,
                        through_phase=str(case["through_phase"]) if case.get("through_phase") else None,
                        replace_existing=True,
                    )
                    result["transaction_replacement_result"] = replacement_result
                    if (
                        not replacement_result.get("ok")
                        or replacement_result.get("ingest_status") != "replaced"
                        or not replacement_result.get("repair_history_file")
                        or list(transaction_artifact_dir.glob(".mas-ingest-txn-*"))
                    ):
                        result["errors"].append("MAS ingest 显式替换未归档旧值并提交完整 artifact set")
                        result["ok"] = False
            if case.get("expect_identity_guard"):
                bound_payload = json.loads(bound_return_path.read_text(encoding="utf-8"))
                stale_payload = dict(bound_payload)
                stale_payload["run_id"] = "stale-run-id"
                stale_path = task_dir / "stale-run-return.json"
                stale_path.write_text(json.dumps(stale_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                stale_result = ingest_mas_artifact_file(stale_path, task_dir, through_phase="pre_draft")
                stale_errors = "\n".join(str(item) for item in stale_result.get("errors", []))
                if stale_result.get("ok") or "run_id 不匹配" not in stale_errors:
                    result["errors"].append("MAS ingest 未拦截跨 run artifact")
                    result["ok"] = False

                original_manifest = json.loads((task_dir / "dispatch_manifest.json").read_text(encoding="utf-8"))
                stale_manifest = dict(original_manifest)
                stale_manifest["run_id"] = "stale-manifest-run"
                (task_dir / "dispatch_manifest.json").write_text(
                    json.dumps(stale_manifest, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                stale_manifest_result = ingest_mas_artifact_file(
                    bound_return_path,
                    task_dir,
                    through_phase="pre_draft",
                )
                stale_manifest_errors = "\n".join(str(item) for item in stale_manifest_result.get("errors", []))
                if stale_manifest_result.get("ok") or "bundle/manifest run_id 不一致" not in stale_manifest_errors:
                    result["errors"].append("MAS ingest 未拦截 stale manifest run_id")
                    result["ok"] = False
                (task_dir / "dispatch_manifest.json").write_text(
                    json.dumps(original_manifest, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )

                owner_payload = {
                    "run_id": bound_payload.get("run_id"),
                    "task_id": bound_payload.get("task_id"),
                    "dispatch_phase": bound_payload.get("dispatch_phase"),
                    "artifact_owner": bound_payload.get("artifact_owner"),
                    "artifact_type": "source_manifest",
                    "artifact": {
                        "source_mode": "audio_plus_document",
                        "materials": [],
                        "archive_allowed": False,
                        "archive_status": "not_started",
                        "skipped_reason": "synthetic_identity_guard"
                    }
                }
                owner_path = task_dir / "cross-owner-return.json"
                owner_path.write_text(json.dumps(owner_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                owner_result = ingest_mas_artifact_file(owner_path, task_dir, through_phase="pre_draft")
                owner_errors = "\n".join(str(item) for item in owner_result.get("errors", []))
                if owner_result.get("ok") or "artifact_owner 必须为 Main Orchestrator" not in owner_errors:
                    result["errors"].append("MAS ingest 未拦截跨 owner artifact")
                    result["ok"] = False
                forged_main_payload = {
                    **owner_payload,
                    "task_id": f"{bound_payload.get('run_id')}:main:source_manifest",
                    "dispatch_phase": "pre_draft",
                    "artifact_owner": "Main Orchestrator",
                    "artifact": {
                        **owner_payload["artifact"],
                        "materials": [
                            {"kind": "audio", "name": "synthetic_meeting.wav"},
                            {"kind": "document", "name": "provided_transcript.md"},
                        ],
                    },
                }
                forged_main_path = task_dir / "forged-main-owned-return.json"
                forged_main_path.write_text(
                    json.dumps(forged_main_payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                forged_main_result = ingest_mas_artifact_file(
                    forged_main_path,
                    task_dir,
                    through_phase="pre_draft",
                )
                forged_main_errors = "\n".join(str(item) for item in forged_main_result.get("errors", []))
                if forged_main_result.get("ok") or "不接受 Main Orchestrator 自有 artifact" not in forged_main_errors:
                    result["errors"].append("MAS ingest 未拒绝伪造的 main-owned artifact")
                    result["ok"] = False
    elif case.get("check") == "mas_plan_summary":
        summary = json.loads(file_path.read_text(encoding="utf-8"))
        result = plan_from_summary(summary)
        expected_status = str(case.get("expect_plan_status") or "")
        if expected_status and result.get("plan_status") != expected_status:
            result["errors"].append(
                f"MAS plan summary status 不符合预期: expected={expected_status} actual={result.get('plan_status')}"
            )
            result["ok"] = False
        plan_text = json.dumps(result, ensure_ascii=False, sort_keys=True)
        for term in [str(item) for item in case.get("required_terms", [])]:
            if term not in plan_text:
                result["errors"].append(f"MAS plan summary 缺少文本锚点: {term}")
                result["ok"] = False
    elif case.get("check") == "mas_next_action_plan":
        request_payload = json.loads(file_path.read_text(encoding="utf-8"))
        if not isinstance(request_payload, dict):
            raise ValueError(f"MAS task request 必须是 JSON object: {file_path}")
        artifact_fixture_path = base_dir / str(case["artifact_file"])
        artifact_payload = json.loads(artifact_fixture_path.read_text(encoding="utf-8"))
        fixture_artifacts = artifact_payload.get("artifacts")
        if not isinstance(fixture_artifacts, dict):
            raise ValueError(f"MAS artifact fixture 必须包含 artifacts object: {artifact_fixture_path}")
        bundle = build_mas_task_bundle_from_request(request_payload)
        errors = validate_mas_task_bundle(bundle)
        warnings: list[str] = []
        omitted = {str(item) for item in case.get("omit_artifacts", [])}
        with tempfile.TemporaryDirectory(prefix="mas-next-action-plan-") as tmpdir:
            task_dir = Path(tmpdir)
            write_mas_dispatch_files(bundle, task_dir)
            _, dispatch_manifest = dispatch_context(task_dir)
            synthetic_markdown = task_dir / "synthetic-final.md"
            synthetic_markdown.write_text(synthetic_final_markdown(fixture_artifacts), encoding="utf-8")
            artifact_dir = task_dir / "artifacts"
            artifact_dir.mkdir(parents=True, exist_ok=True)
            deferred_export: tuple[str, Any] | None = None
            for artifact_type, artifact in fixture_artifacts.items():
                artifact_type = str(artifact_type)
                if artifact_type in omitted or artifact_type not in bundle.get("expected_artifacts", []):
                    continue
                if case.get("record_main_actions") and artifact_type == "export_manifest":
                    deferred_export = (artifact_type, artifact)
                    continue
                payload = fixture_payload(dispatch_manifest, artifact_type, artifact, synthetic_markdown)
                (artifact_dir / f"{artifact_type}.json").write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
            if case.get("record_main_actions"):
                summary = collect_mas_run(task_dir, through_phase="draft_review")
                summary_path = task_dir / "mas_run_summary.json"
                summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                record_main_actions(task_dir, synthetic_markdown, summary_path=summary_path)
                if deferred_export:
                    artifact_type, artifact = deferred_export
                    payload = fixture_payload(dispatch_manifest, artifact_type, artifact, synthetic_markdown)
                    (artifact_dir / f"{artifact_type}.json").write_text(
                        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
            summary = collect_mas_run(
                task_dir,
                through_phase=str(case["through_phase"]) if case.get("through_phase") else None,
            )
            result = plan_from_summary(summary)
            result["errors"] = errors + result["errors"]
            result["warnings"] = warnings + result["warnings"]
            result["ok"] = not result["errors"] and bool(result["ok"])
        expected_status = case.get("expect_plan_status")
        if expected_status and result.get("plan_status") != expected_status:
            result["errors"].append(
                f"MAS next-action plan status 不符合预期: expected={expected_status} "
                f"actual={result.get('plan_status')}"
            )
            result["ok"] = False
        expected_action_type = case.get("expect_next_action_type")
        if expected_action_type and result.get("next_action_type") != expected_action_type:
            result["errors"].append(
                f"MAS next-action plan next_action_type 不符合预期: expected={expected_action_type} "
                f"actual={result.get('next_action_type')}"
            )
            result["ok"] = False
        expected_phase = case.get("expect_phase")
        if expected_phase and result.get("phase") != expected_phase:
            result["errors"].append(
                f"MAS next-action plan phase 不符合预期: expected={expected_phase} "
                f"actual={result.get('phase')}"
            )
            result["ok"] = False
        dispatch_artifacts = {
            str(item.get("artifact_type") or "")
            for item in result.get("dispatch_tasks", [])
            if isinstance(item, dict)
        }
        for artifact in [str(item) for item in case.get("expect_dispatch_artifacts", [])]:
            if artifact not in dispatch_artifacts:
                result["errors"].append(f"MAS next-action plan 缺少 dispatch artifact: {artifact}")
                result["ok"] = False
        plan_text = json.dumps(result, ensure_ascii=False, sort_keys=True)
        for term in [str(item) for item in case.get("required_terms", [])]:
            if term not in plan_text:
                result["errors"].append(f"MAS next-action plan 缺少文本锚点: {term}")
                result["ok"] = False
    elif case.get("check") == "mas_phase_operator":
        artifact_fixture_path = base_dir / str(case["artifact_file"])
        artifact_payload = json.loads(artifact_fixture_path.read_text(encoding="utf-8"))
        fixture_artifacts = artifact_payload.get("artifacts")
        if not isinstance(fixture_artifacts, dict):
            raise ValueError(f"MAS artifact fixture 必须包含 artifacts object: {artifact_fixture_path}")
        with tempfile.TemporaryDirectory(prefix="mas-phase-operator-") as tmpdir:
            tmp_path = Path(tmpdir)
            task_dir = tmp_path / "dispatch"
            request_payload = json.loads(file_path.read_text(encoding="utf-8"))
            if not isinstance(request_payload, dict):
                raise ValueError(f"MAS phase operator request must be a JSON object: {file_path}")
            initialize_with_request = bool(case.get("initialize_with_request"))
            if initialize_with_request and case.get("return_artifacts"):
                raise ValueError("initialize_with_request regression cannot pre-bind return artifacts")
            if initialize_with_request:
                dispatch_manifest: dict[str, Any] = {}
            else:
                bundle = build_mas_task_bundle_from_request(request_payload)
                write_mas_dispatch_files(bundle, task_dir)
                _, dispatch_manifest = dispatch_context(task_dir)
            returns_dir = tmp_path / "returns"
            returns_dir.mkdir(parents=True, exist_ok=True)
            return_paths: list[Path] = []
            emitted_task_ids: set[str] = set()
            errors: list[str] = []
            for artifact_type in [str(item) for item in case.get("return_artifacts", [])]:
                if artifact_type not in fixture_artifacts:
                    errors.append(f"MAS phase operator fixture 缺少 return artifact: {artifact_type}")
                    continue
                identity = fixture_identity(dispatch_manifest, artifact_type)
                task_id = str(identity.get("task_id") or "")
                if task_id in emitted_task_ids:
                    continue
                emitted_task_ids.add(task_id)
                return_path = returns_dir / f"{artifact_type}.json"
                return_path.write_text(
                    json.dumps(
                        fixture_return_payload(dispatch_manifest, artifact_type, fixture_artifacts),
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                return_paths.append(return_path)
            result = run_mas_phase_operator(
                task_dir=task_dir,
                request_path=file_path if initialize_with_request else None,
                return_paths=return_paths,
                through_phase=str(case["through_phase"]) if case.get("through_phase") else None,
                auto_source_manifest=bool(case.get("auto_source_manifest", False)),
            )
            result["errors"] = errors + result["errors"]
            result["ok"] = not result["errors"] and bool(result["ok"])
            for key in [
                "collector_summary_file",
                "combined_artifacts_file",
                "next_action_plan_file",
                "operator_state_file",
            ]:
                output_path = Path(str(result.get(key) or ""))
                if not output_path.exists():
                    result["errors"].append(f"MAS phase operator 未写入输出文件: {key}")
                    result["ok"] = False
        expected_operator_status = case.get("expect_operator_status")
        if expected_operator_status and result.get("operator_status") != expected_operator_status:
            result["errors"].append(
                f"MAS phase operator status 不符合预期: expected={expected_operator_status} "
                f"actual={result.get('operator_status')}"
            )
            result["ok"] = False
        expected_plan_status = case.get("expect_plan_status")
        if expected_plan_status and result.get("plan_status") != expected_plan_status:
            result["errors"].append(
                f"MAS phase operator plan_status 不符合预期: expected={expected_plan_status} "
                f"actual={result.get('plan_status')}"
            )
            result["ok"] = False
        expected_action_type = case.get("expect_next_action_type")
        if expected_action_type and result.get("next_action_type") != expected_action_type:
            result["errors"].append(
                f"MAS phase operator next_action_type 不符合预期: expected={expected_action_type} "
                f"actual={result.get('next_action_type')}"
            )
            result["ok"] = False
        expected_phase = case.get("expect_phase")
        if expected_phase and result.get("phase") != expected_phase:
            result["errors"].append(
                f"MAS phase operator phase 不符合预期: expected={expected_phase} "
                f"actual={result.get('phase')}"
            )
            result["ok"] = False
        dispatch_artifacts = {
            str(item.get("artifact_type") or "")
            for item in result.get("dispatch_tasks", [])
            if isinstance(item, dict)
        }
        for artifact in [str(item) for item in case.get("expect_dispatch_artifacts", [])]:
            if artifact not in dispatch_artifacts:
                result["errors"].append(f"MAS phase operator 缺少 dispatch artifact: {artifact}")
                result["ok"] = False
        main_owned_artifacts = {str(item) for item in result.get("main_owned_missing_artifacts", [])}
        for artifact in [str(item) for item in case.get("expect_main_owned_artifacts", [])]:
            if artifact not in main_owned_artifacts:
                result["errors"].append(f"MAS phase operator 缺少 main-owned artifact: {artifact}")
                result["ok"] = False
        for artifact in [str(item) for item in case.get("forbid_main_owned_artifacts", [])]:
            if artifact in main_owned_artifacts:
                result["errors"].append(f"MAS phase operator 不应缺少 main-owned artifact: {artifact}")
                result["ok"] = False
        expected_auto_status = case.get("expect_auto_source_manifest_status")
        if expected_auto_status:
            auto_result = result.get("auto_source_manifest", {})
            if not isinstance(auto_result, dict) or auto_result.get("status") != expected_auto_status:
                result["errors"].append(
                    "MAS phase operator auto_source_manifest status 不符合预期: "
                    f"expected={expected_auto_status} actual={auto_result.get('status') if isinstance(auto_result, dict) else auto_result}"
                )
                result["ok"] = False
        operator_text = json.dumps(result, ensure_ascii=False, sort_keys=True)
        for term in [str(item) for item in case.get("required_terms", [])]:
            if term not in operator_text:
                result["errors"].append(f"MAS phase operator 缺少文本锚点: {term}")
                result["ok"] = False
    elif case.get("check") == "mas_phase_operator_full_loop":
        artifact_fixture_path = base_dir / str(case["artifact_file"])
        artifact_payload = json.loads(artifact_fixture_path.read_text(encoding="utf-8"))
        fixture_artifacts = artifact_payload.get("artifacts")
        if not isinstance(fixture_artifacts, dict):
            raise ValueError(f"MAS artifact fixture 必须包含 artifacts object: {artifact_fixture_path}")
        runs: list[dict[str, Any]] = []
        errors: list[str] = []
        warnings: list[str] = []
        with tempfile.TemporaryDirectory(prefix="mas-phase-operator-full-loop-") as tmpdir:
            tmp_path = Path(tmpdir)
            task_dir = tmp_path / "dispatch"
            request_payload = json.loads(file_path.read_text(encoding="utf-8"))
            if not isinstance(request_payload, dict):
                raise ValueError(f"MAS phase operator request must be a JSON object: {file_path}")
            bundle = build_mas_task_bundle_from_request(request_payload)
            write_mas_dispatch_files(bundle, task_dir)
            _, dispatch_manifest = dispatch_context(task_dir)
            synthetic_markdown = task_dir / "synthetic-final.md"
            synthetic_markdown.write_text(synthetic_final_markdown(fixture_artifacts), encoding="utf-8")
            (task_dir / "synthetic.verification.json").write_text(
                json.dumps(synthetic_verification_payload(fixture_artifacts), ensure_ascii=False, indent=2)
                + "\n",
                encoding="utf-8",
            )
            for index, run_spec in enumerate(case.get("runs", []), start=1):
                if not isinstance(run_spec, dict):
                    errors.append("MAS phase operator full-loop run spec 必须是 JSON object")
                    continue
                returns_dir = tmp_path / f"returns-{index:02d}"
                returns_dir.mkdir(parents=True, exist_ok=True)
                return_paths: list[Path] = []
                emitted_task_ids: set[str] = set()
                if run_spec.get("record_main_actions"):
                    record_main_actions(
                        task_dir,
                        synthetic_markdown,
                        summary_path=task_dir / "mas_run_summary.json",
                        replace=(task_dir / "artifacts" / "main_action_receipt.json").exists(),
                    )
                for artifact_type in [str(item) for item in run_spec.get("return_artifacts", [])]:
                    if artifact_type not in fixture_artifacts:
                        errors.append(f"MAS phase operator full-loop fixture 缺少 artifact: {artifact_type}")
                        continue
                    identity = fixture_identity(dispatch_manifest, artifact_type)
                    task_id = str(identity.get("task_id") or "")
                    if task_id in emitted_task_ids:
                        continue
                    emitted_task_ids.add(task_id)
                    return_path = returns_dir / f"{artifact_type}.json"
                    return_path.write_text(
                        json.dumps(
                            fixture_return_payload(
                                dispatch_manifest,
                                artifact_type,
                                fixture_artifacts,
                                synthetic_markdown,
                            ),
                            ensure_ascii=False,
                            indent=2,
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                    return_paths.append(return_path)
                run_result = run_mas_phase_operator(
                    task_dir=task_dir,
                    request_path=None,
                    return_paths=return_paths,
                    through_phase=str(run_spec["through_phase"]) if run_spec.get("through_phase") else None,
                    auto_source_manifest=bool(run_spec.get("auto_source_manifest", False)),
                )
                runs.append(
                    {
                        "index": index,
                        "ok": bool(run_result.get("ok")),
                        "operator_status": run_result.get("operator_status"),
                        "phase": run_result.get("phase"),
                        "next_action_type": run_result.get("next_action_type"),
                        "collector_ok": bool(run_result.get("collector_ok")),
                        "main_actions": run_result.get("main_actions", []),
                        "main_action_checklist": run_result.get("main_action_checklist", []),
                        "dispatch_artifacts": [
                            str(item.get("artifact_type") or "")
                            for item in run_result.get("dispatch_tasks", [])
                            if isinstance(item, dict)
                        ],
                        "errors": run_result.get("errors", []),
                        "warnings": run_result.get("warnings", []),
                    }
                )
        result = {
            "ok": not errors and all(bool(item.get("ok")) for item in runs),
            "errors": errors,
            "warnings": warnings,
            "runs": runs,
        }
        expected_runs = case.get("expect_runs", [])
        for expected in expected_runs:
            if not isinstance(expected, dict):
                result["errors"].append("MAS phase operator full-loop expect_runs item 必须是 JSON object")
                result["ok"] = False
                continue
            index = int(expected.get("index") or 0)
            actual = next((item for item in runs if int(item.get("index") or 0) == index), None)
            if not actual:
                result["errors"].append(f"MAS phase operator full-loop 缺少 run: {index}")
                result["ok"] = False
                continue
            for field_name in ["operator_status", "phase", "next_action_type"]:
                if expected.get(field_name) and actual.get(field_name) != expected.get(field_name):
                    result["errors"].append(
                        f"MAS phase operator full-loop run {index} {field_name} 不符合预期: "
                        f"expected={expected.get(field_name)} actual={actual.get(field_name)}"
                    )
                    result["ok"] = False
            for artifact in [str(item) for item in expected.get("dispatch_artifacts", [])]:
                if artifact not in actual.get("dispatch_artifacts", []):
                    result["errors"].append(
                        f"MAS phase operator full-loop run {index} 缺少 dispatch artifact: {artifact}"
                    )
                    result["ok"] = False
        final_run = runs[-1] if runs else {}
        for action in [str(item) for item in case.get("expect_final_main_actions", [])]:
            if action not in final_run.get("main_actions", []):
                result["errors"].append(f"MAS phase operator full-loop final 缺少 main_action: {action}")
                result["ok"] = False
        trace_text = json.dumps(result, ensure_ascii=False, sort_keys=True)
        for term in [str(item) for item in case.get("required_terms", [])]:
            if term not in trace_text:
                result["errors"].append(f"MAS phase operator full-loop 缺少文本锚点: {term}")
                result["ok"] = False
    elif case.get("check") == "mas_live_pilot_trace":
        trace = json.loads(file_path.read_text(encoding="utf-8"))
        if not isinstance(trace, dict):
            raise ValueError(f"MAS live pilot trace 必须是 JSON object: {file_path}")
        errors = []
        warnings = []
        if trace.get("schema_version") != "1.0":
            errors.append(f"MAS live pilot trace schema_version 不符合预期: {trace.get('schema_version')}")
        if trace.get("execution_mode") != "codex_subagent_synthetic_pilot":
            errors.append(
                "MAS live pilot trace execution_mode 不符合预期: "
                f"{trace.get('execution_mode')}"
            )
        if int(trace.get("subagent_task_count", 0)) < 5:
            errors.append(f"MAS live pilot trace subagent_task_count 过低: {trace.get('subagent_task_count')}")
        if not bool(trace.get("repair_loop_observed")):
            errors.append("MAS live pilot trace 缺少 repair_loop_observed=true")
        boundaries = trace.get("boundaries")
        for boundary in [str(item) for item in case.get("require_boundaries", [])]:
            if not isinstance(boundaries, dict) or boundaries.get(boundary) is not True:
                errors.append(f"MAS live pilot trace 缺少边界确认: {boundary}")
        phases = trace.get("phases")
        if not isinstance(phases, list):
            errors.append("MAS live pilot trace phases 必须是 JSON array")
            phases = []
        phase_results = {
            str(item.get("phase") or ""): item
            for item in phases
            if isinstance(item, dict)
        }
        for expected_phase_action in case.get("expect_phase_next_actions", []):
            if not isinstance(expected_phase_action, dict):
                errors.append("MAS live pilot trace expect_phase_next_actions item 必须是 JSON object")
                continue
            phase = str(expected_phase_action.get("phase") or "")
            phase_result = phase_results.get(phase)
            if not phase_result:
                errors.append(f"MAS live pilot trace 缺少 phase 结果: {phase}")
                continue
            if "collector_ok" in expected_phase_action and bool(phase_result.get("collector_ok")) != bool(
                expected_phase_action.get("collector_ok")
            ):
                errors.append(
                    f"MAS live pilot trace {phase} collector_ok 不符合预期: "
                    f"expected={bool(expected_phase_action.get('collector_ok'))} "
                    f"actual={bool(phase_result.get('collector_ok'))}"
                )
            expected_type = expected_phase_action.get("type")
            if expected_type and phase_result.get("next_action_type") != expected_type:
                errors.append(
                    f"MAS live pilot trace {phase} next_action 不符合预期: "
                    f"expected={expected_type} actual={phase_result.get('next_action_type')}"
                )
            if "next_phase" in expected_phase_action:
                expected_phase = str(expected_phase_action.get("next_phase") or "")
                if str(phase_result.get("next_phase") or "") != expected_phase:
                    errors.append(
                        f"MAS live pilot trace {phase} next_phase 不符合预期: "
                        f"expected={expected_phase} actual={phase_result.get('next_phase')}"
                    )
        expected_decision = case.get("expect_final_decision")
        if expected_decision and trace.get("final_decision") != expected_decision:
            errors.append(
                f"MAS live pilot trace final_decision 不符合预期: "
                f"expected={expected_decision} actual={trace.get('final_decision')}"
            )
        final_actions = [str(item) for item in trace.get("final_main_actions", [])]
        for action in [str(item) for item in case.get("require_final_actions", [])]:
            if action not in final_actions:
                errors.append(f"MAS live pilot trace 缺少 final_main_action: {action}")
        trace_text = json.dumps(trace, ensure_ascii=False, sort_keys=True)
        for term in [str(item) for item in case.get("required_terms", [])]:
            if term not in trace_text:
                errors.append(f"MAS live pilot trace 缺少文本锚点: {term}")
        result = {
            "ok": not errors,
            "errors": errors,
            "warnings": warnings,
            "phase_count": len(phases),
        }
    elif case.get("check") == "mas_decision":
        result = summarize_mas_decision_file(
            file_path,
            required_artifacts=[str(item) for item in case.get("require_artifacts", [])],
        )
        expected_decision = case.get("expect_decision")
        if expected_decision and result.get("decision") != expected_decision:
            result["errors"].append(
                f"MAS decision 不符合预期: expected={expected_decision} actual={result.get('decision')}"
            )
            result["ok"] = False
        actions = [str(item) for item in result.get("main_actions", [])]
        for action in [str(item) for item in case.get("require_actions", [])]:
            if action not in actions:
                result["errors"].append(f"MAS decision 缺少 main_action: {action}")
                result["ok"] = False
    else:
        markdown = file_path.read_text(encoding="utf-8")
        result = validate_contract(
            markdown,
            required_terms=[str(term) for term in case.get("required_terms", [])],
            forbidden_terms=[str(term) for term in case.get("forbidden_terms", [])],
            source_mode=str(case.get("source_mode") or case.get("mode") or "auto"),
            require_audio_timestamps=bool(case.get("require_audio_timestamps")),
            timestamp_mode=str(case.get("timestamp_mode") or "auto"),
        )
        if case.get("verification_file") or case.get("require_verification"):
            verification_path = base_dir / str(case["verification_file"]) if case.get("verification_file") else None
            verification_result = validate_verification_sidecar(
                verification_path,
                require_verification=bool(case.get("require_verification")),
            )
            result["verification"] = verification_result
            result["errors"].extend(verification_result["errors"])
            result["warnings"].extend(verification_result["warnings"])
            result["ok"] = result["ok"] and verification_result["ok"]
        if case.get("timestamp_index_file"):
            timestamp_index_path = base_dir / str(case["timestamp_index_file"])
            timestamp_index_result = validate_timestamp_index_file(
                timestamp_index_path,
                require_reliable=bool(case.get("timestamp_index_require_reliable")),
            )
            result["timestamp_index"] = timestamp_index_result
            result["errors"].extend(timestamp_index_result["errors"])
            result["warnings"].extend(timestamp_index_result["warnings"])
            result["ok"] = result["ok"] and timestamp_index_result["ok"]
    result.setdefault("errors", [])
    result.setdefault("warnings", [])
    raw_ok = bool(result["ok"])
    expect_fail = bool(case.get("expect_fail"))
    required_error_terms = [str(term) for term in case.get("required_error_terms", [])]
    required_warning_terms = [str(term) for term in case.get("required_warning_terms", [])]
    error_text = "\n".join(str(error) for error in result.get("errors", []))
    warning_text = "\n".join(str(warning) for warning in result.get("warnings", []))
    expectation_errors: list[str] = []
    if expect_fail:
        if raw_ok:
            expectation_errors.append("负例应失败但实际通过")
        for term in required_error_terms:
            if term not in error_text:
                expectation_errors.append(f"负例缺少预期错误片段: {term}")
        result["ok"] = not expectation_errors
        result["expected_failure"] = raw_ok is False
        result["expectation_errors"] = expectation_errors
    if not expect_fail and required_warning_terms:
        for term in required_warning_terms:
            if term not in warning_text:
                expectation_errors.append(f"样例缺少预期 warning 片段: {term}")
        result["ok"] = bool(result["ok"]) and not expectation_errors
        result["expectation_errors"] = expectation_errors
    result = {
        "name": case.get("name") or file_path.stem,
        "mode": case.get("mode") or "",
        "file": str(file_path),
        **result,
    }
    return result


def print_text(results: list[dict[str, Any]]) -> None:
    for result in results:
        status = "OK" if result["ok"] else "FAIL"
        print(f"[{status}] {result['name']} ({result['mode']})")
        for warning in result["warnings"]:
            print(f"  warning: {warning}")
        expected_failure = bool(result.get("expected_failure"))
        for error in result["errors"]:
            if expected_failure:
                print(f"  expected failure matched: {error}")
            else:
                print(f"  error: {error}")
        for error in result.get("expectation_errors", []):
            print(f"  expectation-error: {error}")


def main() -> int:
    parser = argparse.ArgumentParser(description="运行会议纪要固定回归样例")
    parser.add_argument("--cases", default=str(DEFAULT_CASES_PATH), help="回归样例 cases.json")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    cases_path = Path(args.cases).expanduser()
    base_dir = cases_path.parent
    try:
        results = [run_case(case, base_dir) for case in read_cases(cases_path)]
    except Exception as exc:
        payload = {
            "ok": False,
            "case_count": 0,
            "errors": [f"回归运行失败: {exc.__class__.__name__}: {exc}"],
            "results": [],
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(payload["errors"][0], file=sys.stderr)
        return 1
    payload = {
        "ok": all(result["ok"] for result in results),
        "case_count": len(results),
        "results": results,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print_text(results)
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
