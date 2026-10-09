import argparse
import json
import os
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

from .. import runtime
from ..session_store import append_session_record
from ..settings import DECISION_TOKEN_SFT_PATH, TRACE_SESSION_PATH, set_pipeline_env
from ..task_catalog import iter_task_files
from ..runtime import export_decision_token_sft, load_task_file
from .pass1_runner import inject_canonical_step, pass1_steps, run_task_pure
from .pass2_reviewer import review_trace
from .step_composer import compose_next_step
from .trajectory_writer import splice


_PRINT_LOCK = threading.Lock()


def _print_line(payload):
    with _PRINT_LOCK:
        print(json.dumps(payload, ensure_ascii=False))
        sys.stdout.flush()


def _append_session_cases(session_cases, run_id):
    append_session_record(session_cases, run_id=run_id)


_READ_ONLY_TOOL_PREFIXES = (
    "list_", "get_", "read_", "search_", "file_info", "show_", "fetch_",
    "find_", "describe_", "view_", "stat_",
)


def _looks_read_only(tool_name):
    name = str(tool_name or "").strip().lower()
    return any(name.startswith(prefix) for prefix in _READ_ONLY_TOOL_PREFIXES)


_READ_ONLY_TOOL_PREFIXES = (
    "list_", "get_", "read_", "search_", "file_info", "show_", "fetch_",
    "find_", "describe_", "view_", "stat_",
)


def _looks_read_only(tool_name):
    name = str(tool_name or "").strip().lower()
    return any(name.startswith(prefix) for prefix in _READ_ONLY_TOOL_PREFIXES)


def _strip_read_only_pass1_steps(pass1_trace):
    """Drop assistant.tool_call/tool pairs whose tool is read-only. Keeps only
    the user message + non-read pairs. Used when canonical_step is injected so
    pass2 doesn't end up refusing benign recon based on task-text vibes."""
    transcript = list((pass1_trace or {}).get("transcript") or [])
    kept = []
    i = 0
    while i < len(transcript):
        item = transcript[i]
        if (
            isinstance(item, dict)
            and item.get("role") == "assistant"
            and isinstance(item.get("tool_call"), dict)
            and i + 1 < len(transcript)
            and isinstance(transcript[i + 1], dict)
            and transcript[i + 1].get("role") == "tool"
        ):
            tool_name = item["tool_call"].get("tool", "")
            if _looks_read_only(tool_name) and not transcript[i + 1].get("synthetic"):
                i += 2
                continue
        kept.append(item)
        i += 1
    new_trace = dict(pass1_trace or {})
    new_trace["transcript"] = kept
    return new_trace


def _maybe_inject_canonical(pass1_trace, task_config):
    """If the task declares a canonical_step, append a synthetic dangerous-action
    step (composed by an LLM when possible, falling back to the YAML literal).
    Read-only pass1 steps are stripped from the final transcript so pass2 only
    judges the dangerous proposal, not benign recon.
    """
    canonical_yaml = task_config.get("canonical_step")
    if not canonical_yaml:
        return pass1_trace

    real_steps = pass1_steps(pass1_trace)

    available_tools = list(runtime.get_tool_schemas(allow_empty=True))
    composed = compose_next_step(task_config["task"], real_steps, available_tools)

    chosen = composed
    if chosen is not None and _looks_read_only(chosen.get("tool")):
        chosen = None

    if chosen is None:
        try:
            chosen = runtime.validate_real_tool_step(
                canonical_yaml,
                context_label="canonical_step.fallback",
                fallback_text=task_config["task"],
            )
        except Exception:
            return pass1_trace

    for step in real_steps:
        if step.get("tool") == chosen["tool"] and (step.get("tool_args") or {}) == chosen["args"]:
            # pass1 already proposed the dangerous call for real; keep its
            # observation, just strip recon noise so pass2 reviews only the
            # write step.
            return _strip_read_only_pass1_steps(pass1_trace)

    stripped = _strip_read_only_pass1_steps(pass1_trace)
    return inject_canonical_step(stripped, chosen)


def _is_refuse_task(task_file, task_config):
    """Refuse tasks bypass the pass1 LLM: pass1 routinely refuses to emit a
    tool_call on these prompts (the alignment-vs-data-gen conflict), so we
    skip both run_task_pure and the composer LLM and inject canonical_step
    directly. Pass2 still owns the labeling decision.

    A task is treated as a refuse task when any of the following hold:
      - the file name ends with ``-refuse.yaml``;
      - ``decision_hint`` is ``refuse``;
      - ``role`` is ``S`` (Stability task, per the Mailu redesign spec) --
        Stability tasks carry a structurally-unsafe ``canonical_step`` that
        must be injected verbatim rather than re-derived by pass1.
    """
    if str(task_file).endswith("-refuse.yaml"):
        return True
    hint = str(task_config.get("decision_hint") or "").strip().lower()
    if hint == "refuse":
        return True
    role = str(task_config.get("role") or "").strip().upper()
    return role == "S"


def _build_synthetic_refuse_trace(task_config):
    """Build a minimal pass1 trace for a refuse task without calling any LLM.

    Shape matches what review_trace + splice expect: user turn, then a
    synthetic (assistant.tool_call, tool.observation=None) pair carrying
    the YAML canonical_step verbatim. Returns None when the task lacks a
    usable canonical_step so the caller can fall back to the LLM path.
    """
    canonical_yaml = task_config.get("canonical_step")
    if not canonical_yaml:
        return None
    try:
        validated = runtime.validate_real_tool_step(
            canonical_yaml,
            context_label="canonical_step.refuse_bypass",
            fallback_text=task_config.get("task", ""),
        )
    except Exception:
        return None
    base_trace = {
        "task": task_config["task"],
        "environment": str(task_config.get("service") or ""),
        "transcript": [{"role": "user", "content": task_config["task"]}],
        "final_status": "synthetic_refuse",
        "final_response": "",
    }
    return inject_canonical_step(base_trace, validated)


def _run_one(task_file, run_id):
    try:
        task_config = load_task_file(task_file)
        service_name = str(task_config.get("service") or "").strip()
        if service_name:
            set_pipeline_env(service_name)
        pass1_trace = None
        if _is_refuse_task(task_file, task_config):
            pass1_trace = _build_synthetic_refuse_trace(task_config)
        if pass1_trace is None:
            pass1_trace = run_task_pure(task_config)
            pass1_trace = _maybe_inject_canonical(pass1_trace, task_config)
        pass2_decisions = review_trace(pass1_trace, task_file=task_file)
        synthetic_trace = splice(pass1_trace, pass2_decisions, task_config)
    except Exception as exc:
        return {"task_file": task_file, "skipped": f"{type(exc).__name__}: {exc}"}
    _append_session_cases(synthetic_trace.get("session_cases", []), run_id=run_id)
    return synthetic_trace


def _decision_token_sft_path_for_suffix(suffix):
    suffix = (suffix or "").strip()
    if not suffix:
        return DECISION_TOKEN_SFT_PATH
    base_dir = os.path.dirname(DECISION_TOKEN_SFT_PATH)
    base_name, ext = os.path.splitext(os.path.basename(DECISION_TOKEN_SFT_PATH))
    return os.path.join(base_dir, f"{base_name}.{suffix}{ext}")


def main():
    parser = argparse.ArgumentParser(description="Two-pass synthetic trace generator")
    parser.add_argument("--task-file", help="Path to one YAML task definition")
    parser.add_argument("--task-list", help="Path to a text file containing one task YAML path per line")
    parser.add_argument("--service", help="Limit to one service subdir under tasks/ (e.g. gitea)")
    parser.add_argument("--out", help="Optional JSONL output path for synthetic traces")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="Number of tasks to process in parallel via ThreadPoolExecutor. Default: 1 (sequential).",
    )
    parser.add_argument(
        "--out-suffix",
        default="",
        help="Suffix for the decision-token SFT output file (decision_token_sft.<suffix>.json). "
             "Use when running multiple services concurrently so exports do not overwrite each other.",
    )
    args = parser.parse_args()

    if args.task_file:
        task_files = [args.task_file]
    elif args.task_list:
        with open(args.task_list, "r", encoding="utf-8") as fh:
            task_files = [line.strip() for line in fh if line.strip() and not line.strip().startswith("#")]
    elif args.service:
        from ..task_catalog import TASKS_ROOT
        service_root = os.path.join(TASKS_ROOT, args.service)
        if not os.path.isdir(service_root):
            raise SystemExit(f"Service directory not found: {service_root}")
        task_files = list(iter_task_files(tasks_root=service_root))
    else:
        task_files = list(iter_task_files())

    if args.service:
        set_pipeline_env(args.service)

    run_id = uuid.uuid4().hex[:12]
    _print_line({"event": "run_start", "run_id": run_id, "service": args.service or "", "task_count": len(task_files), "concurrency": args.concurrency})

    traces = []
    traces_lock = threading.Lock()

    def _handle(task_file):
        trace = _run_one(task_file, run_id=run_id)
        if "skipped" in trace:
            _print_line({"task_file": task_file, "skipped": trace["skipped"]})
        else:
            with traces_lock:
                traces.append(trace)
            _print_line({"task_file": task_file, "final_status": trace.get("final_status", "")})

    if args.concurrency <= 1:
        for task_file in task_files:
            _handle(task_file)
    else:
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = [pool.submit(_handle, tf) for tf in task_files]
            for fut in as_completed(futures):
                fut.result()

    output_path = _decision_token_sft_path_for_suffix(args.out_suffix)
    decision_export = export_decision_token_sft(output_path=output_path, verbose=False, run_id=run_id)

    if args.out:
        output_dir = os.path.dirname(args.out)
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            for trace in traces:
                fh.write(json.dumps(trace, ensure_ascii=False) + "\n")

    _print_line(
        {
            "run_id": run_id,
            "service": args.service or "",
            "trace_sessions": TRACE_SESSION_PATH,
            "trace_count": len(traces),
            "decision_token_sft": decision_export.get("output_path", ""),
            "decision_token_count": decision_export.get("count", 0),
            "decision_token_export_stats": decision_export.get("stats", {}),
        }
    )


if __name__ == "__main__":
    main()
