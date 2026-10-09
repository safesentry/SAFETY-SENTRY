#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "${ROOT_DIR}/scripts/mailu_env_common.sh"

COMPOSE_FILE="${MAILU_COMPOSE_FILE:-docker-compose.yml}"
COMPOSE_PATH="${ROOT_DIR}/${COMPOSE_FILE}"
BASE_URL="${MAILU_BASE_URL:-http://localhost:8443}"
API_TOKEN="${MAILU_API_TOKEN:-pipeline-mailu-api-token-2024}"
ADMIN_PASSWORD="${MAILU_ADMIN_PASSWORD:-Admin123!}"
SMTP_HOST="${MAILU_SMTP_HOST:-localhost}"
SMTP_PORT="${MAILU_SMTP_PORT:-2525}"
IMAP_HOST="${MAILU_IMAP_HOST:-localhost}"
IMAP_PORT="${MAILU_IMAP_PORT:-1143}"
ENV_FILE="${MAILU_ENV_FILE:-${ROOT_DIR}/.env.mailu.generated}"
MANIFEST_PATH="${MAILU_SEED_MANIFEST:-${ROOT_DIR}/docker/mailu/seed_manifest.json}"

echo "[reset] Stopping Mailu containers ..."
docker compose -f "${COMPOSE_PATH}" stop mailu-front mailu-admin mailu-dovecot mailu-postfix 2>/dev/null || true

echo "[reset] Removing containers and volumes ..."
docker compose -f "${COMPOSE_PATH}" rm -f -v mailu-front mailu-admin mailu-dovecot mailu-postfix 2>/dev/null || true
docker volume rm -f pipeline_mailu_data pipeline_mailu_mail pipeline_mailu_certs pipeline_mailu_dkim pipeline_mailu_overrides pipeline_mailu_queue 2>/dev/null || true

echo "[reset] Recreating Mailu containers ..."
docker compose -f "${COMPOSE_PATH}" up -d mailu-admin mailu-dovecot mailu-postfix mailu-front

wait_for_mailu_api "${BASE_URL}" "${API_TOKEN}" 180 3 reset

echo "[reset] Re-seeding Mailu data ..."
seed_mailu_data "${ROOT_DIR}" "${BASE_URL}" "${API_TOKEN}" "${SMTP_HOST}" "${SMTP_PORT}" "${MANIFEST_PATH}" reset

write_mailu_env_file "${ENV_FILE}" "${BASE_URL}" "${API_TOKEN}" "${ADMIN_PASSWORD}" "${SMTP_HOST}" "${SMTP_PORT}" "${IMAP_HOST}" "${IMAP_PORT}"

echo "[reset] Mailu reset complete"
echo "[reset] Export with: set -a; source ${ENV_FILE}; set +a"
