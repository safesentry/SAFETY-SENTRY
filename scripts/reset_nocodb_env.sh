#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "${ROOT_DIR}/scripts/nocodb_env_common.sh"

COMPOSE_FILE="${NOCODB_COMPOSE_FILE:-docker-compose.yml}"
COMPOSE_PATH="${ROOT_DIR}/${COMPOSE_FILE}"
CONTAINER_NAME="${NOCODB_CONTAINER_NAME:-pipeline-nocodb}"
BASE_URL="${NOCODB_BASE_URL:-http://localhost:8080}"
ADMIN_EMAIL="${NOCODB_ADMIN_EMAIL:-admin@example.com}"
ADMIN_PASSWORD="${NOCODB_ADMIN_PASSWORD:-Admin123!}"
ENV_FILE="${NOCODB_ENV_FILE:-${ROOT_DIR}/.env.nocodb.generated}"
MANIFEST_PATH="${NOCODB_SEED_MANIFEST:-${ROOT_DIR}/docker/nocodb/seed_manifest.json}"

echo "[reset] Stopping NocoDB containers ..."
docker compose -f "${COMPOSE_PATH}" stop nocodb nocodb-db 2>/dev/null || true

echo "[reset] Removing NocoDB containers and volumes ..."
docker compose -f "${COMPOSE_PATH}" rm -f -v nocodb nocodb-db 2>/dev/null || true
docker volume rm -f pipeline_nocodb_data pipeline_nocodb_pg_data 2>/dev/null || true

echo "[reset] Recreating NocoDB containers ..."
docker compose -f "${COMPOSE_PATH}" up -d nocodb-db nocodb

wait_for_nocodb_api "${BASE_URL}" 120 2 reset

echo "[reset] Re-seeding NocoDB data ..."
SEED_OUTPUT=$(
  NOCODB_BASE_URL="${BASE_URL}" \
  NOCODB_ADMIN_EMAIL="${ADMIN_EMAIL}" \
  NOCODB_ADMIN_PASSWORD="${ADMIN_PASSWORD}" \
  NOCODB_API_TOKEN="" \
  NOCODB_SEED_MANIFEST="${MANIFEST_PATH}" \
  python3 "${ROOT_DIR}/docker/nocodb/scripts/seed_nocodb_data.py"
)
echo "${SEED_OUTPUT}"

API_TOKEN=$(echo "${SEED_OUTPUT}" | grep '^NOCODB_API_TOKEN=' | cut -d'=' -f2- || true)

write_nocodb_env_file "${ENV_FILE}" "${BASE_URL}" "${API_TOKEN}" "${ADMIN_EMAIL}" "${ADMIN_PASSWORD}" "${CONTAINER_NAME}"

echo "[reset] NocoDB reset complete"
echo "[reset] Export with: set -a; source ${ENV_FILE}; set +a"
