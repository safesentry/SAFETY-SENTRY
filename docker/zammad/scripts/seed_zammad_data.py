import json
import os
import subprocess
import sys
import time

try:
    import requests
    from requests.auth import HTTPBasicAuth
except ModuleNotFoundError as exc:
    raise SystemExit("requests is not installed, so Zammad seed data cannot be created.") from exc


BASE_URL = os.environ.get("ZAMMAD_BASE_URL", "http://localhost:8081").rstrip("/")
ADMIN_USER = os.environ.get("ZAMMAD_ADMIN_USER", "admin@example.com")
ADMIN_PASSWORD = os.environ.get("ZAMMAD_ADMIN_PASSWORD", "Admin123!")
MANIFEST_PATH = os.environ.get("ZAMMAD_SEED_MANIFEST")
RAILS_CONTAINER = os.environ.get("ZAMMAD_RAILSSERVER_CONTAINER", "pipeline-zammad-railsserver")


def api(method, path, **kwargs):
    url = f"{BASE_URL}/api/v1/{path.lstrip('/')}"
    kwargs.setdefault("auth", HTTPBasicAuth(ADMIN_USER, ADMIN_PASSWORD))
    kwargs.setdefault("timeout", 30)
    kwargs.setdefault("headers", {})
    headers = kwargs["headers"]
    if "Content-Type" not in headers and method.upper() in {"POST", "PUT", "PATCH"}:
        headers["Content-Type"] = "application/json"
    resp = requests.request(method, url, **kwargs)
    if resp.status_code >= 400:
        raise RuntimeError(f"{method} {path} -> {resp.status_code}: {resp.text[:800]}")
    if not resp.text:
        return None
    try:
        return resp.json()
    except Exception:
        return resp.text


def wait_for_api():
    deadline = time.time() + 240
    while time.time() < deadline:
        try:
            resp = requests.get(
                f"{BASE_URL}/api/v1/users/me",
                auth=HTTPBasicAuth(ADMIN_USER, ADMIN_PASSWORD),
                timeout=10,
            )
            if resp.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(5)
    raise RuntimeError("Timed out waiting for the Zammad API to become ready")


def load_manifest():
    if not MANIFEST_PATH:
        raise RuntimeError("ZAMMAD_SEED_MANIFEST was not provided")
    with open(MANIFEST_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def find_user_by_email(email):
    target = str(email or "").strip().lower()
    if not target:
        return None
    for user in api("GET", "users") or []:
        if not isinstance(user, dict):
            continue
        if str(user.get("email", "")).strip().lower() == target:
            return user
    return None


def find_group(name):
    target = str(name or "").strip().lower()
    if not target:
        return None
    for group in api("GET", "groups") or []:
        if not isinstance(group, dict):
            continue
        if str(group.get("name", "")).strip().lower() == target:
            return group
    return None


def find_ticket_by_title(title):
    target = str(title or "").strip()
    if not target:
        return None
    for ticket in api("GET", "tickets") or []:
        if not isinstance(ticket, dict):
            continue
        if str(ticket.get("title", "")).strip() == target:
            return ticket
    return None


def ensure_group(name):
    existing = find_group(name)
    if existing:
        return existing
    return api(
        "POST",
        "groups",
        json={
            "name": name,
            "assignment_timeout": 0,
            "follow_up_possible": "yes",
            "follow_up_assignment": True,
            "active": True,
        },
    )


def ensure_user(user_spec, roles):
    existing = find_user_by_email(user_spec["email"])
    if existing:
        return existing
    payload = {
        "firstname": user_spec["firstname"],
        "lastname": user_spec["lastname"],
        "email": user_spec["email"],
        "login": user_spec.get("login", user_spec["email"]),
        "roles": roles,
    }
    if user_spec.get("note"):
        payload["note"] = user_spec["note"]
    return api("POST", "users", json=payload)


def ensure_customer(customer):
    return ensure_user(customer, ["Customer"])


def ensure_agent(agent):
    return ensure_user(agent, agent.get("roles", ["Agent"]))


def grant_group_access(email, group_names):
    access_entries = ", ".join([f"\"{name}\" => [\"full\"]" for name in group_names])
    ruby = (
        f'user = User.find_by!(email: "{email}"); '
        f'user.group_names_access_map = {{{access_entries}}}; '
        "user.active = true; "
        "user.save!; "
        "puts user.group_names_access_map.inspect"
    )
    result = subprocess.run(
        [
            "docker",
            "exec",
            RAILS_CONTAINER,
            "bash",
            "-lc",
            f"bundle exec rails r '{ruby}'",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise RuntimeError(f"grant_group_access failed for {email}: {detail}")


def ensure_ticket_articles(ticket_id, articles):
    if not articles:
        return
    existing_articles = api("GET", f"ticket_articles/by_ticket/{ticket_id}") or []
    existing_keys = {
        (
            str(article.get("subject", "")).strip(),
            str(article.get("body", "")).strip(),
            bool(article.get("internal", False)),
        )
        for article in existing_articles
        if isinstance(article, dict)
    }
    for article in articles:
        key = (
            str(article.get("subject", "")).strip(),
            str(article.get("body", "")).strip(),
            bool(article.get("internal", False)),
        )
        if key in existing_keys:
            continue
        api(
            "POST",
            "ticket_articles",
            json={
                "ticket_id": ticket_id,
                "subject": article.get("subject", "Follow-up"),
                "body": article.get("body", ""),
                "content_type": "text/plain",
                "type": article.get("type", "note"),
                "internal": bool(article.get("internal", False)),
                "sender": article.get("sender", "Agent"),
            },
        )
        existing_keys.add(key)


def ensure_ticket_tags(ticket_id, desired_tags):
    if not desired_tags:
        return
    payload = api("GET", f"tags?object=Ticket&o_id={ticket_id}") or {}
    current_tags = {
        str(tag).strip().lower()
        for tag in ((payload.get("tags") if isinstance(payload, dict) else payload) or [])
        if str(tag).strip()
    }
    for tag in desired_tags:
        normalized = str(tag).strip().lower()
        if not normalized or normalized in current_tags:
            continue
        api(
            "POST",
            "tags/add",
            json={
                "object": "Ticket",
                "o_id": ticket_id,
                "item": tag,
            },
        )
        current_tags.add(normalized)


def ensure_ticket(ticket):
    existing = find_ticket_by_title(ticket["title"])
    if not existing:
        created = api(
            "POST",
            "tickets",
            json={
                "title": ticket["title"],
                "group": ticket["group"],
                "customer": ticket["customer_email"],
                "priority": ticket.get("priority", "2 normal"),
                "article": {
                    "subject": ticket.get("subject", ticket["title"]),
                    "body": ticket["body"],
                    "type": "note",
                    "internal": False,
                },
            },
        )
        ticket_id = created["id"]
    else:
        ticket_id = existing["id"]

    state = ticket.get("state", "open")
    update_payload = {
        "group": ticket["group"],
        "priority": ticket.get("priority", "2 normal"),
        "state": state,
        "title": ticket["title"],
    }
    if state.startswith("pending"):
        update_payload["pending_time"] = ticket.get("pending_time", "2026-06-01T09:00:00Z")
    owner_email = ticket.get("owner_email", "")
    if owner_email:
        owner = find_user_by_email(owner_email)
        if not owner:
            raise RuntimeError(f"Ticket owner not found: {owner_email}")
        update_payload["owner_id"] = owner["id"]
    api("PUT", f"tickets/{ticket_id}", json=update_payload)

    ensure_ticket_articles(ticket_id, ticket.get("articles", []))
    ensure_ticket_tags(ticket_id, ticket.get("tags", []))
    return api("GET", f"tickets/{ticket_id}")


def main():
    wait_for_api()
    manifest = load_manifest()

    expected_titles = {ticket["title"] for ticket in manifest.get("tickets", [])}
    for ticket in api("GET", "tickets") or []:
        if not isinstance(ticket, dict):
            continue
        if ticket.get("title") not in expected_titles:
            api("DELETE", f"tickets/{ticket['id']}")

    for group_name in manifest.get("groups", []):
        ensure_group(group_name)

    grant_group_access(ADMIN_USER, ["Users"] + manifest.get("groups", []))

    seeded_agents = []
    for agent in manifest.get("agents", []):
        created = ensure_agent(agent)
        grant_group_access(agent["email"], agent.get("groups", manifest.get("groups", [])))
        seeded_agents.append(created)

    seeded_customers = []
    for customer in manifest.get("customers", []):
        seeded_customers.append(ensure_customer(customer))

    seeded_tickets = []
    for ticket in manifest.get("tickets", []):
        seeded_tickets.append(ensure_ticket(ticket))

    result = {
        "seeded_groups": manifest.get("groups", []),
        "seeded_agents": [agent["email"] for agent in manifest.get("agents", [])],
        "seeded_customers": [customer["email"] for customer in manifest.get("customers", [])],
        "seeded_tickets": [
            {
                "id": ticket.get("id"),
                "title": ticket.get("title"),
                "state_id": ticket.get("state_id"),
                "owner_id": ticket.get("owner_id"),
            }
            for ticket in seeded_tickets
        ],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[seed_zammad_data] {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
