from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


WORKSPACE = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


structured = load_module("structured_regeneration_guard_test", WORKSPACE / "tools/run_structured_regeneration.py")
sanitization = load_module("sanitization_binding_guard_test", WORKSPACE / ".implementation/repair_sanitization_bindings.py")


class GenericEntrypointGuardTests(unittest.TestCase):
    def test_structured_default_is_offline_dry_run(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            sys, "argv", ["run_structured_regeneration.py", "--runtime-dir", directory, "--work-dir", directory]
        ), mock.patch.object(structured, "load_service_module") as load_service, mock.patch("builtins.print"):
            self.assertEqual(structured.main(), 0)
        load_service.assert_not_called()

    def test_sanitization_requires_hash_bound_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = b"before"
            output = b"after"
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({
                "schema_version": 1,
                "source_sha256": hashlib.sha256(source).hexdigest(),
                "output_sha256": hashlib.sha256(output).hexdigest(),
                "replacements": [{"kind": "literal", "before": "before", "after": "after", "expected_count": 1}],
            }), encoding="utf-8")
            source_path = root / "source.md"
            source_path.write_bytes(source)
            with mock.patch.object(sys, "argv", [
                "repair_sanitization_bindings.py", "--manifest", str(manifest), "--input", str(source_path), "--output", str(root / "out.md")
            ]), mock.patch("builtins.print"):
                self.assertEqual(sanitization.main(), 0)
            self.assertFalse((root / "out.md").exists())


if __name__ == "__main__":
    unittest.main()
