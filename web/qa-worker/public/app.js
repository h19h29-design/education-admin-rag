import { solveFirstPartyChallenge } from "./challenge.js";

const STORAGE_KEY = "senqa-preview-questions-v1";
const POLL_INTERVAL_MS = 3_000;
const MAX_POLL_ATTEMPTS = 100;

const elements = {
  answerPanel: document.querySelector("#answer-panel"),
  answerText: document.querySelector("#answer-text"),
  characterCount: document.querySelector("#character-count"),
  clearHistory: document.querySelector("#clear-history"),
  form: document.querySelector("#question-form"),
  formError: document.querySelector("#form-error"),
  historyEmpty: document.querySelector("#history-empty"),
  historyList: document.querySelector("#history-list"),
  progressPanel: document.querySelector("#progress-panel"),
  question: document.querySelector("#question"),
  sourceList: document.querySelector("#source-list"),
  submitButton: document.querySelector("#submit-button"),
  turnstile: document.querySelector("#turnstile"),
};

let turnstileWidget = null;
let turnstileToken = "";
let usingFirstPartyChallenge = false;

function readHistory() {
  try {
    const parsed = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "[]");
    return Array.isArray(parsed) ? parsed.slice(0, 12) : [];
  } catch {
    return [];
  }
}

function writeHistory(items) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(items.slice(0, 12)));
}

function statusLabel(status) {
  return status === "complete" ? "답변 완료" : "답변 준비 중";
}

function renderHistory() {
  const items = readHistory();
  elements.historyList.replaceChildren();
  elements.historyEmpty.hidden = items.length > 0;
  for (const item of items) {
    const row = document.createElement("li");
    row.className = "history-item";
    const button = document.createElement("button");
    button.type = "button";
    const question = document.createElement("span");
    question.className = "history-question";
    question.textContent = item.question;
    const meta = document.createElement("span");
    meta.className = "history-meta";
    const status = document.createElement("span");
    status.textContent = statusLabel(item.status);
    const date = document.createElement("time");
    date.dateTime = new Date(item.createdAt).toISOString();
    date.textContent = new Intl.DateTimeFormat("ko-KR", {
      month: "numeric",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    }).format(new Date(item.createdAt));
    meta.append(status, date);
    button.append(question, meta);
    button.addEventListener("click", () => restoreQuestion(item));
    row.append(button);
    elements.historyList.append(row);
  }
}

function sourcesFromAnswer(answer) {
  const sources = new Set();
  const pattern = /(senqa-20(?:20|21|22|23|24|25)-[a-z0-9-]+)[^\n]{0,100}?(20(?:20|21|22|23|24|25))[^\n]{0,100}?(?:PDF|pdf|p\.)\s*([0-9,\- ]+)/gu;
  for (const match of answer.matchAll(pattern)) {
    sources.add(`${match[1]} · ${match[2]}년 · PDF ${match[3].trim()}쪽`);
  }
  return [...sources];
}

function showAnswer(answer) {
  elements.progressPanel.hidden = true;
  elements.answerPanel.hidden = false;
  elements.answerText.textContent = answer;
  elements.sourceList.replaceChildren();
  const sources = sourcesFromAnswer(answer);
  if (sources.length === 0) {
    const item = document.createElement("li");
    item.textContent = "답변 본문의 사례 ID, 연도, PDF 페이지를 함께 확인하세요.";
    elements.sourceList.append(item);
    return;
  }
  for (const source of sources) {
    const item = document.createElement("li");
    item.textContent = source;
    elements.sourceList.append(item);
  }
}

function showPending() {
  elements.answerPanel.hidden = true;
  elements.progressPanel.hidden = false;
}

function showError(message) {
  elements.formError.textContent = message;
  elements.formError.hidden = false;
}

function clearError() {
  elements.formError.textContent = "";
  elements.formError.hidden = true;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "content-type": "application/json", ...(options.headers ?? {}) },
  });
  const body = await response.json().catch(() => ({ error_code: "response_invalid" }));
  if (!response.ok) throw new Error(body.error_code ?? "request_failed");
  return body;
}

function updateHistory(requestId, changes) {
  const items = readHistory();
  const index = items.findIndex((item) => item.requestId === requestId);
  if (index >= 0) items[index] = { ...items[index], ...changes };
  writeHistory(items);
  renderHistory();
}

async function poll(item, attempt = 0) {
  if (attempt >= MAX_POLL_ATTEMPTS) {
    showError("답변 대기 시간이 길어지고 있습니다. 최근 질문에서 다시 확인해 주세요.");
    return;
  }
  try {
    const result = await api(
      `/api/questions/${item.requestId}?token=${encodeURIComponent(item.pollToken)}`,
    );
    if (result.status === "complete") {
      updateHistory(item.requestId, { answer: result.answer, status: "complete" });
      showAnswer(result.answer);
      return;
    }
  } catch {
    if (attempt > 4) {
      showError("답변 상태를 확인하지 못했습니다. 잠시 후 다시 시도해 주세요.");
      return;
    }
  }
  window.setTimeout(() => poll(item, attempt + 1), POLL_INTERVAL_MS);
}

function restoreQuestion(item) {
  clearError();
  elements.question.value = item.question;
  updateCount();
  if (item.status === "complete" && item.answer) {
    showAnswer(item.answer);
  } else {
    showPending();
    poll(item);
  }
}

function updateCount() {
  elements.characterCount.textContent = `${elements.question.value.length.toLocaleString("ko-KR")} / 1,000`;
  elements.submitButton.disabled = !elements.question.value.trim() || !turnstileToken;
}

async function initializeTurnstile() {
  try {
    const config = await api("/api/config", { headers: {} });
    const waitForTurnstile = () =>
      new Promise((resolve, reject) => {
        let attempts = 0;
        const timer = window.setInterval(() => {
          attempts += 1;
          if (window.turnstile) {
            window.clearInterval(timer);
            resolve(window.turnstile);
          } else if (attempts > 40) {
            window.clearInterval(timer);
            reject(new Error("turnstile_unavailable"));
          }
        }, 50);
      });
    const turnstile = await waitForTurnstile();
    turnstileWidget = turnstile.render(elements.turnstile, {
      sitekey: config.turnstile_site_key,
      callback(token) {
        turnstileToken = token;
        updateCount();
      },
      "expired-callback"() {
        turnstileToken = "";
        updateCount();
      },
      theme: "light",
    });
  } catch {
    try {
      elements.turnstile.textContent = "접속 확인 중…";
      const challenge = await api("/api/challenge", { headers: {} });
      turnstileToken = await solveFirstPartyChallenge(challenge);
      usingFirstPartyChallenge = true;
      elements.turnstile.textContent = "접속 확인 완료";
      clearError();
      updateCount();
    } catch {
      showError("접속 확인을 완료하지 못했습니다. 잠시 후 새로고침해 주세요.");
    }
  }
}

elements.question.addEventListener("input", updateCount);
elements.clearHistory.addEventListener("click", () => {
  localStorage.removeItem(STORAGE_KEY);
  renderHistory();
});

elements.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  clearError();
  const question = elements.question.value.trim().replace(/\s+/gu, " ");
  if (!question || question.length > 1000 || !turnstileToken) {
    showError("질문과 로봇 확인을 완료해 주세요.");
    return;
  }
  elements.submitButton.disabled = true;
  showPending();
  try {
    const result = await api("/api/questions", {
      method: "POST",
      body: JSON.stringify({ question, turnstile_token: turnstileToken }),
    });
    const item = {
      answer: "",
      createdAt: Date.now(),
      pollToken: result.poll_token,
      question,
      requestId: result.request_id,
      status: "pending",
    };
    writeHistory([item, ...readHistory()]);
    renderHistory();
    poll(item);
  } catch {
    elements.progressPanel.hidden = true;
    showError("질문을 접수하지 못했습니다. 잠시 후 다시 시도해 주세요.");
  } finally {
    turnstileToken = "";
    if (window.turnstile && turnstileWidget !== null) {
      window.turnstile.reset(turnstileWidget);
    } else if (usingFirstPartyChallenge) {
      usingFirstPartyChallenge = false;
      initializeTurnstile();
    }
    updateCount();
  }
});

renderHistory();
updateCount();
initializeTurnstile();
