# Source intake and verification

The six approved source PDFs remain outside the repository. Store them directly
under the directory selected by `SEN_QA_SOURCE_ROOT`, using the exact filenames
in `data/manifests/sen_qa_sources.json`; do not rename, transform, or commit
them.

Verify an intake before any extraction:

```bash
SEN_QA_SOURCE_ROOT=/volume1/education-admin/source \
  uv run python -m src.cli verify-sources --manifest data/manifests/sen_qa_sources.json
```

A successful run prints `verified=6 changed=0 failed=0`. Any missing file,
root escape, filename change, SHA-256 change, PDF page-count change, or page
geometry change exits nonzero. The command deliberately reports only document
IDs and verification reasons, never PDF text.

The manifest is a closed six-edition contract: it must contain exactly 2020,
2021, 2022, 2023, 2024, and 2025 in that order. A missing, substituted,
duplicate, reordered, empty, malformed, or schema-invalid manifest fails before
source verification. Manifest-load failures count as one intake failure and
print `verified=0 changed=0 failed=1`; per-document source failures continue so
the final line always carries complete counts.

The manifest tolerates only a `0.01 pt` page-size coordinate difference. This
allows harmless PDF floating-point serialization rounding while still detecting
changed media-box geometry. Front cover, contents, and trailing non-body pages
map to no citation label; callers must use the documented per-source body range
and offset instead of inventing a label.
