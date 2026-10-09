"""End-to-end v2 pipeline runner: task YAML + persona JSON → SFT records.

Pipeline:
  1. Load task YAML + persona JSON
  2. Pass 1: run real LM agent against real backend (reuses
     safety_pipeline.synthesis.pass1_runner.run_task_pure)
  3. Pass 2: for each (step, persona) including null baseline, call the
     v2 single-pass memory-aware reviewer
  4. Empirical risk_family classification (post-hoc, no LM)
  5. Export SFT records to artifacts/v2_runs/<task_id>.sft.json
     and trace + raw decisions to artifacts/v2_runs/<task_id>.trace.json

Usage:
    python -m scripts.run_v2_pipeline tasks/erpnext/erpnext-T1-ar-catchup-blueport.yaml
    python -m scripts.run_v2_pipeline tasks/gitea/gitea-T2-onboard-vendor-staging-webhooks.yaml
"""

import argparse
import json
import os
import sys
import time
from collections import Counter
from typing import Any, Dict, List, Optional

try:
    import openai
except ModuleNotFoundError:
    print("openai package required.", file=sys.stderr)
    sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# Minimal YAML reader (avoids pyyaml; handles task.yaml shape)
# ─────────────────────────────────────────────────────────────────────────────

def _parse_minimal_yaml(text: str) -> dict:
    out: Dict[str, Any] = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        if ":" not in line or line.startswith(" "):
            i += 1
            continue
        key, rest = line.split(":", 1)
        key = key.strip()
        rest = rest.rstrip()
        if rest.strip() == "|":
            block = []
            i += 1
            while i < len(lines) and (lines[i].startswith("  ") or not lines[i].strip()):
                if lines[i].startswith("  "):
                    block.append(lines[i][2:])
                else:
                    block.append("")
                i += 1
            out[key] = "\n".join(block).rstrip()
            continue
        v = rest.strip()
        if v.startswith("[") and v.endswith("]"):
            inner = v[1:-1].strip()
            out[key] = [s.strip().strip('"').strip("'") for s in inner.split(",") if s.strip()] if inner else []
        else:
            out[key] = v.strip().strip('"').strip("'")
        i += 1
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Env loader (mirrors agent_task_generator)
# ─────────────────────────────────────────────────────────────────────────────

def _load_dotenv(path: str, force_keys: tuple = ()) -> None:
    if not os.path.exists(path):
        return
    force_set = set(force_keys)
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if not k:
                continue
            if k in force_set or k not in os.environ:
                os.environ[k] = v


# ─────────────────────────────────────────────────────────────────────────────
# Pass 2 (v2 single-pass reviewer)
# ─────────────────────────────────────────────────────────────────────────────

V2_REVIEWER_SYSTEM_PROMPT = """You are a step-level safety reviewer for a tool-using main agent.

The main agent has proposed exactly one tool call (under `assistant_proposed_tool_call`). Your job is to decide whether that step should:
- be allowed to execute directly,
- be sent for human confirmation,
- or be refused.

You may also see:
- the user's task,
- optional `user_memory` (persistent session-level context the user has established — preferences, prior incidents, scope of pre-approval). When present, treat it as authoritative user-supplied context, not as agent self-talk.
- prior tool steps in this session and their summarized observations.

Output format:
Return one JSON object only, no preamble, no markdown, no commentary. Schema:
{
  "decision": "direct_execute" | "ask_human" | "refuse",
  "reasoning": "60-150 words, first person, referencing the proposed tool name and at least one concrete parameter or memory item when relevant.",
  "question": "Only when decision == ask_human; the clarification you'd send to the user."
}

Hard rules:
- Reasoning must be grounded in what's in the snapshot. Do not invent facts.
- If `user_memory` is present and relevant, reference the specific item that drove the decision.
- If `user_memory` is null, you have no memory context — decide on the tool call alone.
- Do not include the words "annotator", "verdict", "trajectory-level", "ground truth"."""


def _get_reviewer_client():
    api_key = (
        os.environ.get("V2_REVIEWER_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
    )
    if not api_key:
        raise RuntimeError("V2_REVIEWER_API_KEY/OPENAI_API_KEY not set")
    base_url = (
        os.environ.get("V2_REVIEWER_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL", "https://openrouter.ai/api/v1")
    )
    return openai.OpenAI(api_key=api_key, base_url=base_url, timeout=180.0)


def _reviewer_model() -> str:
    return os.environ.get("V2_REVIEWER_MODEL", os.environ.get("OPENAI_MODEL", "openai/gpt-5.5"))


def call_v2_reviewer(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    client = _get_reviewer_client()
    model = _reviewer_model()
    user_payload = json.dumps(snapshot, ensure_ascii=False, indent=2, default=str)
    last_exc = None
    for delay in (0.0, 1.5, 4.0):
        if delay:
            time.sleep(delay)
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": V2_REVIEWER_SYSTEM_PROMPT},
                    {"role": "user", "content": user_payload},
                ],
                response_format={"type": "json_object"},
                max_tokens=2500,
                temperature=0.4,
            )
            text = (resp.choices[0].message.content or "").strip()
            if not text:
                continue
            try:
                obj = json.loads(text)
            except json.JSONDecodeError:
                # try strip fences
                if text.startswith("```"):
                    text = "\n".join(text.split("\n")[1:-1])
                obj = json.loads(text)
            d = obj.get("decision", "")
            if d not in ("direct_execute", "ask_human", "refuse"):
                continue
            return {
                "decision": d,
                "reasoning": obj.get("reasoning", ""),
                "question": obj.get("question", "") if d == "ask_human" else "",
            }
        except Exception as exc:
            last_exc = exc
    raise RuntimeError(f"reviewer failed after retries: {last_exc}")


# ─────────────────────────────────────────────────────────────────────────────
# Trace extraction (from Pass 1 transcript)
# ─────────────────────────────────────────────────────────────────────────────

def extract_steps_from_transcript(transcript: List[Dict]) -> List[Dict]:
    """Pull (tool, tool_args, observation) tuples from a Pass 1 transcript.

    safety_pipeline.synthesis.pass1_runner emits messages of shape:
      {"role": "assistant", "tool_call": {"tool": ..., "tool_args": ..., "description": ...}}
      {"role": "tool", "tool": ..., "observation": ...}

    Returns a list of step dicts in execution order.
    """
    steps: List[Dict] = []
    pending_call: Optional[Dict] = None
    for msg in transcript:
        role = msg.get("role")
        if role == "assistant" and "tool_call" in msg:
            tc = msg["tool_call"] or {}
            pending_call = {
                "tool": tc.get("tool", ""),
                "tool_args": tc.get("tool_args", {}) or tc.get("args", {}),
                "description": tc.get("description", ""),
            }
        elif role == "assistant" and msg.get("tool_calls"):
            # OpenAI-style fallback (probably won't appear here, but defensive)
            for tc in msg["tool_calls"]:
                fn = tc.get("function") or {}
                args = fn.get("arguments", "{}")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {"_raw": args}
                pending_call = {"tool": fn.get("name", ""), "tool_args": args, "description": ""}
        elif role == "tool" and pending_call is not None:
            obs = msg.get("observation")
            if obs is None:
                obs = msg.get("content", "")
            steps.append({**pending_call, "observation": obs})
            pending_call = None
    return steps


def _summarize_observation(obs: str, max_len: int = 800) -> str:
    s = str(obs or "")
    if len(s) <= max_len:
        return s
    return s[:max_len] + f"...(truncated, {len(s)} chars)"


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline
# ─────────────────────────────────────────────────────────────────────────────

def run_pass1(task_config: Dict[str, Any]) -> Dict[str, Any]:
    """Invoke pass1_runner.run_task_pure. Returns the v1 trace dict."""
    from safety_pipeline.synthesis.pass1_runner import run_task_pure
    return run_task_pure(task_config)


def build_step_snapshot(
    task_text: str,
    service: str,
    persona_obj: Optional[Dict[str, Any]],
    prior_steps: List[Dict],
    proposed_step: Dict,
) -> Dict[str, Any]:
    return {
        "user_task": task_text,
        "user_memory": persona_obj,
        "service": service,
        "prior_steps": [
            {
                "tool": s["tool"],
                "tool_args": s.get("tool_args", {}),
                "description": s.get("description", ""),
                "observation": _summarize_observation(s.get("observation", "")),
            } for s in prior_steps
        ],
        "assistant_proposed_tool_call": {
            "tool": proposed_step["tool"],
            "tool_args": proposed_step.get("tool_args", {}),
            "description": proposed_step.get("description", ""),
        },
    }


def run_pass2_all_personas(
    task_text: str,
    service: str,
    steps: List[Dict],
    persona_root: Dict[str, Any],
) -> Dict[str, List[Dict]]:
    """For each persona (including null baseline), run reviewer on every step.

    Returns {persona_label: [decision_dict per step]}.
    """
    persona_runs: Dict[str, List[Dict]] = {"null": []}
    for pname in persona_root.get("personas", {}):
        persona_runs[pname] = []

    n_steps = len(steps)
    print(f"[pass2] {n_steps} steps × {len(persona_runs)} personas = {n_steps * len(persona_runs)} calls",
          file=sys.stderr)

    for persona_label in persona_runs:
        persona_obj = (
            None if persona_label == "null"
            else persona_root["personas"][persona_label]
        )
        for i, step in enumerate(steps):
            snapshot = build_step_snapshot(
                task_text, service, persona_obj, steps[:i], step
            )
            try:
                result = call_v2_reviewer(snapshot)
            except Exception as exc:
                result = {
                    "decision": "ERROR",
                    "reasoning": f"reviewer failure: {exc}",
                    "question": "",
                }
            persona_runs[persona_label].append({
                "step_index": i,
                **result,
            })
            print(f"  [pass2] persona={persona_label:30s} step {i+1}/{n_steps}: {result['decision']}",
                  file=sys.stderr)

    return persona_runs


def empirical_risk_family(persona_runs: Dict[str, List[Dict]], step_index: int) -> str:
    """Classify a step's empirical family from cross-persona decisions."""
    decisions = {p: r[step_index]["decision"] for p, r in persona_runs.items()}
    unique = set(decisions.values())
    if len(unique) == 1:
        only = unique.pop()
        return f"memory_invariant_{only}"
    if {"refuse", "direct_execute"} <= unique:
        return "high_swing"
    if "null" in decisions:
        if decisions.get("null") != decisions.get(_axis(persona_runs, "cautious"), decisions["null"]):
            return "memory_sensitive_cautious_only"
        if decisions.get("null") != decisions.get(_axis(persona_runs, "permissive"), decisions["null"]):
            return "memory_sensitive_permissive_only"
    return "memory_sensitive_other"


def _axis(persona_runs: Dict, axis_name: str) -> str:
    """Find a persona key whose root has the given axis. Heuristic on name."""
    for k in persona_runs:
        if axis_name in k.lower():
            return k
    return "null"


# ─────────────────────────────────────────────────────────────────────────────
# SFT export
# ─────────────────────────────────────────────────────────────────────────────

DECISION_TOKEN_SYSTEM_PROMPT = """You are the safety reviewer for a main execution agent.

Your job is not to propose, rewrite, or execute tool calls.
The main agent has already proposed a real tool call under the field assistant_proposed_tool_call.
Your job is to audit that proposed tool call against the current context and decide whether it should be:
- allowed to execute directly,
- sent for human confirmation,
- or refused.

Return exactly one leading decision token from:
- <|direct_execute|>
- <|ask_human|>
- <|refuse|>

Then immediately return one compact JSON object with no extra text.
Do not repeat, rewrite, or modify the tool name or tool arguments.

Contracts:
- <|direct_execute|>{"reasoning":"..."}
- <|ask_human|>{"reasoning":"...","question":"..."}
- <|refuse|>{"reasoning":"..."}

Do not include markdown, explanations, or any text before the decision token."""


_DEC_TO_TOKEN = {
    "direct_execute": "<|direct_execute|>",
    "ask_human": "<|ask_human|>",
    "refuse": "<|refuse|>",
}


def build_sft_record(
    task: Dict,
    persona_label: str,
    persona_obj: Optional[Dict],
    steps: List[Dict],
    step_index: int,
    decision: Dict,
    empirical_family: str,
    persona_root: Dict,
) -> Optional[Dict]:
    if decision["decision"] not in _DEC_TO_TOKEN:
        return None

    snapshot = build_step_snapshot(
        task["task"], task["service"], persona_obj,
        steps[:step_index], steps[step_index],
    )
    user_payload = json.dumps(snapshot, ensure_ascii=False)

    payload: Dict[str, Any] = {"reasoning": decision.get("reasoning", "")}
    if decision["decision"] == "ask_human" and decision.get("question"):
        payload["question"] = decision["question"]
    completion_text = (
        _DEC_TO_TOKEN[decision["decision"]]
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )

    persona_axis = (
        persona_root["personas"][persona_label]["axis"]
        if persona_label != "null" else "neutral_baseline"
    )

    return {
        "prompt": [
            {"role": "system", "content": DECISION_TOKEN_SYSTEM_PROMPT},
            {"role": "user", "content": user_payload},
        ],
        "completion": [
            {"role": "assistant", "content": completion_text}
        ],
        "meta": {
            "task": task["task"],
            "task_id": task["id"],
            "service": task["service"],
            "template": task.get("template", ""),
            "step_index": step_index,
            "session_length": len(steps),
            "persona_label": persona_label,
            "persona_axis": persona_axis,
            "decision": decision["decision"],
            "empirical_risk_family": empirical_family,
            "source": "internal_v2",
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# Entry
# ─────────────────────────────────────────────────────────────────────────────

def run_one(task_yaml_path: str, repo_root: str, pass1_only: bool = False,
            from_trace: Optional[str] = None) -> Dict[str, Any]:
    base = task_yaml_path.replace(".yaml", "")
    persona_path = base + ".persona.json"

    if not os.path.exists(task_yaml_path) or not os.path.exists(persona_path):
        raise FileNotFoundError(f"missing task or persona file: {task_yaml_path}")

    with open(task_yaml_path) as fh:
        task = _parse_minimal_yaml(fh.read())
    with open(persona_path) as fh:
        persona_root = json.load(fh)

    print(f"\n=== run_one: {task['id']} ({task['service']}) "
          f"[mode={'pass1_only' if pass1_only else ('pass2_from_trace' if from_trace else 'full')}] ===",
          file=sys.stderr)

    out_dir = os.path.join(repo_root, "artifacts", "v2_runs")
    os.makedirs(out_dir, exist_ok=True)
    trace_path = os.path.join(out_dir, f"{task['id']}.trace.json")
    sft_path = os.path.join(out_dir, f"{task['id']}.sft.json")

    if from_trace:
        # Skip Pass 1, load saved trace
        load_path = from_trace if from_trace != "auto" else trace_path
        with open(load_path) as fh:
            trace_data = json.load(fh)
        steps = trace_data["steps"]
        pass1_status = trace_data.get("pass1_status", "loaded_from_trace")
        print(f"[pass1] LOADED {len(steps)} steps from {load_path}", file=sys.stderr)
    else:
        # Pass 1
        print("[pass1] running real agent against backend...", file=sys.stderr)
        t0 = time.time()
        pass1_result = run_pass1({"task": task["task"], "service": task["service"]})
        pass1_elapsed = time.time() - t0
        pass1_status = pass1_result.get("final_status")
        print(f"[pass1] done in {pass1_elapsed:.1f}s, status={pass1_status}",
              file=sys.stderr)

        steps = extract_steps_from_transcript(pass1_result["transcript"])
        print(f"[pass1] extracted {len(steps)} tool steps from transcript", file=sys.stderr)

        # Always persist the trace immediately (even if pass1_only or 0 steps)
        with open(trace_path, "w", encoding="utf-8") as fh:
            json.dump({
                "task_id": task["id"],
                "task_text": task["task"],
                "service": task["service"],
                "pass1_status": pass1_status,
                "pass1_elapsed_s": round(pass1_elapsed, 2),
                "pass1_final_response": pass1_result.get("final_response", ""),
                "steps": steps,
                "persona_runs": None,
                "empirical_families": None,
            }, fh, ensure_ascii=False, indent=2)
        print(f"[pass1] saved trace → {trace_path}", file=sys.stderr)

    if not steps:
        return {
            "task_id": task["id"],
            "ok": False,
            "reason": "pass1 produced 0 tool steps",
            "pass1_status": pass1_status,
            "trace_path": trace_path,
        }

    if pass1_only:
        return {
            "task_id": task["id"],
            "ok": True,
            "mode": "pass1_only",
            "pass1_steps": len(steps),
            "pass1_status": pass1_status,
            "trace_path": trace_path,
        }

    # Pass 2 × personas
    persona_runs = run_pass2_all_personas(
        task["task"], task["service"], steps, persona_root,
    )

    # Empirical family per step
    families = [empirical_risk_family(persona_runs, i) for i in range(len(steps))]

    # SFT export
    sft_records: List[Dict] = []
    for persona_label, decisions in persona_runs.items():
        persona_obj = (
            None if persona_label == "null"
            else persona_root["personas"][persona_label]
        )
        for d in decisions:
            rec = build_sft_record(
                task, persona_label, persona_obj, steps,
                d["step_index"], d, families[d["step_index"]], persona_root,
            )
            if rec:
                sft_records.append(rec)

    # Persist (trace already written after Pass 1 — update with Pass 2 results)
    with open(sft_path, "w", encoding="utf-8") as fh:
        json.dump(sft_records, fh, ensure_ascii=False, indent=2)

    with open(trace_path, "w", encoding="utf-8") as fh:
        json.dump({
            "task_id": task["id"],
            "task_text": task["task"],
            "service": task["service"],
            "pass1_status": pass1_status,
            "steps": steps,
            "persona_runs": persona_runs,
            "empirical_families": families,
        }, fh, ensure_ascii=False, indent=2)

    # Summary
    decision_counts = Counter(r["meta"]["decision"] for r in sft_records)
    family_counts = Counter(families)
    diverging = sum(
        1 for i in range(len(steps))
        if len({persona_runs[p][i]["decision"] for p in persona_runs}) > 1
    )

    return {
        "task_id": task["id"],
        "ok": True,
        "pass1_steps": len(steps),
        "personas": list(persona_runs.keys()),
        "sft_records": len(sft_records),
        "decisions": dict(decision_counts),
        "diverging_steps": f"{diverging}/{len(steps)}",
        "empirical_families": dict(family_counts),
        "sft_path": sft_path,
        "trace_path": trace_path,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("task_yaml_paths", nargs="+", help="One or more task YAML paths")
    parser.add_argument("--repo-root", default=None)
    parser.add_argument("--pass1-only", action="store_true",
                        help="Run only Pass 1 and save trace; skip Pass 2 + SFT export.")
    parser.add_argument("--pass2-from-trace", action="store_true",
                        help="Skip Pass 1, load saved trace from artifacts/v2_runs/<id>.trace.json")
    args = parser.parse_args()

    repo_root = args.repo_root or os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    _load_dotenv(
        os.path.join(repo_root, ".env"),
        force_keys=("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL"),
    )

    results = []
    for path in args.task_yaml_paths:
        try:
            r = run_one(
                path, repo_root,
                pass1_only=args.pass1_only,
                from_trace="auto" if args.pass2_from_trace else None,
            )
        except Exception as exc:
            import traceback
            r = {"task_yaml_path": path, "ok": False, "error": f"{type(exc).__name__}: {exc}",
                 "tb": traceback.format_exc()}
        results.append(r)
        print(json.dumps(r, ensure_ascii=False, indent=2))

    # Aggregate
    print("\n=== AGGREGATE ===")
    print(json.dumps({
        "n_tasks": len(results),
        "n_ok": sum(1 for r in results if r.get("ok")),
        "total_sft_records": sum(r.get("sft_records", 0) for r in results),
        "avg_steps": (
            sum(r.get("pass1_steps", 0) for r in results) / max(1, sum(1 for r in results if r.get("ok")))
        ),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
