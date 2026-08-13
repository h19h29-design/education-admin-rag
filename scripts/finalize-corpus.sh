#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=scripts/_release-common.sh
source "$SCRIPT_DIR/_release-common.sh"
release_init
release_require_pinned_image SEN_QA_INDEXER_IMAGE
for name in \
  SEN_QA_INGESTION_IMAGE_DIGEST \
  SEN_QA_READY_ATTESTATION_SHA256 \
  SEN_QA_REVIEW_REGISTRY_SHA256 \
  SEN_QA_EMBEDDING_MODEL_LOCK_SHA256 \
  SEN_QA_RUNTIME_FINGERPRINT_SHA256 \
  SEN_QA_MODEL_CACHE_ROOT; do
  release_require "$name"
done
if [[ ! "$SEN_QA_INGESTION_IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  release_fail ingestion_image_digest_invalid 2
fi
for value in \
  "$SEN_QA_READY_ATTESTATION_SHA256" \
  "$SEN_QA_REVIEW_REGISTRY_SHA256" \
  "$SEN_QA_EMBEDDING_MODEL_LOCK_SHA256" \
  "$SEN_QA_RUNTIME_FINGERPRINT_SHA256"; do
  if [[ ! "$value" =~ ^[0-9a-f]{64}$ ]]; then
    release_fail canonical_authority_invalid 2
  fi
done
if [[ "$SEN_QA_MODEL_CACHE_ROOT" != /* || ! -d "$SEN_QA_MODEL_CACHE_ROOT" ||
      -L "$SEN_QA_MODEL_CACHE_ROOT" ]]; then
  release_fail model_cache_root_invalid 2
fi
MODEL_CACHE_REAL="$(cd -- "$SEN_QA_MODEL_CACHE_ROOT" && pwd -P)"
if [[ "$MODEL_CACHE_REAL" != "$SEN_QA_MODEL_CACHE_ROOT" ]]; then
  release_fail model_cache_root_invalid 2
fi

RELEASE_ROOT="$SEN_QA_ARTIFACT_ROOT/releases/$SEN_QA_RELEASE_ID"
if [[ ! -d "$RELEASE_ROOT" || -L "$RELEASE_ROOT" ]]; then
  release_fail release_output_invalid 2
fi
REVIEW_ROOT="$RELEASE_ROOT/review"
READY="$REVIEW_ROOT/review-ready.attestation.json"
release_require_regular "$READY" review_checkpoint_missing
if [[ ! -d "$REVIEW_ROOT" || -L "$REVIEW_ROOT" ]]; then
  release_fail review_checkpoint_missing
fi
if [[ -e "$RELEASE_ROOT/canonical" || -L "$RELEASE_ROOT/canonical" ]]; then
  release_fail canonical_bundle_exists
fi
install -d -m 0700 "$RELEASE_ROOT/reports/canonical-build"

ISSUANCE_ROOT="$SEN_QA_ARTIFACT_ROOT/issuance"
if [[ -e "$ISSUANCE_ROOT" && ( ! -d "$ISSUANCE_ROOT" || -L "$ISSUANCE_ROOT" ) ]]; then
  release_fail issuance_registry_invalid 2
fi
install -d -m 0700 "$ISSUANCE_ROOT"
ISSUANCE_REGISTRY="$ISSUANCE_ROOT/registry.sqlite3"
GENESIS_ARGS=()
if [[ ! -e "$ISSUANCE_REGISTRY" && ! -L "$ISSUANCE_REGISTRY" ]]; then
  if [[ "${SEN_QA_INITIALIZE_ISSUANCE_GENESIS:-0}" != 1 ]]; then
    release_fail issuance_genesis_approval_required 2
  fi
  GENESIS_ARGS=(--initialize-genesis)
else
  release_require_regular "$ISSUANCE_REGISTRY" issuance_registry_invalid
fi

docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --user "$(id -u):$(id -g)" \
  --tmpfs /tmp:rw,noexec,nosuid,size=1g \
  -v "$RELEASE_ROOT:/sen-qa/release:rw" \
  -v "$ISSUANCE_ROOT:/sen-qa/issuance:rw" \
  -v "$SEN_QA_MODEL_CACHE_ROOT:/sen-qa/models:ro" \
  "$SEN_QA_INDEXER_IMAGE" build-canonical-corpus \
  --package /sen-qa/release/review \
  --release-root /sen-qa/release \
  --diagnostics-root /sen-qa/release/reports/canonical-build \
  --issuance-registry /sen-qa/issuance/registry.sqlite3 \
  --release-id "$SEN_QA_RELEASE_ID" \
  --ready-attestation-sha256 "$SEN_QA_READY_ATTESTATION_SHA256" \
  --registry-sha256 "$SEN_QA_REVIEW_REGISTRY_SHA256" \
  --model-lock /work/config/models.lock.json \
  --model-root /sen-qa/models \
  --model-lock-sha256 "$SEN_QA_EMBEDDING_MODEL_LOCK_SHA256" \
  --runtime-fingerprint-sha256 "$SEN_QA_RUNTIME_FINGERPRINT_SHA256" \
  --runtime-lock /work/uv.lock \
  --indexer-image-digest "${SEN_QA_INDEXER_IMAGE##*@}" \
  --container-image "$SEN_QA_INGESTION_IMAGE_DIGEST" \
  "${GENESIS_ARGS[@]}"

release_require_regular "$RELEASE_ROOT/canonical/canonical.sqlite3" canonical_bundle_missing
release_require_regular "$RELEASE_ROOT/canonical/manifest.json" canonical_bundle_missing
printf 'release_id=%s stage=canonical_ready failed=0\n' "$SEN_QA_RELEASE_ID"
