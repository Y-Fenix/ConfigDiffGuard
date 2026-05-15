from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .parsers import SUPPORTED_EXTENSIONS
from .rules import RuleSet, matches_pattern
from .source_config import SourceConfig, normalize_source_path, source_included


@dataclass(frozen=True)
class SourceFile:
    path: str
    content: bytes
    label: str


def load_directory(path: str, rules: RuleSet, label: str | None = None, source_config: SourceConfig | None = None) -> dict[str, SourceFile]:
    root = Path(path)
    if not root.exists():
        raise FileNotFoundError(f"Directory does not exist: {path}")
    files: dict[str, SourceFile] = {}
    for file_path in root.rglob("*"):
        if not file_path.is_file():
            continue
        raw_relative = file_path.relative_to(root).as_posix()
        if not included(raw_relative, rules, source_config):
            continue
        relative = config_path(raw_relative, source_config)
        files[relative] = SourceFile(path=relative, content=file_path.read_bytes(), label=label or str(root))
    return files


def load_git_ref(repo: str, ref: str, rules: RuleSet, source_config: SourceConfig | None = None) -> dict[str, SourceFile]:
    names = _git(repo, "ls-tree", "-r", "--name-only", ref).decode("utf-8", errors="replace").splitlines()
    files: dict[str, SourceFile] = {}
    for name in names:
        if not included(name, rules, source_config):
            continue
        relative = config_path(name, source_config)
        try:
            content = _git(repo, "show", f"{ref}:{name}")
        except subprocess.CalledProcessError:
            continue
        files[relative] = SourceFile(path=relative, content=content, label=f"{Path(repo).name}@{ref}")
    return files


def config_path(path: str, source_config: SourceConfig | None = None) -> str:
    return normalize_source_path(path, source_config)


def included(path: str, rules: RuleSet, source_config: SourceConfig | None = None) -> bool:
    if source_config:
        return source_included(path, rules, source_config)
    if Path(path).suffix.lower() not in SUPPORTED_EXTENSIONS:
        return False
    if not any(matches_pattern(path, pattern) for pattern in rules.include):
        return False
    return not any(matches_pattern(path, pattern) for pattern in rules.exclude)


def _git(repo: str, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", repo, *args], stderr=subprocess.STDOUT)
