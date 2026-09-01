"""Optional exact-match speaker normalization from a reviewed CSV."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import unicodedata


REQUIRED_COLUMNS = {
    "presenter_id",
    "canonical_name",
    "aliases",
    "status",
}


def normalized_alias(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip().casefold()


@dataclass(frozen=True)
class SpeakerMaster:
    identities_by_alias: dict[str, tuple[tuple[str, str], ...]]
    warnings: tuple[str, ...]

    @classmethod
    def from_csv(cls, path: Path) -> "SpeakerMaster":
        try:
            with path.open(encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
                if missing:
                    raise ValueError(
                        "speaker master missing columns: " + ", ".join(sorted(missing))
                    )
                candidates: dict[str, set[tuple[str, str]]] = {}
                warnings: list[str] = []
                for line_number, row in enumerate(reader, start=2):
                    if normalized_alias(row.get("status")) != "confirmed":
                        continue
                    presenter_id = str(row.get("presenter_id") or "").strip()
                    canonical_name = str(row.get("canonical_name") or "").strip()
                    aliases = [
                        alias.strip()
                        for alias in str(row.get("aliases") or "").split("|")
                        if alias.strip()
                    ]
                    if not presenter_id or not canonical_name or not aliases:
                        warnings.append(
                            f"speaker master row {line_number}: incomplete confirmed row; skipped"
                        )
                        continue
                    for alias in [canonical_name, *aliases]:
                        key = normalized_alias(alias)
                        if key:
                            candidates.setdefault(key, set()).add(
                                (presenter_id, canonical_name)
                            )
        except (OSError, UnicodeDecodeError, csv.Error) as exc:
            raise ValueError(f"speaker master unavailable: {path}") from exc
        return cls(
            identities_by_alias={
                alias: tuple(sorted(values)) for alias, values in candidates.items()
            },
            warnings=tuple(warnings),
        )

    def resolve(self, presenter: str) -> str:
        candidates = self.identities_by_alias.get(normalized_alias(presenter), ())
        if len(candidates) != 1:
            return presenter
        return candidates[0][1]
