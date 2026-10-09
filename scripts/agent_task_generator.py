"""Agent task generator for v2 stakes-rich tasks.

Loads docs/TASK_AUTHORING.md as the prompt template, fills per-call placeholders
(service / template / vocabulary / discovery tools / few-shot / fingerprints),
calls DeepSeek with native tool-use enabled (read-only tools only), parses
the agent's JSON output, runs pre-flight validation, and writes:

  - tasks/<svc>/<task_id>.yaml          (task YAML)
  - tasks/<svc>/<task_id>.persona.json  (per-task personas)
  - logs/agent_runs/<run_id>/<task_id>.log  (full LM call trace)

The full validator (check_v2_task.py) runs separately and includes both
pre-flight and post-flight checks. This script applies a minimal pre-flight
gate so we don't write obviously broken outputs.

Usage:
    python -m scripts.agent_task_generator --service erpnext --template T1
    python -m scripts.agent_task_generator --service gitea  --template T2 --max-retries 2
"""

import argparse
import datetime
import importlib
import json
import os
import sys
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

try:
    import openai
except ModuleNotFoundError:
    print("openai package required. pip install openai", file=sys.stderr)
    sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# Templates and service eligibility (mirrors docs/TASK_AUTHORING.md §2)
# ─────────────────────────────────────────────────────────────────────────────

TEMPLATE_FULL_NAME = {
    "T1": "T1_bulk_with_review",
    "T2": "T2_multi_stage_onboarding",
    "T3": "T3_escalating_cleanup",
    "T4": "T4_cross_principal_approval",
    "T5": "T5_compliance_grey",
    "T6": "T6_reconciliation_sync",
    "T7": "T7_reactive_triage",
}

TEMPLATE_SERVICES = {
    "T1": {"nocodb", "erpnext", "zammad", "openemr", "gitea", "vaultwarden"},
    "T2": {"gitea", "rocketchat", "owncloud", "vaultwarden"},
    "T3": {"owncloud", "gitea", "vaultwarden", "nocodb"},
    "T4": {"erpnext", "openemr", "zammad", "vaultwarden"},
    "T5": {"openemr", "vaultwarden", "mailu", "owncloud"},
    "T6": {"erpnext", "nocodb", "zammad", "vaultwarden"},
    "T7": {"zammad", "rocketchat", "vaultwarden"},
}


# ─────────────────────────────────────────────────────────────────────────────
# Env loading
# ─────────────────────────────────────────────────────────────────────────────

def _load_dotenv(path: str, force_keys: tuple = ()) -> None:
    """Load .env into os.environ. Existing env vars normally win, except for
    keys listed in `force_keys` which are always overridden by the .env value.
    """
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


_client: Optional[Any] = None
_provider: str = "deepseek"
_model_override: Optional[str] = None


def _get_client():
    global _client
    if _client is not None:
        return _client

    if _provider == "openrouter":
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY not set (expected an sk-or-v1-... OpenRouter key)")
        base_url = os.environ.get("OPENAI_BASE_URL", "https://openrouter.ai/api/v1")
    elif _provider == "deepseek":
        api_key = os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            raise RuntimeError("DEEPSEEK_API_KEY not set in environment / .env")
        base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
    else:
        raise ValueError(f"unknown provider: {_provider}")

    # 300s: pro/thinking models can take 60-100s per turn on big prompts
    _client = openai.OpenAI(api_key=api_key, base_url=base_url, timeout=300.0)
    return _client


def _resolve_model() -> str:
    if _model_override:
        return _model_override
    if _provider == "openrouter":
        return os.environ.get("OPENAI_MODEL", "openai/gpt-5.5")
    return os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")


# ─────────────────────────────────────────────────────────────────────────────
# Prompt assembly
# ─────────────────────────────────────────────────────────────────────────────

def load_prompt_template(repo_root: str) -> str:
    p = os.path.join(repo_root, "docs", "TASK_AUTHORING.md")
    with open(p, "r", encoding="utf-8") as fh:
        return fh.read()


def fill_prompt(
    template: str,
    service: str,
    template_id: str,
    vocabulary: dict,
    discovery_tools: dict,
    few_shot: str,
    history_fingerprints: List[dict],
    runtime_hints: str = "",
) -> str:
    return (
        template
        .replace("{{service}}", service)
        .replace("{{template_id}}", template_id)
        .replace("{{vocabulary_json}}", json.dumps(vocabulary, ensure_ascii=False, indent=2))
        .replace("{{discovery_tools_json}}", json.dumps(discovery_tools, ensure_ascii=False, indent=2))
        .replace("{{few_shot_examples}}", few_shot if few_shot.strip() else "(no few-shot examples loaded for this run)")
        .replace("{{history_fingerprints}}", json.dumps(history_fingerprints, ensure_ascii=False))
        .replace("{{runtime_hints}}", runtime_hints if runtime_hints.strip() else "(none)")
    )


def load_few_shot(repo_root: str, template_id: str) -> str:
    """Load few-shot examples for this template if any.

    Looks for: prompts/few_shot/<template_id>.md
    Returns empty string if file doesn't exist (smoke runs without examples).
    """
    p = os.path.join(repo_root, "prompts", "few_shot", f"{template_id}.md")
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as fh:
            return fh.read()
    return ""


def load_history_fingerprints(repo_root: str, service: str) -> List[dict]:
    """Load already-generated tasks' fingerprints for this service."""
    p = os.path.join(repo_root, "services", service, "_fingerprints.json")
    if not os.path.exists(p):
        return []
    with open(p, "r", encoding="utf-8") as fh:
        return json.load(fh)


def append_fingerprint(repo_root: str, service: str, fp: dict) -> None:
    p = os.path.join(repo_root, "services", service, "_fingerprints.json")
    history = load_history_fingerprints(repo_root, service)
    history.append(fp)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(history, fh, ensure_ascii=False, indent=2)


# ─────────────────────────────────────────────────────────────────────────────
# DeepSeek tool_use bridge
# ─────────────────────────────────────────────────────────────────────────────

def make_tool_specs(discovery_tools: dict) -> List[dict]:
    """Convert discovery_tools.json into OpenAI-style function-call schemas."""
    specs = []
    for t in discovery_tools.get("tools", []):
        properties: Dict[str, dict] = {}
        required: List[str] = []
        for p in t.get("params", []):
            properties[p["name"]] = {"type": "string"}
            if "default" not in p:
                required.append(p["name"])
        specs.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("summary") or f"Read-only tool {t['name']}",
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        })
    return specs


def execute_tool_call(service: str, tool_name: str, arguments: dict) -> str:
    """Call the actual read-only tool implementation. Falls back gracefully if
    backend unreachable (returns an error stub)."""
    try:
        mod = importlib.import_module(f"safety_pipeline.services.tools.{service}")
    except Exception as exc:
        return json.dumps({"error": f"failed to import service tools: {exc}"})

    fn = getattr(mod, tool_name, None)
    if fn is None or not callable(fn):
        return json.dumps({"error": f"tool {tool_name} not found in {service}"})

    try:
        result = fn(**(arguments or {}))
    except Exception as exc:
        return json.dumps({
            "error": f"{type(exc).__name__}: {exc}",
            "hint": "Service backend may not be running. Continue without this tool.",
        })

    text = json.dumps(result, ensure_ascii=False, default=str)
    if len(text) > 4000:
        text = text[:4000] + "\n...(truncated; was {} chars)".format(len(text))
    return text


# ─────────────────────────────────────────────────────────────────────────────
# Agent loop
# ─────────────────────────────────────────────────────────────────────────────

MAX_TOOL_CALLS = 5
MAX_AGENT_TURNS = 10  # safety cap including tool-use rounds


def run_agent(
    prompt: str,
    tool_specs: List[dict],
    service: str,
    log_lines: List[str],
    user_kickoff: Optional[str] = None,
) -> str:
    client = _get_client()
    model = _resolve_model()
    messages: List[dict] = [{"role": "system", "content": prompt}]
    if user_kickoff:
        messages.append({"role": "user", "content": user_kickoff})

    tool_calls_made = 0
    for turn in range(MAX_AGENT_TURNS):
        kwargs = {
            "model": model,
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": 6000,
        }
        if tool_specs and tool_calls_made < MAX_TOOL_CALLS:
            kwargs["tools"] = tool_specs
            kwargs["tool_choice"] = "auto"

        log_lines.append(f"[turn {turn}] calling LM (n_tool_calls_so_far={tool_calls_made})")
        resp = client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message

        if msg.tool_calls:
            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    } for tc in msg.tool_calls
                ],
            })
            for tc in msg.tool_calls:
                if tool_calls_made >= MAX_TOOL_CALLS:
                    result_text = json.dumps({
                        "error": "TOOL_BUDGET_EXHAUSTED",
                        "instruction": (
                            "You have used all 5 tool calls. Now emit ONLY the final JSON "
                            "object (task_yaml + persona_json + fingerprint). No more tool "
                            "calls, no prose, no markdown fences, no commentary. Just the "
                            "JSON object."
                        ),
                    })
                else:
                    try:
                        args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                    except json.JSONDecodeError:
                        args = {}
                    result_text = execute_tool_call(service, tc.function.name, args)
                tool_calls_made += 1
                log_lines.append(
                    f"  tool_call #{tool_calls_made}: {tc.function.name}({tc.function.arguments}) "
                    f"→ {result_text[:200]}"
                )
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result_text,
                })
            continue

        # Final text response
        text = (msg.content or "").strip()
        log_lines.append(f"[turn {turn}] final response ({len(text)} chars)")
        return text

    raise RuntimeError(f"Agent exceeded MAX_AGENT_TURNS={MAX_AGENT_TURNS}")


# ─────────────────────────────────────────────────────────────────────────────
# Output parsing + pre-flight validation
# ─────────────────────────────────────────────────────────────────────────────

def parse_agent_output(text: str) -> dict:
    """Extract a JSON object from agent output, tolerating prose preamble
    and markdown code fences."""
    import re
    text = text.strip()

    # Try a fenced code block first
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if fence_match:
        candidate = fence_match.group(1).strip()
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass  # fall through to brace-matching

    # Brace-matching: find first '{' and last matching '}' that produces valid JSON
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        candidate = text[first_brace:last_brace + 1]
        return json.loads(candidate)

    # Final fallback: try the whole text
    return json.loads(text)


def preflight_validate(parsed: dict, vocabulary: dict) -> List[str]:
    """Minimal structural + anchor-in-vocab checks. Full validator is
    check_v2_task.py."""
    errors: List[str] = []

    task = parsed.get("task_yaml")
    persona_root = parsed.get("persona_json")
    if not isinstance(task, dict):
        errors.append("task_yaml missing or not a dict")
    if not isinstance(persona_root, dict):
        errors.append("persona_json missing or not a dict")
    if errors:
        return errors

    # task_yaml fields
    for fld in ("id", "service", "template", "task"):
        if not task.get(fld):
            errors.append(f"task_yaml missing required field: {fld}")
    if task.get("template") and task["template"] not in TEMPLATE_FULL_NAME.values():
        errors.append(f"task_yaml.template not one of: {list(TEMPLATE_FULL_NAME.values())}")

    # personas
    personas = persona_root.get("personas")
    if not isinstance(personas, dict) or len(personas) < 2:
        errors.append("persona_json.personas must have ≥2 personas")
        return errors

    axes_present = {p.get("axis") for p in personas.values() if isinstance(p, dict)}
    for required_axis in ("cautious", "permissive"):
        if required_axis not in axes_present:
            errors.append(f"missing persona with axis={required_axis}")

    # vocab anchor index for fast lookup
    enum_values = {}
    for entity, schema in vocabulary.get("entity_schemas", {}).items():
        for fname, fdef in schema.get("fields", {}).items():
            if fdef.get("type") == "enum":
                enum_values[f"{entity}.{fname}"] = set(fdef.get("values", []))
    tool_names = set()
    for cat, lst in vocabulary.get("tool_inventory", {}).items():
        for t in lst:
            tool_names.add(t["name"])
    named_entity_buckets = vocabulary.get("named_entities", {})

    # cross-persona divergence: collect anchors per axis
    anchors_by_axis: Dict[str, List[Tuple[str, str, Any]]] = {}
    for pname, p in personas.items():
        if not isinstance(p, dict):
            errors.append(f"persona {pname}: not a dict")
            continue
        items = p.get("items", [])
        if not isinstance(items, list) or len(items) < 2:
            errors.append(f"persona {pname}: needs ≥2 items, got {len(items) if isinstance(items, list) else 'n/a'}")
        anchors = p.get("anchors_referenced")
        if not isinstance(anchors, list) or not anchors:
            errors.append(f"persona {pname}: missing or empty anchors_referenced")
            continue
        ax = p.get("axis", "")
        anchors_by_axis.setdefault(ax, [])
        for a in anchors:
            if not isinstance(a, dict):
                errors.append(f"persona {pname}: anchor not a dict")
                continue
            t, f, v = a.get("type"), a.get("field"), a.get("value")
            anchors_by_axis[ax].append((t, f, v))
            # vocab check (best-effort, soft)
            if t == "enum":
                key = f
                if key not in enum_values:
                    errors.append(f"persona {pname}: enum anchor field {f} not in vocabulary.entity_schemas")
                elif v not in enum_values[key]:
                    errors.append(f"persona {pname}: enum value {v!r} not in vocabulary {f} values {sorted(enum_values[key])}")
            elif t == "tool":
                # Allow glob patterns like bulk_*
                if isinstance(v, str) and v.endswith("_*"):
                    prefix = v[:-1]
                    if not any(name.startswith(prefix) for name in tool_names):
                        errors.append(f"persona {pname}: tool pattern {v!r} matches no known tool")
                elif v not in tool_names:
                    errors.append(f"persona {pname}: tool name {v!r} not in vocabulary.tool_inventory")
            elif t == "named_entity":
                bucket = f
                if bucket not in named_entity_buckets:
                    errors.append(f"persona {pname}: named_entity field {f} not in vocabulary.named_entities")
                elif v not in named_entity_buckets[bucket]:
                    errors.append(f"persona {pname}: named_entity value {v!r} not in vocabulary {f}")
            # numerical / arg: not strictly checked here (full validator handles)

    # divergence: cautious / permissive must differ on ≥2 anchors
    cautious = set(anchors_by_axis.get("cautious", []))
    permissive = set(anchors_by_axis.get("permissive", []))
    diverging = (cautious ^ permissive)  # symmetric difference
    if len(diverging) < 2:
        errors.append(
            f"persona divergence rule: cautious vs permissive must differ on ≥2 anchors, got {len(diverging)}"
        )

    return errors


# ─────────────────────────────────────────────────────────────────────────────
# IO
# ─────────────────────────────────────────────────────────────────────────────

def write_outputs(
    parsed: dict,
    service: str,
    repo_root: str,
    run_id: str,
    log_lines: List[str],
) -> dict:
    task = parsed["task_yaml"]
    persona = parsed["persona_json"]
    task_id = task["id"]

    tasks_dir = os.path.join(repo_root, "tasks", service)
    os.makedirs(tasks_dir, exist_ok=True)

    # task YAML — emit YAML manually to keep it human-readable
    task_yaml_path = os.path.join(tasks_dir, f"{task_id}.yaml")
    write_task_yaml(task_yaml_path, task)

    persona_json_path = os.path.join(tasks_dir, f"{task_id}.persona.json")
    with open(persona_json_path, "w", encoding="utf-8") as fh:
        json.dump(persona, fh, ensure_ascii=False, indent=2)

    log_dir = os.path.join(repo_root, "logs", "agent_runs", run_id)
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f"{task_id}.log")
    with open(log_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(log_lines))

    return {
        "task_yaml_path": task_yaml_path,
        "persona_json_path": persona_json_path,
        "log_path": log_path,
    }


def write_task_yaml(path: str, task: dict) -> None:
    """Emit a small subset of YAML by hand. Avoids pyyaml dependency."""
    lines: List[str] = []
    lines.append(f"id: {task['id']}")
    lines.append(f"service: {task['service']}")
    lines.append(f"template: {task['template']}")
    lines.append("task: |")
    for line in str(task["task"]).splitlines() or [""]:
        lines.append(f"  {line}")
    am = task.get("attack_methods")
    if am:
        if isinstance(am, list):
            inline = "[" + ", ".join(map(str, am)) + "]"
            lines.append(f"attack_methods: {inline}")
        else:
            lines.append(f"attack_methods: {am}")
    notes = task.get("notes")
    if notes:
        lines.append("notes: |")
        for line in str(notes).splitlines() or [""]:
            lines.append(f"  {line}")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


# ─────────────────────────────────────────────────────────────────────────────
# Top-level
# ─────────────────────────────────────────────────────────────────────────────

def generate_one(
    service: str,
    template_id: str,
    repo_root: str,
    run_id: str,
    max_retries: int,
) -> dict:
    if template_id not in TEMPLATE_FULL_NAME:
        raise ValueError(f"template {template_id} not in {list(TEMPLATE_FULL_NAME)}")
    if service not in TEMPLATE_SERVICES.get(template_id, set()):
        print(
            f"[warn] service {service} is not in the recommended list for {template_id}; proceeding anyway",
            file=sys.stderr,
        )

    vocab_path = os.path.join(repo_root, "services", service, "vocabulary.json")
    tools_path = os.path.join(repo_root, "services", service, "discovery_tools.json")
    if not os.path.exists(vocab_path) or not os.path.exists(tools_path):
        raise FileNotFoundError(
            f"Missing vocab or tools for {service}. Run extract_vocabulary.py first."
        )

    with open(vocab_path) as fh:
        vocabulary = json.load(fh)
    with open(tools_path) as fh:
        discovery_tools = json.load(fh)

    prompt_template = load_prompt_template(repo_root)
    few_shot = load_few_shot(repo_root, template_id)
    history = load_history_fingerprints(repo_root, service)
    tool_specs = make_tool_specs(discovery_tools)

    last_errors: List[str] = []
    for attempt in range(max_retries + 1):
        log_lines: List[str] = [
            f"agent_task_generator run_id={run_id} service={service} template={template_id} attempt={attempt+1}",
            f"timestamp={datetime.datetime.utcnow().isoformat()}Z",
        ]
        runtime_hints = ""
        if last_errors:
            runtime_hints = (
                "⚠️ MANDATORY FIX — your previous attempt FAILED validation. "
                "The errors below MUST be fixed in this attempt. Do NOT repeat them.\n\n"
                "ERRORS:\n"
                + "\n".join(f"  - {e}" for e in last_errors)
                + "\n\nROOT CAUSE: you used anchor values that do not exist in the "
                  "service vocabulary. ALL anchor values (especially enum values, "
                  "tool names, and named_entity values) MUST come from "
                  "vocabulary.json — copy them verbatim. If you need a status / "
                  "name that's not in vocab, pick a different one from vocab; do "
                  "NOT invent new ones."
            )

        prompt = fill_prompt(
            prompt_template, service, template_id,
            vocabulary, discovery_tools, few_shot, history, runtime_hints,
        )

        try:
            output_text = run_agent(prompt, tool_specs, service, log_lines)
        except Exception as exc:
            import traceback
            tb = traceback.format_exc()
            last_errors = [f"agent loop failed: {type(exc).__name__}: {exc}"]
            log_lines.append(f"ERROR: {exc}")
            log_lines.append(tb)
            # Always write log even on failure
            log_dir = os.path.join(repo_root, "logs", "agent_runs", run_id)
            os.makedirs(log_dir, exist_ok=True)
            with open(os.path.join(log_dir, f"failed_attempt_{attempt+1}.log"), "w", encoding="utf-8") as fh:
                fh.write("\n".join(log_lines))
            continue

        log_lines.append("--- raw agent output ---")
        log_lines.append(output_text)

        # Always write log so we can inspect
        log_dir = os.path.join(repo_root, "logs", "agent_runs", run_id)
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, f"attempt_{attempt+1}.log"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(log_lines))

        try:
            parsed = parse_agent_output(output_text)
        except Exception as exc:
            last_errors = [f"output JSON parse failed: {exc}"]
            log_lines.append(f"ERROR: parse: {exc}")
            with open(os.path.join(log_dir, f"attempt_{attempt+1}.log"), "w", encoding="utf-8") as fh:
                fh.write("\n".join(log_lines))
            continue

        errors = preflight_validate(parsed, vocabulary)
        if errors:
            last_errors = errors
            log_lines.append(f"PREFLIGHT FAILED: {errors}")
            with open(os.path.join(log_dir, f"attempt_{attempt+1}.log"), "w", encoding="utf-8") as fh:
                fh.write("\n".join(log_lines))
            continue

        # Success — write outputs
        paths = write_outputs(parsed, service, repo_root, run_id, log_lines)

        # Append fingerprint
        fp = parsed.get("fingerprint") or {}
        fp_with_meta = {
            "task_id": parsed["task_yaml"]["id"],
            "template": parsed["task_yaml"]["template"],
            "tools_used": fp.get("tools_used", []),
            "anchor_keys": fp.get("anchor_keys", []),
        }
        append_fingerprint(repo_root, service, fp_with_meta)

        return {
            "ok": True,
            "service": service,
            "template": template_id,
            "task_id": parsed["task_yaml"]["id"],
            "attempt": attempt + 1,
            **paths,
        }

    return {
        "ok": False,
        "service": service,
        "template": template_id,
        "errors": last_errors,
        "attempts": max_retries + 1,
    }


def main():
    global _provider, _model_override
    parser = argparse.ArgumentParser()
    parser.add_argument("--service", required=True)
    parser.add_argument("--template", required=True, choices=list(TEMPLATE_FULL_NAME))
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--repo-root", default=None)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--provider", choices=["deepseek", "openrouter"], default="deepseek",
                        help="LLM provider. openrouter uses OPENAI_* env vars (forced from .env).")
    parser.add_argument("--model", default=None,
                        help="Override the model name. Defaults: deepseek-chat / openai/gpt-5.5.")
    args = parser.parse_args()

    repo_root = args.repo_root or os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    # When using openrouter, force-override OPENAI_* keys from .env so any
    # pre-existing shell env (e.g. an OpenAI direct sk-proj-... key) doesn't
    # collide with the OpenRouter sk-or-v1-... key in .env.
    if args.provider == "openrouter":
        _load_dotenv(os.path.join(repo_root, ".env"),
                     force_keys=("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL"))
    else:
        _load_dotenv(os.path.join(repo_root, ".env"))

    _provider = args.provider
    _model_override = args.model

    run_id = args.run_id or datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:6]

    result = generate_one(args.service, args.template, repo_root, run_id, args.max_retries)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()
