import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config_diff_guard import server
from config_diff_guard.cli import main
from config_diff_guard.engine import compare_sources
from config_diff_guard.models import Severity
from config_diff_guard.rules import FieldRule, RuleSet, TableRule
from config_diff_guard.sources import SourceFile


class DemoReportTest(unittest.TestCase):
    def test_demo_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "report"
            code = main(
                [
                    "--old",
                    "examples/old",
                    "--new",
                    "examples/new",
                    "--rules",
                    "examples/rules.demo.json",
                    "--out",
                    str(output),
                ]
            )
            self.assertEqual(code, 0)
            summary = (output / "summary.md").read_text(encoding="utf-8")
            self.assertIn("Modified fields: 2", summary)
            self.assertIn("Validation issues: 1", summary)

    def test_existing_validation_issue_is_not_reported_as_diff(self) -> None:
        rules = RuleSet(
            include=["*.csv"],
            tables=[
                TableRule(
                    pattern="items.csv",
                    primary_key=["id"],
                    field_rules=[FieldRule(field="name", required=True, severity=Severity.HIGH)],
                )
            ],
        )
        files = {"items.csv": SourceFile("items.csv", b"id,name\n1,\n", "same")}
        result = compare_sources(files, files, rules, "same@old", "same@new")
        self.assertEqual(result.stats.validation_issues, 0)
        self.assertEqual(result.changes, [])

    def test_new_validation_issue_is_reported(self) -> None:
        rules = RuleSet(
            include=["*.csv"],
            tables=[
                TableRule(
                    pattern="items.csv",
                    primary_key=["id"],
                    field_rules=[FieldRule(field="name", required=True, severity=Severity.HIGH)],
                )
            ],
        )
        old_files = {"items.csv": SourceFile("items.csv", b"id,name\n1,ok\n", "old")}
        new_files = {"items.csv": SourceFile("items.csv", b"id,name\n1,\n", "new")}
        result = compare_sources(old_files, new_files, rules, "old", "new")
        self.assertEqual(result.stats.validation_issues, 1)
        self.assertEqual([change.reason for change in result.changes if change.reason == "Required field is empty"], ["Required field is empty"])

    def test_provider_config_supports_mainstream_platforms(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "provider_accounts.json"
            config_path.write_text(
                """
                {
                  "workspaces": [
                    {"id": "gh", "name": "GitHub", "provider": "github", "owner": "demo", "token_env": "GITHUB_TOKEN"},
                    {"id": "gl", "name": "GitLab", "provider": "gitlab", "group": "demo", "token_env": "GITLAB_TOKEN"},
                    {"id": "ge", "name": "Gitee", "provider": "gitee", "owner": "demo", "token_env": "GITEE_TOKEN"},
                    {"id": "bb", "name": "Bitbucket", "provider": "bitbucket", "workspace": "demo", "token_env": "BITBUCKET_TOKEN"}
                  ]
                }
                """,
                encoding="utf-8",
            )
            env = {
                "CONFIG_DIFF_PROVIDER_CONFIG": str(config_path),
                "GITHUB_TOKEN": "gh-token",
                "GITLAB_TOKEN": "gl-token",
                "GITEE_TOKEN": "ge-token",
                "BITBUCKET_TOKEN": "bb-token",
            }
            with patch.dict("os.environ", env):
                workspaces = server.provider_config_workspaces()
        self.assertEqual([workspace["provider"] for workspace in workspaces], ["github", "gitlab", "gitee", "bitbucket"])
        self.assertEqual([workspace["provider_label"] for workspace in workspaces], ["GitHub", "GitLab", "Gitee", "Bitbucket"])
        self.assertEqual([workspace["token"] for workspace in workspaces], ["gh-token", "gl-token", "ge-token", "bb-token"])


if __name__ == "__main__":
    unittest.main()
