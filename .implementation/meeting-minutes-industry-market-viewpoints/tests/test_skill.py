from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


SKILL_ROOT = Path(__file__).resolve().parents[1]
IMPLEMENTATION_ROOT = SKILL_ROOT.parent
sys.path.insert(0, str(SKILL_ROOT))

from industry_market_viewpoints import (  # noqa: E402
    SkillContractError,
    export_reviewed_artifact,
    generate_draft_artifacts,
    parse_review_markdown,
    validate_artifact,
)
from scripts.generate_viewpoints import main as cli_main  # noqa: E402


MEETING_UID = "mtg_550e8400e29b41d4a716446655440000"
SOURCE = """# 会议纪要

## 张三
张三：短期市场风险偏好可能继续修复。
张三：半导体行业下半年需求有望回升。
"""


def context(*, reviewed: bool = False, source_hash: str = ""):
    value = {
        "meeting_uid": MEETING_UID,
        "meeting_date": "2026-08-13",
        "meeting_series": "华鑫周会",
        "meeting_type": "多人复盘会",
        "data_version": 2 if reviewed else 1,
        "source_review_status": "已审核" if reviewed else "未审核",
        "artifact_review_status": "已审核" if reviewed else "未审核",
        "generated_at": "2026-08-13T09:00:00+08:00",
    }
    if source_hash:
        value["source_md_sha256"] = source_hash
    return value


CLAIMS = [
    {
        "claim_ref": "c001",
        "source_refs": ["L003"],
        "view_scope": "market",
        "subject": "市场风险偏好",
        "presenter": "张三",
        "view_type": "看多",
        "viewpoint_text": "市场风险偏好可能继续修复",
    },
    {
        "claim_ref": "c002",
        "source_refs": ["L004"],
        "view_scope": "industry",
        "subject": "半导体",
        "presenter": "张三",
        "view_type": "看多",
        "viewpoint_text": "半导体行业下半年需求有望回升",
    },
]


class IndustryMarketSkillTests(unittest.TestCase):
    def test_generate_produces_one_review_document_and_draft_json(self):
        review, artifact = generate_draft_artifacts(SOURCE, CLAIMS, context())
        self.assertIn("## 市场观点", review)
        self.assertIn("## 行业观点", review)
        self.assertNotIn(MEETING_UID, review)
        self.assertNotIn("sha256", review)
        self.assertNotIn("schema_version", review)
        rows = [
            "| 日期 | 2026-08-13 |",
            "| 主题 | 市场风险偏好 |",
            "| 发言人 | 张三 |",
            "| 观点类型 | 看多 |",
            "| 观点 | 市场风险偏好可能继续修复 |",
        ]
        positions = [review.index(row) for row in rows]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("讲述人", review)
        self.assertEqual(artifact["metadata"]["quality_status"], "unreviewed")
        self.assertEqual(artifact["metadata"]["item_count"], 2)
        self.assertEqual({item["view_scope"] for item in artifact["items"]}, {"market", "industry"})
        self.assertEqual({item["meeting_date"] for item in artifact["items"]}, {"2026-08-13"})
        self.assertEqual(artifact["items"][0]["view_type"], "看多")
        self.assertNotIn("view_period", artifact["items"][0])
        self.assertNotIn("source_text", artifact["items"][0])
        self.assertNotIn("观点周期", review)
        self.assertNotIn("原文依据", review)

    def test_draft_allows_grounded_compression_and_rejects_duplicates(self):
        compressed = [dict(CLAIMS[0], viewpoint_text="风险偏好有望修复")]
        _review, artifact = generate_draft_artifacts(SOURCE, compressed, context())
        self.assertEqual(artifact["items"][0]["viewpoint_text"], "风险偏好有望修复")
        duplicate = [CLAIMS[0], dict(CLAIMS[0], claim_ref="c003")]
        with self.assertRaisesRegex(SkillContractError, "duplicates"):
            generate_draft_artifacts(SOURCE, duplicate, context())
        duplicate_refs = [dict(CLAIMS[0], source_refs=["L003", "L003"])]
        with self.assertRaisesRegex(SkillContractError, "invalid source_refs"):
            generate_draft_artifacts(SOURCE, duplicate_refs, context())

    def test_draft_rejects_invalid_type_and_directionless_watch_type(self):
        invalid_type = [dict(CLAIMS[0], view_type="偏多")]
        with self.assertRaisesRegex(SkillContractError, "invalid view_type"):
            generate_draft_artifacts(SOURCE, invalid_type, context())
        watch_type = [dict(CLAIMS[0], view_type="关注")]
        with self.assertRaisesRegex(SkillContractError, "invalid view_type"):
            generate_draft_artifacts(SOURCE, watch_type, context())

    def test_reviewed_markdown_is_semantic_authority(self):
        review, draft = generate_draft_artifacts(SOURCE, CLAIMS, context())
        reviewed_markdown = review.replace(
            "市场风险偏好可能继续修复", "审核后：风险偏好温和修复"
        )
        artifact = export_reviewed_artifact(
            reviewed_markdown,
            context(reviewed=True, source_hash=draft["metadata"]["source_md_sha256"]),
        )
        self.assertEqual(artifact["metadata"]["quality_status"], "reviewed")
        self.assertEqual(artifact["items"][0]["viewpoint_text"], "审核后：风险偏好温和修复")
        self.assertNotEqual(artifact["items"][0]["viewpoint_id"], draft["items"][0]["viewpoint_id"])

    def test_reviewed_type_is_semantic_authority(self):
        review, draft = generate_draft_artifacts(SOURCE, CLAIMS, context())
        reviewed_markdown = review.replace("| 观点类型 | 看多 |", "| 观点类型 | 中性 |", 1)
        artifact = export_reviewed_artifact(
            reviewed_markdown,
            context(reviewed=True, source_hash=draft["metadata"]["source_md_sha256"]),
        )
        self.assertEqual(artifact["items"][0]["view_type"], "中性")
        self.assertNotEqual(artifact["items"][0]["viewpoint_id"], draft["items"][0]["viewpoint_id"])

    def test_review_requires_new_fields_order_and_matching_date(self):
        review, draft = generate_draft_artifacts(SOURCE, CLAIMS, context())
        old_review = review.replace("| 日期 | 2026-08-13 |\n", "", 1)
        with self.assertRaisesRegex(SkillContractError, "missing fields"):
            parse_review_markdown(old_review)
        reordered = review.replace(
            "| 主题 | 市场风险偏好 |\n| 发言人 | 张三 |",
            "| 发言人 | 张三 |\n| 主题 | 市场风险偏好 |",
            1,
        )
        with self.assertRaisesRegex(SkillContractError, "out of contract order"):
            parse_review_markdown(reordered)
        wrong_date = review.replace("| 日期 | 2026-08-13 |", "| 日期 | 2026-08-14 |", 1)
        with self.assertRaisesRegex(SkillContractError, "does not match context"):
            export_reviewed_artifact(
                wrong_date,
                context(reviewed=True, source_hash=draft["metadata"]["source_md_sha256"]),
            )

    def test_empty_claims_are_valid(self):
        review, artifact = generate_draft_artifacts(SOURCE, [], context())
        self.assertEqual(parse_review_markdown(review), [])
        self.assertEqual(artifact["items"], [])
        validate_artifact(artifact)

    def test_artifact_validator_rejects_row_count_and_duplicate_id(self):
        _review, artifact = generate_draft_artifacts(SOURCE, CLAIMS, context())
        artifact["metadata"]["item_count"] = 3
        with self.assertRaisesRegex(SkillContractError, "item_count"):
            validate_artifact(artifact)
        _review, artifact = generate_draft_artifacts(SOURCE, CLAIMS, context())
        artifact["items"][1]["viewpoint_id"] = artifact["items"][0]["viewpoint_id"]
        with self.assertRaisesRegex(SkillContractError, "duplicate"):
            validate_artifact(artifact)

    def test_skill_metadata_contract_matches_shared_pipeline_contract(self):
        shared_path = IMPLEMENTATION_ROOT / "meeting-pipeline-contract" / "contract" / "artifact-metadata.schema.json"
        if not shared_path.exists():
            self.skipTest("shared meeting-pipeline-contract checkout is unavailable")
        shared_schema = json.loads(
            shared_path.read_text(encoding="utf-8")
        )
        skill_schema = json.loads(
            (SKILL_ROOT / "contract" / "artifact.schema.json").read_text(encoding="utf-8")
        )
        metadata_schema = skill_schema["$defs"]["metadata"]
        self.assertEqual(metadata_schema["required"], shared_schema["required"])
        for field, shared_property in shared_schema["properties"].items():
            skill_property = metadata_schema["properties"][field]
            if field == "artifact_type":
                self.assertIn(skill_property["const"], shared_property["enum"])
            else:
                self.assertEqual(skill_property, shared_property)

    def test_claim_schema_declares_enum_value_type(self):
        claim_schema = json.loads(
            (SKILL_ROOT / "contract" / "claim_units.schema.json").read_text(
                encoding="utf-8"
            )
        )
        view_scope = claim_schema["items"]["properties"]["view_scope"]
        self.assertEqual(view_scope["type"], "string")
        self.assertEqual(view_scope["enum"], ["market", "industry"])
        self.assertEqual(
            claim_schema["items"]["properties"]["view_type"]["enum"],
            ["看多", "看空", "中性"],
        )
        self.assertNotIn("view_period", claim_schema["items"]["properties"])
        self.assertNotIn(
            "uniqueItems",
            claim_schema["items"]["properties"]["source_refs"],
        )

    def test_provider_schema_matches_claim_unit_contract(self):
        claim_schema = json.loads(
            (SKILL_ROOT / "contract" / "claim_units.schema.json").read_text(
                encoding="utf-8"
            )
        )
        provider_schema = json.loads(
            (
                SKILL_ROOT / "contract" / "claim_units.provider.schema.json"
            ).read_text(encoding="utf-8")
        )
        provider_items = provider_schema["properties"]["claim_units"]["items"]
        self.assertEqual(provider_items["required"], claim_schema["items"]["required"])
        self.assertEqual(
            provider_items["properties"], claim_schema["items"]["properties"]
        )

    def test_quality_rules_are_declared_runtime_content(self):
        manifest = json.loads(
            (SKILL_ROOT / "contract" / "manifest.json").read_text(
                encoding="utf-8"
            )
        )
        quality_rules = SKILL_ROOT / "references" / "quality_rules.md"
        self.assertTrue(quality_rules.is_file())
        self.assertIn(
            "references/quality_rules.md",
            manifest["runtime_paths"],
        )
        self.assertEqual(manifest["contract_version"], 3)

    def test_cli_generate_and_validate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_path = root / "source.md"
            claims_path = root / "claims.json"
            context_path = root / "context.json"
            review_path = root / "review.md"
            artifact_path = root / "artifact.json"
            source_path.write_text(SOURCE, encoding="utf-8")
            claims_path.write_text(json.dumps(CLAIMS, ensure_ascii=False), encoding="utf-8")
            context_path.write_text(json.dumps(context(), ensure_ascii=False), encoding="utf-8")
            self.assertEqual(
                cli_main(
                    [
                        "generate",
                        "--meeting-markdown",
                        str(source_path),
                        "--claim-units",
                        str(claims_path),
                        "--context",
                        str(context_path),
                        "--review-output",
                        str(review_path),
                        "--json-output",
                        str(artifact_path),
                    ]
                ),
                0,
            )
            self.assertEqual(cli_main(["validate", "--artifact-json", str(artifact_path)]), 0)


if __name__ == "__main__":
    unittest.main()
