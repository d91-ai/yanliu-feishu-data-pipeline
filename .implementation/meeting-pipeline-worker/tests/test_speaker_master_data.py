from __future__ import annotations

import csv
from pathlib import Path
import unicodedata
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
DATA_ROOT = REPOSITORY_ROOT / "data" / "speaker_identity"


def normalized_alias(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


class SpeakerMasterDataTests(unittest.TestCase):
    def test_confirmed_master_has_unique_ids_and_aliases(self) -> None:
        path = DATA_ROOT / "speaker_master.csv"
        with path.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertTrue(rows)
        required = {
            "presenter_id",
            "canonical_name",
            "aliases",
            "series_scope",
            "status",
            "confidence",
            "evidence_refs",
            "notes",
        }
        self.assertEqual(set(rows[0]), required)
        presenter_ids: set[str] = set()
        aliases: dict[str, str] = {}
        for row in rows:
            self.assertEqual(row["status"], "confirmed")
            self.assertNotIn("zsk", (row["evidence_refs"] + row["notes"]).casefold())
            self.assertTrue(row["presenter_id"])
            self.assertTrue(row["canonical_name"])
            self.assertTrue(row["aliases"])
            self.assertNotIn(row["presenter_id"], presenter_ids)
            presenter_ids.add(row["presenter_id"])
            for alias in [row["canonical_name"], *row["aliases"].split("|")]:
                key = normalized_alias(alias)
                self.assertTrue(key)
                existing = aliases.get(key)
                if existing is not None:
                    self.assertEqual(existing, row["presenter_id"], alias)
                aliases[key] = row["presenter_id"]

    def test_needs_review_file_is_parseable_and_not_runtime_confirmed(self) -> None:
        path = DATA_ROOT / "speaker_aliases.needs-review.csv"
        with path.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertTrue(rows)
        self.assertEqual(
            set(rows[0]),
            {
                "candidate_id",
                "aliases",
                "observed_series",
                "confidence",
                "reason",
                "required_confirmation",
            },
        )
        self.assertEqual(len({row["candidate_id"] for row in rows}), len(rows))
        for row in rows:
            self.assertNotIn(
                "zsk", (row["reason"] + row["required_confirmation"]).casefold()
            )


if __name__ == "__main__":
    unittest.main()
