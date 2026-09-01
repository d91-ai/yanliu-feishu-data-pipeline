from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "create_disabled_worker_env.py"
SPEC = importlib.util.spec_from_file_location("create_disabled_worker_env_tested", SCRIPT)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


class DisabledWorkerEnvironmentTests(unittest.TestCase):
    def test_private_writer_is_create_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            module.write_private_env(path, {"ENABLED": "false", "SECRET": "value"})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertIn("ENABLED=false", path.read_text(encoding="utf-8"))
            with self.assertRaises(module.EnvironmentError):
                module.write_private_env(path, {"ENABLED": "true"})

    def test_enabled_value_is_fixed_false(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('"FEISHU_UNIFIED_PIPELINE_ENABLED": "false"', source)
        self.assertNotIn('"FEISHU_UNIFIED_PIPELINE_ENABLED": "true"', source)


if __name__ == "__main__":
    unittest.main()
