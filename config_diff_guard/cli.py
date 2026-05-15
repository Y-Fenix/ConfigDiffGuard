from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .engine import compare_sources
from .report import write_reports
from .rules import load_rules
from .sources import load_directory, load_git_ref


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="config-diff-guard",
        description="Semantic diff and risk report for large game config changes.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--rules", help="JSON/YAML rule file. JSON works without extra dependencies.")
    parser.add_argument("--out", default="diff_report", help="Output report directory.")
    parser.add_argument("--old", help="Old config directory.")
    parser.add_argument("--new", help="New config directory.")
    parser.add_argument("--repo", help="Git repository path for --old-ref/--new-ref.")
    parser.add_argument("--old-ref", help="Old Git ref, tag, branch, or commit.")
    parser.add_argument("--new-ref", help="New Git ref, tag, branch, or commit.")
    args = parser.parse_args(argv)

    try:
        rules = load_rules(args.rules)
        if args.repo and args.old_ref and args.new_ref:
            old_files = load_git_ref(args.repo, args.old_ref, rules)
            new_files = load_git_ref(args.repo, args.new_ref, rules)
            old_label = f"{Path(args.repo).name}@{args.old_ref}"
            new_label = f"{Path(args.repo).name}@{args.new_ref}"
        elif args.old and args.new:
            old_files = load_directory(args.old, rules, args.old)
            new_files = load_directory(args.new, rules, args.new)
            old_label = args.old
            new_label = args.new
        else:
            parser.error("Use either --old/--new directories or --repo with --old-ref/--new-ref.")
            return 2
        result = compare_sources(old_files, new_files, rules, old_label, new_label)
        write_reports(result, args.out)
    except Exception as exc:  # noqa: BLE001 - CLI should print a clear failure.
        print(f"config-diff-guard failed: {exc}", file=sys.stderr)
        return 1

    print(f"Report written to: {Path(args.out).resolve()}")
    print(f"Changed tables: {result.stats.changed_tables}")
    print(f"Added rows: {result.stats.added_rows}")
    print(f"Removed rows: {result.stats.removed_rows}")
    print(f"Modified fields: {result.stats.modified_fields}")
    print(f"Validation issues: {result.stats.validation_issues}")
    return 0
