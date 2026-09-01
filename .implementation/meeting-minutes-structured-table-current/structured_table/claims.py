"""Claim-unit and review-row normalization rules."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from typing import Any

from .common import clean_cell, clean_evidence_text, normalize_date
from .contract import (
    CONDITION_TYPE_VALUES,
    DIRECTION_VALUES,
    MARKET_VALUES,
    POSITION_CONTEXT_DEFAULT,
    POSITION_PLAN_VALUES,
    POSITION_STATE_VALUES,
    SOURCE_CODE_NOT_PROVIDED,
    TIME_HORIZON_VALUES,
    VIEWPOINT_FIELDS,
)
from .source_document import locate_semantic_evidence
from .security_master import (
    SecurityMaster,
    resolved_review_identity,
    resolved_target_identity,
)
from .speaker_master import SpeakerMaster


def required_text(row: dict[str, Any], field: str, index: int) -> str:
    value = clean_cell(row.get(field))
    if not value:
        raise SystemExit(f"row {index}: {field} is required")
    return value


def warn_fallback(scope: str, field: str, value: Any, fallback: str) -> None:
    print(
        f"warning: {scope}: invalid {field} {clean_cell(value)!r}; using {fallback}",
        file=sys.stderr,
    )


def claim_targets(row: dict[str, Any], index: int) -> list[dict[str, Any]]:
    targets = row.get("targets")
    if not isinstance(targets, list) or not targets:
        raise SystemExit(f"claim unit {index}: targets must be a non-empty array")
    normalized: list[dict[str, Any]] = []
    for target_index, target in enumerate(targets, start=1):
        if not isinstance(target, dict):
            print(
                f"warning: claim unit {index}: targets[{target_index}] must be an object; skipped",
                file=sys.stderr,
            )
            continue
        name = clean_cell(target.get("target_name"))
        if not name:
            print(
                f"warning: claim unit {index}: targets[{target_index}].target_name is required; skipped",
                file=sys.stderr,
            )
            continue
        market = clean_cell(target.get("market"))
        if market not in MARKET_VALUES:
            warn_fallback(
                f"claim unit {index} target {target_index}", "market", market, "其他"
            )
            market = "其他"
        item = {
            "target_name": name,
            "stock_code": SOURCE_CODE_NOT_PROVIDED,
            "market": market,
            "position": target.get("position"),
        }
        normalized.append(item)
    if not normalized:
        raise SystemExit(f"claim unit {index}: no valid targets")
    return normalized


def stable_target_key(target_name: str, stock_code: str, market: str) -> str:
    if stock_code != SOURCE_CODE_NOT_PROVIDED:
        return f"{market}:code:{stock_code.upper()}"
    normalized_name = re.sub(r"\s+", "", target_name).casefold()
    return f"{market}:name:{normalized_name}"


def claim_source_evidence(
    row: dict[str, Any], index: int, meeting_markdown: str
) -> list[dict[str, str]]:
    raw_quotes = row.get("source_quotes")
    if not isinstance(raw_quotes, list) or not raw_quotes:
        raise SystemExit(f"claim unit {index}: source_quotes must be a non-empty array")
    quotes: list[str] = []
    for value in raw_quotes:
        quote = clean_evidence_text(value)
        if quote:
            quotes.append(quote)
    if not quotes:
        raise SystemExit(f"claim unit {index}: source_quotes must contain text")
    return [
        {"text": quote, "locator": locate_semantic_evidence(meeting_markdown, quote)}
        for quote in quotes
    ]


def normalize_horizon(row: dict[str, Any], index: int) -> str:
    horizon = clean_cell(row.get("time_horizon"))
    if horizon in TIME_HORIZON_VALUES:
        return horizon
    warn_fallback(f"claim unit {index}", "time_horizon", horizon, "未说明")
    return "未说明"


def normalize_position(row: dict[str, Any], index: int) -> str:
    position = row.get("position")
    if position is None:
        return POSITION_CONTEXT_DEFAULT
    if not isinstance(position, dict):
        warn_fallback(
            f"claim unit {index} target", "position", position, POSITION_CONTEXT_DEFAULT
        )
        return POSITION_CONTEXT_DEFAULT
    state = clean_cell(position.get("state")) or POSITION_CONTEXT_DEFAULT
    detail = clean_cell(position.get("detail"))
    plan = clean_cell(position.get("plan")) or "无"
    if state not in POSITION_STATE_VALUES:
        warn_fallback(
            f"claim unit {index} target",
            "position.state",
            state,
            POSITION_CONTEXT_DEFAULT,
        )
        state = POSITION_CONTEXT_DEFAULT
    if plan not in POSITION_PLAN_VALUES:
        warn_fallback(f"claim unit {index} target", "position.plan", plan, "无")
        plan = "无"
    context = f"{state}（{detail}）" if detail else state
    return f"{context}；{plan}" if plan != "无" else context


def normalize_conditions(row: dict[str, Any], index: int) -> list[dict[str, Any]]:
    conditions = row.get("conditions")
    if conditions is None:
        conditions = []
    if not isinstance(conditions, list):
        warn_fallback(f"claim unit {index}", "conditions", conditions, "[]")
        return []
    normalized: list[dict[str, Any]] = []
    for condition in conditions:
        if not isinstance(condition, dict):
            continue
        text = clean_cell(condition.get("text"))
        if not text:
            continue
        types = condition.get("types")
        if not isinstance(types, list):
            types = []
        normalized_types: list[str] = []
        for raw_type in types:
            value = clean_cell(raw_type)
            if value not in CONDITION_TYPE_VALUES:
                warn_fallback(f"claim unit {index}", "condition type", value, "未分类")
                value = "未分类"
            if value not in normalized_types:
                normalized_types.append(value)
        if not normalized_types:
            normalized_types.append("未分类")
        normalized.append({"text": text, "types": normalized_types})
    return normalized


def normalize_source_evidence(row: dict[str, Any], index: int) -> list[dict[str, str]]:
    raw_evidence = row.get("source_evidence")
    if not isinstance(raw_evidence, list) or not raw_evidence:
        raise SystemExit(f"row {index}: source_evidence is required")
    evidence: list[dict[str, str]] = []
    for item in raw_evidence:
        if not isinstance(item, dict):
            continue
        text = clean_evidence_text(item.get("text"))
        if not text:
            continue
        locator = clean_cell(item.get("locator"))
        if not locator:
            locator = "Q-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
        evidence.append({"text": text, "locator": locator})
    if not evidence:
        raise SystemExit(f"row {index}: source_evidence must contain text")
    return evidence


def assign_viewpoint_ids(
    rows: list[dict[str, Any]], meeting_id: str
) -> list[dict[str, Any]]:
    """Assign content-derived IDs without dropping exact or near duplicates."""

    counts: dict[str, int] = {}
    output: list[dict[str, Any]] = []
    for row in rows:
        content = {field: row[field] for field in VIEWPOINT_FIELDS if field != "viewpoint_id"}
        canonical = json.dumps(
            [meeting_id, content], ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        content_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        counts[content_hash] = counts.get(content_hash, 0) + 1
        identity = f"{content_hash}:{counts[content_hash]}"
        normalized = {**row, "viewpoint_id": "vp-" + hashlib.sha256(identity.encode()).hexdigest()[:32]}
        output.append({field: normalized[field] for field in VIEWPOINT_FIELDS})
    return output


def normalize_claim_units(
    units: list[dict[str, Any]],
    *,
    meeting_date: str,
    meeting_markdown: str,
    meeting_id: str,
    security_master: SecurityMaster | None = None,
    speaker_master: SpeakerMaster | None = None,
) -> list[dict[str, Any]]:
    if not clean_cell(meeting_id):
        raise SystemExit("meeting_id is required")
    rows: list[dict[str, Any]] = []
    for index, unit in enumerate(units, start=1):
        row = dict(unit)
        source_evidence = claim_source_evidence(row, index, meeting_markdown)
        presenter = required_text(row, "presenter", index)
        presenter_normalized = (
            speaker_master.resolve(presenter) if speaker_master is not None else presenter
        )
        normalized_meeting_date = normalize_date(meeting_date)
        viewpoint_date = normalized_meeting_date
        direction = clean_cell(row.get("direction"))
        if direction not in DIRECTION_VALUES:
            warn_fallback(f"claim unit {index}", "direction", direction, "信息不足")
            direction = "信息不足"
        targets = claim_targets(row, index)
        horizon = normalize_horizon(row, index)
        conditions = normalize_conditions(row, index)
        for target in targets:
            identity = resolved_target_identity(
                target_name=target["target_name"],
                market=target["market"],
                security_master=security_master,
            )
            target["target_name"] = identity.target_name
            target["stock_code"] = identity.stock_code
            target["target_key"] = stable_target_key(
                target["target_name"], target["stock_code"], target["market"]
            )
            normalized = {
                "meeting_date": normalized_meeting_date,
                "viewpoint_date": viewpoint_date,
                "target_key": target["target_key"],
                "target_name": target["target_name"],
                "stock_code": target["stock_code"],
                "market": target["market"],
                "presenter": presenter,
                "presenter_normalized": presenter_normalized,
                "direction": direction,
                "time_horizon": horizon,
                "position_context": normalize_position(target, index),
                "conditions": conditions,
                "source_evidence": source_evidence,
            }
            rows.append(normalized)
    return assign_viewpoint_ids(rows, meeting_id)


def normalize_position_context(value: Any) -> str:
    return clean_cell(value) or POSITION_CONTEXT_DEFAULT


def normalize_review_rows(
    rows: list[dict[str, Any]],
    *,
    meeting_date: str,
    meeting_id: str,
    security_master: SecurityMaster | None = None,
    row_number_start: int = 1,
) -> list[dict[str, Any]]:
    if not clean_cell(meeting_id):
        raise SystemExit("meeting_id is required")
    normalized: list[dict[str, Any]] = []
    for index, source in enumerate(rows, start=row_number_start):
        row = dict(source)
        target_name = required_text(row, "target_name", index)
        market = clean_cell(row.get("market"))
        if market not in MARKET_VALUES:
            warn_fallback(f"structured row {index}", "market", market, "其他")
            market = "其他"
        source_evidence = normalize_source_evidence(row, index)
        identity = resolved_review_identity(
            target_name=target_name,
            stock_code=clean_cell(row.get("stock_code")),
            market=market,
            security_master=security_master,
            scope=f"structured row {index}",
        )
        target_name = identity.target_name
        stock_code = identity.stock_code
        conditions = row.get("conditions", [])
        if not isinstance(conditions, list):
            conditions = []
        normalized_conditions: list[dict[str, Any]] = []
        for condition in conditions:
            if not isinstance(condition, dict):
                continue
            text = clean_cell(condition.get("text"))
            if not text:
                continue
            raw_types = condition.get("types", [])
            types = raw_types if isinstance(raw_types, list) else []
            normalized_types: list[str] = []
            for raw_type in types:
                value = clean_cell(raw_type)
                if value not in CONDITION_TYPE_VALUES:
                    warn_fallback(
                        f"structured row {index}", "condition type", value, "未分类"
                    )
                    value = "未分类"
                if value not in normalized_types:
                    normalized_types.append(value)
            normalized_conditions.append(
                {"text": text, "types": normalized_types or ["未分类"]}
            )
        normalized_meeting_date = normalize_date(row.get("meeting_date") or meeting_date)
        viewpoint_date = normalize_date(row.get("viewpoint_date") or meeting_date)
        presenter = clean_cell(row.get("presenter")) or "发言人待确认"
        presenter_normalized = clean_cell(row.get("presenter_normalized")) or presenter
        direction = clean_cell(row.get("direction"))
        if direction not in DIRECTION_VALUES:
            warn_fallback(f"structured row {index}", "direction", direction, "信息不足")
            direction = "信息不足"
        time_horizon = clean_cell(row.get("time_horizon"))
        if time_horizon not in TIME_HORIZON_VALUES:
            warn_fallback(
                f"structured row {index}", "time_horizon", time_horizon, "未说明"
            )
            time_horizon = "未说明"
        normalized_row = {
            "meeting_date": normalized_meeting_date,
            "viewpoint_date": viewpoint_date,
            "target_key": stable_target_key(target_name, stock_code, market),
            "target_name": target_name,
            "stock_code": stock_code,
            "market": market,
            "presenter": presenter,
            "presenter_normalized": presenter_normalized,
            "direction": direction,
            "time_horizon": time_horizon,
            "position_context": normalize_position_context(row.get("position_context")),
            "conditions": normalized_conditions,
            "source_evidence": source_evidence,
        }
        normalized.append(normalized_row)
    return assign_viewpoint_ids(normalized, meeting_id)
