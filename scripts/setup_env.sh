#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
exec bash "${ROOT_DIR}/scripts/setup_gitea_env.sh"
