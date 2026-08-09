#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
source "$SCRIPT_DIR/_release-common.sh"
release_init
release_require_pinned_image SEN_QA_INDEXER_IMAGE

RELEASE_ROOT="$SEN_QA_ARTIFACT_ROOT/releases/$SEN_QA_RELEASE_ID"
CANONICAL_DB="$RELEASE_ROOT/canonical/canonical.sqlite3"
READY="$RELEASE_ROOT/review/review-ready.attestation.json"
release_require_regular "$CANONICAL_DB" canonical_bundle_missing
release_require_regular "$READY" review_checkpoint_missing
install -d -m 0700 "$RELEASE_ROOT/indexes"

docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --tmpfs /tmp:rw,noexec,nosuid,size=512m \
  -v "$RELEASE_ROOT:/sen-qa/release:rw" \
  "$SEN_QA_INDEXER_IMAGE" build-lexical-index \
  --canonical-db /sen-qa/release/canonical/canonical.sqlite3 \
  --output /sen-qa/release/indexes/lexical.sqlite3

release_fail dense_index_driver_required 3
