const MAX_BODY_BYTES = 16_384;
const MAX_QUESTION_CHARACTERS = 1_000;
const MAX_TURNSTILE_TOKEN_CHARACTERS = 4_096;
const FIRST_PARTY_CHALLENGE_BITS = 15;
const FIRST_PARTY_CHALLENGE_TTL_SECONDS = 300;
const FIRST_PARTY_CHALLENGE_RE = /^pow:(senqa-pow-[0-9a-f]{32}):([0-9]{1,7})$/u;
const REQUEST_RE = /^senqa-[0-9a-f]{32}$/;
const POLL_TOKEN_RE = /^[A-Za-z0-9_-]{40,64}$/;
const ANSWER_V2_PREFIX = "<!-- senqa-answer:v2 request_id=";
const ANSWER_V1_PREFIX = "<!-- senqa-answer:v1 request_id=";
const PUBLIC_SCHEMA = "senqa-public-answer/v1";
const NO_EVIDENCE_TEXT =
  "등록된 사례집에서 이 질문과 관련된 내용을 찾지 못했습니다. 다른 표현이나 핵심어로 다시 검색해 주세요.";
const CASES_ONLY_TEXT =
  "답변을 정리하지 못했습니다. 관련 사례는 아래 목록에서 직접 확인해 주세요.";
const LEGACY_TEXT =
  "이전 형식의 답변입니다. 같은 질문을 다시 검색해 관련 사례를 확인해 주세요.";
const PUBLIC_CASE_RE = /^senqa-20(?:20|21|22|23|24|25)-[a-z0-9-]{1,160}$/u;
const FORBIDDEN_PUBLIC_TERMS = [
  "gitlab",
  "webhook",
  "hermes",
  "rag",
  "production_eligible",
  "warning_code",
  "complete_corpus",
  "review_status",
];
const MAX_ANSWER_NOTE_CHARACTERS = 192 * 1024;

function jsonResponse(value, status, origin) {
  return new Response(JSON.stringify(value), {
    status,
    headers: {
      "access-control-allow-origin": origin,
      "cache-control": "no-store",
      "content-type": "application/json; charset=utf-8",
      "referrer-policy": "no-referrer",
      vary: "Origin",
    },
  });
}

function errorResponse(code, status, origin) {
  return jsonResponse({ error_code: code }, status, origin);
}

function exactOrigin(request, env) {
  const configured = typeof env.PUBLIC_ORIGIN === "string" ? env.PUBLIC_ORIGIN : "";
  const supplied = request.headers.get("origin") ?? "";
  const requestOrigin = new URL(request.url).origin;
  const approvedScheme =
    configured.startsWith("https://") ||
    (env.ENVIRONMENT === "development" && /^http:\/\/127\.0\.0\.1:[0-9]{2,5}$/u.test(configured));
  return approvedScheme &&
    ((supplied && supplied === configured) || (!supplied && requestOrigin === configured))
    ? configured
    : null;
}

export function normalizeQuestion(value) {
  if (typeof value !== "string" || value.length > MAX_QUESTION_CHARACTERS) return null;
  for (const character of value) {
    const code = character.codePointAt(0);
    if ((code < 32 || code === 127) && !/\s/u.test(character)) return null;
  }
  const normalized = value.trim().replace(/\s+/gu, " ");
  return normalized && normalized.length <= MAX_QUESTION_CHARACTERS ? normalized : null;
}

function bytesToHex(bytes) {
  return [...bytes].map((value) => value.toString(16).padStart(2, "0")).join("");
}

function bytesToBase64Url(bytes) {
  let binary = "";
  for (const value of bytes) binary += String.fromCharCode(value);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/u, "");
}

function randomBytes(env, size) {
  if (typeof env.RANDOM_BYTES === "function") return env.RANDOM_BYTES(size);
  const bytes = new Uint8Array(size);
  crypto.getRandomValues(bytes);
  return bytes;
}

async function sha256(value) {
  const bytes = new TextEncoder().encode(value);
  return bytesToHex(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)));
}

function safeEqual(left, right) {
  if (typeof left !== "string" || typeof right !== "string" || left.length !== right.length) {
    return false;
  }
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) {
    difference |= left.charCodeAt(index) ^ right.charCodeAt(index);
  }
  return difference === 0;
}

function exactObject(value, keys) {
  return (
    value !== null &&
    typeof value === "object" &&
    value.constructor === Object &&
    Object.keys(value).sort().join(",") === [...keys].sort().join(",")
  );
}

function boundedPublicText(value, maximum) {
  return (
    typeof value === "string" &&
    value.trim().length > 0 &&
    value.length <= maximum &&
    ![...value].some((character) => {
      const code = character.codePointAt(0);
      return (code < 32 || code === 127) && !"\n\r\t".includes(character);
    })
  );
}

function parsePublicCase(value) {
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
    !PUBLIC_CASE_RE.test(value.case_id) ||
    !Number.isInteger(value.edition_year) ||
    value.edition_year < 2020 ||
    value.edition_year > 2025 ||
    !Array.isArray(value.pdf_pages) ||
    value.pdf_pages.length === 0 ||
    value.pdf_pages.length > 100 ||
    value.pdf_pages.some((page) => !Number.isInteger(page) || page < 1 || page > 10_000) ||
    value.pdf_pages.some((page, index) => index > 0 && page <= value.pdf_pages[index - 1]) ||
    !boundedPublicText(value.title, 2_000) ||
    !boundedPublicText(value.question, 24_000) ||
    !boundedPublicText(value.answer, 24_000)
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
  const folded = answer.toLocaleLowerCase("en-US");
  if (FORBIDDEN_PUBLIC_TERMS.some((term) => folded.includes(term))) return false;
  const mentionedIds = answer.match(/senqa-20(?:20|21|22|23|24|25)-[a-z0-9-]{1,160}/gu) ?? [];
  if (mentionedIds.length > 0) return false;
  const paragraphs = answer.split(/\n\s*\n|\n/gu).map((item) => item.trim()).filter(Boolean);
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

function parsePublicAnswerPayload(raw) {
  if (typeof raw !== "string" || !raw || raw.length > MAX_ANSWER_NOTE_CHARACTERS) return null;
  let value;
  try {
    value = JSON.parse(raw);
  } catch {
    return null;
  }
  if (
    !exactObject(value, ["answer", "answer_kind", "cases", "schema_version"]) ||
    value.schema_version !== PUBLIC_SCHEMA ||
    !["grounded", "no_evidence", "cases_only"].includes(value.answer_kind) ||
    !boundedPublicText(value.answer, 32_000) ||
    !Array.isArray(value.cases) ||
    value.cases.length > 5
  ) {
    return null;
  }
  const cases = value.cases.map(parsePublicCase);
  if (cases.some((item) => item === null)) return null;
  const checkedCases = cases;
  if (new Set(checkedCases.map((item) => item.case_id)).size !== checkedCases.length) return null;
  if (
    (value.answer_kind === "no_evidence" &&
      (value.answer !== NO_EVIDENCE_TEXT || checkedCases.length !== 0)) ||
    (value.answer_kind === "cases_only" &&
      (value.answer !== CASES_ONLY_TEXT || checkedCases.length === 0)) ||
    (value.answer_kind === "grounded" &&
      (checkedCases.length === 0 || !groundedAnswerIsValid(value.answer, checkedCases)))
  ) {
    return null;
  }
  return {
    answer: value.answer,
    answer_kind: value.answer_kind,
    cases: checkedCases,
  };
}

async function parseBody(request) {
  if (request.headers.get("content-type")?.split(";", 1)[0] !== "application/json") {
    return null;
  }
  const raw = new Uint8Array(await request.arrayBuffer());
  if (raw.byteLength === 0 || raw.byteLength > MAX_BODY_BYTES) return null;
  try {
    const parsed = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(raw));
    return parsed && parsed.constructor === Object ? parsed : null;
  } catch {
    return null;
  }
}

async function verifyTurnstile(token, request, env) {
  if (
    typeof token !== "string" ||
    !token ||
    token.length > MAX_TURNSTILE_TOKEN_CHARACTERS ||
    typeof env.TURNSTILE_SECRET !== "string" ||
    !env.TURNSTILE_SECRET
  ) {
    return false;
  }
  const form = new FormData();
  form.set("secret", env.TURNSTILE_SECRET);
  form.set("response", token);
  const remoteIp = request.headers.get("cf-connecting-ip");
  if (remoteIp) form.set("remoteip", remoteIp);
  try {
    const fetchImpl = env.FETCH ?? fetch;
    const response = await fetchImpl(
      "https://challenges.cloudflare.com/turnstile/v0/siteverify",
      { method: "POST", body: form },
    );
    const result = await response.json();
    return response.ok && result?.success === true;
  } catch {
    return false;
  }
}

function hasLeadingZeroBits(hexDigest, difficultyBits) {
  const wholeNibbles = Math.floor(difficultyBits / 4);
  if (!hexDigest.startsWith("0".repeat(wholeNibbles))) return false;
  const remainingBits = difficultyBits % 4;
  return (
    remainingBits === 0 ||
    Number.parseInt(hexDigest[wholeNibbles], 16) < 2 ** (4 - remainingBits)
  );
}

async function clientFingerprint(request) {
  return sha256(request.headers.get("cf-connecting-ip") ?? "unknown");
}

async function verifyFirstPartyChallenge(token, request, env) {
  if (typeof token !== "string" || token.length > MAX_TURNSTILE_TOKEN_CHARACTERS) return false;
  const matched = FIRST_PARTY_CHALLENGE_RE.exec(token);
  if (!matched) return false;
  const nonce = Number.parseInt(matched[2], 10);
  if (!Number.isSafeInteger(nonce) || nonce < 0 || nonce > 2_000_000) return false;
  const key = `challenge:${matched[1]}`;
  const recordText = await env.QUESTIONS.get(key);
  if (recordText === null) return false;
  let record;
  try {
    record = JSON.parse(recordText);
  } catch {
    return false;
  }
  const now = typeof env.NOW === "function" ? env.NOW() : Date.now();
  if (
    !record ||
    record.constructor !== Object ||
    record.difficulty_bits !== FIRST_PARTY_CHALLENGE_BITS ||
    typeof record.client_sha256 !== "string" ||
    record.client_sha256 !== (await clientFingerprint(request)) ||
    !Number.isSafeInteger(record.created_at) ||
    record.created_at > now ||
    now - record.created_at > FIRST_PARTY_CHALLENGE_TTL_SECONDS * 1_000
  ) {
    return false;
  }
  const digest = await sha256(`${matched[1]}:${nonce}`);
  if (!hasLeadingZeroBits(digest, FIRST_PARTY_CHALLENGE_BITS)) return false;
  await env.QUESTIONS.delete(key);
  return true;
}

async function verifyHumanChallenge(token, request, env) {
  if (typeof token === "string" && token.startsWith("pow:")) {
    return verifyFirstPartyChallenge(token, request, env);
  }
  return verifyTurnstile(token, request, env);
}

function gitlabConfig(env) {
  const projectId = typeof env.GITLAB_PROJECT_ID === "string" ? env.GITLAB_PROJECT_ID : "";
  const apiRoot = typeof env.GITLAB_API_ROOT === "string" ? env.GITLAB_API_ROOT : "";
  const token = typeof env.GITLAB_TOKEN === "string" ? env.GITLAB_TOKEN : "";
  if (
    projectId !== "428" ||
    apiRoot !== "https://gitlab.aigov.go.kr/api/v4" ||
    !token ||
    token.length > 512 ||
    /\s/u.test(token)
  ) {
    return null;
  }
  return { projectId, apiRoot, token };
}

async function gitlabRequest(env, path, init = {}) {
  const config = gitlabConfig(env);
  if (!config) return null;
  const fetchImpl = env.FETCH ?? fetch;
  try {
    return await fetchImpl(`${config.apiRoot}/projects/${config.projectId}${path}`, {
      ...init,
      headers: {
        "content-type": "application/json",
        "PRIVATE-TOKEN": config.token,
        ...(init.headers ?? {}),
      },
    });
  } catch {
    return null;
  }
}

async function fixedWindowAllowed(request, env, prefix, limit) {
  const ip = request.headers.get("cf-connecting-ip") ?? "unknown";
  const window = Math.floor((typeof env.NOW === "function" ? env.NOW() : Date.now()) / 60_000);
  const key = `${prefix}:${await sha256(ip)}:${window}`;
  const currentText = await env.QUESTIONS.get(key);
  const current = currentText === null ? 0 : Number.parseInt(currentText, 10);
  if (!Number.isSafeInteger(current) || current >= limit) return false;
  await env.QUESTIONS.put(key, String(current + 1), { expirationTtl: 120 });
  return true;
}

async function rateAllowed(request, env) {
  return fixedWindowAllowed(request, env, "rate", 5);
}

async function createQuestion(request, env, origin) {
  if (!(await rateAllowed(request, env))) return errorResponse("rate_limited", 429, origin);
  const body = await parseBody(request);
  if (!body || Object.keys(body).sort().join(",") !== "question,turnstile_token") {
    return errorResponse("question_invalid", 400, origin);
  }
  const question = normalizeQuestion(body.question);
  if (!question) return errorResponse("question_invalid", 400, origin);
  if (!(await verifyHumanChallenge(body.turnstile_token, request, env))) {
    return errorResponse("challenge_invalid", 403, origin);
  }

  const requestId = `senqa-${bytesToHex(randomBytes(env, 16))}`;
  const pollToken = bytesToBase64Url(randomBytes(env, 32));
  const issueResponse = await gitlabRequest(env, "/issues", {
    method: "POST",
    body: JSON.stringify({
      confidential: true,
      description: "SEN-QA confidential webhook request. Do not make public.",
      title: `[SEN-QA] ${requestId}`,
    }),
  });
  if (!issueResponse || issueResponse.status !== 201) {
    return errorResponse("queue_unavailable", 503, origin);
  }
  let issue;
  try {
    issue = await issueResponse.json();
  } catch {
    return errorResponse("queue_unavailable", 503, origin);
  }
  if (!Number.isInteger(issue?.iid) || issue.iid < 1 || issue.iid > 1_000_000) {
    return errorResponse("queue_unavailable", 503, origin);
  }
  const noteResponse = await gitlabRequest(env, `/issues/${issue.iid}/notes`, {
    method: "POST",
    body: JSON.stringify({ body: `/hermes ask ${requestId}\n${question}` }),
  });
  if (!noteResponse || noteResponse.status !== 201) {
    return errorResponse("queue_unavailable", 503, origin);
  }
  const now = typeof env.NOW === "function" ? env.NOW() : Date.now();
  await env.QUESTIONS.put(
    `question:${requestId}`,
    JSON.stringify({
      created_at: now,
      issue_iid: issue.iid,
      poll_token_sha256: await sha256(pollToken),
    }),
    { expirationTtl: 86_400 },
  );
  return jsonResponse(
    { request_id: requestId, poll_token: pollToken, status: "pending" },
    202,
    origin,
  );
}

async function issueFirstPartyChallenge(request, env, origin) {
  if (!(await fixedWindowAllowed(request, env, "challenge-rate", 10))) {
    return errorResponse("rate_limited", 429, origin);
  }
  const challengeId = `senqa-pow-${bytesToHex(randomBytes(env, 16))}`;
  const createdAt = typeof env.NOW === "function" ? env.NOW() : Date.now();
  await env.QUESTIONS.put(
    `challenge:${challengeId}`,
    JSON.stringify({
      client_sha256: await clientFingerprint(request),
      created_at: createdAt,
      difficulty_bits: FIRST_PARTY_CHALLENGE_BITS,
    }),
    { expirationTtl: FIRST_PARTY_CHALLENGE_TTL_SECONDS },
  );
  return jsonResponse(
    { challenge_id: challengeId, difficulty_bits: FIRST_PARTY_CHALLENGE_BITS },
    200,
    origin,
  );
}

async function pollQuestion(request, env, origin, requestId) {
  if (!REQUEST_RE.test(requestId)) return errorResponse("not_found", 404, origin);
  const token = new URL(request.url).searchParams.get("token") ?? "";
  if (!POLL_TOKEN_RE.test(token)) return errorResponse("not_found", 404, origin);
  const recordText = await env.QUESTIONS.get(`question:${requestId}`);
  if (recordText === null) return errorResponse("not_found", 404, origin);
  let record;
  try {
    record = JSON.parse(recordText);
  } catch {
    return errorResponse("not_found", 404, origin);
  }
  if (
    !Number.isInteger(record?.issue_iid) ||
    record.issue_iid < 1 ||
    typeof record?.poll_token_sha256 !== "string" ||
    !safeEqual(record.poll_token_sha256, await sha256(token))
  ) {
    return errorResponse("not_found", 404, origin);
  }
  const notesResponse = await gitlabRequest(
    env,
    `/issues/${record.issue_iid}/notes?per_page=100&sort=desc`,
  );
  if (!notesResponse?.ok) return errorResponse("queue_unavailable", 503, origin);
  let notes;
  try {
    notes = await notesResponse.json();
  } catch {
    return errorResponse("queue_unavailable", 503, origin);
  }
  if (!Array.isArray(notes)) return errorResponse("queue_unavailable", 503, origin);
  const v2Marker = `${ANSWER_V2_PREFIX}${requestId} -->\n`;
  const answerNote = notes.find(
    (note) => note && typeof note.body === "string" && note.body.startsWith(v2Marker),
  );
  if (answerNote) {
    const parsed = parsePublicAnswerPayload(answerNote.body.slice(v2Marker.length));
    if (!parsed) return errorResponse("answer_invalid", 503, origin);
    return jsonResponse({ ...parsed, request_id: requestId, status: "complete" }, 200, origin);
  }
  const v1Marker = `${ANSWER_V1_PREFIX}${requestId} -->\n`;
  const legacyNote = notes.find(
    (note) => note && typeof note.body === "string" && note.body.startsWith(v1Marker),
  );
  if (legacyNote) {
    return jsonResponse(
      {
        answer: LEGACY_TEXT,
        answer_kind: "cases_only",
        cases: [],
        request_id: requestId,
        status: "complete",
      },
      200,
      origin,
    );
  }
  return jsonResponse({ request_id: requestId, status: "pending" }, 200, origin);
}

async function handleApi(request, env) {
  const origin = exactOrigin(request, env);
  if (!origin) return errorResponse("origin_invalid", 403, "null");
  const url = new URL(request.url);
  if (request.method === "OPTIONS") {
    return new Response(null, {
      status: 204,
      headers: {
        "access-control-allow-headers": "content-type",
        "access-control-allow-methods": "GET,POST,OPTIONS",
        "access-control-allow-origin": origin,
        "access-control-max-age": "600",
      },
    });
  }
  if (request.method === "GET" && url.pathname === "/api/config") {
    const siteKey = typeof env.TURNSTILE_SITE_KEY === "string" ? env.TURNSTILE_SITE_KEY : "";
    if (!/^[A-Za-z0-9_-]{10,128}$/u.test(siteKey)) {
      return errorResponse("config_invalid", 503, origin);
    }
    return jsonResponse(
      {
        max_question_characters: MAX_QUESTION_CHARACTERS,
        turnstile_site_key: siteKey,
      },
      200,
      origin,
    );
  }
  if (request.method === "GET" && url.pathname === "/api/challenge") {
    return issueFirstPartyChallenge(request, env, origin);
  }
  if (request.method === "POST" && url.pathname === "/api/questions") {
    return createQuestion(request, env, origin);
  }
  const matched = /^\/api\/questions\/(senqa-[0-9a-f]{32})$/u.exec(url.pathname);
  if (request.method === "GET" && matched) return pollQuestion(request, env, origin, matched[1]);
  return errorResponse("not_found", 404, origin);
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname.startsWith("/api/")) return handleApi(request, env);
    if (!env.ASSETS?.fetch) return new Response("Not found", { status: 404 });
    const response = await env.ASSETS.fetch(request);
    const headers = new Headers(response.headers);
    headers.set(
      "content-security-policy",
      "default-src 'self'; script-src 'self' https://challenges.cloudflare.com; frame-src https://challenges.cloudflare.com; connect-src 'self'; img-src 'self'; style-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
    );
    headers.set("permissions-policy", "camera=(), microphone=(), geolocation=()");
    headers.set("referrer-policy", "no-referrer");
    headers.set("x-content-type-options", "nosniff");
    return new Response(response.body, { status: response.status, headers });
  },
};
