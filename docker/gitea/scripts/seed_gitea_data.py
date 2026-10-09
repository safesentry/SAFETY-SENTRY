#!/usr/bin/env python3
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

import requests


BASE_URL = os.environ.get("GITEA_BASE_URL", "http://localhost:3000").rstrip("/")
OWNER = os.environ.get("GITEA_OWNER", "root")
ACCESS_TOKEN = os.environ.get("GITEA_ACCESS_TOKEN", "")
MANIFEST_PATH = Path(
    os.environ.get(
        "GITEA_SEED_MANIFEST",
        Path(__file__).resolve().parents[1] / "seed_manifest.json",
    )
)


class SeedError(RuntimeError):
    pass


def require_git():
    if shutil.which("git") is None:
        raise SeedError("git command is required for importing external repositories.")


def require_token():
    if not ACCESS_TOKEN:
        raise SeedError("GITEA_ACCESS_TOKEN is required for seeding.")


def user_exists(username):
    resp = api("GET", f"users/{username}")
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        raise SeedError(f"GET user {username} failed: {resp.status_code} {resp.text[:500]}")
    return resp.json()


def ensure_user(user):
    username = user["username"]
    current = user_exists(username)
    if current:
        print(f"[seed] user exists: {username}")
        return current

    payload = {
        "username": username,
        "email": user["email"],
        "password": user["password"],
        "must_change_password": False,
        "send_notify": False,
    }
    if user.get("full_name"):
        payload["full_name"] = user["full_name"]
    created = api_json("POST", "admin/users", expected={201}, json=payload)
    print(f"[seed] user created: {username}")
    return created


def api(method, path, expected=None, **kwargs):
    url = f"{BASE_URL}/api/v1/{path.lstrip('/')}"
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"token {ACCESS_TOKEN}"
    resp = requests.request(method, url, headers=headers, timeout=30, **kwargs)
    if expected and resp.status_code not in expected:
        raise SeedError(f"{method} {url} -> {resp.status_code}: {resp.text[:500]}")
    return resp


def api_json(method, path, expected=None, **kwargs):
    resp = api(method, path, expected=expected, **kwargs)
    if not resp.text:
        return None
    return resp.json()


def repo_exists(name):
    resp = api("GET", f"repos/{OWNER}/{name}")
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        raise SeedError(f"GET repo {name} failed: {resp.status_code} {resp.text[:500]}")
    return resp.json()


def ensure_repo(repo):
    current = repo_exists(repo["name"])
    if current:
        print(f"[seed] repo exists: {repo['name']}")
        return current, False

    payload = {
        "name": repo["name"],
        "description": repo.get("description", ""),
        "auto_init": not bool(repo.get("source_repo_url")),
        "default_branch": repo["default_branch"],
        "private": False,
    }
    created = api_json("POST", "user/repos", expected={201}, json=payload)
    print(f"[seed] repo created: {repo['name']}")
    return created, True


def build_push_url(repo_name):
    parsed = urllib.parse.urlsplit(BASE_URL)
    if not parsed.scheme or not parsed.netloc:
        raise SeedError(f"Invalid Gitea base URL: {BASE_URL}")
    username = urllib.parse.quote(OWNER, safe="")
    token = urllib.parse.quote(ACCESS_TOKEN, safe="")
    netloc = f"{username}:{token}@{parsed.netloc}"
    repo_path = parsed.path.rstrip("/") + f"/{OWNER}/{repo_name}.git"
    return urllib.parse.urlunsplit((parsed.scheme, netloc, repo_path, "", ""))


def run_git(args, cwd=None):
    try:
        subprocess.run(
            ["git"] + list(args),
            cwd=cwd,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or exc.stdout.strip() or str(exc)
        raise SeedError(f"git {' '.join(args)} failed: {detail}") from exc


def copy_worktree(src_dir, dst_dir):
    for entry in Path(src_dir).iterdir():
        if entry.name == ".git":
            continue
        target = Path(dst_dir) / entry.name
        if entry.is_dir():
            shutil.copytree(entry, target, dirs_exist_ok=True)
        else:
            shutil.copy2(entry, target)


def import_external_repo(repo):
    require_git()
    source_url = repo.get("source_repo_url")
    if not source_url:
        return

    repo_name = repo["name"]
    import_mode = repo.get("source_import_mode", "snapshot")
    branch = repo.get("source_branch", repo["default_branch"])
    push_url = build_push_url(repo_name)

    with tempfile.TemporaryDirectory(prefix=f"gitea-seed-{repo_name}-") as tmpdir:
        tmp_path = Path(tmpdir)
        if import_mode == "mirror":
            clone_dir = tmp_path / "mirror.git"
            run_git(["clone", "--mirror", source_url, str(clone_dir)])
            run_git(["push", "--mirror", push_url], cwd=clone_dir)
        elif import_mode == "snapshot":
            clone_dir = tmp_path / "source"
            export_dir = tmp_path / "export"
            run_git(
                [
                    "clone",
                    "--depth",
                    "1",
                    "--single-branch",
                    "--branch",
                    branch,
                    source_url,
                    str(clone_dir),
                ]
            )
            export_dir.mkdir(parents=True, exist_ok=True)
            copy_worktree(clone_dir, export_dir)
            run_git(["init"], cwd=export_dir)
            run_git(["checkout", "-b", branch], cwd=export_dir)
            run_git(["config", "user.name", "Pipeline Seed Bot"], cwd=export_dir)
            run_git(["config", "user.email", "seed-bot@example.com"], cwd=export_dir)
            run_git(["add", "."], cwd=export_dir)
            run_git(["commit", "-m", f"seed: snapshot {repo_name} from {source_url}"], cwd=export_dir)
            run_git(["remote", "add", "gitea", push_url], cwd=export_dir)
            run_git(["push", "-u", "gitea", branch], cwd=export_dir)
        else:
            raise SeedError(f"Unsupported source_import_mode: {import_mode}")

    print(f"[seed] external repo imported: {repo_name} ({import_mode})")


def ensure_branch(repo_name, new_branch, old_branch):
    last_error = None
    for _ in range(5):
        resp = api(
            "POST",
            f"repos/{OWNER}/{repo_name}/branches",
            json={"new_branch_name": new_branch, "old_branch_name": old_branch},
        )
        if resp.status_code in (201, 204):
            print(f"[seed] branch created: {repo_name}:{new_branch}")
            return
        if resp.status_code in (409, 422):
            print(f"[seed] branch exists: {repo_name}:{new_branch}")
            return
        last_error = f"{resp.status_code} {resp.text[:500]}"
        if resp.status_code == 404 and "Git Repository is empty" in resp.text:
            time.sleep(1)
            continue
        raise SeedError(f"create branch failed: {last_error}")
    raise SeedError(f"create branch failed: {last_error}")


def ensure_file_commit(repo_name, branch, path, content, message):
    encoded_content = base64.b64encode(content.encode("utf-8")).decode("ascii")
    payload = {
        "branch": branch,
        "content": encoded_content,
        "message": message,
    }
    resp = api("POST", f"repos/{OWNER}/{repo_name}/contents/{path}", json=payload)
    if resp.status_code in (201, 200):
        print(f"[seed] file committed: {repo_name}:{branch}:{path}")
        return
    if resp.status_code == 422:
        print(f"[seed] file exists: {repo_name}:{branch}:{path}")
        return
    raise SeedError(f"create file failed: {resp.status_code} {resp.text[:500]}")


def ensure_issue(repo_name, title):
    issues = api_json(
        "GET",
        f"repos/{OWNER}/{repo_name}/issues",
        expected={200},
        params={"state": "all", "type": "issues", "limit": 100},
    )
    if any(issue.get("title") == title for issue in issues):
        print(f"[seed] issue exists: {repo_name}:{title}")
        return
    api_json(
        "POST",
        f"repos/{OWNER}/{repo_name}/issues",
        expected={201},
        json={"title": title},
    )
    print(f"[seed] issue created: {repo_name}:{title}")


def ensure_collaborator(repo_name, collaborator):
    username = collaborator["username"]
    permission = collaborator.get("permission", "read")
    resp = api(
        "PUT",
        f"repos/{OWNER}/{repo_name}/collaborators/{username}",
        json={"permission": permission},
    )
    if resp.status_code not in (201, 204):
        raise SeedError(
            f"ensure collaborator failed: {repo_name}:{username} -> {resp.status_code} {resp.text[:500]}"
        )
    print(f"[seed] collaborator ensured: {repo_name}:{username} ({permission})")


def ensure_pull(repo_name, pull):
    pulls = api_json(
        "GET",
        f"repos/{OWNER}/{repo_name}/pulls",
        expected={200},
        params={"state": "all", "limit": 100},
    )
    if any(pr.get("title") == pull["title"] for pr in pulls):
        print(f"[seed] pull exists: {repo_name}:{pull['title']}")
        return

    seed_path = f".pipeline/{pull['head']}-seed.txt"
    seed_content = f"Seed content for {pull['head']} -> {pull['base']}\n"
    ensure_file_commit(
        repo_name,
        pull["head"],
        seed_path,
        seed_content,
        f"seed: add content for {pull['head']}",
    )
    payload = {
        "title": pull["title"],
        "head": pull["head"],
        "base": pull["base"],
        "body": pull.get("body", ""),
    }
    resp = api("POST", f"repos/{OWNER}/{repo_name}/pulls", json=payload)
    if resp.status_code == 201:
        print(f"[seed] pull created: {repo_name}:{pull['title']}")
        return
    if resp.status_code in (409, 422):
        print(f"[seed] pull skipped: {repo_name}:{pull['title']} ({resp.status_code})")
        return
    raise SeedError(f"create pull failed: {resp.status_code} {resp.text[:500]}")


def ensure_branch_protection(repo_name, branch_name):
    payload = {
        "branch_name": branch_name,
        "enable_push": False,
        "enable_push_whitelist": False,
        "enable_force_push": False,
        "enable_merge_whitelist": False,
    }
    resp = api("POST", f"repos/{OWNER}/{repo_name}/branch_protections", json=payload)
    if resp.status_code in (201, 204):
        print(f"[seed] branch protection created: {repo_name}:{branch_name}")
        return
    if resp.status_code in (409, 422):
        print(f"[seed] branch protection exists or unsupported: {repo_name}:{branch_name}")
        return
    print(
        f"[seed] branch protection skipped: {repo_name}:{branch_name} "
        f"({resp.status_code}) {resp.text[:200]}",
        file=sys.stderr,
    )


def main():
    require_token()
    with MANIFEST_PATH.open("r", encoding="utf-8") as fh:
        manifest = json.load(fh)

    for user in manifest.get("users", []):
        ensure_user(user)

    for repo in manifest["repositories"]:
        _, created = ensure_repo(repo)
        if created and repo.get("source_repo_url"):
            import_external_repo(repo)
        for extra_branch in repo.get("extra_branches", []):
            ensure_branch(repo["name"], extra_branch, repo["default_branch"])
        for collaborator in repo.get("collaborators", []):
            ensure_collaborator(repo["name"], collaborator)
        for issue_title in repo.get("issues", []):
            ensure_issue(repo["name"], issue_title)
        for pull in repo.get("pulls", []):
            ensure_pull(repo["name"], pull)
        if repo.get("protect_default_branch"):
            ensure_branch_protection(repo["name"], repo["default_branch"])

    print("[seed] Gitea seed complete")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[seed] ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
