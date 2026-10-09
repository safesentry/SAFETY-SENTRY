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
AUTO_SEED="${NOCODB_AUTO_SEED:-true}"
MANIFEST_PATH="${NOCODB_SEED_MANIFEST:-${ROOT_DIR}/docker/nocodb/seed_manifest.json}"

echo "[setup] Starting NocoDB containers from ${COMPOSE_FILE} ..."
docker compose -f "${COMPOSE_PATH}" up -d nocodb-db nocodb

wait_for_nocodb_api "${BASE_URL}" 120 2 setup

echo "[setup] Signing up admin user and seeding data ..."
SEED_OUTPUT=$(
  NOCODB_BASE_URL="${BASE_URL}" \
  NOCODB_ADMIN_EMAIL="${ADMIN_EMAIL}" \
  NOCODB_ADMIN_PASSWORD="${ADMIN_PASSWORD}" \
  NOCODB_API_TOKEN="" \
  NOCODB_SEED_MANIFEST="${MANIFEST_PATH}" \
  python3 "${ROOT_DIR}/docker/nocodb/scripts/seed_nocodb_data.py"
)
echo "${SEED_OUTPUT}"

# Extract API token from seed script output
API_TOKEN=$(echo "${SEED_OUTPUT}" | grep '^NOCODB_API_TOKEN=' | cut -d'=' -f2- || true)

write_nocodb_env_file "${ENV_FILE}" "${BASE_URL}" "${API_TOKEN}" "${ADMIN_EMAIL}" "${ADMIN_PASSWORD}" "${CONTAINER_NAME}"

echo "[setup] NocoDB is ready"
echo "[setup] Env file written to ${ENV_FILE}"
echo "[setup] Export with: set -a; source ${ENV_FILE}; set +a"
