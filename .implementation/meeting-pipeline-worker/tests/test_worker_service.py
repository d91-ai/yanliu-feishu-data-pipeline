from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCRIPT = ROOT / "unified_worker_service.py"
SPEC = importlib.util.spec_from_file_location("unified_worker_service_tested", SCRIPT)
assert SPEC and SPEC.loader
service = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = service
SPEC.loader.exec_module(service)


def touch(path: Path, body: str = "{}\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def service_env(root: Path) -> dict[str, str]:
    contract = touch(root / "contract.py", "VERSION = 1\n")
    contract_root = root / "pipeline-contract"
    contract = touch(
        contract_root / "meeting_pipeline_contract.py", "VERSION = 1\n"
    )
    touch(contract_root / "contract" / "manifest.json")
    touch(contract_root / "contract" / "artifact-metadata.schema.json")
    touch(contract_root / "contract" / "unified-base.schema.json")
    structured_service_root = root / "structured-service"
    structured_service = touch(
        structured_service_root / "structured_generate_service.py", "VALUE = 1\n"
    )
    structured_service_contract = touch(
        structured_service_root / "skill_contract.py", "VALUE = 1\n"
    )
    industry = root / "industry"
    industry_script = touch(industry / "scripts" / "generate_viewpoints.py")
    touch(industry / "SKILL.md", "skill\n")
    touch(industry / "industry_market_viewpoints" / "core.py", "VALUE = 1\n")
    industry_manifest = touch(
        industry / "contract" / "manifest.json",
        json.dumps(
            {
                "contract_version": 9,
                "schema_version": 9,
                "semantic_prompt": "semantic_prompt.md",
                "claim_units_schema": "claim_units.schema.json",
                "viewpoints_schema": "viewpoints.schema.json",
                "entrypoints": {"generate_table": "scripts/generate_table.py"},
                "runtime_paths": [
                    "SKILL.md",
                    "contract",
                    "industry_market_viewpoints",
                    "scripts/generate_viewpoints.py",
                ]
            }
        ),
    )
    touch(industry / "contract" / "semantic_prompt.md", "prompt\n")
    touch(industry / "contract" / "claim_units.schema.json")
    structured = root / "structured"
    touch(structured / "SKILL.md", "skill\n")
    touch(structured / "structured_table" / "claims.py", "VALUE = 1\n")
    structured_manifest = touch(
        structured / "contract" / "manifest.json",
        json.dumps(
            {
                "runtime_paths": [
                    "SKILL.md",
                    "contract",
                    "structured_table",
                    "scripts/generate_table.py",
                ]
            }
        ),
    )
    structured_script = touch(structured / "scripts" / "generate_table.py")
    speaker_master = touch(
        root / "speaker_master.csv",
        "presenter_id,canonical_name,aliases,status\n",
    )
    touch(structured / "contract" / "semantic_prompt.md", "prompt\n")
    touch(structured / "contract" / "claim_units.schema.json")
    touch(structured / "contract" / "viewpoints.schema.json")
    codex = touch(root / "codex", "#!/bin/sh\nexit 0\n")
    codex.chmod(0o700)
    return {
        "FEISHU_PIPELINE_REVIEW_JOB_SPOOL_DIR": str(root / "review"),
        "FEISHU_PIPELINE_WORKER_RECEIPT_DIR": str(root / "receipts"),
        "FEISHU_PIPELINE_WORKER_LOCK_PATH": str(root / "worker.lock"),
        "FEISHU_PIPELINE_WORK_DIR": str(root / "work"),
        "MEETING_PIPELINE_CONTRACT_PATH": str(contract),
        "FEISHU_STRUCTURED_SERVICE_ROOT": str(structured_service_root),
        "INDUSTRY_MARKET_SKILL_ROOT": str(industry),
        "STRUCTURED_SKILL_ROOT": str(structured),
        "SPEAKER_MASTER_PATH": str(speaker_master),
        "MEETING_PIPELINE_CONTRACT_SHA256": hashlib.sha256(
            contract.read_bytes()
        ).hexdigest(),
        "MEETING_PIPELINE_CONTRACT_RUNTIME_SHA256": service.fixed_runtime_tree_sha256(
            contract_root, service.PIPELINE_CONTRACT_RUNTIME_PATHS
        ),
        "FEISHU_STRUCTURED_SERVICE_SHA256": hashlib.sha256(
            structured_service.read_bytes()
        ).hexdigest(),
        "FEISHU_STRUCTURED_SERVICE_CONTRACT_SHA256": hashlib.sha256(
            structured_service_contract.read_bytes()
        ).hexdigest(),
        "INDUSTRY_MARKET_SKILL_MANIFEST_SHA256": hashlib.sha256(
            industry_manifest.read_bytes()
        ).hexdigest(),
        "INDUSTRY_MARKET_SKILL_SCRIPT_SHA256": hashlib.sha256(
            industry_script.read_bytes()
        ).hexdigest(),
        "INDUSTRY_MARKET_SKILL_RUNTIME_SHA256": service.runtime_tree_sha256(
            industry, industry_manifest
        ),
        "STRUCTURED_SKILL_MANIFEST_SHA256": hashlib.sha256(
            structured_manifest.read_bytes()
        ).hexdigest(),
        "STRUCTURED_SKILL_SCRIPT_SHA256": hashlib.sha256(
            structured_script.read_bytes()
        ).hexdigest(),
        "STRUCTURED_SKILL_RUNTIME_SHA256": service.runtime_tree_sha256(
            structured, structured_manifest
        ),
        "FEISHU_PIPELINE_CODEX_BIN": str(codex),
        "FEISHU_PIPELINE_WORKER_POLL_SECONDS": "0.1",
        "FEISHU_PIPELINE_WORKER_HTTP_PORT": "8792",
    }


class WorkerServiceConfigTests(unittest.TestCase):
    def test_assets_and_worker_paths_validate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = service.WorkerServiceConfig.from_env(service_env(root))
            config.validate_assets()
            self.assertEqual(config.review_job_root, root / "review")
            self.assertEqual(config.receipt_root, root / "receipts")
            self.assertEqual(config.speaker_master_path, root / "speaker_master.csv")
            self.assertEqual(config.reasoning_effort, "medium")

    def test_reasoning_effort_is_configurable_and_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = service_env(root)
            env["FEISHU_PIPELINE_MODEL_REASONING_EFFORT"] = "medium"
            config = service.WorkerServiceConfig.from_env(env)
            self.assertEqual(config.reasoning_effort, "medium")

            env["FEISHU_PIPELINE_MODEL_REASONING_EFFORT"] = "unexpected"
            with self.assertRaises(service.PipelineJobError) as captured:
                service.WorkerServiceConfig.from_env(env)
            self.assertEqual(captured.exception.code, "worker_config_invalid")

    def test_missing_asset_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = service_env(root)
            Path(env["MEETING_PIPELINE_CONTRACT_PATH"]).unlink()
            config = service.WorkerServiceConfig.from_env(env)
            with self.assertRaises(service.PipelineJobError) as captured:
                config.validate_assets()
            self.assertEqual(captured.exception.code, "worker_asset_missing")

    def test_asset_hash_drift_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = service_env(root)
            config = service.WorkerServiceConfig.from_env(env)
            (root / "structured" / "scripts" / "generate_table.py").write_text(
                "changed\n", encoding="utf-8"
            )
            with self.assertRaises(service.PipelineJobError) as captured:
                config.validate_assets()
            self.assertEqual(captured.exception.code, "worker_asset_hash_mismatch")

    def test_transitive_skill_runtime_drift_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = service_env(root)
            config = service.WorkerServiceConfig.from_env(env)
            (root / "structured" / "structured_table" / "claims.py").write_text(
                "VALUE = 2\n", encoding="utf-8"
            )
            with self.assertRaises(service.PipelineJobError) as captured:
                config.validate_assets()
            self.assertEqual(
                captured.exception.code, "worker_skill_runtime_hash_mismatch"
            )

    def test_transitive_pipeline_contract_drift_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = service_env(root)
            config = service.WorkerServiceConfig.from_env(env)
            (
                root
                / "pipeline-contract"
                / "contract"
                / "artifact-metadata.schema.json"
            ).write_text('{"changed":true}\n', encoding="utf-8")
            with self.assertRaises(service.PipelineJobError) as captured:
                config.validate_assets()
            self.assertEqual(
                captured.exception.code, "worker_contract_runtime_hash_mismatch"
            )

    def test_retry_requires_apply_and_moves_only_retryable_terminal_job(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "review"
            failed = touch(root / "failed" / "job-one.json")
            failed.chmod(0o600)
            with self.assertRaises(service.PipelineJobError) as captured:
                service.retry_job(
                    root, job_id="job-one", from_state="failed", apply=False
                )
            self.assertEqual(captured.exception.code, "retry_requires_apply")
            result = service.retry_job(
                root, job_id="job-one", from_state="failed", apply=True
            )
            self.assertEqual(result["to"], "pending")
            self.assertFalse(failed.exists())
            self.assertTrue((root / "pending" / "job-one.json").is_file())

    def test_retry_fails_when_same_job_exists_in_another_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "review"
            touch(root / "failed" / "job-one.json")
            touch(root / "done" / "job-one.json")
            with self.assertRaises(service.PipelineJobError) as captured:
                service.retry_job(
                    root, job_id="job-one", from_state="failed", apply=True
                )
            self.assertEqual(captured.exception.code, "retry_job_state_conflict")


class WorkerLoopTests(unittest.TestCase):
    def test_idle_worker_becomes_ready_and_stops_cleanly(self):
        class IdleWorker:
            def run_once(self):
                return None

        stop = threading.Event()
        status = service.RuntimeStatus()
        thread = threading.Thread(
            target=service.run_worker_loop,
            kwargs={
                "worker": IdleWorker(),
                "poll_seconds": 0.01,
                "stop": stop,
                "status": status,
            },
        )
        thread.start()
        deadline = time.time() + 1
        while not status.snapshot()["ready"] and time.time() < deadline:
            time.sleep(0.01)
        stop.set()
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        self.assertTrue(status.snapshot()["ready"])

    def test_health_and_readiness_endpoints_disclose_no_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status = service.RuntimeStatus()
            status.update(ready=True)
            server = service.HealthServer(("127.0.0.1", 0), status, root / "g", root / "r")
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                port = server.server_address[1]
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/healthz", timeout=2
                ) as response:
                    health = json.load(response)
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/readyz", timeout=2
                ) as response:
                    ready = json.load(response)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=1)
            self.assertEqual(health, {"live": True})
            self.assertTrue(ready["ready"])
            self.assertNotIn("base_token", json.dumps(ready))


if __name__ == "__main__":
    unittest.main()
