from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


worker = load_module(
    "structured_semantic_worker",
    ROOT / "feishu-structured-generate" / "semantic_worker.py",
)


class SemanticWorkerTests(unittest.TestCase):
    def test_provider_schema_requires_all_object_properties(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "claim.schema.json"
            output = root / "provider.schema.json"
            source.write_text(
                json.dumps(
                    {
                        "type": "object",
                        "properties": {
                            "claim_units": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "claim_ref": {"type": "string"},
                                        "targets": {
                                            "type": "array",
                                            "items": {
                                                "type": "object",
                                                "properties": {
                                                    "target_name": {"type": "string"},
                                                    "source_alias": {"type": "string"},
                                                },
                                            },
                                        },
                                    },
                                },
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            worker.write_provider_claim_schema(source, output)
            value = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(value["required"], ["claim_units"])
        item = value["properties"]["claim_units"]["items"]
        self.assertEqual(item["required"], ["claim_ref", "targets"])
        target = item["properties"]["targets"]["items"]
        self.assertEqual(target["required"], ["target_name", "source_alias"])
        self.assertEqual(target["properties"]["source_alias"]["type"], ["string", "null"])

    def test_codex_command_is_ephemeral_read_only_and_schema_bound(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            schema = root / "schema.json"
            output = root / "result.json"
            schema.write_text("{}\n", encoding="utf-8")
            cmd = worker.build_codex_command(
                codex_bin="codex",
                job_dir=root,
                schema_path=schema,
                output_path=output,
                prompt="Return JSON.",
                model="gpt-test-model",
            )

        self.assertEqual(cmd[:2], ["codex", "exec"])
        self.assertIn("--ephemeral", cmd)
        self.assertIn("--ignore-user-config", cmd)
        self.assertIn("--ignore-rules", cmd)
        self.assertIn("--sandbox", cmd)
        self.assertEqual(cmd[cmd.index("--sandbox") + 1], "read-only")
        self.assertIn("--output-schema", cmd)
        self.assertIn("--output-last-message", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "gpt-test-model")
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", cmd)

    def test_stage_output_accepts_only_claim_units_object_array(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "claims.json"
            path.write_text('{"claim_units":[{"claim_ref":"c001"}]}', encoding="utf-8")
            self.assertEqual(
                worker.load_stage_output(path, "claim_units"),
                [{"claim_ref": "c001"}],
            )
            path.write_text('{"claim_units":["bad"]}', encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "object array"):
                worker.load_stage_output(path, "claim_units")

    def test_processing_recovery_returns_uncommitted_job_to_pending(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            job = root / "processing" / "job-one"
            job.mkdir(parents=True)
            cfg = type("Cfg", (), {"job_root": root})()
            worker.recover_processing_jobs(cfg)
            self.assertTrue((root / "pending" / "job-one").is_dir())

    def test_retry_interrupted_recovers_transient_eintr(self):
        calls = []

        def operation():
            calls.append(1)
            if len(calls) < 3:
                raise InterruptedError("temporary")
            return "ok"

        self.assertEqual(worker.retry_interrupted(operation, attempts=3, delay_seconds=0), "ok")
        self.assertEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main()
