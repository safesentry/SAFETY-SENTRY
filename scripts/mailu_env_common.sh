#!/usr/bin/env bash

wait_for_mailu_api() {
  local base_url="$1"
  local api_token="$2"
  local max_wait="${3:-180}"
  local interval="${4:-3}"
  local label="${5:-setup}"
  local elapsed=0

  echo "[${label}] Waiting for Mailu Admin API at ${base_url} ..."
  while true; do
    local code
    code=$(curl -s -o /dev/null -w '%{http_code}' \
      -H "Authorization: Bearer ${api_token}" \
      "${base_url}/api/v1/domain" 2>/dev/null || echo "000")
    if [ "${code}" = "200" ] || [ "${code}" = "401" ] || [ "${code}" = "403" ]; then
      echo "[${label}] Mailu Admin API is ready (${elapsed}s, HTTP ${code})"
      return 0
    fi
    if [ "${elapsed}" -ge "${max_wait}" ]; then
      echo "[${label}] Timed out after ${max_wait}s waiting for Mailu"
      return 1
    fi
    sleep "${interval}"
    elapsed=$((elapsed + interval))
    echo "[${label}] Waiting... (${elapsed}s, HTTP ${code})"
  done
}

write_mailu_env_file() {
  local env_file="$1"
  local base_url="$2"
  local api_token="$3"
  local admin_password="$4"
  local smtp_host="$5"
  local smtp_port="$6"
  local imap_host="$7"
  local imap_port="$8"

  cat > "${env_file}" <<EOF
MAILU_BASE_URL=${base_url}
MAILU_API_TOKEN=${api_token}
MAILU_ADMIN_PASSWORD=${admin_password}
MAILU_SMTP_HOST=${smtp_host}
MAILU_SMTP_PORT=${smtp_port}
MAILU_IMAP_HOST=${imap_host}
MAILU_IMAP_PORT=${imap_port}
MAILU_FRONT_CONTAINER=pipeline-mailu-front
MAILU_ADMIN_CONTAINER=pipeline-mailu-admin
MAILU_DOVECOT_CONTAINER=pipeline-mailu-dovecot
MAILU_POSTFIX_CONTAINER=pipeline-mailu-postfix
EOF
}

seed_mailu_data() {
  local root_dir="$1"
  local base_url="$2"
  local api_token="$3"
  local smtp_host="$4"
  local smtp_port="$5"
  local manifest_path="${6:-${root_dir}/docker/mailu/seed_manifest.json}"
  local label="${7:-seed}"

  echo "[${label}] Seeding Mailu data from ${manifest_path} ..."
  MAILU_BASE_URL="${base_url}" \
  MAILU_API_TOKEN="${api_token}" \
  MAILU_SMTP_HOST="${smtp_host}" \
  MAILU_SMTP_PORT="${smtp_port}" \
  MAILU_SEED_MANIFEST="${manifest_path}" \
  "${PYTHON:-python3.11}" "${root_dir}/docker/mailu/scripts/seed_mailu_data.py"
}
