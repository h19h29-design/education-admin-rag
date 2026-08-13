#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
source "$SCRIPT_DIR/_release-common.sh"
release_init
release_require_pinned_image SEN_QA_BACKUP_IMAGE
if [[ $# -ne 1 || "$1" != /* ]]; then
  release_fail backup_target_invalid 2
fi
TARGET_ROOT="$1"
if [[ ! -d "$TARGET_ROOT" || -L "$TARGET_ROOT" ]]; then
  release_fail backup_target_invalid 2
fi
TARGET_REAL="$(cd -- "$TARGET_ROOT" && pwd -P)"
for local_root in "$SEN_QA_SOURCE_ROOT" "$SEN_QA_ARTIFACT_ROOT" "$SEN_QA_PRIVATE_EVAL_ROOT"; do
  LOCAL_REAL="$(cd -- "$local_root" && pwd -P)"
  if [[ "$TARGET_REAL" == "$LOCAL_REAL" || "$TARGET_REAL" == "$LOCAL_REAL"/* ||
        "$LOCAL_REAL" == "$TARGET_REAL"/* ]]; then
    release_fail backup_target_not_external 2
  fi
done

release_require SEN_QA_BACKUP_RECIPIENTS_FILE
release_require SEN_QA_ATTESTATION_SECRET_KEY_FILE
release_require_regular "$SEN_QA_BACKUP_RECIPIENTS_FILE" backup_recipients_missing
release_require_regular "$SEN_QA_ATTESTATION_SECRET_KEY_FILE" signing_key_missing
BLIND_LABELS="$SEN_QA_PRIVATE_EVAL_ROOT/retrieval-blind-labels.jsonl"
release_require_regular "$BLIND_LABELS" blind_labels_missing

RELEASE_ROOT="$SEN_QA_ARTIFACT_ROOT/releases/$SEN_QA_RELEASE_ID"
CANONICAL_DB="$RELEASE_ROOT/canonical/canonical.sqlite3"
QDRANT_SNAPSHOT="$RELEASE_ROOT/indexes/qdrant.snapshot"
EVALUATION_REPORT="$RELEASE_ROOT/reports/evaluation-report.json"
SOURCE_MANIFEST="$SCRIPT_DIR/../data/manifests/sen_qa_sources.json"
MODEL_LOCK="$SCRIPT_DIR/../config/models.lock.json"
for required in "$CANONICAL_DB" "$QDRANT_SNAPSHOT" "$EVALUATION_REPORT" "$SOURCE_MANIFEST" "$MODEL_LOCK"; do
  release_require_regular "$required" backup_input_missing
done

BUNDLE_ROOT="$TARGET_ROOT/$SEN_QA_RELEASE_ID"
SIGNATURE="$TARGET_ROOT/$SEN_QA_RELEASE_ID.bundle-manifest.minisig"
if [[ -e "$BUNDLE_ROOT" || -L "$BUNDLE_ROOT" || -e "$SIGNATURE" || -L "$SIGNATURE" ]]; then
  release_fail backup_target_exists
fi

(cd -- "$SCRIPT_DIR/.." && uv run python -m src.cli prepare-backup \
  --output "$BUNDLE_ROOT" \
  --canonical-db "$CANONICAL_DB" \
  --qdrant-snapshot "$QDRANT_SNAPSHOT" \
  --source-manifest "$SOURCE_MANIFEST" \
  --model-lock "$MODEL_LOCK" \
  --evaluation-report "$EVALUATION_REPORT")

docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --user "$(id -u):$(id -g)" \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  -v "$SEN_QA_BACKUP_RECIPIENTS_FILE:/sen-qa/recipients.txt:ro" \
  -v "$BLIND_LABELS:/sen-qa/blind-labels.jsonl:ro" \
  -v "$BUNDLE_ROOT:/sen-qa/backup:rw" \
  "$SEN_QA_BACKUP_IMAGE" age -R /sen-qa/recipients.txt \
  -o /sen-qa/backup/blind-labels.age /sen-qa/blind-labels.jsonl

(cd -- "$SCRIPT_DIR/.." && uv run python -m src.cli backup-manifest \
  --root "$BUNDLE_ROOT" --release-id "$SEN_QA_RELEASE_ID")

docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --user "$(id -u):$(id -g)" \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  -v "$SEN_QA_ATTESTATION_SECRET_KEY_FILE:/sen-qa/signing.key:ro" \
  -v "$BUNDLE_ROOT/bundle-manifest.json:/sen-qa/bundle-manifest.json:ro" \
  -v "$TARGET_ROOT:/sen-qa/target:rw" \
  "$SEN_QA_BACKUP_IMAGE" minisign -S -s /sen-qa/signing.key \
  -m /sen-qa/bundle-manifest.json \
  -x "/sen-qa/target/$SEN_QA_RELEASE_ID.bundle-manifest.minisig"

printf 'release_id=%s backup_created=1 failed=0\n' "$SEN_QA_RELEASE_ID"
