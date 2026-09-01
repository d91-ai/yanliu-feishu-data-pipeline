from __future__ import annotations

import hashlib
import io
from email.message import Message
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import minute_sanitize_service as svc  # noqa: E402
from feishu_gateway import GatewayError, RemoteFile, deterministic_client_token  # noqa: E402
from minute_sanitize_service import (  # noqa: E402
    FIELD_APPROVED_SHA,
    FIELD_APPROVED_VERSION,
    FIELD_ARCHIVE_LINK,
    FIELD_ARCHIVE_STATUS,
    FIELD_ARCHIVE_TIME,
    FIELD_BASELINE_LINK,
    FIELD_BASELINE_SHA,
    FIELD_BASELINE_VERSION,
    FIELD_IDEMPOTENCY,
    FIELD_MD_LINK,
    FIELD_MD_STATUS,
    FIELD_MEETING_DATE,
    FIELD_QUALITY,
    FIELD_REVIEW,
    FIELD_RULES_VERSION,
    FIELD_SOURCE_ID,
    FIELD_VERSION_STATUS,
    FIELD_VERSION_DIFF,
    MinuteSanitizeOrchestrator,
    ServiceConfig,
    SOURCE_STATUS,
    STATUS_GENERATED,
    WorkflowError,
    make_handler,
    safe_error,
)
from skill_adapter import DoctorReport, ReviewArtifact  # noqa: E402


def sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


class FakeSkill:
    def __init__(self, ready: bool = True):
        self.ready = ready
        self.doctor_calls = 0
        self.review_calls = 0
        self.last_meeting_date = ""

    def doctor(self, *, force: bool = False):
        self.doctor_calls += 1
        return DoctorReport(
            self.ready,
            "minute-sanitization/v2" if self.ready else "",
            ("review-md",) if self.ready else (),
            "" if self.ready else "contract_version_mismatch",
            "rules-test" if self.ready else "",
        )

    def generate_review_md(self, source_markdown: bytes, *, meeting_date: str):
        self.review_calls += 1
        self.last_meeting_date = meeting_date
        content = f"""# 脱敏会议纪要

## 一、文档信息

- 会议日期：{meeting_date}
- 会议类型：测试
- 脱敏等级：L2_FACT_PRESERVED
- 处理说明：交付前必须人工复核

## 二、主题纪要

【主题内容】

主题内容。
""".encode("utf-8")
        return ReviewArtifact(content, "rules-test", "passed")

class FakeGateway:
    def __init__(self):
        self.source_records = {}
        self.target_records = {}
        self.files = {}
        self.uploads = []
        self.version_ensures = []
        self.create_tokens = []
        self.calls = 0
        self.clock = 1783900000000
        self._counter = 0

    def now_ms(self):
        self.clock += 1
        return self.clock

    def get_source_record(self, record_id):
        self.calls += 1
        return self.source_records[record_id]

    def update_source_record(self, record_id, fields):
        self.calls += 1
        self.source_records[record_id]["fields"].update(fields)

    def get_target_record(self, record_id):
        self.calls += 1
        return self.target_records[record_id]

    def update_target_record(self, record_id, fields):
        self.calls += 1
        self.target_records[record_id]["fields"].update(fields)

    def find_target_by_source_id(self, source_record_id):
        self.calls += 1
        matches = [r for r in self.target_records.values() if r["fields"].get(FIELD_SOURCE_ID) == source_record_id]
        if len(matches) > 1:
            raise GatewayError("duplicate", "Duplicate target records.")
        return matches[0] if matches else None

    def create_target_record(self, fields, *, client_token):
        self.calls += 1
        self.create_tokens.append(client_token)
        record_id = f"target{len(self.target_records) + 1}"
        record = {"record_id": record_id, "fields": dict(fields)}
        self.target_records[record_id] = record
        return record

    def fetch_file(self, url, *, require_version=False):
        self.calls += 1
        remote = self.files[url]
        if require_version and not remote.version:
            return RemoteFile(remote.token, remote.url, remote.name, remote.content, "v1")
        return remote

    def ensure_auditable_version(self, remote, *, content_type):
        self.calls += 1
        self.version_ensures.append((remote.token, content_type))
        if remote.version:
            return remote
        return RemoteFile(remote.token, remote.url, remote.name, remote.content, "v1")

    def ensure_month_folder(self, root_token, month):
        self.calls += 1
        return f"{root_token}/{month}"

    def ensure_baseline_folder(self, version_root_token, month):
        self.calls += 1
        return f"{version_root_token}/{month}/审核前"

    def upload_or_reuse(self, folder_token, file_name, content, *, content_type):
        self.calls += 1
        for folder, name, remote in self.uploads:
            if folder == folder_token and name == file_name:
                if remote.content != content:
                    raise GatewayError("name_conflict", "Existing file has different content.")
                return remote
        self._counter += 1
        token = f"file{self._counter}"
        url = f"https://example.test/file/{token}"
        remote = RemoteFile(token, url, file_name, content, "v1")
        self.files[url] = remote
        self.uploads.append((folder_token, file_name, remote))
        return remote

    def add_file(self, token, name, content, version="v1"):
        url = f"https://example.test/file/{token}"
        self.files[url] = RemoteFile(token, url, name, content, version)
        return url

    def edit_file(self, url, content, version="v2"):
        current = self.files[url]
        self.files[url] = RemoteFile(current.token, url, current.name, content, version)


class ServiceTests(unittest.TestCase):
    def test_manifest_store_is_private_durable_and_atomic(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "state" / "manifests"
            store = svc.ManifestStore(root)
            store.write(
                "review:rec-1",
                stage="review_generated",
                record_id="rec-1",
                file_token="file-1",
                updated_at=1,
            )
            files = list(root.glob("*.json"))
            self.assertEqual(len(files), 1)
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(files[0].stat().st_mode), 0o600)
            self.assertFalse(list(root.glob("*.tmp")))

    def test_serve_requires_apply_before_runtime_config_is_read(self):
        with mock.patch.object(svc, "read_runtime_config") as read_runtime:
            with self.assertRaisesRegex(SystemExit, "requires explicit --apply"):
                svc.main(["serve"])
        read_runtime.assert_not_called()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.gateway = FakeGateway()
        self.skill = FakeSkill()
        self.cfg = ServiceConfig(
            "pending-root",
            "archive-root",
            "version-root",
            state_dir=Path(self.temp.name),
        )
        self.service = MinuteSanitizeOrchestrator(self.cfg, self.gateway, self.skill)

    def tearDown(self):
        self.temp.cleanup()

    def add_source(self, expected_sha=None):
        content = b"# approved source\n"
        url = self.gateway.add_file("source", "source.md", content)
        self.gateway.source_records["source1"] = {
            "record_id": "source1",
            "fields": {
                FIELD_REVIEW: True,
                FIELD_ARCHIVE_STATUS: "已归档",
                FIELD_VERSION_STATUS: "已完成",
                FIELD_ARCHIVE_LINK: {"link": url},
                FIELD_APPROVED_SHA: expected_sha or sha(content),
                FIELD_MEETING_DATE: "2032-07-13",
                "脱敏生成状态": "待生成",
                FIELD_ARCHIVE_TIME: 1783900000000,
            },
        }

    def test_full_two_stage_flow_and_idempotency(self):
        self.add_source()
        generated = self.service.generate_review_md("source1")
        self.assertEqual(self.skill.last_meeting_date, "2032-07-13")
        target_id = generated["target_record_id"]
        target = self.gateway.target_records[target_id]["fields"]
        self.assertEqual(target[FIELD_MD_STATUS], STATUS_GENERATED)
        self.assertTrue(target[FIELD_IDEMPOTENCY])
        self.assertIn("meeting-minutes-source-adapter/v1", target[FIELD_RULES_VERSION])
        self.assertEqual(target[FIELD_VERSION_STATUS], "基线已留存")
        self.assertEqual(len(self.gateway.version_ensures), 1)
        self.assertEqual(target[FIELD_BASELINE_VERSION], "v1")
        self.assertEqual(
            self.gateway.create_tokens,
            [deterministic_client_token(f"sanitize:{target[FIELD_IDEMPOTENCY]}")],
        )

        pending_url = target[FIELD_MD_LINK]["link"]
        review_text = self.gateway.files[pending_url].content.decode("utf-8")
        self.assertIn("【主题内容】", review_text)
        self.assertNotIn("主题：", review_text)
        self.assertNotIn("待确认业务事项", review_text)
        self.assertNotIn("存疑与待确认", review_text)
        edited = b"# sanitized approved\n\nhuman edit\n"
        self.gateway.edit_file(pending_url, edited)
        target[FIELD_REVIEW] = True
        archived = self.service.archive_review_md(target_id)
        self.assertEqual(archived["version_diff"], "有修改")
        self.assertEqual(target[FIELD_APPROVED_VERSION], "v2")
        self.assertEqual(target[FIELD_APPROVED_SHA], sha(edited))
        self.assertEqual(target[FIELD_ARCHIVE_STATUS], "已归档")
        self.assertEqual(target[FIELD_VERSION_STATUS], "已完成")
        self.assertFalse(any("JSON" in key for key in target))

        uploads = len(self.gateway.uploads)
        self.assertEqual(self.service.generate_review_md("source1")["status"], "skipped_existing")
        self.assertEqual(self.service.archive_review_md(target_id)["status"], "skipped_existing")
        self.assertEqual(len(self.gateway.uploads), uploads)

        manifests = "".join(path.read_text() for path in Path(self.temp.name).glob("*.json"))
        self.assertNotIn("human edit", manifests)
        self.assertNotIn("sanitized approved", manifests)

    def test_legacy_pending_items_migrate_to_canonical_heading(self):
        raw = """# 脱敏会议纪要

## 一、文档信息

- 会议日期：2032-07-13
- 会议类型：测试
- 脱敏等级：L2_FACT_PRESERVED
- 处理说明：交付前必须人工复核

## 二、主题纪要

### 主题：订单｜A公司

订单可能增长，仍待公告确认。

## 三、待确认业务事项

- 订单增幅仍待确认。
""".encode("utf-8")

        self.skill.generate_review_md = lambda source_markdown, meeting_date: ReviewArtifact(  # type: ignore[method-assign]
            raw,
            "rules-test",
            "passed",
        )
        self.add_source()
        generated = self.service.generate_review_md("source1")
        fields = self.gateway.target_records[generated["target_record_id"]]["fields"]
        normalized = self.gateway.files[fields[FIELD_MD_LINK]["link"]].content.decode("utf-8")

        self.assertIn("【订单｜A公司】", normalized)
        self.assertNotIn("主题：", normalized)
        self.assertNotIn("待确认业务事项", normalized)
        self.assertIn("## 三、存疑与待确认", normalized)
        self.assertIn("订单增幅仍待确认。", normalized)

    def test_canonical_pending_items_pass_without_rewriting_content(self):
        raw = """# 脱敏会议纪要

## 一、文档信息

- 会议日期：2032-07-13
- 会议类型：测试
- 脱敏等级：L2_FACT_PRESERVED
- 处理说明：交付前必须人工复核

## 二、主题纪要

【订单｜A公司】

订单可能增长，仍待公告确认。

## 三、存疑与待确认

- 订单增幅仍待确认。
""".encode("utf-8")

        self.skill.generate_review_md = lambda source_markdown, meeting_date: ReviewArtifact(  # type: ignore[method-assign]
            raw,
            "rules-test",
            "passed",
        )
        self.add_source()
        generated = self.service.generate_review_md("source1")
        fields = self.gateway.target_records[generated["target_record_id"]]["fields"]
        normalized = self.gateway.files[fields[FIELD_MD_LINK]["link"]].content

        self.assertEqual(normalized, raw)

    def test_noncanonical_generated_markdown_still_updates_base(self):
        raw = """# 自定义脱敏纪要格式

- 会议日期：2032-07-13
- 脱敏等级：L2_FACT_PRESERVED

这里是已经正常生成的脱敏内容，不使用固定章节或主题标记。
""".encode("utf-8")

        self.skill.generate_review_md = lambda source_markdown, meeting_date: ReviewArtifact(  # type: ignore[method-assign]
            raw,
            "rules-test",
            "passed",
        )
        self.add_source()
        generated = self.service.generate_review_md("source1")
        fields = self.gateway.target_records[generated["target_record_id"]]["fields"]

        self.assertEqual(fields[FIELD_MD_STATUS], STATUS_GENERATED)
        self.assertEqual(fields[FIELD_QUALITY], "已通过")
        self.assertEqual(
            self.gateway.files[fields[FIELD_MD_LINK]["link"]].content,
            raw,
        )

    def test_repeated_create_reuses_unique_consistent_target(self):
        self.add_source()
        search_calls = 0
        create_calls = 0
        pending = None

        def find_target(source_record_id):
            nonlocal search_calls, pending
            self.gateway.calls += 1
            search_calls += 1
            if search_calls <= 2 or pending is None:
                return None
            self.gateway.target_records[pending["record_id"]] = pending
            return pending

        def repeated_create(fields, *, client_token):
            nonlocal create_calls, pending
            self.gateway.calls += 1
            create_calls += 1
            self.gateway.create_tokens.append(client_token)
            pending = {"record_id": "target-reconciled", "fields": dict(fields)}
            raise GatewayError(
                "feishu_http_error",
                "Feishu API returned HTTP 403.",
                http_status=403,
                remote_code="1254608",
            )

        with (
            mock.patch.object(self.gateway, "find_target_by_source_id", side_effect=find_target),
            mock.patch.object(self.gateway, "create_target_record", side_effect=repeated_create),
            mock.patch("minute_sanitize_service.time.sleep") as sleep,
        ):
            generated = self.service.generate_review_md("source1")

        target = self.gateway.target_records[generated["target_record_id"]]["fields"]
        self.assertEqual(generated["status"], "generated")
        self.assertEqual(create_calls, 1)
        self.assertEqual(search_calls, 3)
        self.assertEqual(
            self.gateway.create_tokens,
            [deterministic_client_token(f"sanitize:{target[FIELD_IDEMPOTENCY]}")],
        )
        sleep.assert_called_once_with(0.2)

    def test_repeated_create_without_visible_target_fails_closed(self):
        self.add_source()
        create_calls = 0

        def repeated_create(fields, *, client_token):
            nonlocal create_calls
            self.gateway.calls += 1
            create_calls += 1
            self.gateway.create_tokens.append(client_token)
            raise GatewayError(
                "feishu_http_error",
                "Feishu API returned HTTP 403.",
                http_status=403,
                remote_code="1254608",
            )

        with (
            mock.patch.object(self.gateway, "find_target_by_source_id", return_value=None) as search,
            mock.patch.object(self.gateway, "create_target_record", side_effect=repeated_create),
            mock.patch("minute_sanitize_service.time.sleep") as sleep,
        ):
            with self.assertRaises(WorkflowError) as caught:
                self.service.generate_review_md("source1")

        self.assertEqual(caught.exception.code, "target_create_ambiguous")
        self.assertEqual(caught.exception.http_status, 503)
        self.assertEqual(create_calls, 1)
        self.assertEqual(len(self.gateway.create_tokens), 1)
        self.assertEqual(search.call_count, 5)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.2, 0.5, 1.0])
        self.assertEqual(self.gateway.target_records, {})
        self.assertEqual(self.gateway.uploads, [])
        self.assertEqual(
            self.gateway.source_records["source1"]["fields"]["脱敏生成状态"],
            "生成失败",
        )

    def test_repeated_create_rejects_inconsistent_target(self):
        self.add_source()
        search_calls = 0
        inconsistent = {
            "record_id": "target-conflict",
            "fields": {
                FIELD_SOURCE_ID: "source1",
                FIELD_IDEMPOTENCY: "different-idempotency-key",
            },
        }

        def find_target(source_record_id):
            nonlocal search_calls
            search_calls += 1
            return None if search_calls == 1 else inconsistent

        def repeated_create(fields, *, client_token):
            self.gateway.create_tokens.append(client_token)
            raise GatewayError(
                "feishu_http_error",
                "Feishu API returned HTTP 403.",
                http_status=403,
                remote_code="1254608",
            )

        with (
            mock.patch.object(self.gateway, "find_target_by_source_id", side_effect=find_target),
            mock.patch.object(self.gateway, "create_target_record", side_effect=repeated_create),
            mock.patch("minute_sanitize_service.time.sleep") as sleep,
        ):
            with self.assertRaises(WorkflowError) as caught:
                self.service.generate_review_md("source1")

        self.assertEqual(caught.exception.code, "target_create_conflict")
        self.assertEqual(search_calls, 2)
        self.assertEqual(len(self.gateway.create_tokens), 1)
        self.assertEqual(self.gateway.uploads, [])
        sleep.assert_not_called()

    def test_archive_without_human_edit_keeps_same_version_and_hash(self):
        self.add_source()
        generated = self.service.generate_review_md("source1")
        target = self.gateway.target_records[generated["target_record_id"]]["fields"]
        target[FIELD_REVIEW] = True
        archived = self.service.archive_review_md(generated["target_record_id"])
        self.assertEqual(archived["version_diff"], "无修改")
        self.assertEqual(target[FIELD_VERSION_DIFF], "无修改")
        self.assertEqual(target[FIELD_BASELINE_VERSION], target[FIELD_APPROVED_VERSION])
        self.assertEqual(target[FIELD_BASELINE_SHA], target[FIELD_APPROVED_SHA])

    def test_not_ready_skill_fails_closed_before_gateway_access(self):
        service = MinuteSanitizeOrchestrator(self.cfg, self.gateway, FakeSkill(ready=False))
        with self.assertRaisesRegex(WorkflowError, "doctor"):
            service.generate_review_md("record1")
        self.assertEqual(self.gateway.calls, 0)

    def test_archive_does_not_depend_on_skill_doctor(self):
        self.add_source()
        target_id = self.service.generate_review_md("source1")["target_record_id"]
        fields = self.gateway.target_records[target_id]["fields"]
        fields[FIELD_REVIEW] = True
        unavailable = FakeSkill(ready=False)
        service = MinuteSanitizeOrchestrator(self.cfg, self.gateway, unavailable)
        result = service.archive_review_md(target_id)
        self.assertEqual(result["status"], "archived")
        self.assertEqual(unavailable.doctor_calls, 0)

    def test_source_hash_mismatch_does_not_write_status(self):
        self.add_source(expected_sha="0" * 64)
        before = dict(self.gateway.source_records["source1"]["fields"])
        with self.assertRaisesRegex(WorkflowError, "hash"):
            self.service.generate_review_md("source1")
        self.assertEqual(self.gateway.source_records["source1"]["fields"], before)
        self.assertEqual(self.skill.review_calls, 0)
        self.assertEqual(self.gateway.uploads, [])

    def test_source_before_cutoff_is_rejected_without_artifact(self):
        self.add_source()
        strict = ServiceConfig(
            "pending-root",
            "archive-root",
            "version-root",
            source_cutoff_ms=1783900000001,
            state_dir=Path(self.temp.name),
        )
        service = MinuteSanitizeOrchestrator(strict, self.gateway, self.skill)
        with self.assertRaisesRegex(WorkflowError, "predates"):
            service.generate_review_md("source1")
        self.assertEqual(self.skill.review_calls, 0)
        self.assertEqual(self.gateway.uploads, [])

    def test_terminal_retry_downloads_archive_and_checks_approved_hash(self):
        self.add_source()
        target_id = self.service.generate_review_md("source1")["target_record_id"]
        fields = self.gateway.target_records[target_id]["fields"]
        fields[FIELD_REVIEW] = True
        self.service.archive_review_md(target_id)
        archive_url = fields[FIELD_ARCHIVE_LINK]["link"]
        self.assertEqual(self.service.archive_review_md(target_id)["status"], "skipped_existing")
        self.gateway.edit_file(archive_url, b"tampered\n", version="v2")
        with self.assertRaisesRegex(WorkflowError, "SHA256"):
            self.service.archive_review_md(target_id)

    def test_terminal_retry_rejects_non_markdown_archive(self):
        self.add_source()
        target_id = self.service.generate_review_md("source1")["target_record_id"]
        fields = self.gateway.target_records[target_id]["fields"]
        fields[FIELD_REVIEW] = True
        self.service.archive_review_md(target_id)
        archive_url = fields[FIELD_ARCHIVE_LINK]["link"]
        current = self.gateway.files[archive_url]
        self.gateway.files[archive_url] = RemoteFile(
            current.token,
            current.url,
            "archive.txt",
            current.content,
            current.version,
        )
        with self.assertRaisesRegex(WorkflowError, "Markdown"):
            self.service.archive_review_md(target_id)

    def test_terminal_retry_rejects_non_utf8_archive(self):
        self.add_source()
        target_id = self.service.generate_review_md("source1")["target_record_id"]
        fields = self.gateway.target_records[target_id]["fields"]
        fields[FIELD_REVIEW] = True
        self.service.archive_review_md(target_id)
        archive_url = fields[FIELD_ARCHIVE_LINK]["link"]
        invalid = b"\xff\xfe"
        fields[FIELD_APPROVED_SHA] = sha(invalid)
        self.gateway.edit_file(archive_url, invalid, version="v2")
        with self.assertRaisesRegex(WorkflowError, "UTF-8"):
            self.service.archive_review_md(target_id)

    def test_archive_link_conflict_is_rejected(self):
        self.gateway.target_records["target1"] = {
            "record_id": "target1",
            "fields": {
                FIELD_MD_STATUS: STATUS_GENERATED,
                FIELD_REVIEW: True,
                FIELD_ARCHIVE_STATUS: "待归档",
                FIELD_ARCHIVE_LINK: {"link": "https://example.test/file/existing"},
                FIELD_MD_LINK: {"link": "https://example.test/file/pending"},
                FIELD_VERSION_STATUS: "基线已留存",
                FIELD_BASELINE_LINK: {"link": "https://example.test/file/baseline"},
                FIELD_BASELINE_SHA: "1" * 64,
            },
        }
        with self.assertRaisesRegex(WorkflowError, "already exists"):
            self.service.archive_review_md("target1")

    def test_archive_upload_failure_preserves_baseline_state(self):
        self.add_source()
        target_id = self.service.generate_review_md("source1")["target_record_id"]
        fields = self.gateway.target_records[target_id]["fields"]
        fields[FIELD_REVIEW] = True
        original = self.gateway.upload_or_reuse

        def fail_archive(folder_token, file_name, content, *, content_type):
            if folder_token.startswith("archive-root/"):
                raise GatewayError("archive_upload_failed", "Archive upload failed.")
            return original(folder_token, file_name, content, content_type=content_type)

        self.gateway.upload_or_reuse = fail_archive
        with self.assertRaises(GatewayError):
            self.service.archive_review_md(target_id)
        self.assertEqual(fields[FIELD_ARCHIVE_STATUS], "归档失败")
        self.assertEqual(fields[FIELD_VERSION_STATUS], "基线已留存")
        self.assertEqual(fields["版本差异"], "未比较")

        self.gateway.upload_or_reuse = original
        retried = self.service.archive_review_md(target_id)
        self.assertEqual(retried["status"], "archived")

    def test_generation_failure_state_can_resume_with_same_target(self):
        self.add_source()
        original = self.skill.generate_review_md
        self.skill.generate_review_md = mock.Mock(side_effect=RuntimeError("transient"))
        with self.assertRaises(RuntimeError):
            self.service.generate_review_md("source1")
        self.assertEqual(self.gateway.source_records["source1"]["fields"]["脱敏生成状态"], "生成失败")
        target_count = len(self.gateway.target_records)

        self.skill.generate_review_md = original
        result = self.service.generate_review_md("source1")
        self.assertEqual(result["status"], "generated")
        self.assertEqual(len(self.gateway.target_records), target_count)

    def test_generation_commit_response_loss_reconciles_confirmed_terminal(self):
        self.add_source()
        original = self.gateway.update_source_record

        def lose_response(record_id, fields):
            original(record_id, fields)
            if fields.get(SOURCE_STATUS) == STATUS_GENERATED:
                raise GatewayError("feishu_unreachable", "Response was lost.")

        with mock.patch.object(self.gateway, "update_source_record", side_effect=lose_response):
            result = self.service.generate_review_md("source1")

        self.assertEqual(result["status"], "generated")
        self.assertTrue(result["reconciled"])
        self.assertEqual(self.gateway.source_records["source1"]["fields"][SOURCE_STATUS], STATUS_GENERATED)
        target = self.gateway.target_records[result["target_record_id"]]["fields"]
        self.assertEqual(target[FIELD_MD_STATUS], STATUS_GENERATED)

    def test_generation_partial_commit_is_uncertain_without_downgrade(self):
        self.add_source()
        original = self.gateway.update_target_record

        def lose_response(record_id, fields):
            original(record_id, fields)
            if fields.get(FIELD_MD_STATUS) == STATUS_GENERATED:
                raise GatewayError("feishu_unreachable", "Response was lost.")

        with mock.patch.object(self.gateway, "update_target_record", side_effect=lose_response):
            with self.assertRaises(WorkflowError) as caught:
                self.service.generate_review_md("source1")

        self.assertEqual(caught.exception.code, "review_commit_outcome_uncertain")
        self.assertEqual(caught.exception.http_status, 503)
        self.assertEqual(caught.exception.response_status, "outcome_uncertain")
        self.assertEqual(self.gateway.source_records["source1"]["fields"][SOURCE_STATUS], "生成中")
        target = next(iter(self.gateway.target_records.values()))["fields"]
        self.assertEqual(target[FIELD_MD_STATUS], STATUS_GENERATED)

    def test_generation_terminal_with_quality_drift_is_not_confirmed(self):
        self.add_source()
        original = self.gateway.update_source_record

        def lose_response(record_id, fields):
            original(record_id, fields)
            if fields.get(SOURCE_STATUS) == STATUS_GENERATED:
                target = next(iter(self.gateway.target_records.values()))["fields"]
                target[FIELD_QUALITY] = "未通过"
                raise GatewayError("feishu_unreachable", "Response was lost.")

        with mock.patch.object(self.gateway, "update_source_record", side_effect=lose_response):
            with self.assertRaises(WorkflowError) as caught:
                self.service.generate_review_md("source1")

        self.assertEqual(caught.exception.code, "review_commit_outcome_uncertain")
        target = next(iter(self.gateway.target_records.values()))["fields"]
        self.assertEqual(target[FIELD_MD_STATUS], STATUS_GENERATED)
        self.assertEqual(target[FIELD_QUALITY], "未通过")

    def test_generation_reconciliation_read_failure_is_uncertain_without_downgrade(self):
        self.add_source()
        original_update = self.gateway.update_source_record
        original_get = self.gateway.get_source_record

        def lose_response(record_id, fields):
            original_update(record_id, fields)
            if fields.get(SOURCE_STATUS) == STATUS_GENERATED:
                raise GatewayError("feishu_unreachable", "Response was lost.")

        def fail_terminal_read(record_id):
            if self.gateway.source_records[record_id]["fields"].get(SOURCE_STATUS) == STATUS_GENERATED:
                raise GatewayError("feishu_unreachable", "Reconciliation read failed.")
            return original_get(record_id)

        with mock.patch.object(self.gateway, "update_source_record", side_effect=lose_response), mock.patch.object(
            self.gateway, "get_source_record", side_effect=fail_terminal_read
        ):
            with self.assertRaises(WorkflowError) as caught:
                self.service.generate_review_md("source1")

        self.assertEqual(caught.exception.code, "review_commit_outcome_uncertain")
        target = next(iter(self.gateway.target_records.values()))["fields"]
        self.assertEqual(target[FIELD_MD_STATUS], STATUS_GENERATED)
        self.assertNotEqual(target.get(FIELD_MD_STATUS), "生成失败")

    def test_existing_target_source_commit_response_loss_reconciles(self):
        self.add_source()
        first = self.service.generate_review_md("source1")
        source = self.gateway.source_records["source1"]["fields"]
        source[SOURCE_STATUS] = "生成失败"
        original = self.gateway.update_source_record

        def lose_response(record_id, fields):
            original(record_id, fields)
            if fields.get(SOURCE_STATUS) == STATUS_GENERATED:
                raise GatewayError("feishu_unreachable", "Response was lost.")

        with mock.patch.object(self.gateway, "update_source_record", side_effect=lose_response):
            result = self.service.generate_review_md("source1")

        self.assertEqual(result["status"], "skipped_existing")
        self.assertTrue(result["reconciled"])
        self.assertEqual(result["record_id"], "source1")
        self.assertIn(first["target_record_id"], self.gateway.target_records)

    def test_archive_commit_response_loss_reconciles_confirmed_terminal(self):
        self.add_source()
        target_id = self.service.generate_review_md("source1")["target_record_id"]
        fields = self.gateway.target_records[target_id]["fields"]
        fields[FIELD_REVIEW] = True
        original = self.gateway.update_target_record

        def lose_response(record_id, updates):
            original(record_id, updates)
            if updates.get(FIELD_ARCHIVE_STATUS) == "已归档":
                raise GatewayError("feishu_unreachable", "Response was lost.")

        with mock.patch.object(self.gateway, "update_target_record", side_effect=lose_response):
            result = self.service.archive_review_md(target_id)

        self.assertEqual(result["status"], "archived")
        self.assertTrue(result["reconciled"])
        self.assertEqual(fields[FIELD_ARCHIVE_STATUS], "已归档")
        self.assertEqual(fields[FIELD_VERSION_STATUS], "已完成")

    def test_archive_unconfirmed_commit_does_not_write_failure(self):
        self.add_source()
        target_id = self.service.generate_review_md("source1")["target_record_id"]
        fields = self.gateway.target_records[target_id]["fields"]
        fields[FIELD_REVIEW] = True
        original = self.gateway.update_target_record

        def lose_before_apply(record_id, updates):
            if updates.get(FIELD_ARCHIVE_STATUS) == "已归档":
                raise GatewayError("feishu_unreachable", "Response was lost.")
            original(record_id, updates)

        with mock.patch.object(self.gateway, "update_target_record", side_effect=lose_before_apply):
            with self.assertRaises(WorkflowError) as caught:
                self.service.archive_review_md(target_id)

        self.assertEqual(caught.exception.code, "archive_commit_outcome_uncertain")
        self.assertEqual(caught.exception.response_status, "outcome_uncertain")
        self.assertEqual(fields[FIELD_ARCHIVE_STATUS], "归档中")
        self.assertEqual(fields[FIELD_VERSION_STATUS], "基线已留存")

    def test_archive_reconciliation_read_failure_is_uncertain_without_downgrade(self):
        self.add_source()
        target_id = self.service.generate_review_md("source1")["target_record_id"]
        fields = self.gateway.target_records[target_id]["fields"]
        fields[FIELD_REVIEW] = True
        original_update = self.gateway.update_target_record
        original_get = self.gateway.get_target_record
        reconciliation_started = False

        def lose_response(record_id, updates):
            nonlocal reconciliation_started
            original_update(record_id, updates)
            if updates.get(FIELD_ARCHIVE_STATUS) == "已归档":
                reconciliation_started = True
                raise GatewayError("feishu_unreachable", "Response was lost.")

        def fail_reconciliation_read(record_id):
            if reconciliation_started:
                raise GatewayError("feishu_unreachable", "Reconciliation read failed.")
            return original_get(record_id)

        with mock.patch.object(self.gateway, "update_target_record", side_effect=lose_response), mock.patch.object(
            self.gateway, "get_target_record", side_effect=fail_reconciliation_read
        ):
            with self.assertRaises(WorkflowError) as caught:
                self.service.archive_review_md(target_id)

        self.assertEqual(caught.exception.code, "archive_commit_outcome_uncertain")
        self.assertEqual(fields[FIELD_ARCHIVE_STATUS], "已归档")
        self.assertNotEqual(fields.get(FIELD_ARCHIVE_STATUS), "归档失败")

    def test_handler_exposes_outcome_uncertain_status(self):
        class UncertainOrchestrator:
            cfg = SimpleNamespace(max_error_chars=300)

            def generate_review_md(self, record_id):
                raise WorkflowError(
                    "review_commit_outcome_uncertain",
                    "Review commit outcome could not be confirmed.",
                    503,
                    response_status="outcome_uncertain",
                )

            archive_review_md = generate_review_md

        handler_type = make_handler(UncertainOrchestrator(), "secret")
        handler = object.__new__(handler_type)
        handler.path = "/generate-review-md"
        handler.headers = Message()
        body = b'{"record_id":"source1"}'
        handler.headers.add_header("Authorization", "Bearer secret")
        handler.headers.add_header("Content-Length", str(len(body)))
        handler.headers.add_header("Content-Type", "application/json")
        handler.rfile = io.BytesIO(body)
        handler.wfile = io.BytesIO()
        status = []
        handler.send_response = status.append
        handler.send_header = lambda *args: None
        handler.end_headers = lambda: None

        handler.do_POST()

        payload = svc.json.loads(handler.wfile.getvalue().decode("utf-8"))
        self.assertEqual(status, [503])
        self.assertEqual(payload["status"], "outcome_uncertain")
        self.assertEqual(payload["error_code"], "review_commit_outcome_uncertain")

    def test_distribution_restriction_fails_before_status_or_target_write(self):
        self.add_source()
        source_url = self.gateway.source_records["source1"]["fields"][FIELD_ARCHIVE_LINK]["link"]
        restricted = "# approved source\n不要传出去。\n".encode()
        self.gateway.edit_file(source_url, restricted)
        self.gateway.source_records["source1"]["fields"][FIELD_APPROVED_SHA] = sha(restricted)

        with self.assertRaises(WorkflowError) as caught:
            self.service.generate_review_md("source1")
        self.assertEqual(caught.exception.code, "restricted_distribution_language")
        self.assertEqual(self.gateway.source_records["source1"]["fields"]["脱敏生成状态"], "待生成")
        self.assertEqual(self.gateway.target_records, {})

    def test_unknown_exception_text_is_not_exposed(self):
        code, message = safe_error(RuntimeError("会议正文秘密"), 300)
        self.assertEqual(code, "internal_error")
        self.assertNotIn("会议正文秘密", message)


if __name__ == "__main__":
    unittest.main()
