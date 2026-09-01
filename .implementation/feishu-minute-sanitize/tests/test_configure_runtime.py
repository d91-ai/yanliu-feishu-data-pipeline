from __future__ import annotations

import hashlib
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import configure_runtime as runtime  # noqa: E402
from configure_runtime import ConfigError, RESOURCE_KEYS, ensure_runtime_secrets, render_env  # noqa: E402
from skill_adapter import APPROVED_SKILL_PINS  # noqa: E402


REVISION = "2716eae7d3286abda46f71e9d4e8bbb4712fb32b"


class ConfigureRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_pins = dict(APPROVED_SKILL_PINS)

    def tearDown(self) -> None:
        APPROVED_SKILL_PINS.clear()
        APPROVED_SKILL_PINS.update(self.original_pins)

    @staticmethod
    def make_skill(root: Path) -> tuple[Path, str]:
        skill = root / "skill"
        script = skill / "scripts" / "sanitize_minutes.py"
        script.parent.mkdir(parents=True)
        script.write_text("print('sanitizer')\n", encoding="utf-8")
        digest = hashlib.sha256(script.read_bytes()).hexdigest()
        APPROVED_SKILL_PINS[REVISION] = digest
        return skill, digest

    def test_secrets_are_file_based_and_http_token_is_stable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.env"
            source.write_text("FEISHU_APP_ID=app-test\nFEISHU_APP_SECRET=secret-test\n", encoding="utf-8")
            output = root / "runtime"
            first = ensure_runtime_secrets(source, output)
            token = first["http_token_path"].read_text(encoding="utf-8")
            second = ensure_runtime_secrets(source, output)
            self.assertEqual(second["http_token_path"].read_text(encoding="utf-8"), token)
            self.assertEqual(first["app_secret_path"].read_text(encoding="utf-8"), "secret-test\n")
            self.assertEqual(first["app_secret_path"].stat().st_mode & 0o777, 0o600)

    def test_rendered_env_has_no_secret_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill, script_sha = self.make_skill(root)
            resources = {key: f"value-{key}" for key in RESOURCE_KEYS}
            value = render_env(
                app_id="app-test",
                output_dir=root / "runtime",
                skill_host_dir=skill,
                skill_source_revision=REVISION,
                source_cutoff="2032-07-13 12:00",
                resources=resources,
            )
            self.assertNotIn("secret-test", value)
            self.assertNotIn("workflow-http-token=", value.lower())
            self.assertIn("FEISHU_APP_SECRET_HOST_FILE=", value)
            self.assertIn("FEISHU_SANITIZE_HTTP_TOKEN_HOST_FILE=", value)
            self.assertIn("SANITIZE_SKILL_COMMAND_JSON=", value)
            self.assertIn("SANITIZE_SKILL_SOURCE_REVISION=2716eae7d3286abda46f71e9d4e8bbb4712fb32b", value)
            self.assertIn(f"SANITIZE_SKILL_SCRIPT_SHA256={script_sha}", value)
            self.assertNotIn("FEISHU_SANITIZE_JSON_ROOT_FOLDER_TOKEN", value)
            self.assertNotIn("SANITIZE_JSON_SCHEMA_VERSIONS", value)
            self.assertIn("FEISHU_SANITIZE_HTTP_HOST=127.0.0.1", value)
            self.assertIn("FEISHU_SANITIZE_HTTP_PORT=8791", value)

    def test_missing_resource_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill, _ = self.make_skill(root)
            resources = {key: f"value-{key}" for key in RESOURCE_KEYS}
            resources["target_table_id"] = ""
            with self.assertRaises(ConfigError):
                render_env(
                    app_id="app-test",
                    output_dir=root / "runtime",
                    skill_host_dir=skill,
                    skill_source_revision=REVISION,
                    source_cutoff="2032-07-13 12:00",
                    resources=resources,
                )

    def test_invalid_revision_or_missing_script_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            resources = {key: f"value-{key}" for key in RESOURCE_KEYS}
            skill, _ = self.make_skill(root)
            with self.assertRaises(ConfigError):
                render_env(
                    app_id="app-test",
                    output_dir=root / "runtime",
                    skill_host_dir=skill,
                    skill_source_revision="short",
                    source_cutoff="2032-07-13 12:00",
                    resources=resources,
                )
            (skill / "scripts" / "sanitize_minutes.py").unlink()
            with self.assertRaises(ConfigError):
                render_env(
                    app_id="app-test",
                    output_dir=root / "runtime",
                    skill_host_dir=skill,
                    skill_source_revision=REVISION,
                    source_cutoff="2032-07-13 12:00",
                    resources=resources,
                )

    def test_unapproved_pin_invalid_cutoff_and_symlinked_scripts_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            resources = {key: f"value-{key}" for key in RESOURCE_KEYS}
            skill, _ = self.make_skill(root)
            script = skill / "scripts" / "sanitize_minutes.py"
            script.write_text("print('changed')\n", encoding="utf-8")
            with self.assertRaises(ConfigError):
                render_env(
                    app_id="app-test",
                    output_dir=root / "runtime",
                    skill_host_dir=skill,
                    skill_source_revision=REVISION,
                    source_cutoff="2032-07-13 12:00",
                    resources=resources,
                )

            skill, _ = self.make_skill(root / "cutoff")
            with self.assertRaises(ConfigError):
                render_env(
                    app_id="app-test",
                    output_dir=root / "runtime",
                    skill_host_dir=skill,
                    skill_source_revision=REVISION,
                    source_cutoff="2032-02-31 29:00",
                    resources=resources,
                )

            real_skill, digest = self.make_skill(root / "real")
            linked_skill = root / "linked"
            linked_skill.mkdir()
            linked_skill.joinpath("scripts").symlink_to(real_skill / "scripts", target_is_directory=True)
            APPROVED_SKILL_PINS[REVISION] = digest
            with self.assertRaises(ConfigError):
                render_env(
                    app_id="app-test",
                    output_dir=root / "runtime",
                    skill_host_dir=linked_skill,
                    skill_source_revision=REVISION,
                    source_cutoff="2032-07-13 12:00",
                    resources=resources,
                )

    def test_invalid_apply_does_not_change_existing_runtime_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill, _ = self.make_skill(root)
            source = root / "source.env"
            source.write_text("FEISHU_APP_ID=new-app\nFEISHU_APP_SECRET=new-secret\n", encoding="utf-8")
            output = root / "runtime"
            secret_dir = output / "secrets"
            secret_dir.mkdir(parents=True)
            app_secret = secret_dir / "feishu-app-secret.txt"
            http_token = secret_dir / "workflow-http-token.txt"
            env_path = output / ".env"
            app_secret.write_text("old-secret\n", encoding="utf-8")
            http_token.write_text("x" * 48 + "\n", encoding="utf-8")
            env_path.write_text("FEISHU_APP_ID=old-app\n", encoding="utf-8")

            args = [
                "--source-env", str(source),
                "--output-dir", str(output),
                "--skill-host-dir", str(skill),
                "--skill-source-revision", REVISION,
                "--source-cutoff", "2032-07-13 12:00",
                "--bitable-app-token", "base",
                "--source-table-id", "source",
                # target-table-id intentionally omitted
                "--pending-root-folder-token", "pending",
                "--archive-root-folder-token", "archive",
                "--version-root-folder-token", "versions",
                "--apply",
            ]
            self.assertEqual(runtime.main(args), 2)
            self.assertEqual(app_secret.read_text(encoding="utf-8"), "old-secret\n")
            self.assertEqual(http_token.read_text(encoding="utf-8"), "x" * 48 + "\n")
            self.assertEqual(env_path.read_text(encoding="utf-8"), "FEISHU_APP_ID=old-app\n")

    def test_runtime_bundle_rolls_back_reported_commit_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first.env"
            second = root / "second.env"
            first.write_text("old-first\n", encoding="utf-8")
            second.write_text("old-second\n", encoding="utf-8")
            real_replace = runtime.os.replace
            calls = 0

            def fail_second(source, target):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected commit failure")
                return real_replace(source, target)

            with mock.patch.object(runtime.os, "replace", side_effect=fail_second):
                with self.assertRaises(OSError):
                    runtime.commit_runtime_files(
                        [(first, "new-first\n", 0o600), (second, "new-second\n", 0o600)]
                    )
            self.assertEqual(first.read_text(encoding="utf-8"), "old-first\n")
            self.assertEqual(second.read_text(encoding="utf-8"), "old-second\n")
            self.assertFalse(list(root.glob(".*.tmp")))

    def test_concurrent_runtime_bundles_are_serialized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = [root / "secret-one", root / "secret-two", root / ".env"]
            for path in paths:
                path.write_text("old\n", encoding="utf-8")

            active = 0
            max_active = 0
            state_lock = threading.Lock()
            real_stage = runtime.stage_private_text

            def slow_stage(path, content, mode):
                nonlocal active, max_active
                with state_lock:
                    active += 1
                    max_active = max(max_active, active)
                try:
                    time.sleep(0.01)
                    return real_stage(path, content, mode)
                finally:
                    with state_lock:
                        active -= 1

            errors: list[BaseException] = []

            def apply_bundle(value: str) -> None:
                try:
                    runtime.commit_runtime_files(
                        [(path, value + "\n", 0o600) for path in paths]
                    )
                except BaseException as exc:  # pragma: no cover - asserted below
                    errors.append(exc)

            with mock.patch.object(runtime, "stage_private_text", side_effect=slow_stage):
                threads = [threading.Thread(target=apply_bundle, args=(value,)) for value in ("one", "two")]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=3)

            self.assertFalse(errors)
            self.assertEqual(max_active, 1)
            final_values = {path.read_text(encoding="utf-8") for path in paths}
            self.assertIn(final_values, ({"one\n"}, {"two\n"}))


if __name__ == "__main__":
    unittest.main()
