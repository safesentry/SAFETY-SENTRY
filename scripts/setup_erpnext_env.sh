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
MANIFEST_PATH="${ERPNEXT_SEED_MANIFEST:-${ROOT_DIR}/docker/erpnext/seed_manifest.json}"
BACKEND_CONTAINER="${ERPNEXT_BACKEND_CONTAINER:-pipeline-erpnext-backend}"
DB_CONTAINER="${ERPNEXT_DB_CONTAINER:-pipeline-erpnext-db}"
CREATE_SITE_CONTAINER="${ERPNEXT_CREATE_SITE_CONTAINER:-pipeline-erpnext-create-site}"
SITE_CONFIG_PATH="${SHARED_DIR}/sites/${SITE_NAME}/site_config.json"

echo "[setup] Rebuilding ERPNext shared state ..."
docker compose -f "${COMPOSE_FILE}" down -v --remove-orphans >/dev/null 2>&1 || true
rm -rf "${SHARED_DIR}" "${BASELINE_DIR}"
mkdir -p "${SHARED_DIR}/sites" "${SHARED_DIR}/logs"

docker compose -f "${COMPOSE_FILE}" up -d db redis-cache redis-queue configurator create-site backend websocket frontend queue-short queue-long scheduler

wait_for_erpnext_site "${SITE_CONFIG_PATH}" 480 5 setup
wait_for_container_exit_success "${CREATE_SITE_CONTAINER}" 900 5 setup
wait_for_erpnext_http "${BASE_URL}" 480 5 setup
bootstrap_erpnext_site "${BACKEND_CONTAINER}" "${SITE_NAME}" "${MANIFEST_PATH}" setup
seed_erpnext_data "${BACKEND_CONTAINER}" "${SITE_NAME}" "${MANIFEST_PATH}" setup
write_erpnext_env_file "${ENV_FILE}" "${BASE_URL}" "${ADMIN_USER}" "${ADMIN_PASSWORD}" "${SITE_NAME}" "${COMPOSE_FILE}" "${SHARED_DIR}" "${BASELINE_DIR}" "${BACKEND_CONTAINER}"
snapshot_erpnext_shared_state "${SHARED_DIR}/sites" "${SITE_CONFIG_PATH}" "${DB_CONTAINER}" "${BASELINE_DIR}" "${DB_ROOT_PASSWORD}" setup

echo "[setup] ERPNext is ready at ${BASE_URL}"
echo "[setup] Env file written to ${ENV_FILE}"
