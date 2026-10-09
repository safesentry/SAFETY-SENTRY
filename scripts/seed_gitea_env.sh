#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "${ROOT_DIR}/scripts/gitea_env_common.sh"

BASE_URL="${GITEA_BASE_URL:-http://localhost:3000}"
OWNER="${GITEA_OWNER:-root}"
ENV_FILE="${GITEA_ENV_FILE:-${ROOT_DIR}/.env.gitea.generated}"
MANIFEST_PATH="${GITEA_SEED_MANIFEST:-${ROOT_DIR}/docker/gitea/seed_manifest.json}"

TOKEN="${GITEA_ACCESS_TOKEN:-}"
if [ -z "${TOKEN}" ] && [ -f "${ENV_FILE}" ]; then
  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
  TOKEN="${GITEA_ACCESS_TOKEN:-}"
  BASE_URL="${GITEA_BASE_URL:-${BASE_URL}}"
  OWNER="${GITEA_OWNER:-${OWNER}}"
fi

if [ -z "${TOKEN}" ]; then
  echo "[seed] Missing GITEA_ACCESS_TOKEN. Source ${ENV_FILE} or export the token first."
  exit 1
fi

seed_gitea_data "${ROOT_DIR}" "${BASE_URL}" "${TOKEN}" "${OWNER}" "${MANIFEST_PATH}" seed
echo "[seed] Gitea seed complete"
