#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "${ROOT_DIR}/scripts/vaultwarden_env_common.sh"

COMPOSE_PATH="${VAULTWARDEN_COMPOSE_PATH:-${ROOT_DIR}/docker/vaultwarden/docker-compose.yml}"
BASE_URL="${VAULTWARDEN_BASE_URL:-http://localhost:8093}"
ADMIN_TOKEN="${VAULTWARDEN_ADMIN_TOKEN:-admin-pipeline-token-dev}"
ENV_FILE="${VAULTWARDEN_ENV_FILE:-${ROOT_DIR}/.env.vaultwarden.generated}"
MANIFEST_PATH="${VAULTWARDEN_SEED_MANIFEST:-${ROOT_DIR}/docker/vaultwarden/seed_manifest.json}"
STATE_FILE="${VAULTWARDEN_STATE_FILE:-${ROOT_DIR}/docker/vaultwarden/shared/pipeline_seed_state.json}"
CONTAINER_NAME="${VAULTWARDEN_CONTAINER_NAME:-pipeline-vaultwarden}"

echo "[reset] Recreating Vaultwarden container and fixture state ..."
docker compose -f "${COMPOSE_PATH}" down -v --remove-orphans
docker compose -f "${COMPOSE_PATH}" up -d

wait_for_vaultwarden_alive "${BASE_URL}" 120 2 reset
seed_vaultwarden_data "${ROOT_DIR}" "${MANIFEST_PATH}" "${STATE_FILE}" reset
write_vaultwarden_env_file "${ENV_FILE}" "${BASE_URL}" "${ADMIN_TOKEN}" "${STATE_FILE}" "${MANIFEST_PATH}" "${CONTAINER_NAME}"

echo "[reset] Vaultwarden reset complete"
