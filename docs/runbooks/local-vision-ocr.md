# Local Apple Vision OCR authority

This is the only approved path for the 2024 and 2025 Apple Vision v3 pages.
It runs on the Mac, without Docker or network access, and stops at the private
review package. It never uploads source PDFs, changes a production alias, or
marks review complete.

Use absolute paths whose existing parent directories contain no symbolic links.
Every destination below must be new. The authority command refuses overwrite,
FIFO/device inputs, path aliases, non-macOS hosts, missing Xcode tools, and
runtime hash drift without printing paths or source content.

## 1. Build the helper and runtime authority

Create a private workspace outside the repository. `RAW_ROOT` must already
contain the verified native 2020-2022 and Paddle v2 2023 JSONL files before the
final staging step.

```bash
export VISION_ROOT=/ABSOLUTE/PRIVATE/vision-$SEN_QA_RELEASE_ID
export RAW_ROOT="$VISION_ROOT/raw-pages"
install -d -m 0700 \
  "$VISION_ROOT/bin" \
  "$VISION_ROOT/authority" \
  "$VISION_ROOT/runs" \
  "$RAW_ROOT/ocr-2024" \
  "$RAW_ROOT/ocr-2025"

uv run python -m src.ingestion.vision_authority build-runtime \
  --helper-output "$VISION_ROOT/bin/apple-vision-ocr" \
  --provenance-output "$VISION_ROOT/authority/vision-runtime-provenance.json"
```

The command invokes `/usr/bin/xcrun` itself, compiles the checked-in Swift
source with the fixed `swiftc -O` contract, reads the live compiler and macOS
SDK versions, executes the resulting helper, and binds the live helper source,
binary, Python adapter, extraction pipeline, and PyMuPDF version. Record its
`helper_sha256` and `runtime_sha256` report independently; do not type version
strings into the authority builder.

The existing extraction boundary rechecks the same values. Load them from the
canonical provenance file rather than inventing them:

```bash
export VISION_HELPER_SHA256="$(shasum -a 256 \
  "$VISION_ROOT/bin/apple-vision-ocr" | awk '{print $1}')"
export VISION_RUNTIME_SHA256="$(shasum -a 256 \
  "$VISION_ROOT/authority/vision-runtime-provenance.json" | awk '{print $1}')"
export VISION_SWIFT_VERSION="$(jq -er '.swift_version' \
  "$VISION_ROOT/authority/vision-runtime-provenance.json")"
export VISION_SDK_VERSION="$(jq -er '.sdk_version' \
  "$VISION_ROOT/authority/vision-runtime-provenance.json")"
```

## 2. Run each document twice

`SEN_QA_SOURCE_ROOT` is the verified six-PDF source directory from the source
intake runbook. Each run writes to a new directory. Both years must exit `0`:
2024 with 324 extracted records and 2025 with 314 extracted records. Any other
page count, quarantine, or exit status requires investigation and must not be
staged.

```bash
SEN_QA_INGESTION_IMAGE_DIGEST= \
uv run python -m src.cli extract-vision-ocr \
  --year 2024 --pages 1-324 \
  --output "$VISION_ROOT/runs/2024-run-1" \
  --manifest data/manifests/sen_qa_sources.json \
  --helper "$VISION_ROOT/bin/apple-vision-ocr" \
  --helper-sha256 "$VISION_HELPER_SHA256" \
  --helper-source scripts/apple-vision-ocr.swift \
  --swift-version "$VISION_SWIFT_VERSION" \
  --sdk-version "$VISION_SDK_VERSION" \
  --runtime-provenance "$VISION_ROOT/authority/vision-runtime-provenance.json" \
  --expected-runtime-provenance-sha256 "$VISION_RUNTIME_SHA256"

SEN_QA_INGESTION_IMAGE_DIGEST= \
uv run python -m src.cli extract-vision-ocr \
  --year 2024 --pages 1-324 \
  --output "$VISION_ROOT/runs/2024-run-2" \
  --manifest data/manifests/sen_qa_sources.json \
  --helper "$VISION_ROOT/bin/apple-vision-ocr" \
  --helper-sha256 "$VISION_HELPER_SHA256" \
  --helper-source scripts/apple-vision-ocr.swift \
  --swift-version "$VISION_SWIFT_VERSION" \
  --sdk-version "$VISION_SDK_VERSION" \
  --runtime-provenance "$VISION_ROOT/authority/vision-runtime-provenance.json" \
  --expected-runtime-provenance-sha256 "$VISION_RUNTIME_SHA256"

SEN_QA_INGESTION_IMAGE_DIGEST= \
uv run python -m src.cli extract-vision-ocr \
  --year 2025 --pages 1-314 \
  --output "$VISION_ROOT/runs/2025-run-1" \
  --manifest data/manifests/sen_qa_sources.json \
  --helper "$VISION_ROOT/bin/apple-vision-ocr" \
  --helper-sha256 "$VISION_HELPER_SHA256" \
  --helper-source scripts/apple-vision-ocr.swift \
  --swift-version "$VISION_SWIFT_VERSION" \
  --sdk-version "$VISION_SDK_VERSION" \
  --runtime-provenance "$VISION_ROOT/authority/vision-runtime-provenance.json" \
  --expected-runtime-provenance-sha256 "$VISION_RUNTIME_SHA256"

SEN_QA_INGESTION_IMAGE_DIGEST= \
uv run python -m src.cli extract-vision-ocr \
  --year 2025 --pages 1-314 \
  --output "$VISION_ROOT/runs/2025-run-2" \
  --manifest data/manifests/sen_qa_sources.json \
  --helper "$VISION_ROOT/bin/apple-vision-ocr" \
  --helper-sha256 "$VISION_HELPER_SHA256" \
  --helper-source scripts/apple-vision-ocr.swift \
  --swift-version "$VISION_SWIFT_VERSION" \
  --sdk-version "$VISION_SDK_VERSION" \
  --runtime-provenance "$VISION_ROOT/authority/vision-runtime-provenance.json" \
  --expected-runtime-provenance-sha256 "$VISION_RUNTIME_SHA256"
```

Require byte-for-byte equality, then install one copy into the closed raw-page
layout. Do not inspect or print JSONL content during this check.

```bash
cmp -s "$VISION_ROOT/runs/2024-run-1/sen-qa-2024.jsonl" \
  "$VISION_ROOT/runs/2024-run-2/sen-qa-2024.jsonl"
cmp -s "$VISION_ROOT/runs/2025-run-1/sen-qa-2025.jsonl" \
  "$VISION_ROOT/runs/2025-run-2/sen-qa-2025.jsonl"
test ! -e "$RAW_ROOT/ocr-2024/sen-qa-2024.jsonl"
test ! -e "$RAW_ROOT/ocr-2025/sen-qa-2025.jsonl"
install -m 0600 "$VISION_ROOT/runs/2024-run-1/sen-qa-2024.jsonl" \
  "$RAW_ROOT/ocr-2024/sen-qa-2024.jsonl"
install -m 0600 "$VISION_ROOT/runs/2025-run-1/sen-qa-2025.jsonl" \
  "$RAW_ROOT/ocr-2025/sen-qa-2025.jsonl"
```

## 3. Build the mixed authority lock and stage review

The two years are independently hash-checked even when the same runtime file
is used. The lock builder supplies the fixed, digest-qualified Paddle authority
for 2023 and reports only one file SHA, one self SHA, and the fixed entry count.

```bash
uv run python -m src.ingestion.vision_authority build-lock \
  --runtime-2024 "$VISION_ROOT/authority/vision-runtime-provenance.json" \
  --expected-runtime-2024-sha256 "$VISION_RUNTIME_SHA256" \
  --runtime-2025 "$VISION_ROOT/authority/vision-runtime-provenance.json" \
  --expected-runtime-2025-sha256 "$VISION_RUNTIME_SHA256" \
  --authority-output "$VISION_ROOT/authority/ocr-authority-lock.json"

export SEN_QA_RAW_PAGES_ROOT="$RAW_ROOT"
export SEN_QA_OCR_AUTHORITY_LOCK="$VISION_ROOT/authority/ocr-authority-lock.json"
export SEN_QA_OCR_AUTHORITY_LOCK_SHA256="$(shasum -a 256 \
  "$SEN_QA_OCR_AUTHORITY_LOCK" | awk '{print $1}')"
bash scripts/stage-review-corpus.sh
```

The staging script must stop at `stage=review_pending`. Parser quarantines and
all candidate cases remain ineligible for search and answers until human review
produces the separate ready attestation.
