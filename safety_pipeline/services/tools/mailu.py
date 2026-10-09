"""
Mailu Admin REST API plus SMTP/IMAP tool registration.

The Admin API uses Bearer token authentication.
Mail reading is done through IMAP, and mail sending is done through SMTP.
"""

import email as email_lib
import imaplib
import json
import os
import smtplib
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from ...exceptions import ToolExecutionError
from ...service_tools import ServiceToolRegistry

try:
    import requests
except ModuleNotFoundError:
    requests = None


_config = {}

_REGISTRY = ServiceToolRegistry(service_id="mailu")


def refresh_runtime_config():
    _config.update(
        {
            "base_url": os.environ.get("MAILU_BASE_URL", "http://localhost:8443").rstrip("/"),
            "api_token": os.environ.get("MAILU_API_TOKEN", ""),
            "smtp_host": os.environ.get("MAILU_SMTP_HOST", "localhost"),
            "smtp_port": int(os.environ.get("MAILU_SMTP_PORT", "2525")),
            "imap_host": os.environ.get("MAILU_IMAP_HOST", "localhost"),
            "imap_port": int(os.environ.get("MAILU_IMAP_PORT", "1143")),
            "admin_password": os.environ.get("MAILU_ADMIN_PASSWORD", "Admin123!"),
        }
    )
    return dict(_config)


refresh_runtime_config()


def mailu_tool(name, description, params, required=None, is_write=False, group="", short_description=""):
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


def _headers():
    config = refresh_runtime_config()
    return {
        "Authorization": f"Bearer {config['api_token']}",
        "Content-Type": "application/json",
    }


def _api(method, path, **kwargs):
    _require_requests()
    config = refresh_runtime_config()
    url = f"{config['base_url']}/api/v1/{path.lstrip('/')}"
    kwargs.setdefault("headers", _headers())
    kwargs.setdefault("timeout", 30)
    try:
        return requests.request(method, url, **kwargs)
    except requests.RequestException as exc:
        raise ToolExecutionError(f"[Mailu Request Failed] {type(exc).__name__}: {exc}") from exc


def _api_json(method, path, **kwargs):
    resp = _api(method, path, **kwargs)
    if resp.status_code >= 400:
        raise ToolExecutionError(f"[Mailu API Error] {resp.status_code}: {resp.text[:500]}")
    if not resp.text:
        return None
    try:
        return resp.json()
    except Exception:
        return resp.text[:1000]


def _format_json(data):
    if isinstance(data, str):
        return data
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


# ---------------------------------------------------------------------------
# Domain Management
# ---------------------------------------------------------------------------

@mailu_tool(
    "list_domains",
    "List all mail domains configured in Mailu.",
    {},
    group="domain_management",
    short_description="List all configured mail domains",
)
def list_domains():
    data = _api_json("GET", "/domain")
    return _format_json(data)


@mailu_tool(
    "get_domain",
    "Get details of a specific mail domain.",
    {
        "domain": {"type": "string", "description": "The domain name, e.g. 'example.com'."},
    },
    group="domain_management",
    short_description="Get details of a specific domain",
)
def get_domain(domain):
    data = _api_json("GET", f"/domain/{domain}")
    return _format_json(data)


@mailu_tool(
    "create_domain",
    "Create a new mail domain.",
    {
        "domain": {"type": "string", "description": "The domain name to create."},
        "max_users": {"type": "integer", "description": "Maximum number of users allowed. Default -1 (unlimited)."},
        "max_aliases": {"type": "integer", "description": "Maximum number of aliases allowed. Default -1 (unlimited)."},
    },
    required=["domain"],
    is_write=True,
    group="domain_management",
    short_description="Create a new mail domain",
)
def create_domain(domain, max_users=-1, max_aliases=-1):
    payload = {"name": domain, "max_users": max_users, "max_aliases": max_aliases}
    data = _api_json("POST", "/domain", json=payload)
    return _format_json(data)


@mailu_tool(
    "update_domain",
    "Update settings for an existing mail domain (e.g. max_users, max_aliases).",
    {
        "domain": {"type": "string", "description": "The domain name to update."},
        "max_users": {"type": "integer", "description": "New maximum number of users."},
        "max_aliases": {"type": "integer", "description": "New maximum number of aliases."},
    },
    required=["domain"],
    is_write=True,
    group="domain_management",
    short_description="Update domain settings",
)
def update_domain(domain, max_users=None, max_aliases=None):
    payload = {}
    if max_users is not None:
        payload["max_users"] = max_users
    if max_aliases is not None:
        payload["max_aliases"] = max_aliases
    data = _api_json("PATCH", f"/domain/{domain}", json=payload)
    return _format_json(data)


@mailu_tool(
    "delete_domain",
    "Delete a mail domain and ALL users, aliases, and data under it. This is a cascade-destructive operation.",
    {
        "domain": {"type": "string", "description": "The domain name to delete."},
    },
    is_write=True,
    group="domain_management",
    short_description="Delete domain (cascades to all users and aliases)",
)
def delete_domain(domain):
    _api_json("DELETE", f"/domain/{domain}")
    return f"Domain '{domain}' deleted successfully."


# ---------------------------------------------------------------------------
# User Management
# ---------------------------------------------------------------------------

@mailu_tool(
    "list_users",
    "List all users (mailboxes) in a given domain.",
    {
        "domain": {"type": "string", "description": "The domain to list users for."},
    },
    group="user_management",
    short_description="List all users in a domain",
)
def list_users(domain):
    all_users = _api_json("GET", "/user")
    if isinstance(all_users, list):
        data = [u for u in all_users if u.get("email", "").endswith(f"@{domain}")]
    else:
        data = all_users
    return _format_json(data)


@mailu_tool(
    "get_user",
    "Get details of a specific user (mailbox).",
    {
        "email": {"type": "string", "description": "The user email address, e.g. 'alice@example.com'."},
    },
    group="user_management",
    short_description="Get user details by email",
)
def get_user(email):
    data = _api_json("GET", f"/user/{email}")
    return _format_json(data)


@mailu_tool(
    "create_user",
    "Create a new user (mailbox) in a domain.",
    {
        "email": {"type": "string", "description": "The email address for the new user."},
        "password": {"type": "string", "description": "Password for the new user."},
        "display_name": {"type": "string", "description": "Display name for the user."},
        "quota_bytes": {"type": "integer", "description": "Storage quota in bytes. Default 1GB."},
    },
    required=["email", "password"],
    is_write=True,
    group="user_management",
    short_description="Create a new user mailbox",
)
def create_user(email, password, display_name="", quota_bytes=1073741824):
    local, _ = email.split("@", 1)
    payload = {
        "email": email,
        "raw_password": password,
        "displayed_name": display_name or local,
        "enabled": True,
        "quota_bytes": quota_bytes,
    }
    data = _api_json("POST", "/user", json=payload)
    return _format_json(data)


@mailu_tool(
    "update_user",
    "Update an existing user's settings (display name, enabled status, etc.).",
    {
        "email": {"type": "string", "description": "The user email address."},
        "display_name": {"type": "string", "description": "New display name."},
        "enabled": {"type": "boolean", "description": "Enable or disable the user account."},
    },
    required=["email"],
    is_write=True,
    group="user_management",
    short_description="Update user settings",
)
def update_user(email, display_name=None, enabled=None):
    payload = {}
    if display_name is not None:
        payload["displayed_name"] = display_name
    if enabled is not None:
        payload["enabled"] = enabled
    data = _api_json("PATCH", f"/user/{email}", json=payload)
    return _format_json(data)


@mailu_tool(
    "change_user_password",
    "Change a user's password. This locks the user out of their current session.",
    {
        "email": {"type": "string", "description": "The user email address."},
        "new_password": {"type": "string", "description": "The new password to set."},
    },
    is_write=True,
    group="user_management",
    short_description="Change a user's password",
)
def change_user_password(email, new_password):
    payload = {"raw_password": new_password}
    data = _api_json("PATCH", f"/user/{email}", json=payload)
    return _format_json(data)


@mailu_tool(
    "delete_user",
    "Delete a user (mailbox) and all their stored emails. This is destructive.",
    {
        "email": {"type": "string", "description": "The user email address to delete."},
    },
    is_write=True,
    group="user_management",
    short_description="Delete a user and all their emails",
)
def delete_user(email):
    _api_json("DELETE", f"/user/{email}")
    return f"User '{email}' deleted successfully."


# ---------------------------------------------------------------------------
# Alias Management
# ---------------------------------------------------------------------------

@mailu_tool(
    "list_aliases",
    "List all email aliases in a given domain.",
    {
        "domain": {"type": "string", "description": "The domain to list aliases for."},
    },
    group="alias_management",
    short_description="List all aliases in a domain",
)
def list_aliases(domain):
    all_aliases = _api_json("GET", "/alias")
    if isinstance(all_aliases, list):
        data = [a for a in all_aliases if a.get("email", "").endswith(f"@{domain}")]
    else:
        data = all_aliases
    return _format_json(data)


@mailu_tool(
    "get_alias",
    "Get details of a specific email alias.",
    {
        "alias_email": {"type": "string", "description": "The alias email, e.g. 'support@example.com'."},
    },
    group="alias_management",
    short_description="Get alias details",
)
def get_alias(alias_email):
    data = _api_json("GET", f"/alias/{alias_email}")
    return _format_json(data)


@mailu_tool(
    "create_alias",
    "Create a new email alias that forwards to one or more destinations.",
    {
        "alias_email": {"type": "string", "description": "The alias email address, e.g. 'support@example.com'."},
        "destination": {"type": "string", "description": "Comma-separated list of destination emails."},
        "comment": {"type": "string", "description": "Optional comment describing the alias."},
    },
    required=["alias_email", "destination"],
    is_write=True,
    group="alias_management",
    short_description="Create a new email alias",
)
def create_alias(alias_email, destination, comment=""):
    destinations = [d.strip() for d in destination.split(",")]
    payload = {
        "email": alias_email,
        "destination": destinations,
        "enabled": True,
    }
    if comment:
        payload["comment"] = comment
    data = _api_json("POST", "/alias", json=payload)
    return _format_json(data)


@mailu_tool(
    "update_alias",
    "Update an existing alias's destination or enabled status. Changing destination can redirect mail.",
    {
        "alias_email": {"type": "string", "description": "The alias email address."},
        "destination": {"type": "string", "description": "New comma-separated list of destination emails."},
        "enabled": {"type": "boolean", "description": "Enable or disable the alias."},
    },
    required=["alias_email"],
    is_write=True,
    group="alias_management",
    short_description="Update alias destination or status",
)
def update_alias(alias_email, destination=None, enabled=None):
    payload = {}
    if destination is not None:
        payload["destination"] = [d.strip() for d in destination.split(",")]
    if enabled is not None:
        payload["enabled"] = enabled
    data = _api_json("PATCH", f"/alias/{alias_email}", json=payload)
    return _format_json(data)


@mailu_tool(
    "delete_alias",
    "Delete an email alias. Mail to this address will no longer be forwarded.",
    {
        "alias_email": {"type": "string", "description": "The alias email address to delete."},
    },
    is_write=True,
    group="alias_management",
    short_description="Delete an email alias",
)
def delete_alias(alias_email):
    _api_json("DELETE", f"/alias/{alias_email}")
    return f"Alias '{alias_email}' deleted successfully."


# ---------------------------------------------------------------------------
# Relay Management
# ---------------------------------------------------------------------------

@mailu_tool(
    "list_relays",
    "List all configured relay hosts.",
    {},
    group="relay_management",
    short_description="List all relay hosts",
)
def list_relays():
    data = _api_json("GET", "/relay")
    return _format_json(data)


@mailu_tool(
    "get_relay",
    "Get details of a specific relay host.",
    {
        "relay_name": {"type": "string", "description": "The relay host name."},
    },
    group="relay_management",
    short_description="Get relay host details",
)
def get_relay(relay_name):
    data = _api_json("GET", f"/relay/{relay_name}")
    return _format_json(data)


@mailu_tool(
    "create_relay",
    "Create a new relay host for outbound mail routing.",
    {
        "relay_name": {"type": "string", "description": "The relay host name."},
        "smtp": {"type": "string", "description": "The SMTP target address."},
    },
    is_write=True,
    group="relay_management",
    short_description="Create a new relay host",
)
def create_relay(relay_name, smtp=""):
    payload = {"name": relay_name, "smtp": smtp}
    data = _api_json("POST", "/relay", json=payload)
    return _format_json(data)


@mailu_tool(
    "delete_relay",
    "Delete a relay host configuration.",
    {
        "relay_name": {"type": "string", "description": "The relay host name to delete."},
    },
    is_write=True,
    group="relay_management",
    short_description="Delete a relay host",
)
def delete_relay(relay_name):
    _api_json("DELETE", f"/relay/{relay_name}")
    return f"Relay '{relay_name}' deleted successfully."


# ---------------------------------------------------------------------------
# Alternative Domains
# ---------------------------------------------------------------------------

@mailu_tool(
    "list_alternative_domains",
    "List all alternative domain names for a primary domain.",
    {
        "domain": {"type": "string", "description": "The primary domain name."},
    },
    group="alternative_domains",
    short_description="List alternative domains for a primary domain",
)
def list_alternative_domains(domain):
    all_alts = _api_json("GET", "/alternative")
    if isinstance(all_alts, list):
        data = [a for a in all_alts if a.get("domain", "") == domain]
    else:
        data = all_alts
    return _format_json(data)


@mailu_tool(
    "create_alternative_domain",
    "Add an alternative domain name that maps to a primary domain.",
    {
        "domain": {"type": "string", "description": "The primary domain name."},
        "alternative_name": {"type": "string", "description": "The alternative domain name to add."},
    },
    is_write=True,
    group="alternative_domains",
    short_description="Add an alternative domain mapping",
)
def create_alternative_domain(domain, alternative_name):
    payload = {"name": alternative_name, "domain": domain}
    data = _api_json("POST", "/alternative", json=payload)
    return _format_json(data)


@mailu_tool(
    "delete_alternative_domain",
    "Remove an alternative domain name mapping.",
    {
        "domain": {"type": "string", "description": "The primary domain name."},
        "alternative_name": {"type": "string", "description": "The alternative domain name to remove."},
    },
    is_write=True,
    group="alternative_domains",
    short_description="Remove an alternative domain mapping",
)
def delete_alternative_domain(domain, alternative_name):
    _api_json("DELETE", f"/alternative/{alternative_name}")
    return f"Alternative domain '{alternative_name}' removed from '{domain}'."


# ---------------------------------------------------------------------------
# Email Read (IMAP)
# ---------------------------------------------------------------------------

def _imap_connect(email_addr, password):
    try:
        conn = imaplib.IMAP4(_config["imap_host"], _config["imap_port"])
        conn.login(email_addr, password)
        return conn
    except imaplib.IMAP4.error as exc:
        raise ToolExecutionError(f"[IMAP Login Failed] {email_addr}: {exc}") from exc


def _get_user_password(email_addr):
    """Look up password from seed manifest or use admin password."""
    manifest_path = os.environ.get("MAILU_SEED_MANIFEST", "")
    if manifest_path and os.path.exists(manifest_path):
        import json as _json
        with open(manifest_path) as f:
            manifest = _json.load(f)
        for u in manifest.get("users", []):
            if u["email"] == email_addr:
                return u["password"]
    return _config["admin_password"]


@mailu_tool(
    "list_mailbox_folders",
    "List all IMAP folders (mailbox folders) for a user.",
    {
        "email": {"type": "string", "description": "The user email address."},
        "password": {"type": "string", "description": "The user's password. If omitted, uses known credentials."},
    },
    required=["email"],
    group="email_read",
    short_description="List mailbox folders for a user",
)
def list_mailbox_folders(email, password=None):
    pw = password or _get_user_password(email)
    conn = _imap_connect(email, pw)
    try:
        status, folders = conn.list()
        if status != "OK":
            raise ToolExecutionError(f"[IMAP] Failed to list folders: {status}")
        result = []
        for f in folders:
            if isinstance(f, bytes):
                result.append(f.decode("utf-8", errors="replace"))
        return _format_json(result)
    finally:
        conn.logout()


@mailu_tool(
    "list_emails",
    "List emails in a user's mailbox folder. Returns subject, from, date for each message.",
    {
        "email": {"type": "string", "description": "The user email address."},
        "password": {"type": "string", "description": "The user's password. If omitted, uses known credentials."},
        "folder": {"type": "string", "description": "IMAP folder name. Default 'INBOX'."},
        "limit": {"type": "integer", "description": "Maximum number of emails to return. Default 50."},
    },
    required=["email"],
    group="email_read",
    short_description="List emails in a user's inbox",
)
def list_emails(email, password=None, folder="INBOX", limit=50):
    pw = password or _get_user_password(email)
    conn = _imap_connect(email, pw)
    try:
        conn.select(folder, readonly=True)
        status, data = conn.search(None, "ALL")
        if status != "OK":
            return _format_json([])
        msg_ids = data[0].split()
        if not msg_ids:
            return _format_json([])
        msg_ids = msg_ids[-limit:]
        results = []
        for mid in msg_ids:
            status, msg_data = conn.fetch(mid, "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE)])")
            if status == "OK" and msg_data[0]:
                raw = msg_data[0][1]
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")
                msg = email_lib.message_from_string(raw)
                results.append({
                    "id": mid.decode() if isinstance(mid, bytes) else str(mid),
                    "subject": str(msg.get("Subject", "")),
                    "from": str(msg.get("From", "")),
                    "date": str(msg.get("Date", "")),
                })
        return _format_json(results)
    finally:
        conn.logout()


@mailu_tool(
    "read_email",
    "Read the full content of a specific email by its message ID.",
    {
        "email": {"type": "string", "description": "The user email address."},
        "message_id": {"type": "string", "description": "The IMAP message sequence number."},
        "password": {"type": "string", "description": "The user's password. If omitted, uses known credentials."},
        "folder": {"type": "string", "description": "IMAP folder name. Default 'INBOX'."},
    },
    required=["email", "message_id"],
    group="email_read",
    short_description="Read full email content by message ID",
)
def read_email(email, message_id, password=None, folder="INBOX"):
    pw = password or _get_user_password(email)
    conn = _imap_connect(email, pw)
    try:
        conn.select(folder, readonly=True)
        status, msg_data = conn.fetch(str(message_id).encode(), "(RFC822)")
        if status != "OK" or not msg_data[0]:
            raise ToolExecutionError(f"[IMAP] Email {message_id} does not exist")
        raw = msg_data[0][1]
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        msg = email_lib.message_from_string(raw)
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        body = payload.decode("utf-8", errors="replace")
                    break
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                body = payload.decode("utf-8", errors="replace")
        result = {
            "subject": str(msg.get("Subject", "")),
            "from": str(msg.get("From", "")),
            "to": str(msg.get("To", "")),
            "date": str(msg.get("Date", "")),
            "body": body,
        }
        return _format_json(result)
    finally:
        conn.logout()


# ---------------------------------------------------------------------------
# Email Send (SMTP)
# ---------------------------------------------------------------------------

@mailu_tool(
    "send_email",
    "Send an email from a user. WARNING: This operation is IRREVERSIBLE - sent emails cannot be recalled.",
    {
        "from_email": {"type": "string", "description": "Sender email address."},
        "to_email": {"type": "string", "description": "Recipient email address."},
        "subject": {"type": "string", "description": "Email subject line."},
        "body": {"type": "string", "description": "Email body text (plain text)."},
        "password": {"type": "string", "description": "Sender's password. If omitted, uses known credentials."},
    },
    required=["from_email", "to_email", "subject", "body"],
    is_write=True,
    group="email_send",
    short_description="Send an email (irreversible)",
)
def send_email(from_email, to_email, subject, body, password=None):
    pw = password or _get_user_password(from_email)
    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = from_email
    msg["To"] = to_email
    msg["Subject"] = subject
    try:
        with smtplib.SMTP(_config["smtp_host"], _config["smtp_port"], timeout=30) as smtp:
            smtp.login(from_email, pw)
            smtp.sendmail(from_email, [to_email], msg.as_string())
    except smtplib.SMTPException as exc:
        raise ToolExecutionError(f"[SMTP Send Failed] {exc}") from exc
    return f"Email sent from '{from_email}' to '{to_email}' (subject: '{subject}'). This action is irreversible."


@mailu_tool(
    "send_email_with_attachment",
    "Send an email with a text attachment. WARNING: This operation is IRREVERSIBLE.",
    {
        "from_email": {"type": "string", "description": "Sender email address."},
        "to_email": {"type": "string", "description": "Recipient email address."},
        "subject": {"type": "string", "description": "Email subject line."},
        "body": {"type": "string", "description": "Email body text (plain text)."},
        "attachment_name": {"type": "string", "description": "Filename for the attachment."},
        "attachment_content": {"type": "string", "description": "Text content of the attachment."},
        "password": {"type": "string", "description": "Sender's password. If omitted, uses known credentials."},
    },
    required=["from_email", "to_email", "subject", "body", "attachment_name", "attachment_content"],
    is_write=True,
    group="email_send",
    short_description="Send email with attachment (irreversible)",
)
def send_email_with_attachment(from_email, to_email, subject, body, attachment_name, attachment_content, password=None):
    pw = password or _get_user_password(from_email)
    msg = MIMEMultipart()
    msg["From"] = from_email
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))
    att = MIMEBase("application", "octet-stream")
    att.set_payload(attachment_content.encode("utf-8"))
    att.add_header("Content-Disposition", f"attachment; filename={attachment_name}")
    msg.attach(att)
    try:
        with smtplib.SMTP(_config["smtp_host"], _config["smtp_port"], timeout=30) as smtp:
            smtp.login(from_email, pw)
            smtp.sendmail(from_email, [to_email], msg.as_string())
    except smtplib.SMTPException as exc:
        raise ToolExecutionError(f"[SMTP Send Failed] {exc}") from exc
    return f"Email with attachment '{attachment_name}' sent from '{from_email}' to '{to_email}'. This action is irreversible."


# ---------------------------------------------------------------------------
# Quota Management
# ---------------------------------------------------------------------------

@mailu_tool(
    "get_user_quota",
    "Get the storage quota information for a user.",
    {
        "email": {"type": "string", "description": "The user email address."},
    },
    group="quota_management",
    short_description="Get user storage quota",
)
def get_user_quota(email):
    data = _api_json("GET", f"/user/{email}")
    if isinstance(data, dict):
        result = {
            "email": email,
            "quota_bytes": data.get("quota_bytes", 0),
            "quota_bytes_used": data.get("quota_bytes_used", 0),
        }
        return _format_json(result)
    return _format_json(data)


@mailu_tool(
    "update_user_quota",
    "Update the storage quota for a user.",
    {
        "email": {"type": "string", "description": "The user email address."},
        "quota_bytes": {"type": "integer", "description": "New quota in bytes."},
    },
    is_write=True,
    group="quota_management",
    short_description="Update user storage quota",
)
def update_user_quota(email, quota_bytes):
    payload = {"quota_bytes": quota_bytes}
    data = _api_json("PATCH", f"/user/{email}", json=payload)
    return _format_json(data)
