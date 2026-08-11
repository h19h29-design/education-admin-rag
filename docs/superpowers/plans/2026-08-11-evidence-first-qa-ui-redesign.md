# Evidence-First QA UI Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the public QA preview with a two-theme, evidence-first experience that answers only from relevant cases, shows the source case Q&A separately, and never exposes internal transport, model, repository, or policy details.

**Architecture:** Introduce one strict Python public-answer contract between the local answer bridge and the private issue-note transport. The Worker validates that contract and returns only public-safe fields, while a small static frontend renders the AI answer, related cases, and evidence rail from structured data instead of parsing citations from generated prose. Relevance and answer validation are fail-closed: no relevant cases skips AI entirely, and invalid AI output falls back to the related-case list.

**Tech Stack:** Python 3.12, Pydantic-free strict dataclass/value validation, GitLab private notes as transport, Cloudflare Worker JavaScript, static semantic HTML/CSS/ES modules, Node built-in test runner, pytest.

## Global Constraints

- Exact no-evidence copy: `등록된 사례집에서 이 질문과 관련된 내용을 찾지 못했습니다. 다른 표현이나 핵심어로 다시 검색해 주세요.`
- Exact answer-fallback copy: `답변을 정리하지 못했습니다. 관련 사례는 아래 목록에서 직접 확인해 주세요.`
- Exact progress copy: `관련 사례를 찾고 답변을 정리하고 있습니다.`
- Exact review warning: `미검수 참고 답변입니다. 실제 업무 처리 전 최신 지침을 확인하세요.`
- Public UI and answer text must not contain GitLab, Webhook, Hermes, RAG, repository/project/bot/model details, `production_eligible`, `warning_code`, `complete_corpus`, or `review_status`.
- Public related-case objects contain exactly `case_id`, `edition_year`, `pdf_pages`, `title`, `question`, and `answer`; maximum five cases.
- When relevance filtering returns zero cases, `hermes_runner` must not be called.
- Themes are exactly `light` and `dark`; initial choice follows `prefers-color-scheme`, then persists under `senqa-theme-v1`.
- Existing challenge, confidentiality, request rate limit, origin validation, and polling-token security behavior must remain unchanged.
- Do not stage or commit `.superpowers/` visual-companion artifacts.

---

## File Structure

- Create `src/integrations/gitlab_qa_public.py`: public answer/case validation, relevance filtering, AI-output grounding validation, canonical private-note payload.
- Create `tests/test_gitlab_qa_public.py`: strict contract, relevance, grounding, and information-leak tests.
- Modify `src/integrations/gitlab_qa_bridge.py`: skip AI for no evidence, build safe prompt, validate AI answer, preserve case list on fallback.
- Modify `src/integrations/gitlab_qa_delivery.py`: emit a v2 canonical JSON answer note without visible policy banners.
- Modify `tests/test_gitlab_qa_bridge.py` and `tests/test_hermes_gitlab_qa_delivery.py`: orchestration and v2 delivery regressions.
- Modify `web/qa-worker/src/worker.js`: parse and validate v2 completion notes and return structured public-safe JSON.
- Modify `web/qa-worker/test/worker.test.mjs`: v2 success, malformed payload, leak, and legacy fallback tests.
- Create `web/qa-worker/public/view-model.js`: pure response/history/theme normalization helpers.
- Create `web/qa-worker/test/view-model.test.mjs`: DOM-independent theme, history, and response tests.
- Modify `web/qa-worker/public/index.html`: accepted three-region semantic layout and visible copy.
- Modify `web/qa-worker/public/styles.css`: accepted light/dark design tokens and responsive layout.
- Modify `web/qa-worker/public/app.js`: structured answer/case/evidence rendering, history v2, theme controls.
- Modify deployment/install tests only where their asserted public prompt/comment format changes.

---

### Task 1: Strict Public Answer Contract

**Files:**
- Create: `src/integrations/gitlab_qa_public.py`
- Create: `tests/test_gitlab_qa_public.py`

**Interfaces:**
- Consumes: validated search result dictionaries from `GitLabQaRequest.evidence["results"]`.
- Produces: `PublicCase`, `PublicAnswer`, `public_cases_from_evidence(question, evidence) -> tuple[PublicCase, ...]`, `validate_grounded_answer(content, cases) -> str | None`, `no_evidence_answer() -> PublicAnswer`, `cases_only_answer(cases) -> PublicAnswer`, and `canonical_public_answer_json(answer) -> str`.

- [ ] **Step 1: Write failing strict-contract and relevance tests**

```python
def test_no_relevant_case_returns_empty() -> None:
    evidence = evidence_with_case(title="급식 계약", question="수의계약 기준", answer="기준을 확인합니다")
    assert public_cases_from_evidence("학교폭력 조치", evidence) == ()


def test_two_meaningful_terms_make_case_relevant() -> None:
    evidence = evidence_with_case(title="학교 계약", question="수의계약이 가능한 경우", answer="계약 금액을 확인합니다")
    cases = public_cases_from_evidence("학교 수의계약 기준", evidence)
    assert [case.case_id for case in cases] == ["senqa-2022-case-a"]


def test_long_specific_term_in_title_is_relevant() -> None:
    evidence = evidence_with_case(title="기간제교원 채용", question="절차 안내", answer="공고 후 채용합니다")
    assert len(public_cases_from_evidence("기간제교원", evidence)) == 1


def test_public_case_drops_internal_fields() -> None:
    case = public_cases_from_evidence("학교 수의계약", relevant_evidence())[0]
    assert set(case.as_dict()) == {"case_id", "edition_year", "pdf_pages", "title", "question", "answer"}
```

- [ ] **Step 2: Run the tests and confirm the API is missing**

Run: `uv run pytest tests/test_gitlab_qa_public.py -q`

Expected: collection failure for `src.integrations.gitlab_qa_public`.

- [ ] **Step 3: Implement exact public models and relevance filtering**

```python
NO_EVIDENCE_TEXT = "등록된 사례집에서 이 질문과 관련된 내용을 찾지 못했습니다. 다른 표현이나 핵심어로 다시 검색해 주세요."
CASES_ONLY_TEXT = "답변을 정리하지 못했습니다. 관련 사례는 아래 목록에서 직접 확인해 주세요."
PUBLIC_SCHEMA = "senqa-public-answer/v1"
_STOPWORDS = frozenset({"어떻게", "무엇", "알려줘", "경우", "관련", "대한", "있는", "하는"})


@dataclass(frozen=True, slots=True)
class PublicCase:
    case_id: str
    edition_year: int
    pdf_pages: tuple[int, ...]
    title: str
    question: str
    answer: str

    def as_dict(self) -> dict[str, object]:
        return {
            "answer": self.answer,
            "case_id": self.case_id,
            "edition_year": self.edition_year,
            "pdf_pages": list(self.pdf_pages),
            "question": self.question,
            "title": self.title,
        }


@dataclass(frozen=True, slots=True)
class PublicAnswer:
    answer: str
    answer_kind: Literal["grounded", "no_evidence", "cases_only"]
    cases: tuple[PublicCase, ...]
    schema_version: Literal["senqa-public-answer/v1"] = PUBLIC_SCHEMA
```

Normalize Hangul/alphanumeric query tokens with `re.findall(r"[0-9A-Za-z가-힣]+", text.casefold())`. Keep tokens of length at least two that are not in `_STOPWORDS`. A result is relevant when two distinct query tokens occur in `title + question + answer`, or one token of length at least four occurs in title or question. Preserve original ranked order and cap at five.

- [ ] **Step 4: Write failing grounding and leak tests**

```python
@pytest.mark.parametrize("leak", ["GitLab", "Webhook", "Hermes", "RAG", "production_eligible", "warning_code", "complete_corpus", "review_status"])
def test_grounded_answer_rejects_internal_terms(leak: str) -> None:
    case = public_case()
    assert validate_grounded_answer(f"계약 기준입니다. [{case.case_id} · 2022년 · PDF 4쪽]\n{leak}", (case,)) is None


def test_grounded_answer_requires_allowed_case_year_and_page_per_paragraph() -> None:
    case = public_case()
    assert validate_grounded_answer("계약 기준입니다.", (case,)) is None
    assert validate_grounded_answer("계약 기준입니다. [senqa-2022-case-a · 2022년 · PDF 999쪽]", (case,)) is None
    valid = "계약 기준입니다. [senqa-2022-case-a · 2022년 · PDF 4쪽]"
    assert validate_grounded_answer(valid, (case,)) == valid
```

- [ ] **Step 5: Implement recursive validation and canonical JSON**

`validate_grounded_answer` must reject non-strings, blank/over-32,000 text, prohibited terms case-insensitively, unknown case IDs, wrong years/pages, and any non-empty paragraph without one exact allowed citation. `canonical_public_answer_json` must reconstruct every nested field from exact built-in types, serialize with `ensure_ascii=False, separators=(",", ":"), sort_keys=True`, and never serialize dataclass `__dict__` blindly.

- [ ] **Step 6: Run focused tests**

Run: `uv run pytest tests/test_gitlab_qa_public.py -q`

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/integrations/gitlab_qa_public.py tests/test_gitlab_qa_public.py
git commit -m "feat: add public QA answer contract"
```

### Task 2: Fail-Closed Answer Orchestration

**Files:**
- Modify: `src/integrations/gitlab_qa_bridge.py`
- Modify: `tests/test_gitlab_qa_bridge.py`

**Interfaces:**
- Consumes: Task 1 `PublicAnswer`, `public_cases_from_evidence`, `validate_grounded_answer`, `no_evidence_answer`, and `cases_only_answer`.
- Produces: `build_hermes_prompt(request, cases) -> str` and `run_answer_job(..., deliver: Callable[..., str]) -> str` delivering a `PublicAnswer`.

- [ ] **Step 1: Write failing no-evidence and fallback orchestration tests**

```python
def test_run_answer_job_skips_hermes_when_no_case_is_relevant() -> None:
    calls = {"hermes": 0}
    result = run_answer_job(
        unrelated_request(),
        hermes_path=Path("/approved/hermes"),
        delivery_token="token",
        hermes_runner=lambda command: calls.__setitem__("hermes", calls["hermes"] + 1),
        deliver=record_delivery,
    )
    assert calls["hermes"] == 0
    assert delivered.answer == NO_EVIDENCE_TEXT
    assert delivered.answer_kind == "no_evidence"
    assert delivered.cases == ()


def test_invalid_ai_answer_keeps_related_cases() -> None:
    run_answer_job(relevant_request(), hermes_runner=completed(b"unsupported claim"), deliver=record_delivery, ...)
    assert delivered.answer == CASES_ONLY_TEXT
    assert delivered.answer_kind == "cases_only"
    assert len(delivered.cases) == 1
```

- [ ] **Step 2: Run the focused tests and confirm failure**

Run: `uv run pytest tests/test_gitlab_qa_bridge.py -q`

Expected: no-evidence test reports that Hermes was called and prompt assertions still contain removed policy text.

- [ ] **Step 3: Replace the prompt and job flow**

`build_hermes_prompt` must serialize only `question` and `cases[*].as_dict()`. Its Korean instructions must say: do not call tools; use only supplied cases; one factual paragraph per line; end each paragraph with `[case_id · YYYY년 · PDF N쪽]`; never mention execution systems or internal policy fields. Remove the current mandatory `production_eligible` and `warning_code` instructions.

In `run_answer_job`:

```python
cases = public_cases_from_evidence(checked.question, checked.evidence)
if not cases:
    public_answer = no_evidence_answer()
else:
    try:
        completed = hermes_runner(build_hermes_command(hermes_path, build_hermes_prompt(checked, cases)))
        candidate = decode_bounded_answer(completed)
        grounded = validate_grounded_answer(candidate, cases)
    except (BridgeError, OSError, RuntimeError, UnicodeError, subprocess.SubprocessError):
        grounded = None
    public_answer = PublicAnswer(answer=grounded, answer_kind="grounded", cases=cases) if grounded else cases_only_answer(cases)
return deliver(issue_iid=checked.issue_iid, request_id=checked.request_id, answer=public_answer, token=delivery_token)
```

All caught failures must be converted outside `except` scopes so public exceptions retain neither cause nor context.

- [ ] **Step 4: Add prompt privacy assertions**

```python
def test_prompt_contains_only_public_case_fields() -> None:
    prompt = build_hermes_prompt(relevant_request(), public_cases())
    for forbidden in ("GitLab", "Webhook", "Hermes", "RAG", "production_eligible", "warning_code", "review_status"):
        assert forbidden.casefold() not in prompt.casefold()
    assert "senqa-2022-case-a" in prompt
    assert "PDF 4쪽" in prompt
```

- [ ] **Step 5: Run bridge tests and static checks**

Run: `uv run pytest tests/test_gitlab_qa_bridge.py tests/test_gitlab_qa_public.py -q`

Run: `uv run ruff check src/integrations/gitlab_qa_bridge.py src/integrations/gitlab_qa_public.py tests/test_gitlab_qa_bridge.py tests/test_gitlab_qa_public.py`

Run: `uv run mypy --strict --explicit-package-bases src/integrations/gitlab_qa_bridge.py src/integrations/gitlab_qa_public.py`

Expected: all commands pass.

- [ ] **Step 6: Commit**

```bash
git add src/integrations/gitlab_qa_bridge.py tests/test_gitlab_qa_bridge.py
git commit -m "feat: ground public answers in relevant cases"
```

### Task 3: Structured Private-Note Delivery and Worker Response

**Files:**
- Modify: `src/integrations/gitlab_qa_delivery.py`
- Modify: `tests/test_hermes_gitlab_qa_delivery.py`
- Modify: `web/qa-worker/src/worker.js`
- Modify: `web/qa-worker/test/worker.test.mjs`

**Interfaces:**
- Consumes: Task 1 `PublicAnswer` and `canonical_public_answer_json`.
- Produces: private note marker `<!-- senqa-answer:v2 request_id=... -->` followed by canonical JSON, and Worker completion JSON `{status, request_id, answer, answer_kind, cases}`.

- [ ] **Step 1: Write failing delivery v2 tests**

```python
def test_build_answer_comment_emits_only_v2_canonical_public_payload() -> None:
    comment = build_answer_comment("senqa-" + "a" * 32, grounded_public_answer())
    marker, payload = comment.split("\n", 1)
    assert marker == f"<!-- senqa-answer:v2 request_id={'senqa-' + 'a' * 32} -->"
    assert json.loads(payload)["cases"][0]["pdf_pages"] == [4]
    for forbidden in ("production_eligible", "warning_code", "review_status", "Hermes"):
        assert forbidden not in comment
```

- [ ] **Step 2: Run and verify old v1 banner causes failure**

Run: `uv run pytest tests/test_hermes_gitlab_qa_delivery.py -q`

Expected: v2 marker assertion fails.

- [ ] **Step 3: Implement v2 delivery**

Change signatures to:

```python
def build_answer_comment(request_id: object, answer: object) -> str: ...


def post_answer_comment(*, issue_iid: object, request_id: object, answer: object, token: object) -> str: ...
```

Require `type(answer) is PublicAnswer`, recursively rebuild via Task 1 validation, and serialize only canonical public JSON. Keep existing request-token and response-size checks unchanged.

- [ ] **Step 4: Write failing Worker v2 parser tests**

```javascript
test("poll returns a strict structured v2 answer", async () => {
  const response = await fetchPollWithNote(v2Note({ answer_kind: "grounded", cases: [publicCase] }));
  assert.deepEqual(await response.json(), {
    answer: "계약 기준입니다. [senqa-2022-case-a · 2022년 · PDF 4쪽]",
    answer_kind: "grounded",
    cases: [publicCase],
    request_id: REQUEST_ID,
    status: "complete",
  });
});

test("poll rejects a v2 payload with extra or internal fields", async () => {
  const response = await fetchPollWithNote(v2Note({ ...payload, warning_code: "hidden" }));
  assert.equal(response.status, 503);
});
```

- [ ] **Step 5: Implement exact Worker note parsing**

Add `parsePublicAnswerNote(body, requestId)` that verifies the exact v2 marker, UTF-8/JSON size, exact top-level keys, schema/kind combinations, answer length, maximum five cases, exact case keys, ID/year/page bounds, sorted unique pages, and string bounds. Reject prohibited terms in `answer`; do not scan or manufacture cases from prose. For a legacy v1 note, return a fixed public-safe `cases_only` message with `cases: []`, never its old visible body.

- [ ] **Step 6: Run delivery and Worker tests**

Run: `uv run pytest tests/test_hermes_gitlab_qa_delivery.py tests/test_gitlab_qa_bridge.py -q`

Run: `cd web/qa-worker && npm test`

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add src/integrations/gitlab_qa_delivery.py tests/test_hermes_gitlab_qa_delivery.py web/qa-worker/src/worker.js web/qa-worker/test/worker.test.mjs
git commit -m "feat: deliver structured public QA results"
```

### Task 4: Frontend State, Themes, and Safe History

**Files:**
- Create: `web/qa-worker/public/view-model.js`
- Create: `web/qa-worker/test/view-model.test.mjs`
- Modify: `web/qa-worker/public/app.js`

**Interfaces:**
- Consumes: Worker v2 completion JSON from Task 3.
- Produces: `normalizeCompletion(value)`, `resolveTheme(stored, prefersDark)`, `normalizeHistory(value)`, and a browser controller that renders structured data only.

- [ ] **Step 1: Write failing pure view-model tests**

```javascript
test("theme uses saved value before device preference", () => {
  assert.equal(resolveTheme("light", true), "light");
  assert.equal(resolveTheme(null, true), "dark");
  assert.equal(resolveTheme(null, false), "light");
});

test("completion strips extra and internal fields", () => {
  const normalized = normalizeCompletion({ ...completion, warning_code: "secret" });
  assert.equal(normalized, null);
});

test("history v2 retains cases but rejects old answer-only rows", () => {
  assert.deepEqual(normalizeHistory([completeHistoryRow]).length, 1);
  assert.deepEqual(normalizeHistory([{ question: "old", answer: "production_eligible=false" }]), []);
});
```

- [ ] **Step 2: Run and confirm missing module**

Run: `node --test web/qa-worker/test/view-model.test.mjs`

Expected: module-not-found failure.

- [ ] **Step 3: Implement pure exact validators**

`normalizeCompletion` mirrors the Worker public contract and returns a newly constructed frozen-compatible plain object. `normalizeHistory` caps at 12, requires v2 rows with `requestId`, `pollToken`, `question`, `createdAt`, `status`, and either no result for pending or a valid normalized completion. `resolveTheme` accepts only literal `light`/`dark`.

- [ ] **Step 4: Refactor app controller to structured rendering**

Use `STORAGE_KEY = "senqa-preview-questions-v2"` and `THEME_KEY = "senqa-theme-v1"`. Delete the prose regex `sourcesFromAnswer`. Render with `textContent` and DOM construction only:

```javascript
function showResult(result) {
  answerText.textContent = result.answer;
  renderRelatedCases(result.cases);
  renderEvidence(result.cases);
}
```

Each case uses a `<details>` element whose summary shows title/year/pages and whose body has separate `질의` and `답변` sections. The right rail shows only case ID, year, and PDF pages. Theme buttons update `document.documentElement.dataset.theme`, `colorScheme`, `aria-pressed`, localStorage, and Turnstile theme when the widget supports reset/rerender.

- [ ] **Step 5: Run view-model and existing Worker tests**

Run: `node --test web/qa-worker/test/view-model.test.mjs web/qa-worker/test/worker.test.mjs web/qa-worker/test/challenge.test.mjs`

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add web/qa-worker/public/view-model.js web/qa-worker/test/view-model.test.mjs web/qa-worker/public/app.js
git commit -m "feat: add safe QA result and theme state"
```

### Task 5: Accepted Evidence-First Visual Implementation

**Files:**
- Modify: `web/qa-worker/public/index.html`
- Modify: `web/qa-worker/public/styles.css`
- Modify: `web/qa-worker/public/app.js`

**Interfaces:**
- Consumes: Task 4 DOM IDs and controller.
- Produces: accessible three-region desktop UI and single-column mobile UI matching the approved visual-companion concept.

- [ ] **Step 1: Record the accepted design system in CSS tokens**

Implement these shared tokens before component rules:

```css
:root,
:root[data-theme="light"] {
  color-scheme: light;
  --bg: #f5f7fb;
  --surface: #ffffff;
  --surface-muted: #eef3f9;
  --text: #13243a;
  --muted: #607086;
  --border: #d8e1ec;
  --accent: #1368e8;
  --accent-strong: #0d4fb5;
  --teal: #0f9d91;
  --focus: #ffbf47;
}

:root[data-theme="dark"] {
  color-scheme: dark;
  --bg: #07111f;
  --surface: #0d1a2b;
  --surface-muted: #13233a;
  --text: #edf6ff;
  --muted: #9eb0c8;
  --border: #263a54;
  --accent: #4b91ff;
  --accent-strong: #72a8ff;
  --teal: #39c7b7;
  --focus: #ffd166;
}
```

Use a Korean-first system font stack, 14px chrome text, 16px body, 28–34px responsive H1, 12px radius, low-elevation shadows, 240px left rail, minmax center, and 260px right rail. Desktop grid: `240px minmax(0, 1fr) 260px`; collapse right rail under answer below 1100px and all rails to one column below 760px.

- [ ] **Step 2: Replace visible HTML with the accepted information architecture**

The header must contain brand, privacy reminder, and two theme buttons labeled `밝게` and `어둡게`. Main DOM must include:

```html
<aside id="history-panel" aria-labelledby="history-title">...</aside>
<main id="conversation-panel">
  <section aria-labelledby="page-title">question form and suggestion buttons</section>
  <section id="progress-panel" aria-live="polite" hidden>관련 사례를 찾고 답변을 정리하고 있습니다.</section>
  <section id="answer-panel" hidden>
    <p class="review-warning">미검수 참고 답변입니다. 실제 업무 처리 전 최신 지침을 확인하세요.</p>
    <div id="answer-text"></div>
    <section aria-labelledby="related-cases-title"><div id="related-cases"></div></section>
  </section>
</main>
<aside id="evidence-panel" aria-labelledby="evidence-title"><ol id="evidence-list"></ol></aside>
```

No visible text may name transport, execution, repository, model, or policy internals.

- [ ] **Step 3: Add polished responsive and interaction states**

Implement hover, active, disabled, `:focus-visible`, `<details>[open]`, pending skeleton/spinner, empty evidence, and reduced-motion states. Keep the textarea and submit action prominent. On mobile, place the evidence section after AI answer and before related cases; recent questions use native `<details>`/collapsible semantics without hiding keyboard access.

- [ ] **Step 4: Run static and JS tests**

Run: `node --check web/qa-worker/public/app.js && node --check web/qa-worker/public/view-model.js`

Run: `cd web/qa-worker && npm test`

Expected: all pass.

- [ ] **Step 5: Start local Worker and verify Browser workflow**

Run: `cd web/qa-worker && npx wrangler dev --local --port 8787`

Use the built-in Browser on `http://127.0.0.1:8787/`. Verify light/dark persistence, input and suggestion buttons, pending state, a mocked/fixture structured result, related-case disclosure, evidence rail, recent-question restore, keyboard focus, and a 390px mobile viewport.

- [ ] **Step 6: Capture and inspect visual fidelity**

Capture the accepted visual-companion screen and the current browser implementation at the same desktop viewport, save both under a temporary QA directory, and call `view_image` on both. Record a five-point ledger covering layout, typography, palette, answer/case separation, evidence rail, and responsive behavior. Fix every material mismatch before proceeding, then remove temporary screenshots.

- [ ] **Step 7: Commit**

```bash
git add web/qa-worker/public/index.html web/qa-worker/public/styles.css web/qa-worker/public/app.js
git commit -m "feat: redesign public QA around evidence"
```

### Task 6: Regression, Local Installation, and Public Deployment

**Files:**
- Modify: deployment/install assertions only if exact v2 function signatures require it.
- Do not modify operating aliases.

**Interfaces:**
- Consumes: all prior tasks.
- Produces: tested local bridge, deployed Worker, and one successful end-to-end public question.

- [ ] **Step 1: Run all focused regressions**

Run:

```bash
uv run pytest \
  tests/test_gitlab_qa_public.py \
  tests/test_gitlab_qa_bridge.py \
  tests/test_hermes_gitlab_qa_delivery.py \
  tests/test_hermes_gitlab_qa_webhook.py \
  tests/test_gitlab_public_ci.py \
  tests/test_release_scripts.py -q
cd web/qa-worker && npm test
```

Expected: all pass.

- [ ] **Step 2: Run full quality gates**

Run:

```bash
uv run pytest -q
uv run ruff check .
uv run mypy --strict --explicit-package-bases src
git diff --check
```

Expected: all pass; report unrelated pre-existing format/secret-history debt separately without changing unrelated files.

- [ ] **Step 3: Install and restart the local answer bridge**

Copy only the reviewed bridge/delivery/public-contract files into the existing owner-only installation root, preserve mode/ownership, and restart `com.h19h19.education-admin-hermes-qa` using the established LaunchAgent workflow. Verify the process is healthy without printing tokens, prompts, source text, or private issue contents.

- [ ] **Step 4: Deploy the Worker**

Run: `cd web/qa-worker && npx wrangler deploy`

Record the deployed version ID. Do not change the operating alias or public domain mapping.

- [ ] **Step 5: Run one public end-to-end verification**

In the built-in Browser, submit one supported education-administration question and verify: safe pending copy; structured AI answer; related case Q&A below; evidence ID/year/pages; no internal names/codes in response or DOM; history restore; both themes. Submit one clearly unrelated question and verify the exact no-evidence text and zero related cases.

- [ ] **Step 6: Final browser screenshot and `view_image` sign-off**

Capture the deployed desktop and mobile states. Use `view_image` for the accepted concept and final deployed screenshot in the same QA pass, confirm no material mismatches, and delete temporary QA artifacts.

- [ ] **Step 7: Commit deployment-test adjustments if any**

```bash
git add tests web/qa-worker src/integrations
git commit -m "test: verify public evidence-first QA flow"
```

- [ ] **Step 8: Push the branch without merging**

Run: `git push gitlab codex/gitlab-hermes-qa-web` and the configured GitHub mirror push.

Expected: both pushes succeed; do not merge to `main` without explicit user approval.

---

## Self-Review

- Spec coverage: AI-first response, separate case Q&A, evidence rail, exactly two themes, saved preference, relevance gate, no-AI empty result, fixed fallbacks, and internal-detail suppression are each owned by Tasks 1–5; deployment and end-to-end proof are Task 6.
- Placeholder scan: no `TBD`, `TODO`, unspecified error-handling step, or undefined future interface remains.
- Type consistency: Task 1 `PublicAnswer` is the sole payload accepted by Task 2 delivery, Task 3 serialization, and Worker v2 parsing; Worker completion fields exactly match Task 4 normalization and rendering.
- Security continuity: private issues, origin/rate/challenge/poll-token checks stay unchanged; only the answer-note payload and public rendering contract change.
