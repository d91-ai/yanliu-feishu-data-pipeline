from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

import semantic_worker as worker
import structured_generate_service as service
from skill_contract import load_skill_contract


MEETING_UID = "mtg_550e8400e29b41d4a716446655440000"
SKILL_ROOT = Path(
    os.environ.get(
        "STRUCTURED_TABLE_SKILL_ROOT_TEST",
        "/skills/meeting-minutes-structured-table",
    )
)
if not (SKILL_ROOT / "scripts" / "generate_table.py").is_file():
    raise unittest.SkipTest("v9 structured-table Skill fixture unavailable")
CONTRACT = load_skill_contract(SKILL_ROOT / "scripts" / "generate_table.py")
ROOT = Path(__file__).resolve().parent


def claim_unit(*, targets: list[dict[str, str]] | None = None) -> dict[str, object]:
    return {
        "presenter": "张三",
        "source_quotes": ["晨星芯片短期若回落可买。"],
        "direction": "看多",
        "time_horizon": "短期",
        "conditions": [{"text": "若估值回落", "types": ["价格/估值"]}],
        "targets": targets
        or [{"target_name": "晨星芯片", "market": "A股"}],
    }


class SchemaV9PipelineTests(unittest.TestCase):
    def test_deployment_assets_use_v9_contract_without_legacy_entrypoint(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
        plist_example = (ROOT / "org.example.researchpipeline.feishu-structured-semantic-worker.plist.example").read_text(encoding="utf-8")

        self.assertIn("COPY skill_contract.py", dockerfile)
        self.assertIn(":/skills/meeting-minutes-structured-table:ro", compose)
        self.assertIn("healthcheck:", compose)
        self.assertNotIn("STRUCTURED_OFFICIAL_JSON_PREPARE_SCRIPT", env_example)
        self.assertNotIn("prepare_official_json.py", env_example)
        self.assertNotIn("STRUCTURED_SYMBOL_UNIVERSE_PATH", plist_example)
        self.assertNotIn("STRUCTURED_TABLE_SKILL_SCRIPT_SHA256", plist_example)

    def test_service_config_uses_manifest_unified_entrypoint_and_bundled_master(self) -> None:
        env = {
            "FEISHU_APP_ID": "app",
            "FEISHU_APP_SECRET": "secret",
            "FEISHU_SOURCE_BITABLE_APP_TOKEN": "source-base",
            "FEISHU_SOURCE_TABLE_ID": "source-table",
            "FEISHU_STRUCTURED_BITABLE_APP_TOKEN": "structured-base",
            "FEISHU_STRUCTURED_TABLE_ID": "structured-table",
            "FEISHU_STRUCTURED_PENDING_FOLDER_TOKEN": "pending",
            "FEISHU_STRUCTURED_ARCHIVE_FOLDER_TOKEN": "archive",
            "FEISHU_STRUCTURED_OFFICIAL_JSON_FOLDER_TOKEN": "official",
            "FEISHU_SOURCE_VERSION_RETENTION_ENFORCE": "true",
            "FEISHU_STRUCTURED_VERSION_RETENTION_ENFORCE": "true",
            "STRUCTURED_TABLE_SKILL_SCRIPT": str(CONTRACT.generate_script),
            "STRUCTURED_OFFICIAL_JSON_PREPARE_SCRIPT": str(CONTRACT.generate_script),
        }
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = service.read_config()

        self.assertEqual(cfg.skill_contract_version, 9)
        self.assertEqual(cfg.skill_script.resolve(), cfg.skill_json_script.resolve())
        self.assertEqual(cfg.security_master_path, CONTRACT.security_master_path)

    def test_worker_ignores_legacy_symbol_universe_environment(self) -> None:
        env = {
            "STRUCTURED_SEMANTIC_JOB_DIR_HOST": "/tmp/semantic-jobs-test",
            "STRUCTURED_TABLE_SKILL_SCRIPT_HOST": str(CONTRACT.generate_script),
            "STRUCTURED_SYMBOL_UNIVERSE_PATH": "/tmp/must-not-be-used.csv",
            "STRUCTURED_SEMANTIC_MODEL_VERSION": "codex-cli-default",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = worker.read_config()

        self.assertEqual(cfg.contract_version, 9)
        self.assertEqual(cfg.schema_version, 9)
        self.assertEqual(cfg.security_master_path, CONTRACT.security_master_path)

    def test_claim_schema_is_single_stage_and_source_grounded(self) -> None:
        schema = json.loads(CONTRACT.claim_schema_path.read_text(encoding="utf-8"))
        item = schema["properties"]["claim_units"]["items"]
        self.assertEqual(
            set(item["required"]),
            {"presenter", "source_quotes", "direction", "time_horizon", "targets"},
        )
        self.assertNotIn("core_viewpoint", item["properties"])
        self.assertNotIn("source_alias", item["properties"]["targets"]["items"]["properties"])

    def test_worker_uses_one_high_reasoning_claim_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            job_dir = Path(tmpdir)
            output = {"claim_units": [claim_unit()]}

            def run_command(cmd, *, timeout):
                self.assertEqual(timeout, 900)
                Path(cmd[cmd.index("--output-last-message") + 1]).write_text(
                    json.dumps(output, ensure_ascii=False), encoding="utf-8"
                )
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            cfg = SimpleNamespace(
                codex_bin="codex",
                command_timeout_seconds=900,
                claim_schema_path=CONTRACT.claim_schema_path,
                semantic_prompt=CONTRACT.prompt,
                model_version="codex-cli-default",
            )
            with mock.patch.object(worker, "run_command", side_effect=run_command) as run_mock:
                worker.run_claim_unit_stage(cfg, job_dir)

            cmd = run_mock.call_args.args[0]
            self.assertIn('model_reasoning_effort="high"', cmd)
            self.assertNotIn("identified_targets.schema.json", " ".join(cmd))
            self.assertEqual(
                json.loads((job_dir / "claim_units.json").read_text()),
                {"claim_units": [claim_unit()]},
            )

    def test_worker_derives_provider_compatible_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "provider.schema.json"
            worker.write_provider_claim_schema(CONTRACT.claim_schema_path, output)
            schema = json.loads(output.read_text(encoding="utf-8"))

        def assert_all_properties_required(node):
            if isinstance(node, dict):
                properties = node.get("properties")
                if node.get("type") == "object" and isinstance(properties, dict):
                    self.assertEqual(set(node["required"]), set(properties))
                for value in node.values():
                    assert_all_properties_required(value)
            elif isinstance(node, list):
                for value in node:
                    assert_all_properties_required(value)

        assert_all_properties_required(schema)
        self.assertNotIn(
            "source_alias",
            schema["properties"]["claim_units"]["items"]["properties"]["targets"]["items"]["properties"],
        )

    def test_service_uses_skill_default_master_for_review_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            skill_script = root / "generate_table.py"
            skill_script.write_text("# test\n", encoding="utf-8")
            source = root / "source.md"
            source.write_text("### 张三\n\n晨星芯片、海岳算力短期若回落可买。\n", encoding="utf-8")
            claims = root / "claim_units.json"
            claims.write_text(
                json.dumps(
                    {"claim_units": [
                        claim_unit(
                            targets=[
                                {"target_name": "晨星芯片", "market": "A股"},
                                {"target_name": "海岳算力", "market": "A股"},
                            ]
                        )
                    ]},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            output = root / "structured.md"

            def run_skill(cmd, **_kwargs):
                output.write_text("# 标的观点审阅表\n\n## 观点 1\n\n## 观点 2\n", encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            cfg = SimpleNamespace(
                skill_script=skill_script,
                skill_script_sha256=service.file_sha256(skill_script),
                max_error_chars=500,
                skill_contract_version=9,
            )
            with mock.patch.object(service, "require_pinned_skill_runtime"), mock.patch.object(
                service.subprocess, "run", side_effect=run_skill
            ) as run_mock:
                row_count = service.run_skill(
                    cfg,
                    source_markdown_path=source,
                    claim_units_path=claims,
                    output_path=output,
                    meeting_uid=MEETING_UID,
                    meeting_date="2032-08-03",
                )

            cmd = run_mock.call_args.args[0]
            self.assertEqual(row_count, 2)
            self.assertIn("--claim-units", cmd)
            self.assertNotIn("--security-master", cmd)
            self.assertEqual(cmd[cmd.index("--meeting-id") + 1], MEETING_UID)
            self.assertNotIn("--schema-version", cmd)

    def test_official_json_uses_unified_entrypoint_and_preserves_reviewed_code(self) -> None:
        markdown = """# 标的观点结构化表

- 会议日期：2032-08-03

## 观点 1

| 观点日期 | 标的名称 | 股票代码 | 市场 | 原始发言人 | 观点方向 | 观点周期 | 持仓信息（辅助） |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2032-08-03 | 晨星芯片 | REVIEWED-CODE | A股 | 张三 | 关注 | 未说明 | 信息不足 |

### 原文限定条件

| 原文条件 | 条件类型 |
| --- | --- |
| 无 | 未分类 |

### 原文依据 1

- 原文定位：Q-abc

> 晨星芯片值得关注。
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            approved = root / "approved.md"
            approved.write_text(markdown, encoding="utf-8")
            cfg = SimpleNamespace(
                skill_script=CONTRACT.generate_script,
                skill_script_sha256=CONTRACT.sha256(CONTRACT.generate_script),
                skill_json_script=CONTRACT.generate_script,
                skill_json_script_sha256=CONTRACT.sha256(CONTRACT.generate_script),
                skill_contract_version=9,
                skill_runtime_sha256=CONTRACT.runtime_sha256,
                security_master_path=CONTRACT.security_master_path,
                security_master_cli_flag=CONTRACT.security_master_cli_flag,
                max_error_chars=500,
            )
            prepared = service.run_generate_structured_json(
                cfg,
                approved_markdown_path=approved,
                output_dir=root / "official",
                json_name="official.json",
                meeting_uid=MEETING_UID,
            )
            envelope = json.loads(Path(prepared["json_path"]).read_text(encoding="utf-8"))

        self.assertEqual(prepared["row_count"], 1)
        self.assertEqual(envelope["rows"][0]["stock_code"], "REVIEWED-CODE")
        self.assertEqual(
            envelope["metadata"]["security_master_version"],
            "sha256:" + CONTRACT.sha256(CONTRACT.security_master_path),
        )

    def test_official_json_accepts_empty_rows_and_hashes_original_bytes(self) -> None:
        content = b"# \xe6\xa0\x87\xe7\x9a\x84\xe8\xa7\x82\xe7\x82\xb9\xe7\xbb\x93\xe6\x9e\x84\xe5\x8c\x96\xe8\xa1\xa8\r\n\r\n- \xe4\xbc\x9a\xe8\xae\xae\xe6\x97\xa5\xe6\x9c\x9f\xef\xbc\x9a2032-08-03\r\n"
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            approved = root / "approved.md"
            approved.write_bytes(content)
            cfg = SimpleNamespace(
                skill_script=CONTRACT.generate_script,
                skill_script_sha256=CONTRACT.sha256(CONTRACT.generate_script),
                skill_json_script=CONTRACT.generate_script,
                skill_json_script_sha256=CONTRACT.sha256(CONTRACT.generate_script),
                skill_contract_version=9,
                skill_runtime_sha256=CONTRACT.runtime_sha256,
                security_master_path=CONTRACT.security_master_path,
                security_master_cli_flag=CONTRACT.security_master_cli_flag,
                max_error_chars=500,
            )
            prepared = service.run_generate_structured_json(
                cfg,
                approved_markdown_path=approved,
                output_dir=root / "official",
                json_name="official.json",
                meeting_uid=MEETING_UID,
            )

        self.assertEqual(prepared["row_count"], 0)
        self.assertEqual(prepared["source_md_hash"], service.hashlib.sha256(content).hexdigest())


if __name__ == "__main__":
    unittest.main()
