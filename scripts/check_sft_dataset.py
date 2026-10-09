#!/usr/bin/env python3

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from safety_pipeline.decision_tokens import DECISION_SPECIAL_TOKENS, parse_branch_response_text  # noqa: E402
from safety_pipeline.settings import DECISION_TOKEN_SFT_PATH  # noqa: E402


def load_dataset(path):
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    if not isinstance(payload, list):
        raise RuntimeError("SFT dataset must be a JSON array.")
    return payload


def _safe_json_loads(text):
    try:
        return json.loads(text)
    except Exception:
        return None


def _parse_completion(content):
    """Parse both target formats present in the released corpus."""
    content = str(content or "").lstrip()
    completion_format = "decision_first"
    if content.startswith("<think>"):
        _, separator, content = content.partition("</think>")
        if not separator:
            raise RuntimeError("Completion has an unclosed <think> block.")
        completion_format = "think_then_decision"
    decision, payload = parse_branch_response_text(content)
    return decision, payload, completion_format


def main():
    parser = argparse.ArgumentParser(description="Validate exported decision-token SFT dataset.")
    parser.add_argument("--dataset", default=DECISION_TOKEN_SFT_PATH, help="Path to decision_token_sft.json")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    args = parser.parse_args()

    records = load_dataset(args.dataset)
    errors = []
    decision_counts = Counter()
    completion_format_counts = Counter()
    service_counts = Counter()
    prompt_len_counts = Counter()
    duplicate_hashes = Counter()
    max_prompt_chars = 0
    max_completion_chars = 0
    long_observation_records = 0

    for index, record in enumerate(records):
        prompt = record.get("prompt")
        completion = record.get("completion")
        meta = record.get("meta") or {}

        if not isinstance(prompt, list) or not prompt:
            errors.append(f"record[{index}] missing prompt list")
            continue
        if not isinstance(completion, list) or len(completion) != 1:
            errors.append(f"record[{index}] completion must be a single-message list")
            continue

        prompt_len_counts[len(prompt)] += 1
        prompt_text = json.dumps(prompt, ensure_ascii=False)
        completion_text = json.dumps(completion, ensure_ascii=False)
        max_prompt_chars = max(max_prompt_chars, len(prompt_text))
        max_completion_chars = max(max_completion_chars, len(completion_text))
        duplicate_hashes[(prompt_text, completion_text)] += 1

        first_message = prompt[0]
        if first_message.get("role") != "system":
            errors.append(f"record[{index}] first prompt message must be system")

        completion_message = completion[0]
        if completion_message.get("role") != "assistant":
            errors.append(f"record[{index}] completion role must be assistant")
            continue

        completion_content = str(completion_message.get("content") or "")
        try:
            decision, payload, completion_format = _parse_completion(completion_content)
        except Exception as exc:
            errors.append(f"record[{index}] invalid completion: {exc}")
            continue
        decision_counts[decision] += 1
        completion_format_counts[completion_format] += 1

        service = str(meta.get("service") or "").strip()
        if service:
            service_counts[service] += 1

        if decision == "ask_human" and not str(payload.get("question") or "").strip():
            errors.append(f"record[{index}] ask_human completion is missing question")

        for message in prompt[1:]:
            if message.get("role") != "user":
                continue
            snapshot = _safe_json_loads(message.get("content") or "")
            if not isinstance(snapshot, dict):
                continue
            for prior_step in snapshot.get("prior_steps") or []:
                observation = str((prior_step or {}).get("observation") or "")
                if len(observation) > 400:
                    long_observation_records += 1
                    break

    report = {
        "dataset_path": os.path.abspath(args.dataset),
        "record_count": len(records),
        "error_count": len(errors),
        "decision_counts": dict(decision_counts),
        "completion_format_counts": dict(completion_format_counts),
        "service_counts": dict(service_counts),
        "prompt_length_distribution": dict(prompt_len_counts),
        "duplicate_record_count": sum(count - 1 for count in duplicate_hashes.values() if count > 1),
        "max_prompt_chars": max_prompt_chars,
        "max_completion_chars": max_completion_chars,
        "records_with_long_prior_step_observation": long_observation_records,
    }

    if args.json:
        print(json.dumps({"summary": report, "errors": errors}, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if errors:
            print("\n[SFT Errors]")
            for item in errors[:50]:
                print(f"- {item}")

    raise SystemExit(1 if errors else 0)


if __name__ == "__main__":
    main()
