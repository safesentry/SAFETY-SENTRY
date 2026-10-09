"""
Vaultwarden tool registration.

The local Vaultwarden container provides the service shell, while the pipeline
tools operate on a deterministic seed-backed fixture state. This avoids
Bitwarden's client-side encryption requirements blocking reproducible training
traces.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
from functools import wraps
from pathlib import Path
from typing import Any, Dict, Iterable, List

from ...service_tools import ServiceToolRegistry


REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_MANIFEST = REPO_ROOT / "docker" / "vaultwarden" / "seed_manifest.json"
DEFAULT_STATE = REPO_ROOT / "docker" / "vaultwarden" / "shared" / "pipeline_seed_state.json"

_REGISTRY = ServiceToolRegistry(service_id="vaultwarden")


def _vaultwarden_tool(name, description, params, required=None, is_write=False, group="", short_description=""):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return _format_json(func(*args, **kwargs))
            except Exception as exc:
                return _format_json({"error": f"{type(exc).__name__}: {exc}"})

        return _REGISTRY.register(
            name=name,
            description=description,
            params=params,
            required=required,
            is_write=is_write,
            group=group,
            short_description=short_description,
        )(wrapper)

    return decorator


def get_all_schemas():
    return _REGISTRY.get_all_schemas()


def call_tool(name, args):
    return _REGISTRY.call_tool(name, args)


def get_tool_names():
    return _REGISTRY.get_tool_names()


def _state_path() -> Path:
    return Path(os.environ.get("VAULTWARDEN_STATE_FILE", DEFAULT_STATE))


def _base_url() -> str:
    return os.environ.get("VAULTWARDEN_BASE_URL", "http://localhost:8093").rstrip("/")


def _manifest_path() -> Path:
    return Path(os.environ.get("VAULTWARDEN_SEED_MANIFEST", DEFAULT_MANIFEST))


def _format_json(data):
    if isinstance(data, str):
        return data
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def _load_seed_builder():
    script_path = REPO_ROOT / "docker" / "vaultwarden" / "scripts" / "seed_vaultwarden_data.py"
    spec = importlib.util.spec_from_file_location("_pipeline_vaultwarden_seed", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load Vaultwarden seed builder from {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _build_state_from_manifest() -> Dict[str, Any]:
    with _manifest_path().open("r", encoding="utf-8") as fh:
        seed = json.load(fh)
    return _load_seed_builder().build_state(seed)


def _load_state() -> Dict[str, Any]:
    path = _state_path()
    if path.exists():
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    return _build_state_from_manifest()


def _save_state(state: Dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2, default=str)
        fh.write("\n")


def _normalize(value: Any) -> str:
    return str(value or "").strip().lower()


def _coerce_tags(value: Any) -> List[str]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [part.strip() for part in str(value).split(",") if part.strip()]


def _coerce_emails(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [part.strip() for part in str(value or "").replace(";", ",").split(",") if part.strip()]


def _next_id(records: Iterable[Dict[str, Any]], prefix: str) -> str:
    max_seen = 0
    marker = f"{prefix}-"
    for record in records:
        value = str(record.get("id", ""))
        if value.startswith(marker):
            try:
                max_seen = max(max_seen, int(value.split("-", 1)[1]))
            except ValueError:
                continue
    return f"{prefix}-{max_seen + 1:03d}"


def _find_by_id_or_field(records: Iterable[Dict[str, Any]], value: Any, *fields: str) -> Dict[str, Any] | None:
    needle = _normalize(value)
    if not needle:
        return None
    for record in records:
        candidates = [record.get("id")]
        candidates.extend(record.get(field) for field in fields)
        if needle in {_normalize(candidate) for candidate in candidates}:
            return record
    return None


def _find_org(state: Dict[str, Any], org_id_or_name: Any) -> Dict[str, Any] | None:
    return _find_by_id_or_field(state.get("organizations", []), org_id_or_name, "name", "billing_email")


def _find_collection(state: Dict[str, Any], collection_id_or_name: Any, organization: str = "") -> Dict[str, Any] | None:
    collection = _find_by_id_or_field(state.get("collections", []), collection_id_or_name, "name")
    if not collection:
        return None
    if organization and _normalize(collection.get("organization")) != _normalize(organization):
        return None
    return collection


def _find_item(state: Dict[str, Any], item_id_or_name: Any) -> Dict[str, Any] | None:
    return _find_by_id_or_field(state.get("items", []), item_id_or_name, "name", "username", "uri")


def _find_user(state: Dict[str, Any], email_or_id: Any) -> Dict[str, Any] | None:
    return _find_by_id_or_field(state.get("users", []), email_or_id, "email", "name")


def _find_send(state: Dict[str, Any], send_id: Any) -> Dict[str, Any] | None:
    return _find_by_id_or_field(state.get("sends", []), send_id, "item_name", "send_url")


def _find_emergency(state: Dict[str, Any], emergency_id: Any) -> Dict[str, Any] | None:
    return _find_by_id_or_field(state.get("emergency_accesses", []), emergency_id, "grantor", "grantee")


def _actor() -> str:
    return os.environ.get("VAULTWARDEN_ACTOR_EMAIL", "admin@pipeline.local")


def _audit(state: Dict[str, Any], action: str, item: str = "", organization: str = "", actor: str = "") -> None:
    records = state.setdefault("audit_log", [])
    records.append(
        {
            "id": _next_id(records, "audit"),
            "actor": actor or _actor(),
            "action": action,
            "item": item,
            "organization": organization,
            "created_at": "2026-05-02T00:00:00Z",
        }
    )


def _item_summary(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: item.get(key)
        for key in (
            "id",
            "name",
            "type",
            "sensitivity",
            "organization",
            "collection",
            "username",
            "uri",
            "tags",
            "rotation_days",
            "deleted",
        )
        if key in item
    }


@_vaultwarden_tool(
    "list_organizations",
    "List Vaultwarden organizations.",
    {"count": {"type": "integer"}, "offset": {"type": "integer"}},
    group="organizations",
)
def list_organizations(count=100, offset=0):
    """List Vaultwarden organizations."""
    state = _load_state()
    records = state.get("organizations", [])
    return records[int(offset): int(offset) + int(count)]


@_vaultwarden_tool(
    "get_organization",
    "Get a Vaultwarden organization by id or name.",
    {"org_id_or_name": {"type": "string"}},
    required=["org_id_or_name"],
    group="organizations",
)
def get_organization(org_id_or_name):
    """Get a Vaultwarden organization by id or name."""
    state = _load_state()
    org = _find_org(state, org_id_or_name)
    if not org:
        return {"error": f"organization not found: {org_id_or_name}"}
    members = [m for m in state.get("memberships", []) if m.get("organization") == org.get("name")]
    collections = [c for c in state.get("collections", []) if c.get("organization") == org.get("name")]
    return {**org, "memberships": members, "collections": collections}


@_vaultwarden_tool(
    "list_organization_members",
    "List members of one Vaultwarden organization.",
    {"org_id_or_name": {"type": "string"}},
    required=["org_id_or_name"],
    group="organizations",
)
def list_organization_members(org_id_or_name):
    """List members of one Vaultwarden organization."""
    state = _load_state()
    org = _find_org(state, org_id_or_name)
    if not org:
        return {"error": f"organization not found: {org_id_or_name}"}
    users = {user["email"]: user for user in state.get("users", [])}
    out = []
    for membership in state.get("memberships", []):
        if membership.get("organization") != org.get("name"):
            continue
        user = users.get(membership.get("user"), {})
        out.append({**membership, "name": user.get("name"), "status": user.get("status"), "is_external": user.get("is_external")})
    return out


@_vaultwarden_tool(
    "list_collections",
    "List Vaultwarden collections, optionally filtered by organization.",
    {"organization": {"type": "string"}, "count": {"type": "integer"}, "offset": {"type": "integer"}},
    group="collections",
)
def list_collections(organization="", count=100, offset=0):
    """List Vaultwarden collections, optionally filtered by organization."""
    state = _load_state()
    records = [
        collection
        for collection in state.get("collections", [])
        if not organization or _normalize(collection.get("organization")) == _normalize(organization)
    ]
    return records[int(offset): int(offset) + int(count)]


@_vaultwarden_tool(
    "get_collection",
    "Get a Vaultwarden collection by id or name.",
    {"collection_id_or_name": {"type": "string"}, "organization": {"type": "string"}},
    required=["collection_id_or_name"],
    group="collections",
)
def get_collection(collection_id_or_name, organization=""):
    """Get a Vaultwarden collection by id or name."""
    state = _load_state()
    collection = _find_collection(state, collection_id_or_name, organization=organization)
    if not collection:
        return {"error": f"collection not found: {collection_id_or_name}"}
    items = [item for item in state.get("items", []) if item.get("collection") == collection.get("name") and not item.get("deleted")]
    return {**collection, "items": [_item_summary(item) for item in items]}


@_vaultwarden_tool(
    "list_collection_items",
    "List items inside a Vaultwarden collection.",
    {"collection_id_or_name": {"type": "string"}, "count": {"type": "integer"}, "offset": {"type": "integer"}},
    required=["collection_id_or_name"],
    group="items",
)
def list_collection_items(collection_id_or_name, count=100, offset=0):
    """List items inside a Vaultwarden collection."""
    state = _load_state()
    collection = _find_collection(state, collection_id_or_name)
    if not collection:
        return {"error": f"collection not found: {collection_id_or_name}"}
    records = [item for item in state.get("items", []) if item.get("collection") == collection.get("name") and not item.get("deleted")]
    return [_item_summary(item) for item in records[int(offset): int(offset) + int(count)]]


@_vaultwarden_tool(
    "list_items",
    "List Vaultwarden items with optional organization, collection, and type filters.",
    {
        "organization": {"type": "string"},
        "collection": {"type": "string"},
        "type_filter": {"type": "string"},
        "count": {"type": "integer"},
        "offset": {"type": "integer"},
    },
    group="items",
)
def list_items(organization="", collection="", type_filter="", count=100, offset=0):
    """List Vaultwarden items with optional organization, collection, and type filters."""
    state = _load_state()
    records = []
    for item in state.get("items", []):
        if item.get("deleted"):
            continue
        if organization and _normalize(item.get("organization")) != _normalize(organization):
            continue
        if collection and _normalize(item.get("collection")) != _normalize(collection):
            continue
        if type_filter and _normalize(item.get("type")) != _normalize(type_filter):
            continue
        records.append(_item_summary(item))
    return records[int(offset): int(offset) + int(count)]


@_vaultwarden_tool(
    "get_item",
    "Get a Vaultwarden item by id or name.",
    {"item_id_or_name": {"type": "string"}},
    required=["item_id_or_name"],
    group="items",
)
def get_item(item_id_or_name):
    """Get a Vaultwarden item by id or name."""
    state = _load_state()
    item = _find_item(state, item_id_or_name)
    if not item or item.get("deleted"):
        return {"error": f"item not found: {item_id_or_name}"}
    return copy.deepcopy(item)


@_vaultwarden_tool(
    "search_items",
    "Search Vaultwarden items by keyword.",
    {"query": {"type": "string"}, "organization": {"type": "string"}, "count": {"type": "integer"}},
    required=["query"],
    group="items",
)
def search_items(query, organization="", count=50):
    """Search Vaultwarden items by keyword."""
    state = _load_state()
    needle = _normalize(query)
    results = []
    for item in state.get("items", []):
        if item.get("deleted"):
            continue
        if organization and _normalize(item.get("organization")) != _normalize(organization):
            continue
        haystack = " ".join(
            str(item.get(field, ""))
            for field in ("name", "type", "sensitivity", "username", "uri", "notes", "collection", "organization")
        )
        haystack = f"{haystack} {' '.join(item.get('tags') or [])}".lower()
        if needle in haystack:
            results.append(_item_summary(item))
    return results[: int(count)]


@_vaultwarden_tool(
    "list_users",
    "List Vaultwarden users, optionally filtered by organization.",
    {"organization": {"type": "string"}, "count": {"type": "integer"}, "offset": {"type": "integer"}},
    group="users",
)
def list_users(organization="", count=100, offset=0):
    """List Vaultwarden users, optionally filtered by organization."""
    state = _load_state()
    if not organization:
        records = state.get("users", [])
    else:
        emails = {
            membership.get("user")
            for membership in state.get("memberships", [])
            if _normalize(membership.get("organization")) == _normalize(organization)
        }
        records = [user for user in state.get("users", []) if user.get("email") in emails]
    return records[int(offset): int(offset) + int(count)]


@_vaultwarden_tool(
    "get_user",
    "Get a Vaultwarden user by email or id.",
    {"email_or_id": {"type": "string"}},
    required=["email_or_id"],
    group="users",
)
def get_user(email_or_id):
    """Get a Vaultwarden user by email or id."""
    state = _load_state()
    user = _find_user(state, email_or_id)
    if not user:
        return {"error": f"user not found: {email_or_id}"}
    memberships = [m for m in state.get("memberships", []) if m.get("user") == user.get("email")]
    emergency = [
        access
        for access in state.get("emergency_accesses", [])
        if access.get("grantor") == user.get("email") or access.get("grantee") == user.get("email")
    ]
    return {**user, "memberships": memberships, "emergency_accesses": emergency}


@_vaultwarden_tool(
    "list_user_memberships",
    "List Vaultwarden organization memberships for one user.",
    {"email_or_id": {"type": "string"}},
    required=["email_or_id"],
    group="users",
)
def list_user_memberships(email_or_id):
    """List Vaultwarden organization memberships for one user."""
    state = _load_state()
    user = _find_user(state, email_or_id)
    if not user:
        return {"error": f"user not found: {email_or_id}"}
    return [membership for membership in state.get("memberships", []) if membership.get("user") == user.get("email")]


@_vaultwarden_tool(
    "list_sends",
    "List Vaultwarden sends.",
    {"count": {"type": "integer"}, "offset": {"type": "integer"}},
    group="sends",
)
def list_sends(count=100, offset=0):
    """List Vaultwarden sends."""
    state = _load_state()
    records = state.get("sends", [])
    return records[int(offset): int(offset) + int(count)]


@_vaultwarden_tool(
    "get_send",
    "Get a Vaultwarden send by id.",
    {"send_id": {"type": "string"}},
    required=["send_id"],
    group="sends",
)
def get_send(send_id):
    """Get a Vaultwarden send by id."""
    state = _load_state()
    send = _find_send(state, send_id)
    if not send:
        return {"error": f"send not found: {send_id}"}
    return send


@_vaultwarden_tool(
    "list_emergency_accesses",
    "List Vaultwarden emergency access records.",
    {"direction": {"type": "string"}},
    group="emergency_access",
)
def list_emergency_accesses(direction="all"):
    """List Vaultwarden emergency access records."""
    state = _load_state()
    actor = _actor()
    direction = _normalize(direction or "all")
    records = []
    for access in state.get("emergency_accesses", []):
        if direction in {"all", ""}:
            records.append(access)
        elif direction == "granted_by_me" and _normalize(access.get("grantor")) == _normalize(actor):
            records.append(access)
        elif direction == "granted_to_me" and _normalize(access.get("grantee")) == _normalize(actor):
            records.append(access)
    return records


@_vaultwarden_tool(
    "get_emergency_access",
    "Get one Vaultwarden emergency access record.",
    {"emergency_id": {"type": "string"}},
    required=["emergency_id"],
    group="emergency_access",
)
def get_emergency_access(emergency_id):
    """Get one Vaultwarden emergency access record."""
    state = _load_state()
    access = _find_emergency(state, emergency_id)
    if not access:
        return {"error": f"emergency access not found: {emergency_id}"}
    return access


@_vaultwarden_tool(
    "list_audit_log",
    "List Vaultwarden audit events with optional actor, item, and day filters.",
    {"actor": {"type": "string"}, "item": {"type": "string"}, "days": {"type": "integer"}},
    group="audit",
)
def list_audit_log(actor="", item="", days=7):
    """List Vaultwarden audit events with optional actor, item, and day filters."""
    state = _load_state()
    results = []
    for event in state.get("audit_log", []):
        if actor and _normalize(event.get("actor")) != _normalize(actor):
            continue
        if item and _normalize(event.get("item")) != _normalize(item):
            continue
        results.append(event)
    return results[-int(days) * 20:]


@_vaultwarden_tool(
    "create_item",
    "Create a Vaultwarden item in a collection.",
    {
        "collection": {"type": "string"},
        "name": {"type": "string"},
        "type": {"type": "string"},
        "username": {"type": "string"},
        "password": {"type": "string"},
        "uri": {"type": "string"},
        "notes": {"type": "string"},
        "tags": {"type": "string"},
    },
    required=["collection", "name", "type"],
    is_write=True,
    group="items",
)
def create_item(collection, name, type, username="", password="", uri="", notes="", tags=""):
    """Create a Vaultwarden item in a collection."""
    state = _load_state()
    target = _find_collection(state, collection)
    if not target:
        return {"error": f"collection not found: {collection}"}
    if _find_item(state, name):
        return {"error": f"item already exists: {name}"}
    item = {
        "id": _next_id(state.get("items", []), "item"),
        "collection": target["name"],
        "collection_id": target["id"],
        "organization": target["organization"],
        "organization_id": target["organization_id"],
        "name": name,
        "type": type,
        "sensitivity": target.get("sensitivity", "internal"),
        "username": username,
        "password": password,
        "uri": uri,
        "notes": notes,
        "tags": _coerce_tags(tags),
        "favorite": False,
        "deleted": False,
        "shared_with": [],
    }
    state.setdefault("items", []).append(item)
    _audit(state, "create_item", item=name, organization=target["organization"])
    _save_state(state)
    return item


@_vaultwarden_tool(
    "update_item",
    "Update fields on a Vaultwarden item.",
    {
        "item_id_or_name": {"type": "string"},
        "name": {"type": "string"},
        "username": {"type": "string"},
        "password": {"type": "string"},
        "uri": {"type": "string"},
        "notes": {"type": "string"},
        "tags": {"type": "string"},
        "sensitivity": {"type": "string"},
        "collection": {"type": "string"},
    },
    required=["item_id_or_name"],
    is_write=True,
    group="items",
)
def update_item(item_id_or_name, name="", username="", password="", uri="", notes="", tags="", sensitivity="", collection=""):
    """Update fields on a Vaultwarden item."""
    state = _load_state()
    item = _find_item(state, item_id_or_name)
    if not item or item.get("deleted"):
        return {"error": f"item not found: {item_id_or_name}"}
    for field, value in {"name": name, "username": username, "password": password, "uri": uri, "notes": notes, "sensitivity": sensitivity}.items():
        if value != "":
            item[field] = value
    if tags != "":
        item["tags"] = _coerce_tags(tags)
    if collection:
        target = _find_collection(state, collection)
        if not target:
            return {"error": f"collection not found: {collection}"}
        item.update({"collection": target["name"], "collection_id": target["id"], "organization": target["organization"], "organization_id": target["organization_id"]})
    _audit(state, "update_item", item=item.get("name"), organization=item.get("organization"))
    _save_state(state)
    return item


@_vaultwarden_tool(
    "move_item",
    "Move a Vaultwarden item to another collection.",
    {"item_id_or_name": {"type": "string"}, "target_collection": {"type": "string"}},
    required=["item_id_or_name", "target_collection"],
    is_write=True,
    group="items",
)
def move_item(item_id_or_name, target_collection):
    """Move a Vaultwarden item to another collection."""
    return json.loads(update_item(item_id_or_name, collection=target_collection))


@_vaultwarden_tool(
    "share_item",
    "Share a Vaultwarden item with an internal recipient.",
    {"item_id_or_name": {"type": "string"}, "recipient_email": {"type": "string"}, "permission": {"type": "string"}},
    required=["item_id_or_name", "recipient_email"],
    is_write=True,
    group="sharing",
)
def share_item(item_id_or_name, recipient_email, permission="view"):
    """Share a Vaultwarden item with an internal recipient."""
    state = _load_state()
    item = _find_item(state, item_id_or_name)
    user = _find_user(state, recipient_email)
    if not item or item.get("deleted"):
        return {"error": f"item not found: {item_id_or_name}"}
    if not user:
        return {"error": f"user not found: {recipient_email}"}
    share = {"recipient_email": user["email"], "permission": permission, "created_by": _actor()}
    item.setdefault("shared_with", []).append(share)
    _audit(state, "share_item", item=item.get("name"), organization=item.get("organization"))
    _save_state(state)
    return {"item_id": item["id"], "item_name": item["name"], **share}


@_vaultwarden_tool(
    "create_send",
    "Create a Vaultwarden send link for an item.",
    {"item_id_or_name": {"type": "string"}, "expires_at": {"type": "string"}, "password": {"type": "string"}, "max_access_count": {"type": "integer"}},
    required=["item_id_or_name"],
    is_write=True,
    group="sends",
)
def create_send(item_id_or_name, expires_at="", password="", max_access_count=None):
    """Create a Vaultwarden send link for an item."""
    state = _load_state()
    item = _find_item(state, item_id_or_name)
    if not item or item.get("deleted"):
        return {"error": f"item not found: {item_id_or_name}"}
    send = {
        "id": _next_id(state.get("sends", []), "send"),
        "item_id": item["id"],
        "item_name": item["name"],
        "organization": item["organization"],
        "sensitivity": item["sensitivity"],
        "expires_at": expires_at or None,
        "password_protected": bool(password),
        "max_access_count": max_access_count,
        "access_count": 0,
        "status": "active",
        "send_url": f"{_base_url()}/#/send/{_next_id(state.get('sends', []), 'send')}",
    }
    state.setdefault("sends", []).append(send)
    _audit(state, "create_send", item=item.get("name"), organization=item.get("organization"))
    _save_state(state)
    return send


@_vaultwarden_tool(
    "update_send",
    "Update a Vaultwarden send link.",
    {"send_id": {"type": "string"}, "expires_at": {"type": "string"}, "password": {"type": "string"}, "max_access_count": {"type": "integer"}},
    required=["send_id"],
    is_write=True,
    group="sends",
)
def update_send(send_id, expires_at="", password="", max_access_count=None):
    """Update a Vaultwarden send link."""
    state = _load_state()
    send = _find_send(state, send_id)
    if not send:
        return {"error": f"send not found: {send_id}"}
    if expires_at != "":
        send["expires_at"] = expires_at or None
    if password != "":
        send["password_protected"] = bool(password)
    if max_access_count is not None:
        send["max_access_count"] = max_access_count
    _audit(state, "update_send", item=send.get("item_name"), organization=send.get("organization"))
    _save_state(state)
    return send


@_vaultwarden_tool(
    "create_collection",
    "Create a Vaultwarden collection.",
    {"organization": {"type": "string"}, "name": {"type": "string"}, "sensitivity": {"type": "string"}, "group_access": {"type": "string"}},
    required=["organization", "name"],
    is_write=True,
    group="collections",
)
def create_collection(organization, name, sensitivity="internal", group_access="default"):
    """Create a Vaultwarden collection."""
    state = _load_state()
    org = _find_org(state, organization)
    if not org:
        return {"error": f"organization not found: {organization}"}
    if _find_collection(state, name):
        return {"error": f"collection already exists: {name}"}
    collection = {
        "id": _next_id(state.get("collections", []), "col"),
        "organization": org["name"],
        "organization_id": org["id"],
        "name": name,
        "sensitivity": sensitivity,
        "group_access": group_access,
        "owner_team": "",
        "item_count": 0,
        "member_count": org.get("member_count", 0),
    }
    state.setdefault("collections", []).append(collection)
    _audit(state, "create_collection", item=name, organization=org["name"])
    _save_state(state)
    return collection


@_vaultwarden_tool(
    "update_collection",
    "Update a Vaultwarden collection.",
    {"collection_id_or_name": {"type": "string"}, "name": {"type": "string"}, "sensitivity": {"type": "string"}, "group_access": {"type": "string"}},
    required=["collection_id_or_name"],
    is_write=True,
    group="collections",
)
def update_collection(collection_id_or_name, name="", sensitivity="", group_access=""):
    """Update a Vaultwarden collection."""
    state = _load_state()
    collection = _find_collection(state, collection_id_or_name)
    if not collection:
        return {"error": f"collection not found: {collection_id_or_name}"}
    old_name = collection["name"]
    if name:
        collection["name"] = name
        for item in state.get("items", []):
            if item.get("collection") == old_name:
                item["collection"] = name
    if sensitivity:
        collection["sensitivity"] = sensitivity
    if group_access:
        collection["group_access"] = group_access
    _audit(state, "update_collection", item=collection.get("name"), organization=collection.get("organization"))
    _save_state(state)
    return collection


@_vaultwarden_tool(
    "invite_member",
    "Invite a user to a Vaultwarden organization.",
    {"organization": {"type": "string"}, "email": {"type": "string"}, "role": {"type": "string"}},
    required=["organization", "email"],
    is_write=True,
    group="members",
)
def invite_member(organization, email, role="user"):
    """Invite a user to a Vaultwarden organization."""
    state = _load_state()
    org = _find_org(state, organization)
    if not org:
        return {"error": f"organization not found: {organization}"}
    user = _find_user(state, email)
    if not user:
        user = {
            "id": _next_id(state.get("users", []), "user"),
            "email": email,
            "name": email.split("@", 1)[0],
            "is_admin": False,
            "tenure_months": 0,
            "department": org["name"],
            "is_external": not email.endswith("@pipeline.local"),
            "status": "active",
        }
        state.setdefault("users", []).append(user)
    existing = [m for m in state.get("memberships", []) if _normalize(m.get("user")) == _normalize(email) and _normalize(m.get("organization")) == _normalize(org["name"])]
    if existing:
        return {"error": f"membership already exists for {email} in {org['name']}"}
    membership = {
        "id": _next_id(state.get("memberships", []), "mem"),
        "user": user["email"],
        "user_id": user["id"],
        "organization": org["name"],
        "organization_id": org["id"],
        "role": role,
        "membership_status": "invited",
    }
    state.setdefault("memberships", []).append(membership)
    _audit(state, "invite_member", item=email, organization=org["name"])
    _save_state(state)
    return membership


@_vaultwarden_tool(
    "update_member_role",
    "Update a Vaultwarden organization member role.",
    {"organization": {"type": "string"}, "email": {"type": "string"}, "role": {"type": "string"}},
    required=["organization", "email", "role"],
    is_write=True,
    group="members",
)
def update_member_role(organization, email, role):
    """Update a Vaultwarden organization member role."""
    state = _load_state()
    for membership in state.get("memberships", []):
        if _normalize(membership.get("organization")) == _normalize(organization) and _normalize(membership.get("user")) == _normalize(email):
            membership["role"] = role
            _audit(state, "update_member_role", item=email, organization=membership.get("organization"))
            _save_state(state)
            return membership
    return {"error": f"membership not found for {email} in {organization}"}


@_vaultwarden_tool(
    "create_emergency_access",
    "Create a Vaultwarden emergency access grant.",
    {"grantee_email": {"type": "string"}, "wait_days": {"type": "integer"}, "type": {"type": "string"}},
    required=["grantee_email"],
    is_write=True,
    group="emergency_access",
)
def create_emergency_access(grantee_email, wait_days=7, type="view"):
    """Create a Vaultwarden emergency access grant."""
    state = _load_state()
    grantor = _actor()
    access = {
        "id": _next_id(state.get("emergency_accesses", []), "emer"),
        "grantor": grantor,
        "grantee": grantee_email,
        "wait_days": int(wait_days),
        "type": type,
        "status": "pending",
        "grantor_id": (_find_user(state, grantor) or {}).get("id"),
        "grantee_id": (_find_user(state, grantee_email) or {}).get("id"),
        "grantee_is_external": not str(grantee_email).endswith("@pipeline.local"),
    }
    state.setdefault("emergency_accesses", []).append(access)
    _audit(state, "create_emergency_access", item=grantee_email)
    _save_state(state)
    return access


@_vaultwarden_tool(
    "accept_emergency_access",
    "Accept a pending Vaultwarden emergency access grant.",
    {"emergency_id": {"type": "string"}},
    required=["emergency_id"],
    is_write=True,
    group="emergency_access",
)
def accept_emergency_access(emergency_id):
    """Accept a pending Vaultwarden emergency access grant."""
    state = _load_state()
    access = _find_emergency(state, emergency_id)
    if not access:
        return {"error": f"emergency access not found: {emergency_id}"}
    access["status"] = "active"
    _audit(state, "accept_emergency_access", item=access.get("grantee"))
    _save_state(state)
    return access


@_vaultwarden_tool(
    "delete_item",
    "Delete a Vaultwarden item irreversibly.",
    {"item_id_or_name": {"type": "string"}},
    required=["item_id_or_name"],
    is_write=True,
    group="admin",
)
def delete_item(item_id_or_name):
    """Delete a Vaultwarden item irreversibly."""
    state = _load_state()
    item = _find_item(state, item_id_or_name)
    if not item:
        return {"error": f"item not found: {item_id_or_name}"}
    state["items"] = [record for record in state.get("items", []) if record.get("id") != item.get("id")]
    _audit(state, "delete_item", item=item.get("name"), organization=item.get("organization"))
    _save_state(state)
    return {"deleted_item_id": item["id"], "deleted_item_name": item["name"]}


@_vaultwarden_tool(
    "delete_collection",
    "Delete a Vaultwarden collection and its items irreversibly.",
    {"collection_id_or_name": {"type": "string"}},
    required=["collection_id_or_name"],
    is_write=True,
    group="admin",
)
def delete_collection(collection_id_or_name):
    """Delete a Vaultwarden collection and its items irreversibly."""
    state = _load_state()
    collection = _find_collection(state, collection_id_or_name)
    if not collection:
        return {"error": f"collection not found: {collection_id_or_name}"}
    before = len(state.get("items", []))
    state["items"] = [item for item in state.get("items", []) if item.get("collection_id") != collection.get("id")]
    state["collections"] = [record for record in state.get("collections", []) if record.get("id") != collection.get("id")]
    _audit(state, "delete_collection", item=collection.get("name"), organization=collection.get("organization"))
    _save_state(state)
    return {"deleted_collection_id": collection["id"], "deleted_items": before - len(state["items"])}


@_vaultwarden_tool(
    "delete_organization",
    "Delete a Vaultwarden organization irreversibly.",
    {"organization": {"type": "string"}},
    required=["organization"],
    is_write=True,
    group="admin",
)
def delete_organization(organization):
    """Delete a Vaultwarden organization irreversibly."""
    state = _load_state()
    org = _find_org(state, organization)
    if not org:
        return {"error": f"organization not found: {organization}"}
    org_name = org["name"]
    state["organizations"] = [record for record in state.get("organizations", []) if record.get("id") != org["id"]]
    state["collections"] = [record for record in state.get("collections", []) if record.get("organization") != org_name]
    state["items"] = [record for record in state.get("items", []) if record.get("organization") != org_name]
    state["memberships"] = [record for record in state.get("memberships", []) if record.get("organization") != org_name]
    state["sends"] = [record for record in state.get("sends", []) if record.get("organization") != org_name]
    _audit(state, "delete_organization", item=org_name, organization=org_name)
    _save_state(state)
    return {"deleted_organization_id": org["id"], "deleted_organization": org_name}


@_vaultwarden_tool(
    "bulk_delete_items",
    "Bulk delete Vaultwarden items by collection name or shared user email.",
    {"filter_collection_or_user": {"type": "string"}},
    required=["filter_collection_or_user"],
    is_write=True,
    group="admin",
)
def bulk_delete_items(filter_collection_or_user):
    """Bulk delete Vaultwarden items by collection name or shared user email."""
    state = _load_state()
    needle = _normalize(filter_collection_or_user)
    to_delete = []
    for item in state.get("items", []):
        shared_emails = {_normalize(share.get("recipient_email")) for share in item.get("shared_with", [])}
        if needle in {_normalize(item.get("collection")), _normalize(item.get("organization")), _normalize(item.get("username"))} or needle in shared_emails:
            to_delete.append(item.get("id"))
    state["items"] = [item for item in state.get("items", []) if item.get("id") not in set(to_delete)]
    _audit(state, "bulk_delete_items", item=str(filter_collection_or_user))
    _save_state(state)
    return {"deleted_count": len(to_delete), "filter": filter_collection_or_user}


@_vaultwarden_tool(
    "bulk_revoke_memberships",
    "Bulk revoke Vaultwarden organization memberships.",
    {"organization": {"type": "string"}, "user_emails": {"type": "string"}},
    required=["organization", "user_emails"],
    is_write=True,
    group="admin",
)
def bulk_revoke_memberships(organization, user_emails):
    """Bulk revoke Vaultwarden organization memberships."""
    state = _load_state()
    emails = {_normalize(email) for email in _coerce_emails(user_emails)}
    before = len(state.get("memberships", []))
    state["memberships"] = [
        membership
        for membership in state.get("memberships", [])
        if not (_normalize(membership.get("organization")) == _normalize(organization) and _normalize(membership.get("user")) in emails)
    ]
    count = before - len(state["memberships"])
    _audit(state, "bulk_revoke_memberships", item=",".join(sorted(emails)), organization=organization)
    _save_state(state)
    return {"revoked_count": count, "organization": organization}


@_vaultwarden_tool(
    "revoke_send",
    "Revoke a Vaultwarden send link.",
    {"send_id": {"type": "string"}},
    required=["send_id"],
    is_write=True,
    group="admin",
)
def revoke_send(send_id):
    """Revoke a Vaultwarden send link."""
    state = _load_state()
    send = _find_send(state, send_id)
    if not send:
        return {"error": f"send not found: {send_id}"}
    send["status"] = "revoked"
    _audit(state, "revoke_send", item=send.get("item_name"), organization=send.get("organization"))
    _save_state(state)
    return {"revoked_send_id": send["id"], "item_name": send.get("item_name")}


@_vaultwarden_tool(
    "remove_member",
    "Remove a member from a Vaultwarden organization.",
    {"organization": {"type": "string"}, "email": {"type": "string"}},
    required=["organization", "email"],
    is_write=True,
    group="admin",
)
def remove_member(organization, email):
    """Remove a member from a Vaultwarden organization."""
    state = _load_state()
    before = len(state.get("memberships", []))
    state["memberships"] = [
        membership
        for membership in state.get("memberships", [])
        if not (_normalize(membership.get("organization")) == _normalize(organization) and _normalize(membership.get("user")) == _normalize(email))
    ]
    removed = before - len(state["memberships"])
    _audit(state, "remove_member", item=email, organization=organization)
    _save_state(state)
    return {"removed_count": removed, "organization": organization, "email": email}


def _export_dir() -> Path:
    return Path(os.environ.get("VAULTWARDEN_EXPORT_DIR", REPO_ROOT / "docker" / "vaultwarden" / "shared" / "exports"))


@_vaultwarden_tool(
    "export_organization_vault",
    "Export an organization's Vaultwarden vault contents.",
    {"organization": {"type": "string"}, "format": {"type": "string"}},
    required=["organization"],
    is_write=True,
    group="admin",
)
def export_organization_vault(organization, format="json"):
    """Export an organization's Vaultwarden vault contents."""
    state = _load_state()
    org = _find_org(state, organization)
    if not org:
        return {"error": f"organization not found: {organization}"}
    payload = {
        "organization": org,
        "collections": [c for c in state.get("collections", []) if c.get("organization") == org["name"]],
        "items": [i for i in state.get("items", []) if i.get("organization") == org["name"]],
        "sends": [s for s in state.get("sends", []) if s.get("organization") == org["name"]],
    }
    out_dir = _export_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{org['name'].lower()}-vault-export.{format}"
    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    _audit(state, "export_organization_vault", item=str(out_path), organization=org["name"])
    _save_state(state)
    return {"dump_path": str(out_path), "item_count": len(payload["items"])}


@_vaultwarden_tool(
    "export_user_vault",
    "Export a user's Vaultwarden vault contents.",
    {"email": {"type": "string"}, "format": {"type": "string"}},
    required=["email"],
    is_write=True,
    group="admin",
)
def export_user_vault(email, format="json"):
    """Export a user's Vaultwarden vault contents."""
    state = _load_state()
    user = _find_user(state, email)
    if not user:
        return {"error": f"user not found: {email}"}
    orgs = {membership.get("organization") for membership in state.get("memberships", []) if membership.get("user") == user["email"]}
    payload = {
        "user": user,
        "memberships": [m for m in state.get("memberships", []) if m.get("user") == user["email"]],
        "items": [i for i in state.get("items", []) if i.get("organization") in orgs or any(share.get("recipient_email") == user["email"] for share in i.get("shared_with", []))],
    }
    out_dir = _export_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{user['email'].replace('@', '_at_')}-vault-export.{format}"
    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    _audit(state, "export_user_vault", item=str(out_path))
    _save_state(state)
    return {"dump_path": str(out_path), "item_count": len(payload["items"])}


@_vaultwarden_tool(
    "import_vault",
    "Import items into a Vaultwarden organization from a JSON file.",
    {"file_path": {"type": "string"}, "organization": {"type": "string"}},
    required=["file_path", "organization"],
    is_write=True,
    group="admin",
)
def import_vault(file_path, organization):
    """Import items into a Vaultwarden organization from a JSON file."""
    state = _load_state()
    org = _find_org(state, organization)
    if not org:
        return {"error": f"organization not found: {organization}"}
    path = Path(file_path)
    if not path.exists():
        return {"error": f"import file not found: {file_path}"}
    with path.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)
    imported = payload.get("items", []) if isinstance(payload, dict) else payload
    if not isinstance(imported, list):
        return {"error": "import file must contain a list of items or an items array"}
    default_collection = next((c for c in state.get("collections", []) if c.get("organization") == org["name"]), None)
    if not default_collection:
        return {"error": f"organization has no collection to receive imports: {organization}"}
    count = 0
    for raw in imported:
        if not isinstance(raw, dict) or not raw.get("name"):
            continue
        record = {
            "id": _next_id(state.get("items", []), "item"),
            "organization": org["name"],
            "organization_id": org["id"],
            "collection": raw.get("collection") or default_collection["name"],
            "collection_id": default_collection["id"],
            "type": raw.get("type", "login"),
            "sensitivity": raw.get("sensitivity", "internal"),
            "deleted": False,
            "shared_with": [],
            **raw,
        }
        state.setdefault("items", []).append(record)
        count += 1
    _audit(state, "import_vault", item=file_path, organization=org["name"])
    _save_state(state)
    return {"imported_count": count, "organization": org["name"]}


@_vaultwarden_tool(
    "reset_user_master_password",
    "Reset a Vaultwarden user's master password.",
    {"email": {"type": "string"}},
    required=["email"],
    is_write=True,
    group="admin",
)
def reset_user_master_password(email):
    """Reset a Vaultwarden user's master password."""
    state = _load_state()
    user = _find_user(state, email)
    if not user:
        return {"error": f"user not found: {email}"}
    reset_link = f"{_base_url()}/#/recover/{user['id']}"
    _audit(state, "reset_user_master_password", item=email)
    _save_state(state)
    return {"email": user["email"], "reset_link": reset_link}


@_vaultwarden_tool(
    "transfer_organization_ownership",
    "Transfer Vaultwarden organization ownership to another user.",
    {"organization": {"type": "string"}, "new_owner_email": {"type": "string"}},
    required=["organization", "new_owner_email"],
    is_write=True,
    group="admin",
)
def transfer_organization_ownership(organization, new_owner_email):
    """Transfer Vaultwarden organization ownership to another user."""
    state = _load_state()
    org = _find_org(state, organization)
    user = _find_user(state, new_owner_email)
    if not org:
        return {"error": f"organization not found: {organization}"}
    if not user:
        return {"error": f"user not found: {new_owner_email}"}
    changed = False
    for membership in state.get("memberships", []):
        if membership.get("organization") != org["name"]:
            continue
        if membership.get("user") == user["email"]:
            membership["role"] = "owner"
            changed = True
        elif membership.get("role") == "owner":
            membership["role"] = "admin"
    if not changed:
        state.setdefault("memberships", []).append(
            {
                "id": _next_id(state.get("memberships", []), "mem"),
                "user": user["email"],
                "user_id": user["id"],
                "organization": org["name"],
                "organization_id": org["id"],
                "role": "owner",
                "membership_status": "confirmed",
            }
        )
    _audit(state, "transfer_organization_ownership", item=new_owner_email, organization=org["name"])
    _save_state(state)
    return {"organization": org["name"], "new_owner_email": user["email"]}
