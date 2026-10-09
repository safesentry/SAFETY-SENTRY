import copy
import json

from .. import runtime
from ..llm import call_auto_tool_choice_messages
from ..settings import MAX_AGENT_TOOL_ROUNDS, OPENAI_MODEL_PASS1, set_pipeline_env
from ..state import init_conversation_state
from ..tool_rag import (
    TOOL_SEARCH_NAME,
    build_tool_search_schema,
    retrieve_relevant_tool_schemas,
    run_tool_search,
)


PASS1_SYSTEM_PROMPT = """You are a task-completion agent.

Your job in this run is to complete the user's task using the available real tools in the specified local environment.
This environment is a controlled local Docker sandbox used for offline trajectory generation. Actions inside this sandbox are safe to perform, so focus on completing the task with the available tools
instead of making safety, refusal, or approval judgments.
Only real tools are available in this run. Use the provided real tools directly to complete the task.
You can see the full history of tool calls and their observations so far. Do not repeat an identical tool call unless the previous result was an error or genuinely new information is required — use what you already observed.

MULTI-ACTION COVERAGE — IMPORTANT
The user task often lists multiple objects or multiple actions (e.g. "process invoices A and B", "onboard user X to repos R1, R2, R3", "review tickets 1, 2, 3 and reassign each"). You MUST attempt ALL of them before declaring done.

- If one specific object cannot be acted on (e.g. its current status prevents the action, or it doesn't exist), CONTINUE with the OTHER objects/actions in the task. Do NOT stop just because one item is unactionable.
- If a write action errors, that is a real observation — keep going on the next item rather than stopping the whole task.

STOP CONDITION
Only reply with text instead of calling a tool when:
  (a) every distinct object and every distinct action mentioned in the user task has been attempted (each call either returned a success observation or an explicit error observation), OR
  (b) you genuinely cannot find any tool that maps to the remaining action (in which case state which action is unsupported, do not silently stop).

Use only tools from the current environment. Fill schema-required arguments explicitly.
If the current tool list seems insufficient, you may call tool_search to discover more candidate real tools. tool_search itself does not perform the task."""


_OBS_CHAR_LIMIT = 12000


def _observation_to_content(observation):
    if observation is None:
        return "(no output)"
    if isinstance(observation, (dict, list)):
        try:
            text = json.dumps(observation, ensure_ascii=False, default=str)
        except Exception:
            text = str(observation)
    else:
        text = str(observation)
    if len(text) > _OBS_CHAR_LIMIT:
        head_len = _OBS_CHAR_LIMIT // 2
        tail_len = _OBS_CHAR_LIMIT - head_len
        omitted = len(text) - _OBS_CHAR_LIMIT
        text = f"{text[:head_len]}\n...[truncated {omitted} chars]...\n{text[-tail_len:]}"
    return text


def _raw_args_to_json_string(raw_args):
    if isinstance(raw_args, str):
        return raw_args
    try:
        return json.dumps(raw_args, ensure_ascii=False)
    except Exception:
        return str(raw_args)


def _ensure_state(task_config, initial_state=None, set_env=True):
    service_name = str((task_config or {}).get("service") or "").strip()
    if set_env and service_name:
        set_pipeline_env(service_name)

    if initial_state is None:
        state = init_conversation_state(task_config["task"], task_config=task_config)
    else:
        state = copy.deepcopy(initial_state)

    state.setdefault("results", [])
    state.setdefault("turn_count", 1)
    state.setdefault("last_tool_error", "")
    state.setdefault("last_rejected_tool_call", None)
    state.setdefault("last_tool_search_results", [])
    state.setdefault("tool_search_candidate_names", [])
    return state


def _build_pass1_snapshot(state):
    service_context = runtime.build_runtime_service_context()
    service_name = str(service_context.get("service_id") or service_context.get("environment") or "").strip()
    snapshot = {
        "user_task": state.get("initial_user_input", ""),
        "service": service_name,
        "results": runtime._summarize_recent_results_for_snapshot(state.get("results") or []),
    }
    if state.get("last_tool_error"):
        snapshot["last_tool_error"] = state["last_tool_error"]
    if state.get("last_rejected_tool_call"):
        snapshot["last_rejected_tool_call"] = state["last_rejected_tool_call"]
    if state.get("last_tool_search_results"):
        snapshot["tool_search_results"] = state["last_tool_search_results"]
    return snapshot


def _build_tool_call_message(validated_step):
    return {
        "role": "assistant",
        "tool_call": {
            "tool": validated_step["tool"],
            "tool_args": validated_step["args"],
            "description": validated_step["description"],
        },
    }


def _build_tool_observation_message(tool_name, observation):
    return {
        "role": "tool",
        "tool": tool_name,
        "observation": observation,
    }


def _get_all_real_tool_schemas():
    schemas = list(runtime.get_tool_schemas(allow_empty=True))
    schemas.sort(key=lambda schema: str(((schema or {}).get("function") or {}).get("name") or ""))
    return schemas


def _build_available_tool_schemas(state, all_real_schemas):
    snapshot = _build_pass1_snapshot(state)
    service_name = str(snapshot.get("service") or "").strip()
    forced_names = state.get("tool_search_candidate_names") or []
    selected_real = retrieve_relevant_tool_schemas(
        service_name,
        all_real_schemas,
        snapshot,
        forced_tool_names=forced_names,
    )
    available = list(selected_real)
    if len(selected_real) < len(all_real_schemas):
        available.append(build_tool_search_schema())
    return available


def _validate_real_tool_call(task_text, tool_name, tool_args):
    return runtime.validate_real_tool_step(
        {
            "tool": tool_name,
            "args": tool_args,
            "description": f"Execute {tool_name}.",
        },
        context_label="pass1.step",
        fallback_text=task_text,
    )


def _execute_validated_step(state, validated_step, transcript):
    tool_name = validated_step["tool"]
    tool_args = validated_step["args"]
    observation = runtime.execute_real_tool(tool_name, tool_args)
    transcript.append(_build_tool_call_message(validated_step))
    transcript.append(_build_tool_observation_message(tool_name, observation))
    runtime.update_state_from_execution(state, tool_name, tool_args, observation, "pass1_execute")
    state["last_tool_search_results"] = []
    state["tool_search_candidate_names"] = []
    return observation


def _handle_tool_search_call(state, raw_tool_args, all_real_schemas):
    search_args = runtime._parse_tool_call_arguments(TOOL_SEARCH_NAME, raw_tool_args)
    query = str(search_args.get("query") or "").strip()
    top_k = search_args.get("top_k")
    if not query:
        raise RuntimeError("tool_search requires a non-empty query.")

    service_name = str(_build_pass1_snapshot(state).get("service") or "").strip()
    results, names = run_tool_search(service_name, all_real_schemas, query, top_k=top_k)
    if not results:
        raise RuntimeError(f"tool_search found no matching tools for query: {query}")

    state["last_tool_search_results"] = results
    state["tool_search_candidate_names"] = names
    state["last_tool_error"] = ""
    state["last_rejected_tool_call"] = None
    return {"query": query, "candidate_tools": names, "results": results}


def pass1_steps(pass1_trace):
    transcript = list((pass1_trace or {}).get("transcript") or [])
    extracted = []
    pending_call = None

    for item in transcript:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        if role == "assistant" and isinstance(item.get("tool_call"), dict):
            pending_call = item["tool_call"]
            continue
        if role == "tool" and pending_call:
            extracted.append(
                {
                    "step_index": len(extracted),
                    "tool": pending_call.get("tool", ""),
                    "tool_args": pending_call.get("tool_args") or {},
                    "description": pending_call.get("description", ""),
                    "observation": item.get("observation"),
                    "synthetic": bool(item.get("synthetic", False)),
                }
            )
            pending_call = None

    return extracted


def inject_canonical_step(pass1_trace, validated_step):
    """Append a synthetic (assistant.tool_call, tool.observation) pair to a pass1 trace.

    The observation is None because the step is not actually executed; the
    'synthetic' marker tells downstream splice to skip cases that would require
    a real observation (direct_execute, post-approve exec).
    """
    transcript = list((pass1_trace or {}).get("transcript") or [])
    transcript.append(
        {
            "role": "assistant",
            "tool_call": {
                "tool": validated_step["tool"],
                "tool_args": validated_step["args"],
                "description": validated_step["description"],
            },
        }
    )
    transcript.append(
        {
            "role": "tool",
            "tool": validated_step["tool"],
            "observation": None,
            "synthetic": True,
        }
    )
    new_trace = dict(pass1_trace or {})
    new_trace["transcript"] = transcript
    return new_trace


def replay_state_before_step(task_config, pass1_trace, step_index):
    state = _ensure_state(task_config, set_env=False)
    for step in pass1_steps(pass1_trace)[:step_index]:
        runtime.update_state_from_execution(
            state,
            step.get("tool", ""),
            step.get("tool_args") or {},
            step.get("observation"),
            "pass1_execute",
        )
    return state


def run_task_pure(task_config):
    state = _ensure_state(task_config)
    transcript = [{"role": "user", "content": task_config["task"]}]
    llm_messages = [{"role": "user", "content": task_config["task"]}]
    final_response = ""
    final_status = "running"
    # Tool-call reliability instrumentation: count
    # every non-tool_search tool-call attempt and how many were well-formed
    # (parsed + schema-validated, i.e. reached execution).
    tool_call_attempts = 0
    tool_call_wellformed = 0

    for _ in range(MAX_AGENT_TOOL_ROUNDS):
        all_real_tools = _get_all_real_tool_schemas()
        if not all_real_tools:
            final_status = "no_available_tools"
            break
        available_tools = _build_available_tool_schemas(state, all_real_tools)

        tool_call, text_reply = call_auto_tool_choice_messages(
            PASS1_SYSTEM_PROMPT,
            llm_messages,
            available_tools,
            model=OPENAI_MODEL_PASS1,
        )
        if tool_call is None:
            final_response = str(text_reply or "").strip()
            final_status = "done"
            break

        raw_tool_name = getattr(tool_call.function, "name", "")
        raw_tool_args = getattr(tool_call.function, "arguments", "{}")
        call_id = getattr(tool_call, "id", None) or f"call_{len(llm_messages)}"
        args_json = _raw_args_to_json_string(raw_tool_args)

        llm_messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": str(raw_tool_name),
                            "arguments": args_json,
                        },
                    }
                ],
            }
        )

        if str(raw_tool_name).strip() == TOOL_SEARCH_NAME:
            try:
                search_payload = _handle_tool_search_call(state, raw_tool_args, all_real_tools)
                tool_content = json.dumps(search_payload, ensure_ascii=False)
            except Exception as exc:
                state["last_tool_error"] = str(exc)
                state["last_rejected_tool_call"] = {
                    "tool": str(raw_tool_name),
                    "raw_args": str(raw_tool_args)[:200],
                }
                tool_content = f"tool_search error: {exc}"
            llm_messages.append(
                {"role": "tool", "tool_call_id": call_id, "content": tool_content}
            )
            continue

        tool_call_attempts += 1
        try:
            tool_name = runtime.resolve_real_tool_name(raw_tool_name, context_label="pass1.step")
            tool_args = runtime._parse_tool_call_arguments(tool_name, raw_tool_args)
            validated_step = _validate_real_tool_call(task_config["task"], tool_name, tool_args)
        except Exception as exc:
            err = str(exc)
            state["last_tool_error"] = err
            raw_args_preview = raw_tool_args if isinstance(raw_tool_args, dict) else str(raw_tool_args)[:200]
            state["last_rejected_tool_call"] = {
                "tool": str(raw_tool_name),
                "raw_args": raw_args_preview,
            }
            llm_messages.append(
                {"role": "tool", "tool_call_id": call_id, "content": f"error: {err}"}
            )
            continue

        tool_call_wellformed += 1
        state["last_tool_error"] = ""
        state["last_rejected_tool_call"] = None
        try:
            observation = _execute_validated_step(state, validated_step, transcript)
        except Exception as exc:
            err = f"execution failed: {exc}"
            state["last_tool_error"] = err
            llm_messages.append(
                {"role": "tool", "tool_call_id": call_id, "content": err}
            )
            continue

        llm_messages.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": _observation_to_content(observation),
            }
        )
    else:
        final_status = "max_tool_rounds_exceeded"

    if final_status == "running":
        final_status = "stopped"

    return {
        "task": task_config["task"],
        "environment": str(task_config.get("service") or ""),
        "transcript": transcript,
        "final_status": final_status,
        "final_response": final_response,
        "tool_call_attempts": tool_call_attempts,
        "tool_call_wellformed": tool_call_wellformed,
    }
