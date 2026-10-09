#!/usr/bin/env bash

prime_openemr_sites_dir() {
  local sites_dir="$1"
  local image="$2"
  local db_host="$3"
  local db_name="$4"
  local db_user="$5"
  local db_password="$6"
  local container_id=""

  mkdir -p "${sites_dir}"
  container_id="$(docker create "${image}")"
  docker cp "${container_id}:/var/www/localhost/htdocs/openemr/sites/." "${sites_dir}" >/dev/null 2>&1 || true
  docker rm -f "${container_id}" >/dev/null 2>&1 || true

  mkdir -p "${sites_dir}/default/documents"
  cat > "${sites_dir}/default/sqlconf.php" <<EOF
<?php
//  OpenEMR
//  MySQL Config

global \$disable_utf8_flag;
\$disable_utf8_flag = false;

\$host = '${db_host}';
\$port = '3306';
\$login = '${db_user}';
\$pass = '${db_password}';
\$dbase = '${db_name}';
\$db_encoding = 'utf8mb4';

\$sqlconf = array();
global \$sqlconf;
\$sqlconf["host"] = \$host;
\$sqlconf["port"] = \$port;
\$sqlconf["login"] = \$login;
\$sqlconf["pass"] = \$pass;
\$sqlconf["dbase"] = \$dbase;
\$sqlconf["db_encoding"] = \$db_encoding;

\$config = 1;
?>
EOF
}

wait_for_openemr_http() {
  local base_url="$1"
  local timeout="${2:-360}"
  local interval="${3:-5}"
  local label="${4:-openemr}"
  local deadline=$((SECONDS + timeout))

  while [ "${SECONDS}" -lt "${deadline}" ]; do
    if curl --silent --show-error --location "${base_url%/}/interface/login/login.php?site=default" >/dev/null 2>&1; then
      return 0
    fi
    sleep "${interval}"
  done

  echo "[${label}] Timed out waiting for OpenEMR HTTP endpoint: ${base_url}" >&2
  return 1
}

wait_for_container_health() {
  local container_name="$1"
  local timeout="${2:-300}"
  local interval="${3:-5}"
  local label="${4:-openemr}"
  local deadline=$((SECONDS + timeout))

  while [ "${SECONDS}" -lt "${deadline}" ]; do
    local status
    status="$(docker inspect -f '{{.State.Health.Status}}' "${container_name}" 2>/dev/null || true)"
    if [ "${status}" = "healthy" ]; then
      return 0
    fi
    sleep "${interval}"
  done

  echo "[${label}] Timed out waiting for healthy container: ${container_name}" >&2
  return 1
}

dump_openemr_database() {
  local db_container="$1"
  local db_name="$2"
  local dump_path="$3"
  local root_password="$4"
  local basename
  basename="$(basename "${dump_path}")"

  docker exec "${db_container}" bash -lc \
    "mysqldump -uroot -p${root_password} --single-transaction '${db_name}' > /tmp/${basename}"
  docker cp "${db_container}:/tmp/${basename}" "${dump_path}"
}

restore_openemr_database() {
  local db_container="$1"
  local db_name="$2"
  local dump_path="$3"
  local root_password="$4"
  local basename
  basename="$(basename "${dump_path}")"

  docker cp "${dump_path}" "${db_container}:/tmp/${basename}"
  docker exec "${db_container}" bash -lc \
    "mysql -uroot -p${root_password} -e \"DROP DATABASE IF EXISTS \\\`${db_name}\\\`; CREATE DATABASE \\\`${db_name}\\\` CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;\""
  docker exec "${db_container}" bash -lc \
    "mysql -uroot -p${root_password} '${db_name}' < /tmp/${basename}"
}

seed_openemr_data() {
  local root_dir="$1"
  local manifest_path="$2"
  local db_container="$3"
  local db_name="$4"
  local root_password="$5"

  python3 "${root_dir}/docker/openemr/scripts/seed_openemr_data.py" \
    "${manifest_path}" \
    "${db_container}" \
    "${db_name}" \
    "${root_password}"
}

write_openemr_env_file() {
  local env_file="$1"
  local base_url="$2"
  local admin_user="$3"
  local admin_password="$4"
  local db_container="$5"
  local db_name="$6"
  local db_root_password="$7"
  local compose_file="$8"
  local shared_dir="$9"
  local baseline_dir="${10}"
  local container_name="${11}"

  cat > "${env_file}" <<EOF
PIPELINE_ENV=openemr
OPENEMR_BASE_URL=${base_url}
OPENEMR_ADMIN_USER=${admin_user}
OPENEMR_ADMIN_PASSWORD=${admin_password}
OPENEMR_DB_CONTAINER=${db_container}
OPENEMR_DB_NAME=${db_name}
OPENEMR_DB_ROOT_PASSWORD=${db_root_password}
OPENEMR_COMPOSE_FILE=${compose_file}
OPENEMR_SHARED_DIR=${shared_dir}
OPENEMR_BASELINE_DIR=${baseline_dir}
OPENEMR_CONTAINER_NAME=${container_name}
EOF
}

snapshot_openemr_shared_state() {
  local shared_dir="$1"
  local baseline_dir="$2"
  local db_container="$3"
  local db_name="$4"
  local root_password="$5"

  rm -rf "${baseline_dir}"
  mkdir -p "${baseline_dir}"
  cp -R "${shared_dir}/sites" "${baseline_dir}/sites"
  dump_openemr_database "${db_container}" "${db_name}" "${baseline_dir}/openemr.sql" "${root_password}"
}

restore_openemr_shared_state() {
  local shared_dir="$1"
  local db_container="$2"
  local db_name="$3"
  local baseline_dir="$4"
  local root_password="$5"

  if [ ! -d "${baseline_dir}/sites" ] || [ ! -f "${baseline_dir}/openemr.sql" ]; then
    echo "[reset] Missing OpenEMR baseline state" >&2
    return 1
  fi

  rm -rf "${shared_dir}/sites"
  mkdir -p "${shared_dir}"
  cp -R "${baseline_dir}/sites" "${shared_dir}/sites"
  restore_openemr_database "${db_container}" "${db_name}" "${baseline_dir}/openemr.sql" "${root_password}"
}
