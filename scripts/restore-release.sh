#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
source "$SCRIPT_DIR/_release-common.sh"
release_init

if [[ $# -ne 2 || "$1" != /* || ( "$2" != materialize && "$2" != attest ) ]]; then
  release_fail restore_arguments_invalid 2
fi
TARGET_ROOT="$1"
MODE="$2"
release_require_pinned_image SEN_QA_BACKUP_IMAGE
release_require SEN_QA_ATTESTATION_PUBLIC_KEY_FILE
release_require_regular "$SEN_QA_ATTESTATION_PUBLIC_KEY_FILE" attestation_key_missing

BUNDLE_ROOT="$TARGET_ROOT/$SEN_QA_RELEASE_ID"
SIGNATURE="$TARGET_ROOT/$SEN_QA_RELEASE_ID.bundle-manifest.minisig"
PRIVATE_RESTORE="$SEN_QA_PRIVATE_EVAL_ROOT/restore/$SEN_QA_RELEASE_ID"
ARTIFACT_RESTORE="$SEN_QA_ARTIFACT_ROOT/restores/$SEN_QA_RELEASE_ID"
ATTESTATION_ROOT="$SEN_QA_ARTIFACT_ROOT/restore-attestations/$SEN_QA_RELEASE_ID"
if [[ ! -d "$BUNDLE_ROOT" || -L "$BUNDLE_ROOT" ]]; then
  release_fail backup_bundle_missing
fi
release_require_regular "$SIGNATURE" backup_signature_missing

docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --user "$(id -u):$(id -g)" \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  -v "$SEN_QA_ATTESTATION_PUBLIC_KEY_FILE:/sen-qa/signing.pub:ro" \
  -v "$BUNDLE_ROOT/bundle-manifest.json:/sen-qa/bundle-manifest.json:ro" \
  -v "$SIGNATURE:/sen-qa/bundle-manifest.minisig:ro" \
  "$SEN_QA_BACKUP_IMAGE" minisign -V -p /sen-qa/signing.pub \
  -m /sen-qa/bundle-manifest.json -x /sen-qa/bundle-manifest.minisig
(cd -- "$SCRIPT_DIR/.." && uv run python -m src.cli verify-backup --root "$BUNDLE_ROOT")

if [[ "$MODE" == materialize ]]; then
  release_require SEN_QA_BACKUP_IDENTITY_FILE
  release_require_regular "$SEN_QA_BACKUP_IDENTITY_FILE" backup_identity_missing
  if [[ -e "$PRIVATE_RESTORE" || -L "$PRIVATE_RESTORE" ||
        -e "$ARTIFACT_RESTORE" || -L "$ARTIFACT_RESTORE" ]]; then
    release_fail restore_target_exists
  fi
  install -d -m 0700 "$PRIVATE_RESTORE" "$SEN_QA_ARTIFACT_ROOT/restores"

  docker run --rm --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges --user "$(id -u):$(id -g)" \
    --tmpfs /tmp:rw,noexec,nosuid,size=64m \
    -v "$SEN_QA_BACKUP_IDENTITY_FILE:/sen-qa/identity.txt:ro" \
    -v "$BUNDLE_ROOT/blind-labels.age:/sen-qa/blind-labels.age:ro" \
    -v "$PRIVATE_RESTORE:/sen-qa/private-restore:rw" \
    "$SEN_QA_BACKUP_IMAGE" age -d -i /sen-qa/identity.txt \
    -o /sen-qa/private-restore/retrieval-blind-labels.jsonl \
    /sen-qa/blind-labels.age
  chmod 0600 "$PRIVATE_RESTORE/retrieval-blind-labels.jsonl"
  if [[ "$(release_dir_mode "$PRIVATE_RESTORE")" != 700 ]]; then
    release_fail restore_permissions_invalid
  fi

  (cd -- "$SCRIPT_DIR/.." && uv run python -m src.cli materialize-backup \
    --root "$BUNDLE_ROOT" --output "$ARTIFACT_RESTORE")
  printf 'release_id=%s stage=restore_pending failed=0\n' "$SEN_QA_RELEASE_ID"
  exit 0
fi

release_require SEN_QA_RESTORE_DEV_GOLD_FILE
release_require SEN_QA_RESTORE_BLIND_GOLD_FILE
release_require SEN_QA_RESTORE_OBSERVATION_ROOT
release_require SEN_QA_ATTESTATION_SECRET_KEY_FILE
release_require_regular "$SEN_QA_RESTORE_DEV_GOLD_FILE" public_goldset_missing
release_require_regular "$SEN_QA_RESTORE_BLIND_GOLD_FILE" public_goldset_missing
release_require_regular "$SEN_QA_ATTESTATION_SECRET_KEY_FILE" signing_key_missing
release_require_regular "$PRIVATE_RESTORE/retrieval-blind-labels.jsonl" blind_labels_missing
release_require_regular "$ARTIFACT_RESTORE/canonical.sqlite3" canonical_database_missing
release_require_regular "$ARTIFACT_RESTORE/qdrant.snapshot" qdrant_snapshot_missing
if [[ ! -d "$SEN_QA_RESTORE_OBSERVATION_ROOT" || -L "$SEN_QA_RESTORE_OBSERVATION_ROOT" ]]; then
  release_fail evaluation_observations_missing
fi
for name in ingestion substring lexical dense hybrid; do
  release_require_regular \
    "$SEN_QA_RESTORE_OBSERVATION_ROOT/$name.jsonl" \
    evaluation_observations_missing
done
if [[ -e "$ATTESTATION_ROOT" || -L "$ATTESTATION_ROOT" ]]; then
  release_fail restore_attestation_exists
fi
install -d -m 0700 "$SEN_QA_ARTIFACT_ROOT/restore-attestations" "$ATTESTATION_ROOT"
REPORT="$ATTESTATION_ROOT/evaluation-report.json"
ATTESTATION="$ATTESTATION_ROOT/restore.json"

(cd -- "$SCRIPT_DIR/.." && uv run python -m src.cli evaluate-release-evidence \
  --release-id "$SEN_QA_RELEASE_ID" \
  --canonical-db "$ARTIFACT_RESTORE/canonical.sqlite3" \
  --dev-gold "$SEN_QA_RESTORE_DEV_GOLD_FILE" \
  --blind-gold "$SEN_QA_RESTORE_BLIND_GOLD_FILE" \
  --blind-labels "$PRIVATE_RESTORE/retrieval-blind-labels.jsonl" \
  --ingestion-observations "$SEN_QA_RESTORE_OBSERVATION_ROOT/ingestion.jsonl" \
  --substring-observations "$SEN_QA_RESTORE_OBSERVATION_ROOT/substring.jsonl" \
  --lexical-observations "$SEN_QA_RESTORE_OBSERVATION_ROOT/lexical.jsonl" \
  --dense-observations "$SEN_QA_RESTORE_OBSERVATION_ROOT/dense.jsonl" \
  --hybrid-observations "$SEN_QA_RESTORE_OBSERVATION_ROOT/hybrid.jsonl" \
  --output "$REPORT")
(cd -- "$SCRIPT_DIR/.." && uv run python -m src.cli create-restore-attestation \
  --bundle-root "$BUNDLE_ROOT" \
  --restored-root "$ARTIFACT_RESTORE" \
  --evaluation-report "$REPORT" \
  --output "$ATTESTATION" \
  --release-id "$SEN_QA_RELEASE_ID")

docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --user "$(id -u):$(id -g)" \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  -v "$SEN_QA_ATTESTATION_SECRET_KEY_FILE:/sen-qa/signing.key:ro" \
  -v "$ATTESTATION_ROOT:/sen-qa/attestations:rw" \
  "$SEN_QA_BACKUP_IMAGE" minisign -S -s /sen-qa/signing.key \
  -m /sen-qa/attestations/restore.json \
  -x /sen-qa/attestations/restore.json.minisig
printf 'release_id=%s stage=restore_attested failed=0\n' "$SEN_QA_RELEASE_ID"
