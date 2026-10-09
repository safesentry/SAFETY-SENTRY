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

echo "[setup] Starting Rocket.Chat containers from ${COMPOSE_FILE} ..."
docker compose -f "${COMPOSE_PATH}" up -d rocketchat-mongo rocketchat

wait_for_rocketchat_api "${BASE_URL}" 180 3 setup

echo "[setup] Seeding Rocket.Chat data ..."
seed_rocketchat_data "${ROOT_DIR}" "${BASE_URL}" "${ADMIN_USER}" "${ADMIN_PASSWORD}" "${MANIFEST_PATH}" setup

write_rocketchat_env_file "${ENV_FILE}" "${BASE_URL}" "${ADMIN_USER}" "${ADMIN_PASSWORD}" "${CONTAINER_NAME}" "${MONGO_CONTAINER}"

echo "[setup] Rocket.Chat is ready"
echo "[setup] Env file written to ${ENV_FILE}"
echo "[setup] Export with: set -a; source ${ENV_FILE}; set +a"
