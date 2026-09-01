from __future__ import annotations

import hashlib
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "skills/meeting-minutes-sanitizer/scripts/sanitize_minutes.py"
FIXTURE_PATH = REPO_ROOT / "tests/fixtures/regression_input.md"

SPEC = importlib.util.spec_from_file_location("meeting_minutes_sanitizer_under_test", SCRIPT_PATH)
assert SPEC and SPEC.loader
SANITIZER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SANITIZER
SPEC.loader.exec_module(SANITIZER)


class SanitizerUnitTests(unittest.TestCase):
    def test_identity_fields_are_collected_and_removed_everywhere(self) -> None:
        raw = "姓名：张三\n会议类型：张三专家交流\n【订单】\n张三认为，订单增长20%。"
        names = SANITIZER.collect_speaker_names(raw)
        self.assertEqual(names, ["张三"])
        self.assertEqual(SANITIZER.parse_meeting_type(raw, names), "专家交流")
        cleaned = SANITIZER.strip_speaker_headings_and_identity(raw, names)
        unit = SANITIZER.split_topic_units(cleaned, names)[0]
        self.assertNotIn("张三", unit.full_topic + unit.text)

    def test_markdown_list_identity_and_metadata_fields_do_not_leak(self) -> None:
        raw = (
            "- 会议日期：2032-07-13\n"
            "- 会议类型：专家交流\n"
            "- 姓名：张三\n"
            "【订单】\n"
            "张三认为，订单增长20%。"
        )
        names = SANITIZER.collect_speaker_names(raw)
        self.assertEqual(names, ["张三"])
        cleaned = SANITIZER.strip_speaker_headings_and_identity(raw, names)
        units = SANITIZER.split_topic_units(cleaned, names)
        combined = "\n".join(unit.full_topic + unit.text for unit in units)
        self.assertNotIn("张三", combined)
        self.assertNotIn("会议日期", combined)
        self.assertNotIn("会议类型", combined)

        for prefix in ("+ ", "1. ", "> ", "> - "):
            with self.subTest(prefix=prefix):
                wrapped = (
                    f"{prefix}会议日期：2032-07-13\n"
                    f"{prefix}会议类型：专家交流\n"
                    f"{prefix}姓名：张三\n"
                    "【订单】\n张三认为，订单增长20%。"
                )
                wrapped_names = SANITIZER.collect_speaker_names(wrapped)
                self.assertEqual(wrapped_names, ["张三"])
                self.assertEqual(SANITIZER.parse_meeting_date(wrapped), "2032-07-13")
                self.assertEqual(SANITIZER.parse_meeting_type(wrapped, wrapped_names), "专家交流")
                wrapped_cleaned = SANITIZER.strip_speaker_headings_and_identity(
                    wrapped,
                    wrapped_names,
                )
                wrapped_units = SANITIZER.split_topic_units(wrapped_cleaned, wrapped_names)
                wrapped_output = "\n".join(unit.text for unit in wrapped_units)
                self.assertNotIn("张三", wrapped_output)
                self.assertNotIn("姓名", wrapped_output)

    def test_markdown_emphasized_identity_field_is_collected_and_blocks_residue(self) -> None:
        raw = "**姓名**：张三\n【订单｜A公司】\n张三与团队沟通后，订单增长。"
        names = SANITIZER.collect_speaker_names(raw)
        self.assertEqual(names, ["张三"])
        cleaned = SANITIZER.strip_speaker_headings_and_identity(raw, names)
        units = SANITIZER.split_topic_units(cleaned, names)
        self.assertNotIn("姓名", "\n".join(unit.text for unit in units))
        with self.assertRaises(SystemExit):
            SANITIZER.quality_check(units, [], names, "调研", "safe")

    def test_identity_and_affiliation_values_are_collected_from_explicit_fields(self) -> None:
        raw = "身份：张三\n发言机构：甲研究院\n【订单】\n张三认为，甲研究院表示，订单增长20%。"
        names = SANITIZER.collect_speaker_names(raw)
        self.assertEqual(names, ["张三", "甲研究院"])
        cleaned = SANITIZER.strip_speaker_headings_and_identity(raw, names)
        unit = SANITIZER.split_topic_units(cleaned, names)[0]
        self.assertNotIn("张三", unit.text)
        self.assertNotIn("甲研究院", unit.text)
        self.assertIn("订单增长20%", unit.text)

    def test_cross_speaker_attribution_is_removed_for_collected_identities(self) -> None:
        raw = (
            "姓名：张三\n"
            "姓名：李四\n"
            "发言人称谓：李总\n"
            "【订单｜A公司】\n"
            "张三提到，李四认为订单增长。李总补充，仍待公告确认。"
        )
        names = SANITIZER.collect_speaker_names(raw)
        cleaned = SANITIZER.strip_speaker_headings_and_identity(raw, names)
        units = SANITIZER.split_topic_units(cleaned, names)
        combined = "\n".join(unit.full_topic + unit.text for unit in units)
        for forbidden in ("张三", "李四", "李总"):
            self.assertNotIn(forbidden, combined)
        self.assertIn("订单增长", combined)
        self.assertIn("待公告确认", combined)
        SANITIZER.quality_check(units, [], names, "调研", "safe")

    def test_unhandled_collected_alias_fails_instead_of_silent_export(self) -> None:
        raw = (
            "### 发言人：张三（别名：三哥；任职机构：甲研究院）\n"
            "【订单｜A公司】\n"
            "三哥牵头甲研究院内部项目，订单可能增长20%，该信息未经确认。"
        )
        names = SANITIZER.collect_speaker_names(raw)
        self.assertEqual(names, ["三哥", "甲研究院", "张三"])
        cleaned = SANITIZER.strip_speaker_headings_and_identity(raw, names)
        units = SANITIZER.split_topic_units(cleaned, names)
        self.assertIn("可能", units[0].text)
        with self.assertRaises(SystemExit) as raised:
            SANITIZER.quality_check(units, [], names, "调研", "safe")
        self.assertIn("collected identity", str(raised.exception).lower())
        self.assertNotIn("三哥", str(raised.exception))

    def test_business_heading_company_and_event_time_are_preserved(self) -> None:
        raw = "### 风险判断\n公司：远景能源\n将在2032-07-14 14:30发布产品。"
        names = SANITIZER.collect_speaker_names(raw)
        cleaned = SANITIZER.strip_speaker_headings_and_identity(raw, names)
        output = SANITIZER.neutralize_text(cleaned, names)
        self.assertIn("风险判断", output)
        self.assertIn("公司：远景能源", output)
        self.assertIn("2032-07-14 14:30", output)

    def test_recording_offset_is_removed_without_removing_business_time(self) -> None:
        text = "[00:10] 发言人A：订单增长。2032-07-14 14:30发布产品。"
        output = SANITIZER.neutralize_text(text, [])
        self.assertNotIn("00:10", output)
        self.assertIn("2032-07-14 14:30", output)
        self.assertIn("14:30", SANITIZER.neutralize_text("14:30 订单：A公司交付。", []))

    def test_neutralization_preserves_group_terms_and_removes_meeting_role_attribution(self) -> None:
        text = "大家电行业需求增长。据公开报道，市场上大家认为订单可能增长。随后主持人指出，风险仍待核验。"
        output = SANITIZER.neutralize_text(text, [])
        self.assertIn("大家电", output)
        self.assertIn("市场上大家认为", output)
        self.assertNotIn("主持人指出", output)
        self.assertIn("风险仍待核验", output)

    def test_neutralization_does_not_invent_sample_entities_or_conclusions(self) -> None:
        samples = [
            "市场买它不买吗？",
            "方案都是你们研发的，那器件不采购你们，还采购谁？",
        ]
        output = "\n".join(SANITIZER.neutralize_text(sample, []) for sample in samples)
        self.assertNotIn("高端产品", output)
        self.assertNotIn("炬光", output)

    def test_external_source_context_is_preserved_before_strict_privacy_gate(self) -> None:
        text = "天风国际的郭明錤在推特上表示，海岳终端订单可能下修30%。据某专家表示，该判断仍待核验。"
        output = SANITIZER.neutralize_text(text, [])
        self.assertIn("郭明錤", output)
        self.assertIn("推特", output)
        self.assertIn("可能", output)
        self.assertIn("据某专家表示", output)
        self.assertIn("仍待核验", output)
        unit = SANITIZER.make_topic_unit("订单｜海岳终端", output)
        with self.assertRaises(SystemExit):
            SANITIZER.quality_check([unit], [], [], "调研", "safe")

    def test_metadata_requires_explicit_meeting_labels_and_valid_override(self) -> None:
        raw = "产品类型：高端PCB\n交付时间：2032-09-01"
        self.assertEqual(SANITIZER.parse_meeting_type(raw, []), "未识别")
        self.assertEqual(SANITIZER.parse_meeting_date(raw), "unknown")
        for invalid in ("not-a-date", "2032-02-30"):
            with self.subTest(invalid=invalid), self.assertRaises(SystemExit):
                SANITIZER.parse_meeting_date(raw, invalid)
        for invalid_source in ("会议日期：2032-02-30", "会议日期：not-a-date"):
            with self.subTest(invalid_source=invalid_source), self.assertRaises(SystemExit):
                SANITIZER.parse_meeting_date(invalid_source)
        self.assertEqual(
            SANITIZER.parse_meeting_date("会议日期：2032-07-13", "2032-07-14"),
            "2032-07-14",
        )

    def test_common_explicit_identity_labels_are_collected(self) -> None:
        for label in ("参会嘉宾", "主讲人", "分享人", "报告人"):
            with self.subTest(label=label):
                self.assertEqual(SANITIZER.collect_speaker_names(f"{label}：张三"), ["张三"])

    def test_topic_markers_are_standalone_and_prefix_content_is_preserved(self) -> None:
        inline = SANITIZER.split_topic_units("正文引用【供应链】显示订单改善。", [])
        self.assertEqual(len(inline), 1)
        self.assertIn("正文引用【供应链】", inline[0].text)

        marked = SANITIZER.split_topic_units("摘要判断仍待核验。\n【订单】\n订单改善。", [])
        self.assertEqual([unit.full_topic for unit in marked], ["概览", "订单"])
        self.assertIn("摘要判断", marked[0].text)

    def test_pending_section_keeps_following_sections(self) -> None:
        raw = "【订单】\n10亿元。\n### 待确认业务事项\n- 金额待确认\n## 风险判断\n需求下修。"
        main, pending = SANITIZER.split_pending_section(raw)
        self.assertEqual(pending, ["金额待确认"])
        self.assertIn("风险判断", main)
        self.assertIn("需求下修", main)

    def test_mixed_source_and_ambiguous_person_headings_fail_closed(self) -> None:
        rejected_sources = [
            "### 用户修正\n以公告为准。",
            "## 二、用户修正\n以公告为准。",
            "### **用户修正**\n以公告为准。",
            "### 人工修正（已确认）\n以公告为准。",
            "### 外部核验结果\n以公告为准。",
            "### 模型推断\n订单增长。",
            "原始表述｜当前判断｜候选项｜人工确认\n星源｜名称存疑｜青云光电、星源｜青云光电",
        ]
        for raw in rejected_sources:
            with self.subTest(raw=raw), self.assertRaises(SystemExit):
                SANITIZER.validate_source_and_heading_modes(raw, [])

        ambiguous_headings = [
            "### 张三\n订单增长。",
            "### 王总\n订单增长。",
            "### 张三\n订单增长。\n【高管变动｜张三公司】\n张三离任。",
        ]
        for raw in ambiguous_headings:
            with self.subTest(raw=raw), self.assertRaises(SystemExit) as raised:
                SANITIZER.validate_source_and_heading_modes(raw, SANITIZER.collect_speaker_names(raw))
            self.assertNotIn("张三", str(raised.exception))
            self.assertNotIn("王总", str(raised.exception))

        identified = "姓名：张三\n### 张三\n张三认为，订单增长。"
        names = SANITIZER.collect_speaker_names(identified)
        SANITIZER.validate_source_and_heading_modes(identified, names)
        self.assertNotIn("### 张三", SANITIZER.strip_speaker_headings_and_identity(identified, names))

    def test_identity_business_object_collision_fails_instead_of_deleting_facts(self) -> None:
        output = SANITIZER.neutralize_text("李宁公司订单增长。", ["李宁"])
        self.assertIn("李宁公司", output)
        unit = SANITIZER.make_topic_unit("订单", output, ["李宁"])
        with self.assertRaises(SystemExit):
            SANITIZER.quality_check([unit], [], ["李宁"], "调研", "safe")

    def test_entity_extraction_avoids_grammar_fragments(self) -> None:
        self.assertEqual(SANITIZER.extract_entities("订单", "", "预计订单增长20%。"), [])
        self.assertEqual(SANITIZER.extract_entities("订单", "", "我们预计A公司订单增长20%。"), [])

    def test_safe_default_id_and_reviewed_output_stem(self) -> None:
        raw = "会议日期：2032-07-13\n【订单】\n订单增长。"
        document_id = SANITIZER.build_document_id(raw)
        self.assertEqual(len(document_id), 12)
        stem = SANITIZER.build_output_stem(raw, "2032-07-13")
        self.assertEqual(stem, f"2032-07-13_脱敏会议纪要_{document_id}")
        for invalid in ("reviewed.md", "reviewed.docx", "reviewed.jsonl", "../reviewed", ".hidden"):
            with self.subTest(invalid=invalid), self.assertRaises(SystemExit):
                SANITIZER.build_output_stem(raw, "2032-07-13", invalid)
        unit = SANITIZER.make_topic_unit("订单", "订单增长。")
        with self.assertRaises(SystemExit):
            SANITIZER.quality_check([unit], [], ["张三"], "调研", "张三")

    def test_quality_gate_rejects_unregistered_person_references(self) -> None:
        samples = [
            ("订单｜A公司", "李四认为订单增长。"),
            ("订单｜A公司", "李总负责该项目。"),
            ("订单｜A公司", "天风国际的郭明錤在推特上表示，订单可能下修。"),
            ("订单｜A公司", "A公司董事长表示，订单已经落地。"),
            ("订单｜A公司", "A公司董事长张三表示，订单已经落地。"),
            ("管理层观点｜李四", "订单增长。"),
            ("李四｜A公司", "订单增长。"),
            ("Alice Chen｜A公司", "订单增长。"),
            ("管理层观点｜三哥", "订单增长。"),
            ("订单｜A公司", "Alice表示订单增长。"),
            ("订单｜A公司", "Alice Chen的观点是订单增长。"),
            ("订单｜A公司", "Alice Chen在会上表示订单增长。"),
            ("订单｜A公司", "三哥在会上表示订单增长。"),
            ("订单｜A公司", "A公司董事长在会上表示订单已经落地。"),
        ]
        for topic, sample in samples:
            with self.subTest(topic=topic, sample=sample):
                unit = SANITIZER.make_topic_unit(topic, sample)
                with self.assertRaises(SystemExit):
                    SANITIZER.quality_check([unit], [], [], "调研", "safe")

    def test_quality_gate_rejects_direct_identifiers_without_echoing_values(self) -> None:
        samples = [
            "联系人手机号为PHONE_REDACTED。",
            "联系电话为010-12345678。",
            "身份证号为11010519491231002X。",
            "联系邮箱为user@example.invalid。",
            "详情见https://private.example.com/note。",
            "联系人：李四。",
            "对接人为Alice Chen。",
            "微信号：wx_zhangsan。",
            "微信ID：abc_123。",
            "WeChat ID：abc_123。",
            "微信号：wxid_zhangsan。",
            "企业微信帐号是abc_123。",
            "可联系138-0013-8000了解订单。",
            "电话：12345678。",
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                unit = SANITIZER.make_topic_unit("订单｜A公司", sample)
                with self.assertRaises(SystemExit) as raised:
                    SANITIZER.quality_check([unit], [], [], "调研", "safe")
                self.assertNotIn("PHONE_REDACTED", str(raised.exception))
                self.assertNotIn("user@example.invalid", str(raised.exception))
                self.assertNotIn("private.example.com", str(raised.exception))

    def test_quality_gate_rejects_source_locators(self) -> None:
        samples = [
            "原文位置：附件A第3页第2段，订单增长20%。",
            "来源文件：restricted-note.docx。",
            "附件A第3页第2段显示订单增长。",
            "录音定位在12:34，订单增长。",
            "记录ID为rec_12345，订单增长。",
            "document ID是doc-123，订单增长。",
            "原文第3页第2段显示订单增长。",
            "原文位置在第3页，订单增长。",
            "附件A，第3页显示订单增长。",
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                unit = SANITIZER.make_topic_unit("订单｜A公司", sample)
                with self.assertRaises(SystemExit):
                    SANITIZER.quality_check([unit], [], [], "调研", "safe")

    def test_explicit_market_code_disambiguates_person_like_business_target(self) -> None:
        unit = SANITIZER.make_topic_unit("订单｜李宁（02331.HK）", "李宁订单增长20%。")
        SANITIZER.quality_check([unit], [], [], "调研", "safe")

    def test_render_markdown_has_required_structure_and_preserves_uncertainty(self) -> None:
        units = [
            SANITIZER.make_topic_unit("订单｜A公司", "订单可能增长20%。"),
            SANITIZER.make_topic_unit("风险", "需求下修风险仍待核验。"),
        ]
        rendered = SANITIZER.render_markdown(
            units,
            ["订单增幅仍待确认。"],
            "2032-07-13",
            "专家交流",
        )
        expected = (
            "# 脱敏会议纪要\n\n"
            "## 一、文档信息\n\n"
            "- 会议日期：2032-07-13\n"
            "- 会议类型：专家交流\n"
            "- 脱敏等级：L2_FACT_PRESERVED\n"
            "- 处理说明：仅删除有限规则明确识别到的发言人身份值，并对发言风格执行规则化处理；"
            "以保留业务事实为目标，未执行外部事实核验，交付前必须人工复核\n\n"
            "## 二、主题纪要\n\n"
            "【订单｜A公司】\n\n"
            "订单可能增长20%。\n\n"
            "【风险】\n\n"
            "需求下修风险仍待核验。\n\n"
            "## 三、存疑与待确认\n\n"
            "- 订单增幅仍待确认。\n"
        )
        self.assertEqual(rendered, expected)
        self.assertNotIn("主题：", rendered)
        self.assertNotIn("待确认业务事项", rendered)

    def test_render_markdown_omits_empty_pending_section(self) -> None:
        unit = SANITIZER.make_topic_unit("订单", "订单增长。")
        rendered = SANITIZER.render_markdown([unit], [], "unknown", "未识别")
        self.assertTrue(rendered.endswith("【订单】\n\n订单增长。\n"))
        self.assertNotIn("存疑与待确认", rendered)
        self.assertNotIn("待确认业务事项", rendered)
        self.assertNotIn("主题：", rendered)

    def test_written_markdown_validation_detects_tampering(self) -> None:
        unit = SANITIZER.make_topic_unit("订单", "订单增长。")
        expected = SANITIZER.render_markdown([unit], [], "2032-07-13", "调研")
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "out.md"
            SANITIZER.write_markdown(expected, path)
            SANITIZER.validate_written_markdown(path, expected, [unit], [])
            path.write_text(expected.replace("订单增长", "订单下降"), encoding="utf-8")
            with self.assertRaises(SystemExit):
                SANITIZER.validate_written_markdown(path, expected, [unit], [])

    def test_written_markdown_validation_rejects_forbidden_internal_labels(self) -> None:
        unit = SANITIZER.make_topic_unit("订单", "订单增长。")
        pending = ["增幅仍待确认。"]
        safe = SANITIZER.render_markdown([unit], pending, "2032-07-13", "调研")
        tampered_values = (
            safe.replace("【订单】", "### 主题：订单"),
            safe.replace("## 三、存疑与待确认", "## 三、待确认业务事项"),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "out.md"
            for tampered in tampered_values:
                with self.subTest(tampered=tampered):
                    path.write_text(tampered, encoding="utf-8")
                    with self.assertRaisesRegex(SystemExit, "forbidden internal label"):
                        SANITIZER.validate_written_markdown(
                            path,
                            tampered,
                            [unit],
                            pending,
                        )

    def test_failed_publish_preserves_existing_markdown_and_cleans_temp(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            final_path = root / "final.md"
            temp_path = root / ".sanitizer-temp.md"
            final_path.write_text("old\n", encoding="utf-8")
            temp_path.write_text("new\n", encoding="utf-8")
            original_replace = Path.replace

            def flaky_replace(path: Path, target: Path) -> Path:
                if path == temp_path:
                    raise OSError("injected publish failure")
                return original_replace(path, target)

            with mock.patch.object(Path, "replace", flaky_replace):
                with self.assertRaises(SystemExit):
                    SANITIZER.publish_markdown(temp_path, final_path, force=True)

            self.assertEqual(final_path.read_text(encoding="utf-8"), "old\n")
            self.assertFalse(temp_path.exists())

    def test_non_force_publish_cannot_overwrite_a_concurrently_created_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            final_path = root / "final.md"
            temp_path = root / ".sanitizer-temp.md"
            temp_path.write_text("validated\n", encoding="utf-8")
            original_link = SANITIZER.os.link

            def concurrent_link(source: Path, target: Path) -> None:
                final_path.write_text("concurrent\n", encoding="utf-8")
                original_link(source, target)

            with mock.patch.object(SANITIZER.os, "link", concurrent_link):
                with self.assertRaises(SystemExit) as raised:
                    SANITIZER.publish_markdown(temp_path, final_path, force=False)

            self.assertIn("Refusing to overwrite", str(raised.exception))
            self.assertEqual(final_path.read_text(encoding="utf-8"), "concurrent\n")
            self.assertFalse(temp_path.exists())

    def test_validation_failure_does_not_replace_existing_output(self) -> None:
        unit = SANITIZER.make_topic_unit("订单", "订单增长。")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_path = root / "input.md"
            input_path.write_text("source\n", encoding="utf-8")
            final_path = root / "safe_sanitized.md"
            final_path.write_text("old\n", encoding="utf-8")
            with mock.patch.object(
                SANITIZER,
                "validate_written_markdown",
                side_effect=SystemExit("injected validation failure"),
            ):
                with self.assertRaises(SystemExit):
                    SANITIZER.write_markdown_output(
                        root,
                        "safe",
                        True,
                        input_path,
                        [unit],
                        [],
                        "2032-07-13",
                        "调研",
                    )
            self.assertEqual(final_path.read_text(encoding="utf-8"), "old\n")
            self.assertFalse(list(root.glob(".sanitizer-*")))

    def test_prepublication_cleanup_failure_is_reported(self) -> None:
        unit = SANITIZER.make_topic_unit("订单", "订单增长。")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_path = root / "input.md"
            input_path.write_text("source\n", encoding="utf-8")
            original_unlink = Path.unlink

            def fail_temp_cleanup(path: Path, *args: object, **kwargs: object) -> None:
                if path.name.startswith(".sanitizer-"):
                    raise OSError("injected cleanup failure")
                original_unlink(path, *args, **kwargs)

            with mock.patch.object(
                SANITIZER,
                "validate_written_markdown",
                side_effect=SystemExit("injected validation failure"),
            ):
                with mock.patch.object(Path, "unlink", fail_temp_cleanup):
                    with self.assertRaises(SystemExit) as raised:
                        SANITIZER.write_markdown_output(
                            root,
                            "safe",
                            False,
                            input_path,
                            [unit],
                            [],
                            "2032-07-13",
                            "调研",
                        )

            self.assertIn("Temporary-file cleanup failed", str(raised.exception))
            self.assertEqual(len(list(root.glob(".sanitizer-*"))), 1)
            self.assertFalse((root / "safe_sanitized.md").exists())

    def test_output_cannot_overwrite_input_even_with_force(self) -> None:
        unit = SANITIZER.make_topic_unit("订单", "订单增长。")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_path = root / "same_sanitized.md"
            input_path.write_text("source\n", encoding="utf-8")
            with self.assertRaises(SystemExit):
                SANITIZER.write_markdown_output(
                    root,
                    "same",
                    True,
                    input_path,
                    [unit],
                    [],
                    "2032-07-13",
                    "调研",
                )
            self.assertEqual(input_path.read_text(encoding="utf-8"), "source\n")
            self.assertFalse(list(root.glob(".sanitizer-*")))


class SanitizerCliTests(unittest.TestCase):
    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.run(
            [sys.executable, str(SCRIPT_PATH), *args],
            cwd=REPO_ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_default_run_creates_one_markdown_with_safe_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = self.run_cli(str(FIXTURE_PATH), "--output-dir", temp_dir)
            self.assertEqual(result.returncode, 0, result.stderr)
            output_dir = Path(temp_dir)
            files = [path for path in output_dir.iterdir() if path.is_file()]
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].suffix, ".md")
            self.assertTrue(files[0].name.endswith("_sanitized.md"))
            self.assertNotIn("regression_input", files[0].name)
            self.assertFalse(list(output_dir.glob("*.docx")))
            self.assertFalse(list(output_dir.glob("*.jsonl")))
            self.assertFalse(list(output_dir.glob(".sanitizer-*")))

            raw = files[0].read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
            self.assertNotIn(b"\r", raw)
            text = raw.decode("utf-8")
            self.assertIn("# 脱敏会议纪要", text)
            self.assertEqual(text.count("\n【"), 2)
            self.assertNotIn("主题：", text)
            self.assertNotIn("待确认业务事项", text)
            self.assertIn("## 三、存疑与待确认", text)
            self.assertIn("- A公司订单增幅是否达到 20% 仍待确认。", text)
            self.assertNotIn("张三", text)
            self.assertNotIn("示例研究机构", text)
            self.assertIn("A公司 Q2 订单可能增长 20%", text)
            self.assertIn("2032-07-14 14:30", text)
            self.assertIn("需求下修风险仍需跟踪", text)

    def test_cli_omits_pending_section_when_source_has_no_pending_items(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_path = root / "input.md"
            output_dir = root / "out"
            input_path.write_text(
                "会议日期：2032-07-13\n会议类型：调研\n【订单｜A公司】\n订单可能增长。\n",
                encoding="utf-8",
            )

            result = self.run_cli(str(input_path), "--output-dir", str(output_dir))

            self.assertEqual(result.returncode, 0, result.stderr)
            text = next(output_dir.glob("*.md")).read_text(encoding="utf-8")
            self.assertIn("【订单｜A公司】", text)
            self.assertNotIn("主题：", text)
            self.assertNotIn("存疑与待确认", text)
            self.assertNotIn("待确认业务事项", text)

    def test_txt_input_still_creates_only_one_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_path = root / "input.txt"
            output_dir = root / "out"
            input_path.write_text(FIXTURE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
            result = self.run_cli(str(input_path), "--output-dir", str(output_dir))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(list(output_dir.glob("*.md"))), 1)
            self.assertFalse(list(output_dir.glob("*.docx")))
            self.assertFalse(list(output_dir.glob("*.jsonl")))

    def test_existing_output_is_not_overwritten_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            first = self.run_cli(str(FIXTURE_PATH), "--output-dir", temp_dir)
            self.assertEqual(first.returncode, 0, first.stderr)
            output_path = next(Path(temp_dir).glob("*.md"))
            digest_before = hashlib.sha256(output_path.read_bytes()).hexdigest()

            second = self.run_cli(str(FIXTURE_PATH), "--output-dir", temp_dir)
            self.assertNotEqual(second.returncode, 0)
            self.assertIn("Refusing to overwrite", second.stderr)
            self.assertEqual(hashlib.sha256(output_path.read_bytes()).hexdigest(), digest_before)

    def test_force_atomically_replaces_only_markdown_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            first = self.run_cli(str(FIXTURE_PATH), "--output-dir", temp_dir)
            self.assertEqual(first.returncode, 0, first.stderr)
            output_path = next(Path(temp_dir).glob("*.md"))
            output_path.write_text("corrupt\n", encoding="utf-8")
            legacy_docx = Path(temp_dir) / "legacy.docx"
            legacy_jsonl = Path(temp_dir) / "legacy.jsonl"
            legacy_docx.write_text("keep", encoding="utf-8")
            legacy_jsonl.write_text("keep", encoding="utf-8")

            second = self.run_cli(str(FIXTURE_PATH), "--output-dir", temp_dir, "--force")
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertNotEqual(output_path.read_text(encoding="utf-8"), "corrupt\n")
            self.assertEqual(legacy_docx.read_text(encoding="utf-8"), "keep")
            self.assertEqual(legacy_jsonl.read_text(encoding="utf-8"), "keep")
            self.assertFalse(list(Path(temp_dir).glob(".sanitizer-*")))

    def test_removed_output_format_argument_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "out"
            result = self.run_cli(
                str(FIXTURE_PATH),
                "--output-dir",
                str(output_dir),
                "--output-format",
                "docx",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unrecognized arguments", result.stderr)
            self.assertFalse(output_dir.exists())

    def test_strict_markdown_gate_fails_on_unregistered_person(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_path = root / "input.md"
            output_dir = root / "out"
            input_path.write_text(
                "会议日期：2032-07-13\n【订单｜A公司】\n李四认为订单增长。",
                encoding="utf-8",
            )
            result = self.run_cli(str(input_path), "--output-dir", str(output_dir))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("person-like reference", result.stderr)
            self.assertFalse(list(output_dir.glob("*.md")) if output_dir.exists() else [])

    def test_strict_markdown_gate_does_not_echo_direct_identifier(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_path = root / "input.md"
            output_dir = root / "out"
            sensitive_value = "PHONE_REDACTED"
            input_path.write_text(
                f"会议日期：2032-07-13\n【订单｜A公司】\n联系人手机号为{sensitive_value}。",
                encoding="utf-8",
            )
            result = self.run_cli(str(input_path), "--output-dir", str(output_dir))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("direct identifier", result.stderr)
            self.assertNotIn(sensitive_value, result.stderr)
            self.assertFalse(list(output_dir.glob("*.md")) if output_dir.exists() else [])

    def test_output_path_cannot_equal_input_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_path = root / "same_sanitized.md"
            source = "会议日期：2032-07-13\n【订单】\n订单增长。\n"
            input_path.write_text(source, encoding="utf-8")
            result = self.run_cli(
                str(input_path),
                "--output-dir",
                str(root),
                "--output-stem",
                "same",
                "--force",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("input source file", result.stderr)
            self.assertEqual(input_path.read_text(encoding="utf-8"), source)

    def test_invalid_date_missing_file_and_bad_stem_are_controlled_errors(self) -> None:
        invalid = self.run_cli(str(FIXTURE_PATH), "--meeting-date", "2032-02-30")
        self.assertNotEqual(invalid.returncode, 0)
        self.assertNotIn("Traceback", invalid.stderr)

        missing = self.run_cli("/private/tmp/does-not-exist-sanitizer-review.md")
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("Input file not found", missing.stderr)
        self.assertNotIn("Traceback", missing.stderr)

        bad_stem = self.run_cli(str(FIXTURE_PATH), "--output-stem", "reviewed.md")
        self.assertNotEqual(bad_stem.returncode, 0)
        self.assertIn("Invalid --output-stem", bad_stem.stderr)
        self.assertNotIn("Traceback", bad_stem.stderr)


if __name__ == "__main__":
    unittest.main()
