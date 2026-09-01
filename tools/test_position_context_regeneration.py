#!/usr/bin/env python3
from __future__ import annotations

import unittest

from rerun_approved_structured_tables import (
    merge_position_context_mentions,
    merge_position_context_rows,
    validate_rendered_viewpoint_invariants,
    validate_schema_v3_generation,
)


class FakeGenerator:
    @staticmethod
    def normalize_approved_rows(rows, *, meeting_date):
        normalized = []
        for row in rows:
            item = dict(row)
            item.setdefault("meeting_date", meeting_date)
            item.setdefault("viewpoint_date", meeting_date)
            item.setdefault("position_context", "信息不足")
            item.setdefault("reviewable_prediction", True)
            item.setdefault("non_reviewable_reason", "")
            normalized.append(item)
        return normalized


class PositionContextMigrationTests(unittest.TestCase):
    def base_row(self) -> dict:
        return {
            "viewpoint_id": "vp-1",
            "meeting_date": "2032-07-01",
            "viewpoint_date": "2032-07-01",
            "target_name": "示例股份",
            "stock_code": "000001",
            "market": "A股",
            "sector_name": "示例",
            "presenter": "甲",
            "presenter_normalized": "甲",
            "direction": "看多",
            "conviction": "中",
            "time_horizon": "中期",
            "core_viewpoint": "原观点保持不变",
            "evidence": "原观点证据保持不变",
            "reviewable_prediction": True,
            "non_reviewable_reason": "",
        }

    def test_merges_only_position_context_and_keeps_audit_evidence(self) -> None:
        existing = [self.base_row()]
        merged, audit = merge_position_context_rows(
            existing,
            [
                {
                    "viewpoint_id": "vp-1",
                    "position_context": "持有（约两成）；计划增持",
                    "position_evidence": "我现在大概两成仓，准备再加一点",
                }
            ],
            meeting_date="2032-07-01",
            generator=FakeGenerator,
        )

        self.assertEqual(merged[0]["position_context"], "持有（约两成）；计划增持")
        self.assertEqual(merged[0]["core_viewpoint"], existing[0]["core_viewpoint"])
        self.assertEqual(audit[0]["position_evidence"], "我现在大概两成仓，准备再加一点")

    def test_rejects_incomplete_or_duplicate_viewpoint_coverage(self) -> None:
        existing = [self.base_row()]
        with self.assertRaisesRegex(RuntimeError, "coverage mismatch"):
            merge_position_context_rows(
                existing,
                [],
                meeting_date="2032-07-01",
                generator=FakeGenerator,
            )
        with self.assertRaisesRegex(RuntimeError, "duplicate viewpoint_id"):
            merge_position_context_rows(
                existing,
                [
                    {
                        "viewpoint_id": "vp-1",
                        "position_context": "信息不足",
                        "position_evidence": "",
                    },
                    {
                        "viewpoint_id": "vp-1",
                        "position_context": "信息不足",
                        "position_evidence": "",
                    },
                ],
                meeting_date="2032-07-01",
                generator=FakeGenerator,
            )

    def test_rejects_evidence_contract_violations(self) -> None:
        existing = [self.base_row()]
        with self.assertRaisesRegex(RuntimeError, "must have empty"):
            merge_position_context_rows(
                existing,
                [
                    {
                        "viewpoint_id": "vp-1",
                        "position_context": "信息不足",
                        "position_evidence": "不应有证据",
                    }
                ],
                meeting_date="2032-07-01",
                generator=FakeGenerator,
            )
        with self.assertRaisesRegex(RuntimeError, "requires position_evidence"):
            merge_position_context_rows(
                existing,
                [
                    {
                        "viewpoint_id": "vp-1",
                        "position_context": "持有",
                        "position_evidence": "",
                    }
                ],
                meeting_date="2032-07-01",
                generator=FakeGenerator,
            )

    def test_rendered_invariant_check_rejects_main_field_change(self) -> None:
        expected = [{**self.base_row(), "position_context": "信息不足"}]
        actual = [{**expected[0], "direction": "看空"}]
        with self.assertRaisesRegex(RuntimeError, "rendered viewpoint fields changed"):
            validate_rendered_viewpoint_invariants(expected, actual)

    def test_locally_maps_source_only_mentions_by_speaker_and_target(self) -> None:
        existing = [self.base_row()]
        merged, audit = merge_position_context_mentions(
            existing,
            [
                {
                    "presenter": "甲",
                    "target_name": "示例股份",
                    "stock_code": "",
                    "position_context": "未持有；计划买入",
                    "position_evidence": "我还没持有，准备买入",
                }
            ],
            meeting_date="2032-07-01",
            generator=FakeGenerator,
        )

        self.assertEqual(merged[0]["position_context"], "未持有；计划买入")
        self.assertEqual(audit[0]["match_status"], "unique_local_identity_match")

    def test_unmatched_or_conflicting_mentions_fail_closed_to_information_insufficient(self) -> None:
        existing = [self.base_row()]
        unmatched, unmatched_audit = merge_position_context_mentions(
            existing,
            [
                {
                    "presenter": "乙",
                    "target_name": "其他股份",
                    "stock_code": "",
                    "position_context": "持有",
                    "position_evidence": "我持有其他股份",
                }
            ],
            meeting_date="2032-07-01",
            generator=FakeGenerator,
        )
        self.assertEqual(unmatched[0]["position_context"], "信息不足")
        self.assertEqual(unmatched_audit[0]["match_status"], "no_local_identity_match")

        conflicting, conflicting_audit = merge_position_context_mentions(
            existing,
            [
                {
                    "presenter": "甲",
                    "target_name": "示例股份",
                    "stock_code": "",
                    "position_context": "持有",
                    "position_evidence": "我现在持有",
                },
                {
                    "presenter": "甲",
                    "target_name": "示例股份",
                    "stock_code": "",
                    "position_context": "未持有；计划买入",
                    "position_evidence": "我还没持有，准备买入",
                },
            ],
            meeting_date="2032-07-01",
            generator=FakeGenerator,
        )
        self.assertEqual(conflicting[0]["position_context"], "信息不足")
        self.assertEqual(conflicting_audit[0]["match_status"], "conflicting_local_matches")

    def test_accepts_complete_schema_v3_output(self) -> None:
        rows = [
            {"position_context": "持有（约两成）；计划增持"},
            {"position_context": "未持有；计划买入"},
            {"position_context": "信息不足"},
        ]
        markdown = (
            "---\n"
            "schema_version: 3\n"
            "---\n"
            "| 持仓辅助信息 | 持有（约两成）；计划增持 |\n"
            "| 持仓辅助信息 | 未持有；计划买入 |\n"
            "| 持仓辅助信息 | 信息不足 |\n"
        )

        summary = validate_schema_v3_generation(rows, markdown, 3)

        self.assertEqual(summary, {"持有": 1, "未持有": 1, "信息不足": 1})

    def test_rejects_missing_position_context(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "position_context missing"):
            validate_schema_v3_generation(
                [{"position_context": ""}],
                "schema_version: 3\n| 持仓辅助信息 |  |",
                1,
            )

    def test_rejects_non_v3_or_incomplete_markdown(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "not schema version 3"):
            validate_schema_v3_generation(
                [{"position_context": "信息不足"}],
                "schema_version: 2\n| 持仓辅助信息 | 信息不足 |",
                1,
            )
        with self.assertRaisesRegex(RuntimeError, "position field count mismatch"):
            validate_schema_v3_generation(
                [{"position_context": "信息不足"}],
                "schema_version: 3\n",
                1,
            )


if __name__ == "__main__":
    unittest.main()
