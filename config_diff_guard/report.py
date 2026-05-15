from __future__ import annotations

import csv
import html
import json
from pathlib import Path
from typing import Any

from .models import Change, CompareResult, Severity


HTML_CHANGE_LIMIT = 10000

SEVERITY_LABELS = {
    "critical": "严重",
    "high": "高",
    "medium": "中",
    "low": "低",
    "info": "提示",
}

TYPE_LABELS = {
    "added_row": "新增行",
    "removed_row": "删除行",
    "modified_field": "修改字段",
    "added_file": "新增文件",
    "removed_file": "删除文件",
    "schema_changed": "表结构变化",
    "validation": "校验问题",
}

REASON_LABELS = {
    "New config file": "新增配置文件",
    "Config file removed": "配置文件被删除",
    "Column set changed": "表头字段集合发生变化",
    "Important field changed": "关键字段变更",
    "Required field is empty": "必填字段为空",
    "Value is below minimum": "数值低于最小值",
    "Value is above maximum": "数值高于最大值",
    "Value is outside enum": "枚举值不在允许范围内",
}


def write_reports(result: CompareResult, output_dir: str) -> None:
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    (root / "summary.md").write_text(render_markdown(result), encoding="utf-8")
    (root / "report.html").write_text(render_html(result), encoding="utf-8")
    with (root / "changes.jsonl").open("w", encoding="utf-8") as handle:
        for change in result.changes:
            handle.write(json.dumps(_change_dict(change), ensure_ascii=False, default=str) + "\n")
    with (root / "changes.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["severity", "type", "table", "key", "field", "old", "new", "reason"])
        writer.writeheader()
        for change in result.changes:
            row = _change_dict(change)
            row["old"] = _compact(row.get("old"))
            row["new"] = _compact(row.get("new"))
            writer.writerow(row)


def render_markdown(result: CompareResult) -> str:
    stats = result.stats
    lines = [
        "# ConfigDiffGuard 配置对比报告",
        "",
        f"- Old: `{result.old_label}`",
        f"- New: `{result.new_label}`",
        f"- Generated: `{result.generated_at}`",
        "",
        "## Summary",
        "",
        f"- Tables: {stats.old_tables} -> {stats.new_tables}",
        f"- Added tables: {stats.added_tables}",
        f"- Removed tables: {stats.removed_tables}",
        f"- Changed tables: {stats.changed_tables}",
        f"- Added rows: {stats.added_rows}",
        f"- Removed rows: {stats.removed_rows}",
        f"- Modified fields: {stats.modified_fields}",
        f"- Validation issues: {stats.validation_issues}",
        f"- Parse errors: {stats.parse_errors}",
        "",
        "## Severity",
        "",
    ]
    severity_counts = _severity_counts(result.changes)
    for severity in [Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM, Severity.LOW, Severity.INFO]:
        lines.append(f"- {_severity_label(severity.value)}: {severity_counts.get(severity.value, 0)}")
    lines.extend(["", "## Top Changed Tables", ""])
    lines.append("| Table | Added | Removed | Modified | Validation | Schema |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: |")
    for table, summary in _top_tables(result):
        lines.append(
            f"| `{table}` | {summary.get('added_rows', 0)} | {summary.get('removed_rows', 0)} | "
            f"{summary.get('modified_fields', 0)} | {summary.get('validation', 0)} | {summary.get('schema_changed', 0)} |"
        )
    lines.extend(["", "## High Risk Details", ""])
    high_risk = [c for c in result.changes if c.severity in {Severity.CRITICAL, Severity.HIGH}]
    if not high_risk:
        lines.append("No critical or high risk changes found in retained details.")
    else:
        lines.append("| Severity | Type | Table | Key | Field | Old | New | Reason |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
        for change in high_risk[:300]:
            lines.append(
                f"| {_severity_label(change.severity.value)} | {_type_label(change.type.value)} | `{change.table}` | `{change.key}` | "
                f"`{change.field}` | {html.escape(_compact(change.old))} | {html.escape(_compact(change.new))} | "
                f"{html.escape(_reason_label(change.reason))} |"
            )
    return "\n".join(lines) + "\n"


def render_html(result: CompareResult) -> str:
    payload = json.dumps(
        {
            "old": result.old_label,
            "new": result.new_label,
            "generated": result.generated_at,
            "stats": result.stats.__dict__,
            "tables": result.table_summaries,
            "changes": [_change_dict(change) for change in _display_changes(result.changes, HTML_CHANGE_LIMIT)],
            "display_limit": HTML_CHANGE_LIMIT,
            "total_changes": len(result.changes),
        },
        ensure_ascii=False,
        default=str,
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>ConfigDiffGuard 配置对比</title>
  <style>
    :root {{ color-scheme: light; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    body {{ margin: 0; background: #f6f7f9; color: #1f2933; }}
    header {{ padding: 24px 32px; background: #18202f; color: white; }}
    h1 {{ margin: 0 0 8px; font-size: 24px; }}
    main {{ padding: 24px 32px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin: 18px 0; }}
    .metric {{ background: white; border: 1px solid #dde2ea; border-radius: 8px; padding: 14px; }}
    .metric strong {{ display: block; font-size: 24px; margin-top: 6px; }}
    .controls {{ display: flex; flex-wrap: wrap; gap: 10px; margin: 20px 0; }}
    input, select {{ border: 1px solid #c8d0dc; border-radius: 6px; padding: 8px 10px; background: white; }}
    .table-wrap {{ width: 100%; overflow-x: auto; overflow-y: visible; border: 1px solid #dde2ea; background: white; }}
    table {{ width: 100%; min-width: 1280px; table-layout: fixed; border-collapse: collapse; background: white; }}
    th, td {{ border-bottom: 1px solid #e6eaf0; border-right: 1px solid #edf1f6; padding: 8px 10px; text-align: left; vertical-align: middle; font-size: 13px; white-space: normal; }}
    th:last-child, td:last-child {{ border-right: 0; }}
    th {{ background: #eef2f7; }}
    .col-old, .col-new, .col-reason {{ border-left: 1px solid #d7e0eb; }}
    .floating-head {{ position: fixed; top: 0; left: 0; z-index: 50; display: none; overflow: hidden; border: 1px solid #dde2ea; border-bottom: 1px solid #cfd8e3; background: white; box-shadow: 0 6px 18px rgba(18, 26, 39, 0.12); pointer-events: none; }}
    .floating-head.visible {{ display: block; }}
    .floating-head table {{ margin: 0; will-change: transform; }}
    .floating-head th {{ box-sizing: border-box; }}
    code {{ white-space: normal; word-break: break-word; }}
    .cell-clip {{ display: block; max-width: 100%; overflow: visible; text-overflow: clip; white-space: normal; overflow-wrap: anywhere; word-break: break-word; vertical-align: bottom; }}
    .col-risk {{ width: 48px; min-width: 48px; }}
    .col-type {{ width: 72px; min-width: 72px; }}
    .col-table {{ width: 250px; min-width: 250px; max-width: 250px; }}
    .col-key {{ width: 100px; min-width: 100px; max-width: 100px; }}
    .col-field {{ width: 110px; min-width: 110px; max-width: 110px; }}
    .col-old, .col-new {{ width: 360px; min-width: 220px; max-width: 360px; }}
    .col-reason {{ width: 120px; min-width: 120px; max-width: 120px; }}
    .sev-critical {{ color: #a40000; font-weight: 700; }}
    .sev-high {{ color: #b42318; font-weight: 700; }}
    .sev-medium {{ color: #9a6700; font-weight: 700; }}
    .sev-low {{ color: #316dca; font-weight: 700; }}
    .sev-info {{ color: #57606a; }}
  </style>
</head>
<body>
  <header>
    <h1>ConfigDiffGuard 配置对比</h1>
    <div id="subtitle"></div>
  </header>
  <main>
    <section class="grid" id="metrics"></section>
    <section class="controls">
      <input id="query" placeholder="筛选表、主键、字段、原因" />
      <select id="severity">
        <option value="">全部风险</option>
        <option value="critical">严重</option>
        <option value="high">高</option>
        <option value="medium">中</option>
        <option value="low">低</option>
        <option value="info">提示</option>
      </select>
      <select id="type">
        <option value="">全部类型</option>
      </select>
    </section>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th class="col-risk">风险</th>
            <th class="col-type">类型</th>
            <th class="col-table">表</th>
            <th class="col-key">主键</th>
            <th class="col-field">字段</th>
            <th class="col-old">旧值</th>
            <th class="col-new">新值</th>
            <th class="col-reason">原因</th>
          </tr>
        </thead>
        <tbody id="rows"></tbody>
      </table>
    </div>
  </main>
  <script id="payload" type="application/json">{payload.replace("</", "<\\/")}</script>
  <script>
    const data = JSON.parse(document.getElementById('payload').textContent);
    const severityLabels = {{critical: '严重', high: '高', medium: '中', low: '低', info: '提示'}};
    const typeLabels = {{
      added_row: '新增行',
      removed_row: '删除行',
      modified_field: '修改字段',
      added_file: '新增文件',
      removed_file: '删除文件',
      schema_changed: '表结构变化',
      validation: '校验问题'
    }};
    const reasonLabels = {{
      'New config file': '新增配置文件',
      'Config file removed': '配置文件被删除',
      'Column set changed': '表头字段集合发生变化',
      'Important field changed': '关键字段变更',
      'Required field is empty': '必填字段为空',
      'Value is below minimum': '数值低于最小值',
      'Value is above maximum': '数值高于最大值',
      'Value is outside enum': '枚举值不在允许范围内'
    }};
    function displaySeverity(value) {{ return severityLabels[value] || value || ''; }}
    function displayType(value) {{ return typeLabels[value] || value || ''; }}
    function displayReason(value) {{
      if (!value) return '';
      const duplicate = String(value).match(/^Duplicate primary key appears (\\d+) times in new config$/);
      if (duplicate) return `新配置主键重复${{duplicate[1]}}次`;
      if (String(value).startsWith('Reference target table missing:')) return String(value).replace('Reference target table missing:', '引用目标表缺失：');
      if (String(value).startsWith('Broken reference to ')) return String(value).replace('Broken reference to ', '引用不存在：');
      return reasonLabels[value] || value;
    }}
    document.getElementById('subtitle').textContent = `${{data.old}} -> ${{data.new}} · ${{data.generated}} · HTML展示${{data.changes.length}}/${{data.total_changes}}条风险优先明细`;
    const metricLabels = [
      ['changed_tables', '变化表'],
      ['added_rows', '新增行'],
      ['removed_rows', '删除行'],
      ['modified_fields', '修改字段'],
      ['validation_issues', '校验问题'],
      ['parse_errors', '解析错误']
    ];
    document.getElementById('metrics').innerHTML = metricLabels.map(([key, label]) =>
      `<div class="metric">${{label}}<strong>${{data.stats[key] ?? 0}}</strong></div>`
    ).join('');
    const typeSelect = document.getElementById('type');
    [...new Set(data.changes.map(c => c.type))].sort().forEach(type => {{
      const option = document.createElement('option');
      option.value = type;
      option.textContent = displayType(type);
      typeSelect.appendChild(option);
    }});
    const rows = document.getElementById('rows');
    const query = document.getElementById('query');
    const severity = document.getElementById('severity');
    let floatingHeader = null;
    let floatingHeaderFrame = 0;
    let floatingHeaderSignature = '';
    function compact(value) {{
      if (value === null || value === undefined) return '';
      const text = typeof value === 'string' ? value : JSON.stringify(value);
      return text.length > 180 ? text.slice(0, 180) + '...' : text;
    }}
    function render() {{
      const q = query.value.trim().toLowerCase();
      const sev = severity.value;
      const typ = typeSelect.value;
      const filtered = data.changes.filter(c => {{
        if (sev && c.severity !== sev) return false;
        if (typ && c.type !== typ) return false;
        if (!q) return true;
        return [displaySeverity(c.severity), displayType(c.type), c.table, c.key, c.field, displayReason(c.reason), compact(c.old), compact(c.new)].join(' ').toLowerCase().includes(q);
      }}).slice(0, 2000);
      rows.innerHTML = filtered.map(c => `
        <tr>
          <td class="col-risk sev-${{c.severity}}">${{clip(displaySeverity(c.severity))}}</td>
          <td class="col-type">${{clip(displayType(c.type))}}</td>
          <td class="col-table"><code>${{clip(c.table)}}</code></td>
          <td class="col-key"><code>${{clip(c.key || '')}}</code></td>
          <td class="col-field"><code>${{clip(c.field || '')}}</code></td>
          <td class="col-old"><code>${{clip(compact(c.old), rawText(c.old))}}</code></td>
          <td class="col-new"><code>${{clip(compact(c.new), rawText(c.new))}}</code></td>
          <td class="col-reason">${{clip(displayReason(c.reason))}}</td>
        </tr>
      `).join('');
      scheduleFloatingHeaderUpdate();
    }}
    function setupFloatingHeader() {{
      const wrap = document.querySelector('.table-wrap');
      const table = wrap?.querySelector('table');
      const thead = table?.querySelector('thead');
      if (!wrap || !table || !thead) return;
      floatingHeader = document.createElement('div');
      floatingHeader.className = 'floating-head';
      floatingHeader.setAttribute('aria-hidden', 'true');
      floatingHeader.innerHTML = '<table><colgroup></colgroup><thead></thead></table>';
      document.body.appendChild(floatingHeader);
      window.addEventListener('scroll', scheduleFloatingHeaderUpdate, {{passive: true}});
      window.addEventListener('resize', scheduleFloatingHeaderUpdate);
      wrap.addEventListener('scroll', scheduleFloatingHeaderUpdate, {{passive: true}});
      scheduleFloatingHeaderUpdate();
    }}
    function scheduleFloatingHeaderUpdate() {{
      if (floatingHeaderFrame) return;
      floatingHeaderFrame = requestAnimationFrame(() => {{
        floatingHeaderFrame = 0;
        updateFloatingHeader();
      }});
    }}
    function updateFloatingHeader() {{
      if (!floatingHeader) return;
      const wrap = document.querySelector('.table-wrap');
      const table = wrap?.querySelector('table');
      const thead = table?.querySelector('thead');
      const floatingTable = floatingHeader.querySelector('table');
      const floatingColgroup = floatingHeader.querySelector('colgroup');
      const floatingThead = floatingHeader.querySelector('thead');
      if (!wrap || !table || !thead || !floatingTable || !floatingColgroup || !floatingThead) return;

      const tableRect = table.getBoundingClientRect();
      const wrapRect = wrap.getBoundingClientRect();
      const headRect = thead.getBoundingClientRect();
      const headHeight = Math.max(1, headRect.height);
      const shouldFloat = tableRect.top < 0 && tableRect.bottom > headHeight && wrapRect.bottom > headHeight;

      floatingHeader.classList.toggle('visible', shouldFloat);
      if (!shouldFloat) return;

      if (floatingHeaderSignature !== thead.innerHTML) {{
        floatingThead.innerHTML = thead.innerHTML;
        floatingHeaderSignature = thead.innerHTML;
      }}

      const sourceCells = [...thead.querySelectorAll('th')];
      const sourceWidths = sourceCells.map(cell => Math.ceil(cell.getBoundingClientRect().width));
      const tableWidth = Math.max(Math.ceil(tableRect.width), sourceWidths.reduce((total, width) => total + width, 0));
      floatingColgroup.innerHTML = sourceWidths.map(width => `<col style="width:${{width}}px">`).join('');

      const left = Math.max(0, wrapRect.left);
      const width = Math.min(wrapRect.width, window.innerWidth - left);
      floatingHeader.style.left = left + 'px';
      floatingHeader.style.width = width + 'px';
      floatingHeader.style.height = headHeight + 'px';
      floatingTable.style.width = tableWidth + 'px';
      floatingTable.style.minWidth = tableWidth + 'px';
      floatingTable.style.transform = 'translateX(' + (-wrap.scrollLeft) + 'px)';

      const cloneCells = [...floatingThead.querySelectorAll('th')];
      sourceWidths.forEach((cellWidth, index) => {{
        const clone = cloneCells[index];
        if (!clone) return;
        clone.style.setProperty('width', cellWidth + 'px', 'important');
        clone.style.setProperty('min-width', cellWidth + 'px', 'important');
        clone.style.setProperty('max-width', cellWidth + 'px', 'important');
      }});
    }}
    function clip(value, title = value) {{
      return `<span class="cell-clip" title="${{escapeHtml(title)}}">${{escapeHtml(value)}}</span>`;
    }}
    function rawText(value) {{
      return typeof value === 'string' ? value : JSON.stringify(value ?? '');
    }}
    function escapeHtml(value) {{
      return String(value ?? '').replace(/[&<>"']/g, char => ({{'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}}[char]));
    }}
    query.addEventListener('input', render);
    severity.addEventListener('change', render);
    typeSelect.addEventListener('change', render);
    setupFloatingHeader();
    render();
  </script>
</body>
</html>
"""


def _top_tables(result: CompareResult) -> list[tuple[str, dict[str, int]]]:
    def total(item: tuple[str, dict[str, int]]) -> int:
        return sum(item[1].values())

    return sorted(result.table_summaries.items(), key=total, reverse=True)[:50]


def _display_changes(changes: list[Change], limit: int) -> list[Change]:
    def rank(change: Change) -> tuple[int, str, str, str]:
        severity_rank = {
            Severity.CRITICAL: 0,
            Severity.HIGH: 1,
            Severity.MEDIUM: 2,
            Severity.LOW: 3,
            Severity.INFO: 4,
        }[change.severity]
        return severity_rank, change.table, change.key, change.field

    sorted_changes = sorted(changes, key=rank)
    protected_types = {"added_row", "removed_row", "added_file", "removed_file", "schema_changed", "validation"}
    kept: list[Change] = []
    seen: set[tuple[str, str, str, str, str, str, str]] = set()

    def add(change: Change) -> None:
        if len(kept) >= limit:
            return
        key = (change.type.value, change.table, change.key, change.field, str(change.old), str(change.new), change.reason)
        if key in seen:
            return
        seen.add(key)
        kept.append(change)

    for change in sorted_changes:
        if change.type.value in protected_types:
            add(change)
    for change in sorted_changes:
        add(change)
    return kept


def _severity_counts(changes: list[Change]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for change in changes:
        counts[change.severity.value] = counts.get(change.severity.value, 0) + 1
    return counts


def _change_dict(change: Change) -> dict[str, Any]:
    return {
        "severity": change.severity.value,
        "type": change.type.value,
        "table": change.table,
        "key": change.key,
        "field": change.field,
        "old": change.old,
        "new": change.new,
        "reason": change.reason,
    }


def _severity_label(value: str) -> str:
    return SEVERITY_LABELS.get(value, value)


def _type_label(value: str) -> str:
    return TYPE_LABELS.get(value, value)


def _reason_label(value: str) -> str:
    if value.startswith("Duplicate primary key appears ") and value.endswith(" times in new config"):
        count = value.removeprefix("Duplicate primary key appears ").removesuffix(" times in new config")
        return f"新配置主键重复{count}次"
    if value.startswith("Reference target table missing:"):
        return value.replace("Reference target table missing:", "引用目标表缺失：", 1)
    if value.startswith("Broken reference to "):
        return value.replace("Broken reference to ", "引用不存在：", 1)
    return REASON_LABELS.get(value, value)


def _compact(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    else:
        text = str(value)
    return text if len(text) <= 180 else text[:177] + "..."
