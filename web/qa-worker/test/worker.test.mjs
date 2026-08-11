import assert from "node:assert/strict";
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

test("poll returns only the matching machine-marked answer", async () => {
  const runtime = env(async () =>
    Response.json([
      { body: "ordinary comment" },
      {
        body:
          "<!-- senqa-answer:v1 request_id=senqa-07070707070707070707070707070707 -->\n" +
          "> **미검수 프리뷰** · `production_eligible=false`\n\n근거 답변",
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
  assert.equal(body.answer, "> **미검수 프리뷰** · `production_eligible=false`\n\n근거 답변");
  assert.equal(JSON.stringify(body).includes("issue_iid"), false);
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
