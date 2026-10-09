#!/usr/bin/env bash

wait_for_rocketchat_api() {
  local base_url="$1"
  local max_wait="${2:-180}"
  local interval="${3:-3}"
  local label="${4:-setup}"
  local elapsed=0

  echo "[${label}] Waiting for Rocket.Chat at ${base_url} ..."
  while true; do
    local code
    code=$(curl -s -o /dev/null -w '%{http_code}' "${base_url}/api/info" 2>/dev/null || echo "000")
    if [ "${code}" = "200" ]; then
      echo "[${label}] Rocket.Chat is ready (${elapsed}s)"
      return 0
    fi
    if [ "${elapsed}" -ge "${max_wait}" ]; then
      echo "[${label}] Timed out after ${max_wait}s waiting for Rocket.Chat"
      return 1
    fi
    sleep "${interval}"
    elapsed=$((elapsed + interval))
    echo "[${label}] Waiting... (${elapsed}s, HTTP ${code})"
  done
}

write_rocketchat_env_file() {
  local env_file="$1"
  local base_url="$2"
  local admin_user="$3"
  local admin_password="$4"
  local container_name="$5"
  local mongo_container="$6"

  cat > "${env_file}" <<EOF
ROCKETCHAT_BASE_URL=${base_url}
ROCKETCHAT_ADMIN_USER=${admin_user}
ROCKETCHAT_ADMIN_PASSWORD=${admin_password}
ROCKETCHAT_CONTAINER_NAME=${container_name}
ROCKETCHAT_MONGO_CONTAINER=${mongo_container}
EOF
}

seed_rocketchat_data() {
  local root_dir="$1"
  local base_url="$2"
  local admin_user="$3"
  local admin_password="$4"
  local manifest_path="${5:-${root_dir}/docker/rocketchat/seed_manifest.json}"
  local label="${6:-seed}"

  echo "[${label}] Seeding Rocket.Chat data from ${manifest_path} ..."
  ROCKETCHAT_BASE_URL="${base_url}" \
  ROCKETCHAT_ADMIN_USER="${admin_user}" \
  ROCKETCHAT_ADMIN_PASSWORD="${admin_password}" \
  ROCKETCHAT_SEED_MANIFEST="${manifest_path}" \
  python3 "${root_dir}/docker/rocketchat/scripts/seed_rocketchat_data.py"
}
