#!/usr/bin/env python3
"""Combine the four Mailu OOD axis files into a single eval JSON.

Each input file is in the same SFT shape as sft_test_final.json. We tag
each record with `meta.origin` ({baseline, cautious, permissive, adversarial})
so downstream metric scripts can slice predictions back by axis.

Output: artifacts/mailu_ood_combined_v2.json
"""
from __future__ import annotations
import argparse
import json
from collections import Counter
from pathlib import Path


SRC_DIR = Path(__file__).resolve().parents[1] / "artifacts"
AXES = ["baseline", "cautious", "permissive", "adversarial"]
OUT_PATH = SRC_DIR / "mailu_ood_combined_v2.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Combine generated Mailu OOD axis files into one evaluation JSON. "
            "Run scripts/run_mailu_pass2_with_persona.py first if the axis "
            "files are not present."
        )
    )
    parser.add_argument(
        "--artifacts-dir",
        default=str(SRC_DIR),
        help="Directory containing mailu_ood_<axis>.json files.",
    )
    parser.add_argument(
        "--out",
        default=str(OUT_PATH),
        help="Output path for the combined JSON.",
    )
    parser.add_argument(
        "--axes",
        nargs="+",
        default=AXES,
        help="Axis names to combine.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    src_dir = Path(args.artifacts_dir)
    out_path = Path(args.out)

    combined = []
    counts = {}
    for axis in args.axes:
        path = src_dir / f"mailu_ood_{axis}.json"
        if not path.exists():
            raise SystemExit(
                f"missing input: {path}\n"
                "Run scripts/run_mailu_pass2_with_persona.py first, or pass "
                "--artifacts-dir pointing at the generated axis files."
            )
        records = json.loads(path.read_text(encoding="utf-8"))
        for r in records:
            r = dict(r)
            meta = dict(r.get("meta") or {})
            meta["origin"] = axis
            r["meta"] = meta
            combined.append(r)
        counts[axis] = len(records)
        print(f"  {axis}: {len(records)} records")

    print(f"\nTotal combined: {len(combined)}")
    print(f"  by origin: {counts}")
    print(f"  gold distribution: {dict(Counter(r['meta'].get('decision') for r in combined))}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(combined, ensure_ascii=False), encoding="utf-8")
    print(f"\nWrote: {out_path}")


if __name__ == "__main__":
    main()
