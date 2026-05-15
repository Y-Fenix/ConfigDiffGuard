from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .parsers import SUPPORTED_EXTENSIONS
from .rules import RuleSet, matches_pattern


@dataclass(frozen=True)
class SourceConfig:
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    formats: set[str] = field(default_factory=set)
    normalize_from: list[str] = field(default_factory=list)
    studio_name: str = ""
    project_name: str = ""


def load_source_config(tool_dir: Path, org_id: str, project_name: str) -> SourceConfig | None:
    path = tool_dir / "studio_config_sources.json"
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    studio = _find_studio(payload, org_id)
    if not studio:
        return None
    default_sources = [item for item in studio.get("config_sources", []) if isinstance(item, dict)]
    default_exclude = _merged_list(source.get("exclude", []) for source in default_sources)
    default_normalize = _merged_list(source.get("normalize_from", "") for source in default_sources)
    overrides = studio.get("project_overrides", {})
    override = overrides.get(project_name) if isinstance(overrides, dict) else None
    if isinstance(override, dict):
        include = _list(override.get("include", []))
        exclude = _list(override.get("exclude", [])) or default_exclude
        formats = set(_list(override.get("formats", [])))
        normalize_from = _list(override.get("normalize_from", [])) or default_normalize
    else:
        include = _merged_list(source.get("include", []) for source in default_sources)
        exclude = default_exclude
        formats = set(_merged_list(source.get("formats", []) for source in default_sources))
        normalize_from = default_normalize
    if not include and not formats:
        return None
    return SourceConfig(
        include=include,
        exclude=exclude,
        formats={item.lower().lstrip(".") for item in formats},
        normalize_from=normalize_from,
        studio_name=str(studio.get("name") or ""),
        project_name=project_name,
    )


def source_included(path: str, rules: RuleSet, source_config: SourceConfig | None = None) -> bool:
    suffix = Path(path).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        return False
    if source_config:
        if source_config.formats and suffix.lstrip(".") not in source_config.formats:
            return False
        if source_config.include and not any(matches_pattern(path, pattern) for pattern in source_config.include):
            return False
        return not any(matches_pattern(path, pattern) for pattern in source_config.exclude)
    if not any(matches_pattern(path, pattern) for pattern in rules.include):
        return False
    return not any(matches_pattern(path, pattern) for pattern in rules.exclude)


def normalize_source_path(path: str, source_config: SourceConfig | None = None) -> str:
    parts = [part for part in Path(path).as_posix().split("/") if part and part != "."]
    tokens = source_config.normalize_from if source_config else ["LevelData"]
    for token in tokens:
        if not token:
            continue
        token_parts = [part for part in str(token).split("/") if part]
        index = _subsequence_index(parts, token_parts)
        if index >= 0:
            return "/".join(parts[index:])
    return "/".join(parts)


def source_tree_roots(source_config: SourceConfig | None, project_name: str) -> list[str]:
    if not source_config:
        return [
            "LevelData",
            f"{project_name}/LevelData",
            "Assets/LevelData",
            f"{project_name}/Assets/LevelData",
            "Assets/Resources/LevelData",
            f"{project_name}/Assets/Resources/LevelData",
        ]
    roots: list[str] = []
    for pattern in source_config.include:
        root = _static_root(pattern)
        if root and root not in roots:
            roots.append(root)
    return roots


def source_key(path: str, source_config: SourceConfig | None = None) -> str:
    return normalize_source_path(path, source_config)


def _find_studio(payload: dict[str, Any], org_id: str) -> dict[str, Any] | None:
    studios = payload.get("studios", [])
    if not isinstance(studios, list):
        return None
    for studio in studios:
        if isinstance(studio, dict) and str(studio.get("org_id") or "") == org_id:
            return studio
    default_studio = str(payload.get("default_studio") or "")
    for studio in studios:
        if isinstance(studio, dict) and str(studio.get("name") or "") == default_studio:
            return studio
    return None


def _list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    return []


def _merged_list(values: Any) -> list[str]:
    merged: list[str] = []
    for value in values if not isinstance(values, (str, list)) else [values]:
        for item in _list(value):
            if item not in merged:
                merged.append(item)
    return merged


def _subsequence_index(parts: list[str], token_parts: list[str]) -> int:
    if not token_parts:
        return -1
    for index in range(0, len(parts) - len(token_parts) + 1):
        if parts[index:index + len(token_parts)] == token_parts:
            return index
    return -1


def _static_root(pattern: str) -> str:
    parts: list[str] = []
    for part in Path(pattern).as_posix().split("/"):
        if not part or any(char in part for char in "*?["):
            break
        parts.append(part)
    return "/".join(parts)
