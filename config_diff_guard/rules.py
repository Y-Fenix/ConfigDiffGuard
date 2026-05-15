from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import Severity


DEFAULT_KEY_CANDIDATES = (
    "id",
    "ID",
    "Id",
    "uid",
    "UID",
    "uuid",
    "UUID",
    "guid",
    "GUID",
    "key",
    "Key",
    "name",
    "Name",
    "code",
    "Code",
    "level_id",
    "levelId",
    "level",
    "Level",
    "stage",
    "Stage",
    "chapter",
    "Chapter",
    "word",
    "Word",
    "序号",
    "编号",
    "配置ID",
    "关卡ID",
    "所属关卡ID",
    "DateId",
    "类型",
)


@dataclass
class FieldRule:
    field: str
    severity: Severity = Severity.MEDIUM
    min_value: float | None = None
    max_value: float | None = None
    enum: set[str] | None = None
    required: bool = False


@dataclass
class ReferenceRule:
    field: str
    target_table: str
    severity: Severity = Severity.HIGH


@dataclass
class TableRule:
    pattern: str
    primary_key: list[str] = field(default_factory=list)
    important_fields: dict[str, Severity] = field(default_factory=dict)
    field_rules: list[FieldRule] = field(default_factory=list)
    references: list[ReferenceRule] = field(default_factory=list)
    added_row_severity: Severity = Severity.LOW
    removed_row_severity: Severity = Severity.MEDIUM


@dataclass
class RuleSet:
    include: list[str] = field(default_factory=lambda: ["*.csv", "*.tsv", "*.json", "*.xlsx", "**/*.csv", "**/*.tsv", "**/*.json", "**/*.xlsx"])
    exclude: list[str] = field(default_factory=lambda: [
        "**/.git/**",
        "**/Library/**",
        "**/Temp/**",
        "**/Logs/**",
        "**/Build/**",
        "**/Builds/**",
        "**/obj/**",
        "**/bin/**",
        "**/node_modules/**",
        "**/Packages/**",
        "**/ProjectSettings/**",
        "**/UserSettings/**",
        "**/Plugins/**",
        "**/Spine/**",
        "**/*.meta",
    ])
    tables: list[TableRule] = field(default_factory=list)
    max_details_per_table: int = 200
    max_total_details: int = 5000

    def table_rule_for(self, table: str) -> TableRule:
        for rule in self.tables:
            if matches_pattern(table, rule.pattern):
                return rule
        return TableRule(pattern=table)


def matches_pattern(path: str, pattern: str) -> bool:
    if fnmatch.fnmatch(path, pattern):
        return True
    if "/**/" in pattern and fnmatch.fnmatch(path, pattern.replace("/**/", "/")):
        return True
    if pattern.startswith("**/") and fnmatch.fnmatch(path, pattern[3:]):
        return True
    return False


def load_rules(path: str | None) -> RuleSet:
    if not path:
        return RuleSet()
    data = _load_json_or_yaml(Path(path))
    rules = RuleSet()
    rules.include = list(data.get("include", rules.include))
    rules.exclude = list(data.get("exclude", rules.exclude))
    rules.max_details_per_table = int(data.get("max_details_per_table", rules.max_details_per_table))
    rules.max_total_details = int(data.get("max_total_details", rules.max_total_details))
    for item in data.get("tables", []):
        important = {
            str(name): _severity(value)
            for name, value in item.get("important_fields", {}).items()
        }
        field_rules = []
        for field_item in item.get("field_rules", []):
            enum = field_item.get("enum")
            field_rules.append(
                FieldRule(
                    field=str(field_item["field"]),
                    severity=_severity(field_item.get("severity", "medium")),
                    min_value=field_item.get("min"),
                    max_value=field_item.get("max"),
                    enum=set(map(str, enum)) if enum is not None else None,
                    required=bool(field_item.get("required", False)),
                )
            )
        references = []
        for ref_item in item.get("references", []):
            references.append(
                ReferenceRule(
                    field=str(ref_item["field"]),
                    target_table=str(ref_item["target_table"]),
                    severity=_severity(ref_item.get("severity", "high")),
                )
            )
        rules.tables.append(
            TableRule(
                pattern=str(item["pattern"]),
                primary_key=[str(v) for v in item.get("primary_key", [])],
                important_fields=important,
                field_rules=field_rules,
                references=references,
                added_row_severity=_severity(item.get("added_row_severity", "low")),
                removed_row_severity=_severity(item.get("removed_row_severity", "medium")),
            )
        )
    return rules


def infer_primary_key(columns: list[str], table_rule: TableRule) -> list[str]:
    if table_rule.primary_key:
        return table_rule.primary_key
    column_set = set(columns)
    for name in DEFAULT_KEY_CANDIDATES:
        if name in column_set:
            return [name]
    if columns:
        return [columns[0]]
    return []


def _severity(value: Any) -> Severity:
    if isinstance(value, Severity):
        return value
    try:
        return Severity(str(value).lower())
    except ValueError:
        return Severity.MEDIUM


def _load_json_or_yaml(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yml", ".yaml"}:
        try:
            import yaml  # type: ignore
        except ImportError as exc:
            raise RuntimeError("YAML rules require PyYAML. Use JSON or install PyYAML.") from exc
        return yaml.safe_load(text) or {}
    return json.loads(text)
