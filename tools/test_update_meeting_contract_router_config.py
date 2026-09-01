from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).with_name("update_meeting_contract_router_config.py")
SPEC = importlib.util.spec_from_file_location("update_meeting_contract_router_config", MODULE_PATH)
assert SPEC and SPEC.loader
tool = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = tool
SPEC.loader.exec_module(tool)


class MultiFileConfigUpdateTests(unittest.TestCase):
    def test_preflight_failure_changes_no_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            first = root / "first.env"
            first.write_text("A=old\n", encoding="utf-8")
            missing = root / "missing.env"
            with self.assertRaises(FileNotFoundError):
                tool.update_files_with_rollback([(first, {"A": "new"}), (missing, {"B": "new"})])
            self.assertEqual(first.read_text(encoding="utf-8"), "A=old\n")

    def test_reported_commit_failure_restores_all_originals(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            paths = [root / f"{index}.env" for index in range(4)]
            for index, path in enumerate(paths):
                path.write_text(f"VALUE=old-{index}\n", encoding="utf-8")
            real_replace = tool.os.replace
            calls = 0

            def fail_second(source, target):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected commit failure")
                return real_replace(source, target)

            with mock.patch.object(tool.os, "replace", side_effect=fail_second):
                with self.assertRaises(OSError):
                    tool.update_files_with_rollback(
                        [(path, {"VALUE": f"new-{index}"}) for index, path in enumerate(paths)]
                    )

            for index, path in enumerate(paths):
                self.assertEqual(path.read_text(encoding="utf-8"), f"VALUE=old-{index}\n")
            self.assertFalse(list(root.glob(".*.tmp")))

    def test_staging_failure_removes_the_current_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            path = root / "secret.env"
            path.write_text("SECRET=old\n", encoding="utf-8")
            with mock.patch.object(tool.os, "fsync", side_effect=OSError("injected fsync failure")):
                with self.assertRaisesRegex(OSError, "injected fsync failure"):
                    tool.update_files_with_rollback([(path, {"SECRET": "new"})])

            self.assertEqual(path.read_text(encoding="utf-8"), "SECRET=old\n")
            self.assertFalse(list(root.glob(".secret.env.*.tmp")))

    def test_concurrent_bundle_updates_are_serialized(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            paths = [root / f"{index}.env" for index in range(4)]
            for path in paths:
                path.write_text("BUNDLE=old\n", encoding="utf-8")

            active = 0
            max_active = 0
            state_lock = threading.Lock()
            real_render = tool.render_update

            def slow_render(path, updates):
                nonlocal active, max_active
                with state_lock:
                    active += 1
                    max_active = max(max_active, active)
                try:
                    time.sleep(0.01)
                    return real_render(path, updates)
                finally:
                    with state_lock:
                        active -= 1

            errors: list[BaseException] = []

            def apply_bundle(value: str) -> None:
                try:
                    tool.update_files_with_rollback(
                        [(path, {"BUNDLE": value}) for path in paths]
                    )
                except BaseException as exc:  # pragma: no cover - asserted below
                    errors.append(exc)

            with mock.patch.object(tool, "render_update", side_effect=slow_render):
                threads = [threading.Thread(target=apply_bundle, args=(value,)) for value in ("one", "two")]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=3)

            self.assertFalse(errors)
            self.assertEqual(max_active, 1)
            final_values = {path.read_text(encoding="utf-8") for path in paths}
            self.assertIn(final_values, ({"BUNDLE=one\n"}, {"BUNDLE=two\n"}))


if __name__ == "__main__":
    unittest.main()
