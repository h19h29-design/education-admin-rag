# Task 4 Report - Seoul education institution synchronization

Date: 2026-08-10 KST

Fix-round base: 59e78a1b1ae9425b40b389894bfa19658ccad8d6

Fix-round-2 base: 8c416b0dc6a1a3c9428bd3990fa54f2b54464c07

Round-2 verification date: 2026-08-11 KST

Round-2 status: READY. The independent fixed-point reviewer found no reproducible
blocker across all five findings. No production pointer was created.

## Fix round 2 - durable acquisition attestations and reviewed multi-site data

This section supersedes the round-1 statements about in-memory attestations,
coordinate-attachment count reconciliation, and the single-site SEN resource.

- NEIS now rejects more than one raw `LOAD_DTM` within a page and across pages,
  including rows explicitly excluded from selectable output. One excluded-only page
  can no longer disappear from the raw-vintage contract.
- The builder creates a root-scoped HMAC transaction receipt outside the candidate.
  A mode-0600 random key signs the exact unapproved manifest, source/enrichment
  acquisitions, persisted hashes, issues, previous pointer and phase. Promotion
  trusts the signed receipt under the root lock, not caller-replaceable fields in
  `SnapshotBuildResult`.
- Durable phases are `BUILT`, `MOVED`, `APPROVAL_PREPARED`, `VERIFIED`,
  `POINTER_PREPARED`, and `PUBLISHED`. Exact approval bytes/timestamp/role are signed
  before replacing the manifest. Tests cover forged raw evidence, copied/tampered
  receipts, manually moved approved directories, changed approval timestamps,
  pointer failure/restart, and a crash after pointer fsync but before the final phase.
- School completeness uses a reviewed category resource rather than the coordinate
  attachment. All categories are pinned to the official 2026-03-10 preliminary
  table: kindergarten 724, elementary 609, middle 390, high school 319, special 32,
  and miscellaneous 18 (`각종학교 17 + 고등기술학교 1`). Every category exposes the
  table URL/date/raw hash/preliminary status and must independently stay within 1%;
  the reported 2,092 total is informational (`usedForGate=false`) and never masks a
  category loss.
- Reconciliation also enforces actual population sources: kindergarten rows must be
  `KINDERGARTEN_INFO`, while every school category must be `NEIS`, with one exact
  source date. Before any Kakao call or candidate creation the CLI prints and flushes
  privacy-safe JSON containing source/type/foundation/all-25-district/status counts,
  quarantine IDs and every category delta/evidence. A failing audit exits nonzero.
- The reviewed SEN resource is now 42 physical site rows grouped into 41 institutions.
  `sen:gangseo-library` has exactly one default `:main` site (`본관`) and the official
  `sen:gangseo-library:gayang` site (`가양관`). The official main and Gayang directions
  pages, their raw hashes, the directory hash, canonical CSV digest and canonical
  41-record digest are separately pinned. No telephone/fax/homepage field is modeled.
- A missing coordinate on the default SEN site no longer rejects its branches. Both
  unresolved main and Gayang sites remain inspectable `REVIEW_REQUIRED` evidence;
  after exact geocoding both survive promotion and are independently searchable route
  origins. Duplicate site codes, multiple defaults and conflicting institution rows
  fail closed.

Round-2 RED evidence included mixed dates hidden in excluded NEIS rows, recomputable
public raw-provenance attestations, a forged approved final, count failure before any
audit output, inability to represent Gayang, and a crash window after pointer fsync.

Round-2 GREEN verification so far:

- Focused sync: 153 passed with `PYTHONWARNINGS=error`.
- All institution tests: 301 passed with `PYTHONWARNINGS=error`.
- Full app tests: 398 passed with `PYTHONWARNINGS=error`.
- Targeted attack/restart replay: 15 passed.
- Ruff: all checks passed.
- mypy: success in 27 application/script source files.
- Git diff, privacy-field and literal-secret scans: clean.
- Live credential-free replay: pinned SHA-256, 12,011 nationwide rows and 1,313
  unique Seoul rows (606 elementary, 388 middle, 319 high) all reproduced.

## Fix round 1 - promotion and provenance hardening

The independent review findings were reproduced with failing tests before each
implementation change. This round adds the following release gates:

- Promotion reclassifies every persisted ACTIVE site coordinate and routing anchor
  with the real CoverageService, requires a Seoul address and an ACTIVE default main
  site, and never calculates the 98% rate from branch-site counts.
- Snapshot roots, candidate/final directories, manifest/JSONL files, temporary files,
  and current.json are constrained to exact non-symlink paths. Manifest and JSONL are
  decoded with duplicate-key and nonstandard-constant rejection before any rename or
  pointer write. Pointer, approval-manifest, and directory-rename failures all recover
  through tested retries.
- Every source has one exact sourceAsOf. Source endpoint, license, attribution,
  region/timing, fetched/normalized/preserved/output counts, raw digest and the
  source-normalized and persisted-output digests are mandatory and replayed at
  promotion. Source/enrichment sections and the canonical unapproved manifest have
  in-memory build attestations, so disk replacement of dynamic acquisition evidence
  cannot be self-approved by recomputing a manifest hash.
  Preserved-only source provenance is carried only from the verified previous
  snapshot; no official-looking fallback is synthesized.
- NEIS rejects the known credential-free five-row sample, totals above 5,000, more
  than 200 derived or actual pages, short non-final pages, oversized streamed
  individual/cumulative responses, page-size overflow, rows beyond
  list_total_count, repeated pages and mixed LOAD_DTM dates.
- The CLI compares the same-population NEIS elementary/middle/high count with the
  pinned official standard-location total and blocks a delta over 1%. Its JSON audit
  includes raw/normalized/preserved/output source counts, type, foundation, district,
  status, reconciliation result and sorted quarantined institution and site IDs.
  Step-8 paths and disclosure timing now have backward-compatible defaults.
- Official coordinate and Kakao enrichments are required exactly when their quality
  labels occur. Upstream-normalized and selected site-ID/address/coordinate mapping
  digests are separate and strictly replayed with endpoint, count, region and date.
  Zero-call Kakao evidence is omitted.
- The 25-district kindergarten resource and 41-row SEN resource now pin both the
  official upstream digest and a canonical normalized-body digest. A valid-looking
  substituted SHA, district code 11999, or changed SEN address fails closed.
- SourceInstitutionRecord supports reviewed branch sites. One institution can now
  produce main plus branch origins without duplicate institution rows; a branch with
  no coordinate is persisted as REVIEW_REQUIRED instead of disappearing. Branch
  status is independently checked and site-only changes increment changedCount.
- possibleMatches is an inspectable, deterministic list of sorted cross-source
  institution-ID pairs plus reason. Task 3 verifies its exact schema, count,
  references and uniqueness; possible identities remain distinct.
- NEIS, kindergarten and Kakao failures cross a sanitized outer exception boundary.
  Tests recursively walk application traceback-frame locals and verify that query
  keys, Authorization values, request URLs, causes and contexts retain no secret.
- Promotion runs the complete Task 3 directory verifier before publishing current,
  is idempotent when the same fully verified snapshot is already current, and fsyncs
  snapshot files/directories in rename order before the durable current pointer.
  An exclusive root lock serializes the full validation-and-publish transaction, so
  concurrent candidates cannot both pass the same previous-snapshot comparison.
- Missing official branches are preserved as MISSING_FROM_SOURCE even while their
  parent institution remains current. Preserved standard/Kakao sites retain explicit
  enrichment provenance and separate current/preserved/total matched counts.
- The credential-free standard-school attachment is streamed with a 5 MiB ceiling;
  kindergarten additionally enforces a 25 MiB cumulative response ceiling across
  all 25 districts and pages before retaining another payload.
- Kakao geocoding keeps an incremental raw SHA-256 rather than raw response bodies,
  rejects more than 5,000 paid calls or 25 MiB cumulatively, and clears its key before
  snapshot construction. NEIS/kindergarten clear keys after success as well as
  failure; the CLI and shared HTTP boundary scrub all credential holders and request
  dictionaries in finally blocks, including unexpected transport exceptions.

Representative fix-round RED observations:

1. A Busan ACTIVE site with recomputed sitesSha256 promoted successfully.
2. A mixed-date two-page NEIS response and mixed-source candidate were accepted.
3. Foreign-root candidates, symlinked payload files and duplicate approvedAt keys
   could reach promotion.
4. The credential-free five-row NEIS success shape, a 2,147,483,647 declared total,
   201 derived pages and a page-size overflow were not bounded at the first response.
5. Attacker source/enrichment endpoints, NOT-B10, zero counts and bogus normalized
   hashes were trusted when inserted into a candidate manifest.
6. API keys remained in __context__, HTTP request objects and traceback-frame locals.
7. District code 11999 and a modified SEN address still parsed with unchanged
   provenance metadata.
8. The manifest had only possibleMatchCount and SourceInstitutionRecord could not
   represent a second official physical site.
9. A real SEN provenance object was rejected because its reviewed attribution did
   not match the promotion constant; all 41 missing-coordinate rows also exposed the
   need to keep pre-geocode and persisted digests separate.
10. A bounded NEIS request still buffered the whole body, a one-row response could
    run past the actual 200-page ceiling, and a valid-looking replacement raw/page/
    fetched triple could promote.
11. A missing-coordinate branch was silently dropped; an in-Seoul site relocation
    with recomputed file/source hashes was not bound to its standard enrichment
    match; and a forged lastSeenSnapshot could publish before Task 3 rejected it.
12. A successful promotion called again failed as though previousSnapshotId were
    missing, while impossible source count relations remained valid Task 3 models.
13. Concurrent candidates could both validate the same previous snapshot, and one
    collided on the shared current-pointer temporary file after both were approved.
14. A preserved enriched site could lose its enrichment audit entry, while a missing
    branch of an otherwise-current institution disappeared without any status or
    quarantine trace.
15. The standard-school attachment buffered its entire response before checking its
    digest, and kindergarten retained every bounded page without a cumulative cap.
16. Twenty-six valid Kakao responses retained 27,267,230 raw bytes without a batch
    ceiling; successful adapters also kept credentials until a later build failure.
17. An unexpected MockTransport RuntimeError escaped the shared HTTP boundary with
    the request key still reachable from traceback-frame locals.

Fix-round GREEN commands and results:

- `PYTHONWARNINGS=error ... pytest .../test_sync.py -q`: 123 passed.
- `PYTHONWARNINGS=error ... pytest .../tests/institutions -q`: 271 passed.
- `PYTHONWARNINGS=error ... pytest .../tests -q`: 368 passed.
- `ruff check apps/travel-map/app apps/travel-map/scripts apps/travel-map/tests`:
  all checks passed.
- `mypy apps/travel-map/app apps/travel-map/scripts`: success in 26 source files.
- Atomic directory, approval-manifest and pointer failure/retry tests: all passed.
- Strict privacy and secret scans: clean; git diff check: clean.
- Live credential-free official replay: SHA-256
  `05fc53d5920aea0161cbb5f31aedb9c466450c7939fd60083b797095afe9eab1`,
  12,011 nationwide rows to 1,313 Seoul rows (606 elementary, 388 middle,
  319 high).

Production status remains fail-closed. `NEIS_API_KEY`, `KINDERGARTEN_API_KEY` and
`KAKAO_REST_API_KEY` were checked by presence only and are all absent. There is no
production `current.json`; no sample, synthetic or partial snapshot was promoted.

## Outcome

Implemented the complete offline/live synchronization path for:

- NEIS schoolInfo (B10): credential fail-closed behavior, pagination to
  list_total_count, exact type/foundation mapping, and explicit 공동실습소 exclusion.
- Kindergarten Info basicInfo2: credential fail-closed behavior, pinned disclosure
  timing, official 25-district code provenance, alias conflict and repeated-page
  detection.
- Reviewed Seoul Metropolitan Office of Education CSV: headquarters 1, district
  offices 11, direct agencies 8, lifelong-learning centers 4, libraries 17.
- Kakao Local exact-road-address batch geocoding. Zero or ambiguous results stay
  quarantined.
- Credential-free official national school-location enrichment. Its B-prefixed ID is
  distinct from the NEIS seven-digit code, so coordinates are added only on a unique
  exact name, type, foundation, and road-address composite (or a future exact-ID
  match); the NEIS namespace is never changed.
- Candidate construction, Seoul address/coordinate double validation, cross-source
  non-merging, missing-row preservation, 98% coordinate gate, 10% active-drop gate,
  provenance replay, hash verification, and recoverable atomic promotion.
- Strict manifest evidence now separates fetched, normalized, preserved, and output
  row counts and records request region/timing plus normalized hashes. Official
  standard-school and Kakao coordinate enrichment have independent URL, license,
  raw/normalized hash, request-count, and matched-count entries.

No production current.json or approved institution snapshot was created. This is
intentional: NEIS_API_KEY, KINDERGARTEN_API_KEY, and KAKAO_REST_API_KEY were all
absent when checked by presence only. A sample, synthetic, partial, or credential-free
subset was not promoted.

## Official-source evidence

### NEIS schoolInfo

- Endpoint: https://open.neis.go.kr/hub/schoolInfo
- Required parameters: KEY, Type=json, pIndex, pSize at most 1000,
  ATPT_OFCDC_SC_CODE=B10.
- Live B10 total on 2026-08-10: 1,415.
- Foundation totals: national 12, public 993, private 410.
- No key or an empty key returned HTTP 200 INFO-000 but forced the first five records,
  irrespective of requested page size. This cannot pass completeness gates.
- An invalid key returned ERROR-290. Adapter errors never include the supplied key or
  upstream message.
- Exact selectable mappings include elementary, middle, high, special, foreign,
  broadcast middle/high, miscellaneous schools at elementary/middle/high tier, and
  higher technical school. Joint training centers are explicitly non-selectable and
  still count toward raw pagination completion.

### Kindergarten Info

- Endpoint:
  https://e-childschoolinfo.moe.go.kr/api/notice/basicInfo2.do
- Region-code source:
  https://e-childschoolinfo.moe.go.kr/openApi/sidoSigunguCode.do
- The region-code download is an official HTML table served as an Excel attachment.
  The reviewed Seoul normalization has 25 unique codes and pins source SHA-256
  94bb20b042c7b4bde170b8264c7116076e07dc98f8d97132841bc8f6c91e8925.
- No key returned HTTP 400 HTML. An invalid key returned a denied JSON payload under
  an HTML content type. Production access requires a real key.
- Disclosure 20261 contained 706 Seoul rows: public standalone 49, public attached
  246, private corporation 121, private individual 290. One row lacked coordinates
  and is preserved for quarantine.
- Omitting timing returned 762 rows mixed from 20231 through 20261, so timing is always
  pinned and validated.
- Only documented kinderCode/rpstYn and observed kindercode/rpst_yn aliases are
  accepted. Conflicts fail closed.
- Leader, director, telephone, fax, and homepage fields are neither modeled nor
  retained.

### Seoul education-office institutions

- Directory: https://www.sen.go.kr/www/website.jsp
- Count corroboration:
  https://www.sen.go.kr/resources/www/data/policydata1_9_2.pdf and
  https://www.sen.go.kr/resources/www/data/minwonservice_3.pdf
- The reviewed resource pins the 2026-08-10 directory HTML SHA-256
  9f202202edc653b09b4debb5a0ff939cf9fcdc64dd58174b28f8d009bb1b7424.
- Main/Gayang directions evidence is pinned from
  https://gslib.sen.go.kr/gslib/html.do?menu_idx=52 (SHA-256
  312ca8f63086188dabcb272ed3a2bfdfdb0d2c360f010cdc1fb59e6ff90288e7)
  and https://gylib.sen.go.kr/gylib/html.do?menu_idx=43 (SHA-256
  b3036767d04ef37b77d72c617ca21b052b83682eeab6f19ecb15a8a0aa54dd49).
- Its canonical 42-row CSV digest is
  c2b7e84c476175586b9f3764f54ee008fc35cb7831b4a8a0186ded9b608aac50;
  its grouped 41-record source digest is
  8cd2aa66f3df95a25a2127eaa2791e876f2d21cd7bc47aa700d34be75293b3b3;
  this remains distinct from the post-geocode persisted-output digest.
- Count gates: headquarters 1, district offices 11, direct agencies 8,
  lifelong-learning centers 4, libraries 17; 41 institutions and 42 physical site
  rows total.
- Only official institution/site name, type, foundation, education office, road
  address and district are retained. Coordinates remain blank until exact Kakao
  geocoding. The Student Education Institute's Gyeonggi address is expected to remain
  outside Seoul quarantine. Gangseo Library retains one institution with default
  `sen:gangseo-library:main` and branch `sen:gangseo-library:gayang` route origins.

### Seoul school-count reconciliation

- Official article:
  https://enews.sen.go.kr/news/view.do?bbsSn=191455&step1=3&step2=1
- Official attached table:
  https://enews.sen.go.kr/uploads/img_smart//2026-06-08/20260608075519432.png
- The table is the 2026-03-10 class-formation result and is explicitly preliminary
  until the April 1 education statistics are finalized. Its pinned raw SHA-256 is
  6279b1bc08a593c96b119220ecbfc6cc4884d7e64125a1705db508afeee15e70.
- Reviewed per-category gates are kindergarten 724, elementary 609, middle 390,
  high 319, special 32, and miscellaneous 18 (`각종학교 17 + 고등기술학교 1`).
  The reported total 2,092 is retained as informational evidence and is never used
  to hide a failing category.

### Credential-free official school-location data

- Portal: https://www.data.go.kr/data/15021148/standard.do
- Pinned attachment:
  https://www.data.go.kr/cmm/cmm/fileDownload.do?atchFileId=FILE_000000003635904&fileDetailSn=1
- HTTP 200 without login/key; UTF-8 BOM CSV; source date 2026-03-20; 12,011
  nationwide rows; SHA-256
  05fc53d5920aea0161cbb5f31aedb9c466450c7939fd60083b797095afe9eab1.
- Live adapter replay produced 1,313 unique Seoul rows with complete coordinates:
  elementary 606, middle 388, high 319.
- The source omits kindergarten, special, and miscellaneous schools, so it is
  coordinate enrichment only, not a complete registry.
- Portal metadata conflicts (수시 on the main page versus 반기 on the provider file),
  and the attachment ID rotates on release. URL, date, hash, schema, count, unique IDs,
  and coordinate bounds are pinned.

### Other credential-free alternatives evaluated

- Seoul Open Data OA-20502 is an official KERIS/NEIS-derived snapshot updated on the
  page 2026-08-02. Its CP949 CSV has 3,969 raw rows but 1,415 unique B10 school IDs
  because course rows repeat. It includes special 32 and miscellaneous-tier 21 but no
  coordinates. OA-20560 and OA-20561 are exact ID subsets. The credential-free POST
  is an undocumented official UI endpoint, so it remains reconciliation evidence
  rather than silently replacing the NEIS contract.
- OA-20566 has 946 unique kindergarten IDs, but only 706 are in round 20261; 240 stale
  rows span 20182-20252 and there is no closure/status field. It cannot replace the
  authenticated current registry.
- The 2026-06-26 SEN facility-opening file (data.go.kr 3078512) has 983 selected
  schools, no stable ID/address/coordinates, and severe type undercoverage.
- The annual Seoul school file is dated reconciliation only. The April workbook uses
  a separate KEDI namespace, six broadcast rows lack IDs, and the newer October
  workbook removes IDs. Neither has coordinates.

## TDD evidence

Representative RED observations:

1. Initial parser import failed with ModuleNotFoundError for institutions.sources.
2. Atomic candidate test failed with NotImplementedError.
3. Fact-correction cycle had 11 failures for verified school types, explicit
   exclusion, kindergarten aliases/conflicts, and coordinate quarantine.
4. Live-adapter cycle lacked NeisSource, KindergartenSource, KakaoLocalClient,
   coverage, and provenance interfaces.
5. Candidate self-approval initially did not raise.
6. A NEIS raw-total test reproduced repeated-page failure when a 공동실습소 row was
   excluded from normalized output.
7. Real keyless CSV replay exposed that 데이터기준일자 is ISO 2026-03-20, not the
   initial synthetic 20260320. The real format was added to the test first, then fixed.
8. Independent review reproduced three promotion defects: optional Seoul coverage,
   stale ACTIVE sites inflating the coordinate rate, and approval before directory
   rename. Each received a failing regression test before the implementation fix.
9. A formatted HTTP exception-chain test exposed an API key in the chained httpx URL;
   sanitized source errors now suppress that secret-bearing cause.
10. Forged in-memory candidate issues reproduced bypasses of the 98% coordinate and
    10% active-drop gates. Promotion now recalculates both from persisted JSONL and
    the currently approved previous snapshot.
11. Provenance review exposed fetched-versus-normalized NEIS counts and coordinate
    enrichment evidence being conflated or omitted. The strict manifest schema was
    extended, then replayed through both candidate promotion and Task 3 verification.

Verification recorded before the live date-format regression:

- Focused sync: 33 passed with PYTHONWARNINGS=error.
- All institution tests: 173 passed with PYTHONWARNINGS=error.
- Full app tests: 272 passed with PYTHONWARNINGS=error.
- Ruff: All checks passed.
- mypy: Success, no issues in 24 source files.

Final round-2 verification immediately before commit:

- Focused sync: 153 passed with PYTHONWARNINGS=error.
- All institution tests: 301 passed with PYTHONWARNINGS=error.
- Full app tests: 398 passed with PYTHONWARNINGS=error.
- Ruff: All checks passed.
- mypy: Success, no issues in 27 source files.
- Atomic/path/provenance/secret attack/restart subset: 15 passed.
- Official keyless hash/count replay: 1,313 unique Seoul rows, elementary 606,
  middle 388, high 319, against the pinned 12,011-row attachment.

## Quality and safety gates

- Duplicate source IDs and unknown source/foundation/type/region values fail closed.
- NEIS raw rows must exactly equal list_total_count; excluded rows cannot break
  pagination. Kindergarten detects repeated pages and uses a bounded page contract.
- Address region and WGS84 point must both be Seoul; success must be at least 98%.
- A previous ACTIVE count drop over 10% blocks promotion; temporarily missing rows
  are preserved as MISSING_FROM_SOURCE.
- Cross-source possible matches are counted but never merged.
- Candidate must remain approved=false. Promotion rechecks hashes and candidate state.
  It also reparses records, validates source-specific namespaces/types/foundations,
  and independently recalculates 98% coordinate and 10% active-drop gates. Manifest,
  directory, and current pointer use durable writes and atomic os.replace. Simulated
  directory-rename and pointer failures are recoverable without an approved orphan.
- Kindergarten response region, district, page, and page-size echoes must match each
  request; disclosure timing must match both rows and the official region resource;
  pagination is bounded and repeated pages fail closed.
- Primary-source manifest entries distinguish fetched, normalized, preserved, and
  output rows. Coordinate enrichment entries preserve the pinned standard-school
  attachment URL/hash/count and Kakao response-batch hashes without credentials.
- Privacy field scan: clean.
- Secret fixture scan across application/resources/scripts: clean.
- Production pointer: absent, as required.
- Final independent read-only review: Ready Yes; no release blocker remained.

## Production status and exact blocker

Production promotion is blocked by:

- NEIS_API_KEY
- KINDERGARTEN_API_KEY
- KAKAO_REST_API_KEY

The implementation, official fixtures, reviewed resources, live probes, and gates are
complete. When real credentials are supplied, the sync script will still refuse
promotion if pagination, hashes, counts, timing, Seoul coverage, coordinate success,
or prior-snapshot diff gates fail.
