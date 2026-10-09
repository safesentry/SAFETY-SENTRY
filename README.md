# Safety Sentry

Engineering and data release for **Safety Sentry: Context-Aware Human
Intervention via EXECUTE-ASK-REFUSE Routing**.

This repository contains the self-hosted service sandboxes, task library,
runtime tool adapters, and two-pass trajectory construction pipeline used to
build step-level safety-review records.

## Repository layout

```text
.
|-- safety_pipeline/   Runtime, service backends, tools, and Pass 1/Pass 2 code
|-- scripts/           Service setup/reset, validation, and batch runners
|-- docker/            Seed manifests and service-specific compose assets
|-- services/          Tool vocabularies and discovery indices
|-- tasks/             Task YAML files and persona-memory sidecars
|-- prompts/           Few-shot examples used by the task generator
|-- data/              Released train, test, and held-out Mailu records
`-- docs/              Task authoring, data, and licensing documentation
```

## Released data

| Split | Records | Purpose |
|---|---:|---|
| `data/train_7767.json` | 7,767 | In-domain training records |
| `data/test_1436.json` | 1,436 | In-domain evaluation records |
| `data/mailu_ood_198.json` | 198 | Held-out Mailu evaluation records |

The in-domain splits contain 9,203 step-level records. Each record contains a
chat-style `prompt`, a target `completion`, and a `meta` object. See
[`data/README.md`](data/README.md) and [`data/manifest.json`](data/manifest.json).

## Requirements

- Linux
- Python 3.10 or newer
- Docker Engine with the Compose plugin
- An OpenAI-compatible chat-completions endpoint for trajectory generation

Create an environment and install the runtime dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Set the API credentials and model names in `.env`. Service setup scripts write
local `.env.<service>.generated` files; these files are ignored by Git.

## Run one service and task

The following commands start and seed Gitea, reset it to the expected state,
and run one task through Pass 1:

```bash
bash scripts/setup_gitea_env.sh
bash scripts/reset_gitea_env.sh

python -m scripts.run_v2_pipeline --pass1-only \
  tasks/gitea/gitea-T2-onboard-vendor-staging-webhooks.yaml
```

The trace is written to:

```text
artifacts/v2_runs/<task-id>.trace.json
```

Run Pass 2 against the saved trace:

```bash
python -m scripts.run_v2_pipeline --pass2-from-trace \
  tasks/gitea/gitea-T2-onboard-vendor-staging-webhooks.yaml
```

This adds the reviewer outputs at:

```text
artifacts/v2_runs/<task-id>.sft.json
```

## Batch trajectory construction

Run the synthesis pipeline for one service:

```bash
python -m safety_pipeline.synthesis \
  --service gitea \
  --concurrency 4
```

Run several in-domain services in controlled groups:

```bash
python scripts/run_concurrent_synthesis.py \
  --services gitea rocketchat owncloud zammad \
  --batch-size 2 \
  --concurrency 4 \
  --reset
```

Mailu is retained as the held-out service. Its environment can be reset with:

```bash
bash scripts/reset_mailu_env.sh
```

## Generate a task

`docs/TASK_AUTHORING.md` and `prompts/few_shot/` contain the task-authoring
prompt and template-specific examples. Generate and validate a new task with:

```bash
python -m scripts.agent_task_generator \
  --service gitea \
  --template T2 \
  --provider deepseek

python -m scripts.check_v2_task --mode pre \
  tasks/gitea/<generated-task-id>.yaml
```

## Task validation

Validate all task specifications:

```bash
python scripts/check_tasks.py
```

## Security

All default credentials and identifiers in the sandbox assets are intended for
local, isolated research environments. Do not expose the containers directly
to an untrusted network, and do not reuse the example credentials in a real
deployment. Keep API keys in `.env`; never commit generated environment files.

## License and citation

Source code is released under the MIT License. Dataset provenance, usage
notes, and third-party terms are documented in
[`docs/DATA_AND_LICENSES.md`](docs/DATA_AND_LICENSES.md).

Citation metadata is available in [`CITATION.cff`](CITATION.cff).
