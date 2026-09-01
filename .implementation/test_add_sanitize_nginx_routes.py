from __future__ import annotations

import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


MODULE_PATH = Path(__file__).with_name("add_sanitize_nginx_routes.py")
SPEC = importlib.util.spec_from_file_location("add_sanitize_nginx_routes", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class NginxRouteInstallerTests(unittest.TestCase):
    def test_adds_complete_route_set_once(self) -> None:
        updated, status = MODULE.planned_text("prefix\n" + MODULE.ANCHOR + "suffix\n")
        self.assertEqual(status, "needs_install")
        for marker in MODULE.ROUTE_MARKERS:
            self.assertEqual(updated.count(marker), 1)
        self.assertNotIn(MODULE.LEGACY_ROUTE_MARKER, updated)
        self.assertEqual(updated.count(MODULE.ANCHOR), 1)

    def test_migrates_complete_legacy_route_set_exactly(self) -> None:
        self.assertIn(
            "archive-review-md;\n      include proxy.conf;\n    }\n\n"
            "    location = /feishu-sanitize/generate-official-json",
            MODULE.LEGACY_ROUTES,
        )
        current = "prefix\n" + MODULE.ANCHOR + MODULE.LEGACY_ROUTES + "suffix\n"
        updated, status = MODULE.planned_text(current)
        self.assertEqual(status, "needs_migration")
        self.assertEqual(updated, current.replace(MODULE.LEGACY_ROUTES, MODULE.ROUTES, 1))
        self.assertEqual(updated.count(MODULE.ANCHOR), 1)
        self.assertNotIn(MODULE.LEGACY_ROUTE_MARKER, updated)

    def test_is_idempotent(self) -> None:
        first, status = MODULE.planned_text(MODULE.ANCHOR + MODULE.LEGACY_ROUTES)
        self.assertEqual(status, "needs_migration")
        second, status = MODULE.planned_text(first)
        self.assertEqual(status, "already_present")
        self.assertEqual(first, second)

    def test_rejects_partial_route_set(self) -> None:
        with self.assertRaisesRegex(ValueError, "partial"):
            MODULE.planned_text(MODULE.ANCHOR + MODULE.ROUTE_MARKERS[0])

    def test_rejects_ambiguous_route_sets(self) -> None:
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            MODULE.planned_text(MODULE.ANCHOR + MODULE.LEGACY_ROUTES + MODULE.LEGACY_ROUTES)
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            MODULE.planned_text(MODULE.ANCHOR + MODULE.ROUTES + MODULE.LEGACY_ROUTE_MARKER)

    def test_rejects_missing_or_ambiguous_anchor(self) -> None:
        with self.assertRaisesRegex(ValueError, "anchor"):
            MODULE.planned_text("no anchor")
        with self.assertRaisesRegex(ValueError, "anchor"):
            MODULE.planned_text(MODULE.ANCHOR + MODULE.ANCHOR)

    def test_rejects_different_live_and_template_states(self) -> None:
        with TemporaryDirectory() as temp_dir:
            live = Path(temp_dir) / "default.conf"
            template = Path(temp_dir) / "default.conf.template"
            live.write_text(MODULE.ANCHOR + MODULE.LEGACY_ROUTES, encoding="utf-8")
            template.write_text(MODULE.ANCHOR + MODULE.ROUTES, encoding="utf-8")
            with patch.object(MODULE, "CONFIG_PATHS", (live, template)):
                with self.assertRaisesRegex(ValueError, "states differ"):
                    MODULE.check_paths()


if __name__ == "__main__":
    unittest.main()
