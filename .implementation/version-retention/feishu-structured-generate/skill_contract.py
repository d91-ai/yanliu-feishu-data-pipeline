from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SkillContract:
    root: Path
    manifest_path: Path
    contract_version: int
    schema_version: int
    prompt_path: Path
    claim_schema_path: Path
    viewpoints_schema_path: Path
    generate_script: Path
    security_master_path: Path
    security_master_cli_flag: str
    runtime_files: tuple[Path, ...]

    @property
    def prompt(self) -> str:
        return self.prompt_path.read_text(encoding="utf-8").strip()

    def sha256(self, path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @property
    def runtime_sha256(self) -> str:
        digest = hashlib.sha256()
        for path in self.runtime_files:
            relative = path.relative_to(self.root).as_posix()
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
            digest.update(b"\n")
        return digest.hexdigest()


def _required_text(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise RuntimeError(f"Skill contract missing {label}")
    return text


def _safe_child(root: Path, relative_value: Any, label: str) -> Path:
    relative = Path(_required_text(relative_value, label))
    if relative.is_absolute() or ".." in relative.parts:
        raise RuntimeError(f"Skill contract has unsafe {label}")
    path = (root / relative).resolve()
    if root.resolve() not in path.parents:
        raise RuntimeError(f"Skill contract {label} escapes Skill root")
    if not path.is_file():
        raise RuntimeError(f"Skill contract file not found: {path}")
    return path


def load_skill_contract(skill_script: Path) -> SkillContract:
    skill_script = skill_script.expanduser().resolve()
    if not skill_script.is_file():
        raise RuntimeError(f"Skill script not found: {skill_script}")
    root = skill_script.parent.parent
    manifest_path = root / "contract" / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Invalid Skill contract manifest: {manifest_path}") from exc
    if not isinstance(manifest, dict):
        raise RuntimeError("Skill contract manifest must contain an object")
    try:
        contract_version = int(manifest.get("contract_version"))
        schema_version = int(manifest.get("schema_version"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Skill contract versions must be integers") from exc
    if contract_version <= 0 or schema_version <= 0:
        raise RuntimeError("Skill contract versions must be positive")
    contract_root = manifest_path.parent
    prompt_path = _safe_child(contract_root, manifest.get("semantic_prompt"), "semantic_prompt")
    claim_schema_path = _safe_child(
        contract_root, manifest.get("claim_units_schema"), "claim_units_schema"
    )
    viewpoints_schema_path = _safe_child(
        contract_root, manifest.get("viewpoints_schema"), "viewpoints_schema"
    )
    try:
        claim_schema = json.loads(claim_schema_path.read_text(encoding="utf-8"))
        viewpoints_schema = json.loads(viewpoints_schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("Invalid Skill JSON schema") from exc
    if not isinstance(claim_schema, dict) or not isinstance(viewpoints_schema, dict):
        raise RuntimeError("Skill JSON schemas must contain objects")
    entrypoints = manifest.get("entrypoints")
    if not isinstance(entrypoints, dict):
        raise RuntimeError("Skill contract missing entrypoints")
    generate_script = _safe_child(root, entrypoints.get("generate_table"), "generate_table entrypoint")
    if generate_script != skill_script:
        raise RuntimeError("Configured Skill script does not match contract generate_table entrypoint")
    security_master = manifest.get("security_master")
    if not isinstance(security_master, dict):
        raise RuntimeError("Skill contract missing security_master")
    security_master_cli_flag = _required_text(
        security_master.get("cli_flag"), "security_master.cli_flag"
    )
    if security_master_cli_flag != "--security-master":
        raise RuntimeError("Unsupported security_master.cli_flag")
    security_master_path = _safe_child(
        root, security_master.get("default_path"), "security_master.default_path"
    )
    runtime_values = manifest.get("runtime_paths")
    if not isinstance(runtime_values, list) or not runtime_values:
        raise RuntimeError("Skill contract missing runtime_paths")
    runtime_files = tuple(
        sorted(
            (_safe_child(root, value, "runtime_paths entry") for value in runtime_values),
            key=lambda path: path.relative_to(root).as_posix(),
        )
    )
    if len(set(runtime_files)) != len(runtime_files):
        raise RuntimeError("Skill contract runtime_paths contains duplicates")
    return SkillContract(
        root=root,
        manifest_path=manifest_path,
        contract_version=contract_version,
        schema_version=schema_version,
        prompt_path=prompt_path,
        claim_schema_path=claim_schema_path,
        viewpoints_schema_path=viewpoints_schema_path,
        generate_script=generate_script,
        security_master_path=security_master_path,
        security_master_cli_flag=security_master_cli_flag,
        runtime_files=runtime_files,
    )
