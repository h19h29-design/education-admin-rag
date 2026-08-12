const REQUEST_RE = /^senqa-[0-9a-f]{32}$/u;
const POLL_TOKEN_RE = /^[A-Za-z0-9_-]{40,64}$/u;
const CASE_RE = /^senqa-20(?:20|21|22|23|24|25)-[a-z0-9-]{1,160}$/u;
const NO_EVIDENCE_TEXT =
  "등록된 사례집에서 이 질문과 관련된 내용을 찾지 못했습니다. 다른 표현이나 핵심어로 다시 검색해 주세요.";
const CASES_ONLY_TEXT =
  "답변을 정리하지 못했습니다. 관련 사례는 아래 목록에서 직접 확인해 주세요.";
const LEGACY_TEXT =
  "이전 형식의 답변입니다. 같은 질문을 다시 검색해 관련 사례를 확인해 주세요.";
export const PENDING_TIMEOUT_MS = 5 * 60 * 1_000;
const MAX_DATE_TIMESTAMP = 8_640_000_000_000_000;
const FORBIDDEN_TERMS = [
  "gitlab",
  "webhook",
  "hermes",
  "rag",
  "production_eligible",
  "warning_code",
  "complete_corpus",
  "review_status",
];

function exactObject(value, keys) {
  return (
    value !== null &&
    typeof value === "object" &&
    value.constructor === Object &&
    Object.keys(value).sort().join(",") === [...keys].sort().join(",")
  );
}

function publicText(value, maximum) {
  return (
    typeof value === "string" &&
    value.trim().length > 0 &&
    value.length <= maximum &&
    !FORBIDDEN_TERMS.some((term) => value.toLocaleLowerCase("en-US").includes(term))
  );
}

function normalizeCase(value) {
  if (
    !exactObject(value, [
      "answer",
      "case_id",
      "edition_year",
      "pdf_pages",
      "question",
      "title",
    ]) ||
    typeof value.case_id !== "string" ||
    !CASE_RE.test(value.case_id) ||
    !Number.isInteger(value.edition_year) ||
    value.edition_year < 2020 ||
    value.edition_year > 2025 ||
    !Array.isArray(value.pdf_pages) ||
    value.pdf_pages.length < 1 ||
    value.pdf_pages.length > 100 ||
    value.pdf_pages.some((page) => !Number.isInteger(page) || page < 1 || page > 10_000) ||
    value.pdf_pages.some((page, index) => index > 0 && page <= value.pdf_pages[index - 1]) ||
    !publicText(value.title, 2_000) ||
    !publicText(value.question, 24_000) ||
    !publicText(value.answer, 24_000)
  ) {
    return null;
  }
  return {
    answer: value.answer,
    case_id: value.case_id,
    edition_year: value.edition_year,
    pdf_pages: [...value.pdf_pages],
    question: value.question,
    title: value.title,
  };
}

function groundedAnswerIsValid(answer, cases) {
  if (/senqa-20(?:20|21|22|23|24|25)-[a-z0-9-]{1,160}/u.test(answer)) return false;
  const paragraphs = answer.split(/\n\s*\n|\n/gu).map((value) => value.trim()).filter(Boolean);
  return paragraphs.every((paragraph) =>
    cases.some((item) =>
      item.pdf_pages.some((page) =>
        paragraph.includes(
          `[${item.edition_year}년 · PDF ${page}쪽]`,
        ),
      ),
    ),
  );
}

export function publicSourceLabel(item) {
  return `${item.edition_year}년 · PDF ${item.pdf_pages.join(", ")}쪽`;
}

export function normalizeCompletion(value) {
  if (
    !exactObject(value, ["answer", "answer_kind", "cases", "request_id", "status"]) ||
    value.status !== "complete" ||
    typeof value.request_id !== "string" ||
    !REQUEST_RE.test(value.request_id) ||
    !["grounded", "no_evidence", "cases_only"].includes(value.answer_kind) ||
    !publicText(value.answer, 32_000) ||
    !Array.isArray(value.cases) ||
    value.cases.length > 20
  ) {
    return null;
  }
  const cases = value.cases.map(normalizeCase);
  if (
    cases.some((item) => item === null) ||
    new Set(cases.map((item) => item.case_id)).size !== cases.length
  ) {
    return null;
  }
  if (
    (value.answer_kind === "grounded" &&
      (cases.length === 0 || !groundedAnswerIsValid(value.answer, cases))) ||
    (value.answer_kind === "no_evidence" &&
      (value.answer !== NO_EVIDENCE_TEXT || cases.length !== 0)) ||
    (value.answer_kind === "cases_only" &&
      (value.answer === CASES_ONLY_TEXT ? cases.length === 0 : value.answer !== LEGACY_TEXT || cases.length !== 0))
  ) {
    return null;
  }
  return {
    answer: value.answer,
    answer_kind: value.answer_kind,
    cases,
    request_id: value.request_id,
    status: "complete",
  };
}

export function resolveTheme(stored, prefersDark) {
  if (stored === "light" || stored === "dark") return stored;
  return prefersDark === true ? "dark" : "light";
}

export function historyStatusLabel(status) {
  if (status === "complete") return "검색 완료";
  if (status === "retry") return "다시 검색 필요";
  return "답변 준비 중";
}

export function normalizeHistory(value) {
  if (!Array.isArray(value)) return [];
  const checked = [];
  for (const item of value.slice(0, 12)) {
    if (
      item === null ||
      typeof item !== "object" ||
      item.constructor !== Object ||
      !Number.isSafeInteger(item.createdAt) ||
      item.createdAt < 1 ||
      item.createdAt > MAX_DATE_TIMESTAMP ||
      typeof item.question !== "string" ||
      !item.question.trim() ||
      item.question.length > 1_000 ||
      typeof item.requestId !== "string" ||
      !REQUEST_RE.test(item.requestId)
    ) {
      continue;
    }
    if (
      item.status === "pending" &&
      exactObject(item, ["createdAt", "pollToken", "question", "requestId", "status"]) &&
      typeof item.pollToken === "string" &&
      POLL_TOKEN_RE.test(item.pollToken)
    ) {
      checked.push({
        createdAt: item.createdAt,
        pollToken: item.pollToken,
        question: item.question,
        requestId: item.requestId,
        status: "pending",
      });
      continue;
    }
    if (
      item.status === "complete" &&
      exactObject(item, ["createdAt", "pollToken", "question", "requestId", "result", "status"]) &&
      typeof item.pollToken === "string" &&
      POLL_TOKEN_RE.test(item.pollToken)
    ) {
      const result = normalizeCompletion(item.result);
      if (result && result.request_id === item.requestId) {
        checked.push({
          createdAt: item.createdAt,
          pollToken: item.pollToken,
          question: item.question,
          requestId: item.requestId,
          result,
          status: "complete",
        });
      }
      continue;
    }
    if (
      item.status === "retry" &&
      exactObject(item, ["createdAt", "question", "requestId", "status"])
    ) {
      checked.push({
        createdAt: item.createdAt,
        question: item.question,
        requestId: item.requestId,
        status: "retry",
      });
    }
  }
  return checked;
}

export function expirePendingHistory(value, now) {
  const timestamp = Number.isSafeInteger(now) && now > 0 ? now : Date.now();
  return normalizeHistory(value).map((item) => {
    if (item.status !== "pending" || timestamp - item.createdAt < PENDING_TIMEOUT_MS) {
      return item;
    }
    return {
      createdAt: item.createdAt,
      question: item.question,
      requestId: item.requestId,
      status: "retry",
    };
  });
}
