"""Deterministic, fail-closed security-name to stock-code resolution."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from hashlib import sha256
import io
from pathlib import Path
import re
import sys
import unicodedata

from .common import clean_cell
from .contract import MARKET_VALUES, SOURCE_CODE_NOT_PROVIDED


REQUIRED_COLUMNS = {"target_name", "stock_code", "market", "aliases"}


def normalized_lookup_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", clean_cell(value)).casefold()
    return re.sub(r"\s+", "", text)


def is_single_stock_code(value: str) -> bool:
    return bool(value) and not re.search(r"[\s/|]", value)


@dataclass(frozen=True)
class SecurityIdentity:
    target_name: str
    stock_code: str
    market: str


@dataclass(frozen=True)
class SecurityMaster:
    identities_by_alias: dict[tuple[str, str], frozenset[SecurityIdentity]]
    identities_by_code: dict[tuple[str, str], frozenset[SecurityIdentity]]
    snapshot_version: str

    @classmethod
    def from_csv(cls, path: Path) -> "SecurityMaster":
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise SystemExit(f"security master cannot be read: {path}: {exc}") from None
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise SystemExit(f"security master must be UTF-8 CSV: {path}") from None
        reader = csv.DictReader(io.StringIO(text, newline=""))
        fields = set(reader.fieldnames or [])
        missing = sorted(REQUIRED_COLUMNS - fields)
        if missing:
            raise SystemExit(f"security master missing columns: {', '.join(missing)}")
        mutable: dict[tuple[str, str], set[SecurityIdentity]] = {}
        mutable_codes: dict[tuple[str, str], set[SecurityIdentity]] = {}
        for row_number, row in enumerate(reader, start=2):
            name = clean_cell(row.get("target_name"))
            code = re.sub(r"\s+", " ", str(row.get("stock_code") or "")).strip().upper()
            market = clean_cell(row.get("market"))
            if not name or market not in MARKET_VALUES or not is_single_stock_code(code):
                print(
                    f"warning: security master row {row_number} is invalid; skipped",
                    file=sys.stderr,
                )
                continue
            identity = SecurityIdentity(name, code, market)
            mutable_codes.setdefault((market, code), set()).add(identity)
            aliases = [name, *str(row.get("aliases") or "").split("|")]
            for alias in aliases:
                normalized = normalized_lookup_text(alias)
                if normalized:
                    mutable.setdefault((market, normalized), set()).add(identity)
        return cls(
            {key: frozenset(values) for key, values in mutable.items()},
            {key: frozenset(values) for key, values in mutable_codes.items()},
            f"sha256:{sha256(raw).hexdigest()}",
        )

    def resolve_identity(self, *, target_name: str, market: str) -> SecurityIdentity | None:
        normalized = normalized_lookup_text(target_name)
        candidates = set(self.identities_by_alias.get((market, normalized), ()))
        if len(candidates) == 1:
            return next(iter(candidates))
        return None

    def resolve(self, *, target_name: str, market: str) -> str:
        identity = self.resolve_identity(target_name=target_name, market=market)
        if identity is not None:
            return identity.stock_code
        return SOURCE_CODE_NOT_PROVIDED

    def resolve_code_identity(self, *, stock_code: str, market: str) -> SecurityIdentity | None:
        code = re.sub(r"\s+", "", clean_cell(stock_code)).upper()
        candidates = set(self.identities_by_code.get((market, code), ()))
        if len(candidates) == 1:
            return next(iter(candidates))
        return None


def resolved_target_identity(
    *,
    target_name: str,
    market: str,
    security_master: SecurityMaster | None,
) -> SecurityIdentity:
    if security_master is None:
        return SecurityIdentity(target_name, SOURCE_CODE_NOT_PROVIDED, market)
    identity = security_master.resolve_identity(
        target_name=target_name,
        market=market,
    )
    return identity or SecurityIdentity(target_name, SOURCE_CODE_NOT_PROVIDED, market)


def resolved_review_identity(
    *,
    target_name: str,
    stock_code: str,
    market: str,
    security_master: SecurityMaster | None,
    scope: str,
) -> SecurityIdentity:
    """Preserve current Markdown identity and use the master only for warnings."""

    reviewed_code = re.sub(r"\s+", "", clean_cell(stock_code)).upper()
    if reviewed_code == SOURCE_CODE_NOT_PROVIDED:
        return SecurityIdentity(target_name, SOURCE_CODE_NOT_PROVIDED, market)
    if reviewed_code:
        identity = (
            security_master.resolve_code_identity(stock_code=reviewed_code, market=market)
            if security_master is not None
            else None
        )
        if identity is None:
            print(
                f"warning: {scope}: stock_code {reviewed_code!r} cannot be uniquely verified "
                "by the local master; preserving the current Markdown value",
                file=sys.stderr,
            )
            return SecurityIdentity(target_name, reviewed_code, market)
        name_identity = security_master.resolve_identity(target_name=target_name, market=market)
        if name_identity is None:
            print(
                f"warning: {scope}: target_name {target_name!r} cannot be verified with "
                f"stock_code {reviewed_code!r}; preserving the current Markdown values",
                file=sys.stderr,
            )
        elif name_identity != identity:
            print(
                f"warning: {scope}: name/code conflict identifies different securities; "
                "preserving the current Markdown values",
                file=sys.stderr,
            )
        return SecurityIdentity(target_name, reviewed_code, market)
    return resolved_target_identity(
        target_name=target_name,
        market=market,
        security_master=security_master,
    )
