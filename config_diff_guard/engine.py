from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from .models import Change, ChangeType, CompareResult, CompareStats, Severity
from .parsers import parse_table
from .rules import RuleSet
from .sources import SourceFile


def compare_sources(
    old_files: dict[str, SourceFile],
    new_files: dict[str, SourceFile],
    rules: RuleSet,
    old_label: str,
    new_label: str,
) -> CompareResult:
    old_tables = {
        path: parse_table(path, source.label, source.content, rules)
        for path, source in old_files.items()
    }
    new_tables = {
        path: parse_table(path, source.label, source.content, rules)
        for path, source in new_files.items()
    }
    stats = CompareStats(old_tables=len(old_tables), new_tables=len(new_tables))
    changes: list[Change] = []
    table_summaries: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))  # type: ignore[assignment]

    for table in sorted(set(old_tables) | set(new_tables)):
        old = old_tables.get(table)
        new = new_tables.get(table)
        if old is None and new is not None:
            stats.added_tables += 1
            changes.append(Change(ChangeType.ADDED_FILE, table, severity=Severity.MEDIUM, reason="New config file"))
            table_summaries[table]["added_file"] += 1
            continue
        if new is None and old is not None:
            stats.removed_tables += 1
            changes.append(Change(ChangeType.REMOVED_FILE, table, severity=Severity.HIGH, reason="Config file removed"))
            table_summaries[table]["removed_file"] += 1
            continue
        if old is None or new is None:
            continue

        rule = rules.table_rule_for(table)

        new_parse_errors = [message for message in new.parse_errors if message not in set(old.parse_errors)]
        if new_parse_errors:
            stats.parse_errors += len(new_parse_errors)
            for message in new_parse_errors:
                changes.append(Change(ChangeType.VALIDATION, table, severity=Severity.HIGH, reason=message))
                table_summaries[table]["parse_errors"] += 1
        if rule.primary_key:
            old_duplicate_signatures = {
                (key, count)
                for key, count in old.duplicate_keys.items()
            }
            for key, count in sorted(new.duplicate_keys.items()):
                if (key, count) in old_duplicate_signatures:
                    continue
                stats.validation_issues += 1
                changes.append(
                    Change(
                        ChangeType.VALIDATION,
                        table,
                        key=key,
                        severity=Severity.CRITICAL,
                        reason=f"Duplicate primary key appears {count} times in new config",
                    )
                )
                table_summaries[table]["validation"] += 1

        if set(old.columns) != set(new.columns):
            changes.append(
                Change(
                    ChangeType.SCHEMA_CHANGED,
                    table,
                    old=", ".join(old.columns),
                    new=", ".join(new.columns),
                    severity=Severity.MEDIUM,
                    reason="Column set changed",
                )
            )
            table_summaries[table]["schema_changed"] += 1

        old_keys = set(old.rows)
        new_keys = set(new.rows)
        added = new_keys - old_keys
        removed = old_keys - new_keys
        common = old_keys & new_keys

        for key in sorted(added):
            stats.added_rows += 1
            changes.append(Change(ChangeType.ADDED_ROW, table, key=key, new=new.rows[key].values, severity=rule.added_row_severity))
            table_summaries[table]["added_rows"] += 1
        for key in sorted(removed):
            stats.removed_rows += 1
            changes.append(Change(ChangeType.REMOVED_ROW, table, key=key, old=old.rows[key].values, severity=rule.removed_row_severity))
            table_summaries[table]["removed_rows"] += 1
        for key in sorted(common):
            old_row = old.rows[key]
            new_row = new.rows[key]
            fields = set(old_row.values) | set(new_row.values)
            for field in sorted(fields):
                old_value = old_row.values.get(field, "")
                new_value = new_row.values.get(field, "")
                if old_value == new_value:
                    continue
                stats.modified_fields += 1
                severity = rule.important_fields.get(field, Severity.INFO)
                reason = "Important field changed" if field in rule.important_fields else ""
                changes.append(
                    Change(
                        ChangeType.MODIFIED_FIELD,
                        table,
                        key=key,
                        field=field,
                        old=old_value,
                        new=new_value,
                        severity=severity,
                        reason=reason,
                        source=new.source,
                    )
                )
                table_summaries[table]["modified_fields"] += 1

        old_validation_signatures = {
            validation_signature(issue)
            for issue in validate_table(table, old.rows, rules, old_tables)
        }
        validation = [
            issue
            for issue in validate_table(table, new.rows, rules, new_tables)
            if validation_signature(issue) not in old_validation_signatures
        ]
        stats.validation_issues += len(validation)
        for issue in validation:
            changes.append(issue)
            table_summaries[table]["validation"] += 1

    stats.changed_tables = len(table_summaries)
    table_summaries = {name: dict(summary) for name, summary in table_summaries.items()}
    return CompareResult(
        old_label=old_label,
        new_label=new_label,
        stats=stats,
        changes=changes,
        table_summaries=table_summaries,
        generated_at=datetime.now().isoformat(timespec="seconds"),
    )


def validate_table(table: str, rows: dict[str, Any], rules: RuleSet, all_tables: dict[str, Any]) -> list[Change]:
    table_rule = rules.table_rule_for(table)
    changes: list[Change] = []
    for key, row in rows.items():
        values = row.values
        if _is_empty_values(values):
            continue
        for field_rule in table_rule.field_rules:
            value = values.get(field_rule.field, "")
            if field_rule.required and value in ("", None):
                changes.append(_validation(table, key, field_rule.field, value, field_rule.severity, "Required field is empty"))
                continue
            if field_rule.enum is not None and str(value) not in field_rule.enum:
                changes.append(_validation(table, key, field_rule.field, value, field_rule.severity, "Value is outside enum"))
            numeric = _to_float(value)
            if numeric is not None:
                if field_rule.min_value is not None and numeric < field_rule.min_value:
                    changes.append(_validation(table, key, field_rule.field, value, field_rule.severity, "Value is below minimum"))
                if field_rule.max_value is not None and numeric > field_rule.max_value:
                    changes.append(_validation(table, key, field_rule.field, value, field_rule.severity, "Value is above maximum"))
        for reference in table_rule.references:
            value = values.get(reference.field, "")
            if value in ("", None):
                continue
            target = all_tables.get(reference.target_table)
            if target is None:
                changes.append(_validation(table, key, reference.field, value, Severity.HIGH, f"Reference target table missing: {reference.target_table}"))
                continue
            if str(value) not in target.rows:
                changes.append(_validation(table, key, reference.field, value, reference.severity, f"Broken reference to {reference.target_table}"))
    return changes


def validation_signature(change: Change) -> tuple[str, str, str, str, str]:
    return (
        change.table,
        change.key,
        change.field,
        change.reason,
        str(change.new),
    )


def _validation(table: str, key: str, field: str, value: Any, severity: Severity, reason: str) -> Change:
    return Change(ChangeType.VALIDATION, table=table, key=key, field=field, new=value, severity=severity, reason=reason)


def _is_empty_values(values: dict[str, Any]) -> bool:
    visible_values = [
        value
        for key, value in values.items()
        if not str(key).startswith("__") and str(key) != ""
    ]
    return not visible_values or all(value in ("", None) for value in visible_values)


def _to_float(value: Any) -> float | None:
    try:
        if value in ("", None):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
