"""LangChain port of pass1's tool retrieval flow.

Why this exists: the naive ``create_agent(tools=ALL_SERVICE_TOOLS)`` path
ships every single tool's schema in the prompt on every turn. For services
with 40+ tools (gitea, rocketchat, openemr) this both balloons token cost
and degrades tool-choice quality compared to the original pass1, which:

  1. preselected a small candidate set with embedding + lexical RAG, and
  2. exposed a ``tool_search`` meta-tool the model could call to expand
     the candidate set on demand.

This middleware reproduces both behaviors on top of
``langchain.agents.create_agent``:

  - On every model call, ``wrap_model_call`` filters ``request.tools`` down
    to the **active** subset (initially the RAG-preselected top-K plus
    ``tool_search`` itself).
  - The ``tool_search`` LangChain tool, when invoked by the model, runs the
    same ``run_tool_search`` lexical+tag search used by pass1 and **adds
    the hits to the active set**, so the next model call sees them.

The middleware is stateful but per-instance — construct a fresh one per
task run.

Note on RAG side-effects: ``retrieve_relevant_tool_schemas`` calls
``get_text_embedding`` for the query. If the embedding endpoint is
unreachable the function silently falls back to pure lexical ranking
(see ``safety_pipeline/tool_rag.py``). So this middleware degrades gracefully when an
OpenAI embedding key isn't configured.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Optional

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest

from ..settings import TOOL_RAG_TOP_K, TOOL_SEARCH_TOP_K
from ..tool_rag import (
    TOOL_SEARCH_NAME,
    retrieve_relevant_tool_schemas,
    run_tool_search,
)
from .tools import _load_service_module


def _tool_name(tool_or_dict: Any) -> str:
    """Pull the tool name out of either a ``BaseTool`` instance or an
    OpenAI-style ``{"function": {"name": ...}}`` dict (LangChain's
    ``ModelRequest.tools`` accepts both)."""
    if isinstance(tool_or_dict, dict):
        fn = tool_or_dict.get("function") or {}
        return str(fn.get("name") or tool_or_dict.get("name") or "")
    return str(getattr(tool_or_dict, "name", "") or "")


class _ToolSearchArgs(BaseModel):
    query: str = Field(
        ...,
        description=(
            "A short description of the action or missing tool you need. "
            "Example: 'archive a Gitea repository' or 'reset a user's password'."
        ),
    )
    top_k: Optional[int] = Field(
        default=None,
        description="How many candidate tools to return (default 8, capped at 10).",
    )


class ToolRagMiddleware(AgentMiddleware):
    """Reproduces pass1's RAG preselection + ``tool_search`` expansion.

    Args:
        service: Service name (matches ``safety_pipeline.services.tools.<service>``).
        task_text: The user task text — used for the initial RAG query.
        top_k: How many tools to preselect at startup. Defaults to
            ``TOOL_RAG_TOP_K`` (12 by default in settings).
        search_top_k: How many tools ``tool_search`` returns per invocation.
            Defaults to ``TOOL_SEARCH_TOP_K`` (8 by default).
        always_include: Optional set of tool names that bypass filtering
            and stay active for the entire run (e.g. force a known-required
            tool to always be visible).

    Attributes:
        active_names: Names currently visible to the model. Mutated as the
            run progresses (``tool_search`` adds names; the initial set
            comes from RAG preselection).
        search_history: List of ``{"query", "added_names"}`` records, one
            per ``tool_search`` invocation. Useful for offline analysis.
        tool_search_tool: The ``StructuredTool`` to hand to ``create_agent``
            alongside the service tools.
    """

    def __init__(
        self,
        service: str,
        task_text: str,
        *,
        top_k: Optional[int] = None,
        search_top_k: Optional[int] = None,
        always_include: Optional[set[str]] = None,
    ) -> None:
        super().__init__()
        self.service = service
        self.task_text = task_text or ""
        self.top_k = int(top_k or TOOL_RAG_TOP_K)
        self.search_top_k = int(search_top_k or TOOL_SEARCH_TOP_K)
        self.always_include: set[str] = set(always_include or set())

        # Load the service's full schema list once. Used both for the
        # initial RAG preselect and for run_tool_search at request time.
        module = _load_service_module(service)
        self._all_schemas: list[dict] = list(module.get_all_schemas())

        # Initial RAG preselect from the task text. Snapshot mirrors the
        # one pass1 builds, so the same ``retrieve_relevant_tool_schemas``
        # call produces equivalent results.
        snapshot = {"service": service, "user_task": self.task_text}
        selected = retrieve_relevant_tool_schemas(
            service,
            self._all_schemas,
            snapshot,
            top_k=self.top_k,
        )
        self.active_names: set[str] = {
            str(((s or {}).get("function") or {}).get("name") or "").strip()
            for s in selected
        }
        self.active_names.discard("")
        # Always-include names are pinned and never trimmed.
        self.active_names |= self.always_include

        self.search_history: list[dict] = []
        self.tool_search_tool: StructuredTool = self._build_tool_search_tool()

    # ---- AgentMiddleware hook ----

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Any],
    ) -> Any:
        kept = []
        for tool in request.tools:
            name = _tool_name(tool)
            if not name:
                continue
            # ``tool_search`` is always visible; otherwise honor active_names.
            if name == TOOL_SEARCH_NAME or name in self.active_names or name in self.always_include:
                kept.append(tool)
        # No-op if filtering left the set unchanged — avoids an unnecessary
        # override() allocation.
        if len(kept) == len(request.tools):
            return handler(request)
        return handler(request.override(tools=kept))

    # ---- public helper ----

    def expand_active_set(self, names: list[str]) -> list[str]:
        """Add ``names`` to ``active_names``. Returns the newly added subset."""
        added = []
        for raw in names or []:
            name = str(raw or "").strip()
            if not name or name in self.active_names:
                continue
            self.active_names.add(name)
            added.append(name)
        return added

    # ---- internals ----

    def _build_tool_search_tool(self) -> StructuredTool:
        # Closure-bound so the tool's execution mutates THIS middleware's
        # active_names, not some global state.

        def _search(query: str, top_k: Optional[int] = None) -> str:
            results, names = run_tool_search(
                self.service,
                self._all_schemas,
                query,
                top_k=top_k or self.search_top_k,
            )
            added = self.expand_active_set(names)
            self.search_history.append(
                {"query": query, "candidates": list(names), "added": added}
            )
            payload = {"query": query, "candidates": results, "added": added}
            return json.dumps(payload, ensure_ascii=False)

        return StructuredTool.from_function(
            func=_search,
            name=TOOL_SEARCH_NAME,
            description=(
                "Search the current service's real tools by name, description, "
                "and parameter hints. Use this ONLY when the currently offered "
                "tool list seems insufficient. tool_search does not perform "
                "the task — it just adds candidate tools to your available set."
            ),
            args_schema=_ToolSearchArgs,
            handle_tool_error=True,
        )
