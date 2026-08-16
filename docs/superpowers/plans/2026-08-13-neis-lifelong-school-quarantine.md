# NEIS Lifelong School Quarantine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve the 18 reviewed NEIS lifelong-school rows as `UNCLASSIFIED_SCHOOL` audit records while keeping every institution and site `REVIEW_REQUIRED` and unavailable to public search, origin selection, or routing.

**Architecture:** A strict, hash-pinned CSV defines the only four reviewed raw NEIS labels and exact `2/5/7/4` counts. The NEIS adapter carries each raw label through in-memory normalization and records only its privacy-safe histogram and policy hash in provenance; snapshot construction persists the normalized quarantine type without per-institution raw labels. Candidate review and approval revalidate the policy hash, histogram, persisted count, and forced quarantine state before any pointer write.

**Tech Stack:** Python 3.12, Pydantic v2, FastAPI, pytest, httpx, immutable dataclasses, JSONL snapshot manifests, SHA-256/HMAC approval transactions.

## Global Constraints

- The only accepted raw labels and counts are `평생학교(초)-3년6학기: 2`, `평생학교(중)-2년6학기: 5`, `평생학교(고)-2년6학기: 7`, and `평생학교(고)-3년6학기: 4`.
- Every accepted row normalizes to `UNCLASSIFIED_SCHOOL`; it must never normalize to an existing official school type.
- Every such institution and all of its sites remain `REVIEW_REQUIRED`, even with valid Seoul coordinates.
- These rows are excluded from official school-count reconciliation and public search/origin/routing, but retained in administrator snapshots and audit packets.
- `unclassifiedSchoolKindCounts` contains only sorted raw category labels and counts; it must not contain institution names, addresses, coordinates, credentials, raw rows, or endpoint query strings.
- A new label, a changed count, a malformed/policy-hash mismatch, or an `ACTIVE` quarantine record fails closed without changing `current.json`.
- The policy is documented only in the README administrator section, not in end-user help or UI copy.
- Candidate generation remains separate from human review and digest-gated approval.

---

### Task 1: Add the reviewed classification policy and strict NEIS normalization

**Files:**
- Create: `apps/travel-map/resources/institution-sources/neis-unclassified-school-kinds.csv`
- Create: `apps/travel-map/app/institutions/sources/neis_classification.py`
- Modify: `apps/travel-map/app/institutions/sources/common.py`
- Modify: `apps/travel-map/app/institutions/sources/neis.py`
- Modify: `apps/travel-map/tests/institutions/test_sync.py`

**Interfaces:**
- Produces: `PINNED_POLICY_SHA256: Final[str]`, `NeisUnclassifiedPolicy`, `load_neis_unclassified_policy(path: Path) -> NeisUnclassifiedPolicy`, and `validate_unclassified_school_counts(records: tuple[SourceInstitutionRecord, ...], policy: NeisUnclassifiedPolicy) -> dict[str, int]`.
- Extends: `SourceInstitutionRecord.source_kind_label: str | None = None` and `SourceProvenance.unclassified_school_kind_counts: tuple[tuple[str, int], ...] = ()`, `unclassified_school_policy_sha256: str | None = None`. The source-only label is not persisted in `Institution` JSONL.
- Changes: `NeisSource(*, api_key: str, client: httpx.AsyncClient, unclassified_policy: NeisUnclassifiedPolicy, page_size: int = 1_000)` and `parse_neis_rows(payload: Mapping[str, object], *, unclassified_policy: NeisUnclassifiedPolicy | None = None) -> tuple[SourceInstitutionRecord, ...]`; `None` preserves the existing fail-closed behavior for unknown labels.

- [ ] **Step 1: Add exact resource-loader RED tests**

Add tests that load the production policy, assert its exact metadata and sorted rows, and reject wrong SHA, extra/missing columns, duplicate/unsorted labels, nonpositive or boolean counts, a different reason code, and a symlinked resource.

```python
def test_load_neis_unclassified_policy_accepts_exact_reviewed_resource() -> None:
    policy = load_neis_unclassified_policy(
        SOURCE_RESOURCES / "neis-unclassified-school-kinds.csv"
    )
    assert policy.counts == (
        ("평생학교(고)-2년6학기", 7),
        ("평생학교(고)-3년6학기", 4),
        ("평생학교(중)-2년6학기", 5),
        ("평생학교(초)-3년6학기", 2),
    )
    assert policy.sha256 == (
        "2a9222d34083261c42ba51fd4430dd6b84b2210908a13e377a64cc69298c51a1"
    )
```

- [ ] **Step 2: Run the resource-loader tests and verify RED**

Run:

```bash
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/institutions/test_sync.py \
  -k 'neis_unclassified_policy' -q
```

Expected: collection fails because `neis_classification` and its public interfaces do not exist.

- [ ] **Step 3: Create the exact reviewed CSV**

Create the following UTF-8 file, including the final newline:

```csv
# schemaVersion=1
# sourceUrl=https://open.neis.go.kr/hub/schoolInfo
# sourceRegionCode=B10
# reviewedAsOf=2026-08-13
# reviewerRole=data-steward
school_kind,expected_count,reason_code
평생학교(고)-2년6학기,7,OFFICIAL_CLASSIFICATION_PENDING
평생학교(고)-3년6학기,4,OFFICIAL_CLASSIFICATION_PENDING
평생학교(중)-2년6학기,5,OFFICIAL_CLASSIFICATION_PENDING
평생학교(초)-3년6학기,2,OFFICIAL_CLASSIFICATION_PENDING
```

Its byte SHA-256 must be `2a9222d34083261c42ba51fd4430dd6b84b2210908a13e377a64cc69298c51a1`.

- [ ] **Step 4: Implement the strict policy loader**

In `neis_classification.py`, define an immutable policy with tuple-backed counts and strict metadata. Read at most 16 KiB, reject symlinks/non-files, require exact byte SHA-256, exact ordered metadata/header, sorted unique labels, exact reason code, and total 18.

```python
@dataclass(frozen=True)
class NeisUnclassifiedPolicy:
    counts: tuple[tuple[str, int], ...]
    sha256: str
    reviewed_as_of: str
    reviewer_role: str

    @property
    def labels(self) -> frozenset[str]:
        return frozenset(label for label, _ in self.counts)


def load_neis_unclassified_policy(path: Path) -> NeisUnclassifiedPolicy:
    """Load the one hash-pinned B10 quarantine policy or fail closed."""
```

- [ ] **Step 5: Add NEIS parsing and count-validation RED tests**

Build a fixture with the four labels repeated `2/5/7/4`. Assert every row becomes `UNCLASSIFIED_SCHOOL`, retains its exact `source_kind_label`, and produces a sorted provenance histogram. Add counterexamples for one extra label, one missing row, one extra row, and a known label without a supplied policy.

```python
assert Counter(row.institution_type for row in result.records) == {
    "UNCLASSIFIED_SCHOOL": 18
}
assert result.provenance.unclassified_school_kind_counts == policy.counts
assert result.provenance.unclassified_school_policy_sha256 == policy.sha256
```

- [ ] **Step 6: Run the adapter tests and verify RED**

Run the same focused command. Expected: failures show unsupported NEIS values and missing source/provenance fields.

- [ ] **Step 7: Extend source records, hashing, and the NEIS adapter**

Add the optional source label and provenance fields with empty/null defaults for non-NEIS sources. Keep `normalized_records_sha256()` reconstructible from persisted institution/site fields; the raw label histogram is bound separately by the source provenance and transaction. In `_parse_row`, keep existing official mappings unchanged; only policy-listed unknown labels become `UNCLASSIFIED_SCHOOL`. After all pages are collected, compare the exact label histogram to the policy before returning `SourceFetchResult`.

```python
if raw_kind in _INSTITUTION_TYPES:
    institution_type = _INSTITUTION_TYPES[raw_kind]
elif raw_kind in unclassified_policy.labels:
    institution_type = "UNCLASSIFIED_SCHOOL"
else:
    raise SourceDataError("NEIS row contains an unsupported value")
```

- [ ] **Step 8: Run Task 1 tests and static checks**

```bash
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/institutions/test_sync.py \
  -k 'neis or unclassified_policy' -q
uv run --project apps/travel-map ruff check \
  apps/travel-map/app/institutions/sources \
  apps/travel-map/tests/institutions/test_sync.py
MYPYPATH=apps/travel-map uv run --project apps/travel-map mypy \
  apps/travel-map/app/institutions/sources/common.py \
  apps/travel-map/app/institutions/sources/neis.py \
  apps/travel-map/app/institutions/sources/neis_classification.py
```

Expected: all selected tests and static checks pass without warnings.

- [ ] **Step 9: Commit Task 1**

```bash
git add apps/travel-map/resources/institution-sources/neis-unclassified-school-kinds.csv \
  apps/travel-map/app/institutions/sources/common.py \
  apps/travel-map/app/institutions/sources/neis.py \
  apps/travel-map/app/institutions/sources/neis_classification.py \
  apps/travel-map/tests/institutions/test_sync.py
git commit -m "feat: quarantine reviewed NEIS lifelong schools"
```

---

### Task 2: Bind quarantine status and provenance into snapshots and approval

**Files:**
- Modify: `apps/travel-map/app/institutions/models.py`
- Modify: `apps/travel-map/app/institutions/snapshot.py`
- Modify: `apps/travel-map/app/institutions/sync.py`
- Modify: `apps/travel-map/tests/institutions/test_snapshot.py`
- Modify: `apps/travel-map/tests/institutions/test_sync.py`
- Modify: `apps/travel-map/tests/institutions/test_store.py`
- Modify: `apps/travel-map/tests/api/test_institutions.py`

**Interfaces:**
- Consumes: `NeisUnclassifiedPolicy`, `SourceInstitutionRecord.source_kind_label`, and the two new `SourceProvenance` fields from Task 1.
- Extends: `SourceSnapshotInfo.unclassified_school_kind_counts: dict[str, int]` and `unclassified_school_policy_sha256: str | None`.
- Produces: `reconcile_selectable_school_counts(records: tuple[SourceInstitutionRecord, ...], *, benchmark: ReviewedSchoolCounts, unclassified_policy: NeisUnclassifiedPolicy, tolerance: float = 0.01) -> dict[str, object]` with `unclassifiedSchoolKindCounts`, `unclassifiedSchoolPolicySha256`, and `unclassifiedPolicyPassed`.

- [ ] **Step 1: Add reconciliation and forced-status RED tests**

Add a production-shaped record set with official categories plus the exact 18 unclassified rows. Assert official category counts do not change, the separate policy gate passes, every unclassified institution and site is `REVIEW_REQUIRED`, and `status_source == "OFFICIAL_CLASSIFICATION_PENDING"`.

```python
assert reconciliation["unclassifiedSchoolKindCounts"] == dict(policy.counts)
assert reconciliation["unclassifiedPolicyPassed"] is True
assert all(
    institution.status is InstitutionStatus.REVIEW_REQUIRED
    for institution in institutions
    if institution.institution_type == "UNCLASSIFIED_SCHOOL"
)
```

- [ ] **Step 2: Run focused tests and verify RED**

```bash
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/institutions/test_sync.py \
  -k 'unclassified and (reconciliation or status)' -q
```

Expected: `UNCLASSIFIED_SCHOOL` is rejected by source validation or becomes `ACTIVE`, and reconciliation lacks the policy fields.

- [ ] **Step 3: Implement reconciliation and forced quarantine**

Add `UNCLASSIFIED_SCHOOL` only to NEIS's allowed types. Reconciliation continues to loop only over official benchmark categories, then independently compares raw source labels against the policy. `_build_current_records()` must override both institution and site status for this type before coordinate/coverage logic.

```python
is_unclassified = record.institution_type == "UNCLASSIFIED_SCHOOL"
site_status = (
    InstitutionStatus.REVIEW_REQUIRED
    if is_unclassified
    else _status_from_coordinate_and_coverage(source_site, coverage)
)
status_source = (
    "OFFICIAL_CLASSIFICATION_PENDING" if is_unclassified else record.source
)
```

Update `build_sync_preflight_audit()` so ready/quarantine counts use this classification rule, not coordinate presence alone.

- [ ] **Step 4: Add strict manifest/provenance RED tests**

Extend strict source-field tests to require sorted `unclassifiedSchoolKindCounts` and exact lowercase policy SHA for NEIS, while other sources require `{}` and `null`. Add tampering cases for missing/extra/unsorted keys, changed count, changed policy hash, total not matching persisted `UNCLASSIFIED_SCHOOL` rows, and a quarantined institution or site changed to `ACTIVE`.

```python
neis_source["unclassifiedSchoolKindCounts"]["평생학교(고)-2년6학기"] = 8
resign_candidate(candidate)
with pytest.raises(SnapshotQualityError, match="unclassified"):
    build_candidate_review_packet(
        snapshot_id="quarantine-tamper",
        snapshot_root=snapshot_root,
        coverage=TEST_COVERAGE,
    )
assert current_path.read_bytes() == original_pointer
```

- [ ] **Step 5: Run strict snapshot tests and verify RED**

```bash
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/institutions/test_snapshot.py \
  apps/travel-map/tests/institutions/test_sync.py \
  -k 'unclassified or source_fields or provenance' -q
```

Expected: strict-schema tests fail because the new fields and cross-record invariants are absent.

- [ ] **Step 6: Bind policy data into manifest, review, and approval**

Add the fields to `_SOURCE_FIELDS`, `SourceSnapshotInfo`, candidate source entries, source-provenance recheck, and the privacy-safe review packet. At candidate creation, recompute the histogram from `source_kind_label` and compare it with `SourceProvenance`. When carrying a previous source into a new candidate, copy the prior manifest's histogram and policy hash into the reconstructed provenance. For NEIS review/approval, require exact `PINNED_POLICY_SHA256`, the exact policy map, and a histogram total equal to all current `UNCLASSIFIED_SCHOOL` institutions. For other sources, require an empty map and null hash. During candidate review and approval, reject any unclassified institution/site that is not `REVIEW_REQUIRED` or has the wrong `statusSource`.

```python
packet["unclassifiedSchoolKindCounts"] = dict(
    cast(dict[str, int], neis_entry["unclassifiedSchoolKindCounts"])
)
packet["unclassifiedSchoolPolicySha256"] = neis_entry[
    "unclassifiedSchoolPolicySha256"
]
```

The packet's existing canonical SHA-256 calculation then binds both fields to the human review digest.

- [ ] **Step 7: Add public non-exposure RED tests**

Create a verified fixture containing one active normal school and one quarantined unclassified school with valid Seoul coordinates. Assert `InstitutionStore.search()` returns only the active school, `require_site()` rejects the quarantined site, and `/api/v1/institutions` cannot return it by name or filters.

- [ ] **Step 8: Run non-exposure tests and implement only necessary store/API changes**

Run:

```bash
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/institutions/test_store.py \
  apps/travel-map/tests/api/test_institutions.py \
  -k 'unclassified or review_required' -q
```

Expected: existing ACTIVE-only indexing should already pass. If a test fails, tighten only the shared store boundary; do not add UI-specific filtering.

- [ ] **Step 9: Run Task 2 verification**

```bash
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/institutions/test_snapshot.py \
  apps/travel-map/tests/institutions/test_sync.py \
  apps/travel-map/tests/institutions/test_store.py \
  apps/travel-map/tests/api/test_institutions.py -q
uv run --project apps/travel-map ruff check apps/travel-map
MYPYPATH=apps/travel-map uv run --project apps/travel-map mypy \
  apps/travel-map/app apps/travel-map/scripts
git diff --check
```

Expected: all institution/API tests, Ruff, mypy, and diff checks pass without warnings.

- [ ] **Step 10: Commit Task 2**

```bash
git add apps/travel-map/app/institutions/models.py \
  apps/travel-map/app/institutions/snapshot.py \
  apps/travel-map/app/institutions/sync.py \
  apps/travel-map/tests/institutions/test_snapshot.py \
  apps/travel-map/tests/institutions/test_sync.py \
  apps/travel-map/tests/institutions/test_store.py \
  apps/travel-map/tests/api/test_institutions.py
git commit -m "feat: bind unclassified schools to snapshot review"
```

---

### Task 3: Wire the administrator workflow and document the policy

**Files:**
- Modify: `apps/travel-map/scripts/sync-institutions.py`
- Modify: `apps/travel-map/README.md`
- Modify: `apps/travel-map/tests/institutions/test_sync.py`
- Modify: `apps/travel-map/tests/test_release.py`

**Interfaces:**
- Consumes: `load_neis_unclassified_policy()` and the updated `NeisSource`/reconciliation interfaces.
- Produces: CLI option `--neis-unclassified-policy PATH`, defaulting to `apps/travel-map/resources/institution-sources/neis-unclassified-school-kinds.csv`.
- Preserves: candidate-only synchronization, credential scrubbing, separate review CLI, separate digest-gated approval CLI, and release blocking before approval.

- [ ] **Step 1: Add CLI/audit/release RED tests**

Assert the CLI loads the default policy before network clients, passes it to `NeisSource` and reconciliation, prints `unclassifiedSchoolKindCounts` only in the pre-promotion admin audit, and still emits the compact candidate receipt without automatic approval. Assert a candidate-only snapshot remains release-blocked and an approved snapshot with valid quarantine provenance stages only after review.

```python
assert preflight["reconciliation"]["unclassifiedSchoolKindCounts"] == dict(
    policy.counts
)
assert receipt == {
    "snapshotId": snapshot_id,
    "status": "CANDIDATE_REVIEW_REQUIRED",
}
assert not (snapshot_root / "current.json").exists()
```

- [ ] **Step 2: Run CLI/release tests and verify RED**

```bash
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/institutions/test_sync.py \
  apps/travel-map/tests/test_release.py \
  -k 'unclassified or candidate_review_required' -q
```

Expected: CLI namespace lacks the policy path and production wiring does not pass the policy.

- [ ] **Step 3: Wire policy loading into the sync CLI**

Add the explicit path argument, load the policy before creating the network source, pass it to `NeisSource`, and pass it to reconciliation. Policy validation errors must use the existing safe `SourceDataError`/`SnapshotQualityError` boundary; never print file contents or source rows.

```python
policy = load_neis_unclassified_policy(args.neis_unclassified_policy)
neis_source = NeisSource(
    api_key=keys["NEIS_API_KEY"],
    client=http,
    unclassified_policy=policy,
)
```

- [ ] **Step 4: Update administrator-only documentation**

In the README administrator operation section, document:

- the exact four labels and current total 18;
- `UNCLASSIFIED_SCHOOL` / `REVIEW_REQUIRED` behavior;
- inspection of `unclassifiedSchoolKindCounts` before copying the review digest;
- fail-closed response to a new label or count drift;
- the need for a new reviewed policy change when official classification/statistics become available.

Do not add this internal policy to end-user usage, screenshots, or public UI text.

- [ ] **Step 5: Run Task 3 and full offline verification**

```bash
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests -q
uv run --project apps/travel-map ruff check apps/travel-map
MYPYPATH=apps/travel-map uv run --project apps/travel-map mypy \
  apps/travel-map/app apps/travel-map/scripts
pnpm --dir apps/travel-map install --frozen-lockfile
pnpm --dir apps/travel-map test:e2e
git diff --check
```

Expected: the full Python suite, Ruff, mypy, browser suite, and diff check pass; no production snapshot is created by offline tests.

- [ ] **Step 6: Commit Task 3**

```bash
git add apps/travel-map/scripts/sync-institutions.py \
  apps/travel-map/README.md \
  apps/travel-map/tests/institutions/test_sync.py \
  apps/travel-map/tests/test_release.py
git commit -m "docs: add lifelong school review workflow"
```

---

### Task 4: Re-run the authorized live sync and stop at human review

**Files:**
- Inspect only: `apps/travel-map/resources/institution-snapshots/`
- Write ignored evidence: `.superpowers/sdd/2026-08-13-neis-lifelong-school-quarantine/task-4-report.md`

**Interfaces:**
- Consumes: the user's existing mode-0600 environment file outside this worktree and the three previously authorized sync credentials.
- Produces: one candidate directory plus a privacy-safe review packet; it does not call the approval CLI.

- [ ] **Step 1: Confirm safe preconditions without printing secrets**

Check only credential presence, environment-file mode, clean tracked worktree, absence/presence of `current.json`, and the policy-resource hash. Do not display credential values or full request URLs.

- [ ] **Step 2: Run the candidate-only live sync**

From the isolated worktree, use the already authorized environment file path:

```bash
uv run --project apps/travel-map python \
  apps/travel-map/scripts/sync-institutions.py \
  --env-file /Users/mac-mini/Documents/ChatGPT/학습커뮤니티/apps/travel-map/.env
```

Expected: exit 0, one `PRE_PROMOTION_RECONCILIATION` record showing the exact four-label `2/5/7/4` histogram, then one compact `CANDIDATE_REVIEW_REQUIRED` receipt. `current.json` remains byte-identical or absent.

- [ ] **Step 3: Generate and validate the administrator review packet**

Run `review-institution-snapshot.py` with the emitted snapshot ID. Verify safe aggregate fields, exact policy hash/counts, 18 `UNCLASSIFIED_SCHOOL` institutions, their quarantined IDs, full source/date provenance, coordinate quality, and diff. Scan the serialized packet to confirm it contains no credential values, institution names, addresses, coordinates, or raw response fragments.

- [ ] **Step 4: Stop for explicit human approval**

Present the review packet path, snapshot ID, review digest, exact four-label histogram, quarantine total, overall source/type/status counts, coordinate-quality result, and diff to the user. Do not run `approve-institution-snapshot.py` until the user explicitly approves that exact digest.

- [ ] **Step 5: Record verification without an empty commit**

Write the ignored report with command exit codes and privacy-safe aggregates. If live verification requires no tracked fix, leave the worktree clean and do not create an empty commit. If a source-contract defect appears, stop, preserve the candidate/current pointer state, and start a new RED/GREEN fix task before any approval.
