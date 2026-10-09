#!/usr/bin/env python3

import argparse
import json
import os
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


def load_yaml(path):
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def decision_hint_from_name(name):
    stem = Path(name).stem
    if "-refuse" in stem or stem.endswith("refuse"):
        return "refuse"
    if "-ah" in stem or stem.endswith("ah"):
        return "ask_human"
    return "direct_execute"
def main():
    parser = argparse.ArgumentParser(description="Summarize task coverage across services and decision hints.")
    parser.add_argument("--tasks-root", default=str(TASKS_ROOT), help="Path to tasks root")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    args = parser.parse_args()

    service_counts = Counter()
    decision_counts = Counter()
    matrix = defaultdict(Counter)

    for path in iter_task_files(args.tasks_root):
        if Path(path).name == "TASK_TEMPLATE.yaml":
            continue
        payload = load_yaml(path)
        service = str(payload.get("service") or "unknown").strip()
        decision_hint = decision_hint_from_name(path)

        service_counts[service] += 1
        decision_counts[decision_hint] += 1
        matrix[service][decision_hint] += 1

    report = {
        "tasks_root": os.path.abspath(args.tasks_root),
        "total_tasks": sum(service_counts.values()),
        "service_counts": dict(service_counts),
        "decision_counts": dict(decision_counts),
        "service_by_decision": {
            service: dict(counts)
            for service, counts in sorted(matrix.items())
        },
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
