from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "prepare_public_worker_env.py"
SPEC = importlib.util.spec_from_file_location("prepare_public_worker_env_tested", SCRIPT)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
sys.path.insert(0, str(ROOT))
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


class PreparePublicWorkerEnvTests(unittest.TestCase):
    def test_embedded_assets_and_shared_router_paths_are_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            router_data = Path(directory).resolve()
            values = module.prepared_values(
                {"FEISHU_UNIFIED_PIPELINE_ENABLED": "true"}, router_data
            )
        self.assertEqual(values["FEISHU_UNIFIED_PIPELINE_ENABLED"], "false")
        self.assertEqual(
            values["FEISHU_GENERATION_JOB_SPOOL_PATH"],
            str(router_data / "meeting-generation-jobs"),
        )
        for key in (
            "MEETING_PIPELINE_CONTRACT_SHA256",
            "MEETING_PIPELINE_CONTRACT_RUNTIME_SHA256",
            "INDUSTRY_MARKET_SKILL_RUNTIME_SHA256",
            "STRUCTURED_SKILL_RUNTIME_SHA256",
        ):
            self.assertRegex(values[key], r"^[0-9a-f]{64}$")
        self.assertTrue(
            Path(values["STRUCTURED_SKILL_ROOT"], "SKILL.md").is_file()
        )


if __name__ == "__main__":
    unittest.main()
