#!/usr/bin/env python3
"""
Rocket.Chat seed data script.

Creates the admin user, users, channels, and messages through the REST API.
After Rocket.Chat starts for the first time, the admin user must be registered,
then the admin credentials are used to create the rest of the data.
"""

import json
import os
import time
from pathlib import Path

import requests

BASE_URL = os.environ.get("ROCKETCHAT_BASE_URL", "http://localhost:3100").rstrip("/")
ADMIN_USER = os.environ.get("ROCKETCHAT_ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ROCKETCHAT_ADMIN_PASSWORD", "Admin123!")
ADMIN_EMAIL = os.environ.get("ROCKETCHAT_ADMIN_EMAIL", "admin@example.com")
MANIFEST_PATH = Path(
    os.environ.get(
        "ROCKETCHAT_SEED_MANIFEST",
        Path(__file__).resolve().parents[1] / "seed_manifest.json",
    )
)


class SeedError(RuntimeError):
    pass


_auth_cache = {"user_id": None, "token": None}


def _api(method, endpoint, json_data=None, auth=True):
    """Call Rocket.Chat REST API."""
    url = f"{BASE_URL}/api/v1/{endpoint.lstrip('/')}"
    headers = {"Content-Type": "application/json"}
    if auth and _auth_cache["token"]:
        headers["X-Auth-Token"] = _auth_cache["token"]
        headers["X-User-Id"] = _auth_cache["user_id"]
    resp = requests.request(method, url, json=json_data, headers=headers, timeout=30)
    return resp


def wait_for_rocketchat(max_wait=180, interval=3):
    """Wait for Rocket.Chat API to become ready."""
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            resp = requests.get(f"{BASE_URL}/api/info", timeout=5)
            if resp.status_code == 200:
                print("[seed] Rocket.Chat API is ready")
                return
        except Exception:
            pass
        time.sleep(interval)
    raise SeedError(f"Rocket.Chat API not ready after {max_wait}s")


def setup_admin():
    """Register admin user or login if already exists."""
    # Try login first
    resp = _api("POST", "login", {"user": ADMIN_USER, "password": ADMIN_PASSWORD}, auth=False)
    if resp.status_code == 200:
        data = resp.json().get("data", {})
        _auth_cache["user_id"] = data.get("userId", "")
        _auth_cache["token"] = data.get("authToken", "")
        print(f"[seed] Admin login successful (userId: {_auth_cache['user_id']})")
        return

    # Try registration
    resp = _api("POST", "users.register", {
        "username": ADMIN_USER,
        "email": ADMIN_EMAIL,
        "pass": ADMIN_PASSWORD,
        "name": "Administrator",
    }, auth=False)
    if resp.status_code == 200 and resp.json().get("success"):
        print("[seed] Admin user registered")
    else:
        # May already exist, try login again
        pass

    # Login
    resp = _api("POST", "login", {"user": ADMIN_USER, "password": ADMIN_PASSWORD}, auth=False)
    if resp.status_code != 200:
        raise SeedError(f"Admin login failed: {resp.status_code} {resp.text[:300]}")
    data = resp.json().get("data", {})
    _auth_cache["user_id"] = data.get("userId", "")
    _auth_cache["token"] = data.get("authToken", "")
    print(f"[seed] Admin login successful (userId: {_auth_cache['user_id']})")


def create_user(username, email, password, name, roles=None):
    """Create a user via admin API."""
    resp = _api("POST", "users.create", {
        "username": username,
        "email": email,
        "password": password,
        "name": name,
        "roles": roles or ["user"],
        "verified": True,
    })
    if resp.status_code == 200 and resp.json().get("success"):
        print(f"[seed]   User created: {username}")
        return resp.json().get("user", {}).get("_id", "")
    # May already exist
    error_msg = resp.json().get("error", "") if resp.status_code == 400 else ""
    if "already in use" in error_msg.lower() or "is already taken" in error_msg.lower():
        print(f"[seed]   User exists: {username}")
        # Look up user ID
        lookup = _api("GET", f"users.info?username={username}")
        if lookup.status_code == 200:
            return lookup.json().get("user", {}).get("_id", "")
        return ""
    print(f"[seed]   User creation for {username}: {resp.status_code} {resp.text[:200]}")
    return ""


def create_channel(name, members=None, topic="", description="", private=False):
    """Create a channel or group."""
    endpoint = "groups.create" if private else "channels.create"
    payload = {"name": name, "members": members or []}
    resp = _api("POST", endpoint, payload)

    if resp.status_code == 200 and resp.json().get("success"):
        ch = resp.json().get("channel" if not private else "group", {})
        room_id = ch.get("_id", "")
        print(f"[seed]   {'Group' if private else 'Channel'} created: #{name}")
    else:
        error_msg = resp.json().get("error", "") if resp.status_code == 400 else ""
        if "already exists" in error_msg.lower() or "name is already" in error_msg.lower() or "duplicate" in error_msg.lower():
            print(f"[seed]   {'Group' if private else 'Channel'} exists: #{name}")
            # Look up room ID
            info_endpoint = "groups.info" if private else "channels.info"
            lookup = _api("GET", f"{info_endpoint}?roomName={name}")
            if lookup.status_code == 200:
                key = "group" if private else "channel"
                room_id = lookup.json().get(key, {}).get("_id", "")
            else:
                return ""
        else:
            print(f"[seed]   Channel creation for #{name}: {resp.status_code} {resp.text[:200]}")
            return ""

    # Set topic and description
    set_endpoint = "groups" if private else "channels"
    if topic:
        _api("POST", f"{set_endpoint}.setTopic", {"roomId": room_id, "topic": topic})
    if description:
        _api("POST", f"{set_endpoint}.setDescription", {"roomId": room_id, "description": description})

    return room_id


def send_message(channel_name, text, alias=None):
    """Send a message to a room using chat.postMessage (admin, supports alias)."""
    payload = {"channel": f"#{channel_name}", "text": text}
    if alias:
        payload["alias"] = alias

    resp = _api("POST", "chat.postMessage", json_data=payload)
    if resp.status_code == 200 and resp.json().get("success"):
        return resp.json().get("message", {}).get("_id", "")
    # Fallback for private groups
    payload["channel"] = channel_name
    resp = _api("POST", "chat.postMessage", json_data=payload)
    if resp.status_code == 200 and resp.json().get("success"):
        return resp.json().get("message", {}).get("_id", "")
    print(f"[seed]   Message send to #{channel_name}: {resp.status_code} {resp.text[:200]}")
    return ""


def send_thread_reply(channel_name, root_message_id, text, show_in_room=False):
    info_resp = _api("GET", f"channels.info?roomName={channel_name}")
    if info_resp.status_code == 200 and info_resp.json().get("success"):
        room_id = info_resp.json().get("channel", {}).get("_id", "")
    else:
        info_resp = _api("GET", f"groups.info?roomName={channel_name}")
        room_id = info_resp.json().get("group", {}).get("_id", "") if info_resp.status_code == 200 else ""
    if not room_id:
        print(f"[seed]   Thread reply room lookup failed for #{channel_name}")
        return ""
    payload = {
        "message": {
            "rid": room_id,
            "msg": text,
            "tmid": root_message_id,
            "tshow": bool(show_in_room),
        }
    }
    resp = _api("POST", "chat.sendMessage", json_data=payload)
    if resp.status_code == 200 and resp.json().get("success"):
        return resp.json().get("message", {}).get("_id", "")
    print(f"[seed]   Thread reply send to #{channel_name}: {resp.status_code} {resp.text[:200]}")
    return ""


def send_direct_message(username, text):
    dm_resp = _api("POST", "dm.create", {"username": username})
    if dm_resp.status_code != 200 or not dm_resp.json().get("success"):
        print(f"[seed]   DM room creation for @{username}: {dm_resp.status_code} {dm_resp.text[:200]}")
        return ""
    room_id = dm_resp.json().get("room", {}).get("_id", "")
    if not room_id:
        return ""
    resp = _api("POST", "chat.sendMessage", {
        "message": {
            "rid": room_id,
            "msg": text,
        }
    })
    if resp.status_code == 200 and resp.json().get("success"):
        return resp.json().get("message", {}).get("_id", "")
    print(f"[seed]   DM send to @{username}: {resp.status_code} {resp.text[:200]}")
    return ""


def create_integration(spec):
    name = spec["name"]
    list_resp = _api("GET", f"integrations.list?name={name}")
    if list_resp.status_code == 200:
        for integration in list_resp.json().get("integrations", []) or []:
            if integration.get("name") == name:
                print(f"[seed]   Integration exists: {name}")
                return integration.get("_id", "")

    payload = {
        "type": spec["type"],
        "name": name,
        "enabled": spec.get("enabled", True),
        "channel": spec["channel"],
        "username": spec.get("username", "ops.webhook"),
        "scriptEnabled": spec.get("script_enabled", False),
    }
    if spec["type"] == "webhook-outgoing":
        payload["urls"] = spec.get("target_urls", [])
        payload["event"] = spec.get("event", "sendMessage")
        payload["triggerWords"] = spec.get("trigger_words", [])
    if spec.get("script"):
        payload["script"] = spec["script"]

    resp = _api("POST", "integrations.create", payload)
    if resp.status_code == 200 and resp.json().get("success"):
        print(f"[seed]   Integration created: {name}")
        return resp.json().get("integration", {}).get("_id", "")
    print(f"[seed]   Integration create for {name}: {resp.status_code} {resp.text[:200]}")
    return ""


def login_as(username, password):
    """Login as a specific user, returning (user_id, token)."""
    resp = _api("POST", "login", {"user": username, "password": password}, auth=False)
    if resp.status_code == 200:
        data = resp.json().get("data", {})
        return data.get("userId", ""), data.get("authToken", "")
    return None, None


def seed_from_manifest(manifest_path):
    with open(manifest_path) as f:
        manifest = json.load(f)

    # Create users
    user_passwords = {}
    for user_spec in manifest.get("users", []):
        create_user(
            user_spec["username"],
            user_spec["email"],
            user_spec["password"],
            user_spec["name"],
            user_spec.get("roles", ["user"]),
        )
        user_passwords[user_spec["username"]] = user_spec["password"]

    # Admin password
    admin_spec = manifest.get("admin", {})
    user_passwords[admin_spec.get("username", "admin")] = admin_spec.get("password", ADMIN_PASSWORD)

    # Create channels
    for ch_spec in manifest.get("channels", []):
        create_channel(
            ch_spec["name"],
            members=ch_spec.get("members", []),
            topic=ch_spec.get("topic", ""),
            description=ch_spec.get("description", ""),
            private=ch_spec.get("private", False),
        )

    # Send messages — all via admin using postMessage
    for msg_spec in manifest.get("messages", []):
        sender = msg_spec["sender"]
        channel = msg_spec["channel"]
        for text in msg_spec.get("texts", []):
            # Prefix with sender name so messages have attribution
            prefixed = f"[{sender}] {text}" if sender != ADMIN_USER else text
            send_message(channel, prefixed)
        print(f"[seed]   Sent {len(msg_spec['texts'])} messages to #{channel} as {sender}")

    for dm_spec in manifest.get("direct_messages", []):
        username = dm_spec["username"]
        for text in dm_spec.get("texts", []):
            send_direct_message(username, text)
        print(f"[seed]   Sent {len(dm_spec.get('texts', []))} direct messages to @{username}")

    for thread_spec in manifest.get("threaded_messages", []):
        channel = thread_spec["channel"]
        root_message_id = send_message(channel, thread_spec["root_text"])
        reply_count = 0
        if root_message_id:
            for reply in thread_spec.get("replies", []):
                send_thread_reply(channel, root_message_id, reply, show_in_room=False)
                reply_count += 1
        print(f"[seed]   Seeded thread in #{channel} with {reply_count} replies")

    for integration_spec in manifest.get("integrations", []):
        create_integration(integration_spec)


def main():
    wait_for_rocketchat()
    setup_admin()
    print(f"[seed] Seeding Rocket.Chat as user '{ADMIN_USER}'")
    seed_from_manifest(MANIFEST_PATH)
    print("[seed] Rocket.Chat seeding complete")


if __name__ == "__main__":
    main()
