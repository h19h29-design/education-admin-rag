import { solveFirstPartyChallenge } from "./challenge.js";
import {
  expirePendingHistory,
  historyStatusLabel,
  normalizeCompletion,
  normalizeHistory,
  PENDING_TIMEOUT_MS,
  publicSourceLabel,
  resolveTheme,
} from "./view-model.js";

const STORAGE_KEY = "senqa-preview-questions-v2";
const THEME_KEY = "senqa-theme-v1";
const POLL_INTERVAL_MS = 3_000;
const MAX_CONSECUTIVE_POLL_FAILURES = 5;

const elements = {
  answerCaseCount: document.querySelector("#answer-case-count"),
  answerPanel: document.querySelector("#answer-panel"),
  answerText: document.querySelector("#answer-text"),
  characterCount: document.querySelector("#character-count"),
  clearHistory: document.querySelector("#clear-history"),
  form: document.querySelector("#question-form"),
  formError: document.querySelector("#form-error"),
  historyClose: document.querySelector("#history-close"),
  historyDialog: document.querySelector("#history-dialog"),
  historyEmpty: document.querySelector("#history-empty"),
  historyList: document.querySelector("#history-list"),
  historyOpen: document.querySelector("#history-open"),
  progressPanel: document.querySelector("#progress-panel"),
  question: document.querySelector("#question"),
  relatedCases: document.querySelector("#related-cases"),
  relatedSection: document.querySelector("#related-section"),
  reviewWarning: document.querySelector("#review-warning"),
  submitButton: document.querySelector("#submit-button"),
  themeButtons: [...document.querySelectorAll("[data-theme-choice]")],
  turnstile: document.querySelector("#turnstile"),
};

let turnstileWidget = null;
let turnstileToken = "";
let turnstileSiteKey = "";
let usingFirstPartyChallenge = false;
let selectedRequestId = "";
let currentTheme = resolveTheme(
  safeStorageGet(THEME_KEY),
  window.matchMedia("(prefers-color-scheme: dark)").matches,
);

function safeStorageGet(key) {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function safeStorageSet(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch {
    return;
  }
}

function safeStorageRemove(key) {
  try {
    localStorage.removeItem(key);
  } catch {
    return;
  }
}

function readHistory() {
  try {
    return expirePendingHistory(JSON.parse(safeStorageGet(STORAGE_KEY) ?? "[]"), Date.now());
  } catch {
    return [];
  }
}

function writeHistory(items) {
  safeStorageSet(STORAGE_KEY, JSON.stringify(normalizeHistory(items)));
}

function formatHistoryDate(createdAt) {
  const value = new Date(createdAt);
  if (Number.isNaN(value.getTime())) return "";
  return new Intl.DateTimeFormat("ko-KR", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(value);
}

function closeHistory() {
  if (elements.historyDialog.open) elements.historyDialog.close();
}

function renderHistory() {
  const items = readHistory();
  writeHistory(items);
  elements.historyList.replaceChildren();
  elements.historyEmpty.hidden = items.length > 0;
  for (const item of items) {
    const row = document.createElement("li");
    row.className = "history-item";
    const button = document.createElement("button");
    button.type = "button";
    button.setAttribute("aria-current", String(item.requestId === selectedRequestId));
    const question = document.createElement("span");
    question.className = "history-question";
    question.textContent = item.question;
    const meta = document.createElement("span");
    meta.className = "history-meta";
    const status = document.createElement("span");
    status.textContent = historyStatusLabel(item.status);
    const date = document.createElement("time");
    date.dateTime = new Date(item.createdAt).toISOString();
    date.textContent = formatHistoryDate(item.createdAt);
    meta.append(status, date);
    button.append(question, meta);
    button.addEventListener("click", () => restoreQuestion(item));
    row.append(button);
    elements.historyList.append(row);
  }
}

function renderRelatedCases(cases) {
  elements.relatedCases.replaceChildren();
  for (const [index, item] of cases.entries()) {
    const card = document.createElement("details");
    card.className = "case-card";
    const summary = document.createElement("summary");
    const number = document.createElement("span");
    number.className = "case-index";
    number.textContent = String(index + 1).padStart(2, "0");
    const copy = document.createElement("span");
    copy.className = "case-summary-copy";
    const title = document.createElement("span");
    title.className = "case-title";
    title.textContent = item.title;
    const preview = document.createElement("span");
    preview.className = "case-preview";
    preview.textContent = item.question;
    copy.append(title, preview);
    const meta = document.createElement("span");
    meta.className = "case-meta";
    meta.textContent = publicSourceLabel(item);
    const chevron = document.createElement("span");
    chevron.className = "case-chevron";
    chevron.setAttribute("aria-hidden", "true");
    summary.append(number, copy, meta, chevron);

    const detail = document.createElement("div");
    detail.className = "case-detail";
    for (const [heading, content] of [
      ["질의", item.question],
      ["답변", item.answer],
    ]) {
      const section = document.createElement("section");
      const label = document.createElement("h4");
      label.textContent = heading;
      const paragraph = document.createElement("p");
      paragraph.textContent = content;
      section.append(label, paragraph);
      detail.append(section);
    }
    card.append(summary, detail);
    elements.relatedCases.append(card);
  }
}

function showResult(result) {
  elements.progressPanel.hidden = true;
  elements.answerPanel.hidden = false;
  elements.answerText.textContent = result.answer;
  const hasCases = result.cases.length > 0;
  elements.answerCaseCount.textContent = hasCases ? `관련 사례 ${result.cases.length}건` : "";
  elements.reviewWarning.hidden = !hasCases;
  elements.relatedSection.hidden = !hasCases;
  renderRelatedCases(result.cases);
}

function showPending() {
  elements.answerPanel.hidden = true;
  elements.progressPanel.hidden = false;
}

function showIdle() {
  elements.answerPanel.hidden = true;
  elements.progressPanel.hidden = true;
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

function markHistoryForRetry(item) {
  const items = readHistory().map((entry) => {
    if (entry.requestId !== item.requestId) return entry;
    return {
      createdAt: entry.createdAt,
      question: entry.question,
      requestId: entry.requestId,
      status: "retry",
    };
  });
  writeHistory(items);
  renderHistory();
}

function hasExpired(item) {
  return Date.now() - item.createdAt >= PENDING_TIMEOUT_MS;
}

async function poll(item, failedAttempts = 0) {
  if (item.status !== "pending") return;
  if (hasExpired(item)) {
    markHistoryForRetry(item);
    showIdle();
    showError("답변이 지연되었습니다. 같은 질문으로 다시 검색해 주세요.");
    return;
  }
  try {
    const value = await api(
      `/api/questions/${item.requestId}?token=${encodeURIComponent(item.pollToken)}`,
    );
    if (value.status === "complete") {
      if (hasExpired(item)) {
        markHistoryForRetry(item);
        showIdle();
        showError("답변이 지연되었습니다. 같은 질문으로 다시 검색해 주세요.");
        return;
      }
      const result = normalizeCompletion(value);
      if (!result) throw new Error("response_invalid");
      updateHistory(item.requestId, { result, status: "complete" });
      showResult(result);
      return;
    }
  } catch {
    if (failedAttempts + 1 >= MAX_CONSECUTIVE_POLL_FAILURES) {
      markHistoryForRetry(item);
      showIdle();
      showError("답변이 지연되었습니다. 같은 질문으로 다시 검색해 주세요.");
      return;
    }
    window.setTimeout(() => poll(item, failedAttempts + 1), POLL_INTERVAL_MS);
    return;
  }
  window.setTimeout(() => poll(item, failedAttempts), POLL_INTERVAL_MS);
}

function restoreQuestion(item) {
  clearError();
  selectedRequestId = item.requestId;
  elements.question.value = item.question;
  updateCount();
  renderHistory();
  closeHistory();
  if (item.status === "complete" && item.result) {
    showResult(item.result);
    return;
  }
  showIdle();
  elements.question.focus();
  if (item.status === "pending") poll(item);
}

function updateCount() {
  elements.characterCount.textContent = `${elements.question.value.length.toLocaleString("ko-KR")} / 1,000`;
  elements.submitButton.disabled = !elements.question.value.trim() || !turnstileToken;
}

function renderTurnstile() {
  if (!window.turnstile || !turnstileSiteKey) return;
  if (turnstileWidget !== null) window.turnstile.remove(turnstileWidget);
  elements.turnstile.replaceChildren();
  turnstileToken = "";
  turnstileWidget = window.turnstile.render(elements.turnstile, {
    sitekey: turnstileSiteKey,
    callback(token) {
      turnstileToken = token;
      updateCount();
    },
    "expired-callback"() {
      turnstileToken = "";
      updateCount();
    },
    theme: currentTheme,
  });
  updateCount();
}

function applyTheme(theme, { persist = true, rerenderChallenge = true } = {}) {
  currentTheme = resolveTheme(theme, false);
  document.documentElement.dataset.theme = currentTheme;
  document.documentElement.style.colorScheme = currentTheme;
  for (const button of elements.themeButtons) {
    button.setAttribute("aria-pressed", String(button.dataset.themeChoice === currentTheme));
  }
  if (persist) safeStorageSet(THEME_KEY, currentTheme);
  if (rerenderChallenge && window.turnstile && turnstileSiteKey) renderTurnstile();
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
            resolve();
          } else if (attempts > 40) {
            window.clearInterval(timer);
            reject(new Error("challenge_unavailable"));
          }
        }, 50);
      });
    await waitForTurnstile();
    turnstileSiteKey = config.turnstile_site_key;
    renderTurnstile();
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
  safeStorageRemove(STORAGE_KEY);
  selectedRequestId = "";
  renderHistory();
});
elements.historyOpen.addEventListener("click", () => elements.historyDialog.showModal());
elements.historyClose.addEventListener("click", closeHistory);
elements.historyDialog.addEventListener("click", (event) => {
  if (event.target === elements.historyDialog) closeHistory();
});

for (const button of elements.themeButtons) {
  button.addEventListener("click", () => applyTheme(button.dataset.themeChoice));
}

for (const button of document.querySelectorAll("[data-suggestion]")) {
  button.addEventListener("click", () => {
    elements.question.value = button.dataset.suggestion;
    elements.question.focus();
    updateCount();
  });
}

elements.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  clearError();
  const question = elements.question.value.trim().replace(/\s+/gu, " ");
  if (!question || question.length > 1_000 || !turnstileToken) {
    showError("질문과 접속 확인을 완료해 주세요.");
    return;
  }
  elements.submitButton.disabled = true;
  showPending();
  try {
    const value = await api("/api/questions", {
      method: "POST",
      body: JSON.stringify({ question, turnstile_token: turnstileToken }),
    });
    const item = {
      createdAt: Date.now(),
      pollToken: value.poll_token,
      question,
      requestId: value.request_id,
      status: "pending",
    };
    selectedRequestId = item.requestId;
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

applyTheme(currentTheme, { persist: false, rerenderChallenge: false });
renderHistory();
updateCount();
initializeTurnstile();
