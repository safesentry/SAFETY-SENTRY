"""Sentinel API — unified interface for step-level safety review.

Two backends are supported:

    - ``"llm"``: in-process, calls ``pass2_reviewer.decide_step`` (which itself
      talks to an OpenAI-compatible chat completion endpoint).
    - ``"local"``: HTTP POST to a compatible review endpoint configured with
      ``SENTINEL_LOCAL_URL``.

The public function ``review`` returns a normalized decision dict shaped like::

    {
        "label":     "direct_execute" | "ask_human" | "refuse",
        "reasoning": str,
        "question":  Optional[str],   # only when label == "ask_human"
        "reason":    Optional[str],   # only when label == "refuse"
    }

Selection order for the backend:
    1. explicit ``backend=`` kwarg
    2. ``SENTINEL_BACKEND`` environment variable
    3. default ``"llm"``
"""

from __future__ import annotations

import os
from typing import Any, Literal, Optional

from .synthesis.pass2_reviewer import decide_step as _llm_decide_step


class SentinelError(RuntimeError):
    """Raised when a sentinel backend fails or returns an invalid payload."""


def _normalize(label: str, raw: dict) -> dict:
    label = (label or "").strip().lower()
    return {
        "label": label,
        "reasoning": str(raw.get("reasoning") or "").strip(),
        "question": str(raw.get("question") or "").strip() if label == "ask_human" else None,
        "reason": str(raw.get("reason") or "").strip() if label == "refuse" else None,
    }


def _llm_backend(
    task: str,
    prior_steps: list[dict],
    proposed_tool_call: dict,
    user_memory: Optional[list[str]],
    service: str,
) -> dict:
    step_dict = {
        "tool": proposed_tool_call.get("tool", ""),
        "tool_args": proposed_tool_call.get("tool_args") or {},
        "description": proposed_tool_call.get("description", ""),
    }
    decision = _llm_decide_step(
        step=step_dict,
        prior_steps=prior_steps or [],
        task_text=task or "",
        service=service or "",
        user_memory=user_memory,
    )
    # decide_step uses "result" for the label; flatten to the public shape.
    return _normalize(decision.get("result", ""), decision)


def _local_backend(
    task: str,
    prior_steps: list[dict],
    proposed_tool_call: dict,
    user_memory: Optional[list[str]],
    service: str,
) -> dict:
    try:
        import requests
    except ModuleNotFoundError as exc:
        raise SentinelError("requests is not installed; needed for the local backend") from exc

    endpoint = os.environ.get("SENTINEL_LOCAL_URL", "http://localhost:8001/review")
    payload = {
        "task": task or "",
        "prior_steps": list(prior_steps or []),
        "proposed_tool_call": proposed_tool_call or {},
        "user_memory": list(user_memory or []),
        "service": service or "",
    }
    try:
        resp = requests.post(endpoint, json=payload, timeout=30)
        resp.raise_for_status()
        body = resp.json()
    except Exception as exc:
        raise SentinelError(f"local sentinel HTTP call failed: {exc}") from exc
    label = str(body.get("label") or "").strip().lower()
    if label not in {"direct_execute", "ask_human", "refuse"}:
        raise SentinelError(f"local sentinel returned invalid label: {label!r}")
    return _normalize(label, body)


_BACKENDS = {"llm": _llm_backend, "local": _local_backend}


def review(
    task: str,
    prior_steps: list[dict],
    proposed_tool_call: dict,
    user_memory: Optional[list[str]] = None,
    *,
    service: str = "",
    backend: Optional[Literal["llm", "local"]] = None,
) -> dict[str, Any]:
    """Review a single proposed tool call and return a normalized decision.

    Args:
        task: User task text driving the agent run.
        prior_steps: Already-executed steps for context, shaped as a list of
            ``{tool, tool_args, observation}`` dicts. Only the last 5 are
            meaningful to pass2.
        proposed_tool_call: ``{tool, tool_args, description?}`` for the step
            currently under review.
        user_memory: Optional list of natural-language memory items describing
            the requesting user's standing preferences. None or [] = baseline.
        service: Service name (``mailu``, ``gitea``, …). Optional but
            recommended; pass2's prompt benefits from knowing the service.
        backend: ``"llm"`` (default) or ``"local"``. When None, reads the
            ``SENTINEL_BACKEND`` env var, falling back to ``"llm"``.

    Returns:
        Normalized decision dict — see module docstring.
    """
    chosen = backend or os.environ.get("SENTINEL_BACKEND") or "llm"
    if chosen not in _BACKENDS:
        raise SentinelError(
            f"unknown sentinel backend: {chosen!r}; expected one of {list(_BACKENDS)}"
        )
    try:
        return _BACKENDS[chosen](task, prior_steps, proposed_tool_call, user_memory, service)
    except SentinelError:
        raise
    except Exception as exc:
        raise SentinelError(f"sentinel backend {chosen!r} failed: {exc}") from exc
