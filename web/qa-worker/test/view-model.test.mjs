import assert from "node:assert/strict";
import test from "node:test";

import {
  normalizeCompletion,
  normalizeHistory,
  resolveTheme,
} from "../public/view-model.js";

const PUBLIC_CASE = {
  answer: "관련 기준을 확인합니다.",
  case_id: "senqa-2022-case-a",
  edition_year: 2022,
  pdf_pages: [4],
  question: "수의계약이 가능한가요?",
  title: "계약 사례",
};

const COMPLETION = {
  answer: "계약 기준입니다. [senqa-2022-case-a · 2022년 · PDF 4쪽]",
  answer_kind: "grounded",
  cases: [PUBLIC_CASE],
  request_id: "senqa-0123456789abcdef0123456789abcdef",
  status: "complete",
};

test("theme uses saved value before device preference", () => {
  assert.equal(resolveTheme("light", true), "light");
  assert.equal(resolveTheme("dark", false), "dark");
  assert.equal(resolveTheme(null, true), "dark");
  assert.equal(resolveTheme(null, false), "light");
  assert.equal(resolveTheme("unknown", false), "light");
});

test("completion accepts only exact public fields", () => {
  assert.deepEqual(normalizeCompletion(COMPLETION), COMPLETION);
  assert.equal(normalizeCompletion({ ...COMPLETION, warning_code: "secret" }), null);
  assert.equal(
    normalizeCompletion({
      ...COMPLETION,
      answer: "production_eligible=false",
    }),
    null,
  );
});

test("completion rejects a case with malformed page authority", () => {
  assert.equal(
    normalizeCompletion({
      ...COMPLETION,
      cases: [{ ...PUBLIC_CASE, pdf_pages: [4, 4] }],
    }),
    null,
  );
});

test("no-evidence completion accepts only the exact fixed guidance", () => {
  const value = {
    answer:
      "등록된 사례집에서 이 질문과 관련된 내용을 찾지 못했습니다. 다른 표현이나 핵심어로 다시 검색해 주세요.",
    answer_kind: "no_evidence",
    cases: [],
    request_id: COMPLETION.request_id,
    status: "complete",
  };
  assert.deepEqual(normalizeCompletion(value), value);
  assert.equal(normalizeCompletion({ ...value, answer: "관련 내용이 없습니다." }), null);
});

test("history v2 keeps structured results and drops answer-only legacy rows", () => {
  const completeRow = {
    createdAt: 1_800_000_000_000,
    pollToken: "A".repeat(43),
    question: "수의계약 기준을 알려줘",
    requestId: COMPLETION.request_id,
    result: COMPLETION,
    status: "complete",
  };
  assert.deepEqual(normalizeHistory([completeRow]), [completeRow]);
  assert.deepEqual(
    normalizeHistory([
      {
        answer: "production_eligible=false",
        createdAt: 1_800_000_000_000,
        question: "old",
      },
    ]),
    [],
  );
});

test("history accepts a strict pending row", () => {
  const pending = {
    createdAt: 1_800_000_000_000,
    pollToken: "B".repeat(43),
    question: "학교 계약 기준",
    requestId: "senqa-abcdefabcdefabcdefabcdefabcdefab",
    status: "pending",
  };
  assert.deepEqual(normalizeHistory([pending]), [pending]);
});
