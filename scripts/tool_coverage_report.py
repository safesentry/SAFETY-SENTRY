#!/usr/bin/env python3
"""Tool-coverage report.

For each of the 9 services, lists how many existing task YAMLs mention each
registered tool name, broken down by decision hint (direct_execute / ask_human
/ refuse).
"""

import argparse
import importlib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from safety_pipeline.task_catalog import TASKS_ROOT, iter_task_files  # noqa: E402

try:
    import yaml
except ModuleNotFoundError as exc:  # pragma: no cover
    raise SystemExit("pyyaml is required. Run: pip install pyyaml") from exc


SERVICES = [
    "gitea",
    "rocketchat",
    "owncloud",
    "nocodb",
    "zammad",
    "erpnext",
    "openemr",
    "vaultwarden",
    "mailu",
]

DECISIONS = ["direct_execute", "ask_human", "refuse"]


def decision_hint_from_name(name):
    stem = Path(name).stem
    if "-refuse" in stem or stem.endswith("refuse"):
        return "refuse"
    if "-ah" in stem or stem.endswith("ah"):
        return "ask_human"
    return "direct_execute"


def load_yaml(path):
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_service_tool_names(service):
    module = importlib.import_module(f"safety_pipeline.services.tools.{service}")
    names = list(module.get_tool_names())
    return sorted(names)


def _walk_tool_fields(node):
    """Yield every value under a `tool:` key at any depth."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "tool" and isinstance(value, str):
                yield value
            else:
                yield from _walk_tool_fields(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_tool_fields(item)


def collect_tool_anchors(payload, tool_names_set):
    """Collect (tool, source) pairs for a single task YAML.

    Sources:
      - "canonical": value of any `tool:` field (canonical_step / nested steps)
      - "task_text": underscored tool name appearing in the natural-language task
        body — only counted when also a registered tool, since task body rarely
        uses the underscored form
    """
    anchors = []
    for value in _walk_tool_fields(payload):
        normalized = value.strip()
        if normalized in tool_names_set:
            anchors.append((normalized, "canonical"))

    task_text = str(payload.get("task") or "")
    for name in tool_names_set:
        if re.search(rf"\b{re.escape(name)}\b", task_text):
            anchors.append((name, "task_text"))
    return anchors


def build_coverage(tasks_root):
    coverage = {svc: {} for svc in SERVICES}
    for svc in SERVICES:
        try:
            tool_names = load_service_tool_names(svc)
        except Exception as exc:
            print(f"[warn] failed to load tools for {svc}: {exc}", file=sys.stderr)
            tool_names = []
        coverage[svc] = {
            "tools": tool_names,
            "tool_names_set": set(tool_names),
            "tool_to_count": Counter(),
            "tool_to_decision_counts": defaultdict(Counter),
            "tool_to_source_counts": defaultdict(Counter),
            "task_count": 0,
            "decision_task_count": Counter(),
        }

    for path in iter_task_files(tasks_root):
        if Path(path).name == "TASK_TEMPLATE.yaml":
            continue
        payload = load_yaml(path)
        service = str(payload.get("service") or "").strip()
        if service not in coverage:
            continue
        decision = decision_hint_from_name(path)
        bucket = coverage[service]
        bucket["task_count"] += 1
        bucket["decision_task_count"][decision] += 1
        seen_tools_in_task = set()
        for tool, source in collect_tool_anchors(payload, bucket["tool_names_set"]):
            bucket["tool_to_source_counts"][tool][source] += 1
            if tool in seen_tools_in_task:
                continue
            seen_tools_in_task.add(tool)
            bucket["tool_to_count"][tool] += 1
            bucket["tool_to_decision_counts"][tool][decision] += 1
    return coverage


def summarize(coverage):
    rows = []
    for svc in SERVICES:
        bucket = coverage[svc]
        tools = bucket["tools"]
        total_tools = len(tools)
        cov = bucket["tool_to_count"]
        zero = [t for t in tools if cov[t] == 0]
        once = [t for t in tools if cov[t] == 1]
        twice = [t for t in tools if cov[t] == 2]
        top = cov.most_common(5)
        rows.append(
            {
                "service": svc,
                "total_tools": total_tools,
                "task_count": bucket["task_count"],
                "task_by_decision": dict(bucket["decision_task_count"]),
                "tool_mentions_total": sum(cov.values()),
                "zero_mention_count": len(zero),
                "one_mention_count": len(once),
                "two_mention_count": len(twice),
                "zero_mention_tools": zero,
                "one_mention_tools": once,
                "top_tools": top,
            }
        )
    return rows


def write_markdown(rows, coverage, out_path):
    lines = []
    lines.append("# Tool Coverage Report")
    lines.append("")
    lines.append("For each registered tool per service, counts the number of task YAMLs that anchor on it. A task counts as anchoring on a tool when the tool name appears in any `tool:` field (e.g. `canonical_step.tool`) or as a literal underscored token in the task body.")
    lines.append("")
    lines.append("## Per-service summary")
    lines.append("")
    lines.append("| service | tools | tasks | DE / AH / R | mentions | 0× | 1× | 2× | top3 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for row in rows:
        td = row["task_by_decision"]
        de = td.get("direct_execute", 0)
        ah = td.get("ask_human", 0)
        ref = td.get("refuse", 0)
        top3 = ", ".join(f"{name}({n})" for name, n in row["top_tools"][:3]) or "(none)"
        lines.append(
            f"| {row['service']} | {row['total_tools']} | {row['task_count']} | "
            f"{de} / {ah} / {ref} | {row['tool_mentions_total']} | "
            f"{row['zero_mention_count']} | {row['one_mention_count']} | "
            f"{row['two_mention_count']} | {top3} |"
        )
    lines.append("")

    lines.append("## Underrepresented tools")
    lines.append("")
    lines.append("The lists below identify registered tools with zero or one task anchor.")
    lines.append("")
    for row in rows:
        svc = row["service"]
        bucket = coverage[svc]
        zero = row["zero_mention_tools"]
        once = row["one_mention_tools"]
        lines.append(f"### {svc}")
        lines.append("")
        lines.append(f"- 0-mention ({len(zero)}): {', '.join(f'`{t}`' for t in zero) or '(none)'}")
        lines.append(f"- 1-mention ({len(once)}): {', '.join(f'`{t}` ({_decision_breakdown(bucket, t)})' for t in once) or '(none)'}")
        lines.append("")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def _decision_breakdown(bucket, tool):
    counts = bucket["tool_to_decision_counts"].get(tool, Counter())
    parts = []
    for d in DECISIONS:
        n = counts.get(d, 0)
        if n:
            parts.append(f"{d[:2]}={n}")
    return ",".join(parts) or "—"


def write_json(coverage, out_path):
    serializable = {}
    for svc, bucket in coverage.items():
        serializable[svc] = {
            "tools": bucket["tools"],
            "tool_to_count": dict(bucket["tool_to_count"]),
            "tool_to_decision_counts": {
                t: dict(c) for t, c in bucket["tool_to_decision_counts"].items()
            },
            "tool_to_source_counts": {
                t: dict(c) for t, c in bucket["tool_to_source_counts"].items()
            },
            "task_count": bucket["task_count"],
            "decision_task_count": dict(bucket["decision_task_count"]),
        }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(serializable, fh, ensure_ascii=False, indent=2)


def main():
    parser = argparse.ArgumentParser(description="Tool coverage across tasks per service.")
    parser.add_argument("--tasks-root", default=str(TASKS_ROOT))
    parser.add_argument("--md-out", default=str(ROOT_DIR / "artifacts" / "tool_coverage.md"))
    parser.add_argument("--json-out", default=str(ROOT_DIR / "artifacts" / "tool_coverage.json"))
    parser.add_argument("--print", action="store_true", help="Also print summary to stdout")
    args = parser.parse_args()

    coverage = build_coverage(args.tasks_root)
    rows = summarize(coverage)

    write_markdown(rows, coverage, Path(args.md_out))
    write_json(coverage, Path(args.json_out))

    if args.print:
        print(f"[wrote] {args.md_out}")
        print(f"[wrote] {args.json_out}")
        for row in rows:
            td = row["task_by_decision"]
            top3 = ", ".join(f"{name}({n})" for name, n in row["top_tools"][:3])
            print(
                f"  {row['service']:<11} tools={row['total_tools']:<3} "
                f"tasks={row['task_count']:<3} "
                f"DE/AH/R={td.get('direct_execute',0)}/{td.get('ask_human',0)}/{td.get('refuse',0)} "
                f"0×={row['zero_mention_count']:<3} 1×={row['one_mention_count']:<2} "
                f"2×={row['two_mention_count']:<2} top: {top3}"
            )


if __name__ == "__main__":
    main()
