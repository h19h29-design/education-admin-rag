set -euo pipefail

release_fail() {
  printf 'failed=1 error_code=%s\n' "$1" >&2
  exit "${2:-1}"
}

release_require() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    release_fail release_environment_missing 2
  fi
}

release_init() {
  release_require SEN_QA_RELEASE_ID
  release_require SEN_QA_SOURCE_ROOT
  release_require SEN_QA_ARTIFACT_ROOT
  release_require SEN_QA_PRIVATE_EVAL_ROOT
  if [[ ! "$SEN_QA_RELEASE_ID" =~ ^corpus-[0-9]{14}-[0-9a-f]{8}$ ]]; then
    release_fail release_environment_invalid 2
  fi
  local root
  local resolved_roots=()
  for root in "$SEN_QA_SOURCE_ROOT" "$SEN_QA_ARTIFACT_ROOT" "$SEN_QA_PRIVATE_EVAL_ROOT"; do
    if [[ "$root" != /* || "$root" == / || "$root" == *:* || "$root" == *,* ||
          "$root" == *$'\n'* || "$root" == *$'\r'* || ! -d "$root" || -L "$root" ]]; then
      release_fail release_environment_invalid 2
    fi
    resolved_roots+=("$(cd -- "$root" && pwd -P)")
  done
  local left right
  for left in "${resolved_roots[@]}"; do
    for right in "${resolved_roots[@]}"; do
      if [[ "$left" != "$right" && ( "$left" == "$right"/* || "$right" == "$left"/* ) ]]; then
        release_fail release_environment_invalid 2
      fi
    done
  done
  if [[ "${resolved_roots[0]}" == "${resolved_roots[1]}" ||
        "${resolved_roots[0]}" == "${resolved_roots[2]}" ||
        "${resolved_roots[1]}" == "${resolved_roots[2]}" ]]; then
    release_fail release_environment_invalid 2
  fi
}

release_require_pinned_image() {
  local name="$1"
  release_require "$name"
  if [[ ! "${!name}" =~ ^[a-z0-9./_-]+:[a-zA-Z0-9._-]+@sha256:[0-9a-f]{64}$ ]]; then
    release_fail container_image_unpinned 2
  fi
}

release_require_regular() {
  local path="$1"
  local code="$2"
  if [[ ! -f "$path" || -L "$path" ]]; then
    release_fail "$code"
  fi
}

release_dir_mode() {
  if stat -f '%Lp' "$1" >/dev/null 2>&1; then
    stat -f '%Lp' "$1"
  else
    stat -c '%a' "$1"
  fi
}
