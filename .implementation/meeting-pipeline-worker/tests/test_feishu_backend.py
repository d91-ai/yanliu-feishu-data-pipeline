from __future__ import annotations

import importlib.util
import hashlib
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "feishu_backend.py"
SPEC = importlib.util.spec_from_file_location("feishu_backend_tested", SCRIPT)
assert SPEC and SPEC.loader
backend = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = backend
SPEC.loader.exec_module(backend)


class StubService:
    pass


class StubContract:
    pass


def config(root: Path, *, enabled=False):
    return backend.FeishuBackendConfig(
        app_id="app",
        app_secret="secret",
        base_token="base",
        table_id="table",
        source_current_parent="source",
        industry_md_parent="industry-md",
        industry_json_parent="industry-json",
        structured_md_parent="structured-md",
        structured_json_parent="structured-json",
        baseline_parent="baseline",
        reviewed_parent="reviewed",
        history_parent="history",
        generation_job_root=root / "jobs",
        registry_path=root / "registry.json",
        lock_root=root / "locks",
        folder_registry_path=root / "folders.json",
        output_dir=root / "outputs",
        unified_enabled=enabled,
    )


class FeishuBackendSafetyTests(unittest.TestCase):
    def test_current_runtime_modules_are_importable(self):
        service, contract = backend.load_runtime_modules()
        self.assertTrue(callable(service.get_bitable_record_from))

    def test_publish_filename_includes_anonymous_meeting_uid(self):
        _runtime_service, contract = backend.load_runtime_modules()
        uploaded = []
        service = SimpleNamespace(
            list_drive_folder_items=lambda *_args: [],
            upload_drive_file=lambda _cfg, _folder, name, _content, **_kwargs: (
                uploaded.append(name) or "file-token"
            ),
            resolve_uploaded_file_url=lambda *_args: "https://example.test/file",
        )
        with tempfile.TemporaryDirectory() as directory:
            instance = backend.FeishuPipelineBackend(
                config(Path(directory)), service=service, contract=contract
            )
            instance._month_folder = lambda *_args: "folder-token"
            meeting_uid = "mtg_550e8400e29b41d4a716446655440000"
            result = instance._publish(
                parent="parent-token",
                meeting_date="2032-08-13",
                meeting_series="示例研究周会",
                meeting_uid=meeting_uid,
                artifact_type="structured_viewpoints",
                data_version=1,
                extension="md",
                content=b"# result\n",
            )

        self.assertEqual(uploaded, [result["file_name"]])
        self.assertIn("示例研究周会", result["file_name"])
        self.assertIn(meeting_uid, result["file_name"])
        self.assertTrue(callable(contract.validate_artifact_metadata))

    def test_runtime_modules_can_be_loaded_from_packaged_absolute_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service_root = root / "structured-service"
            service_root.mkdir()
            (service_root / "skill_contract.py").write_text(
                "def load_skill_contract(*args, **kwargs):\n    return None\n",
                encoding="utf-8",
            )
            (service_root / "structured_generate_service.py").write_text(
                "from skill_contract import load_skill_contract\n\n"
                "def get_bitable_record_from():\n    return None\n",
                encoding="utf-8",
            )
            contract_path = root / "meeting_pipeline_contract.py"
            contract_path.write_text(
                "def validate_artifact_metadata(*args, **kwargs):\n    return None\n",
                encoding="utf-8",
            )
            service, contract = backend.load_runtime_modules(
                service_root, contract_path
            )
            self.assertTrue(callable(service.get_bitable_record_from))
            self.assertTrue(callable(contract.validate_artifact_metadata))

    def test_config_requires_all_production_resources(self):
        with self.assertRaises(backend.PipelineJobError) as captured:
            backend.FeishuBackendConfig.from_env({})
        self.assertEqual(captured.exception.code, "feishu_backend_config_missing")

    def test_writes_require_both_apply_and_environment_enable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            disabled_env = backend.FeishuPipelineBackend(
                config(root, enabled=False),
                apply=True,
                service=StubService(),
                contract=StubContract(),
            )
            with self.assertRaisesRegex(backend.PipelineJobError, "production_backend_disabled"):
                disabled_env._require_apply()
            missing_apply = backend.FeishuPipelineBackend(
                config(root, enabled=True),
                apply=False,
                service=StubService(),
                contract=StubContract(),
            )
            with self.assertRaisesRegex(backend.PipelineJobError, "production_backend_disabled"):
                missing_apply._require_apply()

    def test_review_receipt_registry_is_private_and_round_trips(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            instance = backend.FeishuPipelineBackend(
                config(root, enabled=True),
                apply=True,
                service=StubService(),
                contract=StubContract(),
            )
            digest = "a" * 64
            instance._save_registry_entry(
                meeting_uid="mtg_550e8400e29b41d4a716446655440000",
                artifact_type="industry_market_viewpoints",
                data_version=2,
                links={"json": {"url": "https://example.test/file/json"}},
                review_md_sha256=digest,
            )
            self.assertEqual(
                instance.review_receipt(
                    {
                        "meeting_uid": "mtg_550e8400e29b41d4a716446655440000",
                        "artifact_type": "industry_market_viewpoints",
                        "review_md_sha256": digest,
                        "record_id": "rec-one",
                        "data_version": 1,
                    }
                )["data_version"],
                2,
            )
            self.assertEqual(instance.config.registry_path.stat().st_mode & 0o777, 0o600)

    def test_old_file_cleanup_keeps_new_authority_when_move_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            instance = backend.FeishuPipelineBackend(
                config(Path(directory), enabled=True),
                apply=True,
                service=StubService(),
                contract=StubContract(),
            )
            instance._move_to_history = lambda *_args: (_ for _ in ()).throw(
                backend.PipelineJobError("move_failed")
            )
            result = instance._cleanup_old_files(
                old_tokens={"md": "old-md", "json": "old-json"},
                new_tokens={"md": "new-md", "json": "new-json"},
                artifact_type="structured_viewpoints",
                meeting_date="2032-08-13",
            )
            self.assertEqual(result["status"], "cleanup_pending")
            self.assertEqual(
                {item["file_token"] for item in result["pending"]},
                {"old-md", "old-json"},
            )

    def test_v9_structured_json_accepts_empty_rows_and_exact_markdown_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            instance = backend.FeishuPipelineBackend(
                config(Path(directory), enabled=True),
                apply=True,
                service=StubService(),
                contract=StubContract(),
            )
            review_bytes = b"# structured\r\n"
            artifact = backend.GeneratedArtifact(
                artifact_type="structured_viewpoints",
                review_markdown=review_bytes.decode("utf-8"),
                json_artifact={
                    "metadata": {
                        "meeting_id": "mtg_550e8400e29b41d4a716446655440000",
                        "structured_markdown_sha256": hashlib.sha256(review_bytes).hexdigest(),
                        "schema_version": 9,
                        "security_master_version": "unavailable",
                    },
                    "rows": [],
                },
            )
            metadata = instance._validate_structured_json(
                {
                    "meeting_uid": "mtg_550e8400e29b41d4a716446655440000",
                    "artifact_type": "structured_viewpoints",
                },
                artifact,
                review_bytes,
            )
            self.assertEqual(metadata["schema_version"], 9)

    def test_structured_generation_publishes_markdown_and_json(self):
        with tempfile.TemporaryDirectory() as directory:
            instance = backend.FeishuPipelineBackend(
                config(Path(directory), enabled=True),
                apply=True,
                service=SimpleNamespace(
                    list_bitable_fields=lambda *_args: [],
                    fields_by_name=lambda _fields: {},
                    url_cell_value=lambda _fields, _name, url, file_name: {
                        "text": file_name,
                        "link": url,
                    },
                ),
                contract=StubContract(),
            )
            record = {
                "data_version": 1,
                "source_md_sha256": "a" * 64,
                "meeting_date": "2032-08-13",
                "meeting_series": "test",
                "artifact_review_statuses": {"structured_viewpoints": "已审核"},
                "current_file_tokens": {
                    "structured_viewpoints": {"md": "old-md", "json": "old-json"}
                },
            }
            published = []

            def publish(**kwargs):
                published.append(kwargs["extension"])
                return {
                    "file_token": f"new-{kwargs['extension']}",
                    "url": f"https://example.test/{kwargs['extension']}",
                    "file_name": f"new.{kwargs['extension']}",
                    "sha256": "b" * 64,
                }

            committed_fields = {}

            def update(_record_id, fields, expected, _version):
                committed_fields.update(fields)
                self.assertEqual(expected["标的观点JSON"], "https://example.test/json")
                return {"record_id": "rec-one", "data_version": 1}

            instance.get_record = lambda _record_id: record
            instance._publish = publish
            instance._update_and_confirm = update
            instance._cleanup_old_files = lambda **_kwargs: {
                "status": "cleanup_complete",
                "archived": [],
                "pending": [],
            }
            instance._save_registry_entry = lambda **_kwargs: None
            result = instance.commit_generation(
                {
                    "record_id": "rec-one",
                    "meeting_uid": "mtg_550e8400e29b41d4a716446655440000",
                    "artifact_type": "structured_viewpoints",
                    "input_md_sha256": "a" * 64,
                },
                backend.GeneratedArtifact(
                    artifact_type="structured_viewpoints",
                    review_markdown="# structured\n",
                    json_artifact={
                        "metadata": {
                            "meeting_id": "mtg_550e8400e29b41d4a716446655440000",
                            "structured_markdown_sha256": hashlib.sha256(
                                b"# structured\n"
                            ).hexdigest(),
                            "schema_version": 9,
                            "security_master_version": "unavailable",
                        },
                        "rows": [],
                    },
                ),
                expected_version=1,
            )

            self.assertEqual(published, ["md", "md", "json"])
            self.assertEqual(
                committed_fields["标的观点JSON"]["link"],
                "https://example.test/json",
            )
            self.assertEqual(committed_fields["标的观点审核"], "需重审")
            self.assertEqual(result["status"], "cleanup_complete")


if __name__ == "__main__":
    unittest.main()
