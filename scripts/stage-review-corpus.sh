#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=scripts/_release-common.sh
source "$SCRIPT_DIR/_release-common.sh"

release_init
if [[ "$#" -ne 0 ]]; then
  release_fail review_staging_arguments_invalid 2
fi
release_require_pinned_image SEN_QA_INGESTION_IMAGE
for name in \
  SEN_QA_RAW_PAGES_ROOT \
  SEN_QA_OCR_AUTHORITY_LOCK \
  SEN_QA_OCR_AUTHORITY_LOCK_SHA256; do
  release_require "$name"
done
if [[ -n "${SEN_QA_INGESTION_IMAGE_DIGEST:-}" ]]; then
  release_fail review_staging_environment_ambiguous 2
fi
if [[ ! "$SEN_QA_OCR_AUTHORITY_LOCK_SHA256" =~ ^[0-9a-f]{64}$ ]]; then
  release_fail ocr_authority_lock_sha256_invalid 2
fi

if [[ "$SEN_QA_RAW_PAGES_ROOT" != /* || "$SEN_QA_RAW_PAGES_ROOT" == / ||
      "$SEN_QA_RAW_PAGES_ROOT" == *:* || "$SEN_QA_RAW_PAGES_ROOT" == *,* ||
      "$SEN_QA_RAW_PAGES_ROOT" == *$'\n'* ||
      "$SEN_QA_RAW_PAGES_ROOT" == *$'\r'* ||
      ! -d "$SEN_QA_RAW_PAGES_ROOT" || -L "$SEN_QA_RAW_PAGES_ROOT" ]]; then
  release_fail review_raw_root_invalid 2
fi
if ! RAW_ROOT_REAL="$(cd -- "$SEN_QA_RAW_PAGES_ROOT" 2>/dev/null && pwd -P)"; then
  release_fail review_raw_root_invalid 2
fi
PLANNED_RELEASE_ROOT="$SEN_QA_ARTIFACT_ROOT/releases/$SEN_QA_RELEASE_ID"
if [[ "$RAW_ROOT_REAL" != "$SEN_QA_RAW_PAGES_ROOT" ||
      "$RAW_ROOT_REAL" == "$SEN_QA_ARTIFACT_ROOT" ||
      "$SEN_QA_ARTIFACT_ROOT" == "$RAW_ROOT_REAL"/* ||
      "$PLANNED_RELEASE_ROOT" == "$RAW_ROOT_REAL" ||
      "$PLANNED_RELEASE_ROOT" == "$RAW_ROOT_REAL"/* ||
      "$RAW_ROOT_REAL" == "$PLANNED_RELEASE_ROOT"/* ]]; then
  release_fail review_raw_root_invalid 2
fi

if [[ "$SEN_QA_OCR_AUTHORITY_LOCK" != /* ||
      "$SEN_QA_OCR_AUTHORITY_LOCK" == *:* ||
      "$SEN_QA_OCR_AUTHORITY_LOCK" == *,* ||
      "$SEN_QA_OCR_AUTHORITY_LOCK" == *$'\n'* ||
      "$SEN_QA_OCR_AUTHORITY_LOCK" == *$'\r'* ||
      ! -f "$SEN_QA_OCR_AUTHORITY_LOCK" ||
      -L "$SEN_QA_OCR_AUTHORITY_LOCK" ]]; then
  release_fail ocr_authority_lock_invalid 2
fi
if ! AUTHORITY_PARENT="$(
  cd -- "$(dirname -- "$SEN_QA_OCR_AUTHORITY_LOCK")" 2>/dev/null && pwd -P
)"; then
  release_fail ocr_authority_lock_invalid 2
fi
AUTHORITY_REAL="$AUTHORITY_PARENT/$(basename -- "$SEN_QA_OCR_AUTHORITY_LOCK")"
if [[ "$AUTHORITY_REAL" != "$SEN_QA_OCR_AUTHORITY_LOCK" ||
      "$AUTHORITY_REAL" == "$RAW_ROOT_REAL" ||
      "$AUTHORITY_REAL" == "$RAW_ROOT_REAL"/* ]]; then
  release_fail ocr_authority_lock_invalid 2
fi

RELEASES_ROOT="$SEN_QA_ARTIFACT_ROOT/releases"
if [[ ! -e "$RELEASES_ROOT" && ! -L "$RELEASES_ROOT" ]] &&
  ! mkdir -m 0700 "$RELEASES_ROOT" 2>/dev/null; then
  release_fail review_output_invalid 2
fi
if [[ ! -d "$RELEASES_ROOT" || -L "$RELEASES_ROOT" ]]; then
  release_fail review_output_invalid 2
fi
RELEASE_ROOT="$PLANNED_RELEASE_ROOT"
if [[ -e "$RELEASE_ROOT" || -L "$RELEASE_ROOT" ]]; then
  release_fail review_output_exists 2
fi
if ! mkdir -m 0700 "$RELEASE_ROOT" 2>/dev/null; then
  if [[ -e "$RELEASE_ROOT" || -L "$RELEASE_ROOT" ]]; then
    release_fail review_output_exists 2
  fi
  release_fail review_output_invalid 2
fi

if ! docker run --rm --network none --read-only --cap-drop ALL \
  --user "$(id -u):$(id -g)" \
  --security-opt no-new-privileges \
  --tmpfs /tmp:rw,noexec,nosuid,size=512m \
  -v "$RAW_ROOT_REAL:/sen-qa/raw-pages:ro" \
  -v "$AUTHORITY_REAL:/sen-qa/ocr-authority-lock.json:ro" \
  -v "$RELEASE_ROOT:/sen-qa/output:rw" \
  "$SEN_QA_INGESTION_IMAGE" stage-review-corpus \
  --manifest /work/data/manifests/sen_qa_sources.json \
  --input-root /sen-qa/raw-pages \
  --output-root /sen-qa/output \
  --release-id "$SEN_QA_RELEASE_ID" \
  --ingestion-version "image-${SEN_QA_INGESTION_IMAGE##*@sha256:}" \
  --ocr-authority-lock /sen-qa/ocr-authority-lock.json \
  --expected-ocr-authority-lock-sha256 \
    "$SEN_QA_OCR_AUTHORITY_LOCK_SHA256" >/dev/null 2>&1; then
  release_fail review_staging_failed
fi

REVIEW_ROOT="$RELEASE_ROOT/review"
if [[ ! -d "$REVIEW_ROOT" || -L "$REVIEW_ROOT" ||
      "$(release_dir_mode "$REVIEW_ROOT" 2>/dev/null)" != 700 ]]; then
  release_fail review_output_invalid
fi
shopt -s nullglob dotglob
release_entries=("$RELEASE_ROOT"/*)
shopt -u nullglob dotglob
if [[ "${#release_entries[@]}" -ne 1 ||
      "${release_entries[0]}" != "$REVIEW_ROOT" ]]; then
  release_fail review_output_invalid
fi
for name in \
  registry.json \
  documents.json \
  ingestion-evidence.json \
  summary.json \
  review-queue.jsonl \
  review.sqlite3 \
  ocr-authority-lock.json; do
  release_require_regular "$REVIEW_ROOT/$name" review_output_invalid
  if [[ "$(release_dir_mode "$REVIEW_ROOT/$name" 2>/dev/null)" != 600 ]]; then
    release_fail review_output_invalid
  fi
done
if ! cmp -s -- "$AUTHORITY_REAL" "$REVIEW_ROOT/ocr-authority-lock.json"; then
  release_fail review_output_invalid
fi

if [[ ! -d "$REVIEW_ROOT/candidates" || -L "$REVIEW_ROOT/candidates" ||
      "$(release_dir_mode "$REVIEW_ROOT/candidates" 2>/dev/null)" != 700 ]]; then
  release_fail review_output_invalid
fi

if ! SUMMARY_VALUES="$(jq -er '
  (.parser_quarantines_sha256 // "-") as $quarantine_sha
  | if (
      type == "object"
      and (.case_count | type) == "number"
      and (.case_count | floor) == .case_count
      and .case_count >= 1
      and (.quarantine_count | type) == "number"
      and (.quarantine_count | floor) == .quarantine_count
      and .quarantine_count >= 0
      and ($quarantine_sha | type) == "string"
      and (.schema_version | type) == "string"
    )
    then [.case_count, .quarantine_count, $quarantine_sha, .schema_version]
      | map(tostring) | join("|")
    else error("invalid")
    end
' "$REVIEW_ROOT/summary.json" 2>/dev/null)"; then
  release_fail review_output_invalid
fi
IFS='|' read -r CASE_COUNT QUARANTINE_COUNT SUMMARY_QUARANTINE_SHA SUMMARY_SCHEMA \
  <<< "$SUMMARY_VALUES"

if ! EVIDENCE_VALUES="$(jq -er '
  (.parser_quarantines_sha256 // "-") as $quarantine_sha
  | if (
      type == "object"
      and (.parser_quarantine_count | type) == "number"
      and (.parser_quarantine_count | floor) == .parser_quarantine_count
      and .parser_quarantine_count >= 0
      and ($quarantine_sha | type) == "string"
      and (.schema_version | type) == "string"
    )
    then [.parser_quarantine_count, $quarantine_sha, .schema_version]
      | map(tostring) | join("|")
    else error("invalid")
    end
' "$REVIEW_ROOT/ingestion-evidence.json" 2>/dev/null)"; then
  release_fail review_output_invalid
fi
IFS='|' read -r EVIDENCE_QUARANTINE_COUNT EVIDENCE_QUARANTINE_SHA EVIDENCE_SCHEMA \
  <<< "$EVIDENCE_VALUES"

if [[ "$QUARANTINE_COUNT" != "$EVIDENCE_QUARANTINE_COUNT" ]]; then
  release_fail review_output_invalid
fi
if [[ "$QUARANTINE_COUNT" -eq 0 ]]; then
  if [[ "$SUMMARY_SCHEMA" != sen-qa-review-package/v2 ||
        "$EVIDENCE_SCHEMA" != sen-qa-ingestion-evidence/v2 ||
        "$SUMMARY_QUARANTINE_SHA" != - ||
        "$EVIDENCE_QUARANTINE_SHA" != - ||
        -e "$REVIEW_ROOT/parser-quarantines.jsonl" ||
        -L "$REVIEW_ROOT/parser-quarantines.jsonl" ]]; then
    release_fail review_output_invalid
  fi
else
  if [[ "$SUMMARY_SCHEMA" != sen-qa-review-package/v3 ||
        "$EVIDENCE_SCHEMA" != sen-qa-ingestion-evidence/v3 ||
        ! "$SUMMARY_QUARANTINE_SHA" =~ ^[0-9a-f]{64}$ ||
        "$SUMMARY_QUARANTINE_SHA" != "$EVIDENCE_QUARANTINE_SHA" ]]; then
    release_fail review_output_invalid
  fi
  release_require_regular \
    "$REVIEW_ROOT/parser-quarantines.jsonl" review_output_invalid
  if [[ "$(release_dir_mode "$REVIEW_ROOT/parser-quarantines.jsonl" 2>/dev/null)" != 600 ]]; then
    release_fail review_output_invalid
  fi
  if ! ACTUAL_QUARANTINE_SHA="$(
    shasum -a 256 "$REVIEW_ROOT/parser-quarantines.jsonl" 2>/dev/null |
      awk '{print $1}'
  )" || [[ "$ACTUAL_QUARANTINE_SHA" != "$SUMMARY_QUARANTINE_SHA" ]]; then
    release_fail review_output_invalid
  fi
  if ! jq -e -s --argjson expected "$QUARANTINE_COUNT" \
    'length == $expected and all(.[]; type == "object")' \
    "$REVIEW_ROOT/parser-quarantines.jsonl" >/dev/null 2>&1; then
    release_fail review_output_invalid
  fi
fi

if ! REGISTRY_BINDINGS="$(jq -er --argjson expected "$CASE_COUNT" '
  if (
    type == "object"
    and .schema_version == "sen-qa-canonical-review-registry/v1"
    and (.cases | type) == "array"
    and (.cases | length) == $expected
    and ([.cases[].case_id] | unique | length) == $expected
    and all(.cases[];
      (.case_id | type) == "string"
      and (.case_id | test("^[a-z0-9][a-z0-9-]{0,159}$"))
      and (.content_sha256 | type) == "string"
      and (.content_sha256 | test("^[0-9a-f]{64}$"))
    )
  )
  then .cases | sort_by(.case_id) | .[]
    | [.case_id, .content_sha256] | @tsv
  else error("invalid")
  end
' "$REVIEW_ROOT/registry.json" 2>/dev/null)"; then
  release_fail review_output_invalid
fi

shopt -s nullglob dotglob
candidate_entries=("$REVIEW_ROOT/candidates"/*)
shopt -u nullglob dotglob
if [[ "${#candidate_entries[@]}" -ne "$CASE_COUNT" ]]; then
  release_fail review_output_invalid
fi
for candidate_path in "${candidate_entries[@]}"; do
  release_require_regular "$candidate_path" review_output_invalid
  if [[ "$(release_dir_mode "$candidate_path" 2>/dev/null)" != 600 ||
        "$(basename -- "$candidate_path")" != *.json ]]; then
    release_fail review_output_invalid
  fi
done

BINDING_COUNT=0
while IFS=$'\t' read -r CANDIDATE_CASE EXPECTED_CANDIDATE_SHA; do
  CANDIDATE_PATH="$REVIEW_ROOT/candidates/$CANDIDATE_CASE.json"
  release_require_regular "$CANDIDATE_PATH" review_output_invalid
  if ! ACTUAL_CANDIDATE_SHA="$(
    shasum -a 256 "$CANDIDATE_PATH" 2>/dev/null | awk '{print $1}'
  )" || [[ "$ACTUAL_CANDIDATE_SHA" != "$EXPECTED_CANDIDATE_SHA" ]]; then
    release_fail review_output_invalid
  fi
  BINDING_COUNT=$((BINDING_COUNT + 1))
done <<< "$REGISTRY_BINDINGS"
if [[ "$BINDING_COUNT" -ne "$CASE_COUNT" ]]; then
  release_fail review_output_invalid
fi

printf 'release_id=%s stage=review_pending failed=0\n' "$SEN_QA_RELEASE_ID"
