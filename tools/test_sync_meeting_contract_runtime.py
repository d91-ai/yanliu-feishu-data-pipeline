from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest


MODULE_PATH = Path(__file__).with_name("sync_meeting_contract_runtime.py")
SPEC = importlib.util.spec_from_file_location("sync_meeting_contract_runtime", MODULE_PATH)
assert SPEC and SPEC.loader
tool = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = tool
SPEC.loader.exec_module(tool)


class MeetingContractRuntimeSyncTests(unittest.TestCase):
    def test_read_env_value_returns_only_requested_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env = Path(tmpdir) / ".env"
            env.write_text(
                "FEISHU_APP_SECRET=secret\n"
                "FEISHU_MEETING_CONTRACT_VALIDATOR_SHA256=abc123\n",
                encoding="utf-8",
            )
            self.assertEqual(
                tool.read_env_value(env, "FEISHU_MEETING_CONTRACT_VALIDATOR_SHA256"),
                "abc123",
            )

    def test_safe_health_summary_filters_payload(self) -> None:
        summary = tool.safe_health_summary(
            {
                "ok": True,
                "status": 200,
                "payload": {
                    "service": "feishu-minutes-upload",
                    "ready": True,
                    "checks": {"meeting_contract": True},
                    "request_id": "not-needed",
                    "token": "must-not-leak",
                },
            }
        )
        self.assertEqual(summary["service"], "feishu-minutes-upload")
        self.assertNotIn("token", summary)
        self.assertNotIn("request_id", summary)

    def test_restart_without_apply_is_rejected_by_argparse_layer(self) -> None:
        with self.assertRaises(SystemExit):
            old_argv = tool.sys.argv
            try:
                tool.sys.argv = ["sync", "--restart"]
                tool.main()
            finally:
                tool.sys.argv = old_argv


if __name__ == "__main__":
    unittest.main()
