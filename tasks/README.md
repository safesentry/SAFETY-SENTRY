# Task library

Task specifications are grouped by service. Every YAML file declares at least
an `id`, `service`, and natural-language `task`. A matching
`<task-id>.persona.json` file, when present, supplies task-specific persona
memories for Pass 2.

The release includes the complete authoring library retained by the system:

| Service | Task YAML | Persona sidecars |
|---|---:|---:|
| ERPNext | 185 | 94 |
| Gitea | 133 | 82 |
| Mailu | 88 | 52 |
| NocoDB | 100 | 70 |
| OpenEMR | 144 | 95 |
| ownCloud | 129 | 89 |
| Rocket.Chat | 154 | 90 |
| Vaultwarden | 65 | 65 |
| Zammad | 133 | 96 |
| **Total** | **1,131** | **733** |

These are authoring assets, so their count is not the same quantity as the
unique-task count reported for the finalized step-level corpus. Generated
records and their split counts are documented under `data/`.

Validate the library with:

```bash
python scripts/check_tasks.py
python scripts/task_coverage_report.py
```

Use `TASK_TEMPLATE.yaml` as the minimal schema for a new task.
