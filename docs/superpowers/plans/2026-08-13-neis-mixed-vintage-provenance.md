# NEIS Mixed-Vintage Provenance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve every NEIS `LOAD_DTM` observation date through source ingestion, candidate review, digest-gated human approval, and strict snapshot verification without dropping or inventing school records.

**Architecture:** Treat observation dates as immutable provenance, not a source-wide shortcut.  Each source manifest entry records a deterministic raw-observation histogram, a normalized-record histogram, and a preserved-record histogram; `sourceAsOf` is present only when the raw-observation histogram has exactly one date.  Candidate creation remains non-public, while review and approval independently reconstruct and authenticate the same summary before the existing atomic pointer publication path can run.

**Tech Stack:** Python 3.12, Pydantic v2, `httpx`, `pytest`, `ruff`, `mypy`, JSON SHA-256/HMAC transaction receipts, existing `CoverageService` and snapshot verifier.

## Global Constraints

- Preserve all 1,415 observed B10 NEIS rows, including the measured distribution `2026-04-23: 1413`, `2026-05-17: 1`, `2026-06-07: 1`; do not delete, coerce, or re-date an observation.
- Reject missing, invalid, duplicate-key, unsorted, empty, non-positive, count-mismatched, or row-distribution-mismatched provenance before writing `current.json`.
- Keep raw API responses, institution names, road addresses, coordinates, credentials, and request headers out of review packets, logs, and CLI success output.
- Preserve existing pagination, raw-byte, B10-region, source-count, coordinate-quality, enrichment, signed-transaction, locking, fsync, and recovery checks.
- A synchronization run may create only `.<snapshot-id>.candidate`; only `approve-institution-snapshot.py` with a freshly recomputed lowercase digest and `data-steward` role may publish `current.json`.
- Keep user-facing map behavior unchanged.  Update only the administrator-only snapshot section of `apps/travel-map/README.md`.
- Do not add credentials, test fixtures, raw source payloads, or generated snapshots under `apps/travel-map/resources/institution-snapshots/`.
- Execute all Python tests with `PYTHONWARNINGS=error` and run commands from the repository root unless a command explicitly changes directory.

## Approved Execution Sequencing

Tasks 1–3 form one compatibility implementation and review unit.  Execute the RED/GREEN test cycles in their listed order, but do not commit an intermediate state that introduces `sourceAsOf: null` while downstream manifest or candidate validation still requires one date.  Make the first implementation commit only after all Task 1–3 contracts pass together; then perform one combined Task 1 review.  Task 4 also removes automatic promotion from the existing synchronizer, because removing the public promotion API while leaving that CLI import/call creates either a bypass shim or an unusable intermediate command.  Task 5 then adds the credential-free review/approval CLIs and administrator documentation.

---

## File Structure

| File | Responsibility after this change |
| --- | --- |
| `apps/travel-map/app/institutions/sources/common.py` | Defines immutable, deterministic observation-date-count helpers and carries raw/normalized histograms in `SourceProvenance`. |
| `apps/travel-map/app/institutions/sources/neis.py` | Preserves each selectable row's own ISO `LOAD_DTM`, collects all raw-page dates, and emits the NEIS provenance histograms. |
| `apps/travel-map/app/institutions/sources/kindergarten.py` and `apps/travel-map/app/institutions/sources/sen.py` | Populate one-date raw/normalized provenance histograms without changing their existing source-date rules. |
| `apps/travel-map/app/institutions/models.py` | Makes source manifest observation histograms strict, ordered, date-valid, count-valid schema fields. |
| `apps/travel-map/app/institutions/snapshot.py` | Requires the new canonical source fields and verifies raw, normalized, and preserved distributions from persisted JSONL before accepting a snapshot. |
| `apps/travel-map/app/institutions/sync.py` | Builds source histograms into manifests, replays them for candidates, produces privacy-safe review packets, and exposes the only digest-gated approval API. |
| `apps/travel-map/scripts/sync-institutions.py` | Builds one candidate and prints a safe candidate-required status; it never promotes. |
| `apps/travel-map/scripts/review-institution-snapshot.py` | Reads and validates a candidate without credentials/network access, then prints one deterministic review packet. |
| `apps/travel-map/scripts/approve-institution-snapshot.py` | Revalidates a candidate under the existing exclusive lock and publishes only after an exact review-digest/role check. |
| `apps/travel-map/tests/institutions/test_sync.py` | Source, candidate, transaction, review, approval, concurrency, recovery, and CLI regressions. |
| `apps/travel-map/tests/institutions/test_snapshot.py` | Strict manifest schema and persisted-row histogram verification regressions. |
| `apps/travel-map/tests/test_release.py` | Confirms an unapproved candidate cannot unblock the staged release context. |
| `apps/travel-map/README.md` | Administrator-only candidate → review → approval instructions and mixed-vintage interpretation. |

## Shared Contract

Implement the following types and helpers before consumers use them.  The tuple representation preserves a canonical order at the source boundary; JSON manifests use an insertion-ordered `dict[str, int]` and must reject, rather than sort, noncanonical input.

```python
ObservationDateCounts = tuple[tuple[str, int], ...]

def observation_date_counts(dates: Iterable[str]) -> ObservationDateCounts: ...
def observation_counts_as_dict(counts: ObservationDateCounts) -> dict[str, int]: ...
def validate_observation_date_counts(
    counts: ObservationDateCounts,
    *,
    expected_total: int,
    label: str,
) -> None: ...

@dataclass(frozen=True)
class SourceProvenance:
    # Existing fields remain, except source_as_of becomes None for multi-vintage input.
    source_as_of: str | None
    source_observation_date_counts: ObservationDateCounts
    normalized_observation_date_counts: ObservationDateCounts
```

The manifest source entry has these three related fields:

```json
{
  "sourceAsOf": null,
  "sourceObservationDateCounts": {
    "2026-04-23": 1413,
    "2026-05-17": 1,
    "2026-06-07": 1
  },
  "normalizedObservationDateCounts": {
    "2026-04-23": 1413,
    "2026-05-17": 1,
    "2026-06-07": 1
  },
  "preservedObservationDateCounts": {}
}
```

`sourceObservationDateCounts` counts every raw source row and therefore sums to `fetchedRowCount`.  `normalizedObservationDateCounts` counts current (not `MISSING_FROM_SOURCE`) persisted institutions and sums to `normalizedRowCount`.  `preservedObservationDateCounts` counts retained `MISSING_FROM_SOURCE` institutions and sums to `preservedRowCount`.  Their normalized-plus-preserved union must equal the persisted output rows and `rowCount`.  This separation keeps the existing explicit exclusion of raw `공동실습소` rows auditable without redefining `fetchedRowCount`.

`sourceAsOf` is the one raw-observation date only if `sourceObservationDateCounts` has exactly one key; otherwise it is JSON `null`, even when filtering or preservation leaves only one normalized/preserved date.  Snapshot-wide `snapshotAsOf` remains the maximum date across every raw source-observation key and enrichment `sourceAsOf` value.

### Task 1: Preserve raw and normalized NEIS observation dates

**Files:**
- Modify: `apps/travel-map/app/institutions/sources/common.py:1-90, 200-235`
- Modify: `apps/travel-map/app/institutions/sources/neis.py:72-218`
- Modify: `apps/travel-map/app/institutions/sources/kindergarten.py:110-160`
- Modify: `apps/travel-map/app/institutions/sources/sen.py:90-115`
- Test: `apps/travel-map/tests/institutions/test_sync.py:150-235, 1240-1335, 3480-3585`

**Interfaces:**
- Consumes: raw NEIS `schoolInfo` pages and existing `SourceInstitutionRecord.source_as_of`.
- Produces: `SourceProvenance.source_observation_date_counts` and `.normalized_observation_date_counts`, with each selectable NEIS record retaining its own date.

- [ ] **Step 1: Add the source-level failing parser tests**

Replace the two rejection tests for mixed valid dates with tests that use one page and two pages.  Keep invalid-date and repeated-page tests unchanged.

```python
def test_neis_preserves_mixed_load_dates_within_one_page() -> None:
    payload = load_json("neis-school-info.json")
    rows = payload["schoolInfo"][1]["row"]
    rows[0]["LOAD_DTM"] = "20260423"
    rows[1]["LOAD_DTM"] = "20260607"

    assert [row.source_as_of for row in parse_neis_rows(payload)] == [
        "2026-04-23",
        "2026-06-07",
    ]


@pytest.mark.asyncio
async def test_neis_fetch_records_raw_and_normalized_mixed_vintage_histograms() -> None:
    # Mock page 1 as 20260423 + 20260517 and page 2 as 20260423 + 20260607.
    result = await NeisSource(api_key="test-key", client=client, page_size=2).fetch()

    assert result.provenance.source_as_of is None
    assert result.provenance.source_observation_date_counts == (
        ("2026-04-23", 2),
        ("2026-05-17", 1),
        ("2026-06-07", 1),
    )
    assert result.provenance.normalized_observation_date_counts == (
        ("2026-04-23", 2),
        ("2026-05-17", 1),
        ("2026-06-07", 1),
    )
```

- [ ] **Step 2: Run the parser tests to verify the current collapse fails**

Run:

```sh
PYTHONWARNINGS=error uv run --project apps/travel-map python -m pytest \
  apps/travel-map/tests/institutions/test_sync.py \
  -k 'neis and (mixed_load_dates or mixed_vintage_histograms)' -q
```

Expected: FAIL because `parse_neis_rows()` rejects mixed dates and the fetched provenance has no histogram fields.

- [ ] **Step 3: Implement canonical observation-count helpers**

In `common.py`, add a local ISO-date parser using `date.fromisoformat`, build counts with `Counter`, and return sorted tuple pairs.  Validate the exact ordering and totals before returning a provenance object.

```python
ObservationDateCounts = tuple[tuple[str, int], ...]

def observation_date_counts(dates: Iterable[str]) -> ObservationDateCounts:
    values = Counter(dates)
    result = tuple(sorted(values.items()))
    validate_observation_date_counts(
        result,
        expected_total=sum(values.values()),
        label="source observation dates",
    )
    return result


def source_as_of_for(counts: ObservationDateCounts) -> str | None:
    return counts[0][0] if len(counts) == 1 else None
```

Do not accept a mapping in this source-layer helper.  Reject blank, non-ISO, non-positive, duplicate, or non-lexicographically sorted tuple entries with `SourceDataError`.

- [ ] **Step 4: Preserve each NEIS row date and build both histograms**

Make `parse_neis_rows()` return the `_parse_row()` record directly rather than replacing each `source_as_of` with `max(...)`.  In `_fetch_impl()` collect all raw dates before filtering, remove the one-date rejection, and construct provenance like this:

```python
raw_counts = observation_date_counts(raw_dates)
normalized_counts = observation_date_counts(
    record.source_as_of for record in records
)

provenance=SourceProvenance(
    # existing immutable endpoint, hash, count, and request fields
    source_as_of=source_as_of_for(normalized_counts),
    source_observation_date_counts=raw_counts,
    normalized_observation_date_counts=normalized_counts,
)
```

Keep `_raw_neis_load_dates()` validation over every raw row, including `공동실습소`; an invalid date still fails before filtering.  Require `sum(raw_counts) == raw_row_count` and `sum(normalized_counts) == len(records)`.

- [ ] **Step 5: Populate one-date histograms for the other three source paths**

For kindergarten and reviewed SEN provenance, set raw counts to the one trusted source date repeated `fetched_row_count` times and normalized counts to that date repeated `row_count` times.  Update all `SourceProvenance(...)` constructors and the `source_provenance_for()` test helper.  Do not change their timing, pinned-file, or raw-hash checks.

- [ ] **Step 6: Add exclusion and invalid-value regressions**

Add a source fetch test with one selectable `20260423` row and one excluded `공동실습소` `20260607` row.  Assert raw histogram is `{2026-04-23: 1, 2026-06-07: 1}`, normalized histogram is `{2026-04-23: 1}`, `fetched_row_count == 2`, and `row_count == 1`.  Keep a second test that makes the excluded row date invalid and asserts `SourceDataError`.

- [ ] **Step 7: Run the source and provenance slice**

Run:

```sh
PYTHONWARNINGS=error uv run --project apps/travel-map python -m pytest \
  apps/travel-map/tests/institutions/test_sync.py \
  -k 'neis or source_provenance_for or untrusted_source_provenance' -q
```

Expected: PASS, including existing one-vintage kindergarten/SEN paths.

- [ ] **Step 8: Commit the source contract**

```sh
git add apps/travel-map/app/institutions/sources/common.py \
  apps/travel-map/app/institutions/sources/neis.py \
  apps/travel-map/app/institutions/sources/kindergarten.py \
  apps/travel-map/app/institutions/sources/sen.py \
  apps/travel-map/tests/institutions/test_sync.py
git commit -m "feat: preserve NEIS observation dates"
```

### Task 2: Make mixed-vintage source metadata a strict snapshot schema

**Files:**
- Modify: `apps/travel-map/app/institutions/models.py:239-315, 460-520`
- Modify: `apps/travel-map/app/institutions/snapshot.py:39-60, 270-300, 584-625`
- Modify: `apps/travel-map/tests/institutions/test_snapshot.py:150-310, 680-765`
- Modify: `apps/travel-map/tests/fixtures/institutions/snapshot/fixture-001/manifest.json`

**Interfaces:**
- Consumes: Task 1's canonical histogram rules.
- Produces: `SourceSnapshotInfo` with `source_as_of: str | None`, `source_observation_date_counts`, `normalized_observation_date_counts`, and `preserved_observation_date_counts`; `verify_snapshot()` rejects noncanonical persisted manifests.

- [ ] **Step 1: Write strict-manifest failures before changing models**

Construct valid manifest variants in `test_snapshot.py` and assert each fails through `verify_snapshot()`:

```python
@pytest.mark.parametrize(
    ("counts", "match"),
    [
        ({"2026-06-07": 1, "2026-04-23": 1413}, "sorted"),
        ({"2026-04-23": 0}, "positive"),
        ({"2026-04-23": 1414}, "fetchedRowCount"),
    ],
)
def test_snapshot_rejects_invalid_source_observation_date_counts(...): ...


def test_snapshot_rejects_mixed_dates_with_non_null_source_as_of(...): ...


def test_snapshot_rejects_row_dates_that_do_not_match_normalized_histogram(...): ...
```

Use raw JSON text for the unsorted-key case; do not build that fixture through `dict` sorting or Pydantic model serialization.

- [ ] **Step 2: Run the strict-manifest failures**

Run:

```sh
PYTHONWARNINGS=error uv run --project apps/travel-map python -m pytest \
  apps/travel-map/tests/institutions/test_snapshot.py \
  -k 'observation_date_counts or mixed_dates' -q
```

Expected: FAIL because source entries do not yet require or verify the new fields.

- [ ] **Step 3: Add exact source fields and ordered-map model validation**

Extend `_SOURCE_FIELDS` exactly with:

```python
"sourceObservationDateCounts",
"normalizedObservationDateCounts",
"preservedObservationDateCounts",
```

Add corresponding `dict[str, int]` fields to `SourceSnapshotInfo`.  The field validator must:

1. iterate `list(values.items())` before any transformation;
2. require `list(values) == sorted(values)`;
3. parse every ISO key;
4. require every value to be an exact `int` greater than zero, except an empty preserved map when `preservedRowCount == 0`;
5. return the original insertion order, not a sorted replacement.

Permit `source_as_of: str | None`.  In the model-level validator, require a non-null value exactly when `sourceObservationDateCounts` has one date, require that value to equal that raw-observation date, and require `None` otherwise.  Also require raw, normalized, and preserved sums to equal `fetched_row_count`, `normalized_row_count`, and `preserved_row_count` respectively.

- [ ] **Step 4: Verify persisted JSONL distributions rather than one source date**

Replace `_verify_source_counts()`'s per-institution equality check with these exact checks for each source:

```python
current = [
    item for item in institutions
    if item.source == source.source
    and item.status is not InstitutionStatus.MISSING_FROM_SOURCE
]
preserved = [
    item for item in institutions
    if item.source == source.source
    and item.status is InstitutionStatus.MISSING_FROM_SOURCE
]
assert_histogram(source.normalized_observation_date_counts, current)
assert_histogram(source.preserved_observation_date_counts, preserved)
```

`assert_histogram` must derive a sorted `Counter(item.source_as_of for item in rows)` and compare the exact mapping, including the zero-row/empty-map case.  Keep `rowCount == normalizedRowCount + preservedRowCount` and the source total checks.

- [ ] **Step 5: Update the approved fixture to the canonical one-date form**

Add the three source fields to `fixture-001/manifest.json` in source-field order.  Use its existing `sourceAsOf` as the sole key, raw and normalized counts equal to its current fetched/normalized values, and an empty preserved map only when `preservedRowCount` is zero.  Update fixture hashes only if fixture contents require them; do not loosen the verifier.

- [ ] **Step 6: Run snapshot schema and fixture tests**

Run:

```sh
PYTHONWARNINGS=error uv run --project apps/travel-map python -m pytest \
  apps/travel-map/tests/institutions/test_snapshot.py -q
```

Expected: PASS, including duplicate JSON-key, canonical-field, source-date, and existing fixture verification tests.

- [ ] **Step 7: Commit strict snapshot metadata**

```sh
git add apps/travel-map/app/institutions/models.py \
  apps/travel-map/app/institutions/snapshot.py \
  apps/travel-map/tests/institutions/test_snapshot.py \
  apps/travel-map/tests/fixtures/institutions/snapshot/fixture-001/manifest.json
git commit -m "feat: verify source observation date histograms"
```

### Task 3: Bind source histograms to candidate manifests and rechecks

**Files:**
- Modify: `apps/travel-map/app/institutions/sync.py:250-320, 443-655, 1078-1148, 1420-1530, 2149-2240`
- Modify: `apps/travel-map/tests/institutions/test_sync.py:430-640, 1240-1335, 2960-3045, 3480-3585`

**Interfaces:**
- Consumes: Task 1 `SourceProvenance` and Task 2 manifest fields.
- Produces: candidate source entries whose three histograms, hashes, record distributions, transaction attestations, quality gates, and preflight audit all agree.

- [ ] **Step 1: Add candidate and audit regressions for the measured distribution**

Create a small `mixed_neis_records()` helper with `{2026-04-23: 2, 2026-05-17: 1, 2026-06-07: 1}` and a production-shaped test that uses generated 1,413/1/1 dates without names or addresses in assertions.

```python
def test_mixed_vintage_neis_candidate_keeps_row_dates_and_manifest_histogram(...):
    candidate = build_test_candidate(records=mixed_neis_records(), ...)
    manifest = read_candidate_manifest(candidate)
    neis = next(item for item in manifest["sources"] if item["source"] == "NEIS")
    assert neis["sourceAsOf"] is None
    assert neis["sourceObservationDateCounts"] == {
        "2026-04-23": 1413,
        "2026-05-17": 1,
        "2026-06-07": 1,
    }
    assert neis["normalizedObservationDateCounts"] == neis["sourceObservationDateCounts"]
    assert neis["preservedObservationDateCounts"] == {}


def test_school_reconciliation_allows_multi_vintage_neis_but_requires_neis_only(...):
    audit = reconcile_selectable_school_counts(records, benchmark=benchmark)
    assert audit["categories"]["ELEMENTARY_SCHOOL"]["actualSourceAsOf"] == [
        "2026-04-23", "2026-05-17", "2026-06-07"
    ]
    assert audit["categories"]["ELEMENTARY_SCHOOL"]["sourceValidationPassed"] is True
```

Add tamper tests for a changed date, changed raw/normalized/preserved count, changed count-key order written as raw JSON, and changed count total.  Each test captures `current.json` bytes first and asserts bytes are unchanged after `SnapshotQualityError`.

- [ ] **Step 2: Run the candidate and audit tests to verify old single-date gates fail**

Run:

```sh
PYTHONWARNINGS=error uv run --project apps/travel-map python -m pytest \
  apps/travel-map/tests/institutions/test_sync.py \
  -k 'mixed_vintage or observation_date_counts or school_reconciliation_allows' -q
```

Expected: FAIL with the current `each source must have one exact source_as_of` and reconciliation single-date validation.

- [ ] **Step 3: Replace source-wide date assumptions with histogram invariants**

In `build_candidate_snapshot()` delete the `source_dates` set / one-date rejection.  In `_validate_source_provenance()` validate, without mutation:

```python
raw_counts = provenance.source_observation_date_counts
normalized_counts = provenance.normalized_observation_date_counts
validate_observation_date_counts(
    raw_counts, expected_total=checked_fetched_row_count, label="raw observation"
)
validate_observation_date_counts(
    normalized_counts, expected_total=len(records), label="normalized observation"
)
if observation_date_counts(row.source_as_of for row in records) != normalized_counts:
    raise SnapshotQualityError("source provenance observation dates do not match normalized rows")
if provenance.source_as_of != source_as_of_for(normalized_counts):
    raise SnapshotQualityError("source provenance source_as_of is not canonical")
```

Retain endpoint, license, attribution, region, pagination, raw-hash, pinned-source-hash, timing, and NEIS sample/count checks exactly as they are.

- [ ] **Step 4: Write all three histograms into the candidate manifest**

In `_candidate_manifest()`, derive preserved counts from output institutions with `MISSING_FROM_SOURCE`, derive normalized counts from non-preserved output institutions, and require them to agree with the source provenance for current source rows.  Set `sourceAsOf` through `source_as_of_for(provenance.source_observation_date_counts)`; do not use `max(row.source_as_of ...)` or a filtered-record union.

The source entry construction must include:

```python
"sourceObservationDateCounts": observation_counts_as_dict(
    provenance.source_observation_date_counts
),
"normalizedObservationDateCounts": observation_counts_as_dict(
    normalized_counts,
),
"preservedObservationDateCounts": observation_counts_as_dict(
    preserved_counts,
),
```

When copying prior provenance for a source omitted from the current fetch, make `normalizedObservationDateCounts` empty, carry its prior dates into `preservedObservationDateCounts`, set `normalizedRowCount` to zero, and set `sourceAsOf` from the preserved keys.  Do not fabricate a new raw fetch count or hash.

- [ ] **Step 5: Recheck exactly the manifest's persisted distributions**

In `_recheck_source_provenance()`, parse each histogram as strict ordered JSON, recompute current and preserved `Counter(row.source_as_of)` values, and reject a difference before `_transaction_attests_manifest()` or any fsync/rename.  Require raw-count sum against `fetchedRowCount`, normalized/preserved sums against the declared counts, canonical `sourceAsOf`, and the existing normalized source SHA-256 check.  Pass the exact new source list through the existing `_manifest_section_sha256(manifest["sources"])` transaction attestation; do not create a separate unsigned histogram hash.

- [ ] **Step 6: Make school-count reconciliation record, not reject, multiple NEIS dates**

Keep the expected-source check.  Replace `len(actual_source_as_of) == 1` with a required nonempty, sorted `actualSourceAsOf` list and add an `actualSourceObservationDateCounts` ordered map derived from matching records.  This audit is administrator output only; do not include names, IDs, addresses, coordinates, raw rows, or credentials.

- [ ] **Step 7: Run candidate, tamper, recovery, and audit coverage**

Run:

```sh
PYTHONWARNINGS=error uv run --project apps/travel-map python -m pytest \
  apps/travel-map/tests/institutions/test_sync.py -q
```

Expected: PASS.  In particular, one-date NEIS, kindergarten, SEN, source-provenance replay, pointer-recovery, and all new mixed-vintage cases pass.

- [ ] **Step 8: Commit candidate provenance binding**

```sh
git add apps/travel-map/app/institutions/sync.py \
  apps/travel-map/tests/institutions/test_sync.py
git commit -m "feat: bind mixed NEIS vintages to snapshots"
```

### Task 4: Require a deterministic human review digest before promotion

**Files:**
- Modify: `apps/travel-map/app/institutions/sync.py:656-850, 1690-1995, 2149-2520`
- Modify: `apps/travel-map/scripts/sync-institutions.py:1-270`
- Modify: `apps/travel-map/tests/institutions/test_sync.py:430-640, 1740-3440`
- Test: `apps/travel-map/tests/test_release.py`

**Interfaces:**
- Consumes: Task 3 candidates with transaction-attested source histograms.
- Produces:

```python
def build_candidate_review_packet(
    *, snapshot_id: str, snapshot_root: Path, coverage: CoverageService
) -> dict[str, object]: ...

def approve_candidate_snapshot(
    *, snapshot_id: str, review_digest: str, reviewer_role: str,
    snapshot_root: Path, coverage: CoverageService,
) -> str: ...
```

The old public `promote_snapshot()` symbol is removed.  Its atomic mutation body remains private and is reachable only after `approve_candidate_snapshot()` has verified a rebuilt packet under the root lock.  The existing synchronizer stops after an unapproved candidate and prints only its safe candidate-required status; it must neither import nor call a compatibility promotion shim.

- [ ] **Step 1: Write review-packet and approval failures**

Add tests before editing promotion code:

```python
def test_review_packet_is_deterministic_and_only_contains_safe_aggregates(...):
    packet = build_candidate_review_packet(...)
    assert packet["status"] == "CANDIDATE_REVIEW_REQUIRED"
    assert packet["sources"]["NEIS"]["normalizedObservationDateCounts"] == {
        "2026-04-23": 1413, "2026-05-17": 1, "2026-06-07": 1,
    }
    serialized = json.dumps(packet, ensure_ascii=False, sort_keys=True)
    assert "officialName" not in serialized
    assert "roadAddress" not in serialized
    assert "latitude" not in serialized
    assert "test-secret" not in serialized


def test_approval_requires_exact_digest_role_and_unchanged_candidate(...):
    packet = build_candidate_review_packet(...)
    with pytest.raises(SnapshotQualityError, match="review digest"):
        approve_candidate_snapshot(
            snapshot_id="mixed-vintage", review_digest="A" * 64,
            reviewer_role="data-steward", snapshot_root=tmp_path,
            coverage=TEST_COVERAGE,
        )
    assert not (tmp_path / "current.json").exists()
```

Also add cases for an unsafe candidate path, symlinked candidate file, unsigned/tampered transaction, tampered source date histogram, a stale previous snapshot, a quality issue, a changed candidate after packet generation, and same-digest PUBLISHED retry.  Each failure asserts the original pointer bytes remain exact.

- [ ] **Step 2: Run the review/approval tests to confirm automatic promotion is still exposed**

Run:

```sh
PYTHONWARNINGS=error uv run --project apps/travel-map python -m pytest \
  apps/travel-map/tests/institutions/test_sync.py \
  -k 'review_packet or approval_requires or digest or automatic_promotion' -q
```

Expected: FAIL because there is no review packet API and `promote_snapshot()` can publish without a reviewer digest.

- [ ] **Step 3: Extract a read-only candidate loader from promotion**

Create private `_load_reviewable_candidate(...)` that performs all current no-write checks: safe root/path/symlink validation, transaction load/phase checks, strict manifest fields, candidate JSONL hashes/models/counts, source histogram/provenance replay, enrichment replay, coverage quality, and `_transaction_attests_manifest()`.

```python
def _load_reviewable_candidate(
    *, snapshot_id: str, root: Path, coverage: CoverageService,
    allow_final_recovery: bool = False,
) -> tuple[SnapshotBuildResult, dict[str, object], list[Institution], list[InstitutionSite], dict[str, object]]:
    ...
```

It must not open the promotion lock, call fsync, write a transaction, rename a directory, replace a manifest, or write `current.json`.

- [ ] **Step 4: Build the canonical privacy-safe packet and digest**

Derive a packet from the read-only loader with only these keys:

```python
{
    "status": "CANDIDATE_REVIEW_REQUIRED",
    "snapshotId": snapshot_id,
    "createdAt": manifest["createdAt"],
    "snapshotAsOf": manifest["snapshotAsOf"],
    "previousSnapshotId": manifest["diff"]["previousSnapshotId"],
    "sourceCounts": {source: {"fetched": ..., "normalized": ..., "preserved": ..., "output": ...}},
    "sourceObservationDateCounts": {source: manifest histogram},
    "normalizedObservationDateCounts": {source: manifest histogram},
    "preservedObservationDateCounts": {source: manifest histogram},
    "institutionTypeCounts": manifest["countsByType"],
    "foundationCounts": manifest["countsByFoundation"],
    "districtCounts": fixed_25_seoul_district_histogram,
    "statusCounts": manifest["countsByStatus"],
    "coordinateQualityCounts": manifest["coordinateQualityCounts"],
    "quarantinedInstitutionIds": sorted_ids,
    "quarantinedSiteIds": sorted_ids,
    "diff": manifest["diff"],
    "institutionsSha256": manifest["institutionsSha256"],
    "sitesSha256": manifest["sitesSha256"],
    "candidateManifestSha256": sha256(manifest_bytes),
    "sourceProvenanceSha256": _manifest_section_sha256(manifest["sources"]),
    "enrichmentProvenanceSha256": _manifest_section_sha256(manifest["enrichments"]),
}
```

Calculate `reviewDigest` from that object without the digest field using UTF-8 `json.dumps(..., ensure_ascii=False, sort_keys=True, separators=(",", ":"))` and lowercase SHA-256.  Append it only after hashing.  Never include rows, source payload bytes, names, addresses, coordinates, credentials, headers, or HMAC key material.

- [ ] **Step 5: Gate the private mutator with an in-lock recomputation**

Implement `approve_candidate_snapshot()` to validate exact lowercase `[0-9a-f]{64}` digest and literal role `data-steward`, acquire the existing `.promotion.lock`, call `_load_reviewable_candidate()` and `build_candidate_review_packet()` again under that lock, then use `hmac.compare_digest`.  Only on match call the private atomic promotion helper.  After mutation, run `verify_snapshot(snapshot_root)` and return the recomputed digest.

For a `PUBLISHED` transaction that already points to this candidate, accept only the signed, verified final snapshot and the same recomputed digest; do not rewrite a pointer.  Remove the public `promote_snapshot()` export and migrate existing successful, tamper, concurrency, and recovery tests through a helper that first builds a packet and then calls `approve_candidate_snapshot()`.

- [ ] **Step 6: Add release-boundary regression coverage**

In `test_release.py`, create an unapproved candidate via the test helper and assert `stage_release_context()` raises before staging files.  Then obtain a review packet, approve with its exact digest, and assert the resulting selected snapshot is stageable.  Do not change release scripts; they already validate `current.json` through `verify_snapshot()`.

- [ ] **Step 7: Remove automatic promotion from the existing synchronizer**

Remove `promote_snapshot` from `sync-institutions.py` imports and delete its promotion call and post-promotion manifest summary.  After a candidate with no quality issues is built, print only the compact, sorted record below and return.  Keep pre-promotion reconciliation audit output and all credential-clearing `finally` paths.

```python
print(json.dumps(
    {"status": "CANDIDATE_REVIEW_REQUIRED", "snapshotId": snapshot_id},
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
))
```

Add a test through the existing `_run_with_keys()` fixture path that asserts this status, no `current.json`, and no `promote_snapshot` import/call.  A candidate quality issue must still fail safely while leaving any prior pointer unchanged.

- [ ] **Step 8: Run all approval, synchronizer, recovery, and release regressions**

Run:

```sh
PYTHONWARNINGS=error uv run --project apps/travel-map python -m pytest \
  apps/travel-map/tests/institutions/test_sync.py \
  apps/travel-map/tests/test_release.py -q
```

Expected: PASS.  Candidate-only, tampering, locked recheck, state-machine recovery, idempotent retry, and release-blocking behavior all remain covered.

- [ ] **Step 9: Commit human approval enforcement**

```sh
git add apps/travel-map/app/institutions/sync.py \
  apps/travel-map/scripts/sync-institutions.py \
  apps/travel-map/tests/institutions/test_sync.py \
  apps/travel-map/tests/test_release.py
git commit -m "feat: require review digest for snapshot approval"
```

### Task 5: Expose the administrator-only candidate, review, and approval workflow

**Files:**
- Modify: `apps/travel-map/scripts/sync-institutions.py:1-270`
- Create: `apps/travel-map/scripts/review-institution-snapshot.py`
- Create: `apps/travel-map/scripts/approve-institution-snapshot.py`
- Modify: `apps/travel-map/README.md:55-85`
- Modify: `apps/travel-map/tests/institutions/test_sync.py:900-1030, 3410-3475`

**Interfaces:**
- Consumes: Task 4 candidate-review and digest-approval APIs.
- Produces: three credential-safe administrator commands; the normal public app and user-facing map do not display this workflow.

- [ ] **Step 1: Write CLI behavior tests**

Use `runpy`/subprocess-style tests that do not require network credentials:

```python
def test_sync_cli_prints_only_candidate_review_required_without_current_pointer(...):
    result = run_sync_with_fake_sources(...)
    assert json.loads(result.stdout) == {
        "status": "CANDIDATE_REVIEW_REQUIRED",
        "snapshotId": "20260813T010203Z",
    }
    assert not (snapshot_root / "current.json").exists()


def test_review_cli_uses_no_env_or_network_and_prints_one_packet(...):
    result = run_script("review-institution-snapshot.py", ...)
    assert json.loads(result.stdout)["reviewDigest"] == expected_digest
    assert result.stderr == ""


def test_approval_cli_rejects_unknown_or_credential_arguments(...):
    result = run_script("approve-institution-snapshot.py", "--env-file", "x")
    assert result.returncode == 2
```

Add a success case asserting approval stdout equals compact/sorted `{"reviewDigest": ..., "snapshotId": ..., "status": "SNAPSHOT_APPROVED"}` and that no key fixture value occurs in stdout, stderr, exception text, or app-frame locals.

- [ ] **Step 2: Run CLI tests to verify current sync auto-promotes**

Run:

```sh
PYTHONWARNINGS=error uv run --project apps/travel-map python -m pytest \
  apps/travel-map/tests/institutions/test_sync.py \
  -k 'sync_cli or review_cli or approval_cli' -q
```

Expected: FAIL because `sync-institutions.py` imports/calls automatic promotion and the two standalone scripts do not exist.

- [ ] **Step 3: Change the synchronizer to stop at an unapproved candidate**

Remove the `promote_snapshot()` import/call and post-promotion `verify_snapshot()`/verbose manifest summary.  After a clean candidate build, print only:

```python
print(json.dumps(
    {"status": "CANDIDATE_REVIEW_REQUIRED", "snapshotId": snapshot_id},
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
))
```

Keep pre-promotion reconciliation audit output, source/key cleanup `finally` blocks, and quality-issue failure.  A quality issue must leave the candidate for inspection but return the existing safe error code without changing `current.json`.

- [ ] **Step 4: Implement the review script**

Give `review-institution-snapshot.py` only `--snapshot-id`, `--snapshot-root`, and `--geodata-root` arguments.  Build coverage from `seoul.geojson` with `buffer_distance_m=12_000`, call `build_candidate_review_packet()`, and print one compact, sorted UTF-8 JSON object.  It must not import `app.environment`, read environment variables, initialize `httpx`, or accept credential arguments.

- [ ] **Step 5: Implement the approval script**

Give `approve-institution-snapshot.py` required `--snapshot-id`, `--review-digest`, `--reviewer-role` plus snapshot/geodata roots.  Construct the same coverage, call `approve_candidate_snapshot()`, and print the exact success record from Step 1.  Catch only the existing safe validation/IO exception boundary and return a nonzero status without chaining secrets.

- [ ] **Step 6: Document the operator sequence without changing user instructions**

Replace README's automatic-promotion wording with this administrator-only sequence:

```sh
# 1. Networked, credentialed: creates .<id>.candidate only.
uv run --project apps/travel-map python apps/travel-map/scripts/sync-institutions.py \
  --env-file /secure/path/travel-map-sync.env

# 2. Credential-free: inspect source counts, observation-date histograms,
#    quarantine IDs, coordinate quality, provenance hashes, and diff.
uv run --project apps/travel-map python \
  apps/travel-map/scripts/review-institution-snapshot.py \
  --snapshot-id '<candidate-id>'

# 3. After a data steward independently records the review, publish exactly
#    the inspected digest.  This is the only command that can update current.json.
uv run --project apps/travel-map python \
  apps/travel-map/scripts/approve-institution-snapshot.py \
  --snapshot-id '<candidate-id>' --review-digest '<64-lowercase-hex>' \
  --reviewer-role data-steward
```

State that the `1413/1/1` NEIS date distribution is expected until official source dates converge; it is provenance for review, not an automatic rejection or a license to collapse dates.  State that release remains blocked before step 3.  Do not put this material in the public map UI.

- [ ] **Step 7: Run focused CLI and README-adjacent regression tests**

Run:

```sh
PYTHONWARNINGS=error uv run --project apps/travel-map python -m pytest \
  apps/travel-map/tests/institutions/test_sync.py \
  apps/travel-map/tests/test_release.py -q
```

Expected: PASS.  Confirm no command writes a pointer before approval and an unapproved candidate remains a release blocker.

- [ ] **Step 8: Commit the operator workflow**

```sh
git add apps/travel-map/scripts/sync-institutions.py \
  apps/travel-map/scripts/review-institution-snapshot.py \
  apps/travel-map/scripts/approve-institution-snapshot.py \
  apps/travel-map/README.md \
  apps/travel-map/tests/institutions/test_sync.py
git commit -m "feat: add reviewed institution snapshot workflow"
```

### Task 6: Run full verification and exercise the live candidate path safely

**Files:**
- Modify only if a failing verification proves a missing regression: the file that owns that failure and its colocated test.
- Verify: `apps/travel-map/tests`, `apps/travel-map/scripts`, `apps/travel-map/README.md`

**Interfaces:**
- Consumes: all prior tasks.
- Produces: a verified local build whose release gate remains fail-closed until an administrator approves a real candidate.

- [ ] **Step 1: Run the complete offline verification suite**

Run:

```sh
PYTHONWARNINGS=error uv run --project apps/travel-map python -m pytest \
  apps/travel-map/tests -q
uv run --project apps/travel-map ruff check \
  apps/travel-map/app apps/travel-map/tests apps/travel-map/scripts
(cd apps/travel-map && uv run mypy app scripts)
git diff --check
```

Expected: every command exits `0`; tests include one-date legacy sources, mixed-vintage NEIS, strict snapshot JSON, candidate tampering, approval/recovery, release context, and CLI privacy.

- [ ] **Step 2: Run the live synchronizer only after the offline suite is green**

Run without echoing credentials:

```sh
uv run --project apps/travel-map python apps/travel-map/scripts/sync-institutions.py \
  --env-file /secure/path/travel-map-sync.env
```

Expected: one safe `CANDIDATE_REVIEW_REQUIRED` record with a snapshot ID, no `current.json` change, and no raw keys or provider payloads in output.  If an official upstream response, pinned resource, quality threshold, or API credential fails, preserve the existing pointer and stop; do not alter date counts or bypass a gate.

- [ ] **Step 3: Produce and inspect the real review packet before approval**

Run:

```sh
uv run --project apps/travel-map python \
  apps/travel-map/scripts/review-institution-snapshot.py \
  --snapshot-id '<candidate-id>' > /secure/path/review-packet.json
```

Verify in the packet that NEIS has the expected sorted histogram, raw/normalized/preserved sums agree with their declared row counts, provenance hashes are present, quarantine/quality/diff values are expected, and no names, addresses, coordinates, raw records, or secrets appear.  Do not approve if any check differs.

- [ ] **Step 4: Approve only after a designated data steward records review**

Run the Task 5 approval command with the packet's exact lowercase digest.  Then verify only through the normal verifier:

```sh
uv run --project apps/travel-map python -c \
  'from app.institutions.snapshot import verify_snapshot; print(verify_snapshot("apps/travel-map/resources/institution-snapshots").manifest.snapshot_id)'
```

Expected: the printed ID matches the reviewed candidate.  If approval fails, leave `current.json` unchanged and diagnose the candidate; never retry with a modified digest.

- [ ] **Step 5: Re-run release preflight without assuming Docker availability**

Run:

```sh
./apps/travel-map/scripts/release-gate.sh
```

Expected: candidate-only state fails `BLOCKED_INVALID_RELEASE_ARTIFACT`; approved state proceeds beyond snapshot validation and then either completes or reports the explicit environmental Docker status.  Do not create a synthetic snapshot to exercise this path.

- [ ] **Step 6: Commit only verification-driven corrections**

If and only if a regression test and its minimal fix were required in this task:

```sh
git add <exact-owned-source-and-test-files>
git commit -m "fix: verify mixed-vintage snapshot workflow"
```

If no correction was needed, create no empty commit.

## Plan Self-Review

### Spec coverage

- Per-record NEIS date preservation and 1,413/1/1 mixed input: Task 1.
- Sorted, positive, raw/normalized/preserved count contracts and no `max()` proxy: Tasks 1–3.
- Manifest and transaction binding plus strict persisted snapshot verification: Tasks 2–3.
- Candidate-only build, privacy-safe review packet, digest/role/lock recheck, idempotency, and no pointer write on failure: Task 4.
- Credential-free review/approval CLIs and administrator-only documentation: Task 5.
- Offline gates, live candidate creation, human review, approval, and release behavior: Task 6.

### Placeholder scan

The plan contains no deferred implementation markers.  Every code-changing task names files, interfaces, failing tests, commands, exact state checks, and its commit boundary.

### Type consistency

- `SourceProvenance` produces `ObservationDateCounts`; `SourceSnapshotInfo` persists their ordered mapping form; `verify_snapshot()` and `_recheck_source_provenance()` compare the same derived record counters.
- `build_candidate_review_packet()` produces the digest consumed by `approve_candidate_snapshot()` and both CLI scripts use those exact public APIs.
- Only the private promotion state machine may mutate a manifest or `current.json`; the synchronizer and review script have no write path.
