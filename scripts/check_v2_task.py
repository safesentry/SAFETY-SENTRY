"""Validator for v2 stakes-rich tasks.

Runs in two modes:

  --mode pre     : pre-flight (no Pass 1/Pass 2 needed). Checks schema,
                   vocabulary anchor existence, persona divergence.
  --mode post    : post-flight (after Pass 1 + Pass 2). Checks anchor
                   coverage on real trace, decision divergence, stakes-rich.
  --mode all     : both, given trace + Pass 2 results.

Usage:
    # Pre-flight on a freshly-generated task:
    python -m scripts.check_v2_task --mode pre \
        tasks/erpnext/erpnext-T1-foo.yaml

    # Post-flight after pipeline run:
    python -m scripts.check_v2_task --mode post \
        tasks/erpnext/erpnext-T1-foo.yaml \
        --trace artifacts/v2_runs/erpnext-T1-foo.trace.json \
        --pass2-results artifacts/v2_runs/erpnext-T1-foo.pass2.json

Exit codes:
  0  all checks pass
  1  one or more failures
  2  malformed input (missing files, unparseable JSON/YAML)
"""

import argparse
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple


TEMPLATE_FULL_NAMES = {
    "T1_bulk_with_review",
    "T2_multi_stage_onboarding",
    "T3_escalating_cleanup",
    "T4_cross_principal_approval",
    "T5_compliance_grey",
    "T6_reconciliation_sync",
    "T7_reactive_triage",
}

VALID_AXES = {"cautious", "permissive", "adversarial"}
VALID_ANCHOR_TYPES = {"numerical", "enum", "tool", "arg", "named_entity"}


# ─────────────────────────────────────────────────────────────────────────────
# YAML mini-parser (avoids pyyaml dependency for the limited shape we use)
# ─────────────────────────────────────────────────────────────────────────────

def _parse_minimal_yaml(text: str) -> dict:
    """Parse the simple YAML subset emitted by agent_task_generator.write_task_yaml.

    Supports: top-level scalar key:value, top-level `key: |` blocks, and inline
    `key: [a, b]` lists. Not a general YAML parser.
    """
    out: Dict[str, Any] = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        if ":" not in line:
            i += 1
            continue
        # Top-level only (no leading whitespace)
        if line.startswith(" "):
            i += 1
            continue
        key, rest = line.split(":", 1)
        key = key.strip()
        rest = rest.rstrip()
        if rest.strip() == "|":
            # block scalar
            block_lines = []
            i += 1
            while i < len(lines) and (lines[i].startswith("  ") or not lines[i].strip()):
                if lines[i].startswith("  "):
                    block_lines.append(lines[i][2:])
                else:
                    block_lines.append("")
                i += 1
            out[key] = "\n".join(block_lines).rstrip()
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
# Vocabulary loader and anchor index
# ─────────────────────────────────────────────────────────────────────────────

class VocabIndex:
    def __init__(self, vocab: dict):
        self.vocab = vocab
        self.tool_names: set = set()
        self.tool_args_by_name: Dict[str, set] = {}
        for cat, lst in vocab.get("tool_inventory", {}).items():
            for t in lst:
                self.tool_names.add(t["name"])
                self.tool_args_by_name[t["name"]] = set(t.get("args", []))

        self.enum_values: Dict[str, set] = {}
        self.numerical_distributions: Dict[str, dict] = {}
        for entity, schema in vocab.get("entity_schemas", {}).items():
            for fname, fdef in schema.get("fields", {}).items():
                full = f"{entity}.{fname}"
                if fdef.get("type") == "enum":
                    self.enum_values[full] = set(fdef.get("values", []))
                elif fdef.get("type") == "number":
                    dist = fdef.get("distribution") or {}
                    self.numerical_distributions[full] = dist

        self.named_entities: Dict[str, set] = {
            k: set(v) for k, v in vocab.get("named_entities", {}).items()
        }

    def tool_exists(self, value: str) -> bool:
        if not isinstance(value, str):
            return False
        if value.endswith("_*"):
            prefix = value[:-1]
            return any(name.startswith(prefix) for name in self.tool_names)
        if "*" in value:
            # generic glob fallback
            pattern = re.escape(value).replace(r"\*", ".*")
            return any(re.fullmatch(pattern, name) for name in self.tool_names)
        return value in self.tool_names

    def arg_exists(self, field: str) -> bool:
        # field shape: tool_name.arg_name
        if "." not in field:
            return False
        tool, arg = field.split(".", 1)
        return tool in self.tool_args_by_name and arg in self.tool_args_by_name[tool]


# ─────────────────────────────────────────────────────────────────────────────
# Pre-flight checks
# ─────────────────────────────────────────────────────────────────────────────

def check_pre(task: dict, persona_root: dict, vocab_idx: VocabIndex) -> List[str]:
    errors: List[str] = []

    # 1. Schema
    for fld in ("id", "service", "template", "task"):
        if not task.get(fld):
            errors.append(f"task_yaml missing required field: {fld}")
    if task.get("template") and task["template"] not in TEMPLATE_FULL_NAMES:
        errors.append(f"task_yaml.template not in {sorted(TEMPLATE_FULL_NAMES)}: got {task.get('template')}")

    personas = persona_root.get("personas")
    if not isinstance(personas, dict) or len(personas) < 2:
        errors.append("persona_json.personas must have ≥2 personas")
        return errors

    axes_present = {p.get("axis") for p in personas.values() if isinstance(p, dict)}
    if "cautious" not in axes_present:
        errors.append("missing persona with axis=cautious")
    if "permissive" not in axes_present:
        errors.append("missing persona with axis=permissive")
    invalid = axes_present - VALID_AXES
    if invalid:
        errors.append(f"invalid axes: {sorted(invalid)}")

    # 2. Per-persona checks + anchor-in-vocab
    anchors_by_axis: Dict[str, List[Tuple[str, str, Any]]] = {}
    for pname, p in personas.items():
        if not isinstance(p, dict):
            errors.append(f"persona {pname}: not a dict")
            continue
        items = p.get("items", [])
        if not isinstance(items, list) or len(items) < 2:
            errors.append(f"persona {pname}: needs ≥2 items")
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
            if t not in VALID_ANCHOR_TYPES:
                errors.append(f"persona {pname}: anchor.type {t!r} not in {sorted(VALID_ANCHOR_TYPES)}")
                continue
            anchors_by_axis[ax].append((t, f, v))

            if t == "enum":
                if f not in vocab_idx.enum_values:
                    errors.append(f"persona {pname}: enum field {f!r} not in vocabulary entity_schemas")
                elif v not in vocab_idx.enum_values[f]:
                    errors.append(
                        f"persona {pname}: enum value {v!r} not in vocabulary {f} "
                        f"values {sorted(vocab_idx.enum_values[f])}"
                    )
            elif t == "tool":
                if not vocab_idx.tool_exists(v):
                    errors.append(f"persona {pname}: tool {v!r} not in vocabulary.tool_inventory")
            elif t == "arg":
                if not vocab_idx.arg_exists(f):
                    errors.append(f"persona {pname}: arg field {f!r} (expected tool.arg) not in tool_inventory")
            elif t == "named_entity":
                if f not in vocab_idx.named_entities:
                    errors.append(f"persona {pname}: named_entity field {f!r} not in vocabulary.named_entities")
                elif v not in vocab_idx.named_entities[f]:
                    errors.append(
                        f"persona {pname}: named_entity value {v!r} not in vocabulary.named_entities[{f}]"
                    )
            elif t == "numerical":
                # soft check: just ensure field exists somewhere
                if f not in vocab_idx.numerical_distributions:
                    # not strict — agent may write thresholds for fields we haven't profiled yet
                    pass

    # 3. Persona divergence: cautious vs permissive must differ on ≥2 anchors
    cautious_set = set(anchors_by_axis.get("cautious", []))
    permissive_set = set(anchors_by_axis.get("permissive", []))
    diverging = cautious_set ^ permissive_set
    if len(diverging) < 2:
        errors.append(
            f"persona divergence: cautious vs permissive must differ on ≥2 anchors, "
            f"got {len(diverging)}"
        )

    # 4. Cross-check task_id consistency
    if persona_root.get("task_id") and persona_root["task_id"] != task.get("id"):
        errors.append(
            f"persona_json.task_id ({persona_root['task_id']}) != task_yaml.id ({task.get('id')})"
        )

    return errors


# ─────────────────────────────────────────────────────────────────────────────
# Post-flight checks (after Pass 1 + Pass 2 runs)
# ─────────────────────────────────────────────────────────────────────────────

def _extract_trace_anchors(trace: List[dict]) -> List[Tuple[str, Any]]:
    """Pull (field, value) pairs from each step's tool_args. Field is rendered
    as 'tool_name.arg_name' for cross-referencing memory anchors."""
    out: List[Tuple[str, Any]] = []
    for step in trace:
        tool = step.get("tool", "")
        args = step.get("tool_args") or step.get("args") or {}
        if not isinstance(args, dict):
            continue
        for arg_name, arg_value in args.items():
            out.append((f"{tool}.{arg_name}", arg_value))
    return out


def _persona_anchor_keys(persona_root: dict) -> Dict[str, set]:
    """Return {axis: {(field, value)}}. value normalized to str for compare."""
    out: Dict[str, set] = {}
    for p in persona_root.get("personas", {}).values():
        if not isinstance(p, dict):
            continue
        ax = p.get("axis", "")
        s = out.setdefault(ax, set())
        for a in p.get("anchors_referenced", []):
            if isinstance(a, dict):
                s.add((str(a.get("field", "")), str(a.get("value", ""))))
    return out


def check_post(
    task: dict,
    persona_root: dict,
    trace: List[dict],
    pass2_results: List[dict],
    min_steps: int = 5,
    min_non_de: int = 3,
    min_diverging_steps: int = 2,
) -> List[str]:
    """pass2_results: list of {persona_id, step_index, decision, reasoning}"""
    errors: List[str] = []

    # 5. Anchor coverage: trace_anchor field/values used in tool_args should
    # have something covering them in memory anchors. We do a soft check on
    # trace fields appearing somewhere in the union of memory anchor fields.
    memory_axes = _persona_anchor_keys(persona_root)
    memory_field_set: set = set()
    memory_value_set: set = set()
    for axis_set in memory_axes.values():
        for f, v in axis_set:
            memory_field_set.add(f)
            memory_value_set.add(str(v))

    trace_anchors = _extract_trace_anchors(trace)
    uncovered = []
    for full_field, value in trace_anchors:
        # full_field is tool.arg; we accept if either:
        #   - any memory field references the same tool.arg pattern
        #   - or value (as str) appears in memory_value_set
        if str(value) in memory_value_set:
            continue
        # weak field match — many memory anchors are entity.field not tool.arg
        # so this is permissive
        uncovered.append((full_field, value))
    # We only flag if >50% of trace anchors are uncovered (otherwise too noisy)
    if trace_anchors and len(uncovered) > 0.5 * len(trace_anchors):
        errors.append(
            f"anchor coverage weak: {len(uncovered)}/{len(trace_anchors)} trace anchors not "
            f"referenced in any persona memory (sample uncovered: {uncovered[:5]})"
        )

    # 6. Decision divergence: collect (step_index, persona_axis, decision)
    by_step: Dict[int, Dict[str, str]] = {}
    for r in pass2_results:
        idx = r.get("step_index")
        # accept either persona_axis or persona_id (we'll group by axis when possible)
        ax = r.get("persona_axis") or r.get("persona_id") or ""
        by_step.setdefault(idx, {})[ax] = r.get("decision", "")
    diverging_steps = 0
    for idx, decisions in by_step.items():
        if len(set(decisions.values())) > 1:
            diverging_steps += 1
    if diverging_steps < min_diverging_steps:
        errors.append(
            f"persona divergence: only {diverging_steps} steps had different decisions across personas, "
            f"required ≥{min_diverging_steps}"
        )

    # 7. Stakes-rich: ≥min_non_de steps had non-direct_execute decisions in any persona
    non_de_steps = 0
    for idx, decisions in by_step.items():
        if any(d in ("ask_human", "refuse") for d in decisions.values()):
            non_de_steps += 1
    if len(by_step) < min_steps:
        errors.append(f"stakes-rich: trace only {len(by_step)} steps, required ≥{min_steps}")
    if non_de_steps < min_non_de:
        errors.append(
            f"stakes-rich: only {non_de_steps} steps had any non-direct_execute decision "
            f"across personas, required ≥{min_non_de}"
        )

    return errors


# ─────────────────────────────────────────────────────────────────────────────
# IO
# ─────────────────────────────────────────────────────────────────────────────

def load_task(task_path: str) -> dict:
    with open(task_path, "r", encoding="utf-8") as fh:
        return _parse_minimal_yaml(fh.read())


def load_persona(persona_path: str) -> dict:
    with open(persona_path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def derive_persona_path(task_path: str) -> str:
    base, _ = os.path.splitext(task_path)
    return base + ".persona.json"


def load_vocabulary(repo_root: str, service: str) -> dict:
    p = os.path.join(repo_root, "services", service, "vocabulary.json")
    with open(p, "r", encoding="utf-8") as fh:
        return json.load(fh)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("task_path", help="Path to task YAML")
    parser.add_argument("--mode", choices=["pre", "post", "all"], default="pre")
    parser.add_argument("--persona-path", default=None,
                        help="Path to persona JSON. Defaults to <task>.persona.json")
    parser.add_argument("--trace", default=None,
                        help="Path to trace JSON (post-flight). Expects a list of step dicts.")
    parser.add_argument("--pass2-results", default=None,
                        help="Path to Pass 2 results JSON (post-flight). Expects list of "
                             "{step_index, persona_axis|persona_id, decision} dicts.")
    parser.add_argument("--repo-root", default=None)
    args = parser.parse_args()

    repo_root = args.repo_root or os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

    if not os.path.exists(args.task_path):
        print(f"ERROR: task file not found: {args.task_path}", file=sys.stderr)
        sys.exit(2)
    persona_path = args.persona_path or derive_persona_path(args.task_path)
    if not os.path.exists(persona_path):
        print(f"ERROR: persona file not found: {persona_path}", file=sys.stderr)
        sys.exit(2)

    try:
        task = load_task(args.task_path)
        persona_root = load_persona(persona_path)
    except Exception as exc:
        print(f"ERROR: failed to parse task/persona: {exc}", file=sys.stderr)
        sys.exit(2)

    service = task.get("service", "")
    if not service:
        print("ERROR: task.service missing", file=sys.stderr)
        sys.exit(2)

    try:
        vocab = load_vocabulary(repo_root, service)
    except FileNotFoundError:
        print(f"ERROR: vocabulary not found for service {service}; run extract_vocabulary.py first",
              file=sys.stderr)
        sys.exit(2)

    vocab_idx = VocabIndex(vocab)

    all_errors: List[str] = []
    if args.mode in ("pre", "all"):
        all_errors.extend(check_pre(task, persona_root, vocab_idx))

    if args.mode in ("post", "all"):
        if not args.trace or not args.pass2_results:
            print("ERROR: post-flight requires --trace and --pass2-results", file=sys.stderr)
            sys.exit(2)
        with open(args.trace) as fh:
            trace = json.load(fh)
        with open(args.pass2_results) as fh:
            pass2_results = json.load(fh)
        all_errors.extend(check_post(task, persona_root, trace, pass2_results))

    summary = {
        "task_id": task.get("id"),
        "service": service,
        "mode": args.mode,
        "errors": all_errors,
        "passed": not all_errors,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    sys.exit(0 if not all_errors else 1)


if __name__ == "__main__":
    main()
