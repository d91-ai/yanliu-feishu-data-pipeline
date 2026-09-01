from __future__ import annotations

from contextlib import contextmanager
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = ROOT.parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


worker = load("local_e2e_worker", ROOT / "unified_pipeline_worker.py")
INDUSTRY_ROOT = (
    REPOSITORY_ROOT / ".implementation" / "meeting-minutes-industry-market-viewpoints"
)
if str(INDUSTRY_ROOT) not in sys.path:
    sys.path.insert(0, str(INDUSTRY_ROOT))
from industry_market_viewpoints import export_reviewed_artifact, generate_draft_artifacts


STRUCTURED_SKILL_ROOT = Path(
    os.environ.get(
        "STRUCTURED_TABLE_SKILL_ROOT_TEST",
        str(
            REPOSITORY_ROOT
            / ".implementation"
            / "meeting-minutes-structured-table-current"
        ),
    )
)
SPEAKER_MASTER_PATH = REPOSITORY_ROOT / "data" / "speaker_identity" / "speaker_master.csv"
UID = "mtg_550e8400e29b41d4a716446655440000"
SOURCE_TEXT = """**会议日期**：2032-08-13

### 林晓
市场短期风险偏好改善。
甲辰科技（234567.SZ）我看好，短期可以考虑买入。
"""
SOURCE = SOURCE_TEXT.encode("utf-8")
SOURCE_SHA = hashlib.sha256(SOURCE).hexdigest()
INDUSTRY_CLAIMS = [
    {
        "claim_ref": "c001",
        "source_refs": ["L003"],
        "view_scope": "market",
        "subject": "市场风险偏好",
        "presenter": "林晓",
        "view_type": "看多",
        "viewpoint_text": "市场短期风险偏好改善。",
    }
]
STRUCTURED_CLAIMS = {
    "claim_units": [
        {
            "presenter": "林晓",
            "source_quotes": ["甲辰科技（234567.SZ）我看好，短期可以考虑买入。"],
            "direction": "看多",
            "time_horizon": "短期",
            "conditions": [],
            "targets": [
                {
                    "target_name": "甲辰科技",
                    "market": "A股",
                    "position": {
                        "state": "信息不足",
                        "detail": "",
                        "plan": "计划买入",
                    },
                }
            ],
        }
    ]
}


def generation_job(artifact_type, version, source_token, source_hash, source_status):
    return {
        "job_version": 1,
        "job_id": f"{UID}-v{version}-{artifact_type}",
        "state": "pending",
        "meeting_uid": UID,
        "record_id": "rec-one",
        "artifact_type": artifact_type,
        "data_version": version,
        "input_file_token": source_token,
        "input_md_sha256": source_hash,
        "meeting_date": "2032-08-13",
        "meeting_series": "示例研究周会",
        "meeting_type": "多人复盘会",
        "source_review_status": source_status,
        "created_at": "2032-08-13T09:00:00+08:00",
    }


def review_job(artifact_type, version, token, content):
    digest = hashlib.sha256(content).hexdigest()
    return {
        "job_version": 1,
        "job_type": "review_update",
        "job_id": hashlib.sha256(
            f"rec-one\0{artifact_type}\0{digest}\0review-approved".encode()
        ).hexdigest(),
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


class RealCandidateGenerator:
    def _run_structured(self, *, source=None, claims=None, reviewed=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / ("review.md" if source is not None else "reviewed.json")
            command = [sys.executable, str(STRUCTURED_SKILL_ROOT / "scripts" / "generate_table.py")]
            if source is not None:
                source_path = root / "source.md"
                claims_path = root / "claims.json"
                source_path.write_text(source, encoding="utf-8")
                claims_path.write_text(json.dumps(claims, ensure_ascii=False), encoding="utf-8")
                command.extend(["--claim-units", str(claims_path), "--meeting-markdown", str(source_path)])
                command.extend(["--speaker-master", str(SPEAKER_MASTER_PATH)])
            else:
                reviewed_path = root / "reviewed.md"
                reviewed_path.write_text(reviewed, encoding="utf-8")
                command.extend(["--structured-markdown", str(reviewed_path)])
            command.extend(["--meeting-id", UID, "--output", str(output)])
            subprocess.run(command, check=True, text=True, capture_output=True)
            return output.read_text(encoding="utf-8")

    def generate_draft(self, job, source_markdown, context):
        if job["artifact_type"] == "industry_market_viewpoints":
            review, artifact = generate_draft_artifacts(
                source_markdown, INDUSTRY_CLAIMS, context
            )
        else:
            review = self._run_structured(source=source_markdown, claims=STRUCTURED_CLAIMS)
            artifact = json.loads(self._run_structured(reviewed=review))
        return worker.GeneratedArtifact(job["artifact_type"], review, artifact)

    def export_reviewed(self, job, review_markdown, context):
        if job["artifact_type"] == "industry_market_viewpoints":
            artifact = export_reviewed_artifact(review_markdown, context)
        else:
            artifact = json.loads(self._run_structured(reviewed=review_markdown))
        return worker.GeneratedArtifact(job["artifact_type"], review_markdown, artifact)


class E2EBackend:
    def __init__(self):
        self.lock = threading.RLock()
        self.record = {
            "record_id": "rec-one",
            "meeting_uid": UID,
            "meeting_date": "2032-08-13",
            "meeting_series": "示例研究周会",
            "meeting_type": "多人复盘会",
            "data_version": 1,
            "source_file_token": "source-v1",
            "source_md_sha256": SOURCE_SHA,
            "source_review_status": "未审核",
            "review_file_tokens": {
                "meeting_minutes": "source-v1",
                "industry_market_viewpoints": "",
                "structured_viewpoints": "",
            },
            "artifact_review_statuses": {
                "industry_market_viewpoints": "未审核",
                "structured_viewpoints": "未审核",
            },
        }
        self.files = {"source-v1": SOURCE}
        self.current_artifacts = {}
        self.reviewed_artifacts = {}
        self.receipts = {}
        self.generation_jobs = []

    @contextmanager
    def record_lock(self, record_id):
        with self.lock:
            yield

    def get_record(self, record_id):
        return copy.deepcopy(self.record)

    def download_file(self, token):
        return self.files[token]

    def review_receipt(self, job):
        value = self.receipts.get(
            (job["meeting_uid"], job["artifact_type"], job["review_md_sha256"])
        )
        return copy.deepcopy(value) if value else None

    def commit_generation(self, job, artifact, *, expected_version):
        if self.record["data_version"] != expected_version:
            raise worker.StaleJob("version_stale")
        token = f"{job['artifact_type']}-v{expected_version}"
        content = artifact.review_markdown.encode("utf-8")
        self.files[token] = content
        self.record["review_file_tokens"][job["artifact_type"]] = token
        if job["artifact_type"] == "structured_viewpoints":
            self.record["artifact_review_statuses"][job["artifact_type"]] = "未审核"
        else:
            self.record["artifact_review_statuses"][job["artifact_type"]] = artifact.json_artifact[
                "metadata"
            ]["artifact_review_status"]
        self.current_artifacts[job["artifact_type"]] = copy.deepcopy(artifact.json_artifact)
        return {"token": token, "data_version": expected_version}

    def commit_review(self, job, artifact, *, expected_version):
        if self.record["data_version"] != expected_version:
            raise worker.StaleJob("version_stale")
        self.record["data_version"] += 1
        self.record["artifact_review_statuses"][job["artifact_type"]] = "已审核"
        self.reviewed_artifacts[job["artifact_type"]] = copy.deepcopy(artifact.json_artifact)
        receipt = {"data_version": self.record["data_version"]}
        self.receipts[(UID, job["artifact_type"], job["review_md_sha256"])] = receipt
        return copy.deepcopy(self.record)

    def commit_source_review(self, job, source_content, *, expected_version):
        if self.record["data_version"] != expected_version:
            raise worker.StaleJob("version_stale")
        self.record["data_version"] += 1
        token = f"source-v{self.record['data_version']}"
        self.files[token] = source_content
        self.record["source_file_token"] = token
        self.record["review_file_tokens"]["meeting_minutes"] = token
        self.record["source_md_sha256"] = hashlib.sha256(source_content).hexdigest()
        self.record["source_review_status"] = "已审核"
        for artifact_type, status in list(self.record["artifact_review_statuses"].items()):
            self.record["artifact_review_statuses"][artifact_type] = (
                "需重审" if status == "已审核" else "未审核"
            )
        self.receipts[(UID, "meeting_minutes", job["review_md_sha256"])] = {
            "data_version": self.record["data_version"]
        }
        return copy.deepcopy(self.record)

    def enqueue_generation_jobs(self, record):
        jobs = [
            generation_job(
                artifact_type,
                record["data_version"],
                record["source_file_token"],
                record["source_md_sha256"],
                record["source_review_status"],
            )
            for artifact_type in worker.GENERATION_ARTIFACT_TYPES
        ]
        self.generation_jobs = jobs
        return [job["job_id"] for job in jobs]


@unittest.skipUnless(STRUCTURED_SKILL_ROOT.is_dir(), "current structured Skill unavailable")
class LocalEndToEndTests(unittest.TestCase):
    def test_upload_drafts_three_reviews_and_reviewed_jsons(self):
        backend = E2EBackend()
        generator = RealCandidateGenerator()

        for artifact_type in worker.GENERATION_ARTIFACT_TYPES:
            result = worker.process_generation_job(
                generation_job(artifact_type, 1, "source-v1", SOURCE_SHA, "未审核"),
                backend,
                generator,
            )
            self.assertEqual(result["status"], "generated")
            if artifact_type == "structured_viewpoints":
                self.assertEqual(
                    backend.current_artifacts[artifact_type]["metadata"]["schema_version"],
                    9,
                )
                self.assertEqual(
                    backend.current_artifacts[artifact_type]["metadata"]["meeting_id"],
                    UID,
                )
                self.assertEqual(
                    backend.current_artifacts[artifact_type]["rows"][0]["presenter"],
                    "林晓",
                )
                self.assertEqual(
                    backend.current_artifacts[artifact_type]["rows"][0][
                        "presenter_normalized"
                    ],
                    "林晓（主持人）",
                )
            else:
                self.assertEqual(
                    backend.current_artifacts[artifact_type]["metadata"]["quality_status"],
                    "unreviewed",
                )

        source_result = worker.process_review_job(
            review_job("meeting_minutes", 1, "source-v1", SOURCE), backend, generator
        )
        self.assertEqual(source_result["data_version"], 2)
        self.assertEqual(len(backend.generation_jobs), 2)
        for job in backend.generation_jobs:
            worker.process_generation_job(job, backend, generator)

        industry_token = backend.record["review_file_tokens"]["industry_market_viewpoints"]
        industry_result = worker.process_review_job(
            review_job(
                "industry_market_viewpoints",
                2,
                industry_token,
                backend.files[industry_token],
            ),
            backend,
            generator,
        )
        self.assertEqual(industry_result["data_version"], 3)

        structured_token = backend.record["review_file_tokens"]["structured_viewpoints"]
        structured_result = worker.process_review_job(
            review_job(
                "structured_viewpoints",
                3,
                structured_token,
                backend.files[structured_token],
            ),
            backend,
            generator,
        )
        self.assertEqual(structured_result["data_version"], 4)
        self.assertEqual(backend.record["source_review_status"], "已审核")
        self.assertEqual(
            backend.reviewed_artifacts["industry_market_viewpoints"]["metadata"][
                "quality_status"
            ],
            "reviewed",
        )
        self.assertEqual(
            backend.reviewed_artifacts["industry_market_viewpoints"]["metadata"][
                "data_version"
            ],
            3,
        )
        self.assertEqual(
            backend.reviewed_artifacts["structured_viewpoints"]["metadata"]["schema_version"],
            9,
        )
        self.assertEqual(
            backend.reviewed_artifacts["structured_viewpoints"]["metadata"]["meeting_id"],
            UID,
        )
        self.assertEqual(
            backend.reviewed_artifacts["structured_viewpoints"]["rows"][0]["presenter"],
            "林晓",
        )
        self.assertEqual(
            backend.reviewed_artifacts["structured_viewpoints"]["rows"][0][
                "presenter_normalized"
            ],
            "林晓（主持人）",
        )


if __name__ == "__main__":
    unittest.main()
