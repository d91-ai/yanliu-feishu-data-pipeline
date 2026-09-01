from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "backfill_missing_generation_jobs.py"
SPEC = importlib.util.spec_from_file_location("backfill_missing_generation_jobs_tested", SCRIPT)
module = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(module)


class FakeBackend:
    def _file_token(self, value):
        return str(value or "")


class BackfillTests(unittest.TestCase):
    def test_missing_artifacts_are_branch_specific(self):
        fields = {
            "行业与市场观点MD": "i-md",
            "行业与市场观点审核前MD": "i-base",
            "行业与市场观点JSON": "i-json",
            "标的观点MD": "s-md",
            "标的观点审核前MD": "s-base",
            "标的观点JSON": "",
        }
        self.assertEqual(
            module.missing_artifacts(FakeBackend(), fields),
            ("structured_viewpoints",),
        )

    def test_build_job_binds_source_identity(self):
        job = module.build_job(
            {
                "record_id": "rec1",
                "meeting_uid": "mtg_" + "a" * 32,
                "data_version": 1,
                "source_file_token": "file1",
                "source_md_sha256": "b" * 64,
                "meeting_date": "2032-08-14",
                "meeting_series": "科技",
                "meeting_type": "多人复盘",
                "source_review_status": "未审核",
            },
            "industry_market_viewpoints",
            "2032-08-14T00:00:00+00:00",
        )
        self.assertEqual(job["job_id"], "mtg_" + "a" * 32 + "-v1-industry_market_viewpoints")
        self.assertEqual(job["input_md_sha256"], "b" * 64)

    def test_existing_job_state_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "done").mkdir()
            (root / "done" / "job.json").write_text("{}", encoding="utf-8")
            self.assertEqual(module.existing_job_states(root, "job"), ("done",))


if __name__ == "__main__":
    unittest.main()
