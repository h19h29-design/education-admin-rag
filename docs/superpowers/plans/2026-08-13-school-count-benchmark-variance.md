# School Count Benchmark Variance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 검토된 NEIS 1,415행·유치원 706행 모집단 프로필과 서울시교육청 잠정 학교 수의 정확한 차이를 후보 provenance와 사람 승인 digest에 결합하여, 현재 운영 원천으로 안전한 후보 생성을 허용한다.

**Architecture:** 원천 adapter는 원본 범주 histogram을 provenance로 생성하고, 새 hash-pinned 모집단 프로필 모듈이 전체 원천 분포·역할·기준일을 검증한다. 대조 함수는 최종 정규화 유형 전체가 아니라 프로필의 `BENCHMARK` 범주만 공식 통계와 비교하며, strict reconciliation 블록을 manifest·signed transaction·review packet에 결합한다. 기존 candidate-only 동기화와 별도 `data-steward` 승인 경계는 유지한다.

**Tech Stack:** Python 3.12, dataclasses, Pydantic v2, FastAPI application models, pytest, Ruff, mypy, SHA-256/HMAC 기반 snapshot transaction, Playwright 회귀 테스트.

## Global Constraints

- 구현 기준 명세는 `docs/superpowers/specs/2026-08-13-school-count-benchmark-variance-design.md`다.
- 검수 프로필 상태는 정확히 `TEMPORARY_PRELIMINARY_VARIANCE`, 검토일은 `2026-08-13`, 검토 역할은 `data-steward`다.
- NEIS 범위는 `B10`, raw 1,415행, normalized 1,414행이며 역할 합계는 `BENCHMARK=1373`, `SUPPLEMENTARY=23`, `QUARANTINED=18`, `NONSELECTABLE=1`이다.
- 유치원알리미는 공시차수 `20261`, 원천 기준일 `2026-04-01`, 706행이다.
- 공식 기대/프로필 실제/승인 차이는 유치원 `724/706/-18`, 초등 `609/610/+1`, 중등 `390/390/0`, 고등 `319/319/0`, 특수 `32/32/0`, 기타 `18/22/+4`다.
- 방송통신학교 6행과 외국인학교 17행은 서비스 레코드에 유지하되 공식 통계 분자에서 제외한다.
- 평생학교 18행은 기존 `UNCLASSIFIED_SCHOOL`/`REVIEW_REQUIRED` 정책과 exact cross-check하고 공개 검색·출발지·경로 계산에서 계속 제외한다.
- `공동실습소` 1행은 raw provenance에 포함하지만 snapshot 레코드에서는 제외한다.
- 1% tolerance로 차이를 완화하지 않는다. 승인된 signed delta와 정확히 같아야 한다.
- 원본 라벨·건수·기준일·역할·hash 변화는 후보 생성 전에 fail-closed 한다.
- 학교명·주소·좌표·연락처·원시 행·API key·인증 query·HMAC key는 manifest/review packet의 모집단 감사 블록에 넣지 않는다.
- 동기화 CLI는 후보만 생성한다. `current.json`은 별도 review digest와 `data-steward` 승인 전에는 변경하지 않는다.
- 운영 승인 snapshot이 실제로 생성되기 전까지 release gate의 `BLOCKED_INVALID_RELEASE_ARTIFACT`는 정상 상태다.
- 새 외부 런타임 의존성은 추가하지 않는다.

## File Structure

- Create `apps/travel-map/resources/institution-sources/school-count-population-profile.csv`: 검토된 원본 범주·건수·역할·승인 차이 metadata.
- Create `apps/travel-map/app/institutions/sources/school_count_profile.py`: 프로필 dataclass, exact resource loader, source/role/category aggregate helpers.
- Modify `apps/travel-map/app/institutions/sources/common.py`: source category/profile provenance 필드.
- Modify `apps/travel-map/app/institutions/sources/neis.py`: filtering 전 exact raw 학교종류 histogram 수집.
- Modify `apps/travel-map/app/institutions/sources/kindergarten.py`: `KINDERGARTEN_TOTAL` histogram 수집.
- Modify `apps/travel-map/app/institutions/sync.py`: 프로필 provenance 결합, 모집단 범위 대조, manifest/review/approval replay.
- Modify `apps/travel-map/app/institutions/models.py`: strict snapshot source 및 reconciliation Pydantic 모델.
- Modify `apps/travel-map/app/institutions/snapshot.py`: exact manifest schema와 profile/reconciliation 무결성 검증.
- Modify `apps/travel-map/scripts/sync-institutions.py`: 프로필을 네트워크 전에 로드하고 candidate build까지 전달.
- Modify `apps/travel-map/tests/institutions/test_sync.py`: loader/source/reconciliation/candidate/review/approval/CLI TDD.
- Modify `apps/travel-map/tests/institutions/test_snapshot.py`: strict schema와 persisted tamper 회귀.
- Modify `apps/travel-map/tests/fixtures/institutions/snapshot/fixture-001/manifest.json`: 새 mandatory schema의 최소 검증 fixture.
- Modify `apps/travel-map/README.md`: 관리자 전용 프로필·차이 검토 및 갱신 절차.

---

### Task 1: Hash-pinned 모집단 프로필 리소스와 로더

**Files:**
- Create: `apps/travel-map/resources/institution-sources/school-count-population-profile.csv`
- Create: `apps/travel-map/app/institutions/sources/school_count_profile.py`
- Modify: `apps/travel-map/tests/institutions/test_sync.py`

**Interfaces:**
- Consumes: `NeisUnclassifiedPolicy`와 `PINNED_POLICY_SHA256` from `app.institutions.sources.neis_classification`.
- Produces: `SchoolPopulationRow`, `SchoolCountPopulationProfile`, `load_school_count_population_profile(path: Path, *, unclassified_policy: NeisUnclassifiedPolicy) -> SchoolCountPopulationProfile`.
- Produces constant: `PINNED_POPULATION_PROFILE_SHA256 = "e904a254ab4f0fa264a0ec3894827e6bebbb2b94ab263bf635594c812dd7df06"`.

- [ ] **Step 1: Write failing loader and constructor-contract tests**

Add named tests that require the exact resource and reject every trust-boundary mutation:

```python
def test_school_count_population_profile_loads_exact_reviewed_contract() -> None:
    profile = load_school_count_population_profile(
        SOURCE_RESOURCES / "school-count-population-profile.csv",
        unclassified_policy=REVIEWED_NEIS_UNCLASSIFIED_POLICY,
    )
    assert profile.sha256 == PINNED_POPULATION_PROFILE_SHA256
    assert profile.status == "TEMPORARY_PRELIMINARY_VARIANCE"
    assert profile.approved_variances == (
        ("ELEMENTARY_SCHOOL", 1),
        ("HIGH_SCHOOL", 0),
        ("KINDERGARTEN", -18),
        ("MIDDLE_SCHOOL", 0),
        ("MISC_SCHOOL", 4),
        ("SPECIAL_SCHOOL", 0),
    )
    assert profile.source_totals() == {
        "KINDERGARTEN_INFO": 706,
        "NEIS": 1_415,
    }
    assert profile.role_counts("NEIS") == {
        "BENCHMARK": 1_373,
        "NONSELECTABLE": 1,
        "QUARANTINED": 18,
        "SUPPLEMENTARY": 23,
    }
```

Add parameterized mutations for a raw label, count, role, normalized type, benchmark type, timing, source date, official hash, policy hash, sorted order, duplicate row, extra metadata, extra column, malformed UTF-8, symlink, file size `16_385`, tuple/string subclasses, and direct dataclass construction with spoofed fields. Assert `SourceDataError` for resource failures and `ValueError` for caller-constructed contract drift.

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/institutions/test_sync.py \
  -q -k 'school_count_population_profile'
```

Expected: collection error because `school_count_profile` does not exist.

- [ ] **Step 3: Create the exact reviewed CSV**

The first line is the pinned canonical digest; digest calculation excludes only that first line and includes the final newline:

```csv
# normalized_sha256=e904a254ab4f0fa264a0ec3894827e6bebbb2b94ab263bf635594c812dd7df06
# schema_version=1
# profile_status=TEMPORARY_PRELIMINARY_VARIANCE
# reviewed_as_of=2026-08-13
# reviewer_role=data-steward
# neis_region_code=B10
# neis_fetched_row_count=1415
# neis_normalized_row_count=1414
# kindergarten_timing=20261
# kindergarten_source_as_of=2026-04-01
# kindergarten_fetched_row_count=706
# benchmark_source_url=https://enews.sen.go.kr/uploads/img_smart//2026-06-08/20260608075519432.png
# benchmark_source_as_of=2026-03-10
# benchmark_raw_sha256=6279b1bc08a593c96b119220ecbfc6cc4884d7e64125a1705db508afeee15e70
# unclassified_policy_sha256=2a9222d34083261c42ba51fd4430dd6b84b2210908a13e377a64cc69298c51a1
# approved_variance_ELEMENTARY_SCHOOL=1
# approved_variance_HIGH_SCHOOL=0
# approved_variance_KINDERGARTEN=-18
# approved_variance_MIDDLE_SCHOOL=0
# approved_variance_MISC_SCHOOL=4
# approved_variance_SPECIAL_SCHOOL=0
source,source_category,observed_count,normalized_type,reconciliation_role,benchmark_type
KINDERGARTEN_INFO,KINDERGARTEN_TOTAL,706,KINDERGARTEN,BENCHMARK,KINDERGARTEN
NEIS,각종학교(고),13,MISC_SCHOOL,BENCHMARK,MISC_SCHOOL
NEIS,각종학교(중),7,MISC_SCHOOL,BENCHMARK,MISC_SCHOOL
NEIS,각종학교(초),1,MISC_SCHOOL,BENCHMARK,MISC_SCHOOL
NEIS,고등기술학교,1,MISC_SCHOOL,BENCHMARK,MISC_SCHOOL
NEIS,고등학교,319,HIGH_SCHOOL,BENCHMARK,HIGH_SCHOOL
NEIS,공동실습소,1,,NONSELECTABLE,
NEIS,방송통신고등학교,5,HIGH_SCHOOL,SUPPLEMENTARY,
NEIS,방송통신중학교,1,MIDDLE_SCHOOL,SUPPLEMENTARY,
NEIS,외국인학교,17,MISC_SCHOOL,SUPPLEMENTARY,
NEIS,중학교,390,MIDDLE_SCHOOL,BENCHMARK,MIDDLE_SCHOOL
NEIS,초등학교,610,ELEMENTARY_SCHOOL,BENCHMARK,ELEMENTARY_SCHOOL
NEIS,특수학교,32,SPECIAL_SCHOOL,BENCHMARK,SPECIAL_SCHOOL
NEIS,평생학교(고)-2년6학기,7,UNCLASSIFIED_SCHOOL,QUARANTINED,
NEIS,평생학교(고)-3년6학기,4,UNCLASSIFIED_SCHOOL,QUARANTINED,
NEIS,평생학교(중)-2년6학기,5,UNCLASSIFIED_SCHOOL,QUARANTINED,
NEIS,평생학교(초)-3년6학기,2,UNCLASSIFIED_SCHOOL,QUARANTINED,
```

- [ ] **Step 4: Implement strict dataclasses and loader**

Use frozen dataclasses and make their `__post_init__` validate the pinned contract so callers cannot construct a weaker policy:

```python
@dataclass(frozen=True)
class SchoolPopulationRow:
    source: str
    source_category: str
    observed_count: int
    normalized_type: str | None
    reconciliation_role: str
    benchmark_type: str | None


@dataclass(frozen=True)
class SchoolCountPopulationProfile:
    sha256: str
    status: str
    reviewed_as_of: str
    reviewer_role: str
    neis_region_code: str
    neis_fetched_row_count: int
    neis_normalized_row_count: int
    kindergarten_timing: str
    kindergarten_source_as_of: str
    kindergarten_fetched_row_count: int
    benchmark_source_url: str
    benchmark_source_as_of: str
    benchmark_raw_sha256: str
    unclassified_policy_sha256: str
    approved_variances: tuple[tuple[str, int], ...]
    rows: tuple[SchoolPopulationRow, ...]

    def source_category_counts(self, source: str) -> dict[str, int]:
        return {
            row.source_category: row.observed_count
            for row in self.rows
            if row.source == source
        }

    def role_counts(self, source: str) -> dict[str, int]:
        counts: Counter[str] = Counter()
        for row in self.rows:
            if row.source == source:
                counts[row.reconciliation_role] += row.observed_count
        return dict(sorted(counts.items()))
```

Read the file with `os.open(..., O_NOFOLLOW)` and `os.fstat`, reject non-regular files and sizes over 16KiB before decoding, require NFC/exact strings/no surrounding whitespace, exact metadata set, exact six-column header, sorted unique `(source, source_category)` keys, and the exact pinned row tuples above. Recompute canonical SHA-256 from every non-digest line plus final newline and compare it to both metadata and `PINNED_POPULATION_PROFILE_SHA256`. Cross-check the four `QUARANTINED` rows against `unclassified_policy.counts` and its hash.

- [ ] **Step 5: Run focused tests and static checks**

```bash
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/institutions/test_sync.py \
  -q -k 'school_count_population_profile'
uv run --project apps/travel-map ruff check \
  apps/travel-map/app/institutions/sources/school_count_profile.py \
  apps/travel-map/tests/institutions/test_sync.py
MYPYPATH=apps/travel-map uv run --project apps/travel-map mypy \
  apps/travel-map/app/institutions/sources/school_count_profile.py
```

Expected: all selected tests pass; Ruff and mypy report no issues.

- [ ] **Step 6: Commit Task 1**

```bash
git add \
  apps/travel-map/resources/institution-sources/school-count-population-profile.csv \
  apps/travel-map/app/institutions/sources/school_count_profile.py \
  apps/travel-map/tests/institutions/test_sync.py
git commit -m "feat: pin school count population profile"
```

---

### Task 2: 원천 raw category provenance

**Files:**
- Modify: `apps/travel-map/app/institutions/sources/common.py`
- Modify: `apps/travel-map/app/institutions/sources/neis.py`
- Modify: `apps/travel-map/app/institutions/sources/kindergarten.py`
- Modify: `apps/travel-map/tests/institutions/test_sync.py`

**Interfaces:**
- Consumes: no profile mapping inside HTTP adapters; adapters only report exact observed categories.
- Produces on `SourceProvenance`: `source_category_counts: tuple[tuple[str, int], ...]`, `source_population_role_counts: tuple[tuple[str, int], ...] = ()`, `source_population_profile_sha256: str | None = None`.
- Preserves: `SourceInstitutionRecord.source_kind_label` for normalized NEIS rows until candidate persistence.

- [ ] **Step 1: Write RED tests for raw histogram collection**

Add tests with a multi-page payload that asserts filtering does not erase the raw count:

```python
assert result.provenance.source_category_counts == (
    ("고등학교", 1),
    ("공동실습소", 1),
    ("방송통신고등학교", 1),
)
assert len(result.records) == 2
assert result.provenance.fetched_row_count == 3
assert result.provenance.row_count == 2
```

Add a kindergarten source test asserting:

```python
assert result.provenance.source_category_counts == (
    ("KINDERGARTEN_TOTAL", len(result.records)),
)
```

Retain and extend the surrounding-whitespace test so `"고등학교 "` fails before histogram creation. Add exact sorted-key/type tests for `SourceProvenance` serialization helpers.

- [ ] **Step 2: Run source tests and verify RED**

```bash
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/institutions/test_sync.py \
  -q -k 'source_category_counts or raw_school_kind_histogram'
```

Expected: failures because `SourceProvenance` and adapters do not provide the fields.

- [ ] **Step 3: Extend `SourceProvenance`**

Add defaulted immutable fields after the existing unclassified fields:

```python
source_category_counts: tuple[tuple[str, int], ...] = ()
source_population_role_counts: tuple[tuple[str, int], ...] = ()
source_population_profile_sha256: str | None = None
```

Use the existing canonical count validators when serializing these values later; do not accept dict subclasses or normalize labels.

- [ ] **Step 4: Collect NEIS categories before filtering**

Inside `NeisSource._fetch_impl`, maintain a `Counter[str]` and update it from every raw page before `parse_neis_rows`:

```python
raw_school_kind_counts: Counter[str] = Counter()

raw_labels = tuple(_required_school_kind_label(row) for row in raw_rows)
raw_school_kind_counts.update(raw_labels)
```

Set the final provenance field to:

```python
source_category_counts=tuple(sorted(raw_school_kind_counts.items())),
```

This code must use `_required_school_kind_label`, which rejects non-string, non-NFC, blank, and surrounding-whitespace labels. Keep the existing 1,415-row-independent pagination tests valid: an adapter may report any exact test histogram, while Task 3 applies the production profile gate.

- [ ] **Step 5: Collect kindergarten total**

In `KindergartenSource._fetch_impl`, add:

```python
source_category_counts=(("KINDERGARTEN_TOTAL", len(records)),),
```

Keep `request_timing`, `source_as_of`, observation date counts, and row count as independent provenance fields so Task 3 can cross-check all four values.

- [ ] **Step 6: Run source regression and static checks**

```bash
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/institutions/test_sync.py -q \
  -k 'neis or kindergarten or source_category_counts'
uv run --project apps/travel-map ruff check apps/travel-map/app/institutions/sources apps/travel-map/tests/institutions/test_sync.py
MYPYPATH=apps/travel-map uv run --project apps/travel-map mypy apps/travel-map/app/institutions/sources
```

Expected: selected tests pass and static checks are clean.

- [ ] **Step 7: Commit Task 2**

```bash
git add \
  apps/travel-map/app/institutions/sources/common.py \
  apps/travel-map/app/institutions/sources/neis.py \
  apps/travel-map/app/institutions/sources/kindergarten.py \
  apps/travel-map/tests/institutions/test_sync.py
git commit -m "feat: capture source population histograms"
```

---

### Task 3: 모집단 결합과 exact signed variance 대조

**Files:**
- Modify: `apps/travel-map/app/institutions/sync.py`
- Modify: `apps/travel-map/tests/institutions/test_sync.py`

**Interfaces:**
- Consumes: `SchoolCountPopulationProfile`, `ReviewedSchoolCounts`, records, source provenance, `NeisUnclassifiedPolicy`.
- Produces: `bind_school_count_population_profile(provenance: Mapping[str, SourceProvenance], *, profile: SchoolCountPopulationProfile) -> dict[str, SourceProvenance]`.
- Modifies: `reconcile_selectable_school_counts(records, *, benchmark, population_profile, source_provenance, unclassified_policy) -> dict[str, object]`.
- Reconciliation exact shape: `profileStatus`, `profileSha256`, `benchmarkSha256`, `sources`, `categories`, `passed`.

- [ ] **Step 1: Replace tolerance tests with exact population tests**

Remove tests that authorize arbitrary values within 1%. Add a production-shaped helper whose raw provenance has the exact 1,415/706 histograms while its normalized records contain the 1,414 NEIS selectable rows and 706 kindergarten rows.

Assert the exact output:

```python
assert reconciliation["categories"] == {
    "ELEMENTARY_SCHOOL": {
        "expectedCount": 609,
        "actualCount": 610,
        "deltaCount": 1,
        "status": "REVIEWED_VARIANCE",
    },
    "HIGH_SCHOOL": {
        "expectedCount": 319,
        "actualCount": 319,
        "deltaCount": 0,
        "status": "MATCHED",
    },
    "KINDERGARTEN": {
        "expectedCount": 724,
        "actualCount": 706,
        "deltaCount": -18,
        "status": "REVIEWED_VARIANCE",
    },
    "MIDDLE_SCHOOL": {
        "expectedCount": 390,
        "actualCount": 390,
        "deltaCount": 0,
        "status": "MATCHED",
    },
    "MISC_SCHOOL": {
        "expectedCount": 18,
        "actualCount": 22,
        "deltaCount": 4,
        "status": "REVIEWED_VARIANCE",
    },
    "SPECIAL_SCHOOL": {
        "expectedCount": 32,
        "actualCount": 32,
        "deltaCount": 0,
        "status": "MATCHED",
    },
}
```

Assert `sources.NEIS.roleCounts` equals the four approved totals and `sources.KINDERGARTEN_INFO.roleCounts == {"BENCHMARK": 706}`.

- [ ] **Step 2: Add RED drift and scope-isolation tests**

Parameterize one-at-a-time mutations:

- one raw category count `+1` while keeping total fixed by another category `-1`;
- new label with a valid count;
- move `방송통신고등학교` from supplementary into benchmark;
- kindergarten timing `20262`, date `2026-10-01`, or total 705;
- approved delta sign change;
- normalized record label/type mismatch;
- missing or wrong profile hash after binding;
- policy/profile quarantine mismatch.

Assert `SnapshotQualityError` before geocoding/candidate creation. Add positive assertions that the six broadcast rows and 17 foreign rows remain in records but are absent from benchmark actual counts, and the 18 quarantine rows are not counted in any official category.

- [ ] **Step 3: Run reconciliation tests and verify RED**

```bash
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/institutions/test_sync.py -q \
  -k 'population_reconciliation or supplementary_population or signed_variance'
```

Expected: existing tolerance-based implementation returns 324 high/39 misc and fails the exact assertions.

- [ ] **Step 4: Implement provenance binding**

`bind_school_count_population_profile` must:

1. require `NEIS` and `KINDERGARTEN_INFO` entries, leave unrelated reviewed sources such as SEN unchanged, and reject population fields on those unrelated sources;
2. exact-compare each `source_category_counts` against `profile.source_category_counts(source)`;
3. check NEIS fetched/normalized counts and B10 region;
4. check kindergarten timing/date/fetched/normalized counts;
5. return frozen replacements containing profile SHA and sorted role counts.

Core replacement shape:

```python
bound[source] = replace(
    item,
    source_population_role_counts=tuple(
        profile.role_counts(source).items()
    ),
    source_population_profile_sha256=profile.sha256,
)
```

Raise `SnapshotQualityError("source population profile does not match fetched data")` for every mismatch; never echo a dynamic raw label in the error.

- [ ] **Step 5: Implement exact reconciliation**

Build benchmark actual counts only from profile rows with `reconciliation_role == "BENCHMARK"`, grouped by non-null `benchmark_type`. Cross-check each selectable NEIS label against `SourceInstitutionRecord.source_kind_label` and `institution_type`; cross-check quarantine labels against `unclassified_policy`; cross-check kindergarten record count/type/source.

Use signed delta:

```python
delta = actual_count - expected_count
approved_delta = dict(population_profile.approved_variances)[institution_type]
if delta != approved_delta:
    raise SnapshotQualityError(
        "school count variance does not match reviewed profile"
    )
status = "MATCHED" if delta == 0 else "REVIEWED_VARIANCE"
```

Return a sorted canonical mapping with `passed=True` only after every exact check. Do not emit `threshold`, `deltaRatio`, endpoint, school name, address, coordinate, raw row, or credential.

- [ ] **Step 6: Verify audit fail-first behavior**

Update `build_sync_preflight_audit` tests so a mismatch prints a safe `PRE_PROMOTION_RECONCILIATION` JSON block, flushes it, raises before Kakao construction, and leaves snapshot root absent. Assert the output only contains allowlisted category names from the reviewed profile.

- [ ] **Step 7: Run focused and broad sync tests**

```bash
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/institutions/test_sync.py -q
uv run --project apps/travel-map ruff check \
  apps/travel-map/app/institutions/sync.py \
  apps/travel-map/tests/institutions/test_sync.py
MYPYPATH=apps/travel-map uv run --project apps/travel-map mypy \
  apps/travel-map/app/institutions/sync.py
```

Expected: full `test_sync.py` passes; Ruff and mypy are clean.

- [ ] **Step 8: Commit Task 3**

```bash
git add apps/travel-map/app/institutions/sync.py apps/travel-map/tests/institutions/test_sync.py
git commit -m "feat: reconcile reviewed source populations"
```

---

### Task 4: Manifest, review digest, approval replay 결합

**Files:**
- Modify: `apps/travel-map/app/institutions/models.py`
- Modify: `apps/travel-map/app/institutions/snapshot.py`
- Modify: `apps/travel-map/app/institutions/sync.py`
- Modify: `apps/travel-map/tests/institutions/test_sync.py`
- Modify: `apps/travel-map/tests/institutions/test_snapshot.py`
- Modify: `apps/travel-map/tests/fixtures/institutions/snapshot/fixture-001/manifest.json`

**Interfaces:**
- Adds to each source manifest entry: `sourceCategoryCounts`, `sourcePopulationRoleCounts`, `sourcePopulationProfileSha256`.
- Adds mandatory top-level manifest field: `schoolCountReconciliation`.
- Adds review packet fields: `schoolCountReconciliation`, `schoolCountReconciliationSha256`.
- Modifies `build_candidate_snapshot(..., school_count_reconciliation: Mapping[str, object]) -> SnapshotBuildResult`.

- [ ] **Step 1: Write strict model/schema RED tests**

Define tests that reject missing/extra/snake-case fields, unsorted category/role/category-result keys, bool-as-int, wrong status, wrong signed delta, wrong profile/benchmark hash, `passed=False`, noncanonical source/profile relationships, and non-NEIS/KGI source population fields.

The accepted JSON shape is:

```json
{
  "profileStatus": "TEMPORARY_PRELIMINARY_VARIANCE",
  "profileSha256": "e904a254ab4f0fa264a0ec3894827e6bebbb2b94ab263bf635594c812dd7df06",
  "benchmarkSha256": "36158d45a3b8c7e8a083e6d78f63fee706618f69eb49d8624877aef07e3a9332",
  "sources": {
    "KINDERGARTEN_INFO": {
      "fetchedCount": 706,
      "normalizedCount": 706,
      "roleCounts": {"BENCHMARK": 706}
    },
    "NEIS": {
      "fetchedCount": 1415,
      "normalizedCount": 1414,
      "roleCounts": {
        "BENCHMARK": 1373,
        "NONSELECTABLE": 1,
        "QUARANTINED": 18,
        "SUPPLEMENTARY": 23
      }
    }
  },
  "categories": {
    "ELEMENTARY_SCHOOL": {"expectedCount": 609, "actualCount": 610, "deltaCount": 1, "status": "REVIEWED_VARIANCE"},
    "HIGH_SCHOOL": {"expectedCount": 319, "actualCount": 319, "deltaCount": 0, "status": "MATCHED"},
    "KINDERGARTEN": {"expectedCount": 724, "actualCount": 706, "deltaCount": -18, "status": "REVIEWED_VARIANCE"},
    "MIDDLE_SCHOOL": {"expectedCount": 390, "actualCount": 390, "deltaCount": 0, "status": "MATCHED"},
    "MISC_SCHOOL": {"expectedCount": 18, "actualCount": 22, "deltaCount": 4, "status": "REVIEWED_VARIANCE"},
    "SPECIAL_SCHOOL": {"expectedCount": 32, "actualCount": 32, "deltaCount": 0, "status": "MATCHED"}
  },
  "passed": true
}
```

- [ ] **Step 2: Run strict schema tests and verify RED**

```bash
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/institutions/test_snapshot.py \
  apps/travel-map/tests/institutions/test_sync.py -q \
  -k 'school_count_reconciliation or population_provenance'
```

Expected: failures because fields and models are absent.

- [ ] **Step 3: Add strict Pydantic models**

Add camelCase aliases using the existing strict snapshot base:

```python
class SchoolCountCategoryResult(_StrictSnapshotModel):
    expected_count: int = Field(ge=0)
    actual_count: int = Field(ge=0)
    delta_count: int
    status: Literal["MATCHED", "REVIEWED_VARIANCE"]

    @model_validator(mode="after")
    def delta_and_status_match(self) -> Self:
        if self.actual_count - self.expected_count != self.delta_count:
            raise ValueError("school count delta is inconsistent")
        expected_status = "MATCHED" if self.delta_count == 0 else "REVIEWED_VARIANCE"
        if self.status != expected_status:
            raise ValueError("school count status is inconsistent")
        return self


class SchoolCountSourceSummary(_StrictSnapshotModel):
    fetched_count: int = Field(ge=1)
    normalized_count: int = Field(ge=1)
    role_counts: dict[str, int]


class SchoolCountReconciliation(_StrictSnapshotModel):
    profile_status: Literal["TEMPORARY_PRELIMINARY_VARIANCE"]
    profile_sha256: str
    benchmark_sha256: str
    sources: dict[str, SchoolCountSourceSummary]
    categories: dict[str, SchoolCountCategoryResult]
    passed: Literal[True]
```

Validators must require exact sorted keys and exact source/category/role sets from the accepted JSON above, exact pinned hashes, exact expected/actual/delta values, and source role sums equal fetched counts while normalized counts exclude only `NONSELECTABLE`.

Extend `SourceSnapshotInfo` with strict sorted mappings and an optional profile SHA that is mandatory only for NEIS/KGI and null/empty for SEN or `TEST_NEIS`. Extend `SnapshotManifest` with the mandatory JSON field represented as `school_count_reconciliation: SchoolCountReconciliation | None`. The value may be null only when all sources are exactly `TEST_NEIS`, `approvedByRole == "TEST_FIXTURE_REVIEWER"`, and every population mapping is empty/null. Any manifest containing production `NEIS` or `KINDERGARTEN_INFO` requires the exact non-null object above. Add a release-preflight regression proving `TEST_NEIS`/null reconciliation cannot stage as a production artifact.

- [ ] **Step 4: Persist and replay source population provenance**

Update `_SOURCE_FIELDS`, candidate manifest serialization, preserved-source reconstruction, `_validate_source_provenance`, `_recheck_source_provenance`, and snapshot verifier. For NEIS/KGI, compare the declared histogram and role counts to the pinned profile constants, require the exact profile SHA, and verify count relationships. For SEN, require `{}`, `{}`, and `None`.

Because raw NEIS labels are deliberately not persisted in institution JSONL, approval replay must use both controls:

1. source-time exact histogram stored in manifest and authenticated by the signed build transaction's manifest hash;
2. persisted normalized institution counts/statuses cross-checked against profile normalized roles, including exactly 18 `UNCLASSIFIED_SCHOOL` rows.

Do not claim that raw labels are reconstructed from JSONL.

- [ ] **Step 5: Bind reconciliation to candidate and review packet**

Require `school_count_reconciliation` in `build_candidate_snapshot`, validate it against bound source provenance before writing files, and serialize it as `schoolCountReconciliation`.

In `_review_packet_from_loaded_candidate` add:

```python
reconciliation = cast(dict[str, object], manifest["schoolCountReconciliation"])
packet["schoolCountReconciliation"] = reconciliation
packet["schoolCountReconciliationSha256"] = _manifest_section_sha256(
    reconciliation
)
```

Keep both fields inside the packet before computing `reviewDigest`. Approval's final in-lock load must run source and reconciliation replay before the final digest comparison and before any fsync/rename/pointer write.

- [ ] **Step 6: Add tamper, privacy, recovery, and fixture tests**

Add one-at-a-time tampering of source histogram, role count, profile hash, category actual/delta/status, and reconciliation hash. Re-sign public-looking manifest fields where the attack test requires it; assert signed transaction or final replay rejects it and `current.json` bytes are unchanged.

Assert the review packet contains only fixed category labels/counts/hashes and does not contain a sentinel school name, address, coordinate, API key, raw row, or endpoint query. Re-run pointer-failure restart and PUBLISHED idempotency tests with the same reviewed digest.

Update `fixture-001/manifest.json` with `sourceCategoryCounts: {}`, `sourcePopulationRoleCounts: {}`, `sourcePopulationProfileSha256: null`, and mandatory `schoolCountReconciliation: null`. Recompute any fixture hashes expected by tests and keep `current.json` pointing only to `fixture-001`. This test-only exception must be enforced by manifest source/reviewer identity and rejected by production release preflight.

- [ ] **Step 7: Run snapshot, sync, store, API, and release regressions**

```bash
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/institutions \
  apps/travel-map/tests/api/test_institutions.py \
  apps/travel-map/tests/test_release.py -q
uv run --project apps/travel-map ruff check apps/travel-map
MYPYPATH=apps/travel-map uv run --project apps/travel-map mypy \
  apps/travel-map/app apps/travel-map/scripts
git diff --check
```

Expected: all tests pass with no warnings; static checks and diff check are clean.

- [ ] **Step 8: Commit Task 4**

```bash
git add \
  apps/travel-map/app/institutions/models.py \
  apps/travel-map/app/institutions/snapshot.py \
  apps/travel-map/app/institutions/sync.py \
  apps/travel-map/tests/institutions/test_sync.py \
  apps/travel-map/tests/institutions/test_snapshot.py \
  apps/travel-map/tests/fixtures/institutions/snapshot/fixture-001/manifest.json
git commit -m "feat: bind school count review to snapshots"
```

---

### Task 5: CLI wiring, 관리자 문서, 전체 검증과 candidate-only 운영 재실행

**Files:**
- Modify: `apps/travel-map/scripts/sync-institutions.py`
- Modify: `apps/travel-map/tests/institutions/test_sync.py`
- Modify: `apps/travel-map/README.md`
- Create ignored report: `.superpowers/sdd/2026-08-13-school-count-benchmark-variance/final-report.md`

**Interfaces:**
- Adds CLI option: `--school-count-population-profile`, default `apps/travel-map/resources/institution-sources/school-count-population-profile.csv`.
- Preserves exact candidate receipt shape; for snapshot ID `20260813T120000Z` the output is `{"snapshotId":"20260813T120000Z","status":"CANDIDATE_REVIEW_REQUIRED"}` with compact sorted JSON.
- Preserves credential cleanup and no automatic approval.

- [ ] **Step 1: Write CLI RED tests**

Add tests that assert the profile loads before `httpx.AsyncClient`, binds both source provenance entries, performs exact reconciliation before constructing Kakao, passes reconciliation into `build_candidate_snapshot`, and never imports/calls an automatic promotion symbol.

```python
assert args.school_count_population_profile == (
    SOURCE_RESOURCES / "school-count-population-profile.csv"
)
assert observed_order[:3] == [
    "load-unclassified-policy",
    "load-population-profile",
    "open-http-client",
]
assert candidate_kwargs["school_count_reconciliation"] == reconciliation
assert not snapshot_root.joinpath("current.json").exists()
```

Add a mismatch test proving the safe preflight JSON is flushed, Kakao is never constructed, candidate root is absent, and credential holders/keys are scrubbed.

- [ ] **Step 2: Run CLI tests and verify RED**

```bash
PYTHONWARNINGS=error uv run --project apps/travel-map pytest \
  apps/travel-map/tests/institutions/test_sync.py -q \
  -k 'population_profile and cli'
```

Expected: parser/default/order/candidate argument assertions fail.

- [ ] **Step 3: Wire the candidate-only CLI**

Load, in this order, before opening the HTTP client:

```python
policy = load_neis_unclassified_policy(args.neis_unclassified_policy)
population_profile = load_school_count_population_profile(
    args.school_count_population_profile,
    unclassified_policy=policy,
)
benchmark = load_reviewed_school_counts(args.school_counts)
```

After gathering NEIS/KGI/SEN results, call `bind_school_count_population_profile`, then exact reconciliation, then `emit_sync_preflight_audit`. Pass the bound provenance and reconciliation to candidate creation. Do not add a review or approval call to this script.

- [ ] **Step 4: Update administrator-only documentation**

In README's administrator operation section, document:

- why the temporary policy exists;
- the exact 1,415/1,414 and 706 counts;
- the six official expected/actual/signed differences;
- broadcast/foreign supplementary inclusion, lifelong quarantine, workshop exclusion;
- sync → inspect `PRE_PROMOTION_RECONCILIATION` → review packet → digest approval;
- profile update is forbidden without new official evidence, design review, tests, and `data-steward` approval;
- general user instructions must not mention internal labels, quarantined IDs, hashes, or credentials.

Use the existing generic secure env-file examples; do not commit a local absolute env path or secret.

- [ ] **Step 5: Run full local verification**

```bash
uv sync --project apps/travel-map --frozen --dev
PYTHONWARNINGS=error uv run --project apps/travel-map pytest apps/travel-map/tests -q
uv run --project apps/travel-map ruff check apps/travel-map
MYPYPATH=apps/travel-map uv run --project apps/travel-map mypy \
  apps/travel-map/app apps/travel-map/scripts
pnpm --dir apps/travel-map install --frozen-lockfile
pnpm --dir apps/travel-map test:e2e
git diff --check
```

Expected: Python tests, Ruff, mypy, and 16 Playwright tests pass. Record exact counts and durations in the ignored report.

- [ ] **Step 6: Verify release remains fail-closed before live candidate approval**

```bash
apps/travel-map/scripts/release-gate.sh
```

Expected before an approved live snapshot: exit 2 and `BLOCKED_INVALID_RELEASE_ARTIFACT`; Docker must not be reached.

- [ ] **Step 7: Run one authorized candidate-only live sync**

Use the operator's existing mode-0600 env file by passing it via `--env-file`; never print or inspect its values. Capture stdout/stderr only into a mode-0600 ignored report, then verify:

- reconciliation has the exact six expected/actual/delta values;
- NEIS raw/normalized and role totals are exact;
- kindergarten timing/date/count are exact;
- the command emits `CANDIDATE_REVIEW_REQUIRED` rather than approval;
- `current.json` remains absent or byte-for-byte unchanged;
- no secret, school name, address, coordinate, or raw row appears in the aggregate report.

If any raw count, date, category, coordinate quality, provenance, or source hash differs, stop and preserve the existing pointer. Do not edit the reviewed profile to make the run pass.

- [ ] **Step 8: Commit Task 5 without live artifacts**

```bash
git add \
  apps/travel-map/scripts/sync-institutions.py \
  apps/travel-map/tests/institutions/test_sync.py \
  apps/travel-map/README.md
git commit -m "docs: operate reviewed school count variance"
```

Do not add `.env`, raw responses, candidate directories, `current.json`, review packet output, transaction keys, or ignored reports to Git.

- [ ] **Step 9: Request final independent review before approval**

The final review must independently reproduce:

1. one raw category count swap with unchanged total;
2. one supplementary category incorrectly included in benchmark actual;
3. one signed delta mutation;
4. one manifest/review packet tamper with pointer unchanged;
5. one privacy sentinel absent from audit/review packet;
6. one candidate-only live run with no automatic pointer mutation.

Only a `READY` verdict permits presenting the live review packet to the user. Actual snapshot approval remains a separate explicit user decision after the aggregate packet is shown.
