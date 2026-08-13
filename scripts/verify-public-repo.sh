#!/usr/bin/env bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel 2>/dev/null)" || {
  printf '%s\n' 'public_repo_policy=blocked class=repository' >&2
  exit 2
}
cd "$repo_root"

tracked_count=0
blocked_class=""
while IFS= read -r -d '' tracked_path; do
  tracked_count=$((tracked_count + 1))
  case "$tracked_path" in
    artifacts/*|private/*|data/raw/*|data/ocr/*|*/raw-pages/*)
      blocked_class="private-artifact"
      break
      ;;
    *.key|*.pem|*.p12|*.pfx)
      blocked_class="credential"
      break
      ;;
    *.pdf|*.sqlite|*.sqlite3|*.db)
      blocked_class="document"
      break
      ;;
  esac
done < <(git ls-files -z)

if [[ -n "$blocked_class" ]]; then
  printf 'public_repo_policy=blocked class=%s\n' "$blocked_class" >&2
  exit 2
fi

printf 'public_repo_policy=pass tracked_files=%s\n' "$tracked_count"
