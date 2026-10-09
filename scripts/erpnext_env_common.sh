#!/usr/bin/env bash
set -euo pipefail

wait_for_erpnext_site() {
  local site_config_path="${1:?site_config_path required}"
  local timeout="${2:-300}"
  local interval="${3:-5}"
  local label="${4:-erpnext}"
  local deadline=$(( $(date +%s) + timeout ))

  while [ "$(date +%s)" -lt "${deadline}" ]; do
    if [ -f "${site_config_path}" ]; then
      return 0
    fi
    echo "[${label}] Waiting for ERPNext site config at ${site_config_path} ..."
    sleep "${interval}"
  done

  echo "[${label}] Timed out waiting for ERPNext site config" >&2
  return 1
}

wait_for_erpnext_http() {
  local base_url="${1:?base_url required}"
  local timeout="${2:-480}"
  local interval="${3:-5}"
  local label="${4:-erpnext}"
  local deadline=$(( $(date +%s) + timeout ))

  while [ "$(date +%s)" -lt "${deadline}" ]; do
    if curl -fsS "${base_url}/api/method/ping" >/dev/null 2>&1; then
      return 0
    fi
    echo "[${label}] Waiting for ERPNext HTTP endpoint ${base_url} ..."
    sleep "${interval}"
  done

  echo "[${label}] Timed out waiting for ERPNext HTTP service" >&2
  return 1
}

wait_for_container_exit_success() {
  local container_name="${1:?container required}"
  local timeout="${2:-600}"
  local interval="${3:-5}"
  local label="${4:-container}"
  local deadline=$(( $(date +%s) + timeout ))

  while [ "$(date +%s)" -lt "${deadline}" ]; do
    local status
    status="$(docker inspect -f '{{.State.Status}} {{.State.ExitCode}}' "${container_name}" 2>/dev/null || true)"
    if [ "${status}" = "exited 0" ]; then
      return 0
    fi
    if [ -n "${status}" ] && [ "${status#exited }" != "${status}" ] && [ "${status}" != "exited 0" ]; then
      echo "[${label}] Container ${container_name} failed with state ${status}" >&2
      return 1
    fi
    echo "[${label}] Waiting for ${container_name} to exit successfully ..."
    sleep "${interval}"
  done

  echo "[${label}] Timed out waiting for ${container_name} to finish" >&2
  return 1
}

wait_for_container_health() {
  local container_name="${1:?container required}"
  local timeout="${2:-300}"
  local interval="${3:-5}"
  local label="${4:-container}"
  local deadline=$(( $(date +%s) + timeout ))

  while [ "$(date +%s)" -lt "${deadline}" ]; do
    local status
    status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "${container_name}" 2>/dev/null || true)"
    if [ "${status}" = "healthy" ] || [ "${status}" = "running" ]; then
      return 0
    fi
    echo "[${label}] Waiting for ${container_name} to become healthy ..."
    sleep "${interval}"
  done

  echo "[${label}] Timed out waiting for ${container_name} health" >&2
  return 1
}

run_erpnext_site_action() {
  local backend_container="${1:?backend container required}"
  local site_name="${2:?site name required}"
  local action="${3:?action required}"
  local payload_json="${4:-{}}"
  local host_tmp
  host_tmp="$(mktemp /tmp/erpnext-payload.XXXXXX)"
  printf '%s' "${payload_json}" > "${host_tmp}"
  docker cp "${host_tmp}" "${backend_container}:/tmp/pipeline_payload.json" >/dev/null
  docker exec -u 0 "${backend_container}" chmod 644 /tmp/pipeline_payload.json >/dev/null
  rm -f "${host_tmp}"

  docker exec -e "PIPELINE_JSON_PAYLOAD_FILE=/tmp/pipeline_payload.json" "${backend_container}" bash -lc \
    "cd /home/frappe/frappe-bench && /home/frappe/frappe-bench/env/bin/python /opt/pipeline/scripts/erpnext_site_ops.py ${site_name} ${action}"
}

erpnext_db_name() {
  local site_config_path="${1:?site config required}"
  python3 - <<'PY' "${site_config_path}"
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as fh:
    payload = json.load(fh)
print(payload["db_name"])
PY
}

dump_erpnext_database() {
  local db_container="${1:?db container required}"
  local site_config_path="${2:?site config required}"
  local dump_path="${3:?dump path required}"
  local root_password="${4:-admin}"

  local db_name
  db_name="$(erpnext_db_name "${site_config_path}")"
  local basename
  basename="$(basename "${dump_path}")"
  mkdir -p "$(dirname "${dump_path}")"

  docker exec "${db_container}" bash -lc \
    "mysqldump -uroot -p${root_password} --single-transaction --routines --events '${db_name}' > /tmp/${basename}"
  docker cp "${db_container}:/tmp/${basename}" "${dump_path}"
}

restore_erpnext_database() {
  local db_container="${1:?db container required}"
  local site_config_path="${2:?site config required}"
  local dump_path="${3:?dump path required}"
  local root_password="${4:-admin}"

  local db_name
  db_name="$(erpnext_db_name "${site_config_path}")"
  local basename
  basename="$(basename "${dump_path}")"

  docker cp "${dump_path}" "${db_container}:/tmp/${basename}"
  docker exec "${db_container}" bash -lc \
    "mysql -uroot -p${root_password} -e \"DROP DATABASE IF EXISTS \\\`${db_name}\\\`; CREATE DATABASE \\\`${db_name}\\\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;\""
  docker exec "${db_container}" bash -lc \
    "mysql --force -uroot -p${root_password} '${db_name}' < /tmp/${basename}"
}

bootstrap_erpnext_site() {
  local backend_container="${1:?backend container required}"
  local site_name="${2:?site name required}"
  local manifest_path="${3:?manifest path required}"
  local label="${4:-bootstrap}"

  local bootstrap_json
  bootstrap_json="$(python3 - <<'PY' "${manifest_path}"
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as fh:
    payload = json.load(fh)
print(json.dumps(payload.get("bootstrap", {}), ensure_ascii=False))
PY
)"
  local payload_b64
  payload_b64="$(printf '%s' "${bootstrap_json}" | base64 | tr -d '\n')"

  echo "[${label}] Running ERPNext bootstrap ..."
  docker exec -e "PIPELINE_JSON_PAYLOAD_B64=${payload_b64}" "${backend_container}" bash -lc \
    "cd /home/frappe/frappe-bench && /home/frappe/frappe-bench/env/bin/python /opt/pipeline/scripts/erpnext_site_ops.py ${site_name} bootstrap" >/dev/null
}

seed_erpnext_data() {
  local backend_container="${1:?backend container required}"
  local site_name="${2:?site name required}"
  local manifest_path="${3:?manifest path required}"
  local label="${4:-seed}"

  local manifest_json
  manifest_json="$(python3 - <<'PY' "${manifest_path}"
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as fh:
    payload = json.load(fh)
print(json.dumps(payload, ensure_ascii=False))
PY
)"
  local payload_b64
  payload_b64="$(printf '%s' "${manifest_json}" | base64 | tr -d '\n')"

  echo "[${label}] Seeding ERPNext baseline data ..."
  docker exec -e "PIPELINE_JSON_PAYLOAD_B64=${payload_b64}" "${backend_container}" bash -lc \
    "cd /home/frappe/frappe-bench && /home/frappe/frappe-bench/env/bin/python /opt/pipeline/scripts/erpnext_site_ops.py ${site_name} seed"
}

write_erpnext_env_file() {
  local env_file="${1:?env file required}"
  local base_url="${2:?base url required}"
  local admin_user="${3:?admin user required}"
  local admin_password="${4:?admin password required}"
  local site_name="${5:?site name required}"
  local compose_file="${6:?compose file required}"
  local shared_dir="${7:?shared dir required}"
  local baseline_dir="${8:?baseline dir required}"
  local backend_container="${9:?backend container required}"

  cat > "${env_file}" <<EOF
ERPNEXT_BASE_URL=${base_url}
ERPNEXT_ADMIN_USER=${admin_user}
ERPNEXT_ADMIN_PASSWORD=${admin_password}
ERPNEXT_SITE_NAME=${site_name}
ERPNEXT_COMPOSE_FILE=${compose_file}
ERPNEXT_SHARED_DIR=${shared_dir}
ERPNEXT_BASELINE_DIR=${baseline_dir}
ERPNEXT_BACKEND_CONTAINER=${backend_container}
EOF
}

snapshot_erpnext_shared_state() {
  local sites_dir="${1:?sites dir required}"
  local site_config_path="${2:?site config required}"
  local db_container="${3:?db container required}"
  local baseline_dir="${4:?baseline dir required}"
  local root_password="${5:-admin}"
  local label="${6:-snapshot}"

  rm -rf "${baseline_dir}"
  mkdir -p "${baseline_dir}"
  cp -R "${sites_dir}" "${baseline_dir}/sites"
  dump_erpnext_database "${db_container}" "${site_config_path}" "${baseline_dir}/erpnext.sql" "${root_password}"
  echo "[${label}] Snapshot saved to ${baseline_dir}"
}

restore_erpnext_shared_state() {
  local sites_dir="${1:?sites dir required}"
  local site_config_path="${2:?site config required}"
  local db_container="${3:?db container required}"
  local baseline_dir="${4:?baseline dir required}"
  local root_password="${5:-admin}"
  local label="${6:-restore}"

  if [ ! -d "${baseline_dir}" ] || [ ! -d "${baseline_dir}/sites" ] || [ ! -f "${baseline_dir}/erpnext.sql" ]; then
    echo "[${label}] Missing ERPNext baseline dir: ${baseline_dir}" >&2
    return 1
  fi

  rm -rf "${sites_dir}"
  cp -R "${baseline_dir}/sites" "${sites_dir}"
  restore_erpnext_database "${db_container}" "${site_config_path}" "${baseline_dir}/erpnext.sql" "${root_password}"
  echo "[${label}] Restored ERPNext shared state from ${baseline_dir}"
}
