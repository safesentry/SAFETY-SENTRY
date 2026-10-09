#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "${ROOT_DIR}/scripts/rocketchat_env_common.sh"

COMPOSE_FILE="${ROCKETCHAT_COMPOSE_FILE:-docker-compose.yml}"
COMPOSE_PATH="${ROOT_DIR}/${COMPOSE_FILE}"
CONTAINER_NAME="${ROCKETCHAT_CONTAINER_NAME:-pipeline-rocketchat}"
MONGO_CONTAINER="${ROCKETCHAT_MONGO_CONTAINER:-pipeline-rocketchat-mongo}"
BASE_URL="${ROCKETCHAT_BASE_URL:-http://localhost:3100}"
ADMIN_USER="${ROCKETCHAT_ADMIN_USER:-admin}"
ADMIN_PASSWORD="${ROCKETCHAT_ADMIN_PASSWORD:-Admin123!}"
ENV_FILE="${ROCKETCHAT_ENV_FILE:-${ROOT_DIR}/.env.rocketchat.generated}"
MANIFEST_PATH="${ROCKETCHAT_SEED_MANIFEST:-${ROOT_DIR}/docker/rocketchat/seed_manifest.json}"

echo "[reset] Stopping Rocket.Chat containers ..."
docker compose -f "${COMPOSE_PATH}" stop rocketchat rocketchat-mongo 2>/dev/null || true

echo "[reset] Removing containers and volumes ..."
docker compose -f "${COMPOSE_PATH}" rm -f -v rocketchat rocketchat-mongo 2>/dev/null || true
docker volume rm -f pipeline_rocketchat_uploads pipeline_rocketchat_mongo_data 2>/dev/null || true

echo "[reset] Recreating Rocket.Chat containers ..."
docker compose -f "${COMPOSE_PATH}" up -d rocketchat-mongo rocketchat

wait_for_rocketchat_api "${BASE_URL}" 180 3 reset

echo "[reset] Re-seeding Rocket.Chat data ..."
seed_rocketchat_data "${ROOT_DIR}" "${BASE_URL}" "${ADMIN_USER}" "${ADMIN_PASSWORD}" "${MANIFEST_PATH}" reset

write_rocketchat_env_file "${ENV_FILE}" "${BASE_URL}" "${ADMIN_USER}" "${ADMIN_PASSWORD}" "${CONTAINER_NAME}" "${MONGO_CONTAINER}"

echo "[reset] Rocket.Chat reset complete"
echo "[reset] Export with: set -a; source ${ENV_FILE}; set +a"
