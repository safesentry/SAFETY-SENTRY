#!/usr/bin/env bash

wait_for_vaultwarden_alive() {
  local base_url="$1"
  local max_wait="${2:-120}"
  local interval="${3:-2}"
  local label="${4:-setup}"
  local elapsed=0

  echo "[${label}] Waiting for Vaultwarden at ${base_url} ..."
  while true; do
    local status
    status=$(curl -s -o /dev/null -w '%{http_code}' "${base_url}/alive" 2>/dev/null || echo "000")
    if [ "${status}" = "200" ]; then
      echo "[${label}] Vaultwarden is alive (${elapsed}s)"
      return 0
    fi
    if [ "${elapsed}" -ge "${max_wait}" ]; then
      echo "[${label}] Timed out after ${max_wait}s waiting for Vaultwarden"
      return 1
    fi
    sleep "${interval}"
    elapsed=$((elapsed + interval))
    echo "[${label}] Waiting... (${elapsed}s, HTTP ${status})"
  done
}

write_vaultwarden_env_file() {
  local env_file="$1"
  local base_url="$2"
  local admin_token="$3"
  local state_file="$4"
  local manifest_path="$5"
  local container_name="$6"

  cat > "${env_file}" <<EOF
VAULTWARDEN_BASE_URL=${base_url}
VAULTWARDEN_ADMIN_TOKEN=${admin_token}
VAULTWARDEN_STATE_FILE=${state_file}
VAULTWARDEN_SEED_MANIFEST=${manifest_path}
VAULTWARDEN_CONTAINER_NAME=${container_name}
PIPELINE_ENV=vaultwarden
EOF
}

seed_vaultwarden_data() {
  local root_dir="$1"
  local manifest_path="$2"
  local state_file="$3"
  local label="${4:-seed}"

  echo "[${label}] Materializing Vaultwarden seed fixture from ${manifest_path} ..."
  VAULTWARDEN_SEED_MANIFEST="${manifest_path}" \
  VAULTWARDEN_STATE_FILE="${state_file}" \
  python3 "${root_dir}/docker/vaultwarden/scripts/seed_vaultwarden_data.py"
}
