"""Merge multiple SFT JSON files into one, deduplicating by (prompt, completion).

Usage:
    python scripts/merge_sft_files.py \
        artifacts/decision_token_sft.A.json \
        artifacts/decision_token_sft.B.json \
        -o artifacts/decision_token_sft.merged.json
"""

import argparse
import hashlib
import json


def fingerprint(record):
    user_msg = record["prompt"][-1]["content"] if record.get("prompt") else ""
    comp = record["completion"][0]["content"] if record.get("completion") else ""
    return hashlib.sha256((user_msg + "||" + comp).encode("utf-8")).hexdigest()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("inputs", nargs="+")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--no-dedupe", action="store_true")
    args = p.parse_args()

    all_records = []
    seen = set()
    per_input = []

    for path in args.inputs:
        with open(path) as f:
            d = json.load(f)
        kept = 0
        dup = 0
        for r in d:
            if not args.no_dedupe:
                fp = fingerprint(r)
                if fp in seen:
                    dup += 1
                    continue
                seen.add(fp)
            all_records.append(r)
            kept += 1
        per_input.append((path, len(d), kept, dup))

    print("=== merge ===")
    for path, total, kept, dup in per_input:
        print(f"  {path}: total={total} kept={kept} dup={dup}")
    print(f"  → wrote {len(all_records)} records to {args.output}")

    with open(args.output, "w") as f:
        json.dump(all_records, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
