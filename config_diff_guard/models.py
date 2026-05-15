from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Severity(str, Enum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


SEVERITY_ORDER = {
    Severity.INFO: 0,
    Severity.LOW: 1,
    Severity.MEDIUM: 2,
    Severity.HIGH: 3,
    Severity.CRITICAL: 4,
}


class ChangeType(str, Enum):
    ADDED_ROW = "added_row"
    REMOVED_ROW = "removed_row"
    MODIFIED_FIELD = "modified_field"
    ADDED_FILE = "added_file"
    REMOVED_FILE = "removed_file"
    SCHEMA_CHANGED = "schema_changed"
    VALIDATION = "validation"


@dataclass(frozen=True)
class RowRecord:
    table: str
    key: str
    values: dict[str, Any]
    source: str
    line: int | None = None


@dataclass
class TableSnapshot:
    name: str
    source: str
    rows: dict[str, RowRecord] = field(default_factory=dict)
    columns: list[str] = field(default_factory=list)
    duplicate_keys: dict[str, int] = field(default_factory=dict)
    parse_errors: list[str] = field(default_factory=list)


@dataclass
class Change:
    type: ChangeType
    table: str
    key: str = ""
    field: str = ""
    old: Any = None
    new: Any = None
    severity: Severity = Severity.INFO
    reason: str = ""
    source: str = ""


@dataclass
class CompareStats:
    old_tables: int = 0
    new_tables: int = 0
    added_tables: int = 0
    removed_tables: int = 0
    changed_tables: int = 0
    added_rows: int = 0
    removed_rows: int = 0
    modified_fields: int = 0
    validation_issues: int = 0
    parse_errors: int = 0


@dataclass
class CompareResult:
    old_label: str
    new_label: str
    stats: CompareStats
    changes: list[Change]
    table_summaries: dict[str, dict[str, int]]
    generated_at: str
