from __future__ import annotations

from pathlib import Path
import re
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from source_contract_adapter import (  # noqa: E402
    ADAPTER_VERSION,
    SourceContractError,
    adapt_source_contract,
    adapter_sha256,
    pipeline_rules_version,
)


class SourceContractAdapterTests(unittest.TestCase):
    def test_review_contract_maps_speaker_topic_and_target_losslessly(self):
        source = """# 投资会议纪要

**会议日期**：2032-07-14
**会议标题**：研究所周会
**会议类型**：多人复盘会
**会议系列**：研究所周会

## 一、发言整理

### 张三

#### 【科技｜半导体】
##### 【甲辰芯片(123456.SH)】
我继续看好设备订单。
""".encode()
        adapted = adapt_source_contract(source).decode()
        self.assertIn("### 发言人：张三", adapted)
        self.assertIn("【科技｜半导体】", adapted)
        self.assertIn("证券标的：甲辰芯片(123456.SH)", adapted)
        self.assertIn("我继续看好设备订单。", adapted)
        self.assertIn("原会议系列：研究所周会", adapted)

    def test_company_question_preserves_stage_question_and_answer(self):
        source = """# 投资会议纪要
**会议日期**：2032-07-14
**会议标题**：某公司交流会议
**会议类型**：公司交流
**会议系列**：某公司交流
**会议标的**：科技｜软件｜某公司(999999.SZ)
## 一、发言整理
### 投资者问答
**【今年是否会扩产？】**
会根据订单情况分阶段扩产。
""".encode()
        adapted = adapt_source_contract(source).decode()
        self.assertIn("【投资者问答·问题：今年是否会扩产？】", adapted)
        self.assertIn("会根据订单情况分阶段扩产。", adapted)
        self.assertIn("原会议标的：科技｜软件｜某公司(999999.SZ)", adapted)

    def test_pending_table_drops_only_timestamp_locator(self):
        source = """# 投资会议纪要
**会议日期**：2032-07-14
**会议类型**：多人复盘会
## 一、发言整理
### 发言人1
#### 【科技｜软件】
正文。
## 二、存疑与待确认
| 时间戳 | 原始表述 | 当前判断 | 候选项 | 人工确认 |
| --- | --- | --- | --- | --- |
| 12:34 | 某某科技 | 待人工确认 | 候选A | |
""".encode()
        adapted = adapt_source_contract(source).decode()
        self.assertNotIn("12:34", adapted)
        self.assertIn("原始表述：某某科技", adapted)
        self.assertIn("当前判断：待人工确认", adapted)
        self.assertIn("候选项：候选A", adapted)
        self.assertIn("人工确认：", adapted)

    def test_distribution_restriction_fails_closed_without_echo(self):
        with self.assertRaises(SourceContractError) as caught:
            adapt_source_contract("# source\n不要传出去。\n".encode())
        self.assertEqual(caught.exception.code, "restricted_distribution_language")
        self.assertNotIn("不要传出去", caught.exception.safe_message)

    def test_distribution_restriction_with_whitespace_fails_closed(self):
        with self.assertRaises(SourceContractError) as caught:
            adapt_source_contract("# source\n不 要 传 出 去。\n".encode())
        self.assertEqual(caught.exception.code, "restricted_distribution_language")

    def test_canonical_date_must_match_reviewed_record(self):
        source = """# 投资会议纪要
**会议日期**：2032-07-14
**会议类型**：多人复盘会
## 一、发言整理
### 发言人1
正文。
""".encode()
        with self.assertRaises(SourceContractError) as caught:
            adapt_source_contract(source, expected_meeting_date="2032-07-13")
        self.assertEqual(caught.exception.code, "meeting_date_mismatch")

    def test_duplicate_metadata_fails_closed(self):
        source = """# 投资会议纪要
**会议日期**：2032-07-14
**会议日期**：2032-07-14
**会议类型**：多人复盘会
## 一、发言整理
### 发言人1
正文。
""".encode()
        with self.assertRaises(SourceContractError) as caught:
            adapt_source_contract(source)
        self.assertEqual(caught.exception.code, "metadata_duplicate")

    def test_person_like_stage_fails_closed(self):
        source = """# 投资会议纪要
**会议日期**：2032-07-14
**会议类型**：专家交流
## 一、发言整理
### 张三
**【怎么看需求？】**
需求仍需观察。
""".encode()
        with self.assertRaises(SourceContractError) as caught:
            adapt_source_contract(source)
        self.assertEqual(caught.exception.code, "ambiguous_person_stage")

    def test_pipeline_version_contains_real_adapter_hash(self):
        version = pipeline_rules_version("rules-test")
        self.assertIn(ADAPTER_VERSION, version)
        self.assertRegex(adapter_sha256(), r"^[0-9a-f]{64}$")
        self.assertTrue(version.endswith(adapter_sha256()))

    def test_noncanonical_legacy_source_is_unchanged(self):
        source = b"# approved source\n"
        self.assertEqual(adapt_source_contract(source), source)


if __name__ == "__main__":
    unittest.main()
