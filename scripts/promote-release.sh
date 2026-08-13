#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
source "$SCRIPT_DIR/_release-common.sh"
release_init

RELEASE_ROOT="$SEN_QA_ARTIFACT_ROOT/releases/$SEN_QA_RELEASE_ID"
release_require_regular "$RELEASE_ROOT/attestations/verification.json" verification_attestation_missing
release_require_regular "$RELEASE_ROOT/attestations/restore.json" restore_attestation_missing
release_fail qdrant_alias_broker_required 3
