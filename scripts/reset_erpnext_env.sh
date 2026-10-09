#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "${ROOT_DIR}/scripts/erpnext_env_common.sh"

COMPOSE_FILE="${ERPNEXT_COMPOSE_FILE:-${ROOT_DIR}/docker/erpnext/pwd.pipeline.yml}"
SHARED_DIR="${ERPNEXT_SHARED_DIR:-${ROOT_DIR}/docker/erpnext/shared}"
BASELINE_DIR="${ERPNEXT_BASELINE_DIR:-${ROOT_DIR}/docker/erpnext/baseline}"
SITE_NAME="${ERPNEXT_SITE_NAME:-frontend}"
BASE_URL="${ERPNEXT_BASE_URL:-http://localhost:8082}"
ADMIN_USER="${ERPNEXT_ADMIN_USER:-Administrator}"
ADMIN_PASSWORD="${ERPNEXT_ADMIN_PASSWORD:-admin}"
DB_ROOT_PASSWORD="${ERPNEXT_DB_ROOT_PASSWORD:-admin}"
ENV_FILE="${ERPNEXT_ENV_FILE:-${ROOT_DIR}/.env.erpnext.generated}"
BACKEND_CONTAINER="${ERPNEXT_BACKEND_CONTAINER:-pipeline-erpnext-backend}"
DB_CONTAINER="${ERPNEXT_DB_CONTAINER:-pipeline-erpnext-db}"
SITE_CONFIG_PATH="${SHARED_DIR}/sites/${SITE_NAME}/site_config.json"

if [ ! -d "${BASELINE_DIR}" ]; then
  echo "[reset] Missing ERPNext baseline. Falling back to setup."
  bash "${ROOT_DIR}/scripts/setup_erpnext_env.sh"
  exit 0
fi

echo "[reset] Restoring ERPNext baseline ..."
docker compose -f "${COMPOSE_FILE}" down --remove-orphans >/dev/null 2>&1 || true
mkdir -p "${SHARED_DIR}"
docker compose -f "${COMPOSE_FILE}" up -d db redis-cache redis-queue
wait_for_container_health "${DB_CONTAINER}" 300 5 reset
restore_erpnext_shared_state "${SHARED_DIR}/sites" "${SITE_CONFIG_PATH}" "${DB_CONTAINER}" "${BASELINE_DIR}" "${DB_ROOT_PASSWORD}" reset
docker compose -f "${COMPOSE_FILE}" up -d backend websocket frontend queue-short queue-long scheduler
wait_for_erpnext_http "${BASE_URL}" 480 5 reset
write_erpnext_env_file "${ENV_FILE}" "${BASE_URL}" "${ADMIN_USER}" "${ADMIN_PASSWORD}" "${SITE_NAME}" "${COMPOSE_FILE}" "${SHARED_DIR}" "${BASELINE_DIR}" "${BACKEND_CONTAINER}"

echo "[reset] ERPNext baseline restored"
