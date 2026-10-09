import json
import os
import time

from .settings import (
    OPENAI_API_KEY,
    OPENAI_BASE_URL,
    OPENAI_EMBEDDING_MODEL,
    OPENAI_MAX_TOKENS,
    OPENAI_MODEL,
)

# Optional provider-specific request extras, supplied as a JSON string via the
# OPENAI_EXTRA_BODY env var. For example, use it to pass
# {"reasoning": {"enabled": false}} for OpenRouter reasoning-mode backbones
# (e.g. deepseek-v4-pro) that otherwise burn the token budget on hidden
# reasoning and return empty content.
try:
    _OPENAI_EXTRA_BODY = json.loads(os.environ.get("OPENAI_EXTRA_BODY") or "{}")
    if not isinstance(_OPENAI_EXTRA_BODY, dict):
        _OPENAI_EXTRA_BODY = {}
except Exception:
    _OPENAI_EXTRA_BODY = {}

try:
    import openai
except ModuleNotFoundError:
    openai = None


client = None


_TRANSIENT_RETRY_DELAYS = (1.0, 2.0, 4.0)


def _is_transient_network_error(exc):
    if openai is None:
        return False
    transient_types = (
        openai.APIConnectionError,
        openai.APITimeoutError,
        openai.InternalServerError,
    )
    return isinstance(exc, transient_types)


def _call_with_retry(fn, **kwargs):
    last_exc = None
    for attempt, delay in enumerate((0.0,) + _TRANSIENT_RETRY_DELAYS):
        if delay:
            time.sleep(delay)
        try:
            return fn(**kwargs)
        except Exception as exc:
            if not _is_transient_network_error(exc):
                raise
            last_exc = exc
    raise RuntimeError(
        "Failed to reach the LLM endpoint "
        f"{_llm_endpoint_label()} after {len(_TRANSIENT_RETRY_DELAYS) + 1} attempts. "
        "This is usually a DNS/network restriction issue in the current runtime environment, "
        "not a task YAML problem."
    ) from last_exc


def _base_request_kwargs(model=None):
    kwargs = {"model": model or OPENAI_MODEL}
    if OPENAI_MAX_TOKENS and OPENAI_MAX_TOKENS > 0:
        kwargs["max_tokens"] = OPENAI_MAX_TOKENS
    if _OPENAI_EXTRA_BODY:
        kwargs["extra_body"] = dict(_OPENAI_EXTRA_BODY)
    return kwargs


def get_openai_client():
    global client
    if client is not None:
        return client
    if openai is None:
        raise RuntimeError("openai is not installed in the current environment, so the pipeline cannot run.")
    kwargs = {"api_key": OPENAI_API_KEY, "timeout": 120.0}
    if OPENAI_BASE_URL:
        kwargs["base_url"] = OPENAI_BASE_URL
    client = openai.OpenAI(**kwargs)
    return client


def _llm_endpoint_label():
    return OPENAI_BASE_URL or "https://api.openai.com/v1"


def _chat_completion_create(**kwargs):
    llm_client = get_openai_client()
    # Some OpenRouter upstreams (notably Azure-hosted GPT with content
    # filtering) return HTTP 200 with an empty/None `choices` array instead
    # of raising. Retry a few times -- OpenRouter may re-route to a healthy
    # provider -- before surfacing a clear error.
    last_resp = None
    for attempt, delay in enumerate((0.0, 1.5, 3.0, 6.0)):
        if delay:
            time.sleep(delay)
        resp = _call_with_retry(llm_client.chat.completions.create, **kwargs)
        if getattr(resp, "choices", None):
            return resp
        last_resp = resp
    err = getattr(last_resp, "error", None)
    raise RuntimeError(
        "LLM endpoint returned a response with no choices after 4 attempts "
        f"({_llm_endpoint_label()}); likely an upstream content filter or "
        f"provider error. error={err!r}"
    )


def _embedding_create(**kwargs):
    llm_client = get_openai_client()
    return _call_with_retry(llm_client.embeddings.create, **kwargs)


def _extract_message_text(message):
    if message is None:
        return ""

    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
                continue
            if isinstance(item, dict):
                text_value = item.get("text")
                if isinstance(text_value, str):
                    parts.append(text_value)
                    continue
                if isinstance(text_value, dict):
                    nested = text_value.get("value")
                    if isinstance(nested, str):
                        parts.append(nested)
                        continue
                for key in ("input_text", "output_text", "value"):
                    nested = item.get(key)
                    if isinstance(nested, str):
                        parts.append(nested)
                        break
                continue
            text_value = getattr(item, "text", None)
            if isinstance(text_value, str):
                parts.append(text_value)
                continue
            if text_value is not None:
                nested = getattr(text_value, "value", None)
                if isinstance(nested, str):
                    parts.append(nested)
                    continue
            value = getattr(item, "value", None)
            if isinstance(value, str):
                parts.append(value)
        return "\n".join(part.strip() for part in parts if str(part).strip()).strip()

    for attr in ("parsed", "refusal"):
        value = getattr(message, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()

    return ""


def _extract_response_text(response):
    if response is None:
        return ""
    output_text = getattr(response, "output_text", None)
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()
    choices = getattr(response, "choices", None) or []
    if not choices:
        return ""
    message = getattr(choices[0], "message", None)
    return _extract_message_text(message)


def _parse_json_response_text(text):
    text = str(text or "").strip()
    if not text:
        return None
    candidates = [text]
    if "```" in text:
        for chunk in text.split("```"):
            chunk = chunk.strip()
            if not chunk:
                continue
            if chunk.lower().startswith("json"):
                chunk = chunk[4:].strip()
            candidates.append(chunk)
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except Exception:
            continue
    return None


_JSON_RETRY_REMINDER = (
    "\n\nSTRICT OUTPUT RULE: Your previous reply was not valid JSON. "
    "Reply with ONE JSON object only. No prose, no preamble, no markdown fences, "
    "no explanation outside the object. If you are uncertain, encode that uncertainty "
    "inside the JSON fields the schema already provides."
)


def call_json(system_prompt, user_payload, *, model=None):
    base_messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_payload},
    ]
    response = _chat_completion_create(
        **_base_request_kwargs(model=model),
        messages=base_messages,
        response_format={"type": "json_object"},
    )
    raw_text = _extract_response_text(response)
    parsed = _parse_json_response_text(raw_text)
    if parsed is not None:
        return parsed

    retry_messages = [
        {"role": "system", "content": system_prompt + _JSON_RETRY_REMINDER},
        {"role": "user", "content": user_payload},
        {"role": "assistant", "content": raw_text or "(empty)"},
        {"role": "user", "content": "Reply again with ONE valid JSON object only."},
    ]
    retry_response = _chat_completion_create(
        **_base_request_kwargs(model=model),
        messages=retry_messages,
        response_format={"type": "json_object"},
    )
    parsed = _parse_json_response_text(_extract_response_text(retry_response))
    if parsed is None:
        raise RuntimeError("The model did not return a valid JSON object.")
    return parsed


def get_text_embeddings(texts, model=None):
    normalized = [str(text or "").strip() for text in (texts or [])]
    if not normalized:
        return []
    response = _embedding_create(
        model=model or OPENAI_EMBEDDING_MODEL,
        input=normalized,
    )
    data = getattr(response, "data", None) or []
    return [list(item.embedding) for item in data]


def get_text_embedding(text, model=None):
    vectors = get_text_embeddings([text], model=model)
    return vectors[0] if vectors else []


def call_auto_tool_choice(system_prompt, snapshot, tools, *, model=None):
    """tool_choice=auto: the model may either call a tool or reply with text.

    Returns (tool_call, None) or (None, text_content).
    """
    response = _chat_completion_create(
        **_base_request_kwargs(model=model),
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(snapshot, ensure_ascii=False, indent=2)},
        ],
        tools=tools,
        tool_choice="auto",
    )
    message = response.choices[0].message
    if message.tool_calls:
        return message.tool_calls[0], None
    return None, (message.content or "").strip()


def call_auto_tool_choice_messages(system_prompt, messages, tools, *, model=None):
    """Same as call_auto_tool_choice but accepts a pre-built messages array
    (OpenAI tool-use shape: user + interleaved assistant.tool_calls + tool).

    Use this when you want the model to see the full execution history rather
    than a summarized snapshot.
    """
    response = _chat_completion_create(
        **_base_request_kwargs(model=model),
        messages=[{"role": "system", "content": system_prompt}, *messages],
        tools=tools,
        tool_choice="auto",
    )
    message = response.choices[0].message
    if message.tool_calls:
        return message.tool_calls[0], None
    return None, (message.content or "").strip()


def call_required_tool_choice(system_prompt, snapshot, tools, *, model=None):
    """tool_choice=required: force the model to emit exactly one tool call.

    Returns (tool_call, message_text) on success, (None, message_text) if the
    model did not produce a tool call (e.g. refused even under required mode).
    """
    response = _chat_completion_create(
        **_base_request_kwargs(model=model),
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(snapshot, ensure_ascii=False, indent=2)},
        ],
        tools=tools,
        tool_choice="required",
    )
    message = response.choices[0].message
    text = (getattr(message, "content", "") or "").strip()
    if message.tool_calls:
        return message.tool_calls[0], text
    return None, text
