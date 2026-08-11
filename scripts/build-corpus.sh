#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=scripts/_release-common.sh
source "$SCRIPT_DIR/_release-common.sh"

# This legacy entrypoint used one Paddle authority for every OCR year. The
# canonical mixed corpus requires Paddle v2 for 2023 and Apple Vision v3 for
# 2024/2025, so continuing here would fabricate provenance. Stage already
# verified raw pages with scripts/stage-review-corpus.sh instead.
release_init
release_fail legacy_ocr_build_blocked 2
