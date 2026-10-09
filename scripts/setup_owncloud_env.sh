#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "${ROOT_DIR}/scripts/owncloud_env_common.sh"

COMPOSE_FILE="${OWNCLOUD_COMPOSE_FILE:-docker-compose.yml}"
COMPOSE_PATH="${ROOT_DIR}/${COMPOSE_FILE}"
CONTAINER_NAME="${OWNCLOUD_CONTAINER_NAME:-pipeline-owncloud}"
BASE_URL="${OWNCLOUD_BASE_URL:-https://localhost:9200}"
ADMIN_USER="${OWNCLOUD_ADMIN_USER:-admin}"
ADMIN_PASSWORD="${OWNCLOUD_ADMIN_PASSWORD:-Admin123!}"
PUBLIC_LINK_PASSWORD="${OWNCLOUD_PUBLIC_LINK_PASSWORD:-Share123!}"
ENV_FILE="${OWNCLOUD_ENV_FILE:-${ROOT_DIR}/.env.owncloud.generated}"
AUTO_SEED="${OWNCLOUD_AUTO_SEED:-true}"
MANIFEST_PATH="${OWNCLOUD_SEED_MANIFEST:-${ROOT_DIR}/docker/owncloud/seed_manifest.json}"

echo "[setup] Starting ownCloud oCIS container from ${COMPOSE_FILE} ..."
docker compose -f "${COMPOSE_PATH}" up -d owncloud

wait_for_owncloud_api "${BASE_URL}" "${ADMIN_USER}" "${ADMIN_PASSWORD}" 120 3 setup

if [ "${AUTO_SEED}" = "true" ]; then
  echo "[setup] Seeding ownCloud data ..."
  seed_owncloud_data "${ROOT_DIR}" "${BASE_URL}" "${ADMIN_USER}" "${ADMIN_PASSWORD}" "${MANIFEST_PATH}" setup
fi

write_owncloud_env_file "${ENV_FILE}" "${BASE_URL}" "${ADMIN_USER}" "${ADMIN_PASSWORD}" "${CONTAINER_NAME}" "${PUBLIC_LINK_PASSWORD}"

echo "[setup] ownCloud oCIS is ready"
echo "[setup] Env file written to ${ENV_FILE}"
echo "[setup] Export with: set -a; source ${ENV_FILE}; set +a"
