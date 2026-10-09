"""
Zammad REST API tool registration.

Calls the official Zammad API with Basic Auth.
"""

import json
import os

from ...exceptions import ToolExecutionError
from ...service_tools import ServiceToolRegistry

try:
    import requests
    from requests.auth import HTTPBasicAuth
except ModuleNotFoundError:
    requests = None
    HTTPBasicAuth = None


_config = {
    "base_url": os.environ.get("ZAMMAD_BASE_URL", "http://localhost:8081").rstrip("/"),
    "admin_user": os.environ.get("ZAMMAD_ADMIN_USER", "admin@example.com"),
    "admin_password": os.environ.get("ZAMMAD_ADMIN_PASSWORD", "Admin123!"),
}

_REGISTRY = ServiceToolRegistry(service_id="zammad")


def zammad_tool(name, description, params, required=None, is_write=False, group="", short_description=""):
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
        raise ToolExecutionError("requests is not installed. Run: pip install requests")


def _auth():
    return HTTPBasicAuth(_config["admin_user"], _config["admin_password"])


def _api(method, path, **kwargs):
    _require_requests()
    url = f"{_config['base_url']}/api/v1/{path.lstrip('/')}"
    kwargs.setdefault("auth", _auth())
    kwargs.setdefault("timeout", 30)
    kwargs.setdefault("headers", {})
    headers = kwargs["headers"]
    if "Content-Type" not in headers and method.upper() in {"POST", "PUT", "PATCH"}:
        headers["Content-Type"] = "application/json"
    try:
        return requests.request(method, url, **kwargs)
    except requests.RequestException as exc:
        raise ToolExecutionError(f"[Zammad Request Failed] {type(exc).__name__}: {exc}") from exc


def _api_json(method, path, **kwargs):
    resp = _api(method, path, **kwargs)
    if resp.status_code >= 400:
        raise ToolExecutionError(f"[Zammad API Error] {resp.status_code}: {resp.text[:500]}")
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


def _all_roles():
    return [role for role in (_api_json("GET", "roles") or []) if isinstance(role, dict)]


def _roles_by_id():
    return {role["id"]: role for role in _all_roles() if role.get("id") is not None}


def _all_users():
    return [user for user in (_api_json("GET", "users") or []) if isinstance(user, dict)]


def _users_by_id():
    return {user["id"]: user for user in _all_users() if user.get("id") is not None}


def _all_groups():
    return [group for group in (_api_json("GET", "groups") or []) if isinstance(group, dict)]


def _groups_by_id():
    return {group["id"]: group for group in _all_groups() if group.get("id") is not None}


def _all_states():
    return [state for state in (_api_json("GET", "ticket_states") or []) if isinstance(state, dict)]


def _states_by_id():
    return {state["id"]: state for state in _all_states() if state.get("id") is not None}


def _all_priorities():
    return [priority for priority in (_api_json("GET", "ticket_priorities") or []) if isinstance(priority, dict)]


def _priorities_by_id():
    return {
        priority["id"]: priority
        for priority in _all_priorities()
        if priority.get("id") is not None
    }


def _all_tickets():
    return [ticket for ticket in (_api_json("GET", "tickets") or []) if isinstance(ticket, dict)]


def _ticket_articles(ticket_id):
    return [
        article
        for article in (_api_json("GET", f"ticket_articles/by_ticket/{ticket_id}") or [])
        if isinstance(article, dict)
    ]


def _full_name(user):
    parts = [str(user.get("firstname", "")).strip(), str(user.get("lastname", "")).strip()]
    return " ".join(part for part in parts if part).strip()


def _role_names_for_user(user, roles_by_id=None):
    roles_by_id = roles_by_id or _roles_by_id()
    names = []
    for role_id in user.get("role_ids", []) or []:
        role = roles_by_id.get(role_id) or {}
        name = str(role.get("name", "")).strip()
        if name:
            names.append(name)
    return names


def _is_customer(user, roles_by_id=None):
    return "Customer" in _role_names_for_user(user, roles_by_id=roles_by_id)


def _is_agent(user, roles_by_id=None):
    role_names = set(_role_names_for_user(user, roles_by_id=roles_by_id))
    return bool(role_names.intersection({"Admin", "Agent"}))


def _user_summary(user, roles_by_id=None):
    return {
        "id": user.get("id"),
        "email": user.get("email", ""),
        "firstname": user.get("firstname", ""),
        "lastname": user.get("lastname", ""),
        "full_name": _full_name(user),
        "phone": user.get("phone", ""),
        "organization_id": user.get("organization_id"),
        "active": bool(user.get("active", True)),
        "roles": _role_names_for_user(user, roles_by_id=roles_by_id),
        "note": user.get("note", ""),
    }


def _group_summary(group):
    return {
        "id": group.get("id"),
        "name": group.get("name", ""),
        "assignment_timeout": group.get("assignment_timeout"),
        "follow_up_possible": group.get("follow_up_possible"),
        "active": bool(group.get("active", True)),
        "note": group.get("note", ""),
    }


def _priority_name(ticket, priorities_by_id=None):
    priorities_by_id = priorities_by_id or _priorities_by_id()
    priority = priorities_by_id.get(ticket.get("priority_id")) or {}
    return priority.get("name", ticket.get("priority", ""))


def _state_name(ticket, states_by_id=None):
    states_by_id = states_by_id or _states_by_id()
    state = states_by_id.get(ticket.get("state_id")) or {}
    return state.get("name", ticket.get("state", ""))


def _ticket_tags(ticket):
    ticket_id = ticket.get("id")
    if not ticket_id:
        return []
    payload = _api_json("GET", "tags", params={"object": "Ticket", "o_id": ticket_id}) or {}
    tags = payload.get("tags") if isinstance(payload, dict) else payload
    return [str(tag).strip() for tag in (tags or []) if str(tag).strip()]


def _ticket_summary(ticket, users_by_id=None, groups_by_id=None, states_by_id=None, priorities_by_id=None):
    users_by_id = users_by_id or _users_by_id()
    groups_by_id = groups_by_id or _groups_by_id()
    states_by_id = states_by_id or _states_by_id()
    priorities_by_id = priorities_by_id or _priorities_by_id()

    customer = users_by_id.get(ticket.get("customer_id")) or {}
    owner = users_by_id.get(ticket.get("owner_id")) or {}
    group = groups_by_id.get(ticket.get("group_id")) or {}

    return {
        "id": ticket.get("id"),
        "number": ticket.get("number", ""),
        "title": ticket.get("title", ""),
        "state": _state_name(ticket, states_by_id=states_by_id),
        "priority": _priority_name(ticket, priorities_by_id=priorities_by_id),
        "group": group.get("name", ""),
        "customer_email": customer.get("email", ""),
        "owner_email": owner.get("email", ""),
        "article_count": ticket.get("article_count", 0),
        "tags": _ticket_tags(ticket),
        "created_at": ticket.get("created_at", ""),
        "updated_at": ticket.get("updated_at", ""),
    }


def _find_user_by_email(email, *, only_customers=False, only_agents=False):
    target = str(email or "").strip().lower()
    if not target:
        return None
    roles_by_id = _roles_by_id()
    for user in _all_users():
        if str(user.get("email", "")).strip().lower() != target:
            continue
        if only_customers and not _is_customer(user, roles_by_id=roles_by_id):
            continue
        if only_agents and not _is_agent(user, roles_by_id=roles_by_id):
            continue
        return user
    return None


def _find_group(group_name):
    target = str(group_name or "").strip().lower()
    if not target:
        return None
    for group in _all_groups():
        if str(group.get("name", "")).strip().lower() == target:
            return group
    return None


def _find_ticket_by_title(title):
    target = str(title or "").strip()
    if not target:
        return None
    for ticket in _all_tickets():
        if str(ticket.get("title", "")).strip() == target:
            return ticket
    return None


def _search_ticket_assets(payload):
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    assets = payload.get("assets") or {}
    tickets = assets.get("Ticket") or []
    if isinstance(tickets, dict):
        return [item for item in tickets.values() if isinstance(item, dict)]
    return [item for item in tickets if isinstance(item, dict)]


def _update_ticket_payload(ticket_id, *, title="", group="", priority="", owner_email="", state=""):
    payload = {}
    if title:
        payload["title"] = title
    if group:
        payload["group"] = group
    if priority:
        payload["priority"] = priority
    if state:
        payload["state"] = state
    if owner_email:
        owner = _find_user_by_email(owner_email, only_agents=True)
        if not owner:
            raise ToolExecutionError(f"[Error] Assignable agent not found: {owner_email}")
        payload["owner_id"] = owner["id"]
    if not payload:
        raise ToolExecutionError("[Error] At least one field to update must be provided")
    data = _api_json("PUT", f"tickets/{ticket_id}", json=payload)
    return data, payload


def _create_ticket_article(ticket_id, body, *, internal, subject, sender="Agent"):
    data = _api_json(
        "POST",
        "ticket_articles",
        json={
            "ticket_id": ticket_id,
            "subject": subject,
            "body": body,
            "content_type": "text/plain",
            "type": "note",
            "internal": bool(internal),
            "sender": sender,
        },
    )
    return {
        "id": data.get("id"),
        "ticket_id": data.get("ticket_id"),
        "subject": data.get("subject", ""),
        "internal": data.get("internal", False),
    }


@zammad_tool(
    "list_customers",
    "List customers in the support system, optionally filtered by name or email keyword.",
    {
        "query": {"type": "string", "description": "Customer name or email keyword"},
    },
    group="customers",
    short_description="List customer profiles with optional name or email filtering.",
)
def list_customers(query=""):
    roles_by_id = _roles_by_id()
    needle = str(query or "").strip().lower()
    customers = []
    for user in _all_users():
        if not _is_customer(user, roles_by_id=roles_by_id):
            continue
        haystack = " ".join(
            [
                str(user.get("email", "")).lower(),
                str(user.get("firstname", "")).lower(),
                str(user.get("lastname", "")).lower(),
                _full_name(user).lower(),
            ]
        )
        if needle and needle not in haystack:
            continue
        customers.append(_user_summary(user, roles_by_id=roles_by_id))
    return _format_json(customers)


@zammad_tool(
    "get_customer",
    "Get detailed information for a single customer.",
    {
        "customer_id": {"type": "integer", "description": "Customer ID"},
    },
    group="customers",
    short_description="Read a single customer profile by numeric customer id.",
)
def get_customer(customer_id):
    user = _api_json("GET", f"users/{customer_id}")
    roles_by_id = _roles_by_id()
    if not _is_customer(user, roles_by_id=roles_by_id):
        raise ToolExecutionError(f"[Error] User {customer_id} is not a Customer")
    return _format_json(_user_summary(user, roles_by_id=roles_by_id))


@zammad_tool(
    "list_customer_tickets",
    "List tickets for a specific customer, optionally filtered by state.",
    {
        "customer_email": {"type": "string", "description": "Customer email address"},
        "state": {"type": "string", "description": "Ticket state, such as open or closed"},
    },
    required=["customer_email"],
    group="customers",
    short_description="List all tickets for one customer email, optionally filtered by state.",
)
def list_customer_tickets(customer_email, state=""):
    return list_tickets(customer_email=customer_email, state=state)


@zammad_tool(
    "list_agents",
    "List support Agent/Admin accounts, optionally filtered by name or email keyword.",
    {
        "query": {"type": "string", "description": "Agent name or email keyword"},
    },
    group="agents_and_groups",
    short_description="List agent and admin accounts available for ticket ownership.",
)
def list_agents(query=""):
    roles_by_id = _roles_by_id()
    needle = str(query or "").strip().lower()
    results = []
    for user in _all_users():
        if not _is_agent(user, roles_by_id=roles_by_id):
            continue
        haystack = " ".join(
            [
                str(user.get("email", "")).lower(),
                str(user.get("firstname", "")).lower(),
                str(user.get("lastname", "")).lower(),
                _full_name(user).lower(),
            ]
        )
        if needle and needle not in haystack:
            continue
        results.append(_user_summary(user, roles_by_id=roles_by_id))
    return _format_json(results)


@zammad_tool(
    "get_agent",
    "Get details for one Agent/Admin account by email address.",
    {
        "email": {"type": "string", "description": "Agent/Admin email address"},
    },
    required=["email"],
    group="agents_and_groups",
    short_description="Read one agent or admin profile by email address.",
)
def get_agent(email):
    user = _find_user_by_email(email, only_agents=True)
    if not user:
        raise ToolExecutionError(f"[Error] Agent/Admin not found: {email}")
    return _format_json(_user_summary(user, roles_by_id=_roles_by_id()))


@zammad_tool(
    "list_groups",
    "List the current support groups.",
    {},
    group="agents_and_groups",
    short_description="List available support groups such as Billing or Support.",
)
def list_groups():
    return _format_json([_group_summary(group) for group in _all_groups()])


@zammad_tool(
    "get_group",
    "Read details for a single support group.",
    {
        "group_name": {"type": "string", "description": "Group name, such as Billing or Support"},
    },
    required=["group_name"],
    group="agents_and_groups",
    short_description="Read one support group by exact group name.",
)
def get_group(group_name):
    group = _find_group(group_name)
    if not group:
        raise ToolExecutionError(f"[Error] Group not found: {group_name}")
    return _format_json(_group_summary(group))


@zammad_tool(
    "list_ticket_states",
    "List the ticket states available in the system.",
    {},
    group="ticket_queries",
    short_description="List valid ticket states available for workflow transitions.",
)
def list_ticket_states():
    return _format_json(
        [
            {
                "id": state.get("id"),
                "name": state.get("name", ""),
                "next_state_id": state.get("next_state_id"),
                "default_create": bool(state.get("default_create", False)),
                "default_follow_up": bool(state.get("default_follow_up", False)),
            }
            for state in _all_states()
        ]
    )


@zammad_tool(
    "list_ticket_priorities",
    "List the ticket priorities available in the system.",
    {},
    group="ticket_queries",
    short_description="List valid ticket priorities such as low, normal, and high.",
)
def list_ticket_priorities():
    return _format_json(
        [
            {
                "id": priority.get("id"),
                "name": priority.get("name", ""),
                "default_create": bool(priority.get("default_create", False)),
            }
            for priority in _all_priorities()
        ]
    )


@zammad_tool(
    "list_tickets",
    "List tickets, optionally filtered by state, group, customer, owner, priority, or tag.",
    {
        "state": {"type": "string", "description": "Ticket state, such as open or closed"},
        "group": {"type": "string", "description": "Ticket group, such as Billing or Support"},
        "customer_email": {"type": "string", "description": "Customer email address"},
        "owner_email": {"type": "string", "description": "Owner email address"},
        "priority": {"type": "string", "description": "Priority, such as 1 low, 2 normal, or 3 high"},
        "tag": {"type": "string", "description": "Tag name"},
    },
    group="ticket_queries",
    short_description="List tickets with optional filters for workflow, ownership, and tags.",
)
def list_tickets(state="", group="", customer_email="", owner_email="", priority="", tag=""):
    users_by_id = _users_by_id()
    groups_by_id = _groups_by_id()
    states_by_id = _states_by_id()
    priorities_by_id = _priorities_by_id()
    expected_tag = str(tag or "").strip().lower()

    results = []
    for ticket in _all_tickets():
        summary = _ticket_summary(
            ticket,
            users_by_id=users_by_id,
            groups_by_id=groups_by_id,
            states_by_id=states_by_id,
            priorities_by_id=priorities_by_id,
        )
        if state and str(summary["state"]).lower() != str(state).lower():
            continue
        if group and str(summary["group"]).lower() != str(group).lower():
            continue
        if customer_email and str(summary["customer_email"]).lower() != str(customer_email).lower():
            continue
        if owner_email and str(summary["owner_email"]).lower() != str(owner_email).lower():
            continue
        if priority and str(summary["priority"]).lower() != str(priority).lower():
            continue
        if expected_tag and expected_tag not in [item.lower() for item in summary["tags"]]:
            continue
        results.append(summary)
    return _format_json(results)


@zammad_tool(
    "list_group_tickets",
    "List tickets in a specific group, optionally filtered by state.",
    {
        "group": {"type": "string", "description": "Group name, such as Billing or Support"},
        "state": {"type": "string", "description": "Ticket state, such as open or closed"},
    },
    required=["group"],
    group="ticket_queries",
    short_description="List tickets inside one support group with optional state filtering.",
)
def list_group_tickets(group, state=""):
    return list_tickets(group=group, state=state)


@zammad_tool(
    "list_agent_tickets",
    "List tickets currently assigned to one Agent, optionally filtered by state.",
    {
        "owner_email": {"type": "string", "description": "Owner email address"},
        "state": {"type": "string", "description": "Ticket state, such as open or closed"},
    },
    required=["owner_email"],
    group="ticket_queries",
    short_description="List tickets assigned to one agent email, optionally filtered by state.",
)
def list_agent_tickets(owner_email, state=""):
    return list_tickets(owner_email=owner_email, state=state)


@zammad_tool(
    "get_ticket",
    "Get details for a single ticket, including customer, owner, tags, and articles.",
    {
        "ticket_id": {"type": "integer", "description": "Ticket ID"},
    },
    group="ticket_queries",
    short_description="Read one ticket with customer, owner, tags, and article history.",
)
def get_ticket(ticket_id):
    ticket = _api_json("GET", f"tickets/{ticket_id}")
    users_by_id = _users_by_id()
    groups_by_id = _groups_by_id()
    states_by_id = _states_by_id()
    priorities_by_id = _priorities_by_id()
    articles = _ticket_articles(ticket_id)
    customer = users_by_id.get(ticket.get("customer_id")) or {}
    owner = users_by_id.get(ticket.get("owner_id")) or {}

    return _format_json(
        {
            **_ticket_summary(
                ticket,
                users_by_id=users_by_id,
                groups_by_id=groups_by_id,
                states_by_id=states_by_id,
                priorities_by_id=priorities_by_id,
            ),
            "customer": _user_summary(customer, roles_by_id=_roles_by_id()) if customer else {},
            "owner": _user_summary(owner, roles_by_id=_roles_by_id()) if owner else {},
            "articles": [
                {
                    "id": article.get("id"),
                    "subject": article.get("subject", ""),
                    "body": article.get("body", ""),
                    "internal": bool(article.get("internal", False)),
                    "sender": article.get("sender", ""),
                    "type": article.get("type", ""),
                    "created_at": article.get("created_at", ""),
                }
                for article in articles
            ],
        }
    )


@zammad_tool(
    "search_tickets",
    "Run a full-text search for tickets by keyword.",
    {
        "query": {"type": "string", "description": "Search keyword"},
        "limit": {"type": "integer", "description": "Maximum number of results to return. Default: 20"},
    },
    required=["query"],
    group="ticket_queries",
    short_description="Full-text search tickets by keyword and return normalized summaries.",
)
def search_tickets(query, limit=20):
    payload = _api_json("GET", "tickets/search", params={"query": query, "limit": limit, "expand": "true"})
    users_by_id = _users_by_id()
    groups_by_id = _groups_by_id()
    states_by_id = _states_by_id()
    priorities_by_id = _priorities_by_id()

    results = []
    for ticket in _search_ticket_assets(payload)[: int(limit)]:
        results.append(
            _ticket_summary(
                ticket,
                users_by_id=users_by_id,
                groups_by_id=groups_by_id,
                states_by_id=states_by_id,
                priorities_by_id=priorities_by_id,
            )
        )
    return _format_json(results)


@zammad_tool(
    "list_ticket_articles",
    "Get all articles and replies for a specific ticket.",
    {
        "ticket_id": {"type": "integer", "description": "Ticket ID"},
        "include_internal": {"type": "boolean", "description": "Whether to include internal articles. Default: true"},
    },
    required=["ticket_id"],
    group="ticket_articles",
    short_description="List all ticket articles and optionally filter out internal notes.",
)
def list_ticket_articles(ticket_id, include_internal=True):
    results = []
    for article in _ticket_articles(ticket_id):
        if not include_internal and bool(article.get("internal", False)):
            continue
        results.append(
            {
                "id": article.get("id"),
                "ticket_id": article.get("ticket_id"),
                "subject": article.get("subject", ""),
                "body": article.get("body", ""),
                "internal": bool(article.get("internal", False)),
                "sender": article.get("sender", ""),
                "type": article.get("type", ""),
                "created_at": article.get("created_at", ""),
            }
        )
    return _format_json(results)


@zammad_tool(
    "list_ticket_tags",
    "List the current tags attached to a ticket.",
    {
        "ticket_id": {"type": "integer", "description": "Ticket ID"},
    },
    required=["ticket_id"],
    group="tagging",
    short_description="Inspect the current tag list attached to one ticket.",
)
def list_ticket_tags(ticket_id):
    ticket = _api_json("GET", f"tickets/{ticket_id}")
    return _format_json(
        {
            "ticket_id": ticket.get("id"),
            "title": ticket.get("title", ""),
            "tags": _ticket_tags(ticket),
        }
    )


@zammad_tool(
    "create_ticket",
    "Create a new ticket.",
    {
        "title": {"type": "string", "description": "Ticket title"},
        "group": {"type": "string", "description": "Ticket group, such as Billing or Support"},
        "customer_email": {"type": "string", "description": "Customer email address"},
        "body": {"type": "string", "description": "Initial ticket content"},
        "priority": {"type": "string", "description": "Priority, such as 1 low, 2 normal, or 3 high"},
    },
    required=["title", "group", "body", "customer_email"],
    is_write=True,
    group="ticket_creation",
    short_description="Create a new support ticket for an existing customer email.",
)
def create_ticket(title, group, body, customer_email="", priority="2 normal"):
    payload = {
        "title": title,
        "group": group,
        "customer": customer_email,
        "priority": priority,
        "article": {
            "subject": title,
            "body": body,
            "type": "note",
            "internal": False,
        },
    }
    data = _api_json("POST", "tickets", json=payload)
    return _format_json(
        {
            "id": data.get("id"),
            "number": data.get("number", ""),
            "title": data.get("title", ""),
            "group": group,
            "customer_email": customer_email,
            "priority": priority,
        }
    )


@zammad_tool(
    "update_ticket",
    "Update ticket fields such as title, state, priority, group, or owner.",
    {
        "ticket_id": {"type": "integer", "description": "Ticket ID"},
        "title": {"type": "string", "description": "New title"},
        "group": {"type": "string", "description": "New group"},
        "priority": {"type": "string", "description": "New priority"},
        "owner_email": {"type": "string", "description": "New owner email address"},
        "state": {"type": "string", "description": "New state, such as open or closed"},
    },
    required=["ticket_id"],
    is_write=True,
    group="ticket_updates",
    short_description="Generic ticket metadata update for title, state, priority, group, or owner.",
)
def update_ticket(ticket_id, title="", group="", priority="", owner_email="", state=""):
    data, payload = _update_ticket_payload(
        ticket_id,
        title=title,
        group=group,
        priority=priority,
        owner_email=owner_email,
        state=state,
    )
    return _format_json({"ticket_id": data.get("id"), "updated_fields": sorted(payload.keys())})


@zammad_tool(
    "rename_ticket",
    "Update a ticket title.",
    {
        "ticket_id": {"type": "integer", "description": "Ticket ID"},
        "title": {"type": "string", "description": "New title"},
    },
    required=["ticket_id", "title"],
    is_write=True,
    group="ticket_updates",
    short_description="Rename one ticket without changing workflow or ownership.",
)
def rename_ticket(ticket_id, title):
    data, _ = _update_ticket_payload(ticket_id, title=title)
    return _format_json({"ticket_id": data.get("id"), "title": title})


@zammad_tool(
    "update_ticket_state",
    "Update a ticket state.",
    {
        "ticket_id": {"type": "integer", "description": "Ticket ID"},
        "state": {"type": "string", "description": "New state, such as open, pending reminder, or closed"},
    },
    required=["ticket_id", "state"],
    is_write=True,
    group="ticket_updates",
    short_description="Change the workflow state of one ticket.",
)
def update_ticket_state(ticket_id, state):
    data, _ = _update_ticket_payload(ticket_id, state=state)
    return _format_json({"ticket_id": data.get("id"), "state": state})


@zammad_tool(
    "update_ticket_priority",
    "Update a ticket priority.",
    {
        "ticket_id": {"type": "integer", "description": "Ticket ID"},
        "priority": {"type": "string", "description": "New priority, such as 1 low, 2 normal, or 3 high"},
    },
    required=["ticket_id", "priority"],
    is_write=True,
    group="ticket_updates",
    short_description="Change the priority level of one ticket.",
)
def update_ticket_priority(ticket_id, priority):
    data, _ = _update_ticket_payload(ticket_id, priority=priority)
    return _format_json({"ticket_id": data.get("id"), "priority": priority})


@zammad_tool(
    "move_ticket_to_group",
    "Move a ticket to a different group.",
    {
        "ticket_id": {"type": "integer", "description": "Ticket ID"},
        "group": {"type": "string", "description": "Target group name"},
    },
    required=["ticket_id", "group"],
    is_write=True,
    group="ticket_assignment",
    short_description="Move one ticket into a different support group.",
)
def move_ticket_to_group(ticket_id, group):
    data, _ = _update_ticket_payload(ticket_id, group=group)
    return _format_json({"ticket_id": data.get("id"), "group": group})


@zammad_tool(
    "reassign_ticket_owner",
    "Assign a ticket to a specific Agent.",
    {
        "ticket_id": {"type": "integer", "description": "Ticket ID"},
        "owner_email": {"type": "string", "description": "Target owner email address"},
    },
    required=["ticket_id", "owner_email"],
    is_write=True,
    group="ticket_assignment",
    short_description="Assign one ticket to a specific agent email.",
)
def reassign_ticket_owner(ticket_id, owner_email):
    data, _ = _update_ticket_payload(ticket_id, owner_email=owner_email)
    return _format_json({"ticket_id": data.get("id"), "owner_email": owner_email})


@zammad_tool(
    "add_ticket_note",
    "Append an internal note to a ticket.",
    {
        "ticket_id": {"type": "integer", "description": "Ticket ID"},
        "body": {"type": "string", "description": "Note content"},
        "internal": {"type": "boolean", "description": "Whether this is an internal note"},
        "subject": {"type": "string", "description": "Note subject"},
    },
    required=["ticket_id", "body"],
    is_write=True,
    group="ticket_articles",
    short_description="Append an internal note to a ticket conversation.",
)
def add_ticket_note(ticket_id, body, internal=True, subject="Internal note"):
    return _format_json(
        _create_ticket_article(
            ticket_id,
            body,
            internal=internal,
            subject=subject,
            sender="Agent",
        )
    )


@zammad_tool(
    "add_public_ticket_reply",
    "Append a public reply visible to the customer.",
    {
        "ticket_id": {"type": "integer", "description": "Ticket ID"},
        "body": {"type": "string", "description": "Reply content"},
        "subject": {"type": "string", "description": "Reply subject"},
    },
    required=["ticket_id", "body"],
    is_write=True,
    group="ticket_articles",
    short_description="Post a public agent reply into a ticket thread.",
)
def add_public_ticket_reply(ticket_id, body, subject="Agent reply"):
    return _format_json(
        _create_ticket_article(
            ticket_id,
            body,
            internal=False,
            subject=subject,
            sender="Agent",
        )
    )


@zammad_tool(
    "add_ticket_tag",
    "Add a tag to a ticket.",
    {
        "ticket_id": {"type": "integer", "description": "Ticket ID"},
        "tag": {"type": "string", "description": "Tag name"},
    },
    required=["ticket_id", "tag"],
    is_write=True,
    group="tagging",
    short_description="Attach one tag string to a ticket.",
)
def add_ticket_tag(ticket_id, tag):
    _api_json(
        "POST",
        "tags/add",
        json={
            "object": "Ticket",
            "o_id": ticket_id,
            "item": tag,
        },
    )
    return _format_json({"ticket_id": ticket_id, "tag": tag})


@zammad_tool(
    "remove_ticket_tag",
    "Remove a tag from a ticket.",
    {
        "ticket_id": {"type": "integer", "description": "Ticket ID"},
        "tag": {"type": "string", "description": "Tag name"},
    },
    required=["ticket_id", "tag"],
    is_write=True,
    group="tagging",
    short_description="Remove one tag string from a ticket.",
)
def remove_ticket_tag(ticket_id, tag):
    _api_json(
        "DELETE",
        "tags/remove",
        json={
            "object": "Ticket",
            "o_id": ticket_id,
            "item": tag,
        },
    )
    return _format_json({"ticket_id": ticket_id, "removed_tag": tag})


@zammad_tool(
    "delete_ticket",
    "Permanently delete a ticket.",
    {
        "ticket_id": {"type": "integer", "description": "Ticket ID"},
    },
    required=["ticket_id"],
    is_write=True,
    group="destructive_ops",
    short_description="Permanently delete a ticket and its audit trail.",
)
def delete_ticket(ticket_id):
    resp = _api("DELETE", f"tickets/{ticket_id}")
    if resp.status_code >= 400:
        raise ToolExecutionError(f"[Zammad API Error] {resp.status_code}: {resp.text[:500]}")
    return _format_json({"deleted_ticket_id": ticket_id})
