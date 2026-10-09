import datetime
import hashlib
import json
import os
import re


def _json_safe(value):
    """Walk a value and replace datetime.date/datetime objects with ISO strings.

    YAML auto-typing turns unquoted 2026-04-25 into datetime.date, which then
    crashes downstream json.dumps in pass2 / session_store / SFT export. We
    normalize at the canonical-step entry point so every later JSON site is
    safe by construction.
    """
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, tuple):
        return [_json_safe(v) for v in value]
    return value

from .console import print_json_block, print_stage_end, print_stage_start
from .decision_tokens import (
    DECISION_TOKEN_SYSTEM_PROMPT,
    build_branch_target_text,
    branch_payload_from_case,
)
from .service_registry import get_service_spec
from .session_store import load_session_records
from .settings import (
    DECISION_TOKEN_SFT_PATH,
    TRACE_SESSION_PATH,
    get_pipeline_env,
)
from .state import (
    summarize_result_text,
    summarize_trace_value,
    update_state_from_execution,
)

try:
    import yaml
except ModuleNotFoundError:
    yaml = None


DECISION_TRACE_FORMAT_VERSION = 3


def get_environment_backend():
    """Get the current environment backend."""
    from .environment import get_backend
    return get_backend(get_pipeline_env())


def get_tool_schemas(allow_empty=False):
    try:
        return get_environment_backend().get_tool_schemas()
    except RuntimeError:
        if allow_empty:
            return []
        raise

def get_real_tool_schema_map():
    tool_map = {}
    for schema in get_tool_schemas():
        func = schema["function"]
        tool_map[func["name"]] = func
    return tool_map


def get_real_tool_schema_bundle_map(allow_empty=False):
    tool_map = {}
    for schema in get_tool_schemas(allow_empty=allow_empty):
        func = schema.get("function") or {}
        name = func.get("name", "")
        if name:
            tool_map[name] = schema
    return tool_map


def build_tool_schema_hint(tool_name):
    if not tool_name:
        return ""
    tool_schema = get_real_tool_schema_map().get(tool_name)
    if not tool_schema:
        return ""
    parameters = tool_schema.get("parameters", {})
    return (
        f"; correct parameter schema for {tool_name}: "
        f"{json.dumps(parameters, ensure_ascii=False)}"
    )


def _parse_tool_call_arguments(tool_name, raw_arguments):
    if raw_arguments in (None, ""):
        return {}
    if isinstance(raw_arguments, dict):
        return raw_arguments
    if not isinstance(raw_arguments, str):
        raise RuntimeError(
            f"Tool call arguments for {tool_name or '[unknown]'} must be a JSON object string, "
            f"got {type(raw_arguments).__name__}."
        )
    try:
        parsed = json.loads(raw_arguments)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Tool call arguments for {tool_name or '[unknown]'} were not valid JSON: "
            f"{exc.msg} at line {exc.lineno} column {exc.colno}."
        ) from exc
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise RuntimeError(
            f"Tool call arguments for {tool_name or '[unknown]'} must decode to a JSON object, "
            f"got {type(parsed).__name__}."
        )
    return parsed


def resolve_real_tool_name(tool_name, context_label="current_step"):
    tool_name = str(tool_name).strip()
    tool_map = get_real_tool_schema_map()
    if tool_name in tool_map:
        return tool_name

    for prefix in ("functions.", "function.", "tools."):
        if tool_name.startswith(prefix):
            stripped = tool_name[len(prefix):].strip()
            if stripped in tool_map:
                return stripped

    if tool_name in FLOW_TOOL_NAMES:
        raise RuntimeError(
            f"{context_label}.tool cannot use a flow tool: {tool_name}. "
            f"If you need to ask a follow-up question, refuse, or make a risk judgment, call the top-level {tool_name} directly instead of putting it inside a real execution step."
        )

    aliases = {
        "delete_files": "delete_file",
        "get_ci_logs": "get_latest_pipeline_log",
        "get_ci_pipeline_logs": "get_latest_pipeline_log",
    }
    mapped = aliases.get(tool_name)
    if mapped and mapped in tool_map:
        return mapped

    raise RuntimeError(f"{context_label}.tool used an unknown real tool: {tool_name}")


def _parse_inline_arg_value(raw_text, schema):
    text = str(raw_text or "").lstrip()
    if not text:
        return None, 0

    decoder = json.JSONDecoder()
    if text[0] in ('{', '[', '"'):
        try:
            value, end = decoder.raw_decode(text)
            return value, end
        except Exception:
            return None, 0

    if text[0] == "'":
        end_idx = text.find("'", 1)
        if end_idx == -1:
            return None, 0
        return text[1:end_idx], end_idx + 1

    match = re.match(r"^[^\uFF0C,\uFF1B;\u3002\)\]\}\s]+", text)
    if not match:
        return None, 0
    raw_value = match.group(0)
    value_type = (schema or {}).get("type", "")

    if value_type == "integer":
        try:
            return int(raw_value), len(raw_value)
        except ValueError:
            return None, 0
    if value_type == "number":
        try:
            return float(raw_value), len(raw_value)
        except ValueError:
            return None, 0
    if value_type == "boolean":
        lowered = raw_value.lower()
        if lowered == "true":
            return True, len(raw_value)
        if lowered == "false":
            return False, len(raw_value)
        return None, 0
    return raw_value, len(raw_value)


def _extract_inline_tool_args(description, properties, existing_args=None):
    description = str(description or "")
    if not description:
        return dict(existing_args or {})

    merged = dict(existing_args or {})
    for key, schema in (properties or {}).items():
        if key in merged:
            continue

        pattern = rf"(?<![A-Za-z0-9_]){re.escape(key)}\s*=\s*"
        match = re.search(pattern, description)
        if match:
            value, consumed = _parse_inline_arg_value(description[match.end():], schema or {})
            if consumed > 0:
                merged[key] = value
                continue

        if key in {"name", "table_name"} and (schema or {}).get("type") == "string":
            named_match = re.search(
                r"(?:named|\u540d\u4e3a)\s*[\"'\u201C\u201D]?([^\"'\u201C\u201D\u3002\uff0c,\uff1b;\s]+)[\"'\u201C\u201D]?",
                description,
                flags=re.IGNORECASE,
            )
            if named_match:
                merged[key] = named_match.group(1)

    return merged


def _infer_project_id_from_text(text):
    if get_pipeline_env() != "gitea":
        return ""
    text = str(text or "")
    if not text:
        return ""

    explicit_patterns = [
        r"(?:repository|project)\s+(?:id|path)?\s*[=:]?\s*[\"'`]?([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)[\"'`]?",
        r"[\"'`]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)[\"'`]\s+repository",
    ]
    for pattern in explicit_patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1)

    repo_name_patterns = [
        r"(?:the\s+)?[\"'`]?([A-Za-z0-9_.-]+)[\"'`]?\s+repository\b",
        r"\brepository\s+(?:named\s+)?[\"'`]?([A-Za-z0-9_.-]+)[\"'`]?\b",
    ]
    for pattern in repo_name_patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        repo_name = match.group(1)
        if not repo_name:
            continue
        owner = str(os.environ.get("GITEA_OWNER", "root") or "root").strip()
        return f"{owner}/{repo_name}" if owner else repo_name

    return ""


def _infer_numeric_id_args_from_text(text, properties, existing_args=None):
    text = str(text or "")
    merged = dict(existing_args or {})
    if not text:
        return merged

    missing_integer_ids = []
    for key, schema in (properties or {}).items():
        if key in merged or key == "project_id" or not key.endswith("_id"):
            continue
        if (schema or {}).get("type") == "integer":
            missing_integer_ids.append(key)

    for key in missing_integer_ids:
        base = key[:-3]
        full_label = r"\s+".join(re.escape(part) for part in base.split("_") if part)
        tail_token = re.escape((base.split("_") or [""])[-1])
        patterns = [
            rf"\b{full_label}\b\s+with\s+id\s+(\d+)\b",
            rf"\b{full_label}\b\s*(?:id|#)\s*(\d+)\b",
            rf"\b\w*{tail_token}\w*\b\s+with\s+id\s+(\d+)\b",
            rf"\b\w*{tail_token}\w*\b\s*(?:id|#)\s*(\d+)\b",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                merged[key] = int(match.group(1))
                break

    unresolved = [key for key in missing_integer_ids if key not in merged]
    if len(unresolved) == 1:
        generic_patterns = [
            r"\bwith\s+id\s+(\d+)\b",
            r"\bid\s*(?:=|:)?\s*(\d+)\b",
            r"#\s*(\d+)\b",
        ]
        for pattern in generic_patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                merged[unresolved[0]] = int(match.group(1))
                break

    return merged


def _normalize_schema_bound_args(source_payload, properties, existing_args=None, parse_text=""):
    merged = dict(existing_args or {})

    if isinstance(source_payload, dict):
        for key in (properties or {}).keys():
            if key in merged:
                continue
            value = source_payload.get(key)
            if value is not None:
                merged[key] = value

    merged = _extract_inline_tool_args(parse_text, properties, existing_args=merged)
    merged = _infer_numeric_id_args_from_text(parse_text, properties, existing_args=merged)

    if "project_id" in (properties or {}) and "project_id" not in merged:
        inferred_project_id = _infer_project_id_from_text(parse_text)
        if inferred_project_id:
            merged["project_id"] = inferred_project_id

    return merged


def validate_real_tool_step(step, context_label="current_step", fallback_text=""):
    if not isinstance(step, dict):
        raise RuntimeError(f"{context_label} must be an object.")

    tool_name = resolve_real_tool_name(step.get("tool", ""), context_label=context_label)
    description = str(step.get("description", "")).strip()
    args = step.get("args", {})
    if not tool_name or not description:
        raise RuntimeError(f"tool and description in {context_label} cannot be empty.")
    if not isinstance(args, dict):
        raise RuntimeError(f"args in {context_label} must be an object.")
    args = _json_safe(args)

    tool_schema = get_real_tool_schema_map().get(tool_name)

    parameters = tool_schema.get("parameters", {})
    properties = parameters.get("properties", {}) or {}
    required = set(parameters.get("required", []) or [])
    parse_text = description
    if fallback_text:
        parse_text = f"{description}\n{fallback_text}".strip()
    args = _normalize_schema_bound_args(step, properties, existing_args=args, parse_text=parse_text)
    arg_keys = set(args.keys())
    unknown_keys = arg_keys - set(properties.keys())
    missing_keys = required - arg_keys
    schema_hint = build_tool_schema_hint(tool_name)
    if unknown_keys:
        raise RuntimeError(
            f"args in {context_label} contain undefined fields: {sorted(unknown_keys)}"
            f"{schema_hint}"
        )
    if missing_keys:
        raise RuntimeError(
            f"args in {context_label} are missing required fields: {sorted(missing_keys)}"
            f"{schema_hint}"
        )

    return {"tool": tool_name, "args": args, "description": description}


# Flow-tool names are reserved labels in synthetic traces. They are not callable
# real tools in pass1 and should not appear inside validated real execution steps.
FLOW_TOOL_NAMES = {
    "ask_human",
    "refuse",
    "direct_tool",
}


def _summarize_recent_results_for_snapshot(results, limit=2):
    summarized = []
    for item in (results or [])[-limit:]:
        summarized.append(
            {
                "tool": item.get("tool", ""),
                "args": item.get("args") or {},
                "method": item.get("method", ""),
                "result_preview": summarize_trace_value(item.get("result")),
                "result_summary": summarize_result_text(
                    item.get("result"),
                    limit=180,
                ),
            }
        )
    return summarized


def build_runtime_service_context():
    env_name = get_pipeline_env()
    spec = get_service_spec(env_name)
    context = {
        "environment": env_name,
        "backend": env_name,
    }
    if spec is not None:
        context.update(
            {
                "service_id": spec.service_id,
                "display_name": spec.display_name,
                "domain": spec.domain,
                "notes": spec.notes,
            }
        )
    return context


# ==================== Tool Execution ====================


def execute_real_tool(tool_name, args):
    return get_environment_backend().execute_tool(tool_name, args)


# ==================== Decision-Token Export ====================


def _session_fingerprint(session_cases):
    normalized = []
    for case in session_cases:
        step = case.get("step") or {}
        normalized.append(
            {
                "task": case.get("task", ""),
                "environment": case.get("environment", ""),
                "step_index": case.get("step_index", 0),
                "decision": case.get("decision", ""),
                "step_tool": step.get("tool", ""),
                "step_args": step.get("args") or {},
                "observed_result": case.get("observed_result"),
            }
        )
    blob = json.dumps(normalized, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _export_post_approve_exec():
    return os.environ.get("SYNTH_EXPORT_POST_APPROVE_EXEC", "0") == "1"


def _case_is_post_approve_exec(case):
    if str((case or {}).get("decision") or "").strip() != "direct_tool":
        return False
    context_snapshot = (case or {}).get("context_snapshot") or {}
    human_feedback = context_snapshot.get("human_feedback") or {}
    return bool(human_feedback)


def _collect_exportable_sessions(run_id=None):
    seen_fingerprints = set()
    export_post_approve_exec = _export_post_approve_exec()
    stats = {
        "total_records": 0,
        "run_id": str(run_id or ""),
        "skipped_empty": 0,
        "skipped_run_id_mismatch": 0,
        "skipped_version_mismatch": 0,
        "skipped_missing_decision": 0,
        "skipped_abort": 0,
        "skipped_duplicate": 0,
        "skipped_post_approve_exec_cases": 0,
    }
    exportable_sessions = []
    for record in load_session_records():
        stats["total_records"] += 1
        if run_id and str(record.get("run_id") or "").strip() != str(run_id):
            stats["skipped_run_id_mismatch"] += 1
            continue
        session_cases = record.get("session_cases") or []
        if not session_cases:
            stats["skipped_empty"] += 1
            continue
        if any(case.get("trace_format_version") != DECISION_TRACE_FORMAT_VERSION for case in session_cases):
            stats["skipped_version_mismatch"] += 1
            continue
        if not any(case.get("task") and case.get("decision") for case in session_cases):
            stats["skipped_missing_decision"] += 1
            continue
        if any(case.get("decision") == "abort" for case in session_cases):
            stats["skipped_abort"] += 1
            continue
        filtered_cases = []
        for case in session_cases:
            if not export_post_approve_exec and _case_is_post_approve_exec(case):
                stats["skipped_post_approve_exec_cases"] += 1
                continue
            filtered_cases.append(case)
        if not filtered_cases:
            stats["skipped_empty"] += 1
            continue
        fingerprint = _session_fingerprint(filtered_cases)
        if fingerprint in seen_fingerprints:
            stats["skipped_duplicate"] += 1
            continue
        seen_fingerprints.add(fingerprint)
        exportable_sessions.append(filtered_cases)
    return exportable_sessions, stats


def _build_decision_snapshot(case, include_user_task=True):
    case = case or {}
    context_snapshot = case.get("context_snapshot") or {}
    proposed_step = case.get("step") or {}
    service_name = str(case.get("service_id") or case.get("environment") or "").strip()
    snapshot = {}
    if include_user_task:
        snapshot["user_task"] = case.get("task", "")
    snapshot["service"] = service_name
    snapshot["assistant_proposed_tool_call"] = {
        "tool": proposed_step.get("tool", ""),
        "tool_args": proposed_step.get("args") or {},
        "description": proposed_step.get("description", ""),
    }

    prior_steps = list(context_snapshot.get("prior_steps") or [])
    if prior_steps:
        snapshot["prior_steps"] = prior_steps

    human_feedback = context_snapshot.get("human_feedback") or {}
    if human_feedback:
        snapshot["human_feedback"] = human_feedback
    return snapshot


def _prior_step_fingerprint(step):
    step = step or {}
    blob = json.dumps(
        {
            "tool": step.get("tool", ""),
            "tool_args": step.get("tool_args") or {},
            "observation": step.get("observation"),
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _build_decision_prompt_message(case, include_user_task=True):
    return {
        "role": "user",
        "content": json.dumps(
            _build_decision_snapshot(case, include_user_task=include_user_task),
            ensure_ascii=False,
            indent=2,
        ),
    }


def _build_decision_completion_message(case):
    target_text = build_branch_target_text(case)
    if not target_text:
        return None
    return {"role": "assistant", "content": target_text}


def decision_case_to_sft_record(case):
    if _case_is_post_approve_exec(case) and not _export_post_approve_exec():
        return None
    completion_message = _build_decision_completion_message(case)
    if completion_message is None:
        return None
    risk = case.get("risk") or {}
    return {
        "prompt": [
            {"role": "system", "content": DECISION_TOKEN_SYSTEM_PROMPT},
            _build_decision_prompt_message(case),
        ],
        "completion": [completion_message],
        "meta": {
            "task": case.get("task", ""),
            "decision": branch_payload_from_case(case)[0],
            "decision_count": int(case.get("step_index", 0)) + 1,
            "service": str(case.get("service_id") or case.get("environment") or "").strip(),
            "risk_class": str(risk.get("risk_class") or ""),
            "risk_family": str(risk.get("risk_family") or ""),
        },
    }


def _session_prefix_records(session_cases):
    records = []
    prompt_messages = [{"role": "system", "content": DECISION_TOKEN_SYSTEM_PROMPT}]
    decision_count = 0
    seen_prior_step_fingerprints = set()
    for case in session_cases:
        if _case_is_post_approve_exec(case) and not _export_post_approve_exec():
            continue
        completion_message = _build_decision_completion_message(case)
        if completion_message is None:
            continue
        snapshot = _build_decision_snapshot(case, include_user_task=(decision_count == 0))
        prior_steps = list(snapshot.get("prior_steps") or [])
        if prior_steps:
            filtered_prior_steps = []
            for step in prior_steps:
                fingerprint = _prior_step_fingerprint(step)
                if fingerprint in seen_prior_step_fingerprints:
                    continue
                filtered_prior_steps.append(step)
                seen_prior_step_fingerprints.add(fingerprint)
            if filtered_prior_steps:
                snapshot["prior_steps"] = filtered_prior_steps
            else:
                snapshot.pop("prior_steps", None)
        prompt_messages.append(
            {
                "role": "user",
                "content": json.dumps(snapshot, ensure_ascii=False, indent=2),
            }
        )
        decision_count += 1
        last_decision = branch_payload_from_case(case)[0]
        risk = case.get("risk") or {}
        records.append(
            {
                "prompt": list(prompt_messages),
                "completion": [completion_message],
                "meta": {
                    "task": case.get("task", ""),
                    "decision": last_decision,
                    "decision_count": decision_count,
                    "service": str(case.get("service_id") or case.get("environment") or "").strip(),
                    "risk_class": str(risk.get("risk_class") or ""),
                    "risk_family": str(risk.get("risk_family") or ""),
                },
            }
        )
        prompt_messages.append(completion_message)
    return records


def export_decision_token_sft(output_path=DECISION_TOKEN_SFT_PATH, verbose=True, run_id=None):
    records = []
    exportable_sessions, stats = _collect_exportable_sessions(run_id=run_id)
    for session_cases in exportable_sessions:
        records.extend(_session_prefix_records(session_cases))

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(records, fh, ensure_ascii=False, indent=2)

    if verbose:
        print_stage_start("Export Decision Token SFT Data")
        print(f"[Export Source] {TRACE_SESSION_PATH}")
        print(f"[Export Target] {output_path}")
        if run_id:
            print(f"[Run ID] {run_id}")
        print(f"[Sample Count] {len(records)}")
        print_json_block("Export Stats", stats)
        if records:
            print_json_block("First Sample", records[0])
        print_stage_end("Export Decision Token SFT Data", f"Wrote {len(records)} samples")
    return {"output_path": output_path, "count": len(records), "stats": stats}

# ==================== Task File Loading ====================


def load_task_file(path):
    """Load a YAML task definition file."""
    if yaml is None:
        raise RuntimeError("pyyaml is not installed. Run: pip install pyyaml")
    with open(path, "r", encoding="utf-8") as fh:
        payload = yaml.safe_load(fh) or {}
    return payload
