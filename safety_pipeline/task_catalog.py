import os
from dataclasses import asdict, dataclass

from .service_registry import build_service_summary, get_service_spec
from .settings import REPO_ROOT

try:
    import yaml
except ModuleNotFoundError:
    yaml = None


TASKS_ROOT = os.path.join(REPO_ROOT, "tasks")


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    task: str
    service: str
    path: str
    relative_path: str

    def to_dict(self):
        return asdict(self)


def _load_yaml(path):
    if yaml is None:
        raise RuntimeError("pyyaml is not installed. Run: pip install pyyaml")
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def iter_task_files(tasks_root=TASKS_ROOT):
    for root, _, files in os.walk(tasks_root):
        for filename in sorted(files):
            if not filename.endswith((".yaml", ".yml")):
                continue
            yield os.path.join(root, filename)


def load_task_spec(path):
    config = _load_yaml(path)
    relative_path = os.path.relpath(path, REPO_ROOT)
    service = config.get("service") or "unassigned"
    return TaskSpec(
        task_id=config.get("id", os.path.splitext(os.path.basename(path))[0]),
        task=str(config.get("task", "")).strip(),
        service=service,
        path=os.path.abspath(path),
        relative_path=relative_path,
    )


def list_task_specs(tasks_root=TASKS_ROOT):
    tasks = [load_task_spec(path) for path in iter_task_files(tasks_root)]
    return sorted(tasks, key=lambda item: (item.service, item.task_id, item.relative_path))


def build_service_task_index(tasks_root=TASKS_ROOT):
    index = {
        item["service_id"]: {
            "service": item,
            "tasks": [],
        }
        for item in build_service_summary()
    }

    for task in list_task_specs(tasks_root):
        if task.service not in index:
            spec = get_service_spec(task.service)
            service_payload = (
                spec.to_dict()
                if spec
                else {
                    "service_id": task.service,
                    "display_name": task.service,
                    "domain": "unknown",
                    "status": "unregistered",
                    "default_backend": task.service or None,
                    "notes": "Task references a service that is not yet registered.",
                }
            )
            index[task.service] = {"service": service_payload, "tasks": []}
        index[task.service]["tasks"].append(task.to_dict())

    for payload in index.values():
        payload["tasks"].sort(key=lambda item: (item["task_id"], item["relative_path"]))
    return index
