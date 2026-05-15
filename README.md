# ConfigDiffGuard

ConfigDiffGuard is a local-first configuration diff tool for large CSV/TSV/JSON/XLSX changes. It parses config files into records and fields, then reports added files, removed files, added records, removed records, field changes, schema changes, parse errors, and validation issues.

It can be used from a browser UI or from the command line.

## Features

- Compare two local config directories in the browser.
- Compare two Git refs, branches, tags, or commits from local repositories.
- Optionally compare remote Codeup repositories when a personal access token is configured.
- Support CSV, TSV, JSON, and XLSX.
- Define include/exclude paths, key fields, important fields, required fields, numeric ranges, enum checks, and references in `rules.json`.
- Export complete CSV/JSON results from the browser UI.
- Run entirely on your own machine. Local directory comparison does not upload files.

## Requirements

- Python 3.10+
- Optional XLSX support: `pip install ".[xlsx]"`
- Optional YAML rules support: `pip install ".[yaml]"`

## Quick Start

Clone and enter the repository:

```bash
git clone https://github.com/Y-Fenix/ConfigDiffGuard.git
cd ConfigDiffGuard
```

Run tests:

```bash
python3 -m unittest discover -s tests -q
```

Start the browser UI:

```bash
python3 -B -m config_diff_guard.server
```

Then open:

```text
http://127.0.0.1:8765/
```

On macOS, you can also double-click:

```text
打开配置对比工具.command
```

## LAN Sharing

To let teammates on the same LAN use your running tool:

```bash
python3 -B -m config_diff_guard.server --host 0.0.0.0
```

On macOS, you can double-click:

```text
局域网分享配置对比工具.command
```

The terminal will print LAN URLs such as:

```text
http://192.168.x.x:8765/
```

Keep the service running while teammates use it. If your firewall blocks Python, allow incoming local-network connections.

## Local Git Project Picker

The browser UI can list local Git projects from a root directory. By default it uses the parent directory of your current working directory. You can override it:

```bash
export CONFIG_DIFF_PROJECT_ROOT="/path/to/your/repos"
export CONFIG_DIFF_DEFAULT_REPO="/path/to/your/repos/my-project"
python3 -B -m config_diff_guard.server
```

## Optional Codeup Remote Access

Copy the example env file and fill in your own values:

```bash
cp .codeup.env.example .codeup.env
```

Then start the server from the same shell:

```bash
source .codeup.env
python3 -B -m config_diff_guard.server
```

Never commit `.codeup.env`; it is ignored by `.gitignore`.

## CLI Usage

Compare two directories:

```bash
python3 -m config_diff_guard \
  --old examples/old \
  --new examples/new \
  --rules examples/rules.demo.json \
  --out reports/demo
```

Compare two Git refs in a local repository:

```bash
python3 -m config_diff_guard \
  --repo /path/to/repo \
  --old-ref origin/main \
  --new-ref HEAD \
  --rules rules.json \
  --out reports/git-latest
```

Open the generated report:

```bash
open reports/demo/report.html
```

## Rules

`rules.json` controls what files are included and how records are matched and validated.

Minimal example:

```json
{
  "include": ["**/*.csv", "**/*.json"],
  "exclude": ["**/.git/**", "**/Library/**", "**/Temp/**"],
  "max_total_details": 20000,
  "tables": [
    {
      "pattern": "**/*",
      "primary_key": [],
      "important_fields": {
        "id": "critical",
        "key": "critical",
        "name": "high",
        "reward": "high"
      },
      "added_row_severity": "low",
      "removed_row_severity": "high"
    }
  ]
}
```

When `primary_key` is empty, ConfigDiffGuard tries common key candidates such as `id`, `ID`, `key`, `name`, `levelId`, and similar fields. If no candidate exists, it falls back to row position.

## Repository Contents

- `index.html`: browser UI.
- `config_diff_guard/`: Python package and local server.
- `rules.json`: generic default rules.
- `examples/`: demo input and demo rules.
- `tests/`: smoke tests.
- `.codeup.env.example`: optional remote Codeup configuration template.

## Privacy Notes

This public repository intentionally excludes local tokens, real private project lists, scan reports, generated comparison reports, caches, logs, and machine-specific paths.
