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

from safety_pipeline.environment import get_supported_backend_names  # noqa: E402
from safety_pipeline.service_registry import get_service_spec  # noqa: E402
from safety_pipeline.task_catalog import TASKS_ROOT, iter_task_files  # noqa: E402

try:
    import yaml
except ModuleNotFoundError as exc:  # pragma: no cover
    raise SystemExit("pyyaml is required. Run: pip install pyyaml") from exc


STRONG_APPROVAL_PHRASES = (
    "approved",
    "authorized",
    "verified",
    "confirmed",
    "reviewed and accepted",
    "greenlit",
    "signed off",
)

OBVIOUS_MALICIOUS_PHRASES = (
    "without detection",
    "secretly",
    "exfiltrate",
    "steal",
    "hide the evidence",
    "leave no trace",
    "bypass the audit",
)


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


def validate_task(path, args, seen_ids):
    rel_path = os.path.relpath(path, ROOT_DIR)
    payload = load_yaml(path)
    errors = []
    warnings = []

    if not isinstance(payload, dict):
        return {"path": rel_path, "errors": ["task file must decode to a YAML object"], "warnings": []}

    task_id = str(payload.get("id") or "").strip()
    service = str(payload.get("service") or "").strip()
    task_text = str(payload.get("task") or "").strip()
    for field_name, value in (
        ("id", task_id),
        ("service", service),
        ("task", task_text),
    ):
        if not value:
            errors.append(f"missing required field: {field_name}")

    if task_id:
        if task_id in seen_ids:
            errors.append(f"duplicate task id: {task_id}")
        else:
            seen_ids.add(task_id)

    spec = get_service_spec(service) if service else None
    if service and spec is None:
        errors.append(f"unknown service: {service}")

    if service and service not in get_supported_backend_names():
        errors.append(f"service is not a supported backend: {service}")

    decision_hint = decision_hint_from_name(path)
    lowered_task = task_text.lower()
    if decision_hint == "ask_human":
        for phrase in STRONG_APPROVAL_PHRASES:
            if phrase in lowered_task:
                warnings.append(f"ask_human task contains strong approval language: {phrase}")
                break
    if decision_hint == "refuse":
        obvious_hits = [phrase for phrase in OBVIOUS_MALICIOUS_PHRASES if phrase in lowered_task]
        if obvious_hits:
            warnings.append(
                "refuse task may be too explicit; obvious malicious phrases found: "
                + ", ".join(obvious_hits[:3])
            )
    if service and Path(path).parent.name != service:
        warnings.append(f"task file is under {Path(path).parent.name}/ but service is {service}")

    return {
        "path": rel_path,
        "task_id": task_id,
        "service": service,
        "decision_hint": decision_hint,
        "errors": errors,
        "warnings": warnings,
    }


def main():
    parser = argparse.ArgumentParser(description="Validate task YAML files.")
    parser.add_argument("--tasks-root", default=str(TASKS_ROOT), help="Path to tasks root")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    args = parser.parse_args()

    task_paths = [
        path
        for path in iter_task_files(args.tasks_root)
        if Path(path).name != "TASK_TEMPLATE.yaml"
    ]
    seen_ids = set()
    reports = [validate_task(path, args, seen_ids) for path in task_paths]

    error_count = sum(len(item["errors"]) for item in reports)
    warning_count = sum(len(item["warnings"]) for item in reports)
    tasks_with_errors = [item for item in reports if item["errors"]]
    tasks_with_warnings = [item for item in reports if item["warnings"]]

    decision_counts = Counter(item["decision_hint"] for item in reports)
    service_counts = Counter(item["service"] for item in reports if item["service"])
    warning_types = Counter()
    for item in tasks_with_warnings:
        for warning in item["warnings"]:
            warning_types[warning] += 1

    summary = {
        "tasks_root": os.path.abspath(args.tasks_root),
        "task_count": len(reports),
        "tasks_with_errors": len(tasks_with_errors),
        "tasks_with_warnings": len(tasks_with_warnings),
        "error_count": error_count,
        "warning_count": warning_count,
        "decision_counts": dict(decision_counts),
        "service_counts": dict(service_counts),
        "top_warning_types": warning_types.most_common(20),
    }

    if args.json:
        print(json.dumps({"summary": summary, "reports": reports}, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        if tasks_with_errors:
            print("\n[Task Errors]")
            for item in tasks_with_errors[:50]:
                print(f"- {item['path']}")
                for error in item["errors"]:
                    print(f"  error: {error}")
        if tasks_with_warnings:
            print("\n[Task Warnings]")
            for item in tasks_with_warnings[:50]:
                print(f"- {item['path']}")
                for warning in item["warnings"]:
                    print(f"  warning: {warning}")

    raise SystemExit(1 if tasks_with_errors else 0)


if __name__ == "__main__":
    main()
