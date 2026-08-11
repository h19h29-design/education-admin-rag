# Parser Quarantine Resolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a sealed, value-free human resolution authority and closed deterministic reparse gate for parser quarantines.

**Architecture:** A focused ingestion module owns canonical sidecar models, strict I/O, occurrence coverage, event-chain verification, and the annotation projection. Existing annual parsers remain the only parsing authority; staging receives output only after the internal year dispatch returns zero quarantines.

**Tech Stack:** Python 3.11, Pydantic v2, pytest, Typer, existing parser and staging models.

## Global Constraints

- Preserve every original quarantine occurrence exactly once, including repeated identical rows.
- Store no raw source text in the sidecar, logs, or public errors.
- Bind release, registry, manifest, raw, parser, quarantine SHA-256 authorities and count.
- Use bounded no-follow regular-file I/O and canonical bytes.
- Upstream extraction failures remain unresolved until re-extraction.
- Never auto-approve a case or weaken the v3 finalization gate.
- Do not modify `src/ingestion/parse_common.py` or `src/corpus/finalize.py` in this workstream.

---

### Task 1: Canonical authority and exact occurrence coverage

**Files:**
- Create: `src/ingestion/quarantine_review.py`
- Create: `tests/ingestion/test_quarantine_review.py`

**Interfaces:**
- Produces: `create_resolution_draft(...) -> bytes`, `load_resolution_authority(path, expected_sha256=...) -> VerifiedQuarantineResolutionAuthority`.

- [ ] Write a failing test with two identical rows and assert two stable occurrence IDs and ordinals.
- [ ] Run the focused test and confirm the import or behavior fails for the missing feature.
- [ ] Implement strict models, canonical serialization, exact authority fields, value-free occurrence records, and owner-only bounded no-follow reads/writes.
- [ ] Add failing tests for noncanonical JSON, symlinks/FIFOs, modes, oversized input, duplicate keys, extra fields, wrong external SHA, and missing/duplicate occurrence coverage.
- [ ] Implement the minimal validation and rerun the focused tests to green.

### Task 2: Dispositions and broker-compatible event chain

**Files:**
- Modify: `src/ingestion/quarantine_review.py`
- Modify: `tests/ingestion/test_quarantine_review.py`

**Interfaces:**
- Produces: `append_resolution_event(...) -> bytes` and canonical `ResolutionEvent` envelopes.

- [ ] Write failing tests for each disposition, upstream failure rejection, actor/event identifiers, prior-event hash, reviewed occurrence hash, and no-text output.
- [ ] Run the tests and confirm expected failures.
- [ ] Implement append-only event application with one latest state per occurrence and exact chain verification.
- [ ] Add replay, reordering, forged actor, forged reviewed hash, and contradictory annotation tests; implement only the required rejection paths.
- [ ] Run the focused suite to green.

### Task 3: Closed annotation projection and annual reparse

**Files:**
- Modify: `src/ingestion/quarantine_review.py`
- Modify: `tests/ingestion/test_quarantine_review.py`

**Interfaces:**
- Produces: `reparse_with_resolution(pages_by_year, verified_authority) -> tuple[ParseResult, ...]`.

- [ ] Write failing tests proving annotations can only select exact existing page/bbox/text-hash spans and never inject text.
- [ ] Implement exact span lookup and conflict-free semantic projection.
- [ ] Write failing tests for closed year dispatch, confirmed-noncase exclusion, unresolved blocking, upstream failures, and a nonzero reparse quarantine.
- [ ] Implement internal dispatch to `parse_2020`, `parse_2021_2022`, `parse_2023`, and `parse_2024_2025`; reject unsupported years and accept only zero-quarantine results.
- [ ] Run focused parser and resolution tests to green.

### Task 4: Restaging bridge and runbook

**Files:**
- Modify: `src/corpus/staging.py` only if a narrow public constructor is required after the shared staging work stabilizes.
- Modify: `src/cli.py` only if a dedicated value-free command is required after the shared CLI work stabilizes.
- Modify: `docs/runbooks/manual-review.md`
- Modify: `tests/corpus/test_staging.py` and `tests/test_cli.py` only for observable bridge behavior.

**Interfaces:**
- Consumes: verified resolution authority and zero-quarantine closed reparse results.
- Produces: a new `PreparedReviewBatch` with zero parser quarantine bytes/count; never overwrites the original package.

- [ ] Write a failing integration test that preserves all source authorities while restaging zero quarantines and leaves every candidate `needs_review`.
- [ ] Implement the smallest bridge using existing sealed staging constructors.
- [ ] Write runbook commands that separate draft creation, broker events, offline external SHA capture, verification, and new-directory restaging; state the NAS broker integration blocker explicitly.
- [ ] Run resolution, staging, CLI, and finalizer regression suites.
- [ ] Run Ruff, formatting check, strict mypy, full pytest, and `git diff --check`; do not stage or commit.

