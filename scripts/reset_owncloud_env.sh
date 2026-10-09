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
MANIFEST_PATH="${OWNCLOUD_SEED_MANIFEST:-${ROOT_DIR}/docker/owncloud/seed_manifest.json}"

echo "[reset] Stopping ownCloud container ..."
docker compose -f "${COMPOSE_PATH}" stop owncloud 2>/dev/null || true

echo "[reset] Removing ownCloud container and volume ..."
docker compose -f "${COMPOSE_PATH}" rm -f -v owncloud 2>/dev/null || true
docker volume rm -f pipeline_owncloud_data 2>/dev/null || true

echo "[reset] Recreating ownCloud container ..."
docker compose -f "${COMPOSE_PATH}" up -d owncloud

wait_for_owncloud_api "${BASE_URL}" "${ADMIN_USER}" "${ADMIN_PASSWORD}" 120 3 reset

echo "[reset] Re-seeding ownCloud data ..."
seed_owncloud_data "${ROOT_DIR}" "${BASE_URL}" "${ADMIN_USER}" "${ADMIN_PASSWORD}" "${MANIFEST_PATH}" reset

write_owncloud_env_file "${ENV_FILE}" "${BASE_URL}" "${ADMIN_USER}" "${ADMIN_PASSWORD}" "${CONTAINER_NAME}" "${PUBLIC_LINK_PASSWORD}"

echo "[reset] ownCloud reset complete"
echo "[reset] Export with: set -a; source ${ENV_FILE}; set +a"
