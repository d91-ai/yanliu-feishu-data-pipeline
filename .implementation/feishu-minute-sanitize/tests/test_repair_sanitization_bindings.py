from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


REPAIR_PATH = Path(__file__).resolve().parents[2] / "repair_sanitization_bindings.py"
SPEC = importlib.util.spec_from_file_location("repair_sanitization_bindings", REPAIR_PATH)
assert SPEC and SPEC.loader
repair = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = repair
SPEC.loader.exec_module(repair)


class ManifestDrivenRepairTests(unittest.TestCase):
    def fixture(self, root: Path, *, expected_count: int = 1):
        source = "示例段落：待确认。\n".encode("utf-8")
        output = "示例段落：已确认。\n".encode("utf-8")
        manifest = {
            "schema_version": 1,
            "source_sha256": hashlib.sha256(source).hexdigest(),
            "output_sha256": hashlib.sha256(output).hexdigest(),
            "replacements": [
                {"kind": "literal", "before": "待确认", "after": "已确认", "expected_count": expected_count}
            ],
        }
        path = root / "manifest.json"
        path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        return source, output, path

    def test_hash_bound_repair_accepts_reviewed_fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            source, expected, path = self.fixture(Path(directory))
            manifest = repair.load_manifest(path)
            self.assertEqual(repair.normalize_source(source, manifest), expected)

    def test_source_hash_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            _source, _expected, path = self.fixture(Path(directory))
            manifest = repair.load_manifest(path)
            with self.assertRaisesRegex(repair.RepairError, "approved hash"):
                repair.normalize_source(b"changed", manifest)

    def test_replacement_count_drift_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            source, _expected, path = self.fixture(Path(directory), expected_count=2)
            manifest = repair.load_manifest(path)
            with self.assertRaisesRegex(repair.RepairError, "count changed"):
                repair.normalize_source(source, manifest)


if __name__ == "__main__":
    unittest.main()
