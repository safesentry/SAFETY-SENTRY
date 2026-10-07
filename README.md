# Safety Sentry: Context-Aware Human Intervention via EXECUTE-ASK-REFUSE Routing

<!-- Project Page URL to be supplied by the authors. -->

[![arXiv](https://img.shields.io/badge/arXiv-2607.13594-B31B1B?logo=arxiv)](https://arxiv.org/abs/2607.13594)
[![Hugging Face](https://img.shields.io/badge/%F0%9F%A4%97%20Hugging%20Face-Safety%20Sentry-FFD21E)](https://huggingface.co/papers/2607.13594)
![Project Page](https://img.shields.io/badge/Project%20Page-blue?logo=googlechrome&logoColor=white)

## 🌟 Introduction

**Safety Sentry** is a 4B guard model that reviews proposed agent tool calls
and routes them into **EXECUTE**, **ASK**, or **REFUSE**. Decisions take the
user’s task, interaction history, and user memory into account. A decoding
threshold adjusts the balance between autonomous execution and human
confirmation.

This repository provides service sandboxes, task definitions, trajectory
construction tools, and training and evaluation data.

## ⚙️ Method Overview

The framework combines persona-conditioned trajectory annotation and guard
training with per-step routing at deployment.

<p align="center">
  <img src="figures/method.png" alt="Safety Sentry training and deployment framework, Figure 3 of the paper" width="850">
</p>

<p align="center"><em><a href="https://arxiv.org/pdf/2607.13594v1#page=4">Figure 3</a> · Training and deployment framework.</em></p>

## 📊 Evaluation

### Main results

Safety Sentry reaches **91.02% accuracy** and **90.92% Macro-F1** on the
in-distribution test set at the balanced threshold, τ = 0.68.

<p align="center">
  <img src="figures/main-results.png" alt="In-distribution comparison of Safety Sentry and baseline models, Table 1 of the paper" width="850">
</p>

<p align="center"><em><a href="https://arxiv.org/pdf/2607.13594v1#page=6">Table 1</a> · In-distribution evaluation.</em></p>

### Generalization to Mailu

Safety Sentry achieves **85.35% accuracy** on the held-out Mailu service.

<p align="center">
  <img src="figures/ood-results.png" alt="Held-out Mailu comparison of Safety Sentry and baseline models, Table 2 of the paper" width="600">
</p>

<p align="center"><em><a href="https://arxiv.org/pdf/2607.13594v1#page=8">Table 2</a> · Held-out service evaluation.</em></p>

## 💻 Usage

### Installation

Requirements: **Linux**, **Python 3.10+**, **Docker Engine with Compose**, and
an **OpenAI-compatible chat-completions endpoint**.

```bash
git clone https://github.com/safesentry/SAFETY-SENTRY.git
cd SAFETY-SENTRY

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Set `OPENAI_API_KEY` and `OPENAI_BASE_URL` in `.env`. Choose model names for
`OPENAI_MODEL`, `OPENAI_MODEL_PASS1`, and `V2_REVIEWER_MODEL` that are available
at your endpoint. See [`.env.example`](.env.example) for the configuration fields.

### Quickstart

Start and seed the Gitea environment:

```bash
bash scripts/setup_gitea_env.sh
```

The setup script starts the root Docker Compose stack and writes service
connection settings to `.env.gitea.generated`, which the runtime loads automatically.

Run a task through trajectory collection and persona-aware review:

```bash
python -m scripts.run_v2_pipeline \
  tasks/gitea/gitea-T2-onboard-vendor-staging-webhooks.yaml
```

Outputs are saved to `artifacts/v2_runs/<task-id>.trace.json` (trajectory and
review decisions) and `<task-id>.sft.json` (training examples in the same directory).
Each task used by this runner has a matching `<task-id>.persona.json` file.

To run the two stages separately, add `--pass1-only` to collect a trajectory,
then `--pass2-from-trace` to review it. To recreate and reseed Gitea before
another collection run:

```bash
bash scripts/reset_gitea_env.sh
```

### Batch synthesis

After setting up the relevant service environments, run the general synthesis
pipeline across services:

```bash
python scripts/run_concurrent_synthesis.py \
  --services gitea rocketchat owncloud zammad \
  --batch-size 2 \
  --concurrency 4 \
  --reset
```

Logs are saved under `artifacts/batch_logs/`, with per-service exports at
`artifacts/decision_token_sft.<service>.json`. For persona-aware v2 batches,
pass multiple task YAML paths to `python -m scripts.run_v2_pipeline`.

### Data and custom tasks

Use the [data guide](data/README.md) to load the released datasets and the
[task-authoring guide](docs/TASK_AUTHORING.md) to add tasks. Validate task
specifications with:

```bash
python scripts/check_tasks.py
```

## 🤝 Acknowledgements

We thank the authors of When2Call, AT-Bench, AgentHarm, TS-Bench, R-Judge,
and TheAgentCompany, along with the maintainers of the self-hosted services.
See the [data and third-party notices](docs/DATA_AND_LICENSES.md).

## ⚖️ License

Source code is released under the [MIT License](LICENSE). Third-party terms
are documented in [Data and Third-Party Notices](docs/DATA_AND_LICENSES.md).

## 💬 Contact

For questions about the code or data, open a
[GitHub issue](https://github.com/safesentry/SAFETY-SENTRY/issues).

## 📝 Citation

If you use Safety Sentry in your research, please cite the
[paper](https://arxiv.org/abs/2607.13594):

```bibtex
@misc{chen2026safetysentry,
  title = {Safety Sentry: Context-Aware Human Intervention via EXECUTE-ASK-REFUSE Routing},
  author = {Tianyu Chen and Chujia Hu and Wenjie Wang},
  year = {2026},
  eprint = {2607.13594},
  archivePrefix = {arXiv},
  primaryClass = {cs.AI},
  url = {https://arxiv.org/abs/2607.13594}
}
```
