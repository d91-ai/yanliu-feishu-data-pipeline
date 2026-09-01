#!/usr/bin/env python3
"""Re-export source-grounded legacy semantic candidates through schema-v6 Skill.

This is a local-only recovery path.  It never calls Feishu.  A legacy row is
kept only when its current reviewed source is byte-identical to the semantic
job source and the selected source line literally names the target (or code).
Unsupported horizon, position and condition inferences are deliberately reset
instead of being carried into the stricter schema-v6 contract.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from datetime import datetime
from pathlib import Path


SKILL_SCRIPT: Path | None = None
SEMANTIC_DONE: Path | None = None
ALLOWED_DIRECTIONS = {"看多", "看空", "关注", "中性", "信息不足"}
ALLOWED_MARKETS = {"A股", "港股", "美股", "其他"}


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compact(value: str) -> str:
    return re.sub(r"\s+", "", value or "").casefold()


def infer_source_alias(target_name: str, stock_code: str, source_text: str) -> str:
    candidates = [target_name]
    normalized = target_name.replace("Ａ", "A")
    candidates.extend(
        [
            normalized,
            re.sub(r"(?:股份有限公司|有限公司|股份|集团|公司)$", "", normalized),
            re.sub(r"(?:[-－]?[A-ZＡ-Ｚ]|[-－]U)$", "", normalized, flags=re.IGNORECASE),
        ]
    )
    present = [value for value in candidates if len(compact(value)) >= 2 and compact(value) in compact(source_text)]
    if present:
        return max(present, key=lambda value: len(compact(value)))
    return stock_code


def source_fragments(markdown: str) -> dict[str, str]:
    fragments: dict[str, str] = {}
    in_frontmatter = False
    for number, raw in enumerate(markdown.splitlines(), start=1):
        line = raw.strip()
        if line == "---":
            in_frontmatter = not in_frontmatter
            continue
        if line and not in_frontmatter and not line.startswith("#"):
            fragments[f"L{number}"] = line
    return fragments


def source_speakers(markdown: str) -> dict[str, str]:
    speakers: dict[str, str] = {}
    presenter = ""
    in_frontmatter = False
    for number, raw in enumerate(markdown.splitlines(), start=1):
        line = raw.strip()
        if line == "---":
            in_frontmatter = not in_frontmatter
            continue
        if in_frontmatter:
            continue
        if line.startswith("### ") and not line.startswith("#### "):
            presenter = line[4:].strip()
        elif line and not line.startswith("#"):
            speakers[f"L{number}"] = presenter
    return speakers


def choose_fragment(row: dict, fragments: dict[str, str]) -> tuple[str, str] | None:
    target = compact(str(row.get("target_name") or ""))
    code = compact(str(row.get("stock_code") or ""))
    if code in {"", "原文未提供", "待确认"}:
        code = ""
    candidates: list[tuple[int, str, str]] = []
    evidence = compact(str(row.get("evidence") or "").strip("“”\""))
    for ref, text in fragments.items():
        normalized = compact(text)
        if not ((target and target in normalized) or (code and code in normalized)):
            continue
        score = 0
        if target and target in normalized:
            score += 100
        if code and code in normalized:
            score += 50
        if evidence:
            probe = evidence[: min(24, len(evidence))]
            if probe and probe in normalized:
                score += 25
        candidates.append((score, ref, text))
    if not candidates:
        return None
    _, ref, text = max(candidates, key=lambda item: (item[0], -int(item[1][1:])))
    return ref, text


def best_matching_job(record_id: str, source_path: Path) -> Path | None:
    expected_hash = sha256(source_path)
    matches: list[Path] = []
    for job in SEMANTIC_DONE.glob(f"{record_id}-*"):
        old_source = job / "source.md"
        rows = job / "semantic_rows.json"
        if old_source.exists() and rows.exists() and sha256(old_source) == expected_hash:
            matches.append(job)
    return max(matches, key=lambda path: (path / "semantic_rows.json").stat().st_mtime) if matches else None


def build_claim_units(entry: dict, semantic_rows: list[dict]) -> tuple[list[dict], list[dict]]:
    source_path = Path(entry["source_path"])
    source_markdown = source_path.read_text(encoding="utf-8")
    fragments = source_fragments(source_markdown)
    speakers = source_speakers(source_markdown)
    claims: list[dict] = []
    omitted: list[dict] = []
    seen_judgments: set[tuple[str, str, str, str, str]] = set()
    for index, row in enumerate(semantic_rows, start=1):
        selected = choose_fragment(row, fragments)
        if selected is None:
            omitted.append(
                {
                    "legacy_row": index,
                    "target_name": row.get("target_name", ""),
                    "reason": "current source has no fragment literally naming target or stock code",
                }
            )
            continue
        ref, text = selected
        target_name = str(row.get("target_name") or "").strip()
        stock_code = str(row.get("stock_code") or "").strip() or "原文未提供"
        market = str(row.get("market") or "其他").strip()
        direction = str(row.get("direction") or "信息不足").strip()
        presenter = str(row.get("presenter") or row.get("presenter_normalized") or "待确认").strip()
        presenter_normalized = str(row.get("presenter_normalized") or presenter).strip()
        owned_presenter = speakers.get(ref) or presenter_normalized
        normalized_direction = direction if direction in ALLOWED_DIRECTIONS else "信息不足"
        target: dict[str, str] = {
            "target_name": target_name,
            "stock_code": stock_code,
            "market": market if market in ALLOWED_MARKETS else "其他",
        }
        if target_name and compact(target_name) not in compact(text) and compact(stock_code) in compact(text):
            target["source_alias"] = infer_source_alias(target_name, stock_code, text)
        target_identity = stock_code.upper() if stock_code != "原文未提供" else compact(target_name)
        judgment_key = (owned_presenter, target_identity, normalized_direction, ref, compact(text))
        if judgment_key in seen_judgments:
            omitted.append(
                {
                    "legacy_row": index,
                    "target_name": target_name,
                    "reason": "duplicate speaker-target-judgment candidate on the same source fragment",
                }
            )
            continue
        seen_judgments.add(judgment_key)
        claims.append(
            {
                "claim_ref": f"legacy-grounded-{index}-{ref}",
                "source_refs": [ref],
                "presenter": presenter,
                "presenter_normalized": presenter_normalized,
                "direction": normalized_direction,
                "time_horizon": "未说明",
                "position": {"state": "信息不足", "detail": "", "plan": "无"},
                "conditions": [],
                "targets": [target],
            }
        )
    return claims, omitted


def export_entry(entry: dict, generated_at: str, force_migrated: bool) -> dict:
    job_dir = Path(entry["job_dir"])
    result_path = job_dir / "generation_result.json"
    if result_path.exists():
        existing = load_json(result_path)
        if not (force_migrated and existing.get("worker") == "grounded-semantic-migration"):
            return {"source_record_id": entry["source_record_id"], "status": "existing"}
    semantic_job = best_matching_job(entry["source_record_id"], Path(entry["source_path"]))
    if semantic_job is None:
        return {"source_record_id": entry["source_record_id"], "status": "no_matching_semantic_job"}
    semantic_rows = load_json(semantic_job / "semantic_rows.json")
    claims, omitted = build_claim_units(entry, semantic_rows)
    if not claims:
        return {
            "source_record_id": entry["source_record_id"],
            "status": "no_grounded_claims",
            "legacy_row_count": len(semantic_rows),
            "omitted": omitted,
        }
    claim_path = job_dir / "claim_units.json"
    provenance_path = job_dir / "claim_units_provenance.json"
    output_path = Path(entry["output_path"])
    write_json(claim_path, claims)
    write_json(
        provenance_path,
        {
            "mode": "legacy_semantic_candidates_revalidated_against_current_reviewed_source",
            "semantic_job": str(semantic_job),
            "legacy_row_count": len(semantic_rows),
            "grounded_claim_count": len(claims),
            "omitted_count": len(omitted),
            "omitted": omitted,
            "horizon_position_conditions_reset": True,
        },
    )
    command = [
        "python3",
        str(SKILL_SCRIPT),
        "--claim-units",
        str(claim_path),
        "--meeting-markdown",
        entry["source_path"],
        "--output",
        str(output_path),
        "--schema-version",
        "6",
        "--source-record-id",
        entry["source_record_id"],
        "--meeting-uid",
        entry["meeting_uid"],
        "--source-archive-url",
        entry["source_archive_url"],
        "--source-file-name",
        entry["source_name"] + ("" if entry["source_name"].endswith(".md") else ".md"),
        "--meeting-date",
        entry["meeting_date"],
        "--model-version",
        "legacy-semantic-grounded-v6",
        "--generated-at",
        generated_at,
    ]
    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode:
        return {
            "source_record_id": entry["source_record_id"],
            "status": "export_failed",
            "error": completed.stderr.strip() or completed.stdout.strip(),
        }
    output_text = output_path.read_text(encoding="utf-8")
    row_count = len(re.findall(r"^## 观点 \d+$", output_text, flags=re.MULTILINE))
    condition_count = len(re.findall(r"^\| (?!无 \| 无)(.+) \| (?:价格/估值|业绩/基本面|产业供需/价格|产品/技术|政策/事件|市场/流动性|资金/筹码|交易/仓位|未分类) \|$", output_text, flags=re.MULTILINE))
    result = {
        "source_record_id": entry["source_record_id"],
        "row_count": row_count,
        "output_sha256": sha256(output_path),
        "output_size": output_path.stat().st_size,
        "schema_version": 6,
        "condition_count": condition_count,
        "model_version": "legacy-semantic-grounded-v6",
        "skill_script_sha256": sha256(SKILL_SCRIPT),
        "worker": "grounded-semantic-migration",
        "completed_at": generated_at,
        "legacy_row_count": len(semantic_rows),
        "omitted_ungrounded_count": len(omitted),
    }
    write_json(result_path, result)
    (job_dir / "generation_error.txt").unlink(missing_ok=True)
    return {"status": "generated", **result}


def main() -> int:
    global SKILL_SCRIPT, SEMANTIC_DONE
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--skill-script", type=Path, required=True)
    parser.add_argument("--semantic-jobs", type=Path, required=True)
    parser.add_argument("--apply-local", action="store_true")
    parser.add_argument("--force-migrated", action="store_true")
    args = parser.parse_args()
    SKILL_SCRIPT = args.skill_script.expanduser().resolve()
    SEMANTIC_DONE = args.semantic_jobs.expanduser().resolve()
    if not args.apply_local:
        raise SystemExit("local artifact writes require --apply-local")
    manifest = load_json(Path(args.manifest))
    generated_at = datetime.now().astimezone().replace(microsecond=0).isoformat()
    results = [
        export_entry(entry, generated_at, args.force_migrated)
        for entry in manifest["entries"]
    ]
    summary = {
        "generated_at": generated_at,
        "results": results,
        "status_counts": {
            status: sum(item["status"] == status for item in results)
            for status in sorted({item["status"] for item in results})
        },
    }
    summary_path = Path(args.manifest).with_name("grounded_semantic_migration_summary.json")
    write_json(summary_path, summary)
    print(json.dumps(summary["status_counts"], ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
