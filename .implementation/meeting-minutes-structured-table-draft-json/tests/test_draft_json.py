from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "generate_draft_json.py"
SPEC = importlib.util.spec_from_file_location("structured_draft_candidate", SCRIPT)
assert SPEC and SPEC.loader
candidate = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = candidate
SPEC.loader.exec_module(candidate)

IMPLEMENTATION_ROOT = ROOT.parent
PIPELINE_CONTRACT = (
    IMPLEMENTATION_ROOT / "meeting-pipeline-contract" / "meeting_pipeline_contract.py"
)
SKILL_ROOT = Path(
    os.environ.get(
        "STRUCTURED_TABLE_SKILL_ROOT_TEST",
        str(Path.home() / ".codex/skills/meeting-minutes-structured-table"),
    )
)
MEETING_UID = "mtg_550e8400e29b41d4a716446655440000"
SOURCE = """**会议日期**：2032-08-13

### 张三
甲辰科技（234567.SZ）我看好，短期可以考虑买入。
"""
CLAIMS = {
    "claim_units": [
        {
            "claim_ref": "c001",
            "source_refs": ["L4"],
            "presenter": "张三",
            "presenter_normalized": "张三",
            "direction": "看多",
            "time_horizon": "短期",
            "horizon_evidence": "短期",
            "position": {
                "state": "信息不足",
                "detail": "",
                "plan": "计划买入",
                "evidence": "可以考虑买入"
            },
            "conditions": [],
            "targets": [
                {
                    "target_name": "甲辰科技",
                    "stock_code": "234567.SZ",
                    "market": "A股"
                }
            ]
        }
    ]
}


def context():
    return {
        "meeting_uid": MEETING_UID,
        "meeting_date": "2032-08-13",
        "meeting_series": "示例研究周会",
        "meeting_type": "多人复盘会",
        "data_version": 1,
        "source_review_status": "未审核",
        "artifact_review_status": "未审核",
        "generated_at": "2032-08-13T09:00:00+08:00",
    }


@unittest.skipUnless(SKILL_ROOT.is_dir(), "current structured Skill is not available")
class StructuredDraftCandidateTests(unittest.TestCase):
    def test_draft_uses_current_skill_rows_and_common_metadata(self):
        review, artifact = candidate.generate_draft(
            meeting_markdown=SOURCE,
            claim_units=CLAIMS,
            context=context(),
            skill_root=SKILL_ROOT,
            pipeline_contract_path=PIPELINE_CONTRACT,
        )
        self.assertIn("# 标的观点审阅表", review)
        self.assertNotIn(MEETING_UID, review)
        self.assertEqual(artifact["metadata"]["artifact_type"], "structured_viewpoints")
        self.assertEqual(artifact["metadata"]["quality_status"], "unreviewed")
        self.assertEqual(artifact["metadata"]["item_count"], 1)
        self.assertEqual(artifact["rows"][0]["target_name"], "甲辰科技")
        self.assertEqual(artifact["rows"][0]["stock_code"], "234567.SZ")

    def test_draft_cannot_claim_reviewed_status(self):
        invalid_context = context()
        invalid_context["artifact_review_status"] = "已审核"
        with self.assertRaisesRegex(candidate.DraftExportError, "cannot claim"):
            candidate.generate_draft(
                meeting_markdown=SOURCE,
                claim_units=CLAIMS,
                context=invalid_context,
                skill_root=SKILL_ROOT,
                pipeline_contract_path=PIPELINE_CONTRACT,
            )

    def test_candidate_refuses_wrong_skill_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            fake_root = Path(directory)
            (fake_root / "contract").mkdir()
            (fake_root / "contract" / "manifest.json").write_text(
                json.dumps({"contract_version": 3, "schema_version": 6}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(candidate.DraftExportError, "contract v4/schema v7"):
                candidate._load_structured_runtime(fake_root)

    def test_reviewed_markdown_is_authoritative_and_uses_shared_metadata(self):
        review, _draft = candidate.generate_draft(
            meeting_markdown=SOURCE,
            claim_units=CLAIMS,
            context=context(),
            skill_root=SKILL_ROOT,
            pipeline_contract_path=PIPELINE_CONTRACT,
        )
        reviewed_context = context()
        reviewed_context.update(
            {
                "source_review_status": "已审核",
                "artifact_review_status": "已审核",
                "source_md_sha256": candidate._sha256_text(SOURCE),
            }
        )
        reviewed = candidate.generate_reviewed(
            review_markdown=review,
            context=reviewed_context,
            skill_root=SKILL_ROOT,
            pipeline_contract_path=PIPELINE_CONTRACT,
        )
        self.assertEqual(reviewed["metadata"]["quality_status"], "reviewed")
        self.assertEqual(reviewed["metadata"]["data_version"], 1)
        self.assertEqual(reviewed["metadata"]["item_count"], 1)
        self.assertEqual(reviewed["rows"][0]["target_name"], "甲辰科技")

        emptied = review.split("## 观点 1", 1)[0].rstrip() + "\n"
        empty_result = candidate.generate_reviewed(
            review_markdown=emptied,
            context=reviewed_context,
            skill_root=SKILL_ROOT,
            pipeline_contract_path=PIPELINE_CONTRACT,
        )
        self.assertEqual(empty_result["rows"], [])

    def test_reviewed_export_requires_review_status(self):
        invalid_context = context()
        invalid_context["source_md_sha256"] = candidate._sha256_text(SOURCE)
        with self.assertRaisesRegex(candidate.DraftExportError, "requires"):
            candidate.generate_reviewed(
                review_markdown="# 标的观点审阅表\n",
                context=invalid_context,
                skill_root=SKILL_ROOT,
                pipeline_contract_path=PIPELINE_CONTRACT,
            )


if __name__ == "__main__":
    unittest.main()
