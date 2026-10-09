#!/usr/bin/env python3
"""
Mailu seed data script.

Populate domains, users, aliases, relays, and alternative domains via Admin API,
then inject seed emails via SMTP.
"""

import json
import os
import smtplib
import time
from email.mime.text import MIMEText
from pathlib import Path

import requests

BASE_URL = os.environ.get("MAILU_BASE_URL", "http://localhost:8443").rstrip("/")
API_TOKEN = os.environ.get("MAILU_API_TOKEN", "")
SMTP_HOST = os.environ.get("MAILU_SMTP_HOST", "localhost")
SMTP_PORT = int(os.environ.get("MAILU_SMTP_PORT", "2525"))
MANIFEST_PATH = Path(
    os.environ.get(
        "MAILU_SEED_MANIFEST",
        Path(__file__).resolve().parents[1] / "seed_manifest.json",
    )
)


def api(method, endpoint, data=None):
    url = f"{BASE_URL}/api/v1/{endpoint.lstrip('/')}"
    headers = {"Authorization": f"Bearer {API_TOKEN}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    resp = requests.request(method, url, headers=headers, json=data, timeout=30)
    if resp.status_code >= 400:
        detail = resp.text[:500]
        if resp.status_code == 409:
            print(f"  [skip] already exists: {endpoint}")
            return None
        raise RuntimeError(f"API {method} {endpoint} -> {resp.status_code}: {detail}")
    if resp.content:
        return resp.json()
    return None


def wait_for_api(max_wait=180, interval=3):
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            resp = requests.get(f"{BASE_URL}/api/v1/domain", timeout=5,
                                headers={"Authorization": f"Bearer {API_TOKEN}"})
            if resp.status_code in (200, 401, 403):
                print("[seed] Mailu Admin API is ready")
                return
        except Exception:
            pass
        time.sleep(interval)
    raise RuntimeError(f"Mailu Admin API not ready after {max_wait}s")


def seed_domains(domains):
    print("[seed] Creating domains ...")
    for d in domains:
        payload = {"name": d["name"]}
        if "max_users" in d:
            payload["max_users"] = d["max_users"]
        if "max_aliases" in d:
            payload["max_aliases"] = d["max_aliases"]
        result = api("POST", "/domain", payload)
        if result:
            print(f"  + domain: {d['name']}")


def seed_users(users):
    print("[seed] Creating users ...")
    for u in users:
        local, domain = u["email"].split("@", 1)
        payload = {
            "email": u["email"],
            "raw_password": u["password"],
            "displayed_name": u.get("display_name", local),
            "enabled": True,
        }
        if "quota" in u:
            payload["quota_bytes"] = u["quota"]
        if u.get("is_admin"):
            payload["global_admin"] = True
        result = api("POST", "/user", payload)
        if result:
            print(f"  + user: {u['email']}")


def seed_aliases(aliases):
    print("[seed] Creating aliases ...")
    for a in aliases:
        local_part, domain = a["source"].split("@", 1)
        destinations = [d.strip() for d in a["destination"].split(",")]
        payload = {
            "localpart": local_part,
            "destination": destinations,
            "enabled": True,
        }
        if "comment" in a:
            payload["comment"] = a["comment"]
        payload["email"] = a["source"]
        try:
            result = api("POST", "/alias", payload)
        except RuntimeError as exc:
            print(f"  ! alias skipped ({a['source']}): {exc}")
            continue
        if result:
            print(f"  + alias: {a['source']} -> {a['destination']}")


def seed_relays(relays):
    print("[seed] Creating relays ...")
    for r in relays:
        payload = {"name": r["name"], "smtp": r.get("smtp", "")}
        result = api("POST", "/relay", payload)
        if result:
            print(f"  + relay: {r['name']}")


def seed_alternative_domains(alt_domains):
    print("[seed] Creating alternative domains ...")
    for ad in alt_domains:
        payload = {"name": ad["name"]}
        payload["domain"] = ad["domain"]
        result = api("POST", "/alternative", payload)
        if result:
            print(f"  + alternative: {ad['name']} -> {ad['domain']}")


def seed_emails(emails):
    print("[seed] Injecting seed emails via SMTP ...")
    for e in emails:
        msg = MIMEText(e["body"], "plain", "utf-8")
        msg["From"] = e["from"]
        msg["To"] = e["to"]
        msg["Subject"] = e["subject"]
        sender_local, sender_domain = e["from"].split("@", 1)
        manifest = json.loads(MANIFEST_PATH.read_text())
        sender_password = None
        for u in manifest.get("users", []):
            if u["email"] == e["from"]:
                sender_password = u["password"]
                break
        if not sender_password:
            print(f"  [skip] no password found for sender {e['from']}")
            continue
        try:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
                smtp.login(e["from"], sender_password)
                smtp.sendmail(e["from"], [e["to"]], msg.as_string())
            print(f"  + email: {e['from']} -> {e['to']} ({e['subject']})")
        except Exception as exc:
            print(f"  [warn] failed to send {e['from']} -> {e['to']}: {exc}")


def main():
    manifest = json.loads(MANIFEST_PATH.read_text())
    wait_for_api()

    seed_domains(manifest.get("domains", []))
    seed_users(manifest.get("users", []))
    seed_aliases(manifest.get("aliases", []))
    seed_relays(manifest.get("relays", []))
    seed_alternative_domains(manifest.get("alternative_domains", []))

    time.sleep(3)
    seed_emails(manifest.get("seed_emails", []))

    print("[seed] Mailu seeding complete")


if __name__ == "__main__":
    main()
