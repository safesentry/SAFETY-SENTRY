#!/usr/bin/env bash

wait_for_nocodb_api() {
  local base_url="$1"
  local max_wait="${2:-120}"
  local interval="${3:-2}"
  local label="${4:-setup}"
  local elapsed=0

  echo "[${label}] Waiting for NocoDB API at ${base_url} ..."
  while true; do
    local status
    status=$(curl -s -o /dev/null -w '%{http_code}' "${base_url}/api/v1/health" 2>/dev/null || echo "000")
    if [ "${status}" = "200" ]; then
      echo "[${label}] NocoDB API is ready (${elapsed}s)"
      return 0
    fi
    if [ "${elapsed}" -ge "${max_wait}" ]; then
      echo "[${label}] Timed out after ${max_wait}s waiting for NocoDB API"
      return 1
    fi
    sleep "${interval}"
    elapsed=$((elapsed + interval))
    echo "[${label}] Waiting... (${elapsed}s, HTTP ${status})"
  done
}

write_nocodb_env_file() {
  local env_file="$1"
  local base_url="$2"
  local api_token="$3"
  local admin_email="$4"
  local admin_password="$5"
  local container_name="$6"

  cat > "${env_file}" <<EOF
NOCODB_BASE_URL=${base_url}
NOCODB_API_TOKEN=${api_token}
NOCODB_ADMIN_EMAIL=${admin_email}
NOCODB_ADMIN_PASSWORD=${admin_password}
NOCODB_CONTAINER_NAME=${container_name}
EOF
}

seed_nocodb_data() {
  local root_dir="$1"
  local base_url="$2"
  local admin_email="$3"
  local admin_password="$4"
  local api_token="$5"
  local manifest_path="${6:-${root_dir}/docker/nocodb/seed_manifest.json}"
  local label="${7:-seed}"

  echo "[${label}] Seeding NocoDB data from ${manifest_path} ..."
  NOCODB_BASE_URL="${base_url}" \
  NOCODB_ADMIN_EMAIL="${admin_email}" \
  NOCODB_ADMIN_PASSWORD="${admin_password}" \
  NOCODB_API_TOKEN="${api_token}" \
  NOCODB_SEED_MANIFEST="${manifest_path}" \
  python3 "${root_dir}/docker/nocodb/scripts/seed_nocodb_data.py"
}
