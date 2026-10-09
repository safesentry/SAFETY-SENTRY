#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "${ROOT_DIR}/scripts/openemr_env_common.sh"

COMPOSE_FILE="${OPENEMR_COMPOSE_FILE:-${ROOT_DIR}/docker/openemr/docker-compose.yml}"
SHARED_DIR="${OPENEMR_SHARED_DIR:-${ROOT_DIR}/docker/openemr/shared}"
BASELINE_DIR="${OPENEMR_BASELINE_DIR:-${ROOT_DIR}/docker/openemr/baseline}"
BASE_URL="${OPENEMR_BASE_URL:-http://localhost:8083}"
ADMIN_USER="${OPENEMR_ADMIN_USER:-admin}"
ADMIN_PASSWORD="${OPENEMR_ADMIN_PASSWORD:-Admin123!}"
DB_CONTAINER="${OPENEMR_DB_CONTAINER:-pipeline-openemr-mysql}"
DB_NAME="${OPENEMR_DB_NAME:-openemr}"
DB_ROOT_PASSWORD="${OPENEMR_DB_ROOT_PASSWORD:-root}"
CONTAINER_NAME="${OPENEMR_CONTAINER_NAME:-pipeline-openemr}"
ENV_FILE="${OPENEMR_ENV_FILE:-${ROOT_DIR}/.env.openemr.generated}"

if [ ! -d "${BASELINE_DIR}" ]; then
  echo "[reset] Missing OpenEMR baseline. Falling back to setup."
  bash "${ROOT_DIR}/scripts/setup_openemr_env.sh"
  exit 0
fi

echo "[reset] Restoring OpenEMR baseline ..."
docker compose -f "${COMPOSE_FILE}" down --remove-orphans >/dev/null 2>&1 || true
mkdir -p "${SHARED_DIR}" "${SHARED_DIR}/logs"
docker compose -f "${COMPOSE_FILE}" up -d openemr-db
wait_for_container_health "${DB_CONTAINER}" 300 5 reset
restore_openemr_shared_state "${SHARED_DIR}" "${DB_CONTAINER}" "${DB_NAME}" "${BASELINE_DIR}" "${DB_ROOT_PASSWORD}"
docker compose -f "${COMPOSE_FILE}" up -d openemr
wait_for_openemr_http "${BASE_URL}" 420 5 reset
write_openemr_env_file \
  "${ENV_FILE}" \
  "${BASE_URL}" \
  "${ADMIN_USER}" \
  "${ADMIN_PASSWORD}" \
  "${DB_CONTAINER}" \
  "${DB_NAME}" \
  "${DB_ROOT_PASSWORD}" \
  "${COMPOSE_FILE}" \
  "${SHARED_DIR}" \
  "${BASELINE_DIR}" \
  "${CONTAINER_NAME}"

echo "[reset] OpenEMR baseline restored"
