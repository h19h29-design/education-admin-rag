#!/usr/bin/env bash
set -euo pipefail

readonly scanner_version="8.30.1"
readonly repo_root="$(git rev-parse --show-toplevel)"
readonly scanner_config="$repo_root/config/gitleaks.toml"
readonly baseline="$repo_root/config/revoked-secrets-baseline.json"

require_full_history() {
  if [[ "$(git rev-parse --is-shallow-repository)" != "true" ]]; then
    return
  fi

  if git remote get-url origin >/dev/null 2>&1; then
    git fetch --unshallow origin >/dev/null 2>&1 || true
  fi

  if [[ "$(git rev-parse --is-shallow-repository)" == "true" ]]; then
    printf '%s\n' 'BLOCKED: secret history scan requires a non-shallow clone.' >&2
    exit 2
  fi
}

require_no_gitleaksignore() {
  local ignored_path
  ignored_path="$(find "$repo_root" -path "$repo_root/.git" -prune -o -name '.gitleaksignore' -print -quit)"
  if [[ -n "$ignored_path" ]]; then
    printf '%s\n' 'BLOCKED: repository-controlled .gitleaksignore files are not permitted.' >&2
    exit 2
  fi
}

artifact_for_platform() {
  case "$(uname -s)-$(uname -m)" in
    Darwin-arm64)
      artifact="gitleaks_${scanner_version}_darwin_arm64.tar.gz"
      artifact_sha256="b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5"
      ;;
    Darwin-x86_64)
      artifact="gitleaks_${scanner_version}_darwin_x64.tar.gz"
      artifact_sha256="dfe101a4db2255fc85120ac7f3d25e4342c3c20cf749f2c20a18081af1952709"
      ;;
    Linux-aarch64)
      artifact="gitleaks_${scanner_version}_linux_arm64.tar.gz"
      artifact_sha256="e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080"
      ;;
    Linux-x86_64)
      artifact="gitleaks_${scanner_version}_linux_x64.tar.gz"
      artifact_sha256="551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"
      ;;
    *)
      printf 'Unsupported platform for pinned Gitleaks artifact: %s-%s\n' "$(uname -s)" "$(uname -m)" >&2
      exit 2
      ;;
  esac
}

print_metadata() {
  local report="$1"
  jq -r '.[] | "finding fingerprint=\(.Fingerprint // "") commit=\(.Commit // "") path=\(.File // "") rule=\(.RuleID // "")"' "$report"
}

require_full_history
require_no_gitleaksignore
artifact_for_platform
cd "$repo_root"

umask 077
scanner_tmp="$(mktemp -d "${TMPDIR:-/tmp}/education-admin-gitleaks.XXXXXX")"
trap 'rm -rf "$scanner_tmp"' EXIT HUP INT TERM
archive="$scanner_tmp/$artifact"
scanner="$scanner_tmp/gitleaks"
current_report="$scanner_tmp/current.json"
history_report="$scanner_tmp/history.json"

curl --fail --location --silent --show-error --retry 3 \
  "https://github.com/gitleaks/gitleaks/releases/download/v${scanner_version}/${artifact}" \
  --output "$archive"
actual_sha256="$(shasum -a 256 "$archive" | awk '{print $1}')"
if [[ "$actual_sha256" != "$artifact_sha256" ]]; then
  printf '%s\n' 'Pinned Gitleaks artifact checksum mismatch.' >&2
  exit 2
fi
tar -xzf "$archive" -C "$scanner_tmp"
chmod 700 "$scanner"

current_exit=0
"$scanner" dir "$repo_root" --config "$scanner_config" --redact=100 --ignore-gitleaks-allow \
  --report-format json --report-path "$current_report" >/dev/null 2>&1 || current_exit=$?
if [[ "$current_exit" -gt 1 ]]; then
  printf 'Gitleaks working-tree scan failed with exit code %s.\n' "$current_exit" >&2
  exit "$current_exit"
fi
if [[ "$current_exit" -eq 1 ]]; then
  printf '%s\n' 'FAIL: a current working-tree secret finding is not allowlisted.' >&2
  print_metadata "$current_report" >&2
  exit 1
fi

history_exit=0
"$scanner" git --config "$scanner_config" --redact=100 --ignore-gitleaks-allow \
  --report-format json --report-path "$history_report" >/dev/null 2>&1 || history_exit=$?
if [[ "$history_exit" -gt 1 ]]; then
  printf 'Gitleaks history scan failed with exit code %s.\n' "$history_exit" >&2
  exit "$history_exit"
fi

if ! jq -e '
  (.schema_version == 1)
  and ((.known_historical_findings | type) == "array")
  and (.known_historical_findings | length == 1)
' "$baseline" >/dev/null; then
  printf '%s\n' 'Invalid revoked-secret baseline.' >&2
  exit 2
fi

if ! jq -e --slurpfile expected "$baseline" '
  def normalized:
    map({fingerprint: .Fingerprint, commit: .Commit, path: .File, rule_id: .RuleID})
    | sort_by(.fingerprint, .commit, .path, .rule_id);
  normalized == ($expected[0].known_historical_findings | sort_by(.fingerprint, .commit, .path, .rule_id))
' "$history_report" >/dev/null; then
  printf '%s\n' 'FAIL: history findings differ from the single approved historical fingerprint.' >&2
  print_metadata "$history_report" >&2
  exit 1
fi

printf '%s\n' 'Secret gate passed: working tree clean; full history matches one approved historical finding.'
