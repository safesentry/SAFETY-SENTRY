import hashlib
import json
import math
import os
import re

from .llm import get_text_embedding, get_text_embeddings
from .settings import OPENAI_EMBEDDING_MODEL, TOOL_RAG_CACHE_DIR, TOOL_RAG_TOP_K, TOOL_SEARCH_TOP_K


TOOL_SEARCH_NAME = "tool_search"
_INDEX_CACHE = {}
_ACTION_TAGS = {
    "read",
    "share",
    "rename_move",
    "delete",
    "create",
    "update",
    "permission",
    "message",
}
_QUERY_TAG_KEYWORDS = {
    "read": {
        "list", "read", "show", "view", "check", "inspect", "find", "search", "lookup", "look", "locate",
        "review", "browse", "audit", "info", "details",
    },
    "share": {
        "share", "sharing", "grant", "access", "public", "link", "invite",
    },
    "rename_move": {
        "rename", "renaming", "move", "moving", "copy", "copied", "relocate",
    },
    "delete": {
        "delete", "remove", "revoke", "destroy", "purge", "drop",
    },
    "create": {
        "create", "new", "add", "upload", "setup",
    },
    "update": {
        "update", "change", "edit", "modify", "set", "reset", "close", "reopen", "cancel", "archive",
    },
    "permission": {
        "permission", "permissions", "role", "roles", "admin", "reviewer", "quota", "member", "collaborator",
        "owner", "protect", "protection",
    },
    "message": {
        "message", "reply", "comment", "post", "announcement", "announce", "email", "mail", "dm",
    },
}
_TOOL_TAG_OVERRIDES = {
    "create_public_link": {"share"},
    "create_share": {"share"},
    "create_user_share": {"share"},
    "delete_share": {"share", "delete"},
    "get_share": {"share", "read"},
    "list_shares": {"share", "read"},
    "list_user_shares": {"share", "read"},
    "update_share_permissions": {"share", "permission", "update"},
}


def _safe_int(value, default):
    try:
        return int(value)
    except Exception:
        return int(default)


def _clamp_top_k(value, default, hard_limit=20):
    return max(1, min(hard_limit, _safe_int(value, default)))


def _tokenize(text):
    return [token for token in re.findall(r"[a-z0-9_]+", str(text or "").lower()) if token]


def _schema_function(schema):
    return (schema or {}).get("function") or {}


def _schema_name(schema):
    return str(_schema_function(schema).get("name") or "").strip()


def _schema_description(schema):
    return str(_schema_function(schema).get("description") or "").strip()


def _schema_parameters(schema):
    params = ((_schema_function(schema).get("parameters") or {}).get("properties") or {})
    if isinstance(params, dict):
        return params
    return {}


def _tool_document(schema):
    name = _schema_name(schema)
    description = _schema_description(schema)
    parameter_bits = []
    for key, param_schema in sorted(_schema_parameters(schema).items()):
        parameter_bits.append(
            f"{key}: {str((param_schema or {}).get('description') or '').strip()}"
        )
    snake_alias = name.replace("_", " ")
    return "\n".join(
        part
        for part in [
            f"name: {name}",
            f"name_alias: {snake_alias}",
            f"description: {description}",
            "parameters: " + "; ".join(parameter_bits) if parameter_bits else "",
        ]
        if part
    )


def _infer_tool_tags(schema):
    name = _schema_name(schema)
    if not name:
        return {"read"}
    if name in _TOOL_TAG_OVERRIDES:
        return set(_TOOL_TAG_OVERRIDES[name])

    tags = set()
    name_tokens = set(_tokenize(name))
    desc_tokens = set(_tokenize(_schema_description(schema)))
    tokens = name_tokens | desc_tokens

    if name.startswith(("list_", "get_", "find_", "search_", "read_")) or name in {"file_info"}:
        tags.add("read")
    if name.startswith(("create_", "add_", "upload_")):
        tags.add("create")
    if name.startswith(("update_", "change_", "edit_", "set_", "reset_", "close_", "reopen_", "cancel_", "archive_", "unarchive_", "discontinue_")):
        tags.add("update")
    if name.startswith(("rename_", "move_", "copy_")):
        tags.add("rename_move")
    if name.startswith(("delete_", "remove_", "revoke_")):
        tags.add("delete")

    if tokens & {"share", "shares", "public_link", "public", "link"}:
        tags.add("share")
    if tokens & {"permission", "permissions", "quota", "role", "roles", "member", "members", "collaborator", "collaborators", "reviewer", "reviewers", "protection", "protect"}:
        tags.add("permission")
    if tokens & {"message", "messages", "reply", "comment", "post", "posts", "email", "mail", "topic", "announcement", "integration"}:
        tags.add("message")

    if not tags:
        tags.add("read")
    return tags & _ACTION_TAGS


def _infer_query_tags(query_text):
    tokens = set(_tokenize(query_text))
    tags = set()
    for tag, keywords in _QUERY_TAG_KEYWORDS.items():
        if tokens & keywords:
            tags.add(tag)
    return tags


def _allowed_tool_tags_for_query(query_tags):
    if not query_tags:
        return set()
    allowed = set()
    if "read" in query_tags:
        allowed.add("read")
    if "share" in query_tags:
        allowed.update({"share", "permission", "read"})
    if "rename_move" in query_tags:
        allowed.update({"rename_move", "read"})
    if "delete" in query_tags:
        allowed.update({"delete", "read"})
    if "create" in query_tags:
        allowed.update({"create", "read", "permission", "share"})
    if "update" in query_tags:
        allowed.update({"update", "read", "permission", "share"})
    if "permission" in query_tags:
        allowed.update({"permission", "share", "read", "update", "create"})
    if "message" in query_tags:
        allowed.update({"message", "read", "create", "update"})
    return allowed or set(_ACTION_TAGS)


def _tool_tags_conflict_with_query(tool_tags, query_tags):
    if not query_tags:
        return False
    if "share" in tool_tags and "share" not in query_tags and "permission" not in query_tags:
        return True
    return False


def _tool_index_fingerprint(service_name, schemas, model_name):
    payload = {
        "service": service_name,
        "model": model_name,
        "tools": [
            {
                "name": _schema_name(schema),
                "document": _tool_document(schema),
                "tags": sorted(_infer_tool_tags(schema)),
            }
            for schema in sorted(schemas, key=_schema_name)
        ],
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _cache_path(service_name, model_name):
    os.makedirs(TOOL_RAG_CACHE_DIR, exist_ok=True)
    model_slug = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(model_name or "embedding"))
    service_slug = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(service_name or "service"))
    return os.path.join(TOOL_RAG_CACHE_DIR, f"{service_slug}.{model_slug}.json")


def _lexical_score(query_text, schema):
    query = str(query_text or "").strip().lower()
    if not query:
        return 0.0

    name = _schema_name(schema).lower()
    description = _schema_description(schema).lower()
    parameters = _schema_parameters(schema)
    score = 0.0

    if query == name:
        score += 10.0
    if query in name:
        score += 6.0
    if query in description:
        score += 4.0

    query_tokens = set(_tokenize(query))
    if not query_tokens:
        return score

    name_tokens = set(_tokenize(name))
    desc_tokens = set(_tokenize(description))
    param_name_tokens = set()
    param_desc_tokens = set()
    for key, schema_item in parameters.items():
        param_name_tokens.update(_tokenize(key))
        param_desc_tokens.update(_tokenize((schema_item or {}).get("description", "")))

    score += 2.0 * len(query_tokens & name_tokens)
    score += 1.0 * len(query_tokens & desc_tokens)
    score += 0.75 * len(query_tokens & param_name_tokens)
    score += 0.5 * len(query_tokens & param_desc_tokens)
    return score


def _cosine_similarity(vector_a, vector_b):
    if not vector_a or not vector_b or len(vector_a) != len(vector_b):
        return -1.0
    dot = sum(a * b for a, b in zip(vector_a, vector_b))
    norm_a = math.sqrt(sum(a * a for a in vector_a))
    norm_b = math.sqrt(sum(b * b for b in vector_b))
    if norm_a == 0.0 or norm_b == 0.0:
        return -1.0
    return dot / (norm_a * norm_b)


def _query_text_from_snapshot(snapshot):
    snapshot = snapshot or {}
    parts = []

    service = str(snapshot.get("service") or "").strip()
    user_task = str(snapshot.get("user_task") or "").strip()
    if service:
        parts.append(f"service: {service}")
    if user_task:
        parts.append(f"task: {user_task}")

    for item in snapshot.get("results") or []:
        if not isinstance(item, dict):
            continue
        tool_name = str(item.get("tool") or "").strip()
        result_summary = str(item.get("result_summary") or "").strip()
        if tool_name:
            parts.append(f"recent_tool: {tool_name}")
        if result_summary:
            parts.append(f"recent_result: {result_summary}")

    if snapshot.get("last_tool_error"):
        parts.append(f"last_tool_error: {snapshot['last_tool_error']}")

    rejected = snapshot.get("last_rejected_tool_call") or {}
    if isinstance(rejected, dict):
        rejected_name = str(rejected.get("tool") or "").strip()
        if rejected_name:
            parts.append(f"rejected_tool_name: {rejected_name}")
        raw_args = str(rejected.get("raw_args") or "").strip()
        if raw_args:
            parts.append(f"rejected_tool_args: {raw_args}")

    for item in snapshot.get("tool_search_results") or []:
        if not isinstance(item, dict):
            continue
        tool_name = str(item.get("tool") or "").strip()
        if tool_name:
            parts.append(f"search_candidate: {tool_name}")

    return "\n".join(parts)


def _load_or_build_index(service_name, schemas, embedding_model=None):
    model_name = str(embedding_model or OPENAI_EMBEDDING_MODEL or "").strip()
    cache_key = (
        str(service_name or "").strip(),
        model_name,
        _tool_index_fingerprint(service_name, schemas, model_name),
    )
    cached = _INDEX_CACHE.get(cache_key)
    if cached is not None:
        return cached

    path = _cache_path(service_name, model_name)
    records = []
    fingerprint = cache_key[2]

    if os.path.isfile(path):
        try:
            payload = json.loads(open(path, "r", encoding="utf-8").read())
            if (
                isinstance(payload, dict)
                and payload.get("fingerprint") == fingerprint
                and isinstance(payload.get("records"), list)
            ):
                records = payload["records"]
        except Exception:
            records = []

    schema_map = {_schema_name(schema): schema for schema in schemas}
    if records:
        hydrated = []
        for item in records:
            name = str((item or {}).get("tool") or "").strip()
            schema = schema_map.get(name)
            if not schema:
                continue
            hydrated.append(
                {
                    "tool": name,
                    "schema": schema,
                    "document": str((item or {}).get("document") or ""),
                    "embedding": list((item or {}).get("embedding") or []),
                    "tags": set((item or {}).get("tags") or []),
                }
            )
        if len(hydrated) == len(schema_map):
            _INDEX_CACHE[cache_key] = hydrated
            return hydrated

    fresh_records = []
    documents = [_tool_document(schema) for schema in schemas]
    embeddings = []
    try:
        embeddings = get_text_embeddings(documents, model=model_name)
    except Exception:
        embeddings = []

    for index, schema in enumerate(schemas):
        fresh_records.append(
            {
                "tool": _schema_name(schema),
                "schema": schema,
                "document": documents[index],
                "embedding": embeddings[index] if index < len(embeddings) else [],
                "tags": _infer_tool_tags(schema),
            }
        )

    try:
        serializable = {
            "service": service_name,
            "model": model_name,
            "fingerprint": fingerprint,
            "records": [
                {
                    "tool": item["tool"],
                    "document": item["document"],
                    "embedding": item["embedding"],
                    "tags": sorted(item["tags"]),
                }
                for item in fresh_records
            ],
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(serializable, fh, ensure_ascii=False)
    except Exception:
        pass

    _INDEX_CACHE[cache_key] = fresh_records
    return fresh_records


def build_tool_search_schema():
    return {
        "type": "function",
        "function": {
            "name": TOOL_SEARCH_NAME,
            "description": (
                "Search the current service's real tools by name, description, and parameter hints. "
                "Use this only when the currently offered tool list seems insufficient. "
                "tool_search does not perform the task; it only returns more candidate real tools."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "A short description of the action or missing tool you need.",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "How many candidate tools to return. Defaults to 8 and is capped at 10.",
                    },
                },
                "required": ["query"],
            },
        },
    }


def retrieve_relevant_tool_schemas(service_name, schemas, snapshot, top_k=None, forced_tool_names=None):
    schemas = list(schemas or [])
    if not schemas:
        return []

    top_k = _clamp_top_k(top_k or TOOL_RAG_TOP_K, TOOL_RAG_TOP_K, hard_limit=max(1, len(schemas)))
    forced = {str(name).strip() for name in (forced_tool_names or []) if str(name).strip()}

    index_records = _load_or_build_index(service_name, schemas)
    query_text = _query_text_from_snapshot(snapshot)
    query_tags = _infer_query_tags(query_text)
    allowed_tags = _allowed_tool_tags_for_query(query_tags)
    query_embedding = []
    if query_text:
        try:
            query_embedding = get_text_embedding(query_text)
        except Exception:
            query_embedding = []

    scored = []
    for item in index_records:
        schema = item["schema"]
        tool_tags = set(item.get("tags") or [])
        if _tool_tags_conflict_with_query(tool_tags, query_tags) and item["tool"] not in forced:
            continue
        score = _lexical_score(query_text, schema)
        if query_embedding and item.get("embedding"):
            score += 5.0 * max(0.0, _cosine_similarity(query_embedding, item["embedding"]))
        if query_tags:
            if tool_tags & query_tags:
                score += 4.0 * len(tool_tags & query_tags)
            elif tool_tags & allowed_tags:
                score += 1.0 * len(tool_tags & allowed_tags)
            elif item["tool"] not in forced:
                continue
        if item["tool"] in forced:
            score += 1000.0
        scored.append((score, item["tool"], schema))

    scored.sort(key=lambda row: (-row[0], row[1]))
    selected = []
    selected_names = set()
    for _, tool_name, schema in scored:
        if tool_name in selected_names:
            continue
        selected.append(schema)
        selected_names.add(tool_name)
        if len(selected) >= top_k and forced.issubset(selected_names):
            break

    selected.sort(key=_schema_name)
    return selected


def run_tool_search(service_name, schemas, query, top_k=None):
    schemas = list(schemas or [])
    if not schemas:
        return [], []

    query = str(query or "").strip()
    if not query:
        return [], []

    top_k = _clamp_top_k(top_k or TOOL_SEARCH_TOP_K, TOOL_SEARCH_TOP_K, hard_limit=min(10, len(schemas)))
    query_tags = _infer_query_tags(query)
    allowed_tags = _allowed_tool_tags_for_query(query_tags)
    scored = []
    for schema in schemas:
        tool_tags = _infer_tool_tags(schema)
        if _tool_tags_conflict_with_query(tool_tags, query_tags):
            continue
        if query_tags and not (tool_tags & query_tags) and not (tool_tags & allowed_tags):
            continue
        score = _lexical_score(query, schema)
        if query_tags and tool_tags & query_tags:
            score += 4.0 * len(tool_tags & query_tags)
        elif query_tags and tool_tags & allowed_tags:
            score += 1.0 * len(tool_tags & allowed_tags)
        if score <= 0.0:
            continue
        scored.append((score, _schema_name(schema), schema))

    scored.sort(key=lambda row: (-row[0], row[1]))
    chosen = scored[:top_k]
    names = [name for _, name, _ in chosen]
    results = [
        {
            "tool": name,
            "description": _schema_description(schema),
        }
        for _, name, schema in chosen
    ]
    return results, names
