#!/usr/bin/env python3
"""
ownCloud (oCIS) seed data script.

Creates folders and files through WebDAV and shared links through the OCS API.
oCIS uses HTTPS + Basic Auth.
"""

import json
import os
import time
from pathlib import Path

import requests
from requests.auth import HTTPBasicAuth

BASE_URL = os.environ.get("OWNCLOUD_BASE_URL", "https://localhost:9200").rstrip("/")
ADMIN_USER = os.environ.get("OWNCLOUD_ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("OWNCLOUD_ADMIN_PASSWORD", "Admin123!")
PUBLIC_LINK_PASSWORD = os.environ.get("OWNCLOUD_PUBLIC_LINK_PASSWORD", "Share123!")
MANIFEST_PATH = Path(
    os.environ.get(
        "OWNCLOUD_SEED_MANIFEST",
        Path(__file__).resolve().parents[1] / "seed_manifest.json",
    )
)

# oCIS uses self-signed certs in dev mode
VERIFY_SSL = False

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class SeedError(RuntimeError):
    pass


def _auth():
    return HTTPBasicAuth(ADMIN_USER, ADMIN_PASSWORD)


def _webdav_url(path=""):
    return f"{BASE_URL}/dav/files/{ADMIN_USER}/{path.lstrip('/')}"


def _ocs_url(path=""):
    return f"{BASE_URL}/{path.lstrip('/')}"


def wait_for_owncloud(max_wait=120, interval=3):
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            resp = requests.request(
                "PROPFIND",
                _webdav_url(),
                auth=_auth(),
                verify=VERIFY_SSL,
                timeout=5,
                headers={"Depth": "0"},
            )
            if resp.status_code == 207:
                print("[seed] ownCloud WebDAV is ready")
                return
        except Exception:
            pass
        time.sleep(interval)
    raise SeedError(f"ownCloud API not ready after {max_wait}s")


def ensure_folder(path):
    url = _webdav_url(path)
    resp = requests.request("MKCOL", url, auth=_auth(), verify=VERIFY_SSL, timeout=15)
    if resp.status_code in (201, 405):
        # 201=created, 405=already exists
        return
    if resp.status_code == 409:
        # parent missing — shouldn't happen if we create in order
        raise SeedError(f"MKCOL {path} -> 409 Conflict (parent missing?)")
    raise SeedError(f"MKCOL {path} -> {resp.status_code}: {resp.text[:300]}")


def upload_file(path, content):
    url = _webdav_url(path)
    resp = requests.put(
        url, data=content.encode("utf-8"),
        auth=_auth(), verify=VERIFY_SSL, timeout=15,
        headers={"Content-Type": "application/octet-stream"},
    )
    if resp.status_code in (201, 204):
        return
    raise SeedError(f"PUT {path} -> {resp.status_code}: {resp.text[:300]}")


def file_exists(path):
    url = _webdav_url(path)
    resp = requests.request(
        "PROPFIND", url, auth=_auth(), verify=VERIFY_SSL, timeout=15,
        headers={"Depth": "0"},
    )
    return resp.status_code == 207


def list_existing_shares():
    url = _ocs_url("ocs/v2.php/apps/files_sharing/api/v1/shares")
    resp = requests.get(
        url,
        auth=_auth(),
        verify=VERIFY_SSL,
        timeout=15,
        headers={"OCS-APIREQUEST": "true", "Accept": "application/json"},
        params={"format": "json"},
    )
    if resp.status_code != 200:
        raise SeedError(f"GET shares -> {resp.status_code}: {resp.text[:300]}")
    try:
        return resp.json().get("ocs", {}).get("data", []) or []
    except Exception as exc:
        raise SeedError(f"Failed to parse share list: {exc}") from exc


def _share_exists(existing_shares, path, share_type, share_with=""):
    normalized_path = f"/{path.lstrip('/')}"
    for share in existing_shares:
        if str(share.get("path", "")) != normalized_path:
            continue
        if str(share.get("share_type", "")) != str(share_type):
            continue
        if share_with and str(share.get("share_with", "")) != str(share_with):
            continue
        return True
    return False


def create_public_share(path, name="", permissions=1, password="", expire_date=""):
    url = _ocs_url("ocs/v2.php/apps/files_sharing/api/v1/shares")
    effective_password = password or PUBLIC_LINK_PASSWORD
    payload = {
        "path": f"/{path.lstrip('/')}",
        "shareType": "3",
        "permissions": str(permissions),
        "name": name or path.split("/")[-1],
    }
    if effective_password:
        payload["password"] = effective_password
    if expire_date:
        payload["expireDate"] = expire_date
    resp = requests.post(
        url,
        auth=_auth(),
        verify=VERIFY_SSL,
        timeout=15,
        headers={"OCS-APIREQUEST": "true", "Accept": "application/json"},
        data=payload,
    )
    if resp.status_code in (200, 201):
        print(f"[seed]   Created public share for: {path}")
        return
    raise SeedError(f"POST public share {path} -> {resp.status_code}: {resp.text[:300]}")


def create_user_share(path, share_with, permissions=1):
    url = _ocs_url("ocs/v2.php/apps/files_sharing/api/v1/shares")
    resp = requests.post(
        url,
        auth=_auth(),
        verify=VERIFY_SSL,
        timeout=15,
        headers={"OCS-APIREQUEST": "true", "Accept": "application/json"},
        data={
            "path": f"/{path.lstrip('/')}",
            "shareType": "0",
            "shareWith": share_with,
            "permissions": str(permissions),
        },
    )
    if resp.status_code in (200, 201):
        print(f"[seed]   Created user share for: {path} -> {share_with}")
        return
    raise SeedError(f"POST user share {path} -> {resp.status_code}: {resp.text[:300]}")


def seed_from_manifest(manifest_path):
    with open(manifest_path) as f:
        manifest = json.load(f)

    # Create folders
    for folder in manifest.get("folders", []):
        ensure_folder(folder)
        print(f"[seed]   Folder: {folder}")

    # Upload files
    for file_spec in manifest.get("files", []):
        path = file_spec["path"]
        if file_exists(path):
            print(f"[seed]   File exists, skipping: {path}")
            continue
        upload_file(path, file_spec["content"])
        print(f"[seed]   Uploaded: {path}")

    # Create shares
    existing_shares = list_existing_shares()
    for share_spec in manifest.get("shares", []):
        share_type = share_spec.get("share_type", "public_link")
        share_type_code = 3 if share_type == "public_link" else 0
        path = share_spec["path"]
        share_with = share_spec.get("share_with", "")
        if _share_exists(existing_shares, path, share_type_code, share_with=share_with):
            print(f"[seed]   Share exists, skipping: {path} ({share_type})")
            continue
        if share_type == "public_link":
            create_public_share(
                path,
                share_spec.get("name", ""),
                permissions=share_spec.get("permissions", 1),
                password=share_spec.get("password", ""),
                expire_date=share_spec.get("expire_date", ""),
            )
        elif share_type == "user":
            create_user_share(
                path,
                share_with=share_with,
                permissions=share_spec.get("permissions", 1),
            )
        else:
            raise SeedError(f"Unsupported share_type: {share_type}")
        existing_shares = list_existing_shares()


def main():
    wait_for_owncloud()
    print(f"[seed] Seeding ownCloud as user '{ADMIN_USER}'")
    seed_from_manifest(MANIFEST_PATH)
    print("[seed] ownCloud seeding complete")


if __name__ == "__main__":
    main()
