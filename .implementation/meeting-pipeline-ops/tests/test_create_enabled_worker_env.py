from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCRIPT = ROOT / "create_enabled_worker_env.py"
SPEC = importlib.util.spec_from_file_location("create_enabled_worker_env_tested", SCRIPT)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


class EnabledWorkerEnvironmentTests(unittest.TestCase):
    def test_only_enable_flag_changes(self):
        source = {
            "FEISHU_UNIFIED_PIPELINE_ENABLED": "false",
            "FEISHU_APP_SECRET": "secret",
        }
        target = module.enabled_values(source)
        self.assertEqual(target["FEISHU_UNIFIED_PIPELINE_ENABLED"], "true")
        self.assertEqual(target["FEISHU_APP_SECRET"], "secret")
        self.assertEqual(source["FEISHU_UNIFIED_PIPELINE_ENABLED"], "false")

    def test_source_must_be_explicitly_disabled(self):
        with self.assertRaises(module.EnvironmentError):
            module.enabled_values({"FEISHU_UNIFIED_PIPELINE_ENABLED": "true"})


if __name__ == "__main__":
    unittest.main()
