#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "${ROOT_DIR}/scripts/zammad_env_common.sh"

COMPOSE_FILE="${ZAMMAD_COMPOSE_FILE:-docker-compose.yml}"
COMPOSE_PATH="${ROOT_DIR}/${COMPOSE_FILE}"
BASE_URL="${ZAMMAD_BASE_URL:-http://localhost:8081}"
ADMIN_USER="${ZAMMAD_ADMIN_USER:-admin@example.com}"
ADMIN_PASSWORD="${ZAMMAD_ADMIN_PASSWORD:-Admin123!}"
ENV_FILE="${ZAMMAD_ENV_FILE:-${ROOT_DIR}/.env.zammad.generated}"
MANIFEST_PATH="${ZAMMAD_SEED_MANIFEST:-${ROOT_DIR}/docker/zammad/seed_manifest.json}"
NGINX_CONTAINER="${ZAMMAD_NGINX_CONTAINER:-pipeline-zammad-nginx}"
PG_CONTAINER="${ZAMMAD_PG_CONTAINER:-pipeline-zammad-postgresql}"
RAILS_CONTAINER="${ZAMMAD_RAILSSERVER_CONTAINER:-pipeline-zammad-railsserver}"
SCHEDULER_CONTAINER="${ZAMMAD_SCHEDULER_CONTAINER:-pipeline-zammad-scheduler}"
WEBSOCKET_CONTAINER="${ZAMMAD_WEBSOCKET_CONTAINER:-pipeline-zammad-websocket}"

echo "[setup] Starting Zammad containers from ${COMPOSE_FILE} ..."
docker compose -f "${COMPOSE_PATH}" up -d \
  zammad-postgresql \
  zammad-redis \
  zammad-memcached \
  zammad-init \
  zammad-railsserver \
  zammad-scheduler \
  zammad-websocket \
  zammad-nginx

bootstrap_zammad_admin "${RAILS_CONTAINER}" "${ADMIN_USER}" "${ADMIN_PASSWORD}" 180 5 setup
wait_for_zammad_api "${BASE_URL}" "${ADMIN_USER}" "${ADMIN_PASSWORD}" 240 5 setup
seed_zammad_data "${ROOT_DIR}" "${BASE_URL}" "${ADMIN_USER}" "${ADMIN_PASSWORD}" "${MANIFEST_PATH}" setup
write_zammad_env_file "${ENV_FILE}" "${BASE_URL}" "${ADMIN_USER}" "${ADMIN_PASSWORD}" "${NGINX_CONTAINER}" "${PG_CONTAINER}" "${RAILS_CONTAINER}" "${SCHEDULER_CONTAINER}" "${WEBSOCKET_CONTAINER}"

echo "[setup] Zammad is ready"
echo "[setup] Env file written to ${ENV_FILE}"
