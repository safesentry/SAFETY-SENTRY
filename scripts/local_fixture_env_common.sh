#!/usr/bin/env bash

reset_local_fixture_state() {
  local manifest_path="$1"
  local state_path="$2"

  mkdir -p "$(dirname "${state_path}")"
  cp "${manifest_path}" "${state_path}"
}

write_local_fixture_env_file() {
  local env_file="$1"
  local env_key="$2"
  local state_path="$3"

  cat > "${env_file}" <<EOF
${env_key}=${state_path}
EOF
}
