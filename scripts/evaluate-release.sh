#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
source "$SCRIPT_DIR/_release-common.sh"
release_init

release_require_regular "$SEN_QA_PRIVATE_EVAL_ROOT/retrieval-blind-labels.jsonl" blind_labels_missing
release_require_regular "$SCRIPT_DIR/../data/eval/retrieval-dev.jsonl" public_goldset_missing
release_require_regular "$SCRIPT_DIR/../data/eval/retrieval-blind.jsonl" public_goldset_missing
release_fail retrieval_evaluation_driver_required 3
