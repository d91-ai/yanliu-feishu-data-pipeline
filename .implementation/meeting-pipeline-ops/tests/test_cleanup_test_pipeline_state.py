from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "cleanup_test_pipeline_state.py"
SPEC = importlib.util.spec_from_file_location("cleanup_test_pipeline_state_tested", SCRIPT)
module = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(module)


class CleanupTests(unittest.TestCase):
    def test_registry_prune_is_identity_scoped(self):
        target = "mtg_" + "a" * 32
        keep = "mtg_" + "b" * 32
        registry = {
            "version": 1,
            "artifacts": {target + ":structured_viewpoints": {}, keep + ":structured_viewpoints": {}},
            "review_receipts": {target + ":receipt": {}, keep + ":receipt": {}},
        }
        value, removed = module.prune_registry(registry, {target})
        self.assertEqual(set(value["artifacts"]), {keep + ":structured_viewpoints"})
        self.assertEqual(set(value["review_receipts"]), {keep + ":receipt"})
        self.assertEqual(len(removed), 2)


if __name__ == "__main__":
    unittest.main()
