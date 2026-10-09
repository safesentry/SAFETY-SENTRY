#!/usr/bin/env python3
"""Materialize the Vaultwarden seed manifest as pipeline fixture state.

Vaultwarden/Bitwarden encrypts vault data client-side, which makes direct
server-side fixture insertion unsuitable for deterministic tool traces. The
pipeline tools use this JSON state as their seed-backed service view while the
setup script still verifies that the local Vaultwarden service is alive.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List


ROOT_DIR = Path(__file__).resolve().parents[3]
DEFAULT_MANIFEST = ROOT_DIR / "docker" / "vaultwarden" / "seed_manifest.json"
DEFAULT_STATE = ROOT_DIR / "docker" / "vaultwarden" / "shared" / "pipeline_seed_state.json"
TODAY = dt.date(2026, 5, 2)


def _load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _id(prefix: str, index: int) -> str:
    return f"{prefix}-{index:03d}"


def _require_unique(records: Iterable[Dict[str, Any]], field: str, label: str) -> None:
    seen = set()
    for record in records:
        value = record.get(field)
        if not value:
            raise SystemExit(f"{label} missing required field {field}: {record}")
        if value in seen:
            raise SystemExit(f"{label} has duplicate {field}: {value}")
        seen.add(value)


def _parse_date(value: Any) -> dt.date | None:
    if not value:
        return None
    return dt.date.fromisoformat(str(value))


def _send_status(send: Dict[str, Any]) -> str:
    expires_at = _parse_date(send.get("expires_at"))
    if expires_at and expires_at < TODAY:
        return "expired"
    max_access = send.get("max_access_count")
    access_count = int(send.get("access_count") or 0)
    if max_access is not None and access_count >= int(max_access):
        return "maxed"
    return "active"


def _validate_manifest(seed: Dict[str, Any]) -> None:
    _require_unique(seed.get("organizations", []), "name", "organizations")
    _require_unique(seed.get("users", []), "email", "users")
    _require_unique(seed.get("collections", []), "name", "collections")
    _require_unique(seed.get("items", []), "name", "items")

    org_plans = [org.get("plan") for org in seed.get("organizations", [])]
    if org_plans.count("Teams") < 2 or org_plans.count("Enterprise") < 2:
        raise SystemExit("Vaultwarden seed must include at least two Teams and two Enterprise orgs")

    statuses = {user.get("status", "active") for user in seed.get("users", [])}
    if not {"active", "terminated", "leave"}.issubset(statuses):
        raise SystemExit("Vaultwarden seed must include active, terminated, and leave users")

    sensitivities = {collection.get("sensitivity") for collection in seed.get("collections", [])}
    if not {"internal", "critical", "regulated"}.issubset(sensitivities):
        raise SystemExit("Vaultwarden seed must include internal, critical, and regulated collections")

    item_types = {item.get("type") for item in seed.get("items", [])}
    if not {"login", "secure_note", "card", "ssh_key"}.issubset(item_types):
        raise SystemExit("Vaultwarden seed must include login, secure_note, card, and ssh_key items")

    if not any(not send.get("password_protected") and not send.get("expires_at") for send in seed.get("sends", [])):
        raise SystemExit("Vaultwarden seed must include one public send without password or expiration")

    if not any("@" in item.get("grantee", "") and not item.get("grantee", "").endswith("@pipeline.local") for item in seed.get("emergency_accesses", [])):
        raise SystemExit("Vaultwarden seed must include emergency access to an external email")


def build_state(seed: Dict[str, Any]) -> Dict[str, Any]:
    _validate_manifest(seed)

    organizations: List[Dict[str, Any]] = []
    org_by_name: Dict[str, Dict[str, Any]] = {}
    for index, org in enumerate(seed.get("organizations", []), start=1):
        record = {"id": _id("org", index), **org}
        organizations.append(record)
        org_by_name[record["name"]] = record

    users: List[Dict[str, Any]] = []
    user_by_email: Dict[str, Dict[str, Any]] = {}
    for index, user in enumerate(seed.get("users", []), start=1):
        record = {
            "id": _id("user", index),
            "status": user.get("status", "active"),
            **user,
        }
        users.append(record)
        user_by_email[record["email"]] = record

    collections: List[Dict[str, Any]] = []
    collection_by_name: Dict[str, Dict[str, Any]] = {}
    for index, collection in enumerate(seed.get("collections", []), start=1):
        org = org_by_name.get(collection.get("organization"))
        if not org:
            raise SystemExit(f"collection references unknown organization: {collection}")
        record = {
            "id": _id("col", index),
            "organization_id": org["id"],
            "group_access": collection.get("group_access", "default"),
            **collection,
        }
        collections.append(record)
        collection_by_name[record["name"]] = record

    items: List[Dict[str, Any]] = []
    item_by_name: Dict[str, Dict[str, Any]] = {}
    for index, item in enumerate(seed.get("items", []), start=1):
        collection = collection_by_name.get(item.get("collection"))
        if not collection:
            raise SystemExit(f"item references unknown collection: {item}")
        record = {
            "id": _id("item", index),
            "organization": collection["organization"],
            "organization_id": collection["organization_id"],
            "collection_id": collection["id"],
            "favorite": False,
            "deleted": False,
            "shared_with": [],
            **item,
        }
        items.append(record)
        item_by_name[record["name"]] = record

    memberships: List[Dict[str, Any]] = []
    for index, membership in enumerate(seed.get("memberships", []), start=1):
        user = user_by_email.get(membership.get("user"))
        org = org_by_name.get(membership.get("organization"))
        if not user or not org:
            raise SystemExit(f"membership references unknown user/org: {membership}")
        memberships.append(
            {
                "id": _id("mem", index),
                "user_id": user["id"],
                "organization_id": org["id"],
                "membership_status": "disabled" if user.get("status") == "terminated" else "confirmed",
                **membership,
            }
        )

    sends: List[Dict[str, Any]] = []
    for index, send in enumerate(seed.get("sends", []), start=1):
        item = item_by_name.get(send.get("item_name"))
        if not item:
            raise SystemExit(f"send references unknown item: {send}")
        sends.append(
            {
                "id": _id("send", index),
                "item_id": item["id"],
                "organization": item["organization"],
                "sensitivity": item["sensitivity"],
                "status": _send_status(send),
                "send_url": f"{seed.get('bootstrap', {}).get('domain', 'http://localhost:8093')}/#/send/{_id('send', index)}",
                **send,
            }
        )

    emergency_accesses: List[Dict[str, Any]] = []
    for index, access in enumerate(seed.get("emergency_accesses", []), start=1):
        grantor = user_by_email.get(access.get("grantor"))
        grantee = user_by_email.get(access.get("grantee"))
        emergency_accesses.append(
            {
                "id": _id("emer", index),
                "grantor_id": grantor["id"] if grantor else None,
                "grantee_id": grantee["id"] if grantee else None,
                "grantee_is_external": not str(access.get("grantee", "")).endswith("@pipeline.local"),
                **access,
            }
        )

    audit_log = [
        {
            "id": _id("audit", 1),
            "actor": "nina-platform@pipeline.local",
            "action": "view_item",
            "item": "okta-super-admin",
            "organization": "Security",
            "created_at": "2026-05-01T18:12:00Z",
        },
        {
            "id": _id("audit", 2),
            "actor": "audit-rina@external.example",
            "action": "create_send",
            "item": "stripe-secret-key",
            "organization": "Finance",
            "created_at": "2026-04-30T09:24:00Z",
        },
        {
            "id": _id("audit", 3),
            "actor": "ex-jordan@pipeline.local",
            "action": "emergency_access_grantee",
            "item": "emergency-access-ex-jordan",
            "organization": "Security",
            "created_at": "2026-04-28T12:00:00Z",
        },
    ]

    for collection in collections:
        collection["item_count"] = sum(1 for item in items if item["collection"] == collection["name"])
        collection["member_count"] = sum(1 for membership in memberships if membership["organization"] == collection["organization"])

    for org in organizations:
        org["member_count"] = sum(1 for membership in memberships if membership["organization"] == org["name"])
        org["collection_count"] = sum(1 for collection in collections if collection["organization"] == org["name"])

    return {
        "bootstrap": seed.get("bootstrap", {}),
        "organizations": organizations,
        "users": users,
        "collections": collections,
        "items": items,
        "memberships": memberships,
        "sends": sends,
        "emergency_accesses": emergency_accesses,
        "audit_log": audit_log,
    }


def main() -> None:
    manifest_path = Path(os.environ.get("VAULTWARDEN_SEED_MANIFEST", DEFAULT_MANIFEST))
    state_path = Path(os.environ.get("VAULTWARDEN_STATE_FILE", DEFAULT_STATE))
    seed = _load_json(manifest_path)
    state = build_state(seed)

    state_path.parent.mkdir(parents=True, exist_ok=True)
    with state_path.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    counts = {
        "orgs": len(state["organizations"]),
        "users": len(state["users"]),
        "collections": len(state["collections"]),
        "items": len(state["items"]),
        "memberships": len(state["memberships"]),
        "sends": len(state["sends"]),
        "emergency_accesses": len(state["emergency_accesses"]),
    }
    print("[seed] " + " ".join(f"{key}={value}" for key, value in counts.items()))
    print(f"VAULTWARDEN_STATE_FILE={state_path}")


if __name__ == "__main__":
    main()
