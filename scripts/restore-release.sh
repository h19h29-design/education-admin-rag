#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
source "$SCRIPT_DIR/_release-common.sh"
release_init
release_require_pinned_image SEN_QA_BACKUP_IMAGE
release_require SEN_QA_BACKUP_IDENTITY_FILE
release_require SEN_QA_ATTESTATION_PUBLIC_KEY_FILE
release_require_regular "$SEN_QA_BACKUP_IDENTITY_FILE" backup_identity_missing
release_require_regular "$SEN_QA_ATTESTATION_PUBLIC_KEY_FILE" attestation_key_missing
if [[ $# -ne 1 || "$1" != /* ]]; then
  release_fail backup_target_invalid 2
fi
TARGET_ROOT="$1"
BUNDLE_ROOT="$TARGET_ROOT/$SEN_QA_RELEASE_ID"
SIGNATURE="$TARGET_ROOT/$SEN_QA_RELEASE_ID.bundle-manifest.minisig"
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

PRIVATE_RESTORE="$SEN_QA_PRIVATE_EVAL_ROOT/restore/$SEN_QA_RELEASE_ID"
ARTIFACT_RESTORE="$SEN_QA_ARTIFACT_ROOT/restores/$SEN_QA_RELEASE_ID"
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
release_fail restore_evaluation_driver_required 3
