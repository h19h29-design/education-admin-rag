#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
source "$SCRIPT_DIR/_release-common.sh"
release_init
release_require_pinned_image SEN_QA_BACKUP_IMAGE

RELEASE_ROOT="$SEN_QA_ARTIFACT_ROOT/releases/$SEN_QA_RELEASE_ID"
for relative in canonical/manifest.json canonical/canonical.sqlite3 indexes/lexical.sqlite3 indexes/index-attestation.json reports/evaluation-report.json; do
  release_require_regular "$RELEASE_ROOT/$relative" release_evidence_missing
done
EVIDENCE="$RELEASE_ROOT/reports/release-evidence.json"
if [[ -e "$EVIDENCE" || -L "$EVIDENCE" ]]; then
  release_fail release_evidence_exists
fi
release_require SEN_QA_ATTESTATION_SECRET_KEY_FILE
release_require_regular "$SEN_QA_ATTESTATION_SECRET_KEY_FILE" signing_key_missing
ATTESTATION_DIR="$RELEASE_ROOT/attestations"
ATTESTATION="$ATTESTATION_DIR/verification.json"
SIGNATURE="$ATTESTATION_DIR/verification.json.minisig"
if [[ -e "$ATTESTATION" || -L "$ATTESTATION" || -e "$SIGNATURE" || -L "$SIGNATURE" ]]; then
  release_fail verification_attestation_exists
fi
install -d -m 0700 "$ATTESTATION_DIR"
(cd -- "$SCRIPT_DIR/.." && uv run python -m src.cli assemble-release-evidence \
  --release-id "$SEN_QA_RELEASE_ID" \
  --canonical-manifest "$RELEASE_ROOT/canonical/manifest.json" \
  --canonical-db "$RELEASE_ROOT/canonical/canonical.sqlite3" \
  --lexical-index "$RELEASE_ROOT/indexes/lexical.sqlite3" \
  --index-evidence "$RELEASE_ROOT/indexes/index-attestation.json" \
  --evaluation-report "$RELEASE_ROOT/reports/evaluation-report.json" \
  --output "$EVIDENCE")
(cd -- "$SCRIPT_DIR/.." && uv run python -m src.cli create-verification-attestation \
  --evidence "$EVIDENCE" \
  --output "$ATTESTATION" --release-id "$SEN_QA_RELEASE_ID")

docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --user "$(id -u):$(id -g)" \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  -v "$SEN_QA_ATTESTATION_SECRET_KEY_FILE:/sen-qa/signing.key:ro" \
  -v "$ATTESTATION_DIR:/sen-qa/attestations:rw" \
  "$SEN_QA_BACKUP_IMAGE" minisign -S -s /sen-qa/signing.key \
  -m /sen-qa/attestations/verification.json \
  -x /sen-qa/attestations/verification.json.minisig
printf 'release_id=%s verification_attested=1 failed=0\n' "$SEN_QA_RELEASE_ID"
