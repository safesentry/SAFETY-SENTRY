#!/usr/bin/env python3
"""Run two-pass synthesis across multiple services concurrently.

For each group of N services (default 3):
  1. reset_<service>_env.sh is invoked sequentially
  2. python -m safety_pipeline.synthesis --service <svc> --concurrency <K>
     is launched as a subprocess for each service; the N subprocesses run
     in parallel
  3. after all N finish, the next group starts

Each service writes its decision-token SFT to a service-specific file
(decision_token_sft.<service>.json) to avoid trampling.

Usage:
    python scripts/run_concurrent_synthesis.py \
        --services gitea rocketchat owncloud zammad erpnext openemr vaultwarden mailu \
        --batch-size 3 --concurrency 4 --reset

    # skip reset (already reset manually):
    python scripts/run_concurrent_synthesis.py --services gitea ... --no-reset
"""

import argparse
import datetime
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(REPO_ROOT, "artifacts", "batch_logs")


def _ts():
    return datetime.datetime.now().strftime("%H:%M:%S")


def _reset_service(service):
    script = os.path.join(REPO_ROOT, "scripts", f"reset_{service}_env.sh")
    if not os.path.isfile(script):
        print(f"[{_ts()}] [{service}] reset: SKIP (no {script})", flush=True)
        return True
    print(f"[{_ts()}] [{service}] reset: starting", flush=True)
    log_path = os.path.join(LOG_DIR, f"reset_{service}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(log_path, "w") as fh:
        proc = subprocess.run(
            ["bash", script],
            cwd=REPO_ROOT,
            stdout=fh,
            stderr=subprocess.STDOUT,
            check=False,
        )
    ok = proc.returncode == 0
    status = "OK" if ok else f"FAIL (rc={proc.returncode})"
    print(f"[{_ts()}] [{service}] reset: {status}  (log: {log_path})", flush=True)
    return ok


def _run_synthesis(service, concurrency):
    log_path = os.path.join(LOG_DIR, f"{service}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
    os.makedirs(LOG_DIR, exist_ok=True)
    cmd = [
        sys.executable, "-u", "-m", "safety_pipeline.synthesis",
        "--service", service,
        "--concurrency", str(concurrency),
        "--out-suffix", service,
    ]
    print(f"[{_ts()}] [{service}] synthesis: starting (concurrency={concurrency}, log={log_path})", flush=True)
    start = time.time()
    with open(log_path, "w") as fh:
        proc = subprocess.run(
            cmd,
            cwd=REPO_ROOT,
            stdout=fh,
            stderr=subprocess.STDOUT,
            check=False,
        )
    elapsed = time.time() - start
    ok = proc.returncode == 0
    mins = elapsed / 60.0
    status = "OK" if ok else f"FAIL (rc={proc.returncode})"
    print(f"[{_ts()}] [{service}] synthesis: {status}  ({mins:.1f} min, log: {log_path})", flush=True)
    return {"service": service, "ok": ok, "elapsed_sec": elapsed, "log": log_path}


def _run_group(services, concurrency, do_reset):
    # Resets: sequential. They are fast and mostly docker compose churn; running
    # them serially keeps docker daemon happy and makes logs readable.
    if do_reset:
        print(f"\n[{_ts()}] === resetting group: {services} ===", flush=True)
        for svc in services:
            if not _reset_service(svc):
                print(f"[{_ts()}] [{svc}] reset failed; continuing anyway", flush=True)

    print(f"\n[{_ts()}] === running synthesis in parallel: {services} ===", flush=True)
    results = []
    with ThreadPoolExecutor(max_workers=len(services)) as pool:
        futures = [pool.submit(_run_synthesis, svc, concurrency) for svc in services]
        for fut in as_completed(futures):
            results.append(fut.result())
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--services",
        nargs="+",
        required=True,
        help="Service names to run, e.g. gitea rocketchat owncloud",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=3,
        help="How many services run in parallel per group. Default: 3.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=4,
        help="Task-level concurrency inside each service subprocess. Default: 4.",
    )
    reset_group = parser.add_mutually_exclusive_group()
    reset_group.add_argument("--reset", dest="reset", action="store_true", default=True, help="Run reset_<svc>_env.sh before each service (default).")
    reset_group.add_argument("--no-reset", dest="reset", action="store_false", help="Skip environment reset.")
    args = parser.parse_args()

    services = list(args.services)
    print(f"[{_ts()}] orchestrator start: {len(services)} services, batch_size={args.batch_size}, concurrency={args.concurrency}, reset={args.reset}", flush=True)

    all_results = []
    for i in range(0, len(services), args.batch_size):
        group = services[i:i + args.batch_size]
        group_results = _run_group(group, args.concurrency, args.reset)
        all_results.extend(group_results)

    print(f"\n[{_ts()}] === summary ===", flush=True)
    for r in all_results:
        status = "OK  " if r["ok"] else "FAIL"
        print(f"  {status}  {r['service']:12s}  {r['elapsed_sec']/60:5.1f} min  {r['log']}", flush=True)

    failed = [r for r in all_results if not r["ok"]]
    if failed:
        print(f"\n[{_ts()}] {len(failed)} service(s) failed", flush=True)
        sys.exit(1)
    print(f"\n[{_ts()}] all {len(all_results)} service(s) completed", flush=True)


if __name__ == "__main__":
    main()
