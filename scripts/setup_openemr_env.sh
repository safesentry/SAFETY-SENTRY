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
DB_USER="${OPENEMR_DB_USER:-openemr}"
DB_PASSWORD="${OPENEMR_DB_PASSWORD:-openemr}"
DB_ROOT_PASSWORD="${OPENEMR_DB_ROOT_PASSWORD:-root}"
CONTAINER_NAME="${OPENEMR_CONTAINER_NAME:-pipeline-openemr}"
OPENEMR_IMAGE="${OPENEMR_IMAGE:-openemr/openemr:7.0.3}"
BOOTSTRAP_BASE_URL="${OPENEMR_BOOTSTRAP_BASE_URL:-http://localhost:8383}"
ENV_FILE="${OPENEMR_ENV_FILE:-${ROOT_DIR}/.env.openemr.generated}"
MANIFEST_PATH="${OPENEMR_SEED_MANIFEST:-${ROOT_DIR}/docker/openemr/seed_manifest.json}"

echo "[setup] Preparing OpenEMR directories ..."
docker compose -f "${COMPOSE_FILE}" down -v --remove-orphans >/dev/null 2>&1 || true
chmod -R u+rwX "${SHARED_DIR}" "${BASELINE_DIR}" 2>/dev/null || true
rm -rf "${SHARED_DIR}" "${BASELINE_DIR}"
mkdir -p "${SHARED_DIR}/sites" "${SHARED_DIR}/logs" "${BASELINE_DIR}"

BOOTSTRAP_DIR="$(mktemp -d /tmp/openemr-bootstrap.XXXXXX)"
BOOTSTRAP_COMPOSE_FILE="${BOOTSTRAP_DIR}/docker-compose.yml"
BOOTSTRAP_DB_CONTAINER="pipeline-openemr-bootstrap-mysql"
BOOTSTRAP_APP_CONTAINER="pipeline-openemr-bootstrap"
BOOTSTRAP_SQL_DUMP="${BOOTSTRAP_DIR}/openemr-bootstrap.sql"

trap 'docker compose -f "${BOOTSTRAP_COMPOSE_FILE}" down -v --remove-orphans >/dev/null 2>&1 || true; rm -rf "${BOOTSTRAP_DIR}"' EXIT

cat > "${BOOTSTRAP_COMPOSE_FILE}" <<EOF
services:
  openemr-db:
    image: mariadb:10.11
    container_name: ${BOOTSTRAP_DB_CONTAINER}
    environment:
      MYSQL_ROOT_PASSWORD: ${DB_ROOT_PASSWORD}
      MYSQL_DATABASE: ${DB_NAME}
      MYSQL_USER: ${DB_USER}
      MYSQL_PASSWORD: ${DB_PASSWORD}
    healthcheck:
      test: ["CMD-SHELL", "mysqladmin ping -h localhost -p${DB_ROOT_PASSWORD} --silent"]
      interval: 5s
      timeout: 5s
      retries: 30

  openemr:
    image: ${OPENEMR_IMAGE}
    container_name: ${BOOTSTRAP_APP_CONTAINER}
    ports:
      - "8383:80"
    environment:
      MYSQL_HOST: openemr-db
      MYSQL_ROOT_PASS: ${DB_ROOT_PASSWORD}
      MYSQL_USER: ${DB_USER}
      MYSQL_PASS: ${DB_PASSWORD}
      OE_USER: ${ADMIN_USER}
      OE_PASS: ${ADMIN_PASSWORD}
    depends_on:
      openemr-db:
        condition: service_healthy
EOF

echo "[setup] Bootstrapping fresh OpenEMR install ..."
docker compose -f "${BOOTSTRAP_COMPOSE_FILE}" up -d
wait_for_container_health "${BOOTSTRAP_DB_CONTAINER}" 300 5 bootstrap
wait_for_openemr_http "${BOOTSTRAP_BASE_URL}" 420 5 bootstrap
docker exec "${BOOTSTRAP_APP_CONTAINER}" tar -C /var/www/localhost/htdocs/openemr/sites -cf - . | tar -xf - -C "${SHARED_DIR}/sites"
chmod -R u+rwX "${SHARED_DIR}/sites"
dump_openemr_database "${BOOTSTRAP_DB_CONTAINER}" "${DB_NAME}" "${BOOTSTRAP_SQL_DUMP}" "${DB_ROOT_PASSWORD}"

echo "[setup] Starting persistent OpenEMR services ..."
docker compose -f "${COMPOSE_FILE}" up -d openemr-db
wait_for_container_health "${DB_CONTAINER}" 300 5 setup
restore_openemr_database "${DB_CONTAINER}" "${DB_NAME}" "${BOOTSTRAP_SQL_DUMP}" "${DB_ROOT_PASSWORD}"
docker compose -f "${COMPOSE_FILE}" up -d openemr
wait_for_openemr_http "${BASE_URL}" 420 5 setup

echo "[setup] Seeding OpenEMR baseline data ..."
seed_openemr_data "${ROOT_DIR}" "${MANIFEST_PATH}" "${DB_CONTAINER}" "${DB_NAME}" "${DB_ROOT_PASSWORD}"
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
snapshot_openemr_shared_state "${SHARED_DIR}" "${BASELINE_DIR}" "${DB_CONTAINER}" "${DB_NAME}" "${DB_ROOT_PASSWORD}"

echo "[setup] OpenEMR is ready"
echo "[setup] Env file written to ${ENV_FILE}"
