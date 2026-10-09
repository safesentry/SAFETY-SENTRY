"""
ownCloud (oCIS) WebDAV + OCS tool registration.

Uses WebDAV for file and folder operations, and the OCS API for managing
share links.
Public interface exposed to environment.py:
  get_all_schemas() -> list
  call_tool(name, args) -> str
  get_tool_names() -> list
"""

import json
import os
import xml.etree.ElementTree as ET
from collections import deque
from urllib.parse import quote, unquote, urlparse

from ...exceptions import ToolExecutionError
from ...service_tools import ServiceToolRegistry

try:
    import requests
    from requests.auth import HTTPBasicAuth
except ModuleNotFoundError:
    requests = None
    HTTPBasicAuth = None

try:
    import urllib3
except ModuleNotFoundError:
    urllib3 = None

if urllib3 is not None:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


_config = {
    "base_url": os.environ.get("OWNCLOUD_BASE_URL", "https://localhost:9200").rstrip("/"),
    "admin_user": os.environ.get("OWNCLOUD_ADMIN_USER", "admin"),
    "admin_password": os.environ.get("OWNCLOUD_ADMIN_PASSWORD", "Admin123!"),
    "default_public_link_password": os.environ.get("OWNCLOUD_PUBLIC_LINK_PASSWORD", "Share123!"),
}

_REGISTRY = ServiceToolRegistry(service_id="owncloud")

_SHARE_TYPE_LABELS = {
    0: "user",
    3: "public_link",
}


def owncloud_tool(name, description, params, required=None, is_write=False, group="", short_description=""):
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
        raise ToolExecutionError("requests is not installed, so the ownCloud API cannot be called. Run: pip install requests")


def _auth():
    return HTTPBasicAuth(_config["admin_user"], _config["admin_password"])


def _normalize_path(path=""):
    path = (path or "").replace("\\", "/").strip()
    if path in ("", "/"):
        return ""
    parts = [segment for segment in path.split("/") if segment and segment != "."]
    return "/".join(parts)


def _path_parent(path):
    normalized = _normalize_path(path)
    if not normalized or "/" not in normalized:
        return ""
    return normalized.rsplit("/", 1)[0]


def _join_path(*parts):
    segments = []
    for part in parts:
        normalized = _normalize_path(part)
        if normalized:
            segments.extend(normalized.split("/"))
    return "/".join(segments)


def _encode_path(path=""):
    normalized = _normalize_path(path)
    if not normalized:
        return ""
    return "/".join(quote(segment, safe="") for segment in normalized.split("/"))


def _webdav_url(path=""):
    user = quote(_config["admin_user"], safe="")
    encoded_path = _encode_path(path)
    suffix = f"/{encoded_path}" if encoded_path else ""
    return f"{_config['base_url']}/dav/files/{user}{suffix}"


def _ocs_url(path=""):
    return f"{_config['base_url']}/{path.lstrip('/')}"


def _api(method, url, **kwargs):
    _require_requests()
    kwargs.setdefault("auth", _auth())
    kwargs.setdefault("verify", False)
    kwargs.setdefault("timeout", 15)
    try:
        return requests.request(method, url, **kwargs)
    except requests.RequestException as exc:
        raise ToolExecutionError(f"[ownCloud Request Failed] {type(exc).__name__}: {exc}") from exc


def _format_json(data):
    if isinstance(data, str):
        return data
    return json.dumps(data, ensure_ascii=False, indent=2)


def _coerce_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _href_to_relative_path(href):
    parsed = urlparse(href)
    raw_path = parsed.path or href or ""
    decoded = unquote(raw_path)
    user = _config["admin_user"]
    prefix = f"/dav/files/{user}/"
    root_prefix = f"/dav/files/{user}"
    if decoded.startswith(prefix):
        return decoded[len(prefix):].strip("/")
    if decoded.startswith(root_prefix):
        return decoded[len(root_prefix):].strip("/")
    return decoded.strip("/")


def _parse_propfind_entries(xml_text):
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    ns = {
        "d": "DAV:",
        "oc": "http://owncloud.org/ns",
    }
    entries = []
    for response in root.findall("d:response", ns):
        href = response.findtext("d:href", "", ns)
        propstat = response.find("d:propstat", ns)
        if propstat is None:
            continue
        prop = propstat.find("d:prop", ns)
        if prop is None:
            continue
        resource_type = prop.find("d:resourcetype", ns)
        is_dir = resource_type is not None and resource_type.find("d:collection", ns) is not None
        size = prop.findtext("d:getcontentlength", "0", ns)
        last_modified = prop.findtext("d:getlastmodified", "", ns)
        display_name = prop.findtext("d:displayname", "", ns)
        relative_path = _href_to_relative_path(href)
        entries.append(
            {
                "href": href,
                "path": relative_path,
                "name": display_name or (relative_path.rstrip("/").split("/")[-1] if relative_path else "/"),
                "type": "directory" if is_dir else "file",
                "size": _coerce_int(size, 0),
                "last_modified": last_modified,
                "etag": prop.findtext("d:getetag", "", ns),
                "file_id": prop.findtext("oc:fileid", "", ns),
            }
        )
    return entries


def _list_entries_once(path=""):
    normalized = _normalize_path(path)
    url = _webdav_url(normalized)
    resp = _api("PROPFIND", url, headers={"Depth": "1"})
    if resp.status_code == 404:
        raise ToolExecutionError(f"[Error] Path does not exist: {normalized or '/'}")
    if resp.status_code != 207:
        raise ToolExecutionError(f"[Error] PROPFIND {normalized or '/'} -> {resp.status_code}: {resp.text[:300]}")
    return _child_entries(normalized, _parse_propfind_entries(resp.text))


def _walk_directory_entries(path=""):
    normalized = _normalize_path(path)
    pending = deque([normalized])
    seen_dirs = set()
    entries = []
    while pending:
        current = pending.popleft()
        if current in seen_dirs:
            continue
        seen_dirs.add(current)
        children = _list_entries_once(current)
        entries.extend(children)
        for child in children:
            if child.get("type") == "directory":
                child_path = _normalize_path(child.get("path", ""))
                if child_path and child_path not in seen_dirs:
                    pending.append(child_path)
    return entries


def _child_entries(path, entries):
    normalized = _normalize_path(path)
    return [entry for entry in entries if entry.get("path", "") != normalized]


def _ocs_headers(extra=None):
    headers = {
        "OCS-APIREQUEST": "true",
        "Accept": "application/json",
    }
    if extra:
        headers.update(extra)
    return headers


def _normalize_share(share):
    share_type = _coerce_int(share.get("share_type"), -1)
    permissions = _coerce_int(share.get("permissions"), 0)
    return {
        "id": str(share.get("id", "")),
        "path": (share.get("path") or "").lstrip("/"),
        "share_type": share_type,
        "share_type_label": _SHARE_TYPE_LABELS.get(share_type, f"type_{share_type}"),
        "permissions": permissions,
        "name": share.get("name", ""),
        "url": share.get("url", ""),
        "token": share.get("token", ""),
        "share_with": share.get("share_with", ""),
        "expiration": share.get("expiration", ""),
    }


def _strip_xml_ns(tag):
    if "}" in tag:
        return tag.rsplit("}", 1)[-1]
    return tag


def _xml_node_to_value(node):
    children = [child for child in list(node) if isinstance(child.tag, str)]
    if not children:
        return (node.text or "").strip()

    grouped = {}
    for child in children:
        key = _strip_xml_ns(child.tag)
        grouped.setdefault(key, []).append(_xml_node_to_value(child))

    if len(grouped) == 1:
        only_key = next(iter(grouped))
        values = grouped[only_key]
        if len(values) > 1:
            return values
    return {
        key: values[0] if len(values) == 1 else values
        for key, values in grouped.items()
    }


def _extract_ocs_data(resp, action_label):
    try:
        return (resp.json().get("ocs", {}) or {}).get("data", [])
    except ValueError:
        pass

    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as exc:
        raise ToolExecutionError(f"[Error] {action_label} returned an invalid response: {resp.text[:300]}") from exc

    data_node = None
    for child in root.iter():
        if _strip_xml_ns(child.tag) == "data":
            data_node = child
            break
    if data_node is None:
        raise ToolExecutionError(f"[Error] {action_label} response is missing the data node: {resp.text[:300]}")
    return _xml_node_to_value(data_node)


def _list_shares_data(path=""):
    url = _ocs_url("ocs/v2.php/apps/files_sharing/api/v1/shares")
    params = {"format": "json"}
    normalized = _normalize_path(path)
    if normalized:
        params["path"] = f"/{normalized}"
    resp = _api("GET", url, params=params, headers=_ocs_headers())
    if resp.status_code != 200:
        raise ToolExecutionError(f"[Error] Failed to list shares: {resp.status_code}: {resp.text[:300]}")
    data = _extract_ocs_data(resp, "list_shares")
    if isinstance(data, dict):
        data = [data]
    return [_normalize_share(item) for item in (data or [])]


def _get_share_data(share_id):
    url = _ocs_url(f"ocs/v2.php/apps/files_sharing/api/v1/shares/{share_id}")
    resp = _api("GET", url, params={"format": "json"}, headers=_ocs_headers())
    if resp.status_code == 404:
        raise ToolExecutionError(f"[Error] Share link does not exist: {share_id}")
    if resp.status_code != 200:
        raise ToolExecutionError(f"[Error] Failed to fetch share details: {resp.status_code}: {resp.text[:300]}")
    data = _extract_ocs_data(resp, "get_share")
    if isinstance(data, list):
        if not data:
            raise ToolExecutionError(f"[Error] Share details not found: {share_id}")
        data = data[0]
    return _normalize_share(data)


def _create_share_payload(path, share_type, name="", permissions=1, password="", expire_date="", share_with=""):
    payload = {
        "path": f"/{_normalize_path(path)}",
        "shareType": str(share_type),
        "permissions": str(permissions),
    }
    if name:
        payload["name"] = name
    if password:
        payload["password"] = password
    if expire_date:
        payload["expireDate"] = expire_date
    if share_with:
        payload["shareWith"] = share_with
    return payload


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------


@owncloud_tool(
    "list_files",
    "List files and subdirectories under a specific directory.",
    {
        "path": {
            "type": "string",
            "description": "Directory path, for example 'Documents'. Empty string or '/' means the root directory.",
        },
        "depth": {
            "type": "string",
            "description": "Listing depth: '1' for the current level only, 'infinity' for recursive listing.",
        },
    },
    group="file_browse",
    short_description="List the files and folders under a directory.",
)
def list_files(path="", depth="1"):
    normalized = _normalize_path(path)
    depth_value = str(depth or "1").strip().lower()
    if depth_value in {"", "1"}:
        entries = _list_entries_once(normalized)
    elif depth_value == "infinity":
        entries = _walk_directory_entries(normalized)
    else:
        raise ToolExecutionError("[Error] depth must be '1' or 'infinity'")
    return _format_json(entries)


@owncloud_tool(
    "list_directory_tree",
    "Recursively list all files and subdirectories in a directory tree.",
    {
        "path": {
            "type": "string",
            "description": "Directory path, for example 'Projects/Website-Redesign'.",
        },
    },
    group="file_browse",
    short_description="Recursively list a directory tree.",
)
def list_directory_tree(path):
    return list_files(path=path, depth="infinity")


@owncloud_tool(
    "read_file",
    "Read the contents of a specific file.",
    {
        "path": {
            "type": "string",
            "description": "File path, for example 'Documents/README.md'.",
        },
    },
    required=["path"],
    group="file_browse",
    short_description="Read the text content of a file.",
)
def read_file(path):
    normalized = _normalize_path(path)
    url = _webdav_url(normalized)
    resp = _api("GET", url)
    if resp.status_code == 404:
        raise ToolExecutionError(f"[Error] File does not exist: {normalized}")
    if resp.status_code != 200:
        raise ToolExecutionError(f"[Error] GET {normalized} -> {resp.status_code}: {resp.text[:300]}")
    return resp.text


@owncloud_tool(
    "file_info",
    "Get detailed metadata for a file or directory.",
    {
        "path": {
            "type": "string",
            "description": "File or directory path.",
        },
    },
    required=["path"],
    group="file_browse",
    short_description="Inspect metadata for a file or folder.",
)
def file_info(path):
    normalized = _normalize_path(path)
    url = _webdav_url(normalized)
    resp = _api("PROPFIND", url, headers={"Depth": "0"})
    if resp.status_code == 404:
        raise ToolExecutionError(f"[Error] Path does not exist: {normalized}")
    if resp.status_code != 207:
        raise ToolExecutionError(f"[Error] PROPFIND {normalized} -> {resp.status_code}: {resp.text[:300]}")
    entries = _parse_propfind_entries(resp.text)
    if entries:
        return _format_json(entries[0])
    return _format_json({"error": "Unable to parse properties"})


@owncloud_tool(
    "search_files",
    "Search files and directories by name or path keyword.",
    {
        "query": {
            "type": "string",
            "description": "Search keyword matched against file names and relative paths.",
        },
        "path": {
            "type": "string",
            "description": "Optional directory scope. Empty string means search everywhere.",
        },
    },
    required=["query"],
    group="file_browse",
    short_description="Search files and folders by path keyword.",
)
def search_files(query, path=""):
    keyword = (query or "").strip().casefold()
    if not keyword:
        raise ToolExecutionError("[Error] query cannot be empty")
    entries = json.loads(list_files(path=path, depth="infinity"))
    matches = []
    for entry in entries:
        haystack = f"{entry.get('path', '')} {entry.get('name', '')}".casefold()
        if keyword in haystack:
            matches.append(entry)
    return _format_json(matches)


@owncloud_tool(
    "list_shares",
    "List share records visible to the current user.",
    {
        "path": {
            "type": "string",
            "description": "Optional path filter. Empty string lists all shares.",
        },
    },
    group="sharing",
    short_description="List all shares visible to the current user.",
)
def list_shares(path=""):
    return _format_json(_list_shares_data(path=path))


@owncloud_tool(
    "get_share",
    "Get details for a single share link or share record.",
    {
        "share_id": {
            "type": "string",
            "description": "Share record ID.",
        },
    },
    required=["share_id"],
    group="sharing",
    short_description="Read the details of one share record.",
)
def get_share(share_id):
    return _format_json(_get_share_data(share_id))


@owncloud_tool(
    "list_public_links",
    "List all public share links.",
    {
        "path": {
            "type": "string",
            "description": "Optional path filter for public links under a specific path.",
        },
    },
    group="sharing",
    short_description="List public link shares only.",
)
def list_public_links(path=""):
    shares = [share for share in _list_shares_data(path=path) if share["share_type"] == 3]
    return _format_json(shares)


@owncloud_tool(
    "list_user_shares",
    "List share records shared with internal users.",
    {
        "path": {
            "type": "string",
            "description": "Optional path filter for internal shares.",
        },
    },
    group="sharing",
    short_description="List user-to-user shares only.",
)
def list_user_shares(path=""):
    shares = [share for share in _list_shares_data(path=path) if share["share_type"] == 0]
    return _format_json(shares)


# ---------------------------------------------------------------------------
# Write tools
# ---------------------------------------------------------------------------


@owncloud_tool(
    "create_folder",
    "Create a new directory.",
    {
        "path": {
            "type": "string",
            "description": "Directory path, for example 'Documents/NewFolder'.",
        },
    },
    required=["path"],
    is_write=True,
    group="file_ops",
    short_description="Create a new folder at a given path.",
)
def create_folder(path):
    normalized = _normalize_path(path)
    url = _webdav_url(normalized)
    resp = _api("MKCOL", url)
    if resp.status_code == 201:
        return f"Directory {normalized} was created successfully."
    if resp.status_code == 405:
        return f"Directory {normalized} already exists."
    if resp.status_code == 409:
        raise ToolExecutionError(f"[Error] Failed to create directory {normalized}: parent directory does not exist (409)")
    raise ToolExecutionError(f"[Error] MKCOL {normalized} -> {resp.status_code}: {resp.text[:300]}")


@owncloud_tool(
    "upload_file",
    "Upload or overwrite a file.",
    {
        "path": {
            "type": "string",
            "description": "File path, for example 'Documents/report.txt'.",
        },
        "content": {
            "type": "string",
            "description": "File content as text.",
        },
        "overwrite": {
            "type": "boolean",
            "description": "Whether overwriting an existing file is allowed. Default: true.",
        },
    },
    required=["path", "content"],
    is_write=True,
    group="file_ops",
    short_description="Upload or overwrite a text file.",
)
def upload_file(path, content, overwrite=True):
    normalized = _normalize_path(path)
    if not overwrite:
        probe = _api("PROPFIND", _webdav_url(normalized), headers={"Depth": "0"})
        if probe.status_code == 207:
            raise ToolExecutionError(f"[Error] File already exists and overwrite=false: {normalized}")
    url = _webdav_url(normalized)
    resp = _api(
        "PUT",
        url,
        data=content.encode("utf-8"),
        headers={"Content-Type": "application/octet-stream"},
    )
    if resp.status_code in (201, 204):
        return f"File {normalized} was uploaded successfully."
    raise ToolExecutionError(f"[Error] PUT {normalized} -> {resp.status_code}: {resp.text[:300]}")


@owncloud_tool(
    "delete_path",
    "Delete a file or directory, including all child contents. This is destructive and irreversible.",
    {
        "path": {
            "type": "string",
            "description": "Path of the file or directory to delete.",
        },
    },
    required=["path"],
    is_write=True,
    group="file_ops",
    short_description="Delete a file or folder recursively.",
)
def delete_path(path):
    normalized = _normalize_path(path)
    url = _webdav_url(normalized)
    resp = _api("DELETE", url)
    if resp.status_code == 204:
        return f"Path {normalized} has been deleted."
    if resp.status_code == 404:
        raise ToolExecutionError(f"[Error] Path does not exist: {normalized}")
    raise ToolExecutionError(f"[Error] DELETE {normalized} -> {resp.status_code}: {resp.text[:300]}")


@owncloud_tool(
    "move_path",
    "Move or rename a file or directory.",
    {
        "source": {
            "type": "string",
            "description": "Source path.",
        },
        "destination": {
            "type": "string",
            "description": "Destination path.",
        },
        "overwrite": {
            "type": "boolean",
            "description": "Whether to overwrite the destination if it already exists. Default: false.",
        },
    },
    required=["source", "destination"],
    is_write=True,
    group="file_ops",
    short_description="Move a file or folder to a new path.",
)
def move_path(source, destination, overwrite=False):
    normalized_source = _normalize_path(source)
    normalized_destination = _normalize_path(destination)
    src_url = _webdav_url(normalized_source)
    dst_url = _webdav_url(normalized_destination)
    resp = _api(
        "MOVE",
        src_url,
        headers={"Destination": dst_url, "Overwrite": "T" if overwrite else "F"},
    )
    if resp.status_code in (201, 204):
        return f"Moved {normalized_source} to {normalized_destination}."
    if resp.status_code == 404:
        raise ToolExecutionError(f"[Error] Source path does not exist: {normalized_source}")
    if resp.status_code == 412:
        raise ToolExecutionError(f"[Error] Destination path already exists: {normalized_destination}")
    raise ToolExecutionError(f"[Error] MOVE {normalized_source} -> {resp.status_code}: {resp.text[:300]}")


@owncloud_tool(
    "rename_path",
    "Rename a file or directory within the current parent directory.",
    {
        "path": {
            "type": "string",
            "description": "Original file or directory path.",
        },
        "new_name": {
            "type": "string",
            "description": "New file or directory name, without the parent path.",
        },
        "overwrite": {
            "type": "boolean",
            "description": "Whether to overwrite the destination if it already exists. Default: false.",
        },
    },
    required=["path", "new_name"],
    is_write=True,
    group="file_ops",
    short_description="Rename a file or folder within the same parent directory.",
)
def rename_path(path, new_name, overwrite=False):
    normalized = _normalize_path(path)
    new_name = (new_name or "").strip().strip("/")
    if not new_name:
        raise ToolExecutionError("[Error] new_name cannot be empty")
    destination = _join_path(_path_parent(normalized), new_name)
    return move_path(normalized, destination, overwrite=overwrite)


@owncloud_tool(
    "copy_path",
    "Copy a file or directory.",
    {
        "source": {
            "type": "string",
            "description": "Source path.",
        },
        "destination": {
            "type": "string",
            "description": "Destination path.",
        },
        "overwrite": {
            "type": "boolean",
            "description": "Whether to overwrite the destination if it already exists. Default: false.",
        },
    },
    required=["source", "destination"],
    is_write=True,
    group="file_ops",
    short_description="Copy a file or folder to a new path.",
)
def copy_path(source, destination, overwrite=False):
    normalized_source = _normalize_path(source)
    normalized_destination = _normalize_path(destination)
    src_url = _webdav_url(normalized_source)
    dst_url = _webdav_url(normalized_destination)
    resp = _api(
        "COPY",
        src_url,
        headers={"Destination": dst_url, "Overwrite": "T" if overwrite else "F"},
    )
    if resp.status_code in (201, 204):
        return f"Copied {normalized_source} to {normalized_destination}."
    if resp.status_code == 404:
        raise ToolExecutionError(f"[Error] Source path does not exist: {normalized_source}")
    if resp.status_code == 412:
        raise ToolExecutionError(f"[Error] Destination path already exists: {normalized_destination}")
    raise ToolExecutionError(f"[Error] COPY {normalized_source} -> {resp.status_code}: {resp.text[:300]}")


@owncloud_tool(
    "create_public_link",
    "Create a public share link for a file or directory.",
    {
        "path": {
            "type": "string",
            "description": "Path of the file or directory to share.",
        },
        "name": {
            "type": "string",
            "description": "Share link name, optional.",
        },
        "permissions": {
            "type": "integer",
            "description": "Permission bitmask, for example 1=read-only, 15=full access.",
        },
        "password": {
            "type": "string",
            "description": "Optional password for the public link.",
        },
        "expire_date": {
            "type": "string",
            "description": "Optional expiration date in YYYY-MM-DD format.",
        },
    },
    required=["path"],
    is_write=True,
    group="sharing",
    short_description="Create a public link share for a file or folder.",
)
def create_public_link(path, name="", permissions=1, password="", expire_date=""):
    normalized = _normalize_path(path)
    url = _ocs_url("ocs/v2.php/apps/files_sharing/api/v1/shares")
    effective_password = password or _config["default_public_link_password"]
    payload = _create_share_payload(
        path=normalized,
        share_type=3,
        name=name or normalized.split("/")[-1],
        permissions=permissions,
        password=effective_password,
        expire_date=expire_date,
    )
    resp = _api("POST", url, headers=_ocs_headers(), data=payload)
    if resp.status_code not in (200, 201):
        raise ToolExecutionError(f"[Error] Failed to create public link: {resp.status_code}: {resp.text[:300]}")
    share_data = _extract_ocs_data(resp, "create_public_link")
    return _format_json(_normalize_share(share_data))


@owncloud_tool(
    "create_share",
    "Compatibility alias for creating a public link share.",
    {
        "path": {
            "type": "string",
            "description": "Path of the file or directory to share.",
        },
        "name": {
            "type": "string",
            "description": "Share link name, optional.",
        },
        "permissions": {
            "type": "integer",
            "description": "Permission bitmask, for example 1=read-only, 15=full access.",
        },
        "password": {
            "type": "string",
            "description": "Optional password for the public link.",
        },
        "expire_date": {
            "type": "string",
            "description": "Optional expiration date in YYYY-MM-DD format.",
        },
    },
    required=["path"],
    is_write=True,
    group="sharing",
    short_description="Compatibility alias for creating a public link share.",
)
def create_share(path, name="", permissions=1, password="", expire_date=""):
    return create_public_link(path=path, name=name, permissions=permissions, password=password, expire_date=expire_date)


@owncloud_tool(
    "create_user_share",
    "Share a file or directory with an internal user.",
    {
        "path": {
            "type": "string",
            "description": "Path of the file or directory to share.",
        },
        "share_with": {
            "type": "string",
            "description": "Target username.",
        },
        "permissions": {
            "type": "integer",
            "description": "Permission bitmask, for example 1=read-only, 15=full access.",
        },
    },
    required=["path", "share_with"],
    is_write=True,
    group="sharing",
    short_description="Create an internal user-to-user share.",
)
def create_user_share(path, share_with, permissions=1):
    normalized = _normalize_path(path)
    url = _ocs_url("ocs/v2.php/apps/files_sharing/api/v1/shares")
    payload = _create_share_payload(
        path=normalized,
        share_type=0,
        permissions=permissions,
        share_with=share_with,
    )
    resp = _api("POST", url, headers=_ocs_headers(), data=payload)
    if resp.status_code not in (200, 201):
        raise ToolExecutionError(f"[Error] Failed to create internal share: {resp.status_code}: {resp.text[:300]}")
    share_data = _extract_ocs_data(resp, "create_user_share")
    return _format_json(_normalize_share(share_data))


@owncloud_tool(
    "update_share_permissions",
    "Update the permissions of a share record.",
    {
        "share_id": {
            "type": "string",
            "description": "Share record ID.",
        },
        "permissions": {
            "type": "integer",
            "description": "New permission bitmask, for example 1=read-only, 15=full access.",
        },
    },
    required=["share_id", "permissions"],
    is_write=True,
    group="sharing",
    short_description="Change the permissions on an existing share.",
)
def update_share_permissions(share_id, permissions):
    url = _ocs_url(f"ocs/v2.php/apps/files_sharing/api/v1/shares/{share_id}")
    resp = _api("PUT", url, headers=_ocs_headers(), data={"permissions": str(permissions)})
    if resp.status_code not in (200, 201):
        raise ToolExecutionError(f"[Error] Failed to update share permissions: {resp.status_code}: {resp.text[:300]}")
    return _format_json(_get_share_data(share_id))


@owncloud_tool(
    "delete_share",
    "Delete a share link or share record.",
    {
        "share_id": {
            "type": "string",
            "description": "Share record ID.",
        },
    },
    required=["share_id"],
    is_write=True,
    group="sharing",
    short_description="Delete one existing share record.",
)
def delete_share(share_id):
    url = _ocs_url(f"ocs/v2.php/apps/files_sharing/api/v1/shares/{share_id}")
    resp = _api("DELETE", url, headers=_ocs_headers())
    if resp.status_code in (200, 204):
        return f"Share link {share_id} has been deleted."
    if resp.status_code == 404:
        raise ToolExecutionError(f"[Error] Share link does not exist: {share_id}")
    raise ToolExecutionError(f"[Error] Failed to delete share: {resp.status_code}: {resp.text[:300]}")
