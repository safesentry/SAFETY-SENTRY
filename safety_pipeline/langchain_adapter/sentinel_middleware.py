"""LangChain agent middleware that runs the safety sentinel on every tool call.

Why middleware and not ``BaseCallbackHandler``?  In ``langchain==1.x`` the
canonical tool-interception primitive on ``create_agent`` graphs is
``AgentMiddleware.wrap_tool_call(request, handler)``. A middleware can:

  * inspect the tool call before it runs;
  * synthesize a ``ToolMessage`` (e.g. a refusal) **without** invoking the
    tool — much cleaner than raising from a callback;
  * mutate / record per-call metadata in a way the graph can keep going.

The plan doc describes this as "SentinelCallback" — we keep that vocabulary
in the public docstring but ship the actually-correct middleware.

Two run modes:

  - ``experiment`` — when sentinel says ``ask_human`` the tool still runs
    (auto-approve) so we can grade a full trajectory; the decision is
    appended to ``decisions_log`` for offline analysis. When sentinel says
    ``refuse`` we short-circuit with a synthetic ``ToolMessage`` carrying
    the refusal so the agent can react and stop.
  - ``production`` — both ``ask_human`` and ``refuse`` short-circuit; the
    consumer is expected to either inspect the final state and route the
    question to a human, or treat the refusal as terminal.

The middleware also raises ``ToolBlocked`` / ``AskHumanInterrupt`` if the
caller sets ``raise_on_block=True`` — useful when a synchronous script
wants the failure to bubble all the way up instead of being absorbed by
the graph as a tool observation.
"""

from __future__ import annotations

from typing import Any, Callable, Literal, Optional

from langchain_core.messages import ToolMessage

# ``AgentMiddleware`` and ``ToolCallRequest`` live in langchain (not core) and
# require langgraph at runtime; both ship together with ``langchain==1.x``.
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ToolCallRequest

from ..sentinel import review
from ..state import summarize_trace_value
from ..tool_rag import TOOL_SEARCH_NAME


class ToolBlocked(Exception):
    """Raised (only when ``raise_on_block=True``) when sentinel returns refuse."""

    def __init__(self, reason: str, decision: dict):
        super().__init__(reason)
        self.reason = reason
        self.decision = decision


class AskHumanInterrupt(Exception):
    """Raised (only in production mode with ``raise_on_block=True``) when
    sentinel returns ask_human."""

    def __init__(self, question: str, decision: dict):
        super().__init__(question)
        self.question = question
        self.decision = decision


_VALID_MODES = {"experiment", "production"}


class SentinelMiddleware(AgentMiddleware):
    """Intercepts every tool call and routes it through ``sentinel.review``.

    Args:
        task: The user task text. Pass2 needs this verbatim — don't trim.
        user_memory: Optional list of memory strings describing the user.
            ``None`` and ``[]`` are equivalent (baseline / no persona).
        service: Service name (``mailu``, ``gitea``, …). Used by pass2's
            prompt and downstream analytics. Empty string is allowed but
            degrades pass2 quality.
        mode: ``"experiment"`` (default) or ``"production"``. See module
            docstring for the behavior split.
        raise_on_block: When True, refusals (and ask_human in production)
            raise ``ToolBlocked`` / ``AskHumanInterrupt`` instead of
            synthesizing a ``ToolMessage``. Default False.
        max_prior_steps: How many recent (tool, args, observation) triples
            to feed pass2. Default 5 — matches pass2_reviewer's window.

    Attributes:
        decisions_log: Append-only list of ``{tool, tool_args, decision}``
            dicts, one per intercepted call. Useful for post-hoc metrics.
        prior_steps: Append-only list of executed steps with truncated
            observations. Fed into pass2 on the next call.
    """

    def __init__(
        self,
        task: str,
        user_memory: Optional[list[str]] = None,
        service: str = "",
        mode: Literal["experiment", "production"] = "experiment",
        *,
        raise_on_block: bool = False,
        max_prior_steps: int = 5,
        sentinel_backend: Optional[str] = None,
        observation_char_limit: int = 500,
        bypass_tool_names: Optional[set[str]] = None,
    ) -> None:
        super().__init__()
        if mode not in _VALID_MODES:
            raise ValueError(f"mode must be one of {sorted(_VALID_MODES)}, got {mode!r}")
        self.task = task or ""
        self.user_memory = list(user_memory or [])
        self.service = service or ""
        self.mode = mode
        self.raise_on_block = raise_on_block
        self.max_prior_steps = max(1, int(max_prior_steps))
        self.sentinel_backend = sentinel_backend
        self.observation_char_limit = max(80, int(observation_char_limit))
        # ``tool_search`` is a meta-tool: it doesn't touch external state, so
        # the sentinel should not gate it. Callers can pass additional names
        # (e.g. internal helpers) to bypass review.
        default_bypass = {TOOL_SEARCH_NAME}
        self.bypass_tool_names: set[str] = default_bypass | set(bypass_tool_names or set())

        self.decisions_log: list[dict] = []
        self.prior_steps: list[dict] = []

    # ---- AgentMiddleware hook ----

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        tool_call = request.tool_call or {}
        tool_name = str(tool_call.get("name") or "")
        tool_args = self._coerce_args(tool_call.get("args"))
        tool_call_id = str(tool_call.get("id") or "")

        # Meta-tools (e.g. tool_search) are not business actions — they don't
        # touch the service backend — so we pass them straight through without
        # invoking pass2 and without recording them as prior_steps. They DO
        # appear in decisions_log as a bypass entry for traceability.
        if tool_name in self.bypass_tool_names:
            self.decisions_log.append(
                {
                    "tool": tool_name,
                    "tool_args": tool_args,
                    "decision": {"label": "bypass", "reasoning": "meta-tool, not gated"},
                }
            )
            return handler(request)

        decision = review(
            task=self.task,
            prior_steps=self.prior_steps[-self.max_prior_steps:],
            proposed_tool_call={"tool": tool_name, "tool_args": tool_args},
            user_memory=self.user_memory or None,
            service=self.service,
            backend=self.sentinel_backend,
        )

        record = {
            "tool": tool_name,
            "tool_args": tool_args,
            "decision": decision,
        }
        self.decisions_log.append(record)
        label = decision.get("label")

        if label == "refuse":
            return self._handle_refuse(decision, tool_name, tool_call_id)

        if label == "ask_human" and self.mode == "production":
            return self._handle_ask_human_production(decision, tool_name, tool_call_id)

        # ``ask_human`` in experiment mode and ``direct_execute`` both fall
        # through to actual execution; we still record the observation so
        # subsequent prior_steps reflect what really happened.
        tool_message = handler(request)
        self._record_observation(tool_name, tool_args, tool_message)
        return tool_message

    # ---- helpers ----

    @staticmethod
    def _coerce_args(args: Any) -> dict:
        if isinstance(args, dict):
            return dict(args)
        if args is None:
            return {}
        # ``args`` is normalized to dict by langgraph's tool node, but be
        # defensive in case a custom node passes the raw string form.
        if isinstance(args, str):
            import json

            try:
                parsed = json.loads(args)
                return parsed if isinstance(parsed, dict) else {"_raw": args}
            except json.JSONDecodeError:
                return {"_raw": args}
        return {"_raw": str(args)}

    def _truncate(self, text: Any) -> str:
        s = text if isinstance(text, str) else str(text)
        limit = self.observation_char_limit
        if len(s) <= limit:
            return s
        return s[:limit] + f"...[truncated {len(s) - limit} chars]"

    def _record_observation(
        self,
        tool_name: str,
        tool_args: dict,
        tool_message: Any,
    ) -> None:
        if isinstance(tool_message, ToolMessage):
            observation = tool_message.content
        else:
            observation = tool_message
        self.prior_steps.append(
            {
                "tool": tool_name,
                "tool_args": tool_args,
                "observation": summarize_trace_value(self._truncate(observation)),
            }
        )

    def _handle_refuse(self, decision: dict, tool_name: str, tool_call_id: str) -> ToolMessage:
        reason = decision.get("reason") or decision.get("reasoning") or "Refused by sentinel."
        if self.raise_on_block:
            raise ToolBlocked(reason=reason, decision=decision)
        # Synthesize a ToolMessage so the graph can keep going; the agent's
        # next model turn will see the refusal as the tool observation and
        # can decide whether to retry, ask the user, or stop.
        content = (
            f"<sentinel_refuse>{reason}</sentinel_refuse>\n"
            "The safety reviewer blocked this tool call. Do not retry the same call. "
            "Either abandon this step or explain the situation to the user."
        )
        # We do NOT append to prior_steps for blocked calls — pass2 should
        # see the refused step only as part of `decisions_log`, not as if it
        # had been executed.
        return ToolMessage(
            content=content,
            tool_call_id=tool_call_id,
            name=tool_name,
            status="error",
        )

    def _handle_ask_human_production(
        self,
        decision: dict,
        tool_name: str,
        tool_call_id: str,
    ) -> ToolMessage:
        question = decision.get("question") or "Sentinel needs human input before this call proceeds."
        if self.raise_on_block:
            raise AskHumanInterrupt(question=question, decision=decision)
        content = (
            f"<sentinel_ask_human>{question}</sentinel_ask_human>\n"
            "The safety reviewer requires confirmation before this tool call can proceed."
        )
        return ToolMessage(
            content=content,
            tool_call_id=tool_call_id,
            name=tool_name,
            status="error",
        )
