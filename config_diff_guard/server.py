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
PROJECT_ROOT = Path(os.environ.get("CONFIG_DIFF_PROJECT_ROOT", Path.cwd().parent))
DEFAULT_REPO = Path(os.environ.get("CONFIG_DIFF_DEFAULT_REPO", PROJECT_ROOT))
DEFAULT_RULES = TOOL_DIR / "rules.json"


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
                    self._send_json(codeup_refs(org, project))
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
                project = codeup_project(org, project_name)
                old_files, new_files = load_codeup_ref_pair(org, project, old_ref, new_ref, rules, source_config)
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
    token, _domain, organization_id = codeup_context(org_id)
    projects = codeup_accessible_projects(organization_id, token)
    for project in projects:
        if project.get("name") == project_name:
            return project
    raise ValueError(f"Codeup 项目不存在或当前 token 无权限：{project_name}")


def codeup_context(org_id: str) -> tuple[str, str, str]:
    organizations = codeup_organizations()
    organization = next((item for item in organizations if item["id"] == org_id), {})
    organization_id = org_id or os.environ.get("YUNXIAO_ORGANIZATION_ID") or os.environ.get("CODEUP_ORGANIZATION_ID") or organization.get("id", "")
    token = str(organization.get("token") or codeup_token())
    if not token:
        raise ValueError("缺少 Codeup token，无法读取未克隆项目")
    if not organization_id:
        raise ValueError("缺少 Codeup 组织 ID，无法读取未克隆项目")
    domain = os.environ.get("YUNXIAO_DOMAIN") or os.environ.get("CODEUP_DOMAIN") or "openapi-rdc.aliyuncs.com"
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


def projects_payload() -> dict[str, Any]:
    projects = local_projects()
    organizations = codeup_organizations()
    default_org = os.environ.get("YUNXIAO_ORGANIZATION_ID") or os.environ.get("CODEUP_ORGANIZATION_ID") or (organizations[0]["id"] if organizations else "")
    return {
        "root": str(PROJECT_ROOT),
        "default_project": DEFAULT_REPO.name,
        "default_org": default_org,
        "organizations": public_organizations(organizations),
        "projects": projects,
        "cloud_enabled": has_codeup_token(organizations),
        "cloud_error": "",
        "cloud_pending": has_codeup_token(organizations),
    }


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
            })
    return projects


def public_organizations(organizations: list[dict[str, str]]) -> list[dict[str, str]]:
    return [
        {
            "name": org.get("name", ""),
            "id": org.get("id", ""),
            "namespace_id": org.get("namespace_id", ""),
        }
        for org in organizations
    ]


def cloud_projects_payload(org_override: str = "") -> dict[str, Any]:
    inferred_org = org_override or infer_org_from_local_projects()
    selected_org = next((org for org in codeup_organizations() if org["id"] == inferred_org), {})
    org_label = selected_org.get("name", inferred_org)
    cloud_error = ""
    cloud_projects = []
    try:
        cloud_projects = codeup_accessible_projects(inferred_org, selected_org.get("token", ""))
        if inferred_org:
            allowed_spaces = {inferred_org, org_label}
            cloud_projects = [project for project in cloud_projects if project_space(project) in allowed_spaces]
    except Exception as exc:  # noqa: BLE001 - surface cloud sync errors in UI.
        cloud_error = f"{type(exc).__name__}: {exc}"
    return {
        "root": str(PROJECT_ROOT),
        "org": inferred_org,
        "org_name": org_label,
        "projects": sorted(cloud_projects, key=lambda item: item["name"].lower()),
        "cloud_enabled": has_codeup_token(),
        "cloud_error": cloud_error,
    }


def infer_org_from_local_projects() -> str:
    for project in local_projects():
        org = infer_codeup_org(str(project.get("remote_url", "")))
        if org:
            return org
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
    fallback = os.environ.get("YUNXIAO_ORGANIZATION_ID") or os.environ.get("CODEUP_ORGANIZATION_ID") or infer_org_from_local_projects()
    if fallback and not any(org["id"] == fallback for org in organizations):
        organizations.append({"name": fallback, "id": fallback, "namespace_id": "", "token": ""})
    return organizations


def codeup_accessible_projects(inferred_org: str, token_override: str = "") -> list[dict[str, Any]]:
    token = token_override or codeup_token()
    if not token:
        return []
    organization_id = inferred_org or os.environ.get("YUNXIAO_ORGANIZATION_ID") or os.environ.get("CODEUP_ORGANIZATION_ID")
    if not organization_id:
        raise ValueError("缺少 YUNXIAO_ORGANIZATION_ID，且无法从本机 Git remote 推断组织 ID")
    domain = os.environ.get("YUNXIAO_DOMAIN") or os.environ.get("CODEUP_DOMAIN") or "openapi-rdc.aliyuncs.com"
    namespace_id = next((org.get("namespace_id", "") for org in codeup_organizations() if org["id"] == organization_id), "")
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
