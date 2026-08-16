import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import test from "node:test";

import worker, { normalizeQuestion } from "../src/worker.js";

const ORIGIN = "https://ask.example.test";

class MemoryKv {
  values = new Map();

  async get(key) {
    return this.values.get(key) ?? null;
  }

  async put(key, value) {
    this.values.set(key, value);
  }

  async delete(key) {
    this.values.delete(key);
  }
}

function env(fetchImpl) {
  return {
    GITLAB_TOKEN: "gitlab-secret",
    GITLAB_PROJECT_ID: "428",
    GITLAB_API_ROOT: "https://gitlab.aigov.go.kr/api/v4",
    PUBLIC_ORIGIN: ORIGIN,
    TURNSTILE_SECRET: "turnstile-secret",
    TURNSTILE_SITE_KEY: "1x00000000000000000000AA",
    QUESTIONS: new MemoryKv(),
    FETCH: fetchImpl,
    NOW: () => 1_800_000_000_000,
    RANDOM_BYTES: (size) => new Uint8Array(size).fill(7),
  };
}

function postRequest(payload, origin = ORIGIN) {
  return new Request(`${ORIGIN}/api/questions`, {
    method: "POST",
    headers: { "content-type": "application/json", origin },
    body: JSON.stringify(payload),
  });
}

function solveChallenge(challengeId, difficultyBits) {
  const zeroHexCharacters = Math.floor(difficultyBits / 4);
  const remainingBits = difficultyBits % 4;
  for (let nonce = 0; nonce < 2_000_000; nonce += 1) {
    const digest = createHash("sha256").update(`${challengeId}:${nonce}`).digest("hex");
    if (
      digest.startsWith("0".repeat(zeroHexCharacters)) &&
      (remainingBits === 0 || Number.parseInt(digest[zeroHexCharacters], 16) < 2 ** (4 - remainingBits))
    ) {
      return `pow:${challengeId}:${nonce}`;
    }
  }
  throw new Error("test challenge was not solvable within the fixed bound");
}

test("normalizes a bounded Korean question", () => {
  assert.equal(normalizeQuestion("  수의계약\n 기준을  알려줘  "), "수의계약 기준을 알려줘");
  assert.equal(normalizeQuestion(""), null);
  assert.equal(normalizeQuestion("A".repeat(1001)), null);
  assert.equal(normalizeQuestion("hello\u0000world"), null);
});

test("creates a confidential issue then an exact hermes ask note", async () => {
  const calls = [];
  const runtime = env(async (url, init = {}) => {
    calls.push({ url: String(url), init });
    if (String(url).includes("turnstile")) {
      return Response.json({ success: true });
    }
    if (String(url).endsWith("/issues")) {
      return Response.json({ iid: 73 }, { status: 201 });
    }
    return Response.json({ id: 91 }, { status: 201 });
  });

  const response = await worker.fetch(
    postRequest({ question: "수의계약 기준을 알려줘", turnstile_token: "ok" }),
    runtime,
  );
  const body = await response.json();

  assert.equal(response.status, 202);
  assert.equal(body.status, "pending");
  assert.match(body.request_id, /^senqa-[0-9a-f]{32}$/);
  assert.ok(body.poll_token.length >= 40);
  assert.equal(calls.length, 3);
  const issuePayload = JSON.parse(calls[1].init.body);
  assert.equal(issuePayload.confidential, true);
  assert.equal(issuePayload.title, `[SEN-QA] ${body.request_id}`);
  const notePayload = JSON.parse(calls[2].init.body);
  assert.equal(
    notePayload.body,
    `/hermes ask ${body.request_id}\n수의계약 기준을 알려줘`,
  );
  assert.equal(calls[1].init.headers["PRIVATE-TOKEN"], "gitlab-secret");
  const stored = JSON.parse(await runtime.QUESTIONS.get(`question:${body.request_id}`));
  assert.equal(stored.issue_iid, 73);
  assert.notEqual(stored.poll_token_sha256, body.poll_token);
});

test("first-party challenge queues a question when Turnstile is blocked", async () => {
  const calls = [];
  const runtime = env(async (url, init = {}) => {
    calls.push({ url: String(url), init });
    if (String(url).endsWith("/issues")) {
      return Response.json({ iid: 74 }, { status: 201 });
    }
    return Response.json({ id: 92 }, { status: 201 });
  });
  const challengeResponse = await worker.fetch(
    new Request(`${ORIGIN}/api/challenge`, { headers: { origin: ORIGIN } }),
    runtime,
  );
  const challenge = await challengeResponse.json();
  assert.equal(challengeResponse.status, 200);
  const proof = solveChallenge(challenge.challenge_id, challenge.difficulty_bits);

  const response = await worker.fetch(
    postRequest({ question: "출장비 기준을 알려줘", turnstile_token: proof }),
    runtime,
  );
  const body = await response.json();

  assert.equal(response.status, 202);
  assert.equal(body.status, "pending");
  assert.equal(calls.some((call) => call.url.includes("turnstile")), false);
  const replay = await worker.fetch(
    postRequest({ question: "같은 증명을 재사용", turnstile_token: proof }),
    runtime,
  );
  assert.equal(replay.status, 403);
});

test("limits first-party challenge issuance per client and minute", async () => {
  const runtime = env(async () => {
    throw new Error("must not call network");
  });
  const statuses = [];
  for (let index = 0; index < 11; index += 1) {
    const response = await worker.fetch(
      new Request(`${ORIGIN}/api/challenge`, {
        headers: { "cf-connecting-ip": "192.0.2.10", origin: ORIGIN },
      }),
      runtime,
    );
    statuses.push(response.status);
  }

  assert.deepEqual(statuses, [...Array(10).fill(200), 429]);
});

const PUBLIC_CASE = {
  answer: "관련 기준을 확인합니다.",
  case_id: "senqa-2022-case-a",
  edition_year: 2022,
  pdf_pages: [4],
  question: "수의계약이 가능한가요?",
  title: "계약 사례",
};

function answerNote(requestId, overrides = {}) {
  const payload = {
    answer: "계약 기준입니다. [2022년 · PDF 4쪽]",
    answer_kind: "grounded",
    cases: [PUBLIC_CASE],
    schema_version: "senqa-public-answer/v1",
    ...overrides,
  };
  return `<!-- senqa-answer:v2 request_id=${requestId} -->\n${JSON.stringify(payload)}\n`;
}

test("poll returns only the matching structured public answer", async () => {
  const rankedCases = Array.from({ length: 20 }, (_, index) => ({
    ...PUBLIC_CASE,
    case_id: `senqa-2022-case-${index}`,
  }));
  const runtime = env(async () =>
    Response.json([
      { body: "ordinary comment" },
      {
        body: answerNote("senqa-07070707070707070707070707070707", { cases: rankedCases }),
      },
    ]),
  );
  const createResponse = await worker.fetch(
    postRequest({ question: "질문", turnstile_token: "ok" }),
    {
      ...runtime,
      FETCH: async (url) => {
        if (String(url).includes("turnstile")) return Response.json({ success: true });
        if (String(url).endsWith("/issues")) return Response.json({ iid: 73 }, { status: 201 });
        return Response.json({ id: 91 }, { status: 201 });
      },
    },
  );
  const created = await createResponse.json();
  const poll = new Request(
    `${ORIGIN}/api/questions/${created.request_id}?token=${encodeURIComponent(created.poll_token)}`,
    { headers: { origin: ORIGIN } },
  );
  const response = await worker.fetch(poll, runtime);
  const body = await response.json();

  assert.equal(response.status, 200);
  assert.equal(body.status, "complete");
  assert.equal(body.answer, "계약 기준입니다. [2022년 · PDF 4쪽]");
  assert.equal(body.answer_kind, "grounded");
  assert.deepEqual(body.cases, rankedCases);
  assert.equal(JSON.stringify(body).includes("issue_iid"), false);
  assert.equal(JSON.stringify(body).includes("production_eligible"), false);
});

test("poll returns the fixed temporary-unavailability answer without cases", async () => {
  const requestId = "senqa-0123456789abcdef0123456789abcdef";
  const pollToken = "A".repeat(43);
  const runtime = env(async () =>
    Response.json([
      {
        body: answerNote(requestId, {
          answer:
            "서버 부하로 약간의 대기 시간이 필요합니다. 잠시 후 다시 시도해 주세요.",
          answer_kind: "temporarily_unavailable",
          cases: [],
        }),
      },
    ]),
  );
  await runtime.QUESTIONS.put(
    `question:${requestId}`,
    JSON.stringify({
      issue_iid: 73,
      poll_token_sha256: createHash("sha256").update(pollToken).digest("hex"),
    }),
  );
  const response = await worker.fetch(
    new Request(`${ORIGIN}/api/questions/${requestId}?token=${pollToken}`, {
      headers: { origin: ORIGIN },
    }),
    runtime,
  );
  const body = await response.json();

  assert.equal(response.status, 200);
  assert.equal(body.status, "complete");
  assert.equal(body.answer_kind, "temporarily_unavailable");
  assert.deepEqual(body.cases, []);
});

test("poll rejects a structured answer with internal or extra fields", async () => {
  const requestId = "senqa-0123456789abcdef0123456789abcdef";
  const pollToken = "A".repeat(43);
  const runtime = env(async () =>
    Response.json([{ body: answerNote(requestId, { warning_code: "hidden" }) }]),
  );
  await runtime.QUESTIONS.put(
    `question:${requestId}`,
    JSON.stringify({
      issue_iid: 73,
      poll_token_sha256: createHash("sha256").update(pollToken).digest("hex"),
    }),
  );

  const response = await worker.fetch(
    new Request(`${ORIGIN}/api/questions/${requestId}?token=${pollToken}`, {
      headers: { origin: ORIGIN },
    }),
    runtime,
  );

  assert.equal(response.status, 503);
  assert.deepEqual(await response.json(), { error_code: "answer_invalid" });
});

test("legacy completion never returns its internal banner", async () => {
  const requestId = "senqa-0123456789abcdef0123456789abcdef";
  const pollToken = "B".repeat(43);
  const runtime = env(async () =>
    Response.json([
      {
        body:
          `<!-- senqa-answer:v1 request_id=${requestId} -->\n` +
          "> **미검수 프리뷰** · `production_eligible=false`",
      },
    ]),
  );
  await runtime.QUESTIONS.put(
    `question:${requestId}`,
    JSON.stringify({
      issue_iid: 73,
      poll_token_sha256: createHash("sha256").update(pollToken).digest("hex"),
    }),
  );

  const response = await worker.fetch(
    new Request(`${ORIGIN}/api/questions/${requestId}?token=${pollToken}`, {
      headers: { origin: ORIGIN },
    }),
    runtime,
  );
  const body = await response.json();

  assert.equal(response.status, 200);
  assert.equal(body.answer_kind, "cases_only");
  assert.deepEqual(body.cases, []);
  assert.equal(JSON.stringify(body).includes("production_eligible"), false);
});

test("rejects cross-origin, missing turnstile, and invalid poll token", async () => {
  const runtime = env(async () => Response.json({ success: false }));
  const crossOrigin = await worker.fetch(
    postRequest({ question: "질문", turnstile_token: "x" }, "https://evil.test"),
    runtime,
  );
  const turnstile = await worker.fetch(
    postRequest({ question: "질문", turnstile_token: "x" }),
    runtime,
  );

  assert.equal(crossOrigin.status, 403);
  assert.equal(turnstile.status, 403);

  await runtime.QUESTIONS.put(
    "question:senqa-0123456789abcdef0123456789abcdef",
    JSON.stringify({ issue_iid: 1, poll_token_sha256: "0".repeat(64) }),
  );
  const invalidPoll = await worker.fetch(
    new Request(
      `${ORIGIN}/api/questions/senqa-0123456789abcdef0123456789abcdef?token=wrong`,
      { headers: { origin: ORIGIN } },
    ),
    runtime,
  );
  assert.equal(invalidPoll.status, 404);
});

test("oversized and malformed JSON fail with fixed errors", async () => {
  const runtime = env(async () => {
    throw new Error("must not call network");
  });
  const oversized = new Request(`${ORIGIN}/api/questions`, {
    method: "POST",
    headers: { "content-type": "application/json", origin: ORIGIN },
    body: "A".repeat(16_385),
  });
  const malformed = new Request(`${ORIGIN}/api/questions`, {
    method: "POST",
    headers: { "content-type": "application/json", origin: ORIGIN },
    body: "PRIVATE_JSON_SENTINEL",
  });

  for (const request of [oversized, malformed]) {
    const response = await worker.fetch(request, runtime);
    const text = await response.text();
    assert.equal(response.status, 400);
    assert.equal(text.includes("PRIVATE_JSON_SENTINEL"), false);
  }
});
