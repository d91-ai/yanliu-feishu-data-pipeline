from __future__ import annotations

import importlib.util
from pathlib import Path
import re
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "meeting_pipeline_contract", ROOT / "meeting_pipeline_contract.py"
)
assert SPEC and SPEC.loader
contract = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = contract
SPEC.loader.exec_module(contract)


MEETING_UID = "mtg_550e8400e29b41d4a716446655440000"
SHA = "a" * 64


def valid_metadata(**overrides):
    value = {
        "schema_version": 1,
        "meeting_uid": MEETING_UID,
        "meeting_date": "2032-08-13",
        "meeting_series": "示例研究周会",
        "meeting_type": "多人复盘会",
        "artifact_type": "industry_market_viewpoints",
        "data_version": 1,
        "quality_status": "unreviewed",
        "source_review_status": "未审核",
        "artifact_review_status": "未审核",
        "source_md_sha256": SHA,
        "review_md_sha256": SHA,
        "item_count": 2,
        "generated_at": "2032-08-13T09:00:00+08:00",
    }
    value.update(overrides)
    return value


class MeetingPipelineContractTests(unittest.TestCase):
    def test_contract_assets_are_in_sync(self):
        contract.validate_contract_assets()
        self.assertEqual(len(contract.CONTRACT.business_fields), 21)

    def test_generated_uid_matches_existing_pipeline_format(self):
        values = {contract.new_meeting_uid() for _ in range(32)}
        self.assertEqual(len(values), 32)
        for value in values:
            self.assertRegex(value, re.compile(r"^mtg_[0-9a-f]{32}$"))

    def test_short_names_use_numeric_version_and_exclude_uid_and_meeting_type(self):
        names = [
            contract.build_artifact_filename(
                meeting_date="2032-08-13",
                meeting_series="示例研究周会",
                artifact_type="structured_viewpoints",
                data_version=version,
                extension="json",
            )
            for version in (1, 2, 10)
        ]
        self.assertEqual(
            names,
            [
                "2032-08-13 - 示例研究周会 - 标的观点 - v1.json",
                "2032-08-13 - 示例研究周会 - 标的观点 - v2.json",
                "2032-08-13 - 示例研究周会 - 标的观点 - v10.json",
            ],
        )
        self.assertNotIn("mtg_", names[0])
        self.assertNotIn("多人复盘会", names[0])

    def test_filename_rejects_path_characters_and_wrong_extension(self):
        with self.assertRaises(contract.ContractError):
            contract.build_artifact_filename(
                meeting_date="2032-08-13",
                meeting_series="华鑫/周会",
                artifact_type="meeting_minutes",
                data_version=1,
                extension="md",
            )
        with self.assertRaises(contract.ContractError):
            contract.build_artifact_filename(
                meeting_date="2032-08-13",
                meeting_series="示例研究周会",
                artifact_type="meeting_minutes",
                data_version=1,
                extension="json",
            )

    def test_metadata_accepts_unreviewed_and_reviewed_contracts(self):
        draft = contract.validate_artifact_metadata(valid_metadata())
        self.assertEqual(draft["quality_status"], "unreviewed")
        reviewed = contract.validate_artifact_metadata(
            valid_metadata(
                quality_status="reviewed",
                source_review_status="已审核",
                artifact_review_status="已审核",
                data_version=10,
            )
        )
        self.assertEqual(reviewed["data_version"], 10)

    def test_metadata_rejects_invalid_identity_version_and_status(self):
        invalid_values = [
            valid_metadata(meeting_uid="mtg_bad"),
            valid_metadata(meeting_date="2032-02-30"),
            valid_metadata(data_version=True),
            valid_metadata(data_version=0),
            valid_metadata(source_md_sha256="A" * 64),
            valid_metadata(quality_status="reviewed", artifact_review_status="未审核"),
            valid_metadata(quality_status="unreviewed", artifact_review_status="已审核"),
        ]
        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(contract.ContractError):
                    contract.validate_artifact_metadata(value)

    def test_metadata_rejects_missing_or_unknown_fields(self):
        missing = valid_metadata()
        missing.pop("meeting_series")
        with self.assertRaisesRegex(contract.ContractError, "missing fields"):
            contract.validate_artifact_metadata(missing)
        with self.assertRaisesRegex(contract.ContractError, "unknown fields"):
            contract.validate_artifact_metadata(valid_metadata(extra="value"))


if __name__ == "__main__":
    unittest.main()
