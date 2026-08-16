# 교육행정 AI 통합 검색 UI 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task.

**Goal:** 공개 QA 화면을 질문 중심의 단일 흐름으로 바꾼다. 검색 결과는 AI 답변, 검증 가능한 관련 도구(있을 때만), 관련 사례 최대 20건 순서로 보여 주며, 짧은 업무 키워드도 관련 사례를 유지하고 오래 대기한 요청은 안전하게 재검색할 수 있게 한다.

**Architecture:** 기존 Cloudflare Worker와 맥미니 검색·답변 브리지는 그대로 두고, 정적 브라우저 UI가 엄격한 공개 응답을 렌더링한다. `view-model.js`는 히스토리·대기 만료의 순수 계약을 담당하고, `app.js`는 요청·폴링·재검색·서랍 상호작용을 담당한다. 실제로 동작하는지 확인되지 않은 업무 도구는 공개 화면에 만들거나 연결하지 않는다.

**Tech Stack:** 정적 HTML/CSS/ES modules, Cloudflare Worker assets, Node built-in test runner, Wrangler.

## Task 1: 대기 만료·재검색 상태를 순수 모델로 고정

**Files:**

- Modify: `web/qa-worker/public/view-model.js`
- Modify: `web/qa-worker/test/view-model.test.mjs`

- [ ] Write failing tests for a strict `retry` history row that retains only the public question/request metadata, and for a pending entry older than five minutes converting to `retry`.
- [ ] Add exact normalization helpers: a five-minute timeout constant, expired-pending conversion, and a public Korean status label. Never preserve a polling token in a retry row.
- [ ] Keep existing pending/complete rows backward-compatible; malformed, leaked, or mixed-shape browser storage must continue to be dropped.
- [ ] Run `node --test web/qa-worker/test/view-model.test.mjs`.

## Task 2: 승인된 답변 중심 화면을 구현

**Files:**

- Modify: `web/qa-worker/public/index.html`
- Modify: `web/qa-worker/public/styles.css`
- Modify: `web/qa-worker/public/app.js`

- [ ] Replace the fixed three-column layout with a single readable main flow and a recent-questions drawer/dialog.
- [ ] Implement the two approved themes only, a focused question input, concise keyword suggestions, privacy copy near the input, AI answer, and then expandable related cases.
- [ ] Remove the persistent evidence rail. Render `연도 · PDF 쪽` directly in answer/case regions; never render case IDs or internal transport information.
- [ ] Do not render a “related tools” section unless a separately verified, executable public tool mapping exists. The absence of a tool must leave no empty UI area.
- [ ] Make a retry history entry restore its question, focus the input, and create a fresh request on submission rather than polling an expired token.
- [ ] Update the polling loop to use elapsed time from `createdAt`, mark stale requests as retry, and display the fixed delay guidance.
- [ ] Preserve Turnstile and first-party challenge behavior, keyboard focus, aria-live progress, and the 20-case result cap.

## Task 3: functional and visual verification

- [ ] Run Node view-model tests and Worker tests relevant to static assets.
- [ ] Start a local Wrangler preview, exercise a response containing one-word search results, inspect result ordering, history retry behavior, both themes, and keyboard navigation.
- [ ] Capture desktop and 390px mobile screenshots. Compare the rendered screen with the checked-in approved concept using `view_image`.
- [ ] Repair any visual mismatch that materially affects the approved hierarchy, typography, theme contrast, input usability, or responsive overflow.

## Task 4: deploy and hand off

- [ ] Run the Worker’s project test/lint commands and inspect `git diff --check`.
- [ ] Commit only application and test/doc files; do not stage `.superpowers/` artifacts.
- [ ] Push the branch to GitLab and the GitHub mirror.
- [ ] Deploy the approved Cloudflare Worker asset update using the existing project configuration, then verify the public URL with a short query such as `계약`.

## Constraints

- Keep exact no-evidence and answer-fallback public copy from the approved specification.
- Public browser state may contain only question, public request ID, public timestamps, public polling token while pending, and validated completed results.
- Never expose GitLab, webhook, Hermes, RAG, repositories, models, case identifiers, policies, internal URLs, or error codes in the UI.
- Do not alter corpus, review, or OCR worktrees while implementing the public web surface.
