import json
import os

from .settings import TRACE_SESSION_PATH

try:
    import fcntl
except ImportError:
    fcntl = None


def _normalize_session_record(record):
    if not isinstance(record, dict):
        return None
    session_cases = record.get("session_cases")
    if not isinstance(session_cases, list) or not session_cases:
        return None
    normalized = dict(record)
    normalized["session_cases"] = session_cases
    return normalized


def load_session_records(storage_path=TRACE_SESSION_PATH):
    records = []
    try:
        with open(storage_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    continue
                normalized = _normalize_session_record(parsed)
                if normalized is not None:
                    records.append(normalized)
    except FileNotFoundError:
        return []
    return records


def append_session_record(session_cases, run_id=None, storage_path=TRACE_SESSION_PATH):
    session_cases = list(session_cases or [])
    if not session_cases:
        return None
    record = {
        "task": session_cases[0].get("task", ""),
        "environment": session_cases[0].get("environment", ""),
        "session_cases": session_cases,
    }
    if run_id:
        record["run_id"] = str(run_id)
    os.makedirs(os.path.dirname(storage_path), exist_ok=True)
    with open(storage_path, "a", encoding="utf-8") as fh:
        if fcntl is not None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def load_session_cases(storage_path=TRACE_SESSION_PATH):
    return [
        record.get("session_cases") or []
        for record in load_session_records(storage_path=storage_path)
    ]
