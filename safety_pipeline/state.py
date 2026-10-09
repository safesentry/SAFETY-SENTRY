import json
from .settings import MAX_DIALOGUE_SUMMARY_CHARS


def init_conversation_state(initial_user_input, task_config=None):
    return {
        "initial_user_input": initial_user_input,
        "results": [],
        "turn_count": 1,
        "last_tool_error": "",
    }

def truncate_text(text, limit=MAX_DIALOGUE_SUMMARY_CHARS):
    text = str(text)
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def summarize_execution_result(tool_name, args, result):
    summary = f"{tool_name}({json.dumps(args, ensure_ascii=False, sort_keys=True)}) -> {result}"
    return truncate_text(summary)

def summarize_trace_value(value):
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [summarize_trace_value(item) for item in value[:3]]
    if isinstance(value, dict):
        summary = {}
        for key in list(value.keys())[:8]:
            summary[key] = summarize_trace_value(value[key])
        return summary
    return str(value)

def normalize_risk_assessment_payload(payload):
    payload = payload or {}
    if not isinstance(payload, dict):
        return {}
    result = str(payload.get("result") or payload.get("level") or payload.get("risk") or "").strip()
    reasoning = str(payload.get("reasoning") or payload.get("reason") or "").strip()

    normalized = {}
    if result:
        normalized["result"] = result
    if reasoning:
        normalized["reasoning"] = reasoning
    return normalized


def compact_risk_record(risk_assessment):
    normalized = normalize_risk_assessment_payload(risk_assessment)
    if not normalized:
        return {}
    return {
        "level": normalized.get("result", ""),
        "reason": normalized.get("reasoning", ""),
    }


def update_state_from_execution(state, tool_name, args, result, method):
    state["results"].append({"tool": tool_name, "args": args, "result": result, "method": method})


def summarize_result_text(value, limit=220):
    summarized = summarize_trace_value(value)
    if isinstance(summarized, str):
        text = summarized
    else:
        text = json.dumps(summarized, ensure_ascii=False)
    return truncate_text(text, limit)
