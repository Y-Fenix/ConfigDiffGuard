# Config Diff Guard

面向游戏项目的大规模配置语义对比与风险识别工具。

它不是普通文本 diff，而是把 CSV、TSV、JSON、XLSX 配置解析成“表 + 主键 + 字段”，输出可审查的变更摘要、高风险明细和校验问题。适合配置频繁变化、一次变动十几万行的项目。

## 能解决什么

- 按目录或 Git 分支/commit 对比配置。
- 自动识别新增表、删除表、新增行、删除行、字段修改、表结构变化。
- 通过规则文件定义主键、关键字段、数值范围、枚举、必填字段、跨表引用。
- 输出 `summary.md`、`report.html`、`changes.csv`、`changes.jsonl`。
- 对大规模变动自动截断明细，优先保留 critical/high 风险项。

## 快速开始

双击运行：

- `本地开启.command`：启动本地可视化页面，可选择旧目录/新目录，也可选择两个 Git 分支版本后直接对比。
- `局域网开启.command`：前台启动局域网分享版，终端关闭后服务停止，适合临时看日志。
- `局域网 ON.command`：后台启动局域网分享版，关闭终端不影响同事访问；会优先注册开机常驻，受系统限制时自动退到本次后台模式。
- `查看局域网分享状态.command`：查看服务是否运行，并显示本机和同事访问地址。
- `局域网 OFF.command`：停止后台分享服务。
- `配置远程平台账号.command`：创建并打开本地账号配置文件。

当前内置配置：

- `rules.json`：默认对比规则。
- `studio_config_sources.example.json`：配置路径规则模板。复制为 `studio_config_sources.json` 后按自己的项目调整。
- `provider_accounts.example.json`：远程平台账号配置模板。

## 多平台账号配置

商业化版本建议使用 `provider_accounts.json` 管理远程代码平台账号。该文件只放在本地，不要提交到 Git。

快速配置：

```bash
cp provider_accounts.example.json provider_accounts.json
open -e provider_accounts.json
```

当前已支持：

- GitHub：通过 GitHub REST API 读取组织/个人仓库、分支、Tag、提交和文件树。
- 阿里云 Codeup：通过云效 Codeup OpenAPI 读取组织/代码组项目、分支、Tag、提交和文件树。
- GitLab：通过 GitLab REST API 读取组/个人可见项目、分支、Tag、提交和文件树。
- Gitee：通过 Gitee OpenAPI 读取组织/个人仓库、分支、Tag、提交和文件内容。
- Bitbucket：通过 Bitbucket Cloud REST API 读取 Workspace 仓库、分支、Tag、提交和文件内容。

推荐写法是 token 放在 `.codeup.env` 或你的 shell 环境里，`provider_accounts.json` 只写 `token_env`：

```json
{
  "default_workspace": "github-main",
  "workspaces": [
    {
      "id": "github-main",
      "name": "GitHub 主组织",
      "provider": "github",
      "owner": "your-github-org",
      "owner_type": "org",
      "token_env": "GITHUB_TOKEN"
    },
    {
      "id": "codeup-main",
      "name": "Codeup 主组织",
      "provider": "codeup",
      "organization_id": "your-codeup-organization-id",
      "namespace_id": "your-codeup-namespace-id",
      "token_env": "YUNXIAO_TOKEN"
    },
    {
      "id": "gitlab-main",
      "name": "GitLab 主组",
      "provider": "gitlab",
      "group": "your-gitlab-group",
      "token_env": "GITLAB_TOKEN"
    },
    {
      "id": "gitee-main",
      "name": "Gitee 主组织",
      "provider": "gitee",
      "owner": "your-gitee-org",
      "owner_type": "org",
      "token_env": "GITEE_TOKEN"
    },
    {
      "id": "bitbucket-main",
      "name": "Bitbucket Workspace",
      "provider": "bitbucket",
      "workspace": "your-bitbucket-workspace",
      "username_env": "BITBUCKET_USERNAME",
      "token_env": "BITBUCKET_APP_PASSWORD"
    }
  ]
}
```

如果客户只希望工具访问固定仓库，不想遍历全部项目，可以用 `repositories` 固定清单，详见 `provider_accounts.example.json`。固定仓库模式适用于所有平台。

兼容说明：旧的 `.codeup.env` / `YUNXIAO_ORGANIZATIONS` 仍然可用；如果同时存在 `provider_accounts.json`，工具会优先展示 JSON 中配置的平台账号，并保留旧 Codeup 配置作为兼容入口。

命令行运行：

```bash
cd /path/to/ConfigDiffGuard
python3 -m config_diff_guard \
  --old /path/to/old/config \
  --new /path/to/new/config \
  --rules rules.json \
  --out reports/latest
```

对比 Git 分支或 commit：

```bash
python3 -m config_diff_guard \
  --repo /path/to/your/repo \
  --old-ref origin/master \
  --new-ref HEAD \
  --rules rules.json \
  --out reports/git-latest
```

然后打开：

```bash
open reports/latest/report.html
```

## 自测

```bash
cd /path/to/ConfigDiffGuard
python3 -m unittest discover -s tests -q
```

## 规则文件示例

```json
{
  "include": ["LevelData/**/*.csv", "**/*.json"],
  "exclude": ["**/Library/**", "**/Temp/**"],
  "max_details_per_table": 300,
  "max_total_details": 8000,
  "tables": [
    {
      "pattern": "LevelData/Level/*.csv",
      "primary_key": ["id"],
      "important_fields": {
        "word": "high",
        "difficulty": "high",
        "reward": "medium"
      },
      "field_rules": [
        {"field": "id", "required": true, "severity": "critical"},
        {"field": "difficulty", "min": 1, "max": 10, "severity": "high"}
      ]
    }
  ]
}
```

## 建议接入方式

第一阶段先作为发版前手动门禁使用：

```bash
python3 -m config_diff_guard --repo <repo> --old-ref <last_release> --new-ref HEAD --rules <rules> --out reports/release
```

第二阶段接入 CI 或本地一键脚本：当出现 `critical/high` 变更或校验问题时要求开发/策划确认。

## 设计取舍

- 默认零依赖，CSV/TSV/JSON 直接可用。
- XLSX 需要安装 `openpyxl`：`python3 -m pip install openpyxl`。
- YAML 规则需要安装 `PyYAML`，不想装依赖就用 JSON 规则。
- `report.html` 默认展示风险优先的前 10000 条明细，避免浏览器被大变更拖垮；完整明细保留在 `changes.jsonl` 和 `changes.csv` 中。
