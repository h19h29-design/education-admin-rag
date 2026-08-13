#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
source "$SCRIPT_DIR/_release-common.sh"
release_init

RELEASE_ROOT="$SEN_QA_ARTIFACT_ROOT/releases/$SEN_QA_RELEASE_ID"
CANONICAL_DB="$RELEASE_ROOT/canonical/canonical.sqlite3"
RETRIEVAL_INDEX="$RELEASE_ROOT/indexes/qdrant.snapshot"
DEV_GOLD="$SCRIPT_DIR/../data/eval/retrieval-dev.jsonl"
BLIND_GOLD="$SCRIPT_DIR/../data/eval/retrieval-blind.jsonl"
BLIND_LABELS="$SEN_QA_PRIVATE_EVAL_ROOT/retrieval-blind-labels.jsonl"
OBSERVATION_ROOT="$SEN_QA_PRIVATE_EVAL_ROOT/observations/$SEN_QA_RELEASE_ID"
REPORT_DIR="$RELEASE_ROOT/reports"
REPORT="$REPORT_DIR/evaluation-report.json"

release_require_regular "$CANONICAL_DB" canonical_database_missing
release_require_regular "$RETRIEVAL_INDEX" qdrant_snapshot_missing
release_require_regular "$BLIND_LABELS" blind_labels_missing
release_require_regular "$DEV_GOLD" public_goldset_missing
release_require_regular "$BLIND_GOLD" public_goldset_missing
for name in ingestion substring lexical dense hybrid; do
  release_require_regular "$OBSERVATION_ROOT/$name.jsonl" evaluation_observations_missing
done
if [[ -e "$REPORT" || -L "$REPORT" ]]; then
  release_fail evaluation_report_exists
fi
install -d -m 0700 "$REPORT_DIR"

(cd -- "$SCRIPT_DIR/.." && uv run python -m src.cli evaluate-release-evidence \
  --release-id "$SEN_QA_RELEASE_ID" \
  --canonical-db "$CANONICAL_DB" \
  --retrieval-index "$RETRIEVAL_INDEX" \
  --dev-gold "$DEV_GOLD" \
  --blind-gold "$BLIND_GOLD" \
  --blind-labels "$BLIND_LABELS" \
  --ingestion-observations "$OBSERVATION_ROOT/ingestion.jsonl" \
  --substring-observations "$OBSERVATION_ROOT/substring.jsonl" \
  --lexical-observations "$OBSERVATION_ROOT/lexical.jsonl" \
  --dense-observations "$OBSERVATION_ROOT/dense.jsonl" \
  --hybrid-observations "$OBSERVATION_ROOT/hybrid.jsonl" \
  --output "$REPORT")
