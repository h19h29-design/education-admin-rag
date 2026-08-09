#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
source "$SCRIPT_DIR/_release-common.sh"
release_init
release_require SEN_QA_STORAGE_POLICY
release_require_regular "$SEN_QA_STORAGE_POLICY" storage_policy_missing
release_require_pinned_image SEN_QA_PERMISSION_PROBE_IMAGE

POLICY_ENV=""
if ! POLICY_ENV="$(cd -- "$SCRIPT_DIR/.." && uv run python -m src.cli \
  storage-policy-env --policy "$SEN_QA_STORAGE_POLICY")"; then
  release_fail storage_policy_invalid 2
fi
eval "$POLICY_ENV"
if [[ "$SEN_QA_POLICY_SOURCE_ROOT" != "$SEN_QA_SOURCE_ROOT" ||
      "$SEN_QA_POLICY_ARTIFACT_ROOT" != "$SEN_QA_ARTIFACT_ROOT" ||
      "$SEN_QA_POLICY_PRIVATE_EVAL_ROOT" != "$SEN_QA_PRIVATE_EVAL_ROOT" ]]; then
  release_fail storage_policy_root_mismatch 2
fi

SOURCE_PROBE="$SEN_QA_SOURCE_ROOT/.sen-qa-permission-probe"
INGESTION_PROBE="$SEN_QA_ARTIFACT_ROOT/.sen-qa-ingestion-probe"
CANONICAL_PROBE="$SEN_QA_ARTIFACT_ROOT/.sen-qa-canonical-permission-probe"
REVIEW_PROBE="$SEN_QA_ARTIFACT_ROOT/.sen-qa-review-permission-probe"
PRIVATE_PROBE="$SEN_QA_PRIVATE_EVAL_ROOT/.sen-qa-permission-probe"
for required in "$SOURCE_PROBE" "$INGESTION_PROBE" "$CANONICAL_PROBE" "$REVIEW_PROBE" "$PRIVATE_PROBE"; do
  release_require_regular "$required" storage_probe_input_missing
done
REVIEWER_UID="$(id -u)"
if [[ "$REVIEWER_UID" == 0 || "$REVIEWER_UID" == "$SEN_QA_POLICY_INGESTION_UID" ||
      "$REVIEWER_UID" == "$SEN_QA_POLICY_SEARCH_UID" ||
      "$REVIEWER_UID" == "$SEN_QA_POLICY_EVALUATOR_UID" ]]; then
  release_fail reviewer_identity_invalid 2
fi

COMMON_ARGS=(
  run --rm --network none --read-only --cap-drop ALL
  --security-opt no-new-privileges --tmpfs /tmp:rw,noexec,nosuid,size=16m
  -e SEN_QA_RELEASE_ID="$SEN_QA_RELEASE_ID"
  -v "$SEN_QA_SOURCE_ROOT:/sen-qa/source:rw"
  -v "$SEN_QA_ARTIFACT_ROOT:/sen-qa/artifacts:rw"
  -v "$SEN_QA_PRIVATE_EVAL_ROOT:/sen-qa/private-eval:rw"
)
run_probe() {
  local identity="$1"
  local contract="$2"
  if ! docker "${COMMON_ARGS[@]}" --user "$identity" \
    "$SEN_QA_PERMISSION_PROBE_IMAGE" /bin/sh -eu -c "$contract"; then
    release_fail storage_permission_probe_failed
  fi
}

run_probe "$SEN_QA_POLICY_INGESTION_UID:$SEN_QA_POLICY_INGESTION_UID" \
  'test -r /sen-qa/source/.sen-qa-permission-probe; test ! -w /sen-qa/source/.sen-qa-permission-probe; test -w /sen-qa/artifacts/.sen-qa-ingestion-probe'
run_probe "$SEN_QA_POLICY_SEARCH_UID:$SEN_QA_POLICY_SEARCH_UID" \
  'test -r /sen-qa/artifacts/.sen-qa-canonical-permission-probe; test ! -w /sen-qa/artifacts/.sen-qa-canonical-permission-probe; test ! -r /sen-qa/artifacts/.sen-qa-review-permission-probe; test ! -r /sen-qa/private-eval/.sen-qa-permission-probe'
run_probe "$REVIEWER_UID:$SEN_QA_POLICY_REVIEWER_GID" \
  'test -r /sen-qa/artifacts/.sen-qa-review-permission-probe; test -w /sen-qa/artifacts/.sen-qa-review-permission-probe; test ! -r /sen-qa/private-eval/.sen-qa-permission-probe'
run_probe "$SEN_QA_POLICY_EVALUATOR_UID:$SEN_QA_POLICY_EVALUATOR_UID" \
  'test -r /sen-qa/private-eval/.sen-qa-permission-probe; test ! -w /sen-qa/private-eval/.sen-qa-permission-probe'

printf 'storage_permission_probes=4 failed=0\n'
