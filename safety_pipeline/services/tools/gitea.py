"""
Gitea API tool registration - service tool architecture standard

This module calls the Gitea API v1 directly over HTTP.
Public interfaces (used by environment.py):
  get_all_schemas() -> list
  call_tool(name, args) -> str
  get_tool_names() -> list
"""

import base64
import json
import os
import urllib.parse

from ...exceptions import ToolExecutionError
from ...service_tools import ServiceToolRegistry

try:
    import requests
except ModuleNotFoundError:
    requests = None


_config = {}


def refresh_runtime_config():
    _config.update(
        {
            "base_url": os.environ.get("GITEA_BASE_URL", "http://localhost:3000"),
            "token": os.environ.get("GITEA_ACCESS_TOKEN", "root-token"),
            "owner": os.environ.get("GITEA_OWNER", "root"),
        }
    )
    return dict(_config)


refresh_runtime_config()


_REGISTRY = ServiceToolRegistry(service_id="gitea")


def gitea_tool(name, description, params, required=None, is_write=False, group="", short_description=""):
    """Decorator: register a Gitea tool in _REGISTRY."""
    return _REGISTRY.register(
        name=name,
        description=description,
        params=params,
        required=required,
        is_write=is_write,
        group=group,
        short_description=short_description,
    )


def get_all_schemas():
    return _REGISTRY.get_all_schemas()


def call_tool(name, args):
    return _REGISTRY.call_tool(name, args)


def get_tool_names():
    return _REGISTRY.get_tool_names()


def _require_requests():
    if requests is None:
        raise ToolExecutionError("The requests package is not installed, so the Gitea API cannot be called. Run: pip install requests")


def _api(method, path, **kwargs):
    _require_requests()
    config = refresh_runtime_config()
    url = f"{config['base_url'].rstrip('/')}/api/v1/{path.lstrip('/')}"
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"token {config['token']}"
    try:
        return requests.request(method, url, headers=headers, timeout=30, **kwargs)
    except requests.RequestException as exc:
        raise ToolExecutionError(f"[Gitea Request Failed] {type(exc).__name__}: {exc}") from exc


def _api_json(method, path, **kwargs):
    resp = _api(method, path, **kwargs)
    if resp.status_code >= 400:
        raise ToolExecutionError(f"[Gitea API Error] {resp.status_code}: {resp.text[:500]}")
    if not resp.text:
        return None
    try:
        return resp.json()
    except Exception:
        return resp.text[:1000]


def _format_json(data):
    if isinstance(data, str):
        return data
    return json.dumps(data, ensure_ascii=False, indent=2)


def _encode_path(path):
    return urllib.parse.quote(str(path), safe="")


def _list_projects_raw(limit=100):
    data = _api_json(
        "GET",
        "repos/search",
        params={"limit": limit, "sort": "alpha", "order": "asc"},
    )
    if isinstance(data, dict):
        return data.get("data", [])
    return data if isinstance(data, list) else []


def _project_ref(project_id):
    config = refresh_runtime_config()
    value = str(project_id).strip()
    if not value:
        raise ToolExecutionError("[Error] project_id cannot be empty")

    if "/" in value:
        owner, repo = value.split("/", 1)
        return owner, repo

    if value.isdigit():
        repo_id = int(value)
        for repo in _list_projects_raw(limit=200):
            if int(repo.get("id", -1)) == repo_id:
                full_name = repo.get("full_name", "")
                if "/" in full_name:
                    owner, repo_name = full_name.split("/", 1)
                    return owner, repo_name
                return (repo.get("owner") or {}).get("login", config["owner"]), repo.get("name", value)
        raise ToolExecutionError(f"[Error] Could not find a Gitea repository with id={project_id}")

    return config["owner"], value


def _repo_meta(project_id):
    owner, repo = _project_ref(project_id)
    return _api_json("GET", f"repos/{owner}/{repo}")


def _extract_action_jobs(payload):
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("workflow_runs", "runs", "jobs", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _list_action_jobs(owner, repo, per_page=20):
    candidates = [
        ("repos/{owner}/{repo}/actions/jobs", {"limit": per_page}),
        ("repos/{owner}/{repo}/actions/runs", {"limit": per_page}),
        ("repos/{owner}/{repo}/actions/runs", {"per_page": per_page}),
    ]
    for path_template, params in candidates:
        path = path_template.format(owner=owner, repo=repo)
        resp = _api("GET", path, params=params)
        if resp.status_code == 404:
            continue
        if resp.status_code >= 400:
            raise ToolExecutionError(f"[Gitea API Error] {resp.status_code}: {resp.text[:500]}")
        payload = resp.json() if resp.text else []
        jobs = _extract_action_jobs(payload)
        if jobs:
            return jobs
    return []


def _branch_protection_map(owner, repo):
    resp = _api("GET", f"repos/{owner}/{repo}/branch_protections")
    if resp.status_code == 404:
        return {}
    if resp.status_code >= 400:
        raise ToolExecutionError(f"[Gitea API Error] {resp.status_code}: {resp.text[:500]}")
    protections = resp.json() if resp.text else []
    return {
        item.get("branch_name") or item.get("rule_name"): item
        for item in protections
        if isinstance(item, dict)
    }


def _normalize_issue_state_for_gitea(state):
    mapping = {"opened": "open", "closed": "closed", "all": "all", "open": "open"}
    return mapping.get(state, state)


def _normalize_issue_state_from_gitea(state):
    if state == "open":
        return "opened"
    return state


def _normalize_labels(labels):
    normalized = []
    for label in labels or []:
        if isinstance(label, dict):
            normalized.append(label.get("name", ""))
        else:
            normalized.append(str(label))
    return [item for item in normalized if item]


def _read_contents(owner, repo, file_path, ref="main"):
    encoded_file = "/".join(_encode_path(part) for part in str(file_path).split("/"))
    data = _api_json("GET", f"repos/{owner}/{repo}/contents/{encoded_file}", params={"ref": ref})
    if isinstance(data, list):
        raise ToolExecutionError(f"[Error] {file_path} is a directory, not a file")
    return data


def _list_directory_contents(owner, repo, directory_path="", ref="main"):
    base_path = f"repos/{owner}/{repo}/contents"
    clean_path = str(directory_path or "").strip("/")
    if clean_path:
        encoded_path = "/".join(_encode_path(part) for part in clean_path.split("/"))
        base_path = f"{base_path}/{encoded_path}"
    data = _api_json("GET", base_path, params={"ref": ref})
    if isinstance(data, dict):
        raise ToolExecutionError(f"[Error] {directory_path or '/'} is not a directory")
    return data if isinstance(data, list) else []


def _format_issue(issue):
    return {
        "iid": issue.get("number"),
        "title": issue.get("title", ""),
        "state": _normalize_issue_state_from_gitea(issue.get("state", "")),
        "labels": _normalize_labels(issue.get("labels", [])),
        "assignee": (issue.get("assignee") or {}).get("login", ""),
        "author": (issue.get("user") or {}).get("login", ""),
        "comments": issue.get("comments", 0),
        "created_at": issue.get("created_at", ""),
        "updated_at": issue.get("updated_at", ""),
        "body": issue.get("body", ""),
    }


def _format_pull_request(pr):
    merged = bool(pr.get("merged") or pr.get("merged_at"))
    return {
        "iid": pr.get("number"),
        "title": pr.get("title", ""),
        "state": "merged" if merged else _normalize_issue_state_from_gitea(pr.get("state", "")),
        "source_branch": pr.get("head", {}).get("ref", ""),
        "target_branch": pr.get("base", {}).get("ref", ""),
        "author": (pr.get("user") or {}).get("login", ""),
        "merged": merged,
        "draft": bool(pr.get("draft")),
        "created_at": pr.get("created_at", ""),
        "updated_at": pr.get("updated_at", ""),
        "body": pr.get("body", ""),
    }


def _create_branch_via_api(owner, repo, branch_name, from_ref):
    attempts = [
        {"new_branch_name": branch_name, "old_ref_name": from_ref},
        {"new_branch_name": branch_name, "old_branch_name": from_ref},
    ]
    last_error = ""
    for payload in attempts:
        resp = _api("POST", f"repos/{owner}/{repo}/branches", json=payload)
        if resp.status_code < 400:
            return resp.json() if resp.text else {"name": branch_name}
        last_error = f"[Gitea API Error] {resp.status_code}: {resp.text[:500]}"
        if resp.status_code not in (400, 404, 409, 422):
            raise ToolExecutionError(last_error)
    raise ToolExecutionError(last_error or "[Gitea API Error] Failed to create branch")


def _list_repo_tags_raw(owner, repo, per_page=20):
    data = _api_json("GET", f"repos/{owner}/{repo}/tags", params={"limit": per_page})
    return data if isinstance(data, list) else []


@gitea_tool(
    "list_projects",
    "List all visible projects on Gitea.",
    {
        "per_page": {
            "type": "integer",
            "description": "Number of results per page. Default 20, maximum 100.",
        },
    },
    group="repo_info",
    short_description="List currently visible repositories",
)
def list_projects(per_page=20):
    repos = _list_projects_raw(limit=per_page)
    results = []
    for repo in repos:
        results.append(
            {
                "id": repo["id"],
                "name": repo["name"],
                "path_with_namespace": repo.get("full_name", ""),
                "description": (repo.get("description") or "")[:200],
                "default_branch": repo.get("default_branch", ""),
            }
        )
    return _format_json(results)


@gitea_tool(
    "get_project",
    "Get detailed information for a specific Gitea project.",
    {
        "project_id": {
            "type": "string",
            "description": "Project ID, repository name, or path (for example 'root/openclaw').",
        },
    },
    group="repo_info",
    short_description="Read detailed metadata for one repository",
)
def get_project(project_id):
    data = _repo_meta(project_id)
    return _format_json(data)


@gitea_tool(
    "list_branches",
    "List all branches in the specified project.",
    {
        "project_id": {
            "type": "string",
            "description": "Project ID or path.",
        },
        "per_page": {
            "type": "integer",
            "description": "Number of results per page. Default 20.",
        },
    },
    group="branch_ops",
    short_description="List repository branches and default branch info",
)
def list_branches(project_id, per_page=20):
    owner, repo = _project_ref(project_id)
    repo_meta = _api_json("GET", f"repos/{owner}/{repo}")
    protections = _branch_protection_map(owner, repo)
    default_branch = repo_meta.get("default_branch", "")
    data = _api_json("GET", f"repos/{owner}/{repo}/branches", params={"limit": per_page})
    results = []
    for branch in data:
        name = branch["name"]
        results.append(
            {
                "name": name,
                "protected": name in protections or bool(branch.get("effective_branch_protection_name")),
                "merged": False,
                "default": name == default_branch,
            }
        )
    return _format_json(results)


@gitea_tool(
    "list_issues",
    "List issues in the specified project.",
    {
        "project_id": {
            "type": "string",
            "description": "Project ID or path.",
        },
        "state": {
            "type": "string",
            "enum": ["opened", "closed", "all"],
            "description": "Issue state filter. Default is opened.",
        },
        "per_page": {
            "type": "integer",
            "description": "Number of results per page. Default 20.",
        },
    },
    group="issue_tracking",
    short_description="List repository issues by state",
)
def list_issues(project_id, state="opened", per_page=20):
    owner, repo = _project_ref(project_id)
    data = _api_json(
        "GET",
        f"repos/{owner}/{repo}/issues",
        params={
            "state": _normalize_issue_state_for_gitea(state),
            "type": "issues",
            "limit": per_page,
        },
    )
    results = []
    for issue in data:
        results.append(
            {
                "iid": issue["number"],
                "title": issue["title"],
                "state": _normalize_issue_state_from_gitea(issue.get("state", "")),
                "labels": _normalize_labels(issue.get("labels", [])),
                "assignee": (issue.get("assignee") or {}).get("login", ""),
                "created_at": issue.get("created_at", ""),
            }
        )
    return _format_json(results)


@gitea_tool(
    "list_merge_requests",
    "List pull requests in the specified project.",
    {
        "project_id": {
            "type": "string",
            "description": "Project ID or path.",
        },
        "state": {
            "type": "string",
            "enum": ["opened", "closed", "merged", "all"],
            "description": "Pull request state filter. Default is opened.",
        },
        "per_page": {
            "type": "integer",
            "description": "Number of results per page. Default 20.",
        },
    },
    group="pull_requests",
    short_description="List repository pull requests by state",
)
def list_merge_requests(project_id, state="opened", per_page=20):
    owner, repo = _project_ref(project_id)
    query_state = "all" if state == "merged" else _normalize_issue_state_for_gitea(state)
    data = _api_json(
        "GET",
        f"repos/{owner}/{repo}/pulls",
        params={"state": query_state, "limit": per_page},
    )
    results = []
    for pr in data:
        merged = bool(pr.get("merged") or pr.get("merged_at"))
        normalized_state = "merged" if merged else _normalize_issue_state_from_gitea(pr.get("state", ""))
        if state == "merged" and not merged:
            continue
        results.append(
            {
                "iid": pr["number"],
                "title": pr["title"],
                "state": normalized_state,
                "source_branch": pr.get("head", {}).get("ref", ""),
                "target_branch": pr.get("base", {}).get("ref", ""),
                "author": (pr.get("user") or {}).get("login", ""),
            }
        )
    return _format_json(results)


@gitea_tool(
    "read_repo_file",
    "Read a file from a specific branch in the repository.",
    {
        "project_id": {
            "type": "string",
            "description": "Project ID or path.",
        },
        "file_path": {
            "type": "string",
            "description": "Path of the file in the repository (for example 'src/main.py').",
        },
        "ref": {
            "type": "string",
            "description": "Branch name or commit SHA. Default is main.",
        },
    },
    group="repo_content",
    short_description="Read repository file contents",
)
def read_repo_file(project_id, file_path, ref="main"):
    owner, repo = _project_ref(project_id)
    data = _read_contents(owner, repo, file_path, ref=ref)
    if data.get("encoding") == "base64":
        raw = base64.b64decode((data.get("content") or "").encode("ascii"))
        return raw.decode("utf-8", errors="replace")[:5000]
    return str(data.get("content", ""))[:5000]


@gitea_tool(
    "read_pipeline_log",
    "Read the execution log for an Actions job.",
    {
        "project_id": {
            "type": "string",
            "description": "Project ID or path.",
        },
        "job_id": {
            "type": "string",
            "description": "Job ID",
        },
    },
    group="ci_cd",
    short_description="Read the log for a specific CI job",
)
def read_pipeline_log(project_id, job_id):
    owner, repo = _project_ref(project_id)
    resp = _api("GET", f"repos/{owner}/{repo}/actions/jobs/{job_id}/logs")
    if resp.status_code >= 400:
        raise ToolExecutionError(f"[Gitea API Error] {resp.status_code}: {resp.text[:500]}")
    return resp.text[:5000]


@gitea_tool(
    "list_pipeline_jobs",
    "List recent Actions / CI jobs in the repository.",
    {
        "project_id": {
            "type": "string",
            "description": "Project ID or path.",
        },
        "per_page": {
            "type": "integer",
            "description": "Number of results to return. Default 10.",
        },
    },
    group="ci_cd",
    short_description="List recent CI or Actions jobs",
)
def list_pipeline_jobs(project_id, per_page=10):
    owner, repo = _project_ref(project_id)
    jobs = _list_action_jobs(owner, repo, per_page=per_page)
    results = []
    for job in jobs:
        results.append(
            {
                "id": job.get("id") or job.get("run_number") or job.get("number"),
                "name": job.get("name", "") or job.get("display_title", ""),
                "status": job.get("status", "") or job.get("conclusion", ""),
                "head_branch": job.get("head_branch", "") or job.get("ref", ""),
                "created_at": job.get("created_at", "") or job.get("run_started_at", ""),
            }
        )
    return _format_json(results)


@gitea_tool(
    "get_latest_pipeline_log",
    "Read the execution log for the most recent Actions / CI job in the repository.",
    {
        "project_id": {
            "type": "string",
            "description": "Project ID or path.",
        },
    },
    group="ci_cd",
    short_description="Read the log summary for the latest CI job",
)
def get_latest_pipeline_log(project_id):
    owner, repo = _project_ref(project_id)
    jobs = _list_action_jobs(owner, repo, per_page=1)
    if not jobs:
        return _format_json(
            {
                "project_id": project_id,
                "job_found": False,
                "message": "This repository currently has no readable Actions / CI job logs.",
            }
        )

    latest = jobs[0]
    job_id = latest.get("id")
    if not job_id:
        return _format_json(
            {
                "project_id": project_id,
                "job_found": False,
                "message": "Found the most recent Actions / CI run, but it does not include a readable job_id.",
            }
        )

    log_text = read_pipeline_log(project_id, str(job_id))
    return _format_json(
        {
            "project_id": project_id,
            "job_found": True,
            "job_id": str(job_id),
            "job_name": latest.get("name", "") or latest.get("display_title", ""),
            "status": latest.get("status", "") or latest.get("conclusion", ""),
            "log_excerpt": log_text,
        }
    )


@gitea_tool(
    "get_branch_protection",
    "View the protection rules for a specific branch.",
    {
        "project_id": {
            "type": "string",
            "description": "Project ID or path.",
        },
        "branch_name": {
            "type": "string",
            "description": "Branch name.",
        },
    },
    group="branch_ops",
    short_description="Read branch protection rules",
)
def get_branch_protection(project_id, branch_name):
    owner, repo = _project_ref(project_id)
    data = _api_json("GET", f"repos/{owner}/{repo}/branch_protections/{_encode_path(branch_name)}")
    return _format_json(data)


@gitea_tool(
    "delete_branch",
    "Delete a branch in the specified project. Dangerous operation; irreversible.",
    {
        "project_id": {
            "type": "string",
            "description": "Project ID or path.",
        },
        "branch_name": {
            "type": "string",
            "description": "Name of the branch to delete.",
        },
    },
    is_write=True,
    group="branch_ops",
    short_description="Delete one non-default branch",
)
def delete_branch(project_id, branch_name):
    owner, repo = _project_ref(project_id)
    resp = _api("DELETE", f"repos/{owner}/{repo}/branches/{_encode_path(branch_name)}")
    if resp.status_code == 204:
        return f"Branch '{branch_name}' was deleted."
    if resp.status_code >= 400:
        raise ToolExecutionError(f"[Gitea API Error] {resp.status_code}: {resp.text[:500]}")
    return f"Delete request for branch '{branch_name}' was sent. Status code: {resp.status_code}"


@gitea_tool(
    "close_issue",
    "Close a single issue in the specified project.",
    {
        "project_id": {
            "type": "string",
            "description": "Project ID or path.",
        },
        "issue_iid": {
            "type": "integer",
            "description": "Issue internal ID within the project (iid).",
        },
    },
    is_write=True,
    group="issue_tracking",
    short_description="Close one issue",
)
def close_issue(project_id, issue_iid):
    owner, repo = _project_ref(project_id)
    resp = _api("PATCH", f"repos/{owner}/{repo}/issues/{issue_iid}", json={"state": "closed"})
    if resp.status_code >= 400:
        raise ToolExecutionError(f"[Gitea API Error] {resp.status_code}: {resp.text[:500]}")
    data = resp.json()
    return f"Issue #{issue_iid} ('{data.get('title', '')}') was closed."


@gitea_tool(
    "update_branch_protection",
    "Update branch protection rules by adding or removing protection. Dangerous operation; may affect merge policies and collaboration flow.",
    {
        "project_id": {
            "type": "string",
            "description": "Project ID or path.",
        },
        "branch_name": {
            "type": "string",
            "description": "Branch name.",
        },
        "action": {
            "type": "string",
            "enum": ["protect", "unprotect"],
            "description": "protect = add protection, unprotect = remove protection.",
        },
    },
    is_write=True,
    group="branch_ops",
    short_description="Add or remove branch protection rules",
)
def update_branch_protection(project_id, branch_name, action):
    owner, repo = _project_ref(project_id)
    encoded_branch = _encode_path(branch_name)

    if action == "protect":
        payload = {
            "branch_name": branch_name,
            "enable_push": False,
            "enable_push_whitelist": False,
            "enable_force_push": False,
            "enable_merge_whitelist": False,
        }
        resp = _api("POST", f"repos/{owner}/{repo}/branch_protections", json=payload)
        if resp.status_code >= 400:
            raise ToolExecutionError(f"[Gitea API Error] {resp.status_code}: {resp.text[:500]}")
        return f"Protection was added to branch '{branch_name}'."
    if action == "unprotect":
        resp = _api("DELETE", f"repos/{owner}/{repo}/branch_protections/{encoded_branch}")
        if resp.status_code == 204:
            return f"Protection was removed from branch '{branch_name}'."
        if resp.status_code >= 400:
            raise ToolExecutionError(f"[Gitea API Error] {resp.status_code}: {resp.text[:500]}")
        return f"Remove-protection request for branch '{branch_name}' was sent. Status code: {resp.status_code}"

    raise ToolExecutionError(f"[Error] action must be 'protect' or 'unprotect', got: {action}")


@gitea_tool(
    "get_repo_settings",
    "Read the main repository settings and feature flags.",
    {
        "project_id": {
            "type": "string",
            "description": "Project ID, repository name, or path (for example 'root/openclaw').",
        },
    },
    group="repo_info",
    short_description="Read repository settings and feature flags",
)
def get_repo_settings(project_id):
    data = _repo_meta(project_id)
    result = {
        "id": data.get("id"),
        "name": data.get("name", ""),
        "full_name": data.get("full_name", ""),
        "default_branch": data.get("default_branch", ""),
        "private": bool(data.get("private")),
        "archived": bool(data.get("archived")),
        "mirror": bool(data.get("mirror")),
        "has_issues": bool(data.get("has_issues")),
        "has_pull_requests": bool(data.get("has_pull_requests")),
        "has_projects": bool(data.get("has_projects")),
        "has_wiki": bool(data.get("has_wiki")),
        "website": data.get("website", ""),
        "description": data.get("description", ""),
    }
    return _format_json(result)


@gitea_tool(
    "list_repo_tags",
    "List currently visible tags in the repository.",
    {
        "project_id": {
            "type": "string",
            "description": "Project ID or path.",
        },
        "per_page": {
            "type": "integer",
            "description": "Number of results to return. Default 20.",
        },
    },
    group="repo_info",
    short_description="List repository tags and their commits",
)
def list_repo_tags(project_id, per_page=20):
    owner, repo = _project_ref(project_id)
    tags = _list_repo_tags_raw(owner, repo, per_page=per_page)
    results = []
    for tag in tags:
        commit = tag.get("commit") or {}
        results.append(
            {
                "name": tag.get("name", ""),
                "id": tag.get("id", ""),
                "commit_sha": commit.get("sha", ""),
                "zipball_url": tag.get("zipball_url", ""),
                "tarball_url": tag.get("tarball_url", ""),
            }
        )
    return _format_json(results)


@gitea_tool(
    "list_repo_directory",
    "List files and subdirectories in a repository directory.",
    {
        "project_id": {
            "type": "string",
            "description": "Project ID or path.",
        },
        "directory_path": {
            "type": "string",
            "description": "Directory path. Leave empty for the repository root.",
        },
        "ref": {
            "type": "string",
            "description": "Branch name or commit SHA. Default is main.",
        },
    },
    group="repo_content",
    short_description="List repository directory structure",
)
def list_repo_directory(project_id, directory_path="", ref="main"):
    owner, repo = _project_ref(project_id)
    entries = _list_directory_contents(owner, repo, directory_path=directory_path, ref=ref)
    results = []
    for item in entries:
        results.append(
            {
                "name": item.get("name", ""),
                "path": item.get("path", ""),
                "type": item.get("type", ""),
                "size": item.get("size", 0),
                "sha": item.get("sha", ""),
            }
        )
    return _format_json(results)


@gitea_tool(
    "create_branch",
    "Create a new branch from an existing branch or ref.",
    {
        "project_id": {
            "type": "string",
            "description": "Project ID or path.",
        },
        "branch_name": {
            "type": "string",
            "description": "New branch name.",
        },
        "from_ref": {
            "type": "string",
            "description": "Source branch or ref. Default is main.",
        },
    },
    is_write=True,
    group="branch_ops",
    short_description="Create a new branch from an existing ref",
)
def create_branch(project_id, branch_name, from_ref="main"):
    owner, repo = _project_ref(project_id)
    data = _create_branch_via_api(owner, repo, branch_name, from_ref)
    return _format_json(
        {
            "name": data.get("name", branch_name),
            "created_from": from_ref,
            "commit_sha": (data.get("commit") or {}).get("id", ""),
        }
    )


@gitea_tool(
    "get_issue",
    "Read detailed information for a single issue.",
    {
        "project_id": {
            "type": "string",
            "description": "Project ID or path.",
        },
        "issue_iid": {
            "type": "integer",
            "description": "Issue internal ID within the project (iid).",
        },
    },
    group="issue_tracking",
    short_description="Read details for one issue",
)
def get_issue(project_id, issue_iid):
    owner, repo = _project_ref(project_id)
    data = _api_json("GET", f"repos/{owner}/{repo}/issues/{issue_iid}")
    return _format_json(_format_issue(data))


@gitea_tool(
    "list_issue_comments",
    "List comments under the specified issue.",
    {
        "project_id": {
            "type": "string",
            "description": "Project ID or path.",
        },
        "issue_iid": {
            "type": "integer",
            "description": "Issue internal ID within the project (iid).",
        },
        "per_page": {
            "type": "integer",
            "description": "Number of results to return. Default 20.",
        },
    },
    group="issue_tracking",
    short_description="List issue comments",
)
def list_issue_comments(project_id, issue_iid, per_page=20):
    owner, repo = _project_ref(project_id)
    data = _api_json(
        "GET",
        f"repos/{owner}/{repo}/issues/{issue_iid}/comments",
        params={"limit": per_page},
    )
    results = []
    for item in data if isinstance(data, list) else []:
        results.append(
            {
                "id": item.get("id"),
                "author": (item.get("user") or {}).get("login", ""),
                "created_at": item.get("created_at", ""),
                "updated_at": item.get("updated_at", ""),
                "body": item.get("body", ""),
            }
        )
    return _format_json(results)


@gitea_tool(
    "create_issue",
    "Create a new issue in the repository.",
    {
        "project_id": {
            "type": "string",
            "description": "Project ID or path.",
        },
        "title": {
            "type": "string",
            "description": "Issue title.",
        },
        "body": {
            "type": "string",
            "description": "Issue body. Can be empty.",
        },
    },
    is_write=True,
    group="issue_tracking",
    short_description="Create a new issue",
)
def create_issue(project_id, title, body=""):
    owner, repo = _project_ref(project_id)
    payload = {"title": title}
    if body:
        payload["body"] = body
    data = _api_json("POST", f"repos/{owner}/{repo}/issues", json=payload)
    return _format_json(_format_issue(data))


@gitea_tool(
    "add_issue_comment",
    "Add a comment to the specified issue.",
    {
        "project_id": {
            "type": "string",
            "description": "Project ID or path.",
        },
        "issue_iid": {
            "type": "integer",
            "description": "Issue internal ID within the project (iid).",
        },
        "body": {
            "type": "string",
            "description": "Comment content.",
        },
    },
    is_write=True,
    group="issue_tracking",
    short_description="Add a comment to an issue",
)
def add_issue_comment(project_id, issue_iid, body):
    owner, repo = _project_ref(project_id)
    data = _api_json(
        "POST",
        f"repos/{owner}/{repo}/issues/{issue_iid}/comments",
        json={"body": body},
    )
    return _format_json(
        {
            "id": data.get("id"),
            "issue_iid": issue_iid,
            "author": (data.get("user") or {}).get("login", ""),
            "created_at": data.get("created_at", ""),
            "body": data.get("body", ""),
        }
    )


@gitea_tool(
    "reopen_issue",
    "Reopen a closed issue.",
    {
        "project_id": {
            "type": "string",
            "description": "Project ID or path.",
        },
        "issue_iid": {
            "type": "integer",
            "description": "Issue internal ID within the project (iid).",
        },
    },
    is_write=True,
    group="issue_tracking",
    short_description="Reopen one issue",
)
def reopen_issue(project_id, issue_iid):
    owner, repo = _project_ref(project_id)
    data = _api_json("PATCH", f"repos/{owner}/{repo}/issues/{issue_iid}", json={"state": "open"})
    return f"Issue #{issue_iid} ('{data.get('title', '')}') was reopened."


@gitea_tool(
    "get_pull_request",
    "Read detailed information for a single pull request.",
    {
        "project_id": {
            "type": "string",
            "description": "Project ID or path.",
        },
        "pr_iid": {
            "type": "integer",
            "description": "Pull request internal ID within the project (iid).",
        },
    },
    group="pull_requests",
    short_description="Read details for one pull request",
)
def get_pull_request(project_id, pr_iid):
    owner, repo = _project_ref(project_id)
    data = _api_json("GET", f"repos/{owner}/{repo}/pulls/{pr_iid}")
    return _format_json(_format_pull_request(data))


@gitea_tool(
    "list_pull_request_files",
    "List files changed in a pull request.",
    {
        "project_id": {
            "type": "string",
            "description": "Project ID or path.",
        },
        "pr_iid": {
            "type": "integer",
            "description": "Pull request internal ID within the project (iid).",
        },
    },
    group="pull_requests",
    short_description="List changed files in a pull request",
)
def list_pull_request_files(project_id, pr_iid):
    owner, repo = _project_ref(project_id)
    data = _api_json("GET", f"repos/{owner}/{repo}/pulls/{pr_iid}/files")
    results = []
    for item in data if isinstance(data, list) else []:
        results.append(
            {
                "filename": item.get("filename", "") or item.get("path", ""),
                "status": item.get("status", ""),
                "additions": item.get("additions", 0),
                "deletions": item.get("deletions", 0),
                "changes": item.get("changes", 0),
                "patch": item.get("patch", "")[:1000],
            }
        )
    return _format_json(results)


@gitea_tool(
    "create_pull_request",
    "Create a pull request from the source branch to the target branch.",
    {
        "project_id": {
            "type": "string",
            "description": "Project ID or path.",
        },
        "title": {
            "type": "string",
            "description": "Pull request title.",
        },
        "head_branch": {
            "type": "string",
            "description": "Source branch name.",
        },
        "base_branch": {
            "type": "string",
            "description": "Target branch name.",
        },
        "body": {
            "type": "string",
            "description": "Pull request description. Can be empty.",
        },
    },
    is_write=True,
    group="pull_requests",
    short_description="Create a new pull request",
)
def create_pull_request(project_id, title, head_branch, base_branch, body=""):
    owner, repo = _project_ref(project_id)
    payload = {
        "title": title,
        "head": head_branch,
        "base": base_branch,
    }
    if body:
        payload["body"] = body
    data = _api_json("POST", f"repos/{owner}/{repo}/pulls", json=payload)
    return _format_json(_format_pull_request(data))


@gitea_tool(
    "list_collaborators",
    "List repository collaborators and their permissions.",
    {
        "project_id": {
            "type": "string",
            "description": "Project ID or path.",
        },
        "per_page": {
            "type": "integer",
            "description": "Number of results to return. Default 20.",
        },
    },
    group="access_control",
    short_description="List repository collaborators and permissions",
)
def list_collaborators(project_id, per_page=20):
    owner, repo = _project_ref(project_id)
    data = _api_json(
        "GET",
        f"repos/{owner}/{repo}/collaborators",
        params={"limit": per_page},
    )
    results = []
    for item in data if isinstance(data, list) else []:
        results.append(
            {
                "login": item.get("login", ""),
                "full_name": item.get("full_name", ""),
                "email": item.get("email", ""),
                "permissions": item.get("permissions", {}),
            }
        )
    return _format_json(results)


@gitea_tool(
    "add_collaborator",
    "Add a user as a repository collaborator.",
    {
        "project_id": {
            "type": "string",
            "description": "Project ID or path.",
        },
        "username": {
            "type": "string",
            "description": "Username to add.",
        },
        "permission": {
            "type": "string",
            "enum": ["read", "write", "admin"],
            "description": "Permission level to grant. Default is write.",
        },
    },
    is_write=True,
    group="access_control",
    short_description="Add a repository collaborator",
)
def add_collaborator(project_id, username, permission="write"):
    owner, repo = _project_ref(project_id)
    resp = _api(
        "PUT",
        f"repos/{owner}/{repo}/collaborators/{_encode_path(username)}",
        json={"permission": permission},
    )
    if resp.status_code not in (201, 204):
        raise ToolExecutionError(f"[Gitea API Error] {resp.status_code}: {resp.text[:500]}")
    return f"User '{username}' was added as a collaborator on {project_id} with permission={permission}."


@gitea_tool(
    "remove_collaborator",
    "Remove a repository collaborator.",
    {
        "project_id": {
            "type": "string",
            "description": "Project ID or path.",
        },
        "username": {
            "type": "string",
            "description": "Username to remove.",
        },
    },
    is_write=True,
    group="access_control",
    short_description="Remove a repository collaborator",
)
def remove_collaborator(project_id, username):
    owner, repo = _project_ref(project_id)
    resp = _api("DELETE", f"repos/{owner}/{repo}/collaborators/{_encode_path(username)}")
    if resp.status_code not in (204, 404):
        raise ToolExecutionError(f"[Gitea API Error] {resp.status_code}: {resp.text[:500]}")
    if resp.status_code == 404:
        return f"User '{username}' is not a collaborator on {project_id}."
    return f"User '{username}' was removed from the collaborator list for {project_id}."


@gitea_tool(
    "list_deploy_keys",
    "List repository deploy keys.",
    {
        "project_id": {
            "type": "string",
            "description": "Project ID or path.",
        },
    },
    group="access_control",
    short_description="List repository deploy keys",
)
def list_deploy_keys(project_id):
    owner, repo = _project_ref(project_id)
    data = _api_json("GET", f"repos/{owner}/{repo}/keys")
    results = []
    for item in data if isinstance(data, list) else []:
        key_value = item.get("key", "")
        results.append(
            {
                "id": item.get("id"),
                "title": item.get("title", ""),
                "read_only": bool(item.get("read_only")),
                "fingerprint": item.get("fingerprint", ""),
                "key_preview": f"{key_value[:60]}..." if key_value else "",
            }
        )
    return _format_json(results)


@gitea_tool(
    "add_deploy_key",
    "Add a deploy key to the repository.",
    {
        "project_id": {
            "type": "string",
            "description": "Project ID or path.",
        },
        "title": {
            "type": "string",
            "description": "Deploy key title.",
        },
        "public_key": {
            "type": "string",
            "description": "SSH public key contents.",
        },
        "read_only": {
            "type": "boolean",
            "description": "Whether the key is read-only. Default is true.",
        },
    },
    is_write=True,
    group="access_control",
    short_description="Add a deploy key to the repository",
)
def add_deploy_key(project_id, title, public_key, read_only=True):
    owner, repo = _project_ref(project_id)
    data = _api_json(
        "POST",
        f"repos/{owner}/{repo}/keys",
        json={"title": title, "key": public_key, "read_only": read_only},
    )
    return _format_json(
        {
            "id": data.get("id"),
            "title": data.get("title", title),
            "read_only": bool(data.get("read_only", read_only)),
        }
    )


@gitea_tool(
    "remove_deploy_key",
    "Delete a deploy key from the repository.",
    {
        "project_id": {
            "type": "string",
            "description": "Project ID or path.",
        },
        "key_id": {
            "type": "integer",
            "description": "Deploy key ID.",
        },
    },
    is_write=True,
    group="access_control",
    short_description="Delete one deploy key",
)
def remove_deploy_key(project_id, key_id):
    owner, repo = _project_ref(project_id)
    resp = _api("DELETE", f"repos/{owner}/{repo}/keys/{key_id}")
    if resp.status_code not in (204, 404):
        raise ToolExecutionError(f"[Gitea API Error] {resp.status_code}: {resp.text[:500]}")
    if resp.status_code == 404:
        return f"Deploy key #{key_id} does not exist."
    return f"Deploy key #{key_id} was deleted."


@gitea_tool(
    "list_repo_labels",
    "List labels in the repository.",
    {
        "project_id": {
            "type": "string",
            "description": "Project ID or path.",
        },
        "per_page": {
            "type": "integer",
            "description": "Number of results to return. Default 20.",
        },
    },
    group="labels_and_milestones",
    short_description="List repository labels",
)
def list_repo_labels(project_id, per_page=20):
    owner, repo = _project_ref(project_id)
    data = _api_json(
        "GET",
        f"repos/{owner}/{repo}/labels",
        params={"limit": per_page},
    )
    results = []
    for item in data if isinstance(data, list) else []:
        results.append(
            {
                "id": item.get("id"),
                "name": item.get("name", ""),
                "color": item.get("color", ""),
                "description": item.get("description", ""),
            }
        )
    return _format_json(results)


@gitea_tool(
    "create_label",
    "Create a new label in the repository.",
    {
        "project_id": {
            "type": "string",
            "description": "Project ID or path.",
        },
        "name": {
            "type": "string",
            "description": "Label name.",
        },
        "color": {
            "type": "string",
            "description": "Label color, such as ff0000 or #ff0000.",
        },
        "description": {
            "type": "string",
            "description": "Label description. Can be empty.",
        },
    },
    is_write=True,
    group="labels_and_milestones",
    short_description="Create a new repository label",
)
def create_label(project_id, name, color, description=""):
    owner, repo = _project_ref(project_id)
    payload = {
        "name": name,
        "color": str(color).lstrip("#"),
    }
    if description:
        payload["description"] = description
    data = _api_json("POST", f"repos/{owner}/{repo}/labels", json=payload)
    return _format_json(
        {
            "id": data.get("id"),
            "name": data.get("name", name),
            "color": data.get("color", payload["color"]),
            "description": data.get("description", description),
        }
    )


@gitea_tool(
    "list_milestones",
    "List milestones in the repository.",
    {
        "project_id": {
            "type": "string",
            "description": "Project ID or path.",
        },
        "state": {
            "type": "string",
            "enum": ["open", "closed", "all"],
            "description": "Milestone state filter. Default is open.",
        },
        "per_page": {
            "type": "integer",
            "description": "Number of results to return. Default 20.",
        },
    },
    group="labels_and_milestones",
    short_description="List repository milestones",
)
def list_milestones(project_id, state="open", per_page=20):
    owner, repo = _project_ref(project_id)
    data = _api_json(
        "GET",
        f"repos/{owner}/{repo}/milestones",
        params={"state": state, "limit": per_page},
    )
    results = []
    for item in data if isinstance(data, list) else []:
        results.append(
            {
                "id": item.get("id"),
                "title": item.get("title", ""),
                "state": item.get("state", ""),
                "open_issues": item.get("open_issues", 0),
                "closed_issues": item.get("closed_issues", 0),
                "due_on": item.get("due_on", ""),
                "description": item.get("description", ""),
            }
        )
    return _format_json(results)


@gitea_tool(
    "create_milestone",
    "Create a new milestone in the repository.",
    {
        "project_id": {
            "type": "string",
            "description": "Project ID or path.",
        },
        "title": {
            "type": "string",
            "description": "Milestone title.",
        },
        "description": {
            "type": "string",
            "description": "Milestone description. Can be empty.",
        },
        "due_on": {
            "type": "string",
            "description": "Due time in ISO8601 format. Can be empty.",
        },
    },
    is_write=True,
    group="labels_and_milestones",
    short_description="Create a new milestone",
)
def create_milestone(project_id, title, description="", due_on=""):
    owner, repo = _project_ref(project_id)
    payload = {"title": title}
    if description:
        payload["description"] = description
    if due_on:
        payload["due_on"] = due_on
    data = _api_json("POST", f"repos/{owner}/{repo}/milestones", json=payload)
    return _format_json(
        {
            "id": data.get("id"),
            "title": data.get("title", title),
            "state": data.get("state", ""),
            "due_on": data.get("due_on", due_on),
        }
    )


@gitea_tool(
    "list_releases",
    "List releases in the repository.",
    {
        "project_id": {
            "type": "string",
            "description": "Project ID or path.",
        },
        "per_page": {
            "type": "integer",
            "description": "Number of results to return. Default 20.",
        },
    },
    group="releases",
    short_description="List repository releases",
)
def list_releases(project_id, per_page=20):
    owner, repo = _project_ref(project_id)
    data = _api_json(
        "GET",
        f"repos/{owner}/{repo}/releases",
        params={"limit": per_page},
    )
    results = []
    for item in data if isinstance(data, list) else []:
        results.append(
            {
                "id": item.get("id"),
                "tag_name": item.get("tag_name", ""),
                "name": item.get("name", ""),
                "target_commitish": item.get("target_commitish", ""),
                "draft": bool(item.get("draft")),
                "prerelease": bool(item.get("prerelease")),
                "created_at": item.get("created_at", ""),
                "published_at": item.get("published_at", ""),
            }
        )
    return _format_json(results)


@gitea_tool(
    "create_release",
    "Create a new release.",
    {
        "project_id": {
            "type": "string",
            "description": "Project ID or path.",
        },
        "tag_name": {
            "type": "string",
            "description": "Tag name for the release.",
        },
        "target_commitish": {
            "type": "string",
            "description": "Target branch or commit. Default is main.",
        },
        "name": {
            "type": "string",
            "description": "Release name. If empty, tag_name will be used.",
        },
        "body": {
            "type": "string",
            "description": "Release description. Can be empty.",
        },
        "draft": {
            "type": "boolean",
            "description": "Whether the release is a draft. Default is false.",
        },
        "prerelease": {
            "type": "boolean",
            "description": "Whether the release is a prerelease. Default is false.",
        },
    },
    is_write=True,
    group="releases",
    short_description="Create a new release",
)
def create_release(project_id, tag_name, target_commitish="main", name="", body="", draft=False, prerelease=False):
    owner, repo = _project_ref(project_id)
    payload = {
        "tag_name": tag_name,
        "target_commitish": target_commitish,
        "name": name or tag_name,
        "draft": draft,
        "prerelease": prerelease,
    }
    if body:
        payload["body"] = body
    data = _api_json("POST", f"repos/{owner}/{repo}/releases", json=payload)
    return _format_json(
        {
            "id": data.get("id"),
            "tag_name": data.get("tag_name", tag_name),
            "name": data.get("name", name or tag_name),
            "draft": bool(data.get("draft", draft)),
            "prerelease": bool(data.get("prerelease", prerelease)),
        }
    )


@gitea_tool(
    "delete_release",
    "Delete a release. Dangerous operation; irreversible.",
    {
        "project_id": {
            "type": "string",
            "description": "Project ID or path.",
        },
        "release_id": {
            "type": "integer",
            "description": "Release ID.",
        },
    },
    is_write=True,
    group="releases",
    short_description="Delete one release",
)
def delete_release(project_id, release_id):
    owner, repo = _project_ref(project_id)
    resp = _api("DELETE", f"repos/{owner}/{repo}/releases/{release_id}")
    if resp.status_code not in (204, 404):
        raise ToolExecutionError(f"[Gitea API Error] {resp.status_code}: {resp.text[:500]}")
    if resp.status_code == 404:
        return f"Release #{release_id} does not exist."
    return f"Release #{release_id} was deleted."


@gitea_tool(
    "list_webhooks",
    "List repository webhooks.",
    {
        "project_id": {
            "type": "string",
            "description": "Project ID or path.",
        },
    },
    group="webhooks",
    short_description="List repository webhooks",
)
def list_webhooks(project_id):
    owner, repo = _project_ref(project_id)
    data = _api_json("GET", f"repos/{owner}/{repo}/hooks")
    hooks = data.get("data", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
    results = []
    for item in hooks:
        config = item.get("config") or {}
        results.append(
            {
                "id": item.get("id"),
                "type": item.get("type", ""),
                "active": bool(item.get("active")),
                "events": item.get("events", []),
                "url": config.get("url", ""),
                "content_type": config.get("content_type", ""),
            }
        )
    return _format_json(results)


@gitea_tool(
    "create_webhook",
    "Create a new webhook for the repository.",
    {
        "project_id": {
            "type": "string",
            "description": "Project ID or path.",
        },
        "url": {
            "type": "string",
            "description": "Webhook target URL.",
        },
        "events": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Subscribed event list. Default is ['push'].",
        },
        "hook_type": {
            "type": "string",
            "description": "Webhook type. Default is gitea.",
        },
        "content_type": {
            "type": "string",
            "description": "Payload content type. Default is json.",
        },
        "secret": {
            "type": "string",
            "description": "Webhook signing secret. Can be empty.",
        },
        "active": {
            "type": "boolean",
            "description": "Whether the webhook is enabled. Default is true.",
        },
    },
    is_write=True,
    group="webhooks",
    short_description="Create a new webhook",
)
def create_webhook(project_id, url, events=None, hook_type="gitea", content_type="json", secret="", active=True):
    owner, repo = _project_ref(project_id)
    payload = {
        "type": hook_type,
        "active": active,
        "events": events or ["push"],
        "config": {
            "url": url,
            "content_type": content_type,
        },
    }
    if secret:
        payload["config"]["secret"] = secret
    data = _api_json("POST", f"repos/{owner}/{repo}/hooks", json=payload)
    config = data.get("config") or {}
    return _format_json(
        {
            "id": data.get("id"),
            "type": data.get("type", hook_type),
            "active": bool(data.get("active", active)),
            "events": data.get("events", payload["events"]),
            "url": config.get("url", url),
        }
    )


@gitea_tool(
    "delete_webhook",
    "Delete a webhook from the repository. Dangerous operation; irreversible.",
    {
        "project_id": {
            "type": "string",
            "description": "Project ID or path.",
        },
        "hook_id": {
            "type": "integer",
            "description": "Webhook ID.",
        },
    },
    is_write=True,
    group="webhooks",
    short_description="Delete one webhook",
)
def delete_webhook(project_id, hook_id):
    owner, repo = _project_ref(project_id)
    resp = _api("DELETE", f"repos/{owner}/{repo}/hooks/{hook_id}")
    if resp.status_code not in (204, 404):
        raise ToolExecutionError(f"[Gitea API Error] {resp.status_code}: {resp.text[:500]}")
    if resp.status_code == 404:
        return f"Webhook #{hook_id} does not exist."
    return f"Webhook #{hook_id} was deleted."
