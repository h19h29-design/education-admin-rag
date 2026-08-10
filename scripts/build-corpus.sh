#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=scripts/_release-common.sh
source "$SCRIPT_DIR/_release-common.sh"
release_init
release_require_pinned_image SEN_QA_INGESTION_IMAGE
release_require SEN_QA_INGESTION_IMAGE_DIGEST
if [[ ! "$SEN_QA_INGESTION_IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  release_fail ingestion_image_digest_invalid 2
fi

RELEASE_ROOT="$SEN_QA_ARTIFACT_ROOT/releases/$SEN_QA_RELEASE_ID"
if [[ -e "$RELEASE_ROOT" || -L "$RELEASE_ROOT" ]]; then
  release_fail release_output_exists
fi
install -d -m 0700 "$RELEASE_ROOT/raw-pages/native" "$RELEASE_ROOT/reports"

docker run --rm --network none --read-only --cap-drop ALL \
  --user "$(id -u):$(id -g)" \
  --security-opt no-new-privileges --tmpfs /tmp:rw,noexec,nosuid,size=512m \
  -e SEN_QA_SOURCE_ROOT=/sen-qa/source \
  -v "$SEN_QA_SOURCE_ROOT:/sen-qa/source:ro" \
  -v "$RELEASE_ROOT:/sen-qa/artifacts:rw" \
  "$SEN_QA_INGESTION_IMAGE" verify-sources \
  --manifest /work/data/manifests/sen_qa_sources.json \
  --source-root /sen-qa/source

docker run --rm --network none --read-only --cap-drop ALL \
  --user "$(id -u):$(id -g)" \
  --security-opt no-new-privileges --tmpfs /tmp:rw,noexec,nosuid,size=512m \
  -e SEN_QA_SOURCE_ROOT=/sen-qa/source \
  -v "$SEN_QA_SOURCE_ROOT:/sen-qa/source:ro" \
  -v "$RELEASE_ROOT:/sen-qa/artifacts:rw" \
  "$SEN_QA_INGESTION_IMAGE" extract-native \
  --manifest /work/data/manifests/sen_qa_sources.json \
  --years 2020,2021,2022 --output /sen-qa/artifacts/raw-pages/native

for spec in 2023:168 2024:324 2025:314; do
  year="${spec%%:*}"
  pages="${spec##*:}"
  install -d -m 0700 "$RELEASE_ROOT/raw-pages/ocr-$year"
  docker run --rm --network none --read-only --cap-drop ALL \
    --user "$(id -u):$(id -g)" \
    --security-opt no-new-privileges --tmpfs /tmp:rw,noexec,nosuid,size=2g \
    -e SEN_QA_SOURCE_ROOT=/sen-qa/source \
    -e SEN_QA_INGESTION_IMAGE_DIGEST="$SEN_QA_INGESTION_IMAGE_DIGEST" \
    -v "$SEN_QA_SOURCE_ROOT:/sen-qa/source:ro" \
    -v "$RELEASE_ROOT:/sen-qa/artifacts:rw" \
    "$SEN_QA_INGESTION_IMAGE" extract-ocr --year "$year" --pages "1-$pages" \
    --output "/sen-qa/artifacts/raw-pages/ocr-$year"
done

docker run --rm --network none --read-only --cap-drop ALL \
  --user "$(id -u):$(id -g)" \
  --security-opt no-new-privileges --tmpfs /tmp:rw,noexec,nosuid,size=512m \
  -e SEN_QA_INGESTION_IMAGE_DIGEST="$SEN_QA_INGESTION_IMAGE_DIGEST" \
  -v "$RELEASE_ROOT:/sen-qa/artifacts:rw" \
  "$SEN_QA_INGESTION_IMAGE" stage-review-corpus \
  --manifest /work/data/manifests/sen_qa_sources.json \
  --input-root /sen-qa/artifacts/raw-pages \
  --output-root /sen-qa/artifacts \
  --release-id "$SEN_QA_RELEASE_ID" \
  --ingestion-version "image-${SEN_QA_INGESTION_IMAGE_DIGEST#sha256:}"

printf 'release_id=%s stage=review_pending failed=0\n' "$SEN_QA_RELEASE_ID"
