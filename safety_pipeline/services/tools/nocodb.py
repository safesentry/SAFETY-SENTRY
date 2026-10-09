"""
NocoDB API tool registration.

This module calls the NocoDB REST API v2 directly over HTTP.
Public interface exposed to environment.py:
  get_all_schemas() -> list
  call_tool(name, args) -> str
  get_tool_names() -> list
"""

import json
import os

from ...exceptions import ToolExecutionError
from ...service_tools import ServiceToolRegistry

try:
    import requests
except ModuleNotFoundError:
    requests = None


_config = {}

_jwt_cache = {"token": None}

_REGISTRY = ServiceToolRegistry(service_id="nocodb")


def refresh_runtime_config():
    new_config = {
        "base_url": os.environ.get("NOCODB_BASE_URL", "http://localhost:8080").rstrip("/"),
        "token": os.environ.get("NOCODB_API_TOKEN", ""),
        "admin_email": os.environ.get("NOCODB_ADMIN_EMAIL", "admin@example.com"),
        "admin_password": os.environ.get("NOCODB_ADMIN_PASSWORD", "Admin123!"),
    }
    if new_config != _config:
        _jwt_cache["token"] = None
    _config.update(new_config)
    return dict(_config)


refresh_runtime_config()


def nocodb_tool(name, description, params, required=None, is_write=False, group="", short_description=""):
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
        raise ToolExecutionError("requests is not installed, so the NocoDB API cannot be called. Run: pip install requests")


def _get_auth_token():
    config = refresh_runtime_config()
    if config["token"]:
        return config["token"]
    if _jwt_cache["token"]:
        return _jwt_cache["token"]
    _require_requests()
    resp = requests.post(
        f"{config['base_url']}/api/v1/auth/user/signin",
        json={"email": config["admin_email"], "password": config["admin_password"]},
        timeout=30,
    )
    if resp.status_code != 200:
        raise ToolExecutionError(f"NocoDB login failed ({resp.status_code}): {resp.text[:300]}")
    token = resp.json().get("token", "")
    if not token:
        raise ToolExecutionError("NocoDB login response did not contain a token")
    _jwt_cache["token"] = token
    return token


def _headers():
    token = _get_auth_token()
    config = refresh_runtime_config()
    headers = {"Content-Type": "application/json"}
    if config["token"]:
        headers["xc-token"] = token
    else:
        headers["xc-auth"] = token
    return headers


def _api(method, path, **kwargs):
    _require_requests()
    config = refresh_runtime_config()
    url = f"{config['base_url']}/{path.lstrip('/')}"
    try:
        return requests.request(method, url, headers=_headers(), timeout=30, **kwargs)
    except requests.RequestException as exc:
        raise ToolExecutionError(f"[NocoDB Request Failed] {type(exc).__name__}: {exc}") from exc


def _api_json(method, path, **kwargs):
    resp = _api(method, path, **kwargs)
    if resp.status_code >= 400:
        raise ToolExecutionError(f"[NocoDB API Error] {resp.status_code}: {resp.text[:500]}")
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


def _looks_like_nocodb_id(value):
    value = str(value or "")
    return len(value) > 10 and value.isalnum()


def _get_default_workspace_id():
    data = _api_json("GET", "api/v2/meta/workspaces/")
    ws_list = data.get("list", []) if isinstance(data, dict) else []
    if not ws_list:
        raise ToolExecutionError("[Error] No workspace was found")
    return ws_list[0]["id"]


def _list_workspace_bases(limit=100):
    ws_id = _get_default_workspace_id()
    data = _api_json("GET", f"api/v2/meta/workspaces/{ws_id}/bases/", params={"limit": limit})
    return data.get("list", []) if isinstance(data, dict) else []


def _list_base_tables(base_id):
    data = _api_json("GET", f"api/v2/meta/bases/{base_id}/tables")
    return data.get("list", []) if isinstance(data, dict) else []


def _resolve_base_id(base_id_or_name):
    if not base_id_or_name:
        raise ToolExecutionError("[Error] base_id cannot be empty")
    if _looks_like_nocodb_id(base_id_or_name):
        return base_id_or_name
    for base in _list_workspace_bases():
        if base.get("title", "").lower() == str(base_id_or_name).lower():
            return base["id"]
        if base.get("id") == base_id_or_name:
            return base["id"]
    raise ToolExecutionError(f"[Error] Database not found: {base_id_or_name}")


def _resolve_table_id(table_id_or_name, base_id_or_name=""):
    if not table_id_or_name:
        raise ToolExecutionError("[Error] table_id cannot be empty")
    if _looks_like_nocodb_id(table_id_or_name):
        return table_id_or_name
    if not base_id_or_name:
        raise ToolExecutionError("[Error] base_id must be provided when table_id is a name")
    resolved_base_id = _resolve_base_id(base_id_or_name)
    for table in _list_base_tables(resolved_base_id):
        if table.get("title", "").lower() == str(table_id_or_name).lower():
            return table["id"]
        if table.get("id") == table_id_or_name:
            return table["id"]
    raise ToolExecutionError(f"[Error] Table not found in database {base_id_or_name}: {table_id_or_name}")


def _get_base_data(base_id_or_name):
    resolved_id = _resolve_base_id(base_id_or_name)
    for base in _list_workspace_bases():
        if base.get("id") == resolved_id:
            return base
    raise ToolExecutionError(f"[Error] Database not found: {base_id_or_name}")


def _get_table_data(table_id_or_name, base_id_or_name=""):
    resolved_id = _resolve_table_id(table_id_or_name, base_id_or_name)
    data = _api_json("GET", f"api/v2/meta/tables/{resolved_id}")
    return data if isinstance(data, dict) else {}


def _normalize_columns(columns):
    results = []
    for column in columns or []:
        results.append(
            {
                "id": column.get("id", ""),
                "title": column.get("title", ""),
                "uidt": column.get("uidt", ""),
                "pk": bool(column.get("pk")),
                "required": bool(column.get("rqd")),
            }
        )
    return results


def _list_records_data(table_id, base_id="", where="", sort="", limit=25, offset=0):
    resolved_id = _resolve_table_id(table_id, base_id)
    params = {"limit": limit, "offset": offset}
    if where:
        params["where"] = where
    if sort:
        params["sort"] = sort
    data = _api_json("GET", f"api/v2/tables/{resolved_id}/records", params=params)
    records = data.get("list", []) if isinstance(data, dict) else []
    page_info = data.get("pageInfo", {}) if isinstance(data, dict) else {}
    return {
        "records": records,
        "totalRows": page_info.get("totalRows", len(records)),
    }


def _build_where_eq(field_name, field_value):
    if not field_name:
        raise ToolExecutionError("[Error] field_name cannot be empty")
    value = str(field_value)
    return f"({field_name},eq,{value})"


def _find_records_data(table_id, field_name, field_value, base_id="", limit=10):
    return _list_records_data(
        table_id=table_id,
        base_id=base_id,
        where=_build_where_eq(field_name, field_value),
        limit=limit,
        offset=0,
    )


# ---------------------------------------------------------------------------
# Schema tools
# ---------------------------------------------------------------------------


@nocodb_tool(
    "list_bases",
    "List all databases (bases) in NocoDB.",
    {
        "per_page": {
            "type": "integer",
            "description": "Results per page. Default: 20.",
        },
    },
    group="schema",
    short_description="List all bases in the current workspace.",
)
def list_bases(per_page=20):
    results = []
    for base in _list_workspace_bases(limit=per_page):
        results.append(
            {
                "id": base.get("id", ""),
                "title": base.get("title", ""),
                "description": base.get("description", ""),
            }
        )
    return _format_json(results)


@nocodb_tool(
    "get_base",
    "Get details for a specific database (base).",
    {
        "base_id": {
            "type": "string",
            "description": "Database ID or name.",
        },
    },
    required=["base_id"],
    group="schema",
    short_description="Read metadata for one base.",
)
def get_base(base_id):
    base = _get_base_data(base_id)
    return _format_json(
        {
            "id": base.get("id", ""),
            "title": base.get("title", ""),
            "description": base.get("description", ""),
            "workspace_id": base.get("fk_workspace_id", ""),
        }
    )


@nocodb_tool(
    "create_base",
    "Create a new database (base).",
    {
        "name": {
            "type": "string",
            "description": "New database name.",
        },
        "description": {
            "type": "string",
            "description": "Optional database description.",
        },
    },
    required=["name"],
    is_write=True,
    group="schema",
    short_description="Create a new base in the default workspace.",
)
def create_base(name, description=""):
    ws_id = _get_default_workspace_id()
    data = _api_json(
        "POST",
        f"api/v2/meta/workspaces/{ws_id}/bases/",
        json={"title": name, "description": description},
    )
    return _format_json(
        {
            "id": data.get("id", ""),
            "title": data.get("title", ""),
            "description": data.get("description", ""),
        }
    )


@nocodb_tool(
    "list_tables",
    "List all tables in a specific database.",
    {
        "base_id": {
            "type": "string",
            "description": "Database (base) ID or name.",
        },
    },
    required=["base_id"],
    group="schema",
    short_description="List all tables inside one base.",
)
def list_tables(base_id):
    resolved_id = _resolve_base_id(base_id)
    results = []
    for table in _list_base_tables(resolved_id):
        results.append(
            {
                "id": table.get("id", ""),
                "title": table.get("title", ""),
                "meta": table.get("meta", {}),
            }
        )
    return _format_json(results)


@nocodb_tool(
    "get_table",
    "Get details for a specific table, including column definitions.",
    {
        "table_id": {
            "type": "string",
            "description": "Table ID or name. If a name is used, provide base_id too.",
        },
        "base_id": {
            "type": "string",
            "description": "Database ID or name. Required when table_id is a name.",
        },
    },
    required=["table_id"],
    group="schema",
    short_description="Read table metadata and column definitions.",
)
def get_table(table_id, base_id=""):
    data = _get_table_data(table_id, base_id)
    return _format_json(
        {
            "id": data.get("id", ""),
            "title": data.get("title", ""),
            "columns": _normalize_columns(data.get("columns") or []),
        }
    )


@nocodb_tool(
    "list_columns",
    "List column definitions for a specific table.",
    {
        "table_id": {
            "type": "string",
            "description": "Table ID or name. If a name is used, provide base_id too.",
        },
        "base_id": {
            "type": "string",
            "description": "Database ID or name. Required when table_id is a name.",
        },
    },
    required=["table_id"],
    group="schema",
    short_description="List columns for a table.",
)
def list_columns(table_id, base_id=""):
    data = _get_table_data(table_id, base_id)
    return _format_json(_normalize_columns(data.get("columns") or []))


@nocodb_tool(
    "create_table",
    "Create a new table in a specific database.",
    {
        "base_id": {
            "type": "string",
            "description": "Database ID or name.",
        },
        "table_name": {
            "type": "string",
            "description": "New table name.",
        },
        "columns": {
            "type": "array",
            "description": "Array of column definitions, for example [{\"name\": \"TaskCode\", \"uidt\": \"SingleLineText\"}].",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "uidt": {"type": "string"},
                },
                "required": ["name", "uidt"],
            },
        },
    },
    required=["base_id", "table_name", "columns"],
    is_write=True,
    group="schema",
    short_description="Create a new table with column definitions.",
)
def create_table(base_id, table_name, columns):
    resolved_base_id = _resolve_base_id(base_id)
    table_columns = [
        {"column_name": column["name"], "title": column["name"], "uidt": column["uidt"]}
        for column in columns
    ]
    data = _api_json(
        "POST",
        f"api/v2/meta/bases/{resolved_base_id}/tables",
        json={"table_name": table_name, "title": table_name, "columns": table_columns},
    )
    return _format_json(
        {
            "id": data.get("id", ""),
            "title": data.get("title", ""),
            "columns_count": len(columns),
        }
    )


# ---------------------------------------------------------------------------
# Record tools
# ---------------------------------------------------------------------------


@nocodb_tool(
    "list_records",
    "List records in a specific table.",
    {
        "table_id": {
            "type": "string",
            "description": "Table ID or name.",
        },
        "base_id": {
            "type": "string",
            "description": "Database ID or name. Required when table_id is a name.",
        },
        "where": {
            "type": "string",
            "description": "Filter condition using NocoDB where syntax, for example '(Status,eq,active)'.",
        },
        "sort": {
            "type": "string",
            "description": "Sort field, for example '-Salary' or 'FullName'.",
        },
        "limit": {
            "type": "integer",
            "description": "Maximum number of results to return. Default: 25.",
        },
        "offset": {
            "type": "integer",
            "description": "Offset. Default: 0.",
        },
    },
    required=["table_id"],
    group="records",
    short_description="List records from a table, optionally filtered.",
)
def list_records(table_id, base_id="", where="", sort="", limit=25, offset=0):
    return _format_json(_list_records_data(table_id, base_id=base_id, where=where, sort=sort, limit=limit, offset=offset))


@nocodb_tool(
    "query_records",
    "Query records in a specific table with a filter condition.",
    {
        "table_id": {
            "type": "string",
            "description": "Table ID or name.",
        },
        "where": {
            "type": "string",
            "description": "Filter condition using NocoDB where syntax, for example '(ProjectCode,eq,PRJ-DATA)'.",
        },
        "base_id": {
            "type": "string",
            "description": "Database ID or name. Required when table_id is a name.",
        },
        "sort": {
            "type": "string",
            "description": "Sort field, for example '-Priority'.",
        },
        "limit": {
            "type": "integer",
            "description": "Maximum number of results to return. Default: 25.",
        },
        "offset": {
            "type": "integer",
            "description": "Offset. Default: 0.",
        },
    },
    required=["table_id", "where"],
    group="views_queries",
    short_description="Query records with an explicit where clause.",
)
def query_records(table_id, where, base_id="", sort="", limit=25, offset=0):
    return _format_json(_list_records_data(table_id, base_id=base_id, where=where, sort=sort, limit=limit, offset=offset))


@nocodb_tool(
    "find_records",
    "Find records that match a specific field value.",
    {
        "table_id": {
            "type": "string",
            "description": "Table ID or name.",
        },
        "field_name": {
            "type": "string",
            "description": "Field name to match, for example 'ProjectCode'.",
        },
        "field_value": {
            "type": "string",
            "description": "Field value to match.",
        },
        "base_id": {
            "type": "string",
            "description": "Database ID or name. Required when table_id is a name.",
        },
        "limit": {
            "type": "integer",
            "description": "Maximum number of results to return. Default: 10.",
        },
    },
    required=["table_id", "field_name", "field_value"],
    group="views_queries",
    short_description="Find records by one business field value.",
)
def find_records(table_id, field_name, field_value, base_id="", limit=10):
    return _format_json(_find_records_data(table_id, field_name, field_value, base_id=base_id, limit=limit))


@nocodb_tool(
    "get_record",
    "Get a single record from a specific table.",
    {
        "table_id": {
            "type": "string",
            "description": "Table ID or name.",
        },
        "record_id": {
            "type": "string",
            "description": "Record ID (row ID).",
        },
        "base_id": {
            "type": "string",
            "description": "Database ID or name. Required when table_id is a name.",
        },
    },
    required=["table_id", "record_id"],
    group="records",
    short_description="Read one record by row id.",
)
def get_record(table_id, record_id, base_id=""):
    resolved_id = _resolve_table_id(table_id, base_id)
    data = _api_json("GET", f"api/v2/tables/{resolved_id}/records/{record_id}")
    return _format_json(data)


@nocodb_tool(
    "create_record",
    "Create a new record in a specific table.",
    {
        "table_id": {
            "type": "string",
            "description": "Table ID or name.",
        },
        "base_id": {
            "type": "string",
            "description": "Database ID or name. Required when table_id is a name.",
        },
        "fields": {
            "type": "object",
            "description": "Field key/value pairs for the record, for example {\"TaskCode\": \"TASK-001\", \"Status\": \"todo\"}.",
        },
    },
    required=["table_id", "fields"],
    is_write=True,
    group="records",
    short_description="Create one record in a table.",
)
def create_record(table_id, fields, base_id=""):
    resolved_id = _resolve_table_id(table_id, base_id)
    data = _api_json("POST", f"api/v2/tables/{resolved_id}/records", json=fields)
    return _format_json(data)


@nocodb_tool(
    "update_record",
    "Update a single record in a specific table.",
    {
        "table_id": {
            "type": "string",
            "description": "Table ID or name.",
        },
        "record_id": {
            "type": "string",
            "description": "Record ID (row ID).",
        },
        "base_id": {
            "type": "string",
            "description": "Database ID or name. Required when table_id is a name.",
        },
        "fields": {
            "type": "object",
            "description": "Field key/value pairs to update.",
        },
    },
    required=["table_id", "record_id", "fields"],
    is_write=True,
    group="records",
    short_description="Update one record by row id.",
)
def update_record(table_id, record_id, fields, base_id=""):
    resolved_id = _resolve_table_id(table_id, base_id)
    payload = dict(fields or {})
    payload["Id"] = record_id
    data = _api_json("PATCH", f"api/v2/tables/{resolved_id}/records", json=payload)
    return _format_json(data)


@nocodb_tool(
    "update_record_by_field",
    "Match a record by business field and update it.",
    {
        "table_id": {
            "type": "string",
            "description": "Table ID or name.",
        },
        "match_field": {
            "type": "string",
            "description": "Field name used to locate the record, for example 'ProjectCode'.",
        },
        "match_value": {
            "type": "string",
            "description": "Field value used to locate the record.",
        },
        "fields": {
            "type": "object",
            "description": "Field key/value pairs to update.",
        },
        "base_id": {
            "type": "string",
            "description": "Database ID or name. Required when table_id is a name.",
        },
        "require_unique": {
            "type": "boolean",
            "description": "Whether the match must be unique. Default: true.",
        },
    },
    required=["table_id", "match_field", "match_value", "fields"],
    is_write=True,
    group="records",
    short_description="Find a record by business key and update it.",
)
def update_record_by_field(table_id, match_field, match_value, fields, base_id="", require_unique=True):
    matched = _find_records_data(table_id, match_field, match_value, base_id=base_id, limit=10)
    records = matched.get("records", [])
    if not records:
        raise ToolExecutionError(f"[Error] No matching record found: {match_field}={match_value}")
    if require_unique and len(records) != 1:
        raise ToolExecutionError(f"[Error] Matched {len(records)} records, cannot update uniquely: {match_field}={match_value}")
    record = records[0]
    record_id = record.get("Id") or record.get("id")
    if record_id in (None, ""):
        raise ToolExecutionError("[Error] Matched record is missing the Id field and cannot be updated")
    return update_record(table_id=table_id, record_id=str(record_id), fields=fields, base_id=base_id)


@nocodb_tool(
    "delete_record",
    "Delete a single record from a specific table. This is destructive and irreversible.",
    {
        "table_id": {
            "type": "string",
            "description": "Table ID or name.",
        },
        "record_id": {
            "type": "string",
            "description": "Record ID (row ID).",
        },
        "base_id": {
            "type": "string",
            "description": "Database ID or name. Required when table_id is a name.",
        },
    },
    required=["table_id", "record_id"],
    is_write=True,
    group="records",
    short_description="Delete one record by row id.",
)
def delete_record(table_id, record_id, base_id=""):
    resolved_id = _resolve_table_id(table_id, base_id)
    _api_json("DELETE", f"api/v2/tables/{resolved_id}/records", json=[{"Id": record_id}])
    return f"Record {record_id} has been deleted."


# ---------------------------------------------------------------------------
# Bulk tools
# ---------------------------------------------------------------------------


@nocodb_tool(
    "bulk_delete_records",
    "Bulk delete multiple records from a specific table. This is highly destructive and irreversible.",
    {
        "table_id": {
            "type": "string",
            "description": "Table ID or name.",
        },
        "record_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "List of record IDs to delete.",
        },
        "base_id": {
            "type": "string",
            "description": "Database ID or name. Required when table_id is a name.",
        },
    },
    required=["table_id", "record_ids"],
    is_write=True,
    group="bulk_ops",
    short_description="Delete multiple records at once.",
)
def bulk_delete_records(table_id, record_ids, base_id=""):
    resolved_id = _resolve_table_id(table_id, base_id)
    payload = [{"Id": record_id} for record_id in record_ids]
    _api_json("DELETE", f"api/v2/tables/{resolved_id}/records", json=payload)
    return f"Deleted {len(record_ids)} record(s) in bulk."


@nocodb_tool(
    "delete_table",
    "Delete an entire table and all of its data. This is extremely dangerous and irreversible.",
    {
        "table_id": {
            "type": "string",
            "description": "Table ID or name.",
        },
        "base_id": {
            "type": "string",
            "description": "Database ID or name. Required when table_id is a name.",
        },
    },
    required=["table_id"],
    is_write=True,
    group="bulk_ops",
    short_description="Delete a whole table and all rows.",
)
def delete_table(table_id, base_id=""):
    resolved_id = _resolve_table_id(table_id, base_id)
    _api_json("DELETE", f"api/v2/meta/tables/{resolved_id}")
    return f"Table {table_id} and all of its data have been permanently deleted."
