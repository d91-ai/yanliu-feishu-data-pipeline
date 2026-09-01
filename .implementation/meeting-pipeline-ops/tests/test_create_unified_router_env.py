from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCRIPT = ROOT / "create_unified_router_env.py"
SPEC = importlib.util.spec_from_file_location("create_unified_router_env_tested", SCRIPT)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


class UnifiedRouterEnvironmentTests(unittest.TestCase):
    def test_route_is_unified_but_form_ingress_stays_separately_disabled(self):
        values = module.build_route_values(
            {
                "FEISHU_MEETING_CONTRACT_ENABLED": "true",
                "FEISHU_MEETING_CONTRACT_VALIDATOR": "/skills/validator.py",
                "FEISHU_MEETING_CONTRACT_VALIDATOR_SHA256": "a" * 64,
            },
            {
                "base_token": "base",
                "table_id": "table",
                "folders": {
                    "source_current": "source",
                    "history": "history",
                    "baseline": "baseline",
                },
            },
            watermark_ms=123,
            form_ingress_enabled=False,
        )
        self.assertEqual(values["FEISHU_PIPELINE_MODE"], "unified")
        self.assertEqual(values["FEISHU_FORM_INGRESS_ENABLED"], "false")
        self.assertEqual(values["FEISHU_PIPELINE_EVENT_NOT_BEFORE_MS"], "123")

    def test_watermark_is_required(self):
        with self.assertRaises(module.EnvironmentError):
            module.build_route_values(
                {},
                {"folders": {}},
                watermark_ms=0,
                form_ingress_enabled=False,
            )


if __name__ == "__main__":
    unittest.main()
