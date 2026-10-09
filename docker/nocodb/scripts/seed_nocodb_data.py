#!/usr/bin/env python3
"""
NocoDB seed data script.

Reads seed_manifest.json and creates bases, tables, and records through the
NocoDB REST API v2. NocoDB v0.300+ uses the workspace hierarchy:
workspace -> base -> table -> record.
"""

import json
import os
import sys
import time
from pathlib import Path

import requests

BASE_URL = os.environ.get("NOCODB_BASE_URL", "http://localhost:8080").rstrip("/")
ADMIN_EMAIL = os.environ.get("NOCODB_ADMIN_EMAIL", "admin@example.com")
ADMIN_PASSWORD = os.environ.get("NOCODB_ADMIN_PASSWORD", "Admin123!")
API_TOKEN = os.environ.get("NOCODB_API_TOKEN", "")
MANIFEST_PATH = Path(
    os.environ.get(
        "NOCODB_SEED_MANIFEST",
        Path(__file__).resolve().parents[1] / "seed_manifest.json",
    )
)


class SeedError(RuntimeError):
    pass


def _get_auth_token():
    """Sign in and return JWT auth token."""
    resp = requests.post(
        f"{BASE_URL}/api/v1/auth/user/signin",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        timeout=30,
    )
    if resp.status_code != 200:
        raise SeedError(f"NocoDB signin failed ({resp.status_code}): {resp.text[:500]}")
    token = resp.json().get("token")
    if not token:
        raise SeedError("No token in signin response")
    return token


def _headers(token):
    return {"xc-auth": token, "Content-Type": "application/json"}


def api(method, path, token, expected=None, **kwargs):
    url = f"{BASE_URL}/{path.lstrip('/')}"
    resp = requests.request(method, url, headers=_headers(token), timeout=30, **kwargs)
    if expected and resp.status_code not in expected:
        raise SeedError(f"{method} {url} -> {resp.status_code}: {resp.text[:500]}")
    return resp


def wait_for_nocodb(max_wait=120, interval=2):
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            resp = requests.get(f"{BASE_URL}/api/v1/health", timeout=5)
            if resp.status_code == 200:
                print("[seed] NocoDB API is ready")
                return
        except Exception:
            pass
        time.sleep(interval)
    raise SeedError(f"NocoDB API not ready after {max_wait}s")


def signup_admin(email, password):
    resp = requests.post(
        f"{BASE_URL}/api/v1/auth/user/signup",
        json={"email": email, "password": password},
        timeout=30,
    )
    if resp.status_code == 200:
        print(f"[seed] Admin user {email} created")
        return
    if resp.status_code in (400, 409):
        print(f"[seed] Admin user already exists, signing in")
        return
    raise SeedError(f"Signup failed ({resp.status_code}): {resp.text[:500]}")


def get_default_workspace(token):
    resp = api("GET", "api/v2/meta/workspaces/", token, expected=[200])
    ws_list = resp.json().get("list", [])
    if not ws_list:
        raise SeedError("No workspace found")
    return ws_list[0]


def list_bases(token, workspace_id):
    resp = api("GET", f"api/v2/meta/workspaces/{workspace_id}/bases/", token, expected=[200])
    return resp.json().get("list", [])


def create_base(token, workspace_id, name, description=""):
    resp = api(
        "POST",
        f"api/v2/meta/workspaces/{workspace_id}/bases/",
        token,
        expected=[200, 201],
        json={"title": name, "description": description},
    )
    base = resp.json()
    print(f"[seed] Created base: {name} (id={base.get('id', '?')})")
    return base


def list_tables(token, base_id):
    resp = api("GET", f"api/v2/meta/bases/{base_id}/tables", token, expected=[200])
    return resp.json().get("list", [])


def create_table(token, base_id, name, columns):
    table_columns = [
        {"column_name": col["name"], "title": col["name"], "uidt": col["uidt"]}
        for col in columns
    ]
    resp = api(
        "POST",
        f"api/v2/meta/bases/{base_id}/tables",
        token,
        expected=[200, 201],
        json={"table_name": name, "title": name, "columns": table_columns},
    )
    table = resp.json()
    print(f"[seed]   Created table: {name} (id={table.get('id', '?')})")
    return table


def insert_records(token, table_id, records):
    if not records:
        return
    resp = api(
        "POST",
        f"api/v2/tables/{table_id}/records",
        token,
        expected=[200, 201],
        json=records,
    )
    result = resp.json()
    count = len(result) if isinstance(result, list) else 1
    print(f"[seed]     Inserted {count} records")
    return result


def create_api_token(token):
    try:
        resp = api(
            "POST",
            "api/v1/tokens",
            token,
            expected=[200, 201],
            json={"description": "pipeline-bootstrap"},
        )
    except SeedError as exc:
        print(f"[seed] Skipping API token creation: {exc}")
        return ""
    data = resp.json()
    api_token = data.get("token", "")
    if api_token:
        print(f"[seed] Created API token: {api_token[:8]}...")
    return api_token


def seed_from_manifest(token, workspace_id, manifest_path):
    with open(manifest_path) as f:
        manifest = json.load(f)

    existing_bases = {b["title"]: b for b in list_bases(token, workspace_id)}

    for base_spec in manifest.get("bases", []):
        base_name = base_spec["name"]
        if base_name in existing_bases:
            print(f"[seed] Base '{base_name}' already exists, skipping")
            continue

        base = create_base(token, workspace_id, base_name, base_spec.get("description", ""))
        base_id = base["id"]

        existing_tables = {t["title"]: t for t in list_tables(token, base_id)}

        for table_spec in base_spec.get("tables", []):
            table_name = table_spec["name"]
            if table_name in existing_tables:
                print(f"[seed]   Table '{table_name}' already exists, skipping")
                table = existing_tables[table_name]
            else:
                table = create_table(
                    token, base_id, table_name, table_spec.get("columns", [])
                )
            table_id = table["id"]
            insert_records(token, table_id, table_spec.get("records", []))


def main():
    wait_for_nocodb()

    signup_admin(ADMIN_EMAIL, ADMIN_PASSWORD)
    token = _get_auth_token()

    api_token = create_api_token(token)

    ws = get_default_workspace(token)
    ws_id = ws["id"]
    print(f"[seed] Using workspace: {ws['title']} (id={ws_id})")

    seed_from_manifest(token, ws_id, MANIFEST_PATH)

    print("[seed] NocoDB seeding complete")
    if api_token:
        print(f"NOCODB_API_TOKEN={api_token}")


if __name__ == "__main__":
    main()
