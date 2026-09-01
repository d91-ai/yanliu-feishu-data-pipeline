from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from skill_adapter import CliSkillAdapter, SkillContractError  # noqa: E402


REVISION = "919125c568ae5bb5be6369179bf775beab6d5ffe"
VALID_MARKDOWN = """# 脱敏会议纪要

## 一、文档信息

- 会议日期：2032-07-13
- 会议类型：隔离验证
- 脱敏等级：L2_FACT_PRESERVED
- 处理说明：交付前必须人工复核

## 二、主题纪要

【订单｜A公司】

订单可能增长，仍待公告确认。

## 三、存疑与待确认

- 订单增幅仍待确认。
"""

FAKE_SCRIPT = textwrap.dedent(
    f"""
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser()
    parser.add_argument("input_file")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--meeting-date")
    parser.add_argument("--output-stem", required=True)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / f"{{args.output_stem}}_sanitized.md").write_text({VALID_MARKDOWN!r}, encoding="utf-8")
    """
).lstrip()


class SkillAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.script = self.root / "sanitize_minutes.py"
        self.script.write_text(FAKE_SCRIPT, encoding="utf-8")
        self.script_sha = hashlib.sha256(self.script.read_bytes()).hexdigest()

    def tearDown(self):
        self.temp.cleanup()

    def make_adapter(self, **overrides):
        values = {
            "command": ["python3", str(self.script)],
            "expected_contract_version": "minute-sanitization/v2",
            "expected_source_revision": REVISION,
            "expected_script_sha256": self.script_sha,
            "doctor_cache_seconds": 30,
            "approved_skill_pins": {REVISION: self.script_sha},
        }
        values.update(overrides)
        return CliSkillAdapter(**values)

    def test_real_single_markdown_cli_is_ready(self):
        report = self.make_adapter().doctor(force=True)
        self.assertTrue(report.ready)
        self.assertEqual(report.capabilities, ("review-md",))
        self.assertEqual(report.contract_version, "minute-sanitization/v2")
        self.assertIn(REVISION, report.rules_version)
        self.assertIn(self.script_sha, report.rules_version)

    def test_generate_uses_single_markdown_and_returns_pinned_rules(self):
        adapter = self.make_adapter()
        artifact = adapter.generate_review_md(b"meeting source", meeting_date="2032-07-13")
        self.assertEqual(artifact.content.decode("utf-8"), VALID_MARKDOWN)
        self.assertEqual(artifact.quality_status, "passed")
        self.assertIn(REVISION, artifact.rules_version)
        self.assertIn(self.script_sha, artifact.rules_version)

    def test_generate_accepts_omitted_empty_pending_section(self):
        adapter = self.make_adapter()
        no_pending = VALID_MARKDOWN.split("\n## 三、存疑与待确认\n", maxsplit=1)[0] + "\n"

        def fake_run(args, **kwargs):
            output_dir = Path(args[args.index("--output-dir") + 1])
            output_dir.joinpath("review_sanitized.md").write_text(no_pending, encoding="utf-8")
            return subprocess.CompletedProcess(["python3"], 0, "", "")

        adapter._run = fake_run  # type: ignore[method-assign]
        artifact = adapter.generate_review_md(b"meeting source", meeting_date="2032-07-13")
        self.assertEqual(artifact.content.decode("utf-8"), no_pending)

    def test_script_hash_mismatch_fails_doctor_without_running(self):
        adapter = self.make_adapter(
            expected_script_sha256="0" * 64,
            approved_skill_pins={REVISION: "0" * 64},
        )
        report = adapter.doctor(force=True)
        self.assertFalse(report.ready)
        self.assertEqual(report.reason_code, "skill_script_hash_mismatch")

    def test_command_shape_fails_closed(self):
        adapter = self.make_adapter(command=["python3", "-I", str(self.script)])
        report = adapter.doctor(force=True)
        self.assertFalse(report.ready)
        self.assertEqual(report.reason_code, "skill_command_invalid")

    def test_doctor_rejects_extra_artifact(self):
        adapter = self.make_adapter()

        def fake_run(args, **kwargs):
            output_dir = Path(args[args.index("--output-dir") + 1])
            output_dir.joinpath("review_sanitized.md").write_text(VALID_MARKDOWN, encoding="utf-8")
            output_dir.joinpath("result.json").write_text("{}", encoding="utf-8")
            return subprocess.CompletedProcess(["python3"], 0, "private path", "private text")

        adapter._run = fake_run  # type: ignore[method-assign]
        report = adapter.doctor(force=True)
        self.assertFalse(report.ready)
        self.assertEqual(report.reason_code, "skill_extra_artifacts")

    def test_doctor_accepts_noncanonical_bom_and_line_endings(self):
        adapter = self.make_adapter()

        def fake_run(args, **kwargs):
            output_dir = Path(args[args.index("--output-dir") + 1])
            output_dir.joinpath("review_sanitized.md").write_bytes(
                "\ufeff自定义格式\r\n"
                "- 会议日期：2032-07-13\r\n"
                "- 脱敏等级：L2_FACT_PRESERVED\r\n"
                "正文\r\n".encode("utf-8")
            )
            return subprocess.CompletedProcess(["python3"], 0, "", "")

        adapter._run = fake_run  # type: ignore[method-assign]
        report = adapter.doctor(force=True)
        self.assertTrue(report.ready)

    def test_generate_rejects_rules_change_after_cached_doctor(self):
        adapter = self.make_adapter()
        self.assertTrue(adapter.doctor(force=True).ready)
        self.script.write_text(FAKE_SCRIPT + "\n# changed\n", encoding="utf-8")
        with self.assertRaises(SkillContractError) as caught:
            adapter.generate_review_md(b"meeting source", meeting_date="2032-07-13")
        self.assertEqual(caught.exception.code, "skill_script_hash_mismatch")

    def test_nonzero_exit_does_not_expose_child_output(self):
        adapter = self.make_adapter()
        secret = "PHONE_REDACTED user@example.invalid https://private.example"
        adapter._run = lambda *args, **kwargs: subprocess.CompletedProcess(  # type: ignore[method-assign]
            ["python3"], 2, secret, secret
        )
        report = adapter.doctor(force=True)
        self.assertFalse(report.ready)
        self.assertEqual(report.reason_code, "skill_review_failed")
        self.assertNotIn("private", str(report.public_dict()))

    def test_revision_and_hash_require_full_hex_values(self):
        with self.assertRaises(ValueError):
            self.make_adapter(expected_source_revision="short")
        with self.assertRaises(ValueError):
            self.make_adapter(expected_script_sha256="short")

    def test_unapproved_revision_and_hash_pair_is_rejected(self):
        with self.assertRaises(ValueError):
            self.make_adapter(approved_skill_pins={})

    def test_doctor_rejects_probe_identity_residue(self):
        adapter = self.make_adapter()

        def fake_run(args, **kwargs):
            output_dir = Path(args[args.index("--output-dir") + 1])
            leaked = VALID_MARKDOWN.replace("订单可能增长", "测试甲认为，订单可能增长")
            output_dir.joinpath("review_sanitized.md").write_text(leaked, encoding="utf-8")
            return subprocess.CompletedProcess(["python3"], 0, "", "")

        adapter._run = fake_run  # type: ignore[method-assign]
        report = adapter.doctor(force=True)
        self.assertFalse(report.ready)
        self.assertEqual(report.reason_code, "probe_identity_leak")

    def test_generate_passes_meeting_date_and_rejects_output_date_mismatch(self):
        adapter = self.make_adapter()
        with self.assertRaises(SkillContractError) as caught:
            adapter.generate_review_md(b"source without metadata", meeting_date="2032-07-14")
        self.assertEqual(caught.exception.code, "review_date_mismatch")

    def test_doctor_allows_noncanonical_format_but_rejects_invalid_date(self):
        topic_after_pending = VALID_MARKDOWN.replace(
            "【订单｜A公司】\n\n订单可能增长，仍待公告确认。\n\n",
            "订单可能增长，仍待公告确认。\n\n",
        ) + "\n【订单｜A公司】\n\n补充内容。\n"
        for content in (
            VALID_MARKDOWN + "\n## 四、额外区段\n\n内容\n",
            topic_after_pending,
            VALID_MARKDOWN.replace("【订单｜A公司】", "### 主题：订单｜A公司"),
        ):
            with self.subTest(content=content):
                adapter = self.make_adapter()

                def fake_run(args, **kwargs):
                    output_dir = Path(args[args.index("--output-dir") + 1])
                    output_dir.joinpath("review_sanitized.md").write_text(content, encoding="utf-8")
                    return subprocess.CompletedProcess(["python3"], 0, "", "")

                adapter._run = fake_run  # type: ignore[method-assign]
                report = adapter.doctor(force=True)
                self.assertTrue(report.ready)

        adapter = self.make_adapter()

        def fake_invalid_date(args, **kwargs):
            output_dir = Path(args[args.index("--output-dir") + 1])
            output_dir.joinpath("review_sanitized.md").write_text(
                VALID_MARKDOWN.replace("2032-07-13", "2032-99-99"),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(["python3"], 0, "", "")

        adapter._run = fake_invalid_date  # type: ignore[method-assign]
        report = adapter.doctor(force=True)
        self.assertFalse(report.ready)
        self.assertEqual(report.reason_code, "review_date_invalid")

    def test_scripts_directory_symlink_is_rejected(self):
        real_root = self.root / "real"
        real_script = real_root / "scripts" / "sanitize_minutes.py"
        real_script.parent.mkdir(parents=True)
        real_script.write_text(FAKE_SCRIPT, encoding="utf-8")
        linked_root = self.root / "linked"
        linked_root.mkdir()
        linked_root.joinpath("scripts").symlink_to(real_script.parent, target_is_directory=True)
        digest = hashlib.sha256(real_script.read_bytes()).hexdigest()
        adapter = self.make_adapter(
            command=["python3", str(linked_root / "scripts" / "sanitize_minutes.py")],
            expected_script_sha256=digest,
            approved_skill_pins={REVISION: digest},
        )
        report = adapter.doctor(force=True)
        self.assertFalse(report.ready)
        self.assertEqual(report.reason_code, "skill_script_invalid")


if __name__ == "__main__":
    unittest.main()
