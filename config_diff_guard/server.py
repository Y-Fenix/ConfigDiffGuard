from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlparse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .engine import compare_sources
from .rules import load_rules
from .source_config import SourceConfig, load_source_config, source_tree_roots
from .sources import SourceFile, config_path, included, load_git_ref


TOOL_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = Path(os.environ.get("CONFIG_DIFF_PROJECT_ROOT", str(Path.home() / "Documents" / "Unity")))
DEFAULT_REPO = Path(os.environ.get("CONFIG_DIFF_DEFAULT_REPO", str(PROJECT_ROOT / os.environ.get("CONFIG_DIFF_DEFAULT_PROJECT", "WordGroup"))))
DEFAULT_RULES = TOOL_DIR / "rules.json"
PROVIDER_CONFIG = TOOL_DIR / "provider_accounts.json"
SUPPORTED_REMOTE_PROVIDERS = {"github", "codeup", "gitlab", "gitee", "bitbucket"}


class DiffRequestHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(TOOL_DIR), **kwargs)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        parsed = urlparse(self.path)
        if parsed.path == "/api/projects":
            try:
                self._send_json(projects_payload())
            except Exception as exc:  # noqa: BLE001 - API returns clear UI errors.
                self._send_json({"error": f"{type(exc).__name__}: {exc}"}, status=500)
            return
        if parsed.path == "/api/cloud-projects":
            try:
                org = parse_qs(parsed.query).get("org", [""])[0]
                self._send_json(cloud_projects_payload(org))
            except Exception as exc:  # noqa: BLE001 - API returns clear UI errors.
                self._send_json({"error": f"{type(exc).__name__}: {exc}"}, status=500)
            return
        if parsed.path == "/api/refs":
            try:
                project = parse_qs(parsed.query).get("project", [""])[0]
                org = parse_qs(parsed.query).get("org", [""])[0]
                source = parse_qs(parsed.query).get("source", [""])[0]
                if source == "cloud":
                    self._send_json(remote_refs(org, project))
                else:
                    self._send_json(git_refs(resolve_project(project)))
            except Exception as exc:  # noqa: BLE001 - API returns clear UI errors.
                self._send_json({"error": f"{type(exc).__name__}: {exc}"}, status=500)
            return
        if parsed.path == "/":
            self.path = "/index.html"
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        parsed = urlparse(self.path)
        if parsed.path != "/api/git-compare":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            source = str(payload.get("source", "")).strip()
            project_name = str(payload.get("project", "")).strip()
            org = str(payload.get("org", "")).strip()
            old_ref = str(payload["old_ref"]).strip()
            new_ref = str(payload["new_ref"]).strip()
            if not old_ref or not new_ref:
                raise ValueError("old_ref and new_ref are required")
            rules = load_rules(str(DEFAULT_RULES))
            source_config = load_source_config(TOOL_DIR, org, project_name)
            if source == "cloud":
                project = remote_project(org, project_name)
                old_files, new_files = load_remote_ref_pair(org, project, old_ref, new_ref, rules, source_config)
                old_label = f"{project['name']}@{old_ref}"
                new_label = f"{project['name']}@{new_ref}"
            else:
                repo = resolve_project(project_name)
                old_files = load_git_ref(str(repo), old_ref, rules, source_config)
                new_files = load_git_ref(str(repo), new_ref, rules, source_config)
                old_label = f"{repo.name}@{old_ref}"
                new_label = f"{repo.name}@{new_ref}"
            result = compare_sources(
                old_files,
                new_files,
                rules,
                old_label,
                new_label,
            )
            self._send_json(result_to_payload(result, int(getattr(rules, "max_total_details", 20000))))
        except Exception as exc:  # noqa: BLE001 - API returns clear UI errors.
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, status=500)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - http.server API
        sys.stderr.write("[ConfigDiff] " + format % args + "\n")

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def git_refs(repo: Path) -> dict[str, Any]:
    return {
        "project": repo.name,
        "repo": str(repo),
        "current_branch": git(repo, "branch", "--show-current").strip(),
        "local_branches": lines(git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads")),
        "remote_branches": [
            ref
            for ref in lines(git(repo, "for-each-ref", "--format=%(refname:short)", "refs/remotes"))
            if not ref.endswith("/HEAD")
        ],
        "tags": lines(git(repo, "for-each-ref", "--sort=-creatordate", "--format=%(refname:short)", "refs/tags")),
        "commits": recent_commits(repo),
    }


def remote_refs(org_id: str, project_name: str) -> dict[str, Any]:
    workspace = remote_workspace(org_id)
    provider = workspace.get("provider", "codeup")
    if provider == "github":
        return github_refs(workspace, project_name)
    if provider == "gitlab":
        return gitlab_refs(workspace, project_name)
    if provider == "gitee":
        return gitee_refs(workspace, project_name)
    if provider == "bitbucket":
        return bitbucket_refs(workspace, project_name)
    if provider == "codeup":
        return codeup_refs(org_id, project_name)
    raise ValueError(f"暂不支持的平台：{provider}")


def remote_project(org_id: str, project_name: str) -> dict[str, Any]:
    workspace = remote_workspace(org_id)
    provider = workspace.get("provider", "codeup")
    if provider == "github":
        return github_project(workspace, project_name)
    if provider in {"gitlab", "gitee", "bitbucket"}:
        return remote_project_from_list(workspace, project_name)
    if provider == "codeup":
        return codeup_project(org_id, project_name)
    raise ValueError(f"暂不支持的平台：{provider}")


def load_remote_ref_pair(
    org_id: str,
    project: dict[str, Any],
    old_ref: str,
    new_ref: str,
    rules: Any,
    source_config: SourceConfig | None = None,
) -> tuple[dict[str, SourceFile], dict[str, SourceFile]]:
    provider = str(project.get("provider") or remote_workspace(org_id).get("provider") or "codeup")
    if provider == "github":
        return load_github_ref_pair(remote_workspace(org_id), project, old_ref, new_ref, rules, source_config)
    if provider == "gitlab":
        return load_gitlab_ref_pair(remote_workspace(org_id), project, old_ref, new_ref, rules, source_config)
    if provider == "gitee":
        return load_gitee_ref_pair(remote_workspace(org_id), project, old_ref, new_ref, rules, source_config)
    if provider == "bitbucket":
        return load_bitbucket_ref_pair(remote_workspace(org_id), project, old_ref, new_ref, rules, source_config)
    if provider == "codeup":
        return load_codeup_ref_pair(org_id, project, old_ref, new_ref, rules, source_config)
    raise ValueError(f"暂不支持的平台：{provider}")


def codeup_refs(org_id: str, project_name: str) -> dict[str, Any]:
    project = codeup_project(org_id, project_name)
    token, domain, organization_id = codeup_context(org_id)
    base = codeup_repository_url(domain, organization_id, codeup_repo_path(project))
    branches = codeup_get_all(base, "/branches", token, domain, {"page": 1, "perPage": 100}, max_pages=20)
    tags = codeup_get_all(base, "/tags", token, domain, {"page": 1, "perPage": 100}, max_pages=20)
    default_branch = next((str(item.get("name", "")) for item in branches if item.get("defaultBranch")), "")
    default_branch = default_branch or str(project.get("default_branch") or "")
    branch_names = [str(item.get("name", "")).strip() for item in branches if str(item.get("name", "")).strip()]
    commit_ref = default_branch or (branch_names[0] if branch_names else "")
    commits = codeup_commits(base, token, domain, commit_ref) if commit_ref else []
    return {
        "project": project["name"],
        "repo": project.get("web_url") or project.get("remote_url") or codeup_repo_path(project),
        "source": "cloud",
        "current_branch": default_branch,
        "local_branches": [],
        "remote_branches": branch_names,
        "tags": [str(item.get("name", "")).strip() for item in tags if str(item.get("name", "")).strip()],
        "commits": commits,
    }


def codeup_commits(base: str, token: str, domain: str, ref_name: str) -> list[dict[str, str]]:
    payload = codeup_get_all(base, "/commits", token, domain, {"refName": ref_name, "page": 1, "perPage": 80}, max_pages=1)
    commits = []
    for item in payload:
        commit_id = str(item.get("id") or "").strip()
        if not commit_id:
            continue
        message = str(item.get("message") or "")
        commits.append({
            "short": str(item.get("shortId") or commit_id[:8]),
            "hash": commit_id,
            "date": str(item.get("committedDate") or item.get("authoredDate") or ""),
            "author": str(item.get("authorName") or item.get("committerName") or ""),
            "subject": str(item.get("title") or (message.splitlines()[0] if message else "")),
        })
    return commits


def github_refs(workspace: dict[str, Any], project_name: str) -> dict[str, Any]:
    project = github_project(workspace, project_name)
    owner, repo = github_repo_owner_name(workspace, project)
    token, api_base = github_context(workspace)
    base = f"{api_base}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
    branches = github_get_all(f"{base}/branches", token, {"per_page": 100})
    tags = github_get_all(f"{base}/tags", token, {"per_page": 100})
    default_branch = str(project.get("default_branch") or "")
    branch_names = [str(item.get("name", "")).strip() for item in branches if str(item.get("name", "")).strip()]
    commit_ref = default_branch or (branch_names[0] if branch_names else "")
    commits = github_commits(base, token, commit_ref) if commit_ref else []
    return {
        "project": project["name"],
        "repo": project.get("web_url") or project.get("remote_url") or f"{owner}/{repo}",
        "source": "cloud",
        "provider": "github",
        "provider_label": workspace.get("provider_label") or "GitHub",
        "current_branch": default_branch,
        "local_branches": [],
        "remote_branches": branch_names,
        "tags": [str(item.get("name", "")).strip() for item in tags if str(item.get("name", "")).strip()],
        "commits": commits,
    }


def github_commits(base: str, token: str, ref_name: str) -> list[dict[str, str]]:
    payload = github_get_all(f"{base}/commits", token, {"sha": ref_name, "per_page": 80}, max_pages=1)
    commits = []
    for item in payload:
        commit_id = str(item.get("sha") or "").strip()
        if not commit_id:
            continue
        commit = item.get("commit") if isinstance(item.get("commit"), dict) else {}
        author = commit.get("author") if isinstance(commit.get("author"), dict) else {}
        message = str(commit.get("message") or "")
        commits.append({
            "short": commit_id[:8],
            "hash": commit_id,
            "date": str(author.get("date") or ""),
            "author": str(author.get("name") or item.get("author", {}).get("login") if isinstance(item.get("author"), dict) else ""),
            "subject": message.splitlines()[0] if message else "",
        })
    return commits


def load_github_ref_pair(
    workspace: dict[str, Any],
    project: dict[str, Any],
    old_ref: str,
    new_ref: str,
    rules: Any,
    source_config: SourceConfig | None = None,
) -> tuple[dict[str, SourceFile], dict[str, SourceFile]]:
    old_index = github_file_index(workspace, project, old_ref, rules, source_config)
    new_index = github_file_index(workspace, project, new_ref, rules, source_config)
    old_entries: list[tuple[str, str, str]] = []
    new_entries: list[tuple[str, str, str]] = []
    for relative in sorted(set(old_index) | set(new_index)):
        old_item = old_index.get(relative)
        new_item = new_index.get(relative)
        if old_item and new_item and old_item.get("sha") == new_item.get("sha"):
            continue
        if old_item:
            old_entries.append((relative, str(old_item.get("path") or ""), str(old_item.get("sha") or "")))
        if new_item:
            new_entries.append((relative, str(new_item.get("path") or ""), str(new_item.get("sha") or "")))
    old_files = download_github_files(workspace, project, old_entries, old_ref, f"{project['name']}@{old_ref}")
    new_files = download_github_files(workspace, project, new_entries, new_ref, f"{project['name']}@{new_ref}")
    return old_files, new_files


def github_file_index(
    workspace: dict[str, Any],
    project: dict[str, Any],
    ref: str,
    rules: Any,
    source_config: SourceConfig | None = None,
) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    project_name = str(project.get("name") or "")
    tree = github_config_tree(workspace, project, ref, project_name, source_config)
    for item in tree:
        if str(item.get("type") or "").lower() != "blob":
            continue
        raw_path = str(item.get("path") or "")
        if not included(raw_path, rules, source_config):
            continue
        relative = config_path(raw_path, source_config)
        index[relative] = item
    return index


def github_config_tree(
    workspace: dict[str, Any],
    project: dict[str, Any],
    ref: str,
    project_name: str,
    source_config: SourceConfig | None = None,
) -> list[dict[str, Any]]:
    token, api_base = github_context(workspace)
    owner, repo = github_repo_owner_name(workspace, project)
    base = f"{api_base}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
    commit = github_get_json(f"{base}/commits/{quote(ref, safe='')}", token)
    tree_sha = str((commit.get("commit") or {}).get("tree", {}).get("sha") or "")
    if not tree_sha:
        raise ValueError(f"GitHub 无法读取 ref：{ref}")
    tree_payload = github_get_json(f"{base}/git/trees/{quote(tree_sha, safe='')}?recursive=1", token)
    tree = [item for item in tree_payload.get("tree", []) if isinstance(item, dict)]
    candidates = source_tree_roots(source_config, project_name)
    if not source_config:
        return tree
    return [
        item for item in tree
        if any(str(item.get("path") or "").startswith(root.rstrip("/") + "/") or str(item.get("path") or "") == root.rstrip("/") for root in candidates)
    ]


def download_github_files(
    workspace: dict[str, Any],
    project: dict[str, Any],
    entries: list[tuple[str, str, str]],
    ref: str,
    label: str,
) -> dict[str, SourceFile]:
    if not entries:
        return {}
    max_workers = max(1, min(int(os.environ.get("REMOTE_DOWNLOAD_WORKERS", "16")), len(entries)))
    files: dict[str, SourceFile] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(cached_github_file_content, workspace, project, raw_path, ref, content_id): (relative, raw_path)
            for relative, raw_path, content_id in entries
        }
        for future in as_completed(futures):
            relative, _raw_path = futures[future]
            files[relative] = SourceFile(path=relative, content=future.result(), label=label)
    return files


def cached_github_file_content(
    workspace: dict[str, Any],
    project: dict[str, Any],
    file_path: str,
    ref: str,
    content_id: str,
) -> bytes:
    owner, repo = github_repo_owner_name(workspace, project)
    stable_id = content_id or ref
    digest = hashlib.sha256(f"github\0{owner}/{repo}\0{file_path}\0{stable_id}".encode("utf-8")).hexdigest()
    cache_path = TOOL_DIR / ".cache" / "github_files" / digest[:2] / digest
    if cache_path.exists():
        return cache_path.read_bytes()
    token, api_base = github_context(workspace)
    base = f"{api_base}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
    payload = github_get_json(f"{base}/git/blobs/{quote(content_id, safe='')}", token)
    content = str(payload.get("content") or "")
    if str(payload.get("encoding") or "").lower() == "base64":
        data = base64.b64decode(content)
    else:
        data = content.encode("utf-8")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(data)
    return data


def load_codeup_ref(
    org_id: str,
    project: dict[str, Any],
    ref: str,
    rules: Any,
    source_config: SourceConfig | None = None,
) -> dict[str, SourceFile]:
    token, domain, organization_id = codeup_context(org_id)
    base = codeup_repository_url(domain, organization_id, codeup_repo_path(project))
    tree = codeup_config_tree(base, token, domain, ref, str(project.get("name") or ""), source_config)
    entries: list[tuple[str, str, str]] = []
    for item in tree:
        if str(item.get("type") or "").lower() != "blob":
            continue
        raw_path = str(item.get("path") or "")
        if not included(raw_path, rules, source_config):
            continue
        relative = config_path(raw_path, source_config)
        entries.append((relative, raw_path, str(item.get("id") or "")))
    return download_codeup_files(base, token, domain, entries, ref, f"{project['name']}@{ref}")


def load_codeup_ref_pair(
    org_id: str,
    project: dict[str, Any],
    old_ref: str,
    new_ref: str,
    rules: Any,
    source_config: SourceConfig | None = None,
) -> tuple[dict[str, SourceFile], dict[str, SourceFile]]:
    token, domain, organization_id = codeup_context(org_id)
    base = codeup_repository_url(domain, organization_id, codeup_repo_path(project))
    project_name = str(project.get("name") or "")
    old_index = codeup_file_index(base, token, domain, old_ref, project_name, rules, source_config)
    new_index = codeup_file_index(base, token, domain, new_ref, project_name, rules, source_config)
    old_files: dict[str, SourceFile] = {}
    new_files: dict[str, SourceFile] = {}
    old_entries: list[tuple[str, str, str]] = []
    new_entries: list[tuple[str, str, str]] = []
    for relative in sorted(set(old_index) | set(new_index)):
        old_item = old_index.get(relative)
        new_item = new_index.get(relative)
        if old_item and new_item and old_item.get("id") == new_item.get("id"):
            continue
        if old_item:
            old_path = str(old_item.get("path") or "")
            old_entries.append((relative, old_path, str(old_item.get("id") or "")))
        if new_item:
            new_path = str(new_item.get("path") or "")
            new_entries.append((relative, new_path, str(new_item.get("id") or "")))
    old_files = download_codeup_files(base, token, domain, old_entries, old_ref, f"{project_name}@{old_ref}")
    new_files = download_codeup_files(base, token, domain, new_entries, new_ref, f"{project_name}@{new_ref}")
    return old_files, new_files


def download_codeup_files(
    base: str,
    token: str,
    domain: str,
    entries: list[tuple[str, str, str]],
    ref: str,
    label: str,
) -> dict[str, SourceFile]:
    if not entries:
        return {}
    max_workers = max(1, min(int(os.environ.get("YUNXIAO_DOWNLOAD_WORKERS", "16")), len(entries)))
    files: dict[str, SourceFile] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(cached_codeup_file_content, base, token, domain, raw_path, ref, content_id): (relative, raw_path)
            for relative, raw_path, content_id in entries
        }
        for future in as_completed(futures):
            relative, _raw_path = futures[future]
            files[relative] = SourceFile(path=relative, content=future.result(), label=label)
    return files


def cached_codeup_file_content(
    base: str,
    token: str,
    domain: str,
    file_path: str,
    ref: str,
    content_id: str = "",
) -> bytes:
    cache_path = codeup_file_cache_path(base, file_path, ref, content_id)
    if cache_path.exists():
        return cache_path.read_bytes()
    content = codeup_file_content(base, token, domain, file_path, ref)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(content)
    return content


def codeup_file_cache_path(base: str, file_path: str, ref: str, content_id: str = "") -> Path:
    stable_id = content_id or ref
    digest = hashlib.sha256(f"{base}\0{file_path}\0{stable_id}".encode("utf-8")).hexdigest()
    return TOOL_DIR / ".cache" / "codeup_files" / digest[:2] / digest


def codeup_file_index(
    base: str,
    token: str,
    domain: str,
    ref: str,
    project_name: str,
    rules: Any,
    source_config: SourceConfig | None = None,
) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    tree = codeup_config_tree(base, token, domain, ref, project_name, source_config)
    for item in tree:
        if str(item.get("type") or "").lower() != "blob":
            continue
        raw_path = str(item.get("path") or "")
        if not included(raw_path, rules, source_config):
            continue
        relative = config_path(raw_path, source_config)
        index[relative] = item
    return index


def codeup_config_tree(
    base: str,
    token: str,
    domain: str,
    ref: str,
    project_name: str,
    source_config: SourceConfig | None = None,
) -> list[dict[str, Any]]:
    candidates = source_tree_roots(source_config, project_name)
    seen_paths: set[str] = set()
    tree: list[dict[str, Any]] = []
    for path in candidates:
        if not path or path in seen_paths:
            continue
        seen_paths.add(path)
        try:
            items = codeup_get_all(
                base,
                "/files/tree",
                token,
                domain,
                {"path": path, "ref": ref, "type": "RECURSIVE", "page": 1, "perPage": 100},
                max_pages=int(os.environ.get("YUNXIAO_TREE_MAX_PAGES", "80")),
            )
        except ValueError:
            continue
        tree.extend(items)
    if tree:
        return tree
    if source_config:
        return []
    return codeup_get_all(
        base,
        "/files/tree",
        token,
        domain,
        {"ref": ref, "type": "RECURSIVE", "page": 1, "perPage": 100},
        max_pages=int(os.environ.get("YUNXIAO_TREE_FALLBACK_MAX_PAGES", "20")),
    )


def codeup_file_content(base: str, token: str, domain: str, file_path: str, ref: str) -> bytes:
    encoded_path = quote(file_path, safe="")
    payload, _ = codeup_get_json(f"{base}/files/{encoded_path}?{urlencode({'ref': ref})}", token, domain)
    content = str(payload.get("content") or "")
    if str(payload.get("encoding") or "").lower() == "base64":
        return base64.b64decode(content)
    return content.encode("utf-8")


def codeup_get_all(
    base: str,
    path: str,
    token: str,
    domain: str,
    params: dict[str, Any],
    max_pages: int | None = None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    page = int(params.get("page", 1) or 1)
    limit = max_pages if max_pages is not None else int(os.environ.get("YUNXIAO_MAX_PAGES", "2"))
    while page <= limit:
        params["page"] = page
        url = f"{base}{path}?{urlencode(params)}"
        payload, next_page = codeup_get_json(url, token, domain)
        page_items = collect_items(payload)
        if not page_items:
            break
        new_items = []
        for item in page_items:
            key = str(item.get("path") or item.get("id") or item)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            new_items.append(item)
        if not new_items:
            break
        items.extend(new_items)
        next_page_number = int(next_page) if next_page.isdigit() else 0
        if next_page_number > page:
            page = next_page_number
            continue
        page += 1
        if len(page_items) < int(params.get("perPage", 100) or 100):
            break
    return items


def collect_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("result", "data", "items", "list"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def codeup_project(org_id: str, project_name: str) -> dict[str, Any]:
    if not project_name:
        raise ValueError("project is required")
    workspace = remote_workspace(org_id)
    token, _domain, organization_id = codeup_context(org_id)
    projects = codeup_accessible_projects(organization_id, token, workspace)
    for project in projects:
        if project.get("name") == project_name:
            return project
    raise ValueError(f"Codeup 项目不存在或当前 token 无权限：{project_name}")


def codeup_context(org_id: str) -> tuple[str, str, str]:
    organization = remote_workspace(org_id) if org_id else {}
    organization_id = (
        str(organization.get("organization_id") or "")
        or org_id
        or os.environ.get("YUNXIAO_ORGANIZATION_ID")
        or os.environ.get("CODEUP_ORGANIZATION_ID")
        or str(organization.get("id") or "")
    )
    token = str(organization.get("token") or codeup_token())
    if not token:
        raise ValueError("缺少 Codeup token，无法读取未克隆项目")
    if not organization_id:
        raise ValueError("缺少 Codeup 组织 ID，无法读取未克隆项目")
    domain = str(organization.get("api_base") or os.environ.get("YUNXIAO_DOMAIN") or os.environ.get("CODEUP_DOMAIN") or "openapi-rdc.aliyuncs.com")
    return token, domain, organization_id


def codeup_repository_url(domain: str, organization_id: str, repo_path: str) -> str:
    return (
        f"https://{domain}/oapi/v1/codeup/organizations/"
        f"{quote(organization_id, safe='')}/repositories/{quote(repo_path, safe='')}"
    )


def codeup_repo_path(project: dict[str, Any]) -> str:
    full_path = str(project.get("path_with_namespace") or project.get("name_with_namespace") or "").strip().replace(" / ", "/")
    if full_path and "/" in full_path:
        return full_path
    space = project_space(project)
    if not space:
        raise ValueError(f"无法识别 Codeup 项目路径：{project.get('name')}")
    return f"{space}/{project['name']}"


def remote_workspaces() -> list[dict[str, Any]]:
    configured = provider_config_workspaces()
    legacy_codeup = legacy_codeup_workspaces()
    seen: set[str] = set()
    workspaces: list[dict[str, Any]] = []
    for workspace in [*configured, *legacy_codeup]:
        workspace_id = str(workspace.get("id") or "")
        if not workspace_id or workspace_id in seen:
            continue
        seen.add(workspace_id)
        workspaces.append(workspace)
    return workspaces


def remote_workspace(workspace_id: str) -> dict[str, Any]:
    workspaces = remote_workspaces()
    if workspace_id:
        for workspace in workspaces:
            if workspace.get("id") == workspace_id:
                return workspace
    if workspaces:
        return workspaces[0]
    raise ValueError("未配置远程代码平台账号。请配置 provider_accounts.json 或 .codeup.env。")


def provider_config_workspaces() -> list[dict[str, Any]]:
    path = Path(os.environ.get("CONFIG_DIFF_PROVIDER_CONFIG", PROVIDER_CONFIG))
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw_workspaces = payload.get("workspaces", [])
    if not isinstance(raw_workspaces, list):
        raise ValueError("provider_accounts.json 的 workspaces 必须是数组")
    workspaces: list[dict[str, Any]] = []
    for item in raw_workspaces:
        if not isinstance(item, dict):
            continue
        provider = str(item.get("provider") or "").strip().lower()
        if provider not in SUPPORTED_REMOTE_PROVIDERS:
            continue
        configured_name = str(item.get("name") or "").strip()
        workspace_key = str(item.get("owner") or item.get("group") or item.get("workspace") or item.get("organization_id") or configured_name or provider).strip()
        name = configured_name or workspace_key or provider
        workspace_id = str(item.get("id") or f"{provider}:{workspace_key or name}").strip()
        token = str(item.get("token") or os.environ.get(str(item.get("token_env") or ""), "") or "").strip()
        workspaces.append({
            **item,
            "id": workspace_id,
            "name": name,
            "provider": provider,
            "provider_label": item.get("provider_label") or provider_label(provider),
            "token": token,
            "namespace_id": str(item.get("namespace_id") or ""),
        })
    return workspaces


def legacy_codeup_workspaces() -> list[dict[str, Any]]:
    workspaces: list[dict[str, Any]] = []
    for org in codeup_organizations():
        org_id = str(org.get("id") or "")
        if not org_id:
            continue
        workspaces.append({
            "id": org_id,
            "name": org.get("name") or org_id,
            "provider": "codeup",
            "provider_label": "Codeup",
            "organization_id": org_id,
            "namespace_id": org.get("namespace_id", ""),
            "token": org.get("token", ""),
        })
    return workspaces


def provider_label(provider: str) -> str:
    return {
        "github": "GitHub",
        "codeup": "Codeup",
        "gitlab": "GitLab",
        "gitee": "Gitee",
        "bitbucket": "Bitbucket",
    }.get(provider, provider)


def projects_payload() -> dict[str, Any]:
    projects = local_projects()
    organizations = remote_workspaces()
    default_org = default_workspace_id(organizations)
    return {
        "root": str(PROJECT_ROOT),
        "default_project": DEFAULT_REPO.name,
        "default_org": default_org,
        "organizations": public_organizations(organizations),
        "projects": projects,
        "cloud_enabled": has_remote_token(organizations),
        "cloud_error": "",
        "cloud_pending": has_remote_token(organizations),
    }


def default_workspace_id(workspaces: list[dict[str, Any]]) -> str:
    configured_default = ""
    if PROVIDER_CONFIG.exists():
        try:
            payload = json.loads(PROVIDER_CONFIG.read_text(encoding="utf-8"))
            configured_default = str(payload.get("default_workspace") or "")
        except (OSError, json.JSONDecodeError):
            configured_default = ""
    legacy_default = os.environ.get("YUNXIAO_ORGANIZATION_ID") or os.environ.get("CODEUP_ORGANIZATION_ID") or ""
    for candidate in (configured_default, legacy_default):
        if candidate and any(workspace.get("id") == candidate for workspace in workspaces):
            return candidate
    return str(workspaces[0].get("id") or "") if workspaces else ""


def local_projects() -> list[dict[str, Any]]:
    projects: list[dict[str, Any]] = []
    if PROJECT_ROOT.exists():
        for child in sorted(PROJECT_ROOT.iterdir(), key=lambda item: item.name.lower()):
            if not child.is_dir() or not (child / ".git").exists():
                continue
            current_branch = ""
            remote_url = ""
            try:
                current_branch = git(child, "branch", "--show-current").strip()
            except subprocess.CalledProcessError:
                current_branch = ""
            try:
                remote_url = git(child, "remote", "get-url", "origin").strip()
            except subprocess.CalledProcessError:
                remote_url = ""
            projects.append({
                "name": child.name,
                "display_name": child.name,
                "path": str(child),
                "current_branch": current_branch,
                "selected": child.resolve() == DEFAULT_REPO.resolve(),
                "cloned": True,
                "source": "本机",
                "remote_url": remote_url,
                "org_id": infer_remote_workspace_id(remote_url),
            })
    return projects


def public_organizations(organizations: list[dict[str, str]]) -> list[dict[str, str]]:
    return [
        {
            "name": org.get("name", ""),
            "id": org.get("id", ""),
            "namespace_id": org.get("namespace_id", ""),
            "provider": org.get("provider", "codeup"),
            "provider_label": org.get("provider_label", provider_label(str(org.get("provider", "codeup")))),
            "owner": org.get("owner", ""),
        }
        for org in organizations
    ]


def cloud_projects_payload(org_override: str = "") -> dict[str, Any]:
    inferred_org = org_override or infer_org_from_local_projects() or default_workspace_id(remote_workspaces())
    selected_org = remote_workspace(inferred_org)
    org_label = selected_org.get("name", inferred_org)
    cloud_error = ""
    cloud_projects = []
    try:
        if selected_org.get("provider") == "codeup":
            cloud_projects = codeup_accessible_projects(inferred_org, selected_org.get("token", ""), selected_org)
        else:
            cloud_projects = remote_accessible_projects(selected_org)
    except Exception as exc:  # noqa: BLE001 - surface cloud sync errors in UI.
        cloud_error = f"{type(exc).__name__}: {exc}"
    return {
        "root": str(PROJECT_ROOT),
        "org": inferred_org,
        "org_name": org_label,
        "projects": sorted(cloud_projects, key=lambda item: item["name"].lower()),
        "cloud_enabled": has_remote_token(),
        "cloud_error": cloud_error,
    }


def infer_org_from_local_projects() -> str:
    for project in local_projects():
        org = infer_remote_workspace_id(str(project.get("remote_url", ""))) or infer_codeup_org(str(project.get("remote_url", "")))
        if org:
            return org
    return ""


def infer_remote_workspace_id(remote_url: str) -> str:
    if not remote_url:
        return ""
    parsed = urlparse(remote_url)
    host = parsed.netloc.lower()
    path_parts = [part.removesuffix(".git") for part in parsed.path.split("/") if part]
    for workspace in remote_workspaces():
        provider = workspace.get("provider")
        if provider == "github" and "github.com" in host and path_parts:
            owner = str(workspace.get("owner") or "").lower()
            if owner and path_parts[0].lower() == owner:
                return str(workspace.get("id") or "")
        if provider == "gitlab" and ("gitlab" in host or workspace_host_matches(workspace, host)) and path_parts:
            group = str(workspace.get("group") or workspace.get("owner") or "").lower()
            if not group or path_parts[0].lower() == group.split("/")[0]:
                return str(workspace.get("id") or "")
        if provider == "gitee" and ("gitee.com" in host or workspace_host_matches(workspace, host)) and path_parts:
            owner = str(workspace.get("owner") or "").lower()
            if not owner or path_parts[0].lower() == owner:
                return str(workspace.get("id") or "")
        if provider == "bitbucket" and ("bitbucket.org" in host or workspace_host_matches(workspace, host)) and path_parts:
            bitbucket_workspace = str(workspace.get("workspace") or workspace.get("owner") or "").lower()
            if not bitbucket_workspace or path_parts[0].lower() == bitbucket_workspace:
                return str(workspace.get("id") or "")
        if provider == "codeup" and "codeup.aliyun.com" in host and path_parts:
            allowed = {str(workspace.get("id") or ""), str(workspace.get("organization_id") or ""), str(workspace.get("name") or "")}
            if path_parts[0] in allowed:
                return str(workspace.get("id") or "")
    return ""


def resolve_project(name: str) -> Path:
    project_name = name or DEFAULT_REPO.name
    if "/" in project_name or "\\" in project_name or project_name in {"", ".", ".."}:
        raise ValueError("project is invalid")
    repo = (PROJECT_ROOT / project_name).resolve()
    root = PROJECT_ROOT.resolve()
    if root not in [repo, *repo.parents]:
        raise ValueError("project is outside allowed root")
    if not repo.is_dir() or not (repo / ".git").exists():
        raise ValueError(f"project is not a Git repo: {project_name}")
    return repo


def codeup_token() -> str:
    return os.environ.get("YUNXIAO_TOKEN") or os.environ.get("CODEUP_TOKEN") or ""


def has_codeup_token(organizations: list[dict[str, str]] | None = None) -> bool:
    return bool(codeup_token() or any(org.get("token") for org in (organizations or codeup_organizations())))


def has_remote_token(workspaces: list[dict[str, Any]] | None = None) -> bool:
    items = workspaces if workspaces is not None else remote_workspaces()
    env_tokens = (
        codeup_token()
        or os.environ.get("GITHUB_TOKEN")
        or os.environ.get("GITLAB_TOKEN")
        or os.environ.get("GITEE_TOKEN")
        or os.environ.get("BITBUCKET_TOKEN")
        or os.environ.get("BITBUCKET_APP_PASSWORD")
    )
    return any(workspace.get("token") or workspace.get("repositories") for workspace in items) or bool(env_tokens)


def codeup_organizations() -> list[dict[str, str]]:
    raw = os.environ.get("YUNXIAO_ORGANIZATIONS") or os.environ.get("CODEUP_ORGANIZATIONS") or ""
    organizations: list[dict[str, str]] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if "=" in item:
            name, org_id = item.split("=", 1)
        else:
            name = org_id = item
        name = name.strip()
        org_id = org_id.strip()
        token = ""
        if "|" in org_id:
            org_id, token = [part.strip() for part in org_id.split("|", 1)]
        namespace_id = ""
        if "#" in org_id:
            org_id, namespace_id = [part.strip() for part in org_id.split("#", 1)]
        if org_id:
            organizations.append({"name": name or org_id, "id": org_id, "namespace_id": namespace_id, "token": token})
    fallback = os.environ.get("YUNXIAO_ORGANIZATION_ID") or os.environ.get("CODEUP_ORGANIZATION_ID") or infer_codeup_org_from_local_git()
    if fallback and not any(org["id"] == fallback for org in organizations):
        organizations.append({"name": fallback, "id": fallback, "namespace_id": "", "token": ""})
    return organizations


def infer_codeup_org_from_local_git() -> str:
    if not PROJECT_ROOT.exists():
        return ""
    for child in sorted(PROJECT_ROOT.iterdir(), key=lambda item: item.name.lower()):
        if not child.is_dir() or not (child / ".git").exists():
            continue
        try:
            remote_url = git(child, "remote", "get-url", "origin").strip()
        except subprocess.CalledProcessError:
            continue
        org = infer_codeup_org(remote_url)
        if org:
            return org
    return ""


def codeup_accessible_projects(inferred_org: str, token_override: str = "", workspace: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    token = token_override or codeup_token()
    if not token:
        return []
    workspace = workspace or {}
    organization_id = str(workspace.get("organization_id") or "") or inferred_org or os.environ.get("YUNXIAO_ORGANIZATION_ID") or os.environ.get("CODEUP_ORGANIZATION_ID")
    if not organization_id:
        raise ValueError("缺少 YUNXIAO_ORGANIZATION_ID，且无法从本机 Git remote 推断组织 ID")
    domain = str(workspace.get("api_base") or os.environ.get("YUNXIAO_DOMAIN") or os.environ.get("CODEUP_DOMAIN") or "openapi-rdc.aliyuncs.com")
    namespace_id = str(workspace.get("namespace_id") or next((org.get("namespace_id", "") for org in codeup_organizations() if org["id"] == organization_id), ""))
    return codeup_repository_projects(domain, organization_id, token, namespace_id)


def codeup_repository_projects(domain: str, organization_id: str, token: str, namespace_id: str = "") -> list[dict[str, Any]]:
    repos: dict[str, dict[str, Any]] = {}
    page = 1
    max_pages = int(os.environ.get("YUNXIAO_MAX_PAGES", "2"))
    while page <= max_pages:
        namespace_query = f"&namespaceId={quote(namespace_id)}" if namespace_id else ""
        url = f"https://{domain}/oapi/v1/codeup/organizations/{quote(organization_id)}/repositories?page={page}&perPage=100{namespace_query}"
        payload, next_page = codeup_get_json(url, token, domain)
        repository_infos = collect_repository_infos(payload)
        if not repository_infos and isinstance(payload, list):
            repository_infos = [item for item in payload if isinstance(item, dict)]
        if not repository_infos:
            break
        add_codeup_repos(repos, repository_infos)
        next_page_number = int(next_page) if next_page.isdigit() else 0
        if next_page_number > page:
            page = next_page_number
            continue
        page += 1
        if len(repository_infos) < 100:
            break
    return list(repos.values())


def github_context(workspace: dict[str, Any]) -> tuple[str, str]:
    token = str(workspace.get("token") or os.environ.get("GITHUB_TOKEN") or "").strip()
    api_base = str(workspace.get("api_base") or "https://api.github.com").rstrip("/")
    return token, api_base


def github_accessible_projects(workspace: dict[str, Any]) -> list[dict[str, Any]]:
    configured_repos = workspace.get("repositories")
    if isinstance(configured_repos, list) and configured_repos:
        return [github_project_from_config(workspace, item) for item in configured_repos if isinstance(item, dict)]
    token, api_base = github_context(workspace)
    if not token:
        raise ValueError("缺少 GitHub token，无法遍历 GitHub 仓库；也可以在 repositories 中配置固定项目清单。")
    owner = str(workspace.get("owner") or "").strip()
    owner_type = str(workspace.get("owner_type") or "org").strip().lower()
    list_mode = str(workspace.get("list_mode") or "").strip().lower()
    if list_mode == "viewer" or not owner:
        repos = github_get_all(
            f"{api_base}/user/repos",
            token,
            {"affiliation": "owner,collaborator,organization_member", "visibility": "all", "sort": "updated", "per_page": 100},
            max_pages=int(os.environ.get("GITHUB_MAX_PAGES", "10")),
        )
        if owner:
            repos = [repo for repo in repos if str((repo.get("owner") or {}).get("login") or "").lower() == owner.lower()]
    elif owner_type == "user":
        repos = github_get_all(f"{api_base}/users/{quote(owner, safe='')}/repos", token, {"type": "all", "sort": "updated", "per_page": 100})
    else:
        repos = github_get_all(f"{api_base}/orgs/{quote(owner, safe='')}/repos", token, {"type": "all", "sort": "updated", "per_page": 100})
    return [github_project_from_api(workspace, repo) for repo in repos if isinstance(repo, dict)]


def github_project(workspace: dict[str, Any], project_name: str) -> dict[str, Any]:
    for project in github_accessible_projects(workspace):
        if project.get("name") == project_name or project.get("full_name") == project_name:
            return project
    raise ValueError(f"GitHub 项目不存在或当前 token 无权限：{project_name}")


def github_project_from_config(workspace: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    owner = str(item.get("owner") or workspace.get("owner") or "").strip()
    repo = str(item.get("repo") or item.get("name") or "").strip()
    full_name = str(item.get("full_name") or f"{owner}/{repo}" if owner and repo else repo)
    return {
        "id": str(item.get("id") or full_name),
        "name": repo or full_name.rsplit("/", 1)[-1],
        "display_name": str(item.get("display_name") or repo or full_name),
        "full_name": full_name,
        "owner": owner,
        "repo": repo or full_name.rsplit("/", 1)[-1],
        "path_with_namespace": full_name,
        "namespace_id": str(workspace.get("id") or ""),
        "remote_url": str(item.get("remote_url") or f"https://github.com/{full_name}.git"),
        "web_url": str(item.get("web_url") or f"https://github.com/{full_name}"),
        "default_branch": str(item.get("default_branch") or ""),
        "provider": "github",
        "provider_label": workspace.get("provider_label") or "GitHub",
        "org_id": workspace.get("id"),
    }


def github_project_from_api(workspace: dict[str, Any], repo: dict[str, Any]) -> dict[str, Any]:
    owner = str((repo.get("owner") or {}).get("login") or workspace.get("owner") or "")
    name = str(repo.get("name") or "")
    return {
        "id": str(repo.get("id") or repo.get("node_id") or f"{owner}/{name}"),
        "name": name,
        "display_name": name,
        "full_name": str(repo.get("full_name") or f"{owner}/{name}"),
        "owner": owner,
        "repo": name,
        "path_with_namespace": str(repo.get("full_name") or f"{owner}/{name}"),
        "namespace_id": str(workspace.get("id") or ""),
        "remote_url": str(repo.get("clone_url") or ""),
        "web_url": str(repo.get("html_url") or ""),
        "default_branch": str(repo.get("default_branch") or ""),
        "last_activity_at": str(repo.get("pushed_at") or repo.get("updated_at") or ""),
        "provider": "github",
        "provider_label": workspace.get("provider_label") or "GitHub",
        "org_id": workspace.get("id"),
    }


def github_repo_owner_name(workspace: dict[str, Any], project: dict[str, Any]) -> tuple[str, str]:
    owner = str(project.get("owner") or workspace.get("owner") or "").strip()
    repo = str(project.get("repo") or project.get("name") or "").strip()
    full_name = str(project.get("full_name") or project.get("path_with_namespace") or "").strip()
    if full_name and "/" in full_name:
        owner, repo = full_name.split("/", 1)
    if not owner or not repo:
        raise ValueError(f"GitHub 项目路径配置不完整：{project.get('name')}")
    return owner, repo


def github_get_all(
    url: str,
    token: str,
    params: dict[str, Any] | None = None,
    max_pages: int | None = None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    page = int((params or {}).get("page", 1) or 1)
    limit = max_pages if max_pages is not None else int(os.environ.get("GITHUB_MAX_PAGES", "10"))
    params = dict(params or {})
    while page <= limit:
        params["page"] = page
        separator = "&" if "?" in url else "?"
        payload = github_get_json(f"{url}{separator}{urlencode(params)}", token)
        page_items = payload if isinstance(payload, list) else collect_items(payload)
        if not page_items:
            break
        items.extend([item for item in page_items if isinstance(item, dict)])
        if len(page_items) < int(params.get("per_page", 100) or 100):
            break
        page += 1
    return items


def github_get_json(url: str, token: str) -> Any:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "ConfigDiffGuard",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, headers=headers)
    timeout = float(os.environ.get("GITHUB_TIMEOUT", "12"))
    attempts = max(1, int(os.environ.get("GITHUB_RETRIES", "3")))
    for attempt in range(attempts):
        try:
            with urlopen(request, timeout=timeout) as response:  # noqa: S310 - configured GitHub endpoint.
                body = response.read().decode("utf-8", errors="replace")
                return json.loads(body) if body else {}
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                error_payload = json.loads(body)
                message = error_payload.get("message") or body
            except json.JSONDecodeError:
                message = body[:160]
            if exc.code not in {429, 500, 502, 503, 504} or attempt == attempts - 1:
                raise ValueError(f"GitHub API 调用失败：{exc.code}，{message}") from exc
        except URLError:
            if attempt == attempts - 1:
                raise
        time.sleep(0.25 * (attempt + 1))
    raise ValueError("GitHub API 调用失败：重试后仍无响应")


def remote_accessible_projects(workspace: dict[str, Any]) -> list[dict[str, Any]]:
    provider = str(workspace.get("provider") or "").lower()
    if provider == "github":
        return github_accessible_projects(workspace)
    if provider == "gitlab":
        return gitlab_accessible_projects(workspace)
    if provider == "gitee":
        return gitee_accessible_projects(workspace)
    if provider == "bitbucket":
        return bitbucket_accessible_projects(workspace)
    raise ValueError(f"暂不支持的平台：{provider}")


def remote_project_from_list(workspace: dict[str, Any], project_name: str) -> dict[str, Any]:
    provider = str(workspace.get("provider") or "远程平台")
    for project in remote_accessible_projects(workspace):
        if project.get("name") == project_name or project.get("full_name") == project_name or project.get("path_with_namespace") == project_name:
            return project
    raise ValueError(f"{provider_label(provider)} 项目不存在或当前 token 无权限：{project_name}")


def workspace_host_matches(workspace: dict[str, Any], host: str) -> bool:
    api_base = str(workspace.get("api_base") or "").strip()
    web_base = str(workspace.get("web_base") or "").strip()
    hosts = {urlparse(value).netloc.lower() for value in (api_base, web_base) if value}
    return host.lower() in hosts


def remote_cache_path(provider: str, project_key: str, file_path: str, ref: str, content_id: str = "") -> Path:
    stable_id = content_id or ref
    digest = hashlib.sha256(f"{provider}\0{project_key}\0{file_path}\0{stable_id}".encode("utf-8")).hexdigest()
    return TOOL_DIR / ".cache" / f"{provider}_files" / digest[:2] / digest


def platform_get_json(url: str, headers: dict[str, str], provider_name: str, timeout_env: str, retries_env: str) -> tuple[Any, dict[str, str]]:
    request = Request(url, headers=headers)
    timeout = float(os.environ.get(timeout_env, "12"))
    attempts = max(1, int(os.environ.get(retries_env, "3")))
    for attempt in range(attempts):
        try:
            with urlopen(request, timeout=timeout) as response:  # noqa: S310 - configured code hosting endpoint.
                body = response.read().decode("utf-8", errors="replace")
                return (json.loads(body) if body else {}), {key.lower(): value for key, value in response.headers.items()}
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                error_payload = json.loads(body)
                message = error_payload.get("message") or error_payload.get("error") or error_payload.get("error_description") or body
            except json.JSONDecodeError:
                message = body[:160]
            if exc.code not in {429, 500, 502, 503, 504} or attempt == attempts - 1:
                raise ValueError(f"{provider_name} API 调用失败：{exc.code}，{message}") from exc
        except (URLError, json.JSONDecodeError) as exc:
            if attempt == attempts - 1:
                raise ValueError(f"{provider_name} API 调用失败：{exc}") from exc
        time.sleep(0.25 * (attempt + 1))
    raise ValueError(f"{provider_name} API 调用失败：重试后仍无响应")


def platform_get_bytes(url: str, headers: dict[str, str], provider_name: str, timeout_env: str, retries_env: str) -> bytes:
    request = Request(url, headers=headers)
    timeout = float(os.environ.get(timeout_env, "12"))
    attempts = max(1, int(os.environ.get(retries_env, "3")))
    for attempt in range(attempts):
        try:
            with urlopen(request, timeout=timeout) as response:  # noqa: S310 - configured code hosting endpoint.
                return response.read()
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if exc.code not in {429, 500, 502, 503, 504} or attempt == attempts - 1:
                raise ValueError(f"{provider_name} 文件下载失败：{exc.code}，{body[:160]}") from exc
        except URLError as exc:
            if attempt == attempts - 1:
                raise ValueError(f"{provider_name} 文件下载失败：{exc}") from exc
        time.sleep(0.25 * (attempt + 1))
    raise ValueError(f"{provider_name} 文件下载失败：重试后仍无响应")


def gitlab_context(workspace: dict[str, Any]) -> tuple[str, str]:
    token = str(workspace.get("token") or os.environ.get("GITLAB_TOKEN") or "").strip()
    api_base = str(workspace.get("api_base") or "https://gitlab.com/api/v4").rstrip("/")
    return token, api_base


def gitlab_headers(token: str) -> dict[str, str]:
    headers = {"Accept": "application/json", "User-Agent": "ConfigDiffGuard"}
    if token:
        headers["PRIVATE-TOKEN"] = token
    return headers


def gitlab_accessible_projects(workspace: dict[str, Any]) -> list[dict[str, Any]]:
    configured_repos = workspace.get("repositories")
    if isinstance(configured_repos, list) and configured_repos:
        return [gitlab_project_from_config(workspace, item) for item in configured_repos if isinstance(item, dict)]
    token, api_base = gitlab_context(workspace)
    if not token:
        raise ValueError("缺少 GitLab token，无法遍历 GitLab 项目；也可以在 repositories 中配置固定项目清单。")
    group = str(workspace.get("group") or workspace.get("owner") or "").strip()
    if group:
        url = f"{api_base}/groups/{quote(group, safe='')}/projects"
        projects = gitlab_get_all(url, token, {"include_subgroups": "true", "simple": "true", "per_page": 100})
    else:
        projects = gitlab_get_all(f"{api_base}/projects", token, {"membership": "true", "simple": "true", "per_page": 100})
    return [gitlab_project_from_api(workspace, project) for project in projects]


def gitlab_project_from_config(workspace: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    group = str(item.get("group") or workspace.get("group") or workspace.get("owner") or "").strip().strip("/")
    repo = str(item.get("repo") or item.get("name") or "").strip()
    full_name = str(item.get("full_name") or item.get("path_with_namespace") or (f"{group}/{repo}" if group and repo else repo)).strip()
    project_id = str(item.get("project_id") or item.get("api_id") or item.get("id") or full_name).strip()
    web_base = str(workspace.get("web_base") or "https://gitlab.com").rstrip("/")
    return {
        "id": project_id,
        "api_id": project_id,
        "name": repo or full_name.rsplit("/", 1)[-1],
        "display_name": repo or full_name,
        "full_name": full_name,
        "path_with_namespace": full_name,
        "owner": group,
        "repo": repo or full_name.rsplit("/", 1)[-1],
        "remote_url": str(item.get("remote_url") or f"{web_base}/{full_name}.git"),
        "web_url": str(item.get("web_url") or f"{web_base}/{full_name}"),
        "default_branch": str(item.get("default_branch") or ""),
        "provider": "gitlab",
        "provider_label": workspace.get("provider_label") or "GitLab",
        "org_id": workspace.get("id"),
    }


def gitlab_project_from_api(workspace: dict[str, Any], project: dict[str, Any]) -> dict[str, Any]:
    full_name = str(project.get("path_with_namespace") or "")
    namespace = project.get("namespace") if isinstance(project.get("namespace"), dict) else {}
    owner = str(namespace.get("full_path") or full_name.rsplit("/", 1)[0] if "/" in full_name else "")
    name = str(project.get("path") or project.get("name") or full_name.rsplit("/", 1)[-1])
    return {
        "id": str(project.get("id") or full_name),
        "api_id": str(project.get("id") or full_name),
        "name": name,
        "display_name": str(project.get("name") or name),
        "full_name": full_name,
        "path_with_namespace": full_name,
        "owner": owner,
        "repo": name,
        "remote_url": str(project.get("http_url_to_repo") or project.get("ssh_url_to_repo") or ""),
        "web_url": str(project.get("web_url") or ""),
        "default_branch": str(project.get("default_branch") or ""),
        "last_activity_at": str(project.get("last_activity_at") or ""),
        "provider": "gitlab",
        "provider_label": workspace.get("provider_label") or "GitLab",
        "org_id": workspace.get("id"),
    }


def gitlab_refs(workspace: dict[str, Any], project_name: str) -> dict[str, Any]:
    project = remote_project_from_list(workspace, project_name)
    token, api_base = gitlab_context(workspace)
    base = f"{api_base}/projects/{quote(gitlab_project_id(project), safe='')}"
    branches = gitlab_get_all(f"{base}/repository/branches", token, {"per_page": 100})
    tags = gitlab_get_all(f"{base}/repository/tags", token, {"per_page": 100})
    default_branch = str(project.get("default_branch") or "")
    default_branch = default_branch or next((str(item.get("name") or "") for item in branches if item.get("default")), "")
    branch_names = [str(item.get("name", "")).strip() for item in branches if str(item.get("name", "")).strip()]
    commit_ref = default_branch or (branch_names[0] if branch_names else "")
    commits = gitlab_commits(base, token, commit_ref) if commit_ref else []
    return remote_refs_payload(project, default_branch, branch_names, [str(item.get("name", "")).strip() for item in tags if str(item.get("name", "")).strip()], commits)


def gitlab_commits(base: str, token: str, ref_name: str) -> list[dict[str, str]]:
    payload = gitlab_get_all(f"{base}/repository/commits", token, {"ref_name": ref_name, "per_page": 80}, max_pages=1)
    return [
        {
            "short": str(item.get("short_id") or str(item.get("id") or "")[:8]),
            "hash": str(item.get("id") or ""),
            "date": str(item.get("committed_date") or item.get("created_at") or ""),
            "author": str(item.get("author_name") or item.get("committer_name") or ""),
            "subject": str(item.get("title") or str(item.get("message") or "").splitlines()[0] if item.get("message") else ""),
        }
        for item in payload
        if str(item.get("id") or "")
    ]


def load_gitlab_ref_pair(
    workspace: dict[str, Any],
    project: dict[str, Any],
    old_ref: str,
    new_ref: str,
    rules: Any,
    source_config: SourceConfig | None = None,
) -> tuple[dict[str, SourceFile], dict[str, SourceFile]]:
    old_index = gitlab_file_index(workspace, project, old_ref, rules, source_config)
    new_index = gitlab_file_index(workspace, project, new_ref, rules, source_config)
    old_entries, new_entries = changed_remote_entries(old_index, new_index, "id")
    return (
        download_gitlab_files(workspace, project, old_entries, old_ref, f"{project['name']}@{old_ref}"),
        download_gitlab_files(workspace, project, new_entries, new_ref, f"{project['name']}@{new_ref}"),
    )


def gitlab_file_index(workspace: dict[str, Any], project: dict[str, Any], ref: str, rules: Any, source_config: SourceConfig | None) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for item in gitlab_config_tree(workspace, project, ref, source_config):
        if str(item.get("type") or "").lower() not in {"blob", "file"}:
            continue
        raw_path = str(item.get("path") or "")
        if not included(raw_path, rules, source_config):
            continue
        index[config_path(raw_path, source_config)] = item
    return index


def gitlab_config_tree(workspace: dict[str, Any], project: dict[str, Any], ref: str, source_config: SourceConfig | None) -> list[dict[str, Any]]:
    token, api_base = gitlab_context(workspace)
    base = f"{api_base}/projects/{quote(gitlab_project_id(project), safe='')}/repository/tree"
    if not source_config:
        return gitlab_get_all(base, token, {"ref": ref, "recursive": "true", "per_page": 100}, max_pages=int(os.environ.get("GITLAB_TREE_MAX_PAGES", "100")))
    tree: list[dict[str, Any]] = []
    for root in source_tree_roots(source_config, str(project.get("name") or "")):
        try:
            tree.extend(gitlab_get_all(base, token, {"ref": ref, "path": root, "recursive": "true", "per_page": 100}, max_pages=int(os.environ.get("GITLAB_TREE_MAX_PAGES", "100"))))
        except ValueError as exc:
            if "404" not in str(exc):
                raise
    return tree


def download_gitlab_files(workspace: dict[str, Any], project: dict[str, Any], entries: list[tuple[str, str, str]], ref: str, label: str) -> dict[str, SourceFile]:
    if not entries:
        return {}
    token, api_base = gitlab_context(workspace)
    project_id = gitlab_project_id(project)
    max_workers = max(1, min(int(os.environ.get("REMOTE_DOWNLOAD_WORKERS", "16")), len(entries)))
    files: dict[str, SourceFile] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(cached_gitlab_file_content, api_base, token, project_id, raw_path, ref, content_id): (relative, raw_path)
            for relative, raw_path, content_id in entries
        }
        for future in as_completed(futures):
            relative, _raw_path = futures[future]
            files[relative] = SourceFile(path=relative, content=future.result(), label=label)
    return files


def cached_gitlab_file_content(api_base: str, token: str, project_id: str, file_path: str, ref: str, content_id: str) -> bytes:
    cache_path = remote_cache_path("gitlab", project_id, file_path, ref, content_id)
    if cache_path.exists():
        return cache_path.read_bytes()
    url = f"{api_base}/projects/{quote(project_id, safe='')}/repository/blobs/{quote(content_id, safe='')}/raw"
    data = platform_get_bytes(url, gitlab_headers(token), "GitLab", "GITLAB_TIMEOUT", "GITLAB_RETRIES")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(data)
    return data


def gitlab_get_all(url: str, token: str, params: dict[str, Any], max_pages: int | None = None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    page = int(params.get("page", 1) or 1)
    limit = max_pages if max_pages is not None else int(os.environ.get("GITLAB_MAX_PAGES", "10"))
    params = dict(params)
    while page <= limit:
        params["page"] = page
        separator = "&" if "?" in url else "?"
        payload, headers = platform_get_json(f"{url}{separator}{urlencode(params)}", gitlab_headers(token), "GitLab", "GITLAB_TIMEOUT", "GITLAB_RETRIES")
        page_items = payload if isinstance(payload, list) else collect_items(payload)
        if not page_items:
            break
        items.extend([item for item in page_items if isinstance(item, dict)])
        next_page = str(headers.get("x-next-page") or "").strip()
        if next_page.isdigit() and int(next_page) > page:
            page = int(next_page)
            continue
        if len(page_items) < int(params.get("per_page", 100) or 100):
            break
        page += 1
    return items


def gitlab_project_id(project: dict[str, Any]) -> str:
    return str(project.get("api_id") or project.get("id") or project.get("path_with_namespace") or project.get("full_name") or "").strip()


def gitee_context(workspace: dict[str, Any]) -> tuple[str, str]:
    token = str(workspace.get("token") or os.environ.get("GITEE_TOKEN") or "").strip()
    api_base = str(workspace.get("api_base") or "https://gitee.com/api/v5").rstrip("/")
    return token, api_base


def gitee_url(url: str, token: str, params: dict[str, Any] | None = None) -> str:
    params = dict(params or {})
    if token:
        params.setdefault("access_token", token)
    if not params:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{urlencode(params)}"


def gitee_accessible_projects(workspace: dict[str, Any]) -> list[dict[str, Any]]:
    configured_repos = workspace.get("repositories")
    if isinstance(configured_repos, list) and configured_repos:
        return [gitee_project_from_config(workspace, item) for item in configured_repos if isinstance(item, dict)]
    token, api_base = gitee_context(workspace)
    if not token:
        raise ValueError("缺少 Gitee token，无法遍历 Gitee 仓库；也可以在 repositories 中配置固定项目清单。")
    owner = str(workspace.get("owner") or "").strip()
    owner_type = str(workspace.get("owner_type") or "org").strip().lower()
    list_mode = str(workspace.get("list_mode") or "").strip().lower()
    if list_mode == "viewer" or not owner:
        repos = gitee_get_all(f"{api_base}/user/repos", token, {"type": "all", "sort": "updated", "per_page": 100})
        if owner:
            repos = [repo for repo in repos if gitee_repo_owner(repo).lower() == owner.lower()]
    elif owner_type == "user":
        repos = gitee_get_all(f"{api_base}/users/{quote(owner, safe='')}/repos", token, {"type": "all", "sort": "updated", "per_page": 100})
    else:
        repos = gitee_get_all(f"{api_base}/orgs/{quote(owner, safe='')}/repos", token, {"type": "all", "sort": "updated", "per_page": 100})
    return [gitee_project_from_api(workspace, repo) for repo in repos]


def gitee_project_from_config(workspace: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    owner = str(item.get("owner") or workspace.get("owner") or "").strip()
    repo = str(item.get("repo") or item.get("name") or "").strip()
    full_name = str(item.get("full_name") or item.get("path_with_namespace") or (f"{owner}/{repo}" if owner and repo else repo)).strip()
    return {
        "id": str(item.get("id") or full_name),
        "name": repo or full_name.rsplit("/", 1)[-1],
        "display_name": str(item.get("display_name") or repo or full_name),
        "full_name": full_name,
        "path_with_namespace": full_name,
        "owner": owner,
        "repo": repo or full_name.rsplit("/", 1)[-1],
        "remote_url": str(item.get("remote_url") or f"https://gitee.com/{full_name}.git"),
        "web_url": str(item.get("web_url") or f"https://gitee.com/{full_name}"),
        "default_branch": str(item.get("default_branch") or ""),
        "provider": "gitee",
        "provider_label": workspace.get("provider_label") or "Gitee",
        "org_id": workspace.get("id"),
    }


def gitee_project_from_api(workspace: dict[str, Any], repo: dict[str, Any]) -> dict[str, Any]:
    owner = gitee_repo_owner(repo) or str(workspace.get("owner") or "")
    name = str(repo.get("path") or repo.get("name") or "")
    full_name = str(repo.get("full_name") or repo.get("path_with_namespace") or f"{owner}/{name}")
    return {
        "id": str(repo.get("id") or full_name),
        "name": name,
        "display_name": str(repo.get("human_name") or repo.get("name") or name),
        "full_name": full_name,
        "path_with_namespace": full_name,
        "owner": owner,
        "repo": name,
        "remote_url": str(repo.get("html_url") or f"https://gitee.com/{full_name}") + ".git",
        "web_url": str(repo.get("html_url") or f"https://gitee.com/{full_name}"),
        "default_branch": str(repo.get("default_branch") or ""),
        "last_activity_at": str(repo.get("updated_at") or repo.get("pushed_at") or ""),
        "provider": "gitee",
        "provider_label": workspace.get("provider_label") or "Gitee",
        "org_id": workspace.get("id"),
    }


def gitee_refs(workspace: dict[str, Any], project_name: str) -> dict[str, Any]:
    project = remote_project_from_list(workspace, project_name)
    token, api_base = gitee_context(workspace)
    owner, repo = gitee_owner_repo(workspace, project)
    base = f"{api_base}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
    branches = gitee_get_all(f"{base}/branches", token, {"per_page": 100})
    tags = gitee_get_all(f"{base}/tags", token, {"per_page": 100})
    default_branch = str(project.get("default_branch") or "")
    branch_names = [str(item.get("name", "")).strip() for item in branches if str(item.get("name", "")).strip()]
    commit_ref = default_branch or (branch_names[0] if branch_names else "")
    commits = gitee_commits(base, token, commit_ref) if commit_ref else []
    return remote_refs_payload(project, default_branch, branch_names, [str(item.get("name", "")).strip() for item in tags if str(item.get("name", "")).strip()], commits)


def gitee_commits(base: str, token: str, ref_name: str) -> list[dict[str, str]]:
    payload = gitee_get_all(f"{base}/commits", token, {"sha": ref_name, "per_page": 80}, max_pages=1)
    commits: list[dict[str, str]] = []
    for item in payload:
        commit_id = str(item.get("sha") or "").strip()
        commit = item.get("commit") if isinstance(item.get("commit"), dict) else {}
        author = commit.get("author") if isinstance(commit.get("author"), dict) else {}
        message = str(commit.get("message") or "")
        if commit_id:
            commits.append({"short": commit_id[:8], "hash": commit_id, "date": str(author.get("date") or ""), "author": str(author.get("name") or ""), "subject": message.splitlines()[0] if message else ""})
    return commits


def load_gitee_ref_pair(
    workspace: dict[str, Any],
    project: dict[str, Any],
    old_ref: str,
    new_ref: str,
    rules: Any,
    source_config: SourceConfig | None = None,
) -> tuple[dict[str, SourceFile], dict[str, SourceFile]]:
    old_index = gitee_file_index(workspace, project, old_ref, rules, source_config)
    new_index = gitee_file_index(workspace, project, new_ref, rules, source_config)
    old_entries, new_entries = changed_remote_entries(old_index, new_index, "sha")
    return (
        download_gitee_files(workspace, project, old_entries, old_ref, f"{project['name']}@{old_ref}"),
        download_gitee_files(workspace, project, new_entries, new_ref, f"{project['name']}@{new_ref}"),
    )


def gitee_file_index(workspace: dict[str, Any], project: dict[str, Any], ref: str, rules: Any, source_config: SourceConfig | None) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for item in gitee_config_tree(workspace, project, ref, source_config):
        raw_path = str(item.get("path") or "")
        if not included(raw_path, rules, source_config):
            continue
        index[config_path(raw_path, source_config)] = item
    return index


def gitee_config_tree(workspace: dict[str, Any], project: dict[str, Any], ref: str, source_config: SourceConfig | None) -> list[dict[str, Any]]:
    roots = source_tree_roots(source_config, str(project.get("name") or ""))
    tree: list[dict[str, Any]] = []
    for root in roots:
        tree.extend(gitee_walk_contents(workspace, project, ref, root))
    if tree or source_config:
        return tree
    return gitee_walk_contents(workspace, project, ref, "")


def gitee_walk_contents(workspace: dict[str, Any], project: dict[str, Any], ref: str, path: str) -> list[dict[str, Any]]:
    token, api_base = gitee_context(workspace)
    owner, repo = gitee_owner_repo(workspace, project)
    suffix = f"/{quote(path.strip('/'), safe='/')}" if path else ""
    url = gitee_url(f"{api_base}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/contents{suffix}", token, {"ref": ref})
    try:
        payload, _ = platform_get_json(url, {"Accept": "application/json", "User-Agent": "ConfigDiffGuard"}, "Gitee", "GITEE_TIMEOUT", "GITEE_RETRIES")
    except ValueError as exc:
        if "404" in str(exc):
            return []
        raise
    items = payload if isinstance(payload, list) else [payload] if isinstance(payload, dict) else []
    files: list[dict[str, Any]] = []
    for item in items:
        item_type = str(item.get("type") or "").lower()
        item_path = str(item.get("path") or "")
        if item_type in {"dir", "tree"}:
            files.extend(gitee_walk_contents(workspace, project, ref, item_path))
        elif item_type in {"file", "blob"}:
            files.append(item)
    return files


def download_gitee_files(workspace: dict[str, Any], project: dict[str, Any], entries: list[tuple[str, str, str]], ref: str, label: str) -> dict[str, SourceFile]:
    if not entries:
        return {}
    max_workers = max(1, min(int(os.environ.get("REMOTE_DOWNLOAD_WORKERS", "16")), len(entries)))
    files: dict[str, SourceFile] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(cached_gitee_file_content, workspace, project, raw_path, ref, content_id): (relative, raw_path)
            for relative, raw_path, content_id in entries
        }
        for future in as_completed(futures):
            relative, _raw_path = futures[future]
            files[relative] = SourceFile(path=relative, content=future.result(), label=label)
    return files


def cached_gitee_file_content(workspace: dict[str, Any], project: dict[str, Any], file_path: str, ref: str, content_id: str) -> bytes:
    owner, repo = gitee_owner_repo(workspace, project)
    project_key = f"{owner}/{repo}"
    cache_path = remote_cache_path("gitee", project_key, file_path, ref, content_id)
    if cache_path.exists():
        return cache_path.read_bytes()
    token, api_base = gitee_context(workspace)
    suffix = quote(file_path.strip("/"), safe="/")
    url = gitee_url(f"{api_base}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/contents/{suffix}", token, {"ref": ref})
    payload, _ = platform_get_json(url, {"Accept": "application/json", "User-Agent": "ConfigDiffGuard"}, "Gitee", "GITEE_TIMEOUT", "GITEE_RETRIES")
    content = str(payload.get("content") or "") if isinstance(payload, dict) else ""
    data = base64.b64decode(content) if str(payload.get("encoding") or "").lower() == "base64" else content.encode("utf-8")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(data)
    return data


def gitee_get_all(url: str, token: str, params: dict[str, Any], max_pages: int | None = None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    page = int(params.get("page", 1) or 1)
    limit = max_pages if max_pages is not None else int(os.environ.get("GITEE_MAX_PAGES", "10"))
    params = dict(params)
    while page <= limit:
        params["page"] = page
        payload, _headers = platform_get_json(gitee_url(url, token, params), {"Accept": "application/json", "User-Agent": "ConfigDiffGuard"}, "Gitee", "GITEE_TIMEOUT", "GITEE_RETRIES")
        page_items = payload if isinstance(payload, list) else collect_items(payload)
        if not page_items:
            break
        items.extend([item for item in page_items if isinstance(item, dict)])
        if len(page_items) < int(params.get("per_page", 100) or 100):
            break
        page += 1
    return items


def gitee_repo_owner(repo: dict[str, Any]) -> str:
    namespace = repo.get("namespace") if isinstance(repo.get("namespace"), dict) else {}
    owner = repo.get("owner") if isinstance(repo.get("owner"), dict) else {}
    return str(namespace.get("path") or namespace.get("name") or owner.get("login") or owner.get("name") or "")


def gitee_owner_repo(workspace: dict[str, Any], project: dict[str, Any]) -> tuple[str, str]:
    owner = str(project.get("owner") or workspace.get("owner") or "").strip()
    repo = str(project.get("repo") or project.get("name") or "").strip()
    full_name = str(project.get("full_name") or project.get("path_with_namespace") or "").strip()
    if full_name and "/" in full_name:
        owner, repo = full_name.split("/", 1)
    if not owner or not repo:
        raise ValueError(f"Gitee 项目路径配置不完整：{project.get('name')}")
    return owner, repo


def bitbucket_context(workspace: dict[str, Any]) -> tuple[str, str, str]:
    token = str(workspace.get("token") or os.environ.get("BITBUCKET_TOKEN") or os.environ.get("BITBUCKET_APP_PASSWORD") or "").strip()
    username_env = str(workspace.get("username_env") or "").strip()
    username = str(workspace.get("username") or (os.environ.get(username_env) if username_env else "") or os.environ.get("BITBUCKET_USERNAME") or "").strip()
    api_base = str(workspace.get("api_base") or "https://api.bitbucket.org/2.0").rstrip("/")
    return token, username, api_base


def bitbucket_headers(workspace: dict[str, Any]) -> dict[str, str]:
    token, username, _api_base = bitbucket_context(workspace)
    headers = {"Accept": "application/json", "User-Agent": "ConfigDiffGuard"}
    if token and username:
        raw = base64.b64encode(f"{username}:{token}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {raw}"
    elif token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def bitbucket_accessible_projects(workspace: dict[str, Any]) -> list[dict[str, Any]]:
    configured_repos = workspace.get("repositories")
    if isinstance(configured_repos, list) and configured_repos:
        return [bitbucket_project_from_config(workspace, item) for item in configured_repos if isinstance(item, dict)]
    token, _username, api_base = bitbucket_context(workspace)
    bitbucket_workspace = str(workspace.get("workspace") or workspace.get("owner") or "").strip()
    if not token:
        raise ValueError("缺少 Bitbucket token 或 app password，无法遍历 Bitbucket 仓库；也可以在 repositories 中配置固定项目清单。")
    if not bitbucket_workspace:
        raise ValueError("缺少 Bitbucket workspace，无法遍历 Bitbucket 仓库；也可以在 repositories 中配置固定项目清单。")
    repos = bitbucket_get_all(f"{api_base}/repositories/{quote(bitbucket_workspace, safe='')}", workspace, {"pagelen": 100, "role": "member"})
    return [bitbucket_project_from_api(workspace, repo) for repo in repos]


def bitbucket_project_from_config(workspace: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    bitbucket_workspace = str(item.get("workspace") or workspace.get("workspace") or workspace.get("owner") or "").strip()
    repo_slug = str(item.get("repo_slug") or item.get("repo") or item.get("name") or "").strip()
    full_name = str(item.get("full_name") or f"{bitbucket_workspace}/{repo_slug}" if bitbucket_workspace and repo_slug else repo_slug)
    return {
        "id": str(item.get("uuid") or item.get("id") or full_name),
        "name": repo_slug or full_name.rsplit("/", 1)[-1],
        "display_name": str(item.get("display_name") or item.get("name") or repo_slug or full_name),
        "full_name": full_name,
        "path_with_namespace": full_name,
        "owner": bitbucket_workspace,
        "workspace": bitbucket_workspace,
        "repo": repo_slug or full_name.rsplit("/", 1)[-1],
        "repo_slug": repo_slug or full_name.rsplit("/", 1)[-1],
        "remote_url": str(item.get("remote_url") or f"https://bitbucket.org/{full_name}.git"),
        "web_url": str(item.get("web_url") or f"https://bitbucket.org/{full_name}"),
        "default_branch": str(item.get("default_branch") or ""),
        "provider": "bitbucket",
        "provider_label": workspace.get("provider_label") or "Bitbucket",
        "org_id": workspace.get("id"),
    }


def bitbucket_project_from_api(workspace: dict[str, Any], repo: dict[str, Any]) -> dict[str, Any]:
    bitbucket_workspace = str(((repo.get("workspace") if isinstance(repo.get("workspace"), dict) else {}) or {}).get("slug") or workspace.get("workspace") or workspace.get("owner") or "")
    repo_slug = str(repo.get("slug") or repo.get("name") or "")
    links = repo.get("links") if isinstance(repo.get("links"), dict) else {}
    html_link = links.get("html") if isinstance(links.get("html"), dict) else {}
    default_branch = repo.get("mainbranch") if isinstance(repo.get("mainbranch"), dict) else {}
    return {
        "id": str(repo.get("uuid") or f"{bitbucket_workspace}/{repo_slug}"),
        "name": repo_slug,
        "display_name": str(repo.get("name") or repo_slug),
        "full_name": str(repo.get("full_name") or f"{bitbucket_workspace}/{repo_slug}"),
        "path_with_namespace": str(repo.get("full_name") or f"{bitbucket_workspace}/{repo_slug}"),
        "owner": bitbucket_workspace,
        "workspace": bitbucket_workspace,
        "repo": repo_slug,
        "repo_slug": repo_slug,
        "remote_url": str((links.get("clone") or [{}])[0].get("href") if isinstance(links.get("clone"), list) and links.get("clone") else ""),
        "web_url": str(html_link.get("href") or ""),
        "default_branch": str(default_branch.get("name") or ""),
        "last_activity_at": str(repo.get("updated_on") or ""),
        "provider": "bitbucket",
        "provider_label": workspace.get("provider_label") or "Bitbucket",
        "org_id": workspace.get("id"),
    }


def bitbucket_refs(workspace: dict[str, Any], project_name: str) -> dict[str, Any]:
    project = remote_project_from_list(workspace, project_name)
    _token, _username, api_base = bitbucket_context(workspace)
    owner, repo = bitbucket_owner_repo(workspace, project)
    base = f"{api_base}/repositories/{quote(owner, safe='')}/{quote(repo, safe='')}"
    branches = bitbucket_get_all(f"{base}/refs/branches", workspace, {"pagelen": 100})
    tags = bitbucket_get_all(f"{base}/refs/tags", workspace, {"pagelen": 100})
    default_branch = str(project.get("default_branch") or "")
    branch_names = [str(item.get("name", "")).strip() for item in branches if str(item.get("name", "")).strip()]
    commit_ref = default_branch or (branch_names[0] if branch_names else "")
    commits = bitbucket_commits(base, workspace, commit_ref) if commit_ref else []
    return remote_refs_payload(project, default_branch, branch_names, [str(item.get("name", "")).strip() for item in tags if str(item.get("name", "")).strip()], commits)


def bitbucket_commits(base: str, workspace: dict[str, Any], ref_name: str) -> list[dict[str, str]]:
    payload = bitbucket_get_all(f"{base}/commits/{quote(ref_name, safe='')}", workspace, {"pagelen": 80}, max_pages=1)
    commits: list[dict[str, str]] = []
    for item in payload:
        commit_id = str(item.get("hash") or "").strip()
        author = item.get("author") if isinstance(item.get("author"), dict) else {}
        message = str(item.get("message") or "")
        if commit_id:
            commits.append({"short": commit_id[:8], "hash": commit_id, "date": str(item.get("date") or ""), "author": str(author.get("raw") or ((author.get("user") or {}) if isinstance(author.get("user"), dict) else {}).get("display_name") or ""), "subject": message.splitlines()[0] if message else ""})
    return commits


def load_bitbucket_ref_pair(
    workspace: dict[str, Any],
    project: dict[str, Any],
    old_ref: str,
    new_ref: str,
    rules: Any,
    source_config: SourceConfig | None = None,
) -> tuple[dict[str, SourceFile], dict[str, SourceFile]]:
    old_index = bitbucket_file_index(workspace, project, old_ref, rules, source_config)
    new_index = bitbucket_file_index(workspace, project, new_ref, rules, source_config)
    old_entries, new_entries = changed_remote_entries(old_index, new_index, "id")
    return (
        download_bitbucket_files(workspace, project, old_entries, old_ref, f"{project['name']}@{old_ref}"),
        download_bitbucket_files(workspace, project, new_entries, new_ref, f"{project['name']}@{new_ref}"),
    )


def bitbucket_file_index(workspace: dict[str, Any], project: dict[str, Any], ref: str, rules: Any, source_config: SourceConfig | None) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for item in bitbucket_config_tree(workspace, project, ref, source_config):
        raw_path = str(item.get("path") or "")
        if not included(raw_path, rules, source_config):
            continue
        index[config_path(raw_path, source_config)] = item
    return index


def bitbucket_config_tree(workspace: dict[str, Any], project: dict[str, Any], ref: str, source_config: SourceConfig | None) -> list[dict[str, Any]]:
    roots = source_tree_roots(source_config, str(project.get("name") or ""))
    tree: list[dict[str, Any]] = []
    for root in roots:
        tree.extend(bitbucket_walk_src(workspace, project, ref, root))
    if tree or source_config:
        return tree
    return bitbucket_walk_src(workspace, project, ref, "")


def bitbucket_walk_src(workspace: dict[str, Any], project: dict[str, Any], ref: str, path: str) -> list[dict[str, Any]]:
    _token, _username, api_base = bitbucket_context(workspace)
    owner, repo = bitbucket_owner_repo(workspace, project)
    base = f"{api_base}/repositories/{quote(owner, safe='')}/{quote(repo, safe='')}/src/{quote(ref, safe='')}"
    suffix = f"/{quote(path.strip('/'), safe='/')}" if path else "/"
    try:
        items = bitbucket_get_all(f"{base}{suffix}", workspace, {"pagelen": 100})
    except ValueError as exc:
        if "404" in str(exc) or "JSON" in str(exc):
            return []
        raise
    files: list[dict[str, Any]] = []
    for item in items:
        item_type = str(item.get("type") or "").lower()
        item_path = str(item.get("path") or "")
        if item_type == "commit_directory":
            files.extend(bitbucket_walk_src(workspace, project, ref, item_path))
        elif item_type == "commit_file":
            content_id = str(((item.get("commit") if isinstance(item.get("commit"), dict) else {}) or {}).get("hash") or item.get("path") or "")
            files.append({**item, "id": content_id})
    return files


def download_bitbucket_files(workspace: dict[str, Any], project: dict[str, Any], entries: list[tuple[str, str, str]], ref: str, label: str) -> dict[str, SourceFile]:
    if not entries:
        return {}
    max_workers = max(1, min(int(os.environ.get("REMOTE_DOWNLOAD_WORKERS", "16")), len(entries)))
    files: dict[str, SourceFile] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(cached_bitbucket_file_content, workspace, project, raw_path, ref, content_id): (relative, raw_path)
            for relative, raw_path, content_id in entries
        }
        for future in as_completed(futures):
            relative, _raw_path = futures[future]
            files[relative] = SourceFile(path=relative, content=future.result(), label=label)
    return files


def cached_bitbucket_file_content(workspace: dict[str, Any], project: dict[str, Any], file_path: str, ref: str, content_id: str) -> bytes:
    owner, repo = bitbucket_owner_repo(workspace, project)
    project_key = f"{owner}/{repo}"
    cache_path = remote_cache_path("bitbucket", project_key, file_path, ref, content_id)
    if cache_path.exists():
        return cache_path.read_bytes()
    _token, _username, api_base = bitbucket_context(workspace)
    url = f"{api_base}/repositories/{quote(owner, safe='')}/{quote(repo, safe='')}/src/{quote(ref, safe='')}/{quote(file_path.strip('/'), safe='/')}"
    data = platform_get_bytes(url, bitbucket_headers(workspace), "Bitbucket", "BITBUCKET_TIMEOUT", "BITBUCKET_RETRIES")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(data)
    return data


def bitbucket_get_all(url: str, workspace: dict[str, Any], params: dict[str, Any], max_pages: int | None = None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    page = 1
    limit = max_pages if max_pages is not None else int(os.environ.get("BITBUCKET_MAX_PAGES", "10"))
    current_url = f"{url}{'&' if '?' in url else '?'}{urlencode(params)}"
    while current_url and page <= limit:
        payload, _headers = platform_get_json(current_url, bitbucket_headers(workspace), "Bitbucket", "BITBUCKET_TIMEOUT", "BITBUCKET_RETRIES")
        page_items = payload.get("values", []) if isinstance(payload, dict) else []
        if not page_items:
            break
        items.extend([item for item in page_items if isinstance(item, dict)])
        current_url = str(payload.get("next") or "") if isinstance(payload, dict) else ""
        page += 1
    return items


def bitbucket_owner_repo(workspace: dict[str, Any], project: dict[str, Any]) -> tuple[str, str]:
    owner = str(project.get("workspace") or project.get("owner") or workspace.get("workspace") or workspace.get("owner") or "").strip()
    repo = str(project.get("repo_slug") or project.get("repo") or project.get("name") or "").strip()
    full_name = str(project.get("full_name") or project.get("path_with_namespace") or "").strip()
    if full_name and "/" in full_name:
        owner, repo = full_name.split("/", 1)
    if not owner or not repo:
        raise ValueError(f"Bitbucket 项目路径配置不完整：{project.get('name')}")
    return owner, repo


def remote_refs_payload(project: dict[str, Any], default_branch: str, branch_names: list[str], tags: list[str], commits: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "project": project["name"],
        "repo": project.get("web_url") or project.get("remote_url") or project.get("path_with_namespace") or project.get("full_name") or project["name"],
        "source": "cloud",
        "provider": project.get("provider", ""),
        "provider_label": project.get("provider_label") or provider_label(str(project.get("provider", ""))),
        "current_branch": default_branch,
        "local_branches": [],
        "remote_branches": branch_names,
        "tags": tags,
        "commits": commits,
    }


def changed_remote_entries(
    old_index: dict[str, dict[str, Any]],
    new_index: dict[str, dict[str, Any]],
    content_key: str,
) -> tuple[list[tuple[str, str, str]], list[tuple[str, str, str]]]:
    old_entries: list[tuple[str, str, str]] = []
    new_entries: list[tuple[str, str, str]] = []
    for relative in sorted(set(old_index) | set(new_index)):
        old_item = old_index.get(relative)
        new_item = new_index.get(relative)
        old_id = str(old_item.get(content_key) or old_item.get("id") or old_item.get("sha") or "") if old_item else ""
        new_id = str(new_item.get(content_key) or new_item.get("id") or new_item.get("sha") or "") if new_item else ""
        if old_item and new_item and old_id and old_id == new_id:
            continue
        if old_item:
            old_entries.append((relative, str(old_item.get("path") or ""), old_id))
        if new_item:
            new_entries.append((relative, str(new_item.get("path") or ""), new_id))
    return old_entries, new_entries


def codeup_get_json(url: str, token: str, domain: str) -> tuple[Any, str]:
    request = Request(url, headers={"Content-Type": "application/json", "x-yunxiao-token": token})
    timeout = float(os.environ.get("YUNXIAO_TIMEOUT", "8"))
    attempts = max(1, int(os.environ.get("YUNXIAO_RETRIES", "3")))
    for attempt in range(attempts):
        try:
            with urlopen(request, timeout=timeout) as response:  # noqa: S310 - configured Codeup endpoint.
                body = response.read().decode("utf-8", errors="replace")
                content_type = response.headers.get("content-type", "")
                if "json" not in content_type.lower():
                    raise ValueError(
                        f"Codeup 返回的不是 JSON，domain 可能配置错了：{domain}，"
                        f"content-type={content_type}，响应开头={body[:80]!r}"
                    )
                return json.loads(body), response.headers.get("x-next-page", "").strip()
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                error_payload = json.loads(body)
                message = error_payload.get("errorMessage") or error_payload.get("message") or body
                code = error_payload.get("errorCode") or exc.code
            except json.JSONDecodeError:
                message = body[:160]
                code = exc.code
            if exc.code not in {429, 500, 502, 503, 504} or attempt == attempts - 1:
                raise ValueError(f"Codeup OpenAPI 调用失败：{code}，{message}") from exc
        except URLError:
            if attempt == attempts - 1:
                raise
        time.sleep(0.25 * (attempt + 1))
    raise ValueError("Codeup OpenAPI 调用失败：重试后仍无响应")


def add_codeup_repos(target: dict[str, dict[str, Any]], repository_infos: list[dict[str, Any]]) -> None:
    for repo in repository_infos:
        name = str(repo.get("name") or repo.get("path") or "").strip()
        remote_url = str(repo.get("httpUrlToRepo") or repo.get("sshUrlToRepo") or "").strip()
        if not name:
            continue
        target[name] = {
            "id": str(repo.get("id") or ""),
            "name": name,
            "display_name": name,
            "path_with_namespace": str(repo.get("pathWithNamespace") or repo.get("nameWithNamespace") or repo.get("name_with_namespace") or ""),
            "namespace_id": str(repo.get("namespaceId") or repo.get("namespace_id") or ""),
            "remote_url": remote_url,
            "web_url": str(repo.get("webUrl") or ""),
            "default_branch": str(repo.get("defaultBranch") or ""),
            "last_activity_at": str(repo.get("lastActivityAt") or repo.get("updatedAt") or ""),
        }


def collect_repository_infos(payload: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            repo = value.get("repository_info")
            if isinstance(repo, dict):
                found.append(repo)
            elif value.get("httpUrlToRepo") and value.get("name"):
                found.append(value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(payload)
    return found


def infer_codeup_org(remote_url: str) -> str:
    if not remote_url:
        return ""
    parsed = urlparse(remote_url)
    if "codeup.aliyun.com" not in parsed.netloc:
        return ""
    parts = [part for part in parsed.path.split("/") if part]
    return parts[0] if parts else ""


def project_space(project: dict[str, Any]) -> str:
    return infer_codeup_org(str(project.get("web_url") or project.get("remote_url") or ""))


def recent_commits(repo: Path) -> list[dict[str, str]]:
    output = git(
        repo,
        "log",
        "--all",
        "--date=format:%Y-%m-%d %H:%M",
        "--pretty=format:%h%x09%H%x09%ad%x09%an%x09%s",
        "-n",
        "120",
    )
    commits = []
    seen: set[str] = set()
    for line in output.splitlines():
        parts = line.split("\t", 4)
        if len(parts) != 5 or parts[1] in seen:
            continue
        seen.add(parts[1])
        commits.append(
            {
                "short": parts[0],
                "hash": parts[1],
                "date": parts[2],
                "author": parts[3],
                "subject": parts[4],
            }
        )
    return commits


def result_to_payload(result: Any, limit: int) -> dict[str, Any]:
    changes = sorted(
        result.changes,
        key=lambda change: (
            {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}.get(change.severity.value, 9),
            change.table,
            change.key,
            change.field,
        ),
    )
    return {
        "old": result.old_label,
        "new": result.new_label,
        "generated": result.generated_at,
        "stats": result.stats.__dict__,
        "tables": result.table_summaries,
        "changes": [
            {
                "severity": change.severity.value,
                "type": change.type.value,
                "table": change.table,
                "key": change.key,
                "field": change.field,
                "old": change.old,
                "new": change.new,
                "reason": change.reason,
            }
            for change in changes
        ],
        "total_changes": len(result.changes),
    }


def retain_display_changes(changes: list[Any], limit: int) -> list[Any]:
    protected_types = {"added_row", "removed_row", "added_file", "removed_file", "schema_changed", "validation"}
    sorted_changes = sorted(
        changes,
        key=lambda change: (
            {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}.get(change.severity.value, 9),
            change.table,
            change.key,
            change.field,
        ),
    )
    kept: list[Any] = []
    seen: set[tuple[Any, ...]] = set()

    def add(change: Any) -> None:
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


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.STDOUT).decode("utf-8", errors="replace")


def lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


def run_server(host: str = "127.0.0.1", start_port: int = 8765, open_browser: bool = True) -> None:
    port = start_port
    while True:
        try:
            server = ThreadingHTTPServer((host, port), DiffRequestHandler)
            break
        except OSError:
            if port >= start_port + 34:
                raise RuntimeError(f"无法启动本地服务：{start_port}-{start_port + 34} 端口都不可用")
            port += 1
    local_url = f"http://127.0.0.1:{port}/"
    display_urls = [local_url]
    if host in {"0.0.0.0", "::"}:
        display_urls.extend(f"http://{ip}:{port}/" for ip in lan_ips())
    else:
        display_urls = [f"http://{host}:{port}/"]
    if open_browser:
        threading.Thread(target=lambda: (time.sleep(0.3), webbrowser.open(local_url)), daemon=True).start()
    print("配置对比工具已启动：")
    for url in dict.fromkeys(display_urls):
        print(f"  {url}")
    if host in {"0.0.0.0", "::"}:
        print("局域网分享模式：同事需和你在同一网络，并使用上面的内网地址访问。")
    print("关闭这个终端窗口即可停止服务。")
    server.serve_forever()


def lan_ips() -> list[str]:
    ips: set[str] = set()
    try:
        host_name = socket.gethostname()
        for info in socket.getaddrinfo(host_name, None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                ips.add(ip)
    except OSError:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
            if not ip.startswith("127."):
                ips.add(ip)
    except OSError:
        pass
    return sorted(ips)


def main() -> None:
    parser = argparse.ArgumentParser(description="启动配置对比工具本地服务")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址。局域网分享使用 0.0.0.0")
    parser.add_argument("--port", type=int, default=8765, help="起始端口，默认 8765")
    parser.add_argument("--no-open", action="store_true", help="启动后不自动打开浏览器")
    args = parser.parse_args()
    run_server(host=args.host, start_port=args.port, open_browser=not args.no_open)


if __name__ == "__main__":
    main()
