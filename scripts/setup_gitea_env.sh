#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "${ROOT_DIR}/scripts/gitea_env_common.sh"

RUNTIME="$(detect_gitea_container_runtime)"
COMPOSE_FILE="${GITEA_COMPOSE_FILE:-docker-compose.yml}"
COMPOSE_PATH="$(resolve_repo_path "${ROOT_DIR}" "${COMPOSE_FILE}")"
CONTAINER_NAME="${GITEA_CONTAINER_NAME:-pipeline-gitea}"
BASE_URL="${GITEA_BASE_URL:-http://localhost:3000}"
ADMIN_USERNAME="${GITEA_ADMIN_USERNAME:-root}"
ADMIN_PASSWORD="${GITEA_ADMIN_PASSWORD:-root123456}"
ADMIN_EMAIL="${GITEA_ADMIN_EMAIL:-root@example.com}"
OWNER="${GITEA_OWNER:-${ADMIN_USERNAME}}"
TOKEN_NAME="${GITEA_TOKEN_NAME:-pipeline-bootstrap-$(date +%s)}"
TOKEN_SCOPES="${GITEA_TOKEN_SCOPES:-all}"
ENV_FILE="${GITEA_ENV_FILE:-${ROOT_DIR}/.env.gitea.generated}"
AUTO_SEED="${GITEA_AUTO_SEED:-true}"
MANIFEST_PATH="${GITEA_SEED_MANIFEST:-${ROOT_DIR}/docker/gitea/seed_manifest.json}"

start_gitea_service "${RUNTIME}" "${ROOT_DIR}" "${COMPOSE_PATH}" "${CONTAINER_NAME}" "${BASE_URL}" setup

wait_for_gitea_api "${BASE_URL}" 120 2 setup
ensure_gitea_admin_user "${RUNTIME}" "${CONTAINER_NAME}" "${ADMIN_USERNAME}" "${ADMIN_PASSWORD}" "${ADMIN_EMAIL}" setup

echo "[setup] Creating a fresh access token ..."
TOKEN="$(create_gitea_access_token "${BASE_URL}" "${ADMIN_USERNAME}" "${ADMIN_PASSWORD}" "${TOKEN_NAME}" "${TOKEN_SCOPES}")"
VERIFY_CODE="$(verify_gitea_token "${BASE_URL}" "${TOKEN}")"
if [ "${VERIFY_CODE}" != "200" ]; then
  echo "[setup] Token verification failed with HTTP ${VERIFY_CODE}"
  exit 1
fi

write_gitea_env_file "${ENV_FILE}" "${BASE_URL}" "${TOKEN}" "${OWNER}" "${CONTAINER_NAME}"

if [ "${AUTO_SEED}" = "true" ]; then
  seed_gitea_data "${ROOT_DIR}" "${BASE_URL}" "${TOKEN}" "${OWNER}" "${MANIFEST_PATH}" setup
fi

echo "[setup] Gitea is ready"
echo "[setup] Runtime: ${RUNTIME}"
echo "[setup] Env file written to ${ENV_FILE}"
echo "[setup] Export with: set -a; source ${ENV_FILE}; set +a"
