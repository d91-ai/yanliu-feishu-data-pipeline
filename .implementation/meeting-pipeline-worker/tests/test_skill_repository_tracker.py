from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "track_skill_repositories.py"
SPEC = importlib.util.spec_from_file_location("skill_repository_tracker_tested", SCRIPT)
assert SPEC and SPEC.loader
tracker = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = tracker
SPEC.loader.exec_module(tracker)


def repository(*, promoted: str, enabled: bool) -> dict[str, object]:
    return {
        "name": "example-skill",
        "url": "https://github.com/your-org/example-skill.git",
        "branch": "main",
        "promoted_commit": promoted,
        "runtime_enabled": enabled,
    }


class SkillRepositoryTrackerTests(unittest.TestCase):
    def test_enabled_runtime_requires_explicit_promoted_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "repositories.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "repositories": [repository(promoted="", enabled=True)],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(tracker.TrackerError):
                tracker.read_config(path)

    def test_enabled_update_requires_promotion(self):
        status = tracker.status_for(
            repository(promoted="a" * 40, enabled=True), "b" * 40
        )
        self.assertTrue(status["update_available"])
        self.assertTrue(status["promotion_required"])

    def test_disabled_unpromoted_repository_is_tracking_only(self):
        status = tracker.status_for(
            repository(promoted="", enabled=False), "b" * 40
        )
        self.assertTrue(status["update_available"])
        self.assertFalse(status["promotion_required"])

    def test_matching_promoted_commit_has_no_update(self):
        head = "a" * 40
        status = tracker.status_for(
            repository(promoted=head, enabled=True), head
        )
        self.assertFalse(status["update_available"])
        self.assertFalse(status["promotion_required"])

    def test_mirror_head_reads_fetched_branch_without_second_remote_request(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "example-skill.git"
            target.mkdir()
            original = tracker.git
            calls = []
            try:
                tracker.git = lambda args: calls.append(args) or "a" * 40
                self.assertEqual(
                    tracker.mirror_head(
                        root, repository(promoted="a" * 40, enabled=True)
                    ),
                    "a" * 40,
                )
            finally:
                tracker.git = original
            self.assertEqual(calls[0][-1], "refs/heads/main")


if __name__ == "__main__":
    unittest.main()
