#!/usr/bin/env python3
"""Run pass1 + pass2 on all mailu tasks (50), producing 4 OOD eval files:

  mailu_ood_baseline.json    - no user_memory
  mailu_ood_cautious.json    - cautious persona memory
  mailu_ood_permissive.json  - permissive persona memory
  mailu_ood_adversarial.json - adversarial persona memory

Each file follows the standard SFT export schema (prompt/completion/meta).
The four files share the same underlying pass1 traces; only pass2's
user_memory_per_step differs, isolating the memory-conditioning effect.

Output goes to artifacts/.
"""
from __future__ import annotations
import argparse
import json, os, sys, uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import threading

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))
from safety_pipeline.runtime import load_task_file, export_decision_token_sft
from safety_pipeline.settings import set_pipeline_env, DECISION_TOKEN_SFT_PATH
from safety_pipeline.session_store import append_session_record
from safety_pipeline.synthesis.__main__ import (
    _build_synthetic_refuse_trace, _is_refuse_task, _maybe_inject_canonical
)
from safety_pipeline.synthesis.pass1_runner import run_task_pure, pass1_steps
from safety_pipeline.synthesis.pass2_reviewer import review_trace
from safety_pipeline.synthesis.trajectory_writer import splice

AXES = ['baseline', 'cautious', 'permissive', 'adversarial']

print_lock = threading.Lock()
def log(msg):
    with print_lock:
        print(msg, flush=True)


def load_persona_memory(task_file: str, axis: str) -> list[str] | None:
    if axis == 'baseline':
        return None
    pjson = task_file.replace('.yaml', '.persona.json')
    if not os.path.exists(pjson):
        return None
    personas = json.load(open(pjson)).get('personas', {})
    for name, p in personas.items():
        if p.get('axis') == axis:
            return p.get('items', [])
    return None


def run_one_task(task_file: str, axis: str, run_id: str):
    try:
        cfg = load_task_file(task_file)
        if cfg.get('service'):
            set_pipeline_env(cfg['service'])
        # pass1
        if _is_refuse_task(task_file, cfg):
            pass1_trace = _build_synthetic_refuse_trace(cfg)
        else:
            pass1_trace = run_task_pure(cfg)
            pass1_trace = _maybe_inject_canonical(pass1_trace, cfg)

        # build memory list per step
        memory = load_persona_memory(task_file, axis)
        steps_count = len(pass1_steps(pass1_trace))
        user_memory_per_step = [memory] * steps_count if memory else None

        # pass2 with memory
        decisions = review_trace(pass1_trace, task_file=task_file,
                                 user_memory_per_step=user_memory_per_step)
        synthetic_trace = splice(pass1_trace, decisions, cfg)

        cases = synthetic_trace.get('session_cases', [])
        # tag cases with axis so export can later distinguish
        for c in cases:
            c.setdefault('meta', {})['persona_axis'] = axis
            c['meta']['service'] = 'mailu'  # ensure
        append_session_record(cases, run_id=run_id)
        return {'task': os.path.basename(task_file), 'axis': axis,
                'final': synthetic_trace.get('final_status', ''),
                'n_cases': len(cases)}
    except Exception as e:
        return {'task': os.path.basename(task_file), 'axis': axis,
                'error': f'{type(e).__name__}: {e}'}


def task_has_axis(task_file: str, axis: str) -> bool:
    """True if the task should run under this axis. baseline always runs;
    a memory axis runs only when the sidecar carries that persona block.
    Post-redesign, Flip tasks have cautious+permissive and Stability tasks
    have cautious+adversarial, so this skips the ~82 dead (task, axis)
    pairs instead of emitting baseline-equivalent records into the wrong
    per-axis file."""
    if axis == 'baseline':
        return True
    return bool(load_persona_memory(task_file, axis))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Regenerate Mailu OOD pass2 axis files from Mailu task YAMLs. "
            "This runs the LLM-backed pass1/pass2 pipeline."
        )
    )
    parser.add_argument(
        "--tasks-dir",
        default=str(ROOT_DIR / "tasks" / "mailu"),
        help="Directory containing Mailu task YAML/persona files.",
    )
    parser.add_argument(
        "--out-dir",
        default=str(ROOT_DIR / "artifacts"),
        help="Directory to write mailu_ood_<axis>.json files.",
    )
    parser.add_argument(
        "--axes",
        nargs="+",
        default=AXES,
        choices=AXES,
        help="Persona axes to regenerate.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=6,
        help="Task-level worker count.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    mailu_dir = Path(args.tasks_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    task_files = sorted(str(mailu_dir / f) for f in os.listdir(mailu_dir) if f.endswith('.yaml'))
    log(f'Mailu tasks: {len(task_files)}')

    for axis in args.axes:
        run_id = f'mailu-{axis}-{uuid.uuid4().hex[:6]}'
        axis_tasks = [tf for tf in task_files if task_has_axis(tf, axis)]
        log(f'\n=== axis={axis} run_id={run_id} ({len(axis_tasks)} tasks) ===')
        results = []
        with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
            futs = [ex.submit(run_one_task, tf, axis, run_id) for tf in axis_tasks]
            for f in as_completed(futs):
                r = f.result()
                results.append(r)
                if 'error' in r:
                    log(f'  ERR {r["task"]}: {r["error"]}')
                else:
                    log(f'  OK  {r["task"]}: {r["n_cases"]} cases, final={r["final"]}')
        # export
        out_path = out_dir / f'mailu_ood_{axis}.json'
        export_decision_token_sft(output_path=str(out_path), verbose=False, run_id=run_id)
        log(f'  → {out_path}')


if __name__ == '__main__':
    main()
