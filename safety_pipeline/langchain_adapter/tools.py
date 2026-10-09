"""Convert safety_pipeline service tools into LangChain ``StructuredTool``s.

We reuse the existing per-service registry (``safety_pipeline/services/tools/<svc>.py``,
each exposes ``get_all_schemas()`` and ``call_tool(name, args)``) and only
add a thin adapter that:

  1. converts the OpenAI-style JSON schema into a runtime-built Pydantic v2
     model for ``args_schema``;
  2. wraps ``call_tool`` so the result is returned as a string (LangChain's
     ``ToolMessage.content`` expects a string).

We deliberately do **not** route through ``runtime.execute_real_tool`` /
``validate_real_tool_step``; those carry text-parsing heuristics from the
old synthesis loop. LangChain's tool-calling already produces structured
args, so we feed them straight into ``call_tool``.
"""

from __future__ import annotations

import importlib
import json
from typing import Any, Optional

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field, create_model

from ..settings import set_pipeline_env


_JSON_TO_PY = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _json_type_to_python(spec: dict) -> Any:
    """Map a JSON-schema property spec to a Python type for Pydantic.

    Generics like ``list[str]`` and ``dict[str, Any]`` are not narrowed —
    LangChain's tool calling pipes through JSON either way and Pydantic
    just needs a permissive container type.
    """
    if not isinstance(spec, dict):
        return Any
    json_type = spec.get("type")
    if isinstance(json_type, list):
        # union types — pick the first non-null member.
        for member in json_type:
            if member != "null":
                json_type = member
                break
        else:
            json_type = "string"
    return _JSON_TO_PY.get(json_type, Any)


def _sanitize_model_name(tool_name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in tool_name)
    if not cleaned or not cleaned[0].isalpha():
        cleaned = f"Tool_{cleaned}"
    return f"{cleaned.title()}Args"


def _build_pydantic_args(tool_name: str, parameters: dict) -> type[BaseModel]:
    properties = (parameters or {}).get("properties") or {}
    required = set((parameters or {}).get("required") or [])

    fields: dict[str, tuple] = {}
    for name, spec in properties.items():
        py_type = _json_type_to_python(spec if isinstance(spec, dict) else {})
        description = ""
        if isinstance(spec, dict):
            description = str(spec.get("description") or "")
        if name in required:
            fields[name] = (py_type, Field(..., description=description))
        else:
            # ``Optional[py_type]`` so the model is allowed to omit it.
            fields[name] = (Optional[py_type], Field(default=None, description=description))

    if not fields:
        # An empty pydantic model is fine — LangChain still treats the tool as
        # callable with no args.
        return create_model(_sanitize_model_name(tool_name), __base__=BaseModel)
    return create_model(_sanitize_model_name(tool_name), **fields)


def _stringify_result(result: Any) -> str:
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception:
        return str(result)


def _make_runner(call_tool, tool_name: str):
    def runner(**kwargs):
        # LangChain occasionally passes None for optional slots; drop them so
        # the underlying handler binds defaults / treats them as absent.
        cleaned = {k: v for k, v in kwargs.items() if v is not None}
        try:
            return _stringify_result(call_tool(tool_name, cleaned))
        except Exception as exc:
            # Convert real-service errors (404 from a backend, validation
            # failures, network blips) into observable strings rather than
            # exceptions. Otherwise langgraph's tool node default error
            # handler re-raises and kills the whole agent loop, even with
            # ``handle_tool_error=True`` on the StructuredTool.
            return f"<tool_error>{type(exc).__name__}: {exc}</tool_error>"

    runner.__name__ = f"call_{tool_name}"
    return runner


def _load_service_module(service: str):
    """Import ``safety_pipeline.services.tools.<service>`` and refresh its
    runtime config so env-var changes (post ``set_pipeline_env``) take effect.
    """
    module = importlib.import_module(f"safety_pipeline.services.tools.{service}")
    refresh = getattr(module, "refresh_runtime_config", None)
    if callable(refresh):
        refresh()
    return module


def build_langchain_tools(service: str) -> list[StructuredTool]:
    """Return a list of LangChain ``StructuredTool`` for ``service``.

    Side effect: calls ``set_pipeline_env(service)`` so the rest of the
    pipeline runtime (validators, prior-step summarizers) sees the same
    service.
    """
    set_pipeline_env(service)
    module = _load_service_module(service)
    schemas = module.get_all_schemas()
    call_tool = module.call_tool

    tools: list[StructuredTool] = []
    for schema in schemas:
        fn = (schema or {}).get("function") or {}
        name = fn.get("name") or ""
        if not name:
            continue
        description = fn.get("description") or f"Execute {name} on the {service} service."
        parameters = fn.get("parameters") or {"type": "object", "properties": {}}
        args_schema = _build_pydantic_args(name, parameters)
        runner = _make_runner(call_tool, name)

        tools.append(
            StructuredTool.from_function(
                func=runner,
                name=name,
                description=description,
                args_schema=args_schema,
                # call_tool may raise ToolExecutionError; let LangChain surface
                # the error string as the tool's observation rather than aborting
                # the whole graph.
                handle_tool_error=True,
            )
        )
    return tools
