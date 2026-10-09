#!/usr/bin/env bash

wait_for_zammad_api() {
  local base_url="$1"
  local admin_user="$2"
  local admin_password="$3"
  local max_wait="${4:-240}"
  local interval="${5:-5}"
  local label="${6:-setup}"
  local elapsed=0

  echo "[${label}] Waiting for Zammad at ${base_url} ..."
  while true; do
    local code
    code=$(curl -s -u "${admin_user}:${admin_password}" -o /dev/null -w '%{http_code}' "${base_url}/api/v1/users/me" 2>/dev/null || echo "000")
    if [ "${code}" = "200" ]; then
      echo "[${label}] Zammad is ready (${elapsed}s)"
      return 0
    fi
    if [ "${elapsed}" -ge "${max_wait}" ]; then
      echo "[${label}] Timed out after ${max_wait}s waiting for Zammad"
      return 1
    fi
    sleep "${interval}"
    elapsed=$((elapsed + interval))
    echo "[${label}] Waiting... (${elapsed}s, HTTP ${code})"
  done
}

bootstrap_zammad_admin() {
  local rails_container="$1"
  local admin_user="$2"
  local admin_password="$3"
  local max_wait="${4:-180}"
  local interval="${5:-5}"
  local label="${6:-setup}"
  local elapsed=0

  echo "[${label}] Bootstrapping Zammad admin user in ${rails_container} ..."
  while true; do
    if docker exec "${rails_container}" bash -lc "bundle exec rails r 'user = User.find_by(email: \"${admin_user}\") || User.find_by(email: \"nicole.braun@zammad.org\") || User.where.not(email: [nil, \"\"]).first; raise \"no bootstrap user\" unless user; user.login = \"${admin_user}\"; user.email = \"${admin_user}\"; user.firstname = \"Pipeline\"; user.lastname = \"Admin\"; user.active = true; user.roles = Role.where(name: [\"Admin\", \"Agent\"]); user.group_names_access_map = {\"Users\" => [\"full\"]}; user.password = \"${admin_password}\"; user.save!'" >/dev/null 2>&1; then
      echo "[${label}] Admin bootstrap complete"
      return 0
    fi
    if [ "${elapsed}" -ge "${max_wait}" ]; then
      echo "[${label}] Timed out after ${max_wait}s bootstrapping Zammad admin"
      return 1
    fi
    sleep "${interval}"
    elapsed=$((elapsed + interval))
    echo "[${label}] Waiting for rails bootstrap... (${elapsed}s)"
  done
}

write_zammad_env_file() {
  local env_file="$1"
  local base_url="$2"
  local admin_user="$3"
  local admin_password="$4"
  local nginx_container="$5"
  local pg_container="$6"
  local rails_container="$7"
  local scheduler_container="$8"
  local websocket_container="$9"

  cat > "${env_file}" <<EOF
ZAMMAD_BASE_URL=${base_url}
ZAMMAD_ADMIN_USER=${admin_user}
ZAMMAD_ADMIN_PASSWORD=${admin_password}
ZAMMAD_NGINX_CONTAINER=${nginx_container}
ZAMMAD_PG_CONTAINER=${pg_container}
ZAMMAD_RAILSSERVER_CONTAINER=${rails_container}
ZAMMAD_SCHEDULER_CONTAINER=${scheduler_container}
ZAMMAD_WEBSOCKET_CONTAINER=${websocket_container}
EOF
}

seed_zammad_data() {
  local root_dir="$1"
  local base_url="$2"
  local admin_user="$3"
  local admin_password="$4"
  local manifest_path="${5:-${root_dir}/docker/zammad/seed_manifest.json}"
  local label="${6:-seed}"

  echo "[${label}] Seeding Zammad data from ${manifest_path} ..."
  ZAMMAD_BASE_URL="${base_url}" \
  ZAMMAD_ADMIN_USER="${admin_user}" \
  ZAMMAD_ADMIN_PASSWORD="${admin_password}" \
  ZAMMAD_SEED_MANIFEST="${manifest_path}" \
  python3 "${root_dir}/docker/zammad/scripts/seed_zammad_data.py"
}
