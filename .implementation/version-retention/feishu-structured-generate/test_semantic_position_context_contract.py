#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
import unittest

from skill_contract import load_skill_contract


SKILL_ROOT = Path(
    os.environ.get(
        "STRUCTURED_TABLE_SKILL_ROOT_TEST",
        "/skills/meeting-minutes-structured-table",
    )
)
if not (SKILL_ROOT / "scripts" / "generate_table.py").is_file():
    raise unittest.SkipTest("v9 structured-table Skill fixture unavailable")
CONTRACT = load_skill_contract(SKILL_ROOT / "scripts" / "generate_table.py")


class SemanticPositionContextContractTests(unittest.TestCase):
    def test_claim_schema_keeps_position_structured_but_auxiliary(self) -> None:
        schema = json.loads(CONTRACT.claim_schema_path.read_text(encoding="utf-8"))
        item = schema["properties"]["claim_units"]["items"]
        target = item["properties"]["targets"]["items"]
        self.assertNotIn("position", target["required"])
        self.assertIn("position", target["properties"])
        self.assertNotIn("position_context", item["properties"])

    def test_claim_schema_uses_controlled_position_fields(self) -> None:
        schema = json.loads(CONTRACT.claim_schema_path.read_text(encoding="utf-8"))
        position = schema["properties"]["claim_units"]["items"]["properties"]["targets"]["items"]["properties"]["position"]
        self.assertEqual(set(position["required"]), {"state", "detail", "plan"})
        self.assertEqual(set(position["properties"]["state"]["enum"]), {"持有", "未持有", "信息不足"})
        self.assertIn("计划增持", position["properties"]["plan"]["enum"])

    def test_prompt_forbids_viewpoint_based_position_inference(self) -> None:
        prompt = CONTRACT.prompt
        self.assertIn("同一发言人", prompt)
        self.assertIn("不要根据单个经营指标", prompt)
        self.assertIn("source_quotes", prompt)
        self.assertIn("不要改写", prompt)
        self.assertNotIn("source_alias", prompt)

    def test_contract_is_v9_schema_v9_with_bundled_security_master(self) -> None:
        self.assertEqual(CONTRACT.contract_version, 9)
        self.assertEqual(CONTRACT.schema_version, 9)
        self.assertEqual(CONTRACT.generate_script.name, "generate_table.py")
        self.assertEqual(CONTRACT.viewpoints_schema_path.name, "viewpoints.schema.json")
        self.assertEqual(CONTRACT.security_master_path.name, "security_master.csv")
        self.assertTrue(CONTRACT.security_master_path.is_file())


if __name__ == "__main__":
    unittest.main()
