"""
Rocket.Chat REST API tool registration.

Provides read and write operations for public channels, private channels, messages, direct messages, users, and integrations through the REST API.
"""

import json
import os

from ...exceptions import ToolExecutionError
from ...service_tools import ServiceToolRegistry

try:
    import requests
except ModuleNotFoundError:
    requests = None


_config = {
    "base_url": os.environ.get("ROCKETCHAT_BASE_URL", "http://localhost:3100").rstrip("/"),
    "admin_user": os.environ.get("ROCKETCHAT_ADMIN_USER", "admin"),
    "admin_password": os.environ.get("ROCKETCHAT_ADMIN_PASSWORD", "Admin123!"),
}

_auth_cache = {"user_id": None, "token": None}

_REGISTRY = ServiceToolRegistry(service_id="rocketchat")


def rocketchat_tool(name, description, params, required=None, is_write=False, group="", short_description=""):
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
        raise ToolExecutionError("The requests package is not installed. Run: pip install requests")


def _ensure_auth():
    if _auth_cache["token"]:
        return
    _require_requests()
    resp = requests.post(
        f"{_config['base_url']}/api/v1/login",
        json={"user": _config["admin_user"], "password": _config["admin_password"]},
        timeout=30,
    )
    if resp.status_code != 200:
        raise ToolExecutionError(f"Rocket.Chat login failed: {resp.status_code} {resp.text[:300]}")
    data = resp.json().get("data", {})
    _auth_cache["user_id"] = data.get("userId", "")
    _auth_cache["token"] = data.get("authToken", "")
    if not _auth_cache["token"]:
        raise ToolExecutionError("Rocket.Chat login response did not include authToken")


def _headers():
    _ensure_auth()
    return {
        "Content-Type": "application/json",
        "X-Auth-Token": _auth_cache["token"],
        "X-User-Id": _auth_cache["user_id"],
    }


def _api(method, endpoint, **kwargs):
    _require_requests()
    url = f"{_config['base_url']}/api/v1/{endpoint.lstrip('/')}"
    try:
        return requests.request(method, url, headers=_headers(), timeout=30, **kwargs)
    except requests.RequestException as exc:
        raise ToolExecutionError(f"[Rocket.Chat Request Failed] {type(exc).__name__}: {exc}") from exc


def _api_json(method, endpoint, **kwargs):
    resp = _api(method, endpoint, **kwargs)
    if resp.status_code >= 400:
        raise ToolExecutionError(f"[Rocket.Chat API Error] {resp.status_code}: {resp.text[:500]}")
    try:
        return resp.json()
    except Exception:
        return {"success": False, "raw": resp.text[:1000]}


def _format_json(data):
    if isinstance(data, str):
        return data
    return json.dumps(data, ensure_ascii=False, indent=2)


def _normalize_room(room, room_type):
    return {
        "id": room.get("_id", ""),
        "name": room.get("name", room.get("fname", "")),
        "type": room_type,
        "topic": room.get("topic", ""),
        "description": room.get("description", ""),
        "members_count": room.get("usersCount", 0),
        "msgs_count": room.get("msgs", 0),
        "read_only": room.get("ro", False),
        "archived": room.get("_hidden", False) or room.get("archived", False),
        "default": room.get("default", False),
    }


def _normalize_user(user):
    return {
        "id": user.get("_id", ""),
        "username": user.get("username", ""),
        "name": user.get("name", ""),
        "email": (user.get("emails") or [{}])[0].get("address", ""),
        "roles": user.get("roles", []),
        "status": user.get("status", ""),
        "active": user.get("active", True),
    }


def _normalize_message(message):
    return {
        "id": message.get("_id", ""),
        "room_id": message.get("rid", ""),
        "text": message.get("msg", ""),
        "sender": message.get("u", {}).get("username", ""),
        "timestamp": message.get("ts", ""),
        "thread_id": message.get("tmid", ""),
        "show_in_room": message.get("tshow", False),
        "pinned": bool(message.get("pinned")),
        "replies": message.get("tcount", 0),
    }


def _normalize_integration(integration):
    urls = integration.get("urls") or []
    if integration.get("target_url") and not urls:
        urls = [integration.get("target_url")]
    return {
        "id": integration.get("_id", integration.get("integrationId", "")),
        "name": integration.get("name", ""),
        "type": integration.get("type", ""),
        "enabled": integration.get("enabled", True),
        "channel": integration.get("channel", integration.get("channelName", "")),
        "username": integration.get("username", ""),
        "urls": urls,
        "event": integration.get("event", ""),
    }


def _public_room_info(channel_name):
    data = _api_json("GET", "channels.info", params={"roomName": channel_name})
    channel = data.get("channel") or {}
    if not channel.get("_id"):
        raise ToolExecutionError(f"[Error] Public channel not found: {channel_name}")
    return channel


def _private_room_info(room_name):
    data = _api_json("GET", "groups.info", params={"roomName": room_name})
    group = data.get("group") or {}
    if not group.get("_id"):
        raise ToolExecutionError(f"[Error] Private channel not found: {room_name}")
    return group


def _room_info(room_name, room_kind="any"):
    if room_kind == "public":
        return _public_room_info(room_name), "public"
    if room_kind == "private":
        return _private_room_info(room_name), "private"

    try:
        return _public_room_info(room_name), "public"
    except ToolExecutionError:
        return _private_room_info(room_name), "private"


def _room_history(room_name, count=20, offset=0, room_kind="any"):
    room, resolved_kind = _room_info(room_name, room_kind=room_kind)
    endpoint = "channels.history" if resolved_kind == "public" else "groups.history"
    data = _api_json(
        "GET",
        endpoint,
        params={"roomId": room.get("_id", ""), "count": count, "offset": offset},
    )
    return room, resolved_kind, data.get("messages", [])


def _user_info(username):
    data = _api_json("GET", "users.info", params={"username": username})
    user = data.get("user") or {}
    if not user.get("_id"):
        raise ToolExecutionError(f"[Error] User not found: {username}")
    return user


def _user_id(username):
    return _user_info(username).get("_id", "")


def _message_info(message_id):
    data = _api_json("GET", "chat.getMessage", params={"msgId": message_id})
    message = data.get("message") or {}
    if not message.get("_id"):
        raise ToolExecutionError(f"[Error] Message not found: {message_id}")
    return message


def _find_integration(name="", integration_type="", integration_id=""):
    if integration_id:
        data = _api_json("GET", "integrations.get", params={"integrationId": integration_id})
        integration = data.get("integration") or {}
        if not integration.get("_id"):
            raise ToolExecutionError(f"[Error] Integration not found: {integration_id}")
        return integration

    params = {"count": 100, "offset": 0}
    if name:
        params["name"] = name
    data = _api_json("GET", "integrations.list", params=params)
    integrations = data.get("integrations", []) or []
    for integration in integrations:
        if name and integration.get("name") != name:
            continue
        if integration_type and integration.get("type") != integration_type:
            continue
        return integration
    raise ToolExecutionError(f"[Error] Integration not found: {name or integration_id}")


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------

@rocketchat_tool(
    "list_channels",
    "List public channels.",
    {
        "count": {"type": "integer", "description": "Number of results to return. Default 50."},
        "offset": {"type": "integer", "description": "Offset. Default 0."},
    },
    group="channels",
    short_description="List public channels with topic and description.",
)
def list_channels(count=50, offset=0):
    data = _api_json("GET", "channels.list", params={"count": count, "offset": offset})
    return _format_json([_normalize_room(item, "public") for item in data.get("channels", [])])


@rocketchat_tool(
    "get_channel_info",
    "Get detailed information for a public channel.",
    {
        "channel_name": {"type": "string", "description": "Public channel name without #."},
    },
    group="channels",
    short_description="Read a public channel's metadata and current settings.",
)
def get_channel_info(channel_name):
    return _format_json(_normalize_room(_public_room_info(channel_name), "public"))


@rocketchat_tool(
    "list_channel_members",
    "List members of a public channel.",
    {
        "channel_name": {"type": "string", "description": "Public channel name without #."},
        "count": {"type": "integer", "description": "Number of results to return. Default 100."},
        "offset": {"type": "integer", "description": "Offset. Default 0."},
    },
    group="channels",
    short_description="List members of a public channel.",
)
def list_channel_members(channel_name, count=100, offset=0):
    channel = _public_room_info(channel_name)
    data = _api_json(
        "GET",
        "channels.members",
        params={"roomId": channel.get("_id", ""), "count": count, "offset": offset},
    )
    return _format_json([_normalize_user(user) for user in data.get("members", [])])


@rocketchat_tool(
    "list_private_channels",
    "List private channels.",
    {
        "count": {"type": "integer", "description": "Number of results to return. Default 50."},
        "offset": {"type": "integer", "description": "Offset. Default 0."},
    },
    group="private_channels",
    short_description="List private channels visible to the admin user.",
)
def list_private_channels(count=50, offset=0):
    data = _api_json("GET", "groups.listAll", params={"count": count, "offset": offset})
    return _format_json([_normalize_room(item, "private") for item in data.get("groups", [])])


@rocketchat_tool(
    "get_private_channel_info",
    "Get detailed information for a private channel.",
    {
        "room_name": {"type": "string", "description": "Private channel name without #."},
    },
    group="private_channels",
    short_description="Read a private channel's metadata and settings.",
)
def get_private_channel_info(room_name):
    return _format_json(_normalize_room(_private_room_info(room_name), "private"))


@rocketchat_tool(
    "list_private_channel_members",
    "List members of a private channel.",
    {
        "room_name": {"type": "string", "description": "Private channel name without #."},
        "count": {"type": "integer", "description": "Number of results to return. Default 100."},
        "offset": {"type": "integer", "description": "Offset. Default 0."},
    },
    group="private_channels",
    short_description="List members of a private channel.",
)
def list_private_channel_members(room_name, count=100, offset=0):
    group = _private_room_info(room_name)
    data = _api_json(
        "GET",
        "groups.members",
        params={"roomId": group.get("_id", ""), "count": count, "offset": offset},
    )
    return _format_json([_normalize_user(user) for user in data.get("members", [])])


@rocketchat_tool(
    "list_channel_messages",
    "List messages in a public or private channel.",
    {
        "room_name": {"type": "string", "description": "Public or private channel name without #."},
        "count": {"type": "integer", "description": "Number of messages to return. Default 20."},
        "offset": {"type": "integer", "description": "Offset. Default 0."},
    },
    group="messages",
    short_description="Read recent messages from a room.",
)
def list_channel_messages(room_name, count=20, offset=0):
    _, _, messages = _room_history(room_name, count=count, offset=offset)
    return _format_json([_normalize_message(msg) for msg in messages])


@rocketchat_tool(
    "get_message",
    "Read the details of a single message.",
    {
        "message_id": {"type": "string", "description": "Message ID."},
    },
    group="messages",
    short_description="Inspect one message by ID.",
)
def get_message(message_id):
    return _format_json(_normalize_message(_message_info(message_id)))


@rocketchat_tool(
    "list_thread_messages",
    "Read replies in a thread.",
    {
        "message_id": {"type": "string", "description": "Thread root message ID."},
        "count": {"type": "integer", "description": "Number of messages to return. Default 20."},
    },
    group="messages",
    short_description="Read replies under a thread root message.",
)
def list_thread_messages(message_id, count=20):
    data = _api_json(
        "GET",
        "chat.getThreadMessages",
        params={"tmid": message_id, "count": count},
    )
    return _format_json([_normalize_message(msg) for msg in data.get("messages", [])])


@rocketchat_tool(
    "list_direct_messages",
    "List direct-message conversations visible to the current admin user.",
    {
        "count": {"type": "integer", "description": "Number of results to return. Default 50."},
        "offset": {"type": "integer", "description": "Offset. Default 0."},
    },
    group="direct_messages",
    short_description="List DM rooms visible to the current admin user.",
)
def list_direct_messages(count=50, offset=0):
    data = _api_json("GET", "dm.list", params={"count": count, "offset": offset})
    results = []
    for room in data.get("ims", []):
        results.append({
            "id": room.get("_id", ""),
            "name": room.get("name", room.get("fname", "")),
            "usernames": room.get("usernames", []),
            "msgs_count": room.get("msgs", 0),
        })
    return _format_json(results)


@rocketchat_tool(
    "list_direct_message_messages",
    "Read messages in the direct-message conversation with the specified user.",
    {
        "username": {"type": "string", "description": "The other user's username."},
        "count": {"type": "integer", "description": "Number of messages to return. Default 20."},
        "offset": {"type": "integer", "description": "Offset. Default 0."},
    },
    group="direct_messages",
    short_description="Read messages from the DM room with a given user.",
)
def list_direct_message_messages(username, count=20, offset=0):
    room = _api_json("POST", "dm.create", json={"username": username}).get("room", {})
    room_id = room.get("_id", "")
    if not room_id:
        raise ToolExecutionError(f"[Error] Could not create or locate the direct message room with {username}")
    data = _api_json(
        "GET",
        "dm.messages",
        params={"roomId": room_id, "count": count, "offset": offset},
    )
    return _format_json([_normalize_message(msg) for msg in data.get("messages", [])])


@rocketchat_tool(
    "list_users",
    "List users in the workspace.",
    {
        "count": {"type": "integer", "description": "Number of results to return. Default 50."},
        "offset": {"type": "integer", "description": "Offset. Default 0."},
    },
    group="users",
    short_description="List workspace users and their current status.",
)
def list_users(count=50, offset=0):
    data = _api_json("GET", "users.list", params={"count": count, "offset": offset})
    return _format_json([_normalize_user(user) for user in data.get("users", [])])


@rocketchat_tool(
    "get_user_info",
    "Get detailed information for a specific user.",
    {
        "username": {"type": "string", "description": "Username."},
    },
    group="users",
    short_description="Read a user's profile, roles, and status.",
)
def get_user_info(username):
    return _format_json(_normalize_user(_user_info(username)))


@rocketchat_tool(
    "list_integrations",
    "List integrations in the current workspace.",
    {
        "count": {"type": "integer", "description": "Number of results to return. Default 50."},
        "offset": {"type": "integer", "description": "Offset. Default 0."},
        "name_filter": {"type": "string", "description": "Filter by integration name (optional)."},
    },
    group="integrations",
    short_description="List incoming and outgoing integrations.",
)
def list_integrations(count=50, offset=0, name_filter=""):
    params = {"count": count, "offset": offset}
    if name_filter:
        params["name"] = name_filter
    data = _api_json("GET", "integrations.list", params=params)
    return _format_json([_normalize_integration(item) for item in data.get("integrations", [])])


@rocketchat_tool(
    "get_integration",
    "Read detailed information for a single integration.",
    {
        "name": {"type": "string", "description": "Integration name."},
        "integration_type": {"type": "string", "description": "Integration type, for example webhook-incoming or webhook-outgoing."},
    },
    group="integrations",
    short_description="Inspect one integration by name and type.",
)
def get_integration(name, integration_type=""):
    return _format_json(_normalize_integration(_find_integration(name=name, integration_type=integration_type)))


# ---------------------------------------------------------------------------
# Write tools
# ---------------------------------------------------------------------------

@rocketchat_tool(
    "send_message",
    "Send a message to a public or private channel.",
    {
        "room_name": {"type": "string", "description": "Public or private channel name without #."},
        "text": {"type": "string", "description": "Message content."},
    },
    is_write=True,
    group="messages",
    short_description="Post a message into a room.",
)
def send_message(room_name, text):
    room, _, _ = _room_history(room_name, count=1, offset=0)
    data = _api_json("POST", "chat.sendMessage", json={"message": {"rid": room.get("_id", ""), "msg": text}})
    if data.get("success"):
        return _format_json(_normalize_message(data.get("message", {})))
    raise ToolExecutionError(f"[Error] Failed to send message: {_format_json(data)}")


@rocketchat_tool(
    "send_thread_reply",
    "Reply inside an existing thread.",
    {
        "message_id": {"type": "string", "description": "Thread root message ID."},
        "text": {"type": "string", "description": "Reply content."},
        "show_in_room": {"type": "boolean", "description": "Whether to also show the reply in the main room. Default false."},
    },
    is_write=True,
    group="messages",
    short_description="Reply inside an existing thread.",
)
def send_thread_reply(message_id, text, show_in_room=False):
    root_message = _message_info(message_id)
    data = _api_json(
        "POST",
        "chat.sendMessage",
        json={
            "message": {
                "rid": root_message.get("rid", ""),
                "msg": text,
                "tmid": message_id,
                "tshow": bool(show_in_room),
            }
        },
    )
    if data.get("success"):
        return _format_json(_normalize_message(data.get("message", {})))
    raise ToolExecutionError(f"[Error] Failed to send thread reply: {_format_json(data)}")


@rocketchat_tool(
    "pin_message",
    "Pin a message.",
    {
        "message_id": {"type": "string", "description": "Message ID."},
    },
    is_write=True,
    group="messages",
    short_description="Pin an existing message.",
)
def pin_message(message_id):
    data = _api_json("POST", "chat.pinMessage", json={"messageId": message_id})
    if data.get("success"):
        return f"Message {message_id} was pinned."
    raise ToolExecutionError(f"[Error] Failed to pin message: {_format_json(data)}")


@rocketchat_tool(
    "unpin_message",
    "Unpin a message.",
    {
        "message_id": {"type": "string", "description": "Message ID."},
    },
    is_write=True,
    group="messages",
    short_description="Remove pin from a message.",
)
def unpin_message(message_id):
    data = _api_json("POST", "chat.unPinMessage", json={"messageId": message_id})
    if data.get("success"):
        return f"Message {message_id} was unpinned."
    raise ToolExecutionError(f"[Error] Failed to unpin message: {_format_json(data)}")


@rocketchat_tool(
    "delete_message",
    "Delete a message. Dangerous operation; irreversible.",
    {
        "message_id": {"type": "string", "description": "Message ID."},
    },
    is_write=True,
    group="messages",
    short_description="Delete one message permanently.",
)
def delete_message(message_id):
    message = _message_info(message_id)
    data = _api_json("POST", "chat.delete", json={"roomId": message.get("rid", ""), "msgId": message_id})
    if data.get("success"):
        return f"Message {message_id} was deleted."
    raise ToolExecutionError(f"[Error] Failed to delete message: {_format_json(data)}")


@rocketchat_tool(
    "create_channel",
    "Create a new public channel.",
    {
        "name": {"type": "string", "description": "Channel name without # and without spaces."},
        "members": {"type": "array", "items": {"type": "string"}, "description": "Initial member username list (optional)."},
        "topic": {"type": "string", "description": "Channel topic (optional)."},
        "description": {"type": "string", "description": "Channel description (optional)."},
    },
    is_write=True,
    group="channels",
    short_description="Create a new public channel.",
)
def create_channel(name, members=None, topic="", description=""):
    data = _api_json("POST", "channels.create", json={"name": name, "members": members or []})
    channel = data.get("channel") or {}
    room_id = channel.get("_id", "")
    if not room_id:
        raise ToolExecutionError(f"[Error] Failed to create public channel: {_format_json(data)}")
    if topic:
        _api_json("POST", "channels.setTopic", json={"roomId": room_id, "topic": topic})
    if description:
        _api_json("POST", "channels.setDescription", json={"roomId": room_id, "description": description})
    return f"Public channel #{name} was created successfully."


@rocketchat_tool(
    "invite_user_to_channel",
    "Invite a user to a public channel.",
    {
        "channel_name": {"type": "string", "description": "Public channel name without #."},
        "username": {"type": "string", "description": "Username to invite."},
    },
    is_write=True,
    group="channels",
    short_description="Invite a user into a public channel.",
)
def invite_user_to_channel(channel_name, username):
    channel = _public_room_info(channel_name)
    data = _api_json(
        "POST",
        "channels.invite",
        json={"roomId": channel.get("_id", ""), "userId": _user_id(username)},
    )
    if data.get("success"):
        return f"Invited {username} to join #{channel_name}."
    raise ToolExecutionError(f"[Error] Failed to invite user to public channel: {_format_json(data)}")


@rocketchat_tool(
    "remove_user_from_channel",
    "Remove a user from a public channel.",
    {
        "channel_name": {"type": "string", "description": "Public channel name without #."},
        "username": {"type": "string", "description": "Username to remove."},
    },
    is_write=True,
    group="moderation",
    short_description="Remove a user from a public channel.",
)
def remove_user_from_channel(channel_name, username):
    channel = _public_room_info(channel_name)
    data = _api_json(
        "POST",
        "channels.kick",
        json={"roomId": channel.get("_id", ""), "userId": _user_id(username)},
    )
    if data.get("success"):
        return f"Removed {username} from #{channel_name}."
    raise ToolExecutionError(f"[Error] Failed to remove user from public channel: {_format_json(data)}")


@rocketchat_tool(
    "set_channel_topic",
    "Set the topic of a public channel.",
    {
        "channel_name": {"type": "string", "description": "Public channel name without #."},
        "topic": {"type": "string", "description": "New topic content."},
    },
    is_write=True,
    group="channels",
    short_description="Update a public channel topic.",
)
def set_channel_topic(channel_name, topic):
    channel = _public_room_info(channel_name)
    data = _api_json("POST", "channels.setTopic", json={"roomId": channel.get("_id", ""), "topic": topic})
    if data.get("success"):
        return f"Topic for channel #{channel_name} was updated."
    raise ToolExecutionError(f"[Error] Failed to set public channel topic: {_format_json(data)}")


@rocketchat_tool(
    "set_channel_description",
    "Set the description of a public channel.",
    {
        "channel_name": {"type": "string", "description": "Public channel name without #."},
        "description": {"type": "string", "description": "New description content."},
    },
    is_write=True,
    group="channels",
    short_description="Update a public channel description.",
)
def set_channel_description(channel_name, description):
    channel = _public_room_info(channel_name)
    data = _api_json(
        "POST",
        "channels.setDescription",
        json={"roomId": channel.get("_id", ""), "description": description},
    )
    if data.get("success"):
        return f"Description for channel #{channel_name} was updated."
    raise ToolExecutionError(f"[Error] Failed to set public channel description: {_format_json(data)}")


@rocketchat_tool(
    "archive_channel",
    "Archive a public channel and make it read-only.",
    {
        "channel_name": {"type": "string", "description": "Public channel name without #."},
    },
    is_write=True,
    group="moderation",
    short_description="Archive a public channel.",
)
def archive_channel(channel_name):
    channel = _public_room_info(channel_name)
    data = _api_json("POST", "channels.archive", json={"roomId": channel.get("_id", "")})
    if data.get("success"):
        return f"Channel #{channel_name} was archived."
    raise ToolExecutionError(f"[Error] Failed to archive public channel: {_format_json(data)}")


@rocketchat_tool(
    "delete_channel",
    "Delete a public channel and all its messages. Dangerous operation; irreversible.",
    {
        "channel_name": {"type": "string", "description": "Public channel name to delete, without #."},
    },
    is_write=True,
    group="moderation",
    short_description="Delete a public channel permanently.",
)
def delete_channel(channel_name):
    channel = _public_room_info(channel_name)
    data = _api_json("POST", "rooms.delete", json={"roomId": channel.get("_id", "")})
    if data.get("success"):
        return f"Public channel #{channel_name} was deleted."
    raise ToolExecutionError(f"[Error] Failed to delete public channel: {_format_json(data)}")


@rocketchat_tool(
    "create_private_channel",
    "Create a new private channel.",
    {
        "name": {"type": "string", "description": "Private channel name without # and without spaces."},
        "members": {"type": "array", "items": {"type": "string"}, "description": "Initial member username list (optional)."},
        "topic": {"type": "string", "description": "Channel topic (optional)."},
        "description": {"type": "string", "description": "Channel description (optional)."},
    },
    is_write=True,
    group="private_channels",
    short_description="Create a new private channel.",
)
def create_private_channel(name, members=None, topic="", description=""):
    data = _api_json("POST", "groups.create", json={"name": name, "members": members or []})
    group = data.get("group") or {}
    room_id = group.get("_id", "")
    if not room_id:
        raise ToolExecutionError(f"[Error] Failed to create private channel: {_format_json(data)}")
    if topic:
        _api_json("POST", "groups.setTopic", json={"roomId": room_id, "topic": topic})
    if description:
        _api_json("POST", "groups.setDescription", json={"roomId": room_id, "description": description})
    return f"Private channel #{name} was created successfully."


@rocketchat_tool(
    "invite_user_to_private_channel",
    "Invite a user to a private channel.",
    {
        "room_name": {"type": "string", "description": "Private channel name without #."},
        "username": {"type": "string", "description": "Username to invite."},
    },
    is_write=True,
    group="private_channels",
    short_description="Invite a user into a private channel.",
)
def invite_user_to_private_channel(room_name, username):
    group = _private_room_info(room_name)
    data = _api_json(
        "POST",
        "groups.invite",
        json={"roomId": group.get("_id", ""), "userId": _user_id(username)},
    )
    if data.get("success"):
        return f"Invited {username} to join private channel #{room_name}."
    raise ToolExecutionError(f"[Error] Failed to invite user to private channel: {_format_json(data)}")


@rocketchat_tool(
    "remove_user_from_private_channel",
    "Remove a user from a private channel.",
    {
        "room_name": {"type": "string", "description": "Private channel name without #."},
        "username": {"type": "string", "description": "Username to remove."},
    },
    is_write=True,
    group="moderation",
    short_description="Remove a user from a private channel.",
)
def remove_user_from_private_channel(room_name, username):
    group = _private_room_info(room_name)
    data = _api_json(
        "POST",
        "groups.kick",
        json={"roomId": group.get("_id", ""), "userId": _user_id(username)},
    )
    if data.get("success"):
        return f"Removed {username} from private channel #{room_name}."
    raise ToolExecutionError(f"[Error] Failed to remove user from private channel: {_format_json(data)}")


@rocketchat_tool(
    "set_private_channel_topic",
    "Set the topic of a private channel.",
    {
        "room_name": {"type": "string", "description": "Private channel name without #."},
        "topic": {"type": "string", "description": "New topic content."},
    },
    is_write=True,
    group="private_channels",
    short_description="Update a private channel topic.",
)
def set_private_channel_topic(room_name, topic):
    group = _private_room_info(room_name)
    data = _api_json("POST", "groups.setTopic", json={"roomId": group.get("_id", ""), "topic": topic})
    if data.get("success"):
        return f"Topic for private channel #{room_name} was updated."
    raise ToolExecutionError(f"[Error] Failed to set private channel topic: {_format_json(data)}")


@rocketchat_tool(
    "set_private_channel_description",
    "Set the description of a private channel.",
    {
        "room_name": {"type": "string", "description": "Private channel name without #."},
        "description": {"type": "string", "description": "New description content."},
    },
    is_write=True,
    group="private_channels",
    short_description="Update a private channel description.",
)
def set_private_channel_description(room_name, description):
    group = _private_room_info(room_name)
    data = _api_json(
        "POST",
        "groups.setDescription",
        json={"roomId": group.get("_id", ""), "description": description},
    )
    if data.get("success"):
        return f"Description for private channel #{room_name} was updated."
    raise ToolExecutionError(f"[Error] Failed to set private channel description: {_format_json(data)}")


@rocketchat_tool(
    "archive_private_channel",
    "Archive a private channel and make it read-only.",
    {
        "room_name": {"type": "string", "description": "Private channel name without #."},
    },
    is_write=True,
    group="moderation",
    short_description="Archive a private channel.",
)
def archive_private_channel(room_name):
    group = _private_room_info(room_name)
    data = _api_json("POST", "groups.archive", json={"roomId": group.get("_id", "")})
    if data.get("success"):
        return f"Private channel #{room_name} was archived."
    raise ToolExecutionError(f"[Error] Failed to archive private channel: {_format_json(data)}")


@rocketchat_tool(
    "delete_private_channel",
    "Delete a private channel and all its messages. Dangerous operation; irreversible.",
    {
        "room_name": {"type": "string", "description": "Private channel name to delete, without #."},
    },
    is_write=True,
    group="moderation",
    short_description="Delete a private channel permanently.",
)
def delete_private_channel(room_name):
    group = _private_room_info(room_name)
    data = _api_json("POST", "rooms.delete", json={"roomId": group.get("_id", "")})
    if data.get("success"):
        return f"Private channel #{room_name} was deleted."
    raise ToolExecutionError(f"[Error] Failed to delete private channel: {_format_json(data)}")


@rocketchat_tool(
    "create_direct_message",
    "Create or open a direct-message conversation with the specified user.",
    {
        "username": {"type": "string", "description": "The other user's username."},
    },
    is_write=True,
    group="direct_messages",
    short_description="Open a DM room with a user.",
)
def create_direct_message(username):
    data = _api_json("POST", "dm.create", json={"username": username})
    room = data.get("room") or {}
    if not room.get("_id"):
        raise ToolExecutionError(f"[Error] Failed to create direct-message room: {_format_json(data)}")
    return _format_json({
        "id": room.get("_id", ""),
        "name": room.get("name", room.get("fname", "")),
        "usernames": room.get("usernames", []),
    })


@rocketchat_tool(
    "send_direct_message",
    "Send a direct message to the specified user.",
    {
        "username": {"type": "string", "description": "The other user's username."},
        "text": {"type": "string", "description": "Direct message content."},
    },
    is_write=True,
    group="direct_messages",
    short_description="Send a DM to a user.",
)
def send_direct_message(username, text):
    room = _api_json("POST", "dm.create", json={"username": username}).get("room", {})
    room_id = room.get("_id", "")
    if not room_id:
        raise ToolExecutionError(f"[Error] Could not create or locate the direct message room with {username}")
    data = _api_json("POST", "chat.sendMessage", json={"message": {"rid": room_id, "msg": text}})
    if data.get("success"):
        return _format_json(_normalize_message(data.get("message", {})))
    raise ToolExecutionError(f"[Error] Failed to send direct message: {_format_json(data)}")


@rocketchat_tool(
    "create_user",
    "Create a new user account.",
    {
        "username": {"type": "string", "description": "Username."},
        "email": {"type": "string", "description": "Email address."},
        "password": {"type": "string", "description": "Initial password."},
        "name": {"type": "string", "description": "Display name."},
        "roles": {"type": "array", "items": {"type": "string"}, "description": "Role list. Default is ['user']."},
    },
    is_write=True,
    group="users",
    short_description="Create a workspace user.",
)
def create_user(username, email, password, name, roles=None):
    data = _api_json(
        "POST",
        "users.create",
        json={
            "username": username,
            "email": email,
            "password": password,
            "name": name,
            "roles": roles or ["user"],
            "verified": True,
        },
    )
    user = data.get("user") or {}
    if user.get("_id"):
        return _format_json(_normalize_user(user))
    raise ToolExecutionError(f"[Error] Failed to create user: {_format_json(data)}")


@rocketchat_tool(
    "set_user_active_status",
    "Enable or disable a user account.",
    {
        "username": {"type": "string", "description": "Username."},
        "active": {"type": "boolean", "description": "true enables the account, false disables it."},
    },
    is_write=True,
    group="moderation",
    short_description="Enable or disable a user account.",
)
def set_user_active_status(username, active):
    data = _api_json(
        "POST",
        "users.setActiveStatus",
        json={"userId": _user_id(username), "activeStatus": bool(active)},
    )
    if data.get("success"):
        return f"User {username} was {'enabled' if active else 'disabled'}."
    raise ToolExecutionError(f"[Error] Failed to update user status: {_format_json(data)}")


@rocketchat_tool(
    "delete_user",
    "Delete a user and its data. Extremely dangerous; irreversible.",
    {
        "username": {"type": "string", "description": "Username to delete."},
    },
    is_write=True,
    group="moderation",
    short_description="Delete a user account permanently.",
)
def delete_user(username):
    data = _api_json("POST", "users.delete", json={"userId": _user_id(username)})
    if data.get("success"):
        return f"User {username} was permanently deleted."
    raise ToolExecutionError(f"[Error] Failed to delete user: {_format_json(data)}")


@rocketchat_tool(
    "create_incoming_integration",
    "Create an incoming webhook integration.",
    {
        "name": {"type": "string", "description": "Integration name."},
        "channel": {"type": "string", "description": "Target channel name, for example #general."},
        "username": {"type": "string", "description": "Display username for messages."},
        "enabled": {"type": "boolean", "description": "Whether the integration is enabled. Default true."},
        "script_enabled": {"type": "boolean", "description": "Whether scripting is enabled. Default false."},
        "script": {"type": "string", "description": "Optional script content."},
    },
    is_write=True,
    group="integrations",
    short_description="Create an incoming webhook integration.",
)
def create_incoming_integration(name, channel, username, enabled=True, script_enabled=False, script=""):
    payload = {
        "type": "webhook-incoming",
        "name": name,
        "enabled": bool(enabled),
        "channel": channel,
        "username": username,
        "scriptEnabled": bool(script_enabled),
    }
    if script:
        payload["script"] = script
    data = _api_json("POST", "integrations.create", json=payload)
    if data.get("success"):
        return _format_json(_normalize_integration(data.get("integration", {})))
    raise ToolExecutionError(f"[Error] Failed to create incoming integration: {_format_json(data)}")


@rocketchat_tool(
    "create_outgoing_integration",
    "Create an outgoing webhook integration.",
    {
        "name": {"type": "string", "description": "Integration name."},
        "channel": {"type": "string", "description": "Source channel name, for example #incidents."},
        "username": {"type": "string", "description": "Display username for messages."},
        "target_urls": {"type": "array", "items": {"type": "string"}, "description": "Target URL list."},
        "event": {"type": "string", "description": "Trigger event. Default is sendMessage."},
        "enabled": {"type": "boolean", "description": "Whether the integration is enabled. Default true."},
        "script_enabled": {"type": "boolean", "description": "Whether scripting is enabled. Default false."},
        "script": {"type": "string", "description": "Optional script content."},
        "trigger_words": {"type": "array", "items": {"type": "string"}, "description": "Trigger word list (optional)."},
    },
    is_write=True,
    group="integrations",
    short_description="Create an outgoing webhook integration.",
)
def create_outgoing_integration(
    name,
    channel,
    username,
    target_urls,
    event="sendMessage",
    enabled=True,
    script_enabled=False,
    script="",
    trigger_words=None,
):
    payload = {
        "type": "webhook-outgoing",
        "name": name,
        "enabled": bool(enabled),
        "channel": channel,
        "username": username,
        "urls": target_urls,
        "event": event,
        "scriptEnabled": bool(script_enabled),
        "triggerWords": trigger_words or [],
    }
    if script:
        payload["script"] = script
    data = _api_json("POST", "integrations.create", json=payload)
    if data.get("success"):
        return _format_json(_normalize_integration(data.get("integration", {})))
    raise ToolExecutionError(f"[Error] Failed to create outgoing integration: {_format_json(data)}")


@rocketchat_tool(
    "remove_integration",
    "Delete an integration. Dangerous operation; irreversible.",
    {
        "name": {"type": "string", "description": "Integration name."},
        "integration_type": {"type": "string", "description": "Integration type, for example webhook-incoming or webhook-outgoing."},
    },
    is_write=True,
    group="integrations",
    short_description="Remove an existing integration.",
)
def remove_integration(name, integration_type=""):
    integration = _find_integration(name=name, integration_type=integration_type)
    integration_id = integration.get("_id", integration.get("integrationId", ""))
    if not integration_id:
        raise ToolExecutionError(f"[Error] Integration ID not found: {name}")
    data = _api_json("POST", "integrations.remove", json={"integrationId": integration_id})
    if data.get("success"):
        return f"Integration {name} was deleted."
    raise ToolExecutionError(f"[Error] Failed to delete integration: {_format_json(data)}")
