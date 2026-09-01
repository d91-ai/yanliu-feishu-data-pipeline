from __future__ import annotations

from contextlib import contextmanager
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import threading
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "unified_pipeline_worker.py"
SPEC = importlib.util.spec_from_file_location("unified_pipeline_worker_tested", SCRIPT)
assert SPEC and SPEC.loader
worker = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = worker
SPEC.loader.exec_module(worker)


UID = "mtg_550e8400e29b41d4a716446655440000"
SOURCE = b"# meeting\n\nmarket and security views\n"
SOURCE_HASH = hashlib.sha256(SOURCE).hexdigest()


def generation_job(artifact_type="industry_market_viewpoints", version=1):
    return {
        "job_version": 1,
        "job_id": f"{UID}-v{version}-{artifact_type}",
        "state": "pending",
        "meeting_uid": UID,
        "record_id": "rec-one",
        "artifact_type": artifact_type,
        "data_version": version,
        "input_file_token": "source-token",
        "input_md_sha256": SOURCE_HASH,
        "meeting_date": "2032-08-13",
        "meeting_series": "示例研究周会",
        "meeting_type": "多人复盘会",
        "source_review_status": "未审核",
        "created_at": "2032-08-13T09:00:00+08:00",
    }


def review_job(artifact_type, token, content, version=1):
    digest = hashlib.sha256(content).hexdigest()
    identity = hashlib.sha256(
        f"rec-one\0{artifact_type}\0{digest}\0review-approved".encode()
    ).hexdigest()
    return {
        "job_version": 1,
        "job_type": "review_update",
        "job_id": identity,
        "record_id": "rec-one",
        "meeting_uid": UID,
        "artifact_type": artifact_type,
        "data_version": version,
        "review_file_token": token,
        "review_url": f"https://example.test/file/{token}",
        "review_md_sha256": digest,
        "review_action": "approved",
        "event_time": "1786579200000",
    }


class FakeGenerator:
    def __init__(self):
        self.draft_calls = []
        self.review_calls = []
        self.fail_type = ""

    def generate_draft(self, job, source_markdown, context):
        self.draft_calls.append((copy.deepcopy(job), source_markdown, copy.deepcopy(context)))
        if self.fail_type == job["artifact_type"]:
            raise worker.PipelineJobError("fixture_generation_failed")
        return worker.GeneratedArtifact(
            artifact_type=job["artifact_type"],
            review_markdown=f"# {job['artifact_type']} review\n",
            json_artifact={"metadata": context, "rows": []},
        )

    def export_reviewed(self, job, review_markdown, context):
        self.review_calls.append((copy.deepcopy(job), review_markdown, copy.deepcopy(context)))
        return worker.GeneratedArtifact(
            artifact_type=job["artifact_type"],
            review_markdown=review_markdown,
            json_artifact={"metadata": context, "rows": []},
        )


class FakeBackend:
    def __init__(self):
        self.record = {
            "record_id": "rec-one",
            "meeting_uid": UID,
            "meeting_date": "2032-08-13",
            "meeting_series": "示例研究周会",
            "meeting_type": "多人复盘会",
            "data_version": 1,
            "source_file_token": "source-token",
            "source_md_sha256": SOURCE_HASH,
            "source_review_status": "未审核",
            "review_file_tokens": {
                "meeting_minutes": "source-token",
                "industry_market_viewpoints": "industry-review",
                "structured_viewpoints": "structured-review",
            },
            "artifact_review_statuses": {
                "industry_market_viewpoints": "未审核",
                "structured_viewpoints": "未审核",
            },
        }
        self.files = {
            "source-token": SOURCE,
            "industry-review": b"# industry reviewed\n",
            "structured-review": b"# structured reviewed\n",
        }
        self.lock = threading.RLock()
        self.receipts = {}
        self.commits = []
        self.queued = []
        self.mutate_before_second_read = False
        self.read_count = 0
        self.reconcile_lost_receipt = False

    @contextmanager
    def record_lock(self, record_id):
        with self.lock:
            yield

    def get_record(self, record_id):
        self.read_count += 1
        if self.mutate_before_second_read and self.read_count == 2:
            self.record["data_version"] += 1
        return copy.deepcopy(self.record)

    def download_file(self, file_token):
        return self.files[file_token]

    def review_receipt(self, job):
        key = (job["meeting_uid"], job["artifact_type"], job["review_md_sha256"])
        value = self.receipts.get(key)
        if (
            value is None
            and self.reconcile_lost_receipt
            and self.record["data_version"] == job["data_version"] + 1
        ):
            value = {"data_version": self.record["data_version"], "reconciled": True}
            self.receipts[key] = value
        return copy.deepcopy(value) if value else None

    def commit_generation(self, job, artifact, *, expected_version):
        if self.record["data_version"] != expected_version:
            raise worker.StaleJob("commit_version_stale")
        self.commits.append(("generation", job["artifact_type"], expected_version))
        self.record["artifact_review_statuses"][job["artifact_type"]] = (
            artifact.json_artifact["metadata"]["artifact_review_status"]
        )
        return {"data_version": expected_version}

    def commit_review(self, job, artifact, *, expected_version):
        if self.record["data_version"] != expected_version:
            raise worker.StaleJob("commit_version_stale")
        self.record["data_version"] += 1
        self.record["artifact_review_statuses"][job["artifact_type"]] = "已审核"
        receipt = {"data_version": self.record["data_version"]}
        self.receipts[(job["meeting_uid"], job["artifact_type"], job["review_md_sha256"])] = receipt
        self.commits.append(("review", job["artifact_type"], expected_version))
        return copy.deepcopy(self.record)

    def commit_source_review(self, job, source_content, *, expected_version):
        if self.record["data_version"] != expected_version:
            raise worker.StaleJob("commit_version_stale")
        self.record["data_version"] += 1
        self.record["source_review_status"] = "已审核"
        self.record["source_md_sha256"] = hashlib.sha256(source_content).hexdigest()
        for artifact_type, status in list(self.record["artifact_review_statuses"].items()):
            self.record["artifact_review_statuses"][artifact_type] = (
                "需重审" if status == "已审核" else "未审核"
            )
        receipt = {"data_version": self.record["data_version"]}
        self.receipts[(job["meeting_uid"], job["artifact_type"], job["review_md_sha256"])] = receipt
        self.commits.append(("source-review", job["artifact_type"], expected_version))
        return copy.deepcopy(self.record)

    def enqueue_generation_jobs(self, record):
        values = [
            f"{record['meeting_uid']}-v{record['data_version']}-{artifact_type}"
            for artifact_type in worker.GENERATION_ARTIFACT_TYPES
        ]
        self.queued.extend(values)
        return values


class WorkerCoreTests(unittest.TestCase):
    def test_successful_command_warning_does_not_log_model_content(self):
        sensitive_output = "synthetic private meeting content"

        def run(command, **kwargs):
            return SimpleNamespace(returncode=0, stdout="", stderr=sensitive_output)

        generator = worker.CandidateArtifactGenerator(
            work_root=Path("/tmp/unused-work-root"),
            industry_skill_root=Path("/tmp/unused-industry-root"),
            structured_skill_root=Path("/tmp/unused-structured-root"),
            command_runner=run,
        )
        with self.assertLogs(level="WARNING") as captured:
            generator._run(["synthetic-command"])
        log_text = "\n".join(captured.output)
        self.assertNotIn(sensitive_output, log_text)
        self.assertIn("stderr_sha256=", log_text)

    def test_candidate_rejects_empty_model_claim_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            industry = root / "industry"
            contract = industry / "contract"
            contract.mkdir(parents=True)
            (contract / "semantic_prompt.md").write_text("read inputs", encoding="utf-8")
            (contract / "claim_units.provider.schema.json").write_text(
                json.dumps(
                    {
                        "type": "object",
                        "required": ["claim_units"],
                        "properties": {"claim_units": {"type": "array"}},
                    }
                ),
                encoding="utf-8",
            )
            (contract / "manifest.json").write_text(
                json.dumps(
                    {
                        "semantic_prompt": "semantic_prompt.md",
                        "provider_claim_units_schema": "claim_units.provider.schema.json",
                    }
                ),
                encoding="utf-8",
            )

            def run(command, **kwargs):
                Path(command[command.index("--output-last-message") + 1]).write_text(
                    '{"claim_units": []}', encoding="utf-8"
                )
                self.assertIn("code_mode", command)
                self.assertIn("code_mode_host", command)
                self.assertEqual(command[-1], "-")
                self.assertIn('model_reasoning_effort="medium"', command)
                self.assertIn('"source_markdown":"meeting source"', kwargs["input"])
                self.assertIn('"source_fragments":[]', kwargs["input"])
                self.assertIn("不要调用工具", kwargs["input"])
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            generator = worker.CandidateArtifactGenerator(
                work_root=root / "work",
                industry_skill_root=industry,
                structured_skill_root=root / "structured",
                reasoning_effort="medium",
                command_runner=run,
            )
            job_dir = root / "job"
            job_dir.mkdir()
            (job_dir / "source.md").write_text("meeting source", encoding="utf-8")
            (job_dir / "source_fragments.json").write_text("[]", encoding="utf-8")

            with self.assertRaises(worker.PipelineJobError) as raised:
                generator._generate_claims("industry_market_viewpoints", job_dir)

            self.assertEqual(raised.exception.code, "model_claim_output_empty")
            self.assertFalse((job_dir / "claim_units.json").exists())

    def test_candidate_structured_generation_uses_v9_cli_for_markdown_and_json(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            structured = root / "structured"
            (structured / "contract").mkdir(parents=True)
            (structured / "scripts").mkdir()
            (structured / "contract" / "semantic_prompt.md").write_text("read source.md", encoding="utf-8")
            (structured / "contract" / "claim_units.schema.json").write_text(
                json.dumps(
                    {
                        "type": "object",
                        "properties": {"claim_units": {"type": "array", "items": {"type": "object", "properties": {}}}},
                    }
                ),
                encoding="utf-8",
            )
            (structured / "contract" / "claim_units.provider.schema.json").write_text(
                json.dumps(
                    {
                        "type": "object",
                        "required": ["claim_units"],
                        "properties": {"claim_units": {"type": "array"}},
                    }
                ),
                encoding="utf-8",
            )
            (structured / "contract" / "manifest.json").write_text(
                json.dumps(
                    {
                        "contract_version": 9,
                        "schema_version": 9,
                        "semantic_prompt": "semantic_prompt.md",
                        "claim_units_schema": "claim_units.schema.json",
                        "provider_claim_units_schema": "claim_units.provider.schema.json",
                        "viewpoints_schema": "viewpoints.schema.json",
                        "speaker_master": {"cli_flag": "--speaker-master"},
                        "entrypoints": {"generate_table": "scripts/generate_table.py"},
                    }
                ),
                encoding="utf-8",
            )
            (structured / "contract" / "viewpoints.schema.json").write_text("{}", encoding="utf-8")
            (structured / "scripts" / "generate_table.py").write_text("# fixture\n", encoding="utf-8")
            speaker_master = root / "speaker_master.csv"
            speaker_master.write_text(
                "presenter_id,canonical_name,aliases,status\n"
                "spk-1,正式姓名,原始姓名,confirmed\n",
                encoding="utf-8",
            )
            commands = []

            def run(command, **_kwargs):
                commands.append(command)
                if "--output-last-message" in command:
                    Path(command[command.index("--output-last-message") + 1]).write_text(
                        '{"claim_units": [{}]}', encoding="utf-8"
                    )
                elif "--structured-markdown" in command:
                    review_path = Path(command[command.index("--structured-markdown") + 1])
                    value = {
                        "metadata": {
                            "meeting_id": UID,
                            "structured_markdown_sha256": hashlib.sha256(
                                review_path.read_bytes()
                            ).hexdigest(),
                            "schema_version": 9,
                            "security_master_version": "unavailable",
                        },
                        "rows": [],
                    }
                    Path(command[command.index("--output") + 1]).write_text(
                        json.dumps(value), encoding="utf-8"
                    )
                else:
                    Path(command[command.index("--output") + 1]).write_text(
                        "# 标的观点结构化表\n", encoding="utf-8"
                    )
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            generator = worker.CandidateArtifactGenerator(
                work_root=root / "work",
                industry_skill_root=root / "industry",
                structured_skill_root=structured,
                speaker_master_path=speaker_master,
                command_runner=run,
            )
            artifact = generator.generate_draft(
                generation_job("structured_viewpoints"),
                SOURCE.decode("utf-8"),
                {"artifact_review_status": "未审核"},
            )

            self.assertEqual(artifact.json_artifact["metadata"]["schema_version"], 9)
            self.assertEqual(artifact.json_artifact["rows"], [])
            self.assertEqual(len(commands), 3)
            draft_command = commands[-2]
            self.assertIn("--speaker-master", draft_command)
            self.assertEqual(
                Path(draft_command[draft_command.index("--speaker-master") + 1]),
                speaker_master,
            )
            skill_command = commands[-1]
            self.assertIn("--structured-markdown", skill_command)
            self.assertIn("--meeting-id", skill_command)
            self.assertNotIn("--json-output", skill_command)
            self.assertNotIn("--generate-source-fragments", skill_command)
            job_dir = next((root / "work").iterdir())
            self.assertFalse((job_dir / "claim_units.json").exists())
            self.assertFalse((job_dir / "context.json").exists())
            result_manifest = json.loads(
                (job_dir / "draft-result.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                result_manifest["speaker_master_sha256"],
                hashlib.sha256(speaker_master.read_bytes()).hexdigest(),
            )

            generator.generate_draft(
                generation_job("structured_viewpoints"),
                SOURCE.decode("utf-8"),
                {"artifact_review_status": "未审核"},
            )
            self.assertEqual(len(commands), 3)

            speaker_master.write_text(
                "presenter_id,canonical_name,aliases,status\n"
                "spk-1,更新姓名,原始姓名,confirmed\n",
                encoding="utf-8",
            )
            generator.generate_draft(
                generation_job("structured_viewpoints"),
                SOURCE.decode("utf-8"),
                {"artifact_review_status": "未审核"},
            )
            self.assertEqual(len(commands), 6)

    def test_candidate_omits_speaker_master_for_skill_without_manifest_capability(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            structured = root / "structured"
            (structured / "contract").mkdir(parents=True)
            (structured / "contract" / "manifest.json").write_text(
                json.dumps({"contract_version": 9, "schema_version": 9}),
                encoding="utf-8",
            )
            speaker_master = root / "speaker_master.csv"
            speaker_master.write_text(
                "presenter_id,canonical_name,aliases,status\n",
                encoding="utf-8",
            )
            generator = worker.CandidateArtifactGenerator(
                work_root=root / "work",
                industry_skill_root=root / "industry",
                structured_skill_root=structured,
                speaker_master_path=speaker_master,
            )
            self.assertIsNone(generator._speaker_master_for_structured_skill())
            self.assertEqual(
                generator._speaker_master_cache_key(
                    generation_job("structured_viewpoints"), reviewed=False
                ),
                "unavailable",
            )

    def test_generation_fresh_reads_source_and_does_not_increment_version(self):
        backend = FakeBackend()
        generator = FakeGenerator()
        result = worker.process_generation_job(generation_job(), backend, generator)
        self.assertEqual(result["status"], "generated")
        self.assertEqual(backend.record["data_version"], 1)
        self.assertEqual(backend.commits, [("generation", "industry_market_viewpoints", 1)])
        self.assertEqual(generator.draft_calls[0][2]["artifact_review_status"], "未审核")

    def test_generation_becomes_stale_if_record_changes_before_commit(self):
        backend = FakeBackend()
        backend.mutate_before_second_read = True
        with self.assertRaisesRegex(worker.StaleJob, "data_version_stale"):
            worker.process_generation_job(generation_job(), backend, FakeGenerator())
        self.assertEqual(backend.commits, [])

    def test_two_generation_branches_fail_independently(self):
        backend = FakeBackend()
        generator = FakeGenerator()
        generator.fail_type = "industry_market_viewpoints"
        with self.assertRaisesRegex(worker.PipelineJobError, "fixture_generation_failed"):
            worker.process_generation_job(generation_job(), backend, generator)
        result = worker.process_generation_job(
            generation_job("structured_viewpoints"), backend, generator
        )
        self.assertEqual(result["status"], "generated")

    def test_branch_review_increments_once_and_duplicate_is_skipped(self):
        backend = FakeBackend()
        generator = FakeGenerator()
        content = backend.files["industry-review"]
        job = review_job("industry_market_viewpoints", "industry-review", content)
        first = worker.process_review_job(job, backend, generator)
        second = worker.process_review_job(job, backend, generator)
        self.assertEqual(first["status"], "reviewed")
        self.assertEqual(first["data_version"], 2)
        self.assertEqual(second["status"], "skipped_duplicate_review")
        self.assertEqual(backend.record["data_version"], 2)
        self.assertEqual(len(generator.review_calls), 1)

    def test_lost_review_receipt_is_reconciled_without_second_version_increment(self):
        backend = FakeBackend()
        generator = FakeGenerator()
        job = review_job(
            "industry_market_viewpoints",
            "industry-review",
            backend.files["industry-review"],
        )
        first = worker.process_review_job(job, backend, generator)
        self.assertEqual(first["data_version"], 2)
        backend.receipts.clear()
        backend.reconcile_lost_receipt = True
        replay = worker.process_review_job(job, backend, generator)
        self.assertEqual(replay["status"], "skipped_duplicate_review")
        self.assertEqual(backend.record["data_version"], 2)
        self.assertEqual(len(generator.review_calls), 1)

    def test_source_review_increments_and_queues_both_new_version_jobs(self):
        backend = FakeBackend()
        job = review_job("meeting_minutes", "source-token", SOURCE)
        result = worker.process_review_job(job, backend, FakeGenerator())
        self.assertEqual(result["status"], "source_reviewed")
        self.assertEqual(result["data_version"], 2)
        self.assertEqual(
            result["generation_queued"],
            [
                f"{UID}-v2-industry_market_viewpoints",
                f"{UID}-v2-structured_viewpoints",
            ],
        )
        self.assertEqual(backend.record["source_review_status"], "已审核")
        duplicate = worker.process_review_job(job, backend, FakeGenerator())
        self.assertEqual(duplicate["status"], "skipped_duplicate_review")
        self.assertEqual(duplicate["generation_queued"], result["generation_queued"])
        self.assertEqual(backend.record["data_version"], 2)

    def test_review_with_old_version_cannot_overwrite_current(self):
        backend = FakeBackend()
        backend.record["data_version"] = 2
        job = review_job(
            "structured_viewpoints", "structured-review", backend.files["structured-review"]
        )
        with self.assertRaisesRegex(worker.StaleJob, "data_version_stale"):
            worker.process_review_job(job, backend, FakeGenerator())


class QueueTests(unittest.TestCase):
    def test_queue_moves_stale_job_to_terminal_state_and_writes_private_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            generation = root / "generation"
            review = root / "review"
            (generation / "pending").mkdir(parents=True)
            job = generation_job()
            job["data_version"] = 2
            path = generation / "pending" / "job.json"
            path.write_text(json.dumps(job), encoding="utf-8")
            backend = FakeBackend()
            queue = worker.QueueWorker(
                generation_root=generation,
                review_root=review,
                receipt_root=root / "receipts",
                lock_path=root / "worker.lock",
                backend=backend,
                generator=FakeGenerator(),
            )
            receipt = queue.run_once()
            self.assertEqual(receipt["result"]["status"], "stale")
            self.assertTrue((generation / "stale" / "job.json").is_file())
            receipt_path = next((root / "receipts").glob("*.json"))
            self.assertEqual(receipt_path.stat().st_mode & 0o777, 0o600)

    def test_queue_keeps_sanitized_failure_detail_in_private_receipt(self):
        class DetailGenerator(FakeGenerator):
            def generate_draft(self, job, source_markdown, context):
                raise worker.PipelineJobError(
                    "fixture_generation_failed", "line one\nline two"
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            generation = root / "generation"
            review = root / "review"
            (generation / "pending").mkdir(parents=True)
            (generation / "pending" / "job.json").write_text(
                json.dumps(generation_job()), encoding="utf-8"
            )
            queue = worker.QueueWorker(
                generation_root=generation,
                review_root=review,
                receipt_root=root / "receipts",
                lock_path=root / "worker.lock",
                backend=FakeBackend(),
                generator=DetailGenerator(),
            )
            receipt = queue.run_once()
            self.assertEqual(receipt["result"]["status"], "failed")
            self.assertEqual(
                receipt["result"]["error_code"], "fixture_generation_failed"
            )
            self.assertEqual(receipt["result"]["error_detail"], "line one line two")
            receipt_path = next((root / "receipts").glob("*.json"))
            self.assertEqual(receipt_path.stat().st_mode & 0o777, 0o600)

    def test_recovery_returns_processing_file_to_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            generation = root / "generation"
            review = root / "review"
            (generation / "processing").mkdir(parents=True)
            (generation / "processing" / "job.json").write_text("{}", encoding="utf-8")
            queue = worker.QueueWorker(
                generation_root=generation,
                review_root=review,
                receipt_root=root / "receipts",
                lock_path=root / "worker.lock",
                backend=FakeBackend(),
                generator=FakeGenerator(),
            )
            queue.recover()
            self.assertTrue((generation / "pending" / "job.json").is_file())


if __name__ == "__main__":
    unittest.main()
