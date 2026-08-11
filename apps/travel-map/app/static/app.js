import { api, ApiError } from "./api.js";
import { KakaoMapController } from "./kakao-map.js";

const $ = (selector) => document.querySelector(selector);
const controls = {
  form: $("#trip-form"),
  origin: $("#origin-search"),
  destination: $("#destination-search"),
  originResults: $("#origin-results"),
  destinationResults: $("#destination-results"),
  originNote: $("#origin-selection"),
  destinationNote: $("#destination-selection"),
  policy: $("#policy-profile"),
  formError: $("#form-error"),
  results: $("#results"),
  routeList: $("#route-list"),
  routeCount: $("#route-count"),
  mapCollapse: $("#map-collapse"),
  filtersToggle: $("#institution-filters-toggle"),
};

const state = {
  origin: null,
  destination: null,
  preview: null,
  activeRouteId: null,
  sort: "time",
  activeSuggestions: { origin: -1, destination: -1 },
};

const map = new KakaoMapController($("#map"), $("#map-status"), reverseDestination);
const modeName = { TRANSIT: "대중교통", CAR: "자동차", WALK: "도보" };
const bestName = {
  fastestRouteId: "최단시간",
  shortestRouteId: "최단거리",
  cheapestRouteId: "최저비용",
};

function formatMoney(value) {
  return value == null ? "비용 정보 없음" : `${new Intl.NumberFormat("ko-KR").format(value)}원`;
}

function formatDistance(value) {
  return value >= 1000 ? `${(value / 1000).toFixed(1)}km` : `${value}m`;
}

function formatDuration(seconds) {
  const minutes = Math.round(seconds / 60);
  return minutes >= 60 ? `${Math.floor(minutes / 60)}시간 ${minutes % 60}분` : `${minutes}분`;
}

function errorMessage(error) {
  if (error instanceof ApiError) {
    return error.message === "PLACE_PROVIDER_UNAVAILABLE"
      ? "출장지 검색 서비스를 잠시 이용할 수 없습니다."
      : "요청을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요.";
  }
  return "네트워크 연결을 확인한 뒤 다시 시도해 주세요.";
}

function setFormError(message = "") {
  controls.formError.textContent = message;
  controls.formError.hidden = !message;
}

function setDefaultDates() {
  const date = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Seoul",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date());
  $("#starts-date").value = date;
  $("#returns-date").value = date;
}

function optionText(item, kind) {
  return kind === "origin"
    ? `${item.siteName} · ${item.institutionType} · ${item.foundationType} · ${item.district} · ${item.roadAddress}`
    : `${item.name} · ${item.roadAddress || item.lotAddress}`;
}

function hideSuggestions(kind) {
  const input = kind === "origin" ? controls.origin : controls.destination;
  const list = kind === "origin" ? controls.originResults : controls.destinationResults;
  list.hidden = true;
  list.replaceChildren();
  input.setAttribute("aria-expanded", "false");
  input.removeAttribute("aria-activedescendant");
  state.activeSuggestions[kind] = -1;
}

function renderSuggestions(kind, items) {
  const input = kind === "origin" ? controls.origin : controls.destination;
  const list = kind === "origin" ? controls.originResults : controls.destinationResults;
  list.replaceChildren();
  state.activeSuggestions[kind] = -1;
  if (!items.length) {
    hideSuggestions(kind);
    return;
  }
  items.forEach((item, index) => {
    const option = document.createElement("li");
    option.id = `${kind}-option-${index}`;
    option.setAttribute("role", "option");
    option.tabIndex = -1;
    option.dataset.index = String(index);
    option.textContent = optionText(item, kind);
    option.addEventListener("mousedown", (event) => event.preventDefault());
    option.addEventListener("click", () => selectSuggestion(kind, item));
    list.append(option);
  });
  list.hidden = false;
  input.setAttribute("aria-expanded", "true");
}

function selectSuggestion(kind, item) {
  if (kind === "origin") {
    state.origin = item;
    controls.origin.value = item.siteName;
    controls.originNote.textContent = `${item.institutionType} · ${item.foundationType} · ${item.district} · ${item.roadAddress}`;
  } else {
    state.destination = item;
    controls.destination.value = item.name;
    controls.destinationNote.textContent = item.roadAddress || item.lotAddress;
  }
  hideSuggestions(kind);
  setFormError();
}

function moveSuggestion(kind, increment) {
  const list = kind === "origin" ? controls.originResults : controls.destinationResults;
  const input = kind === "origin" ? controls.origin : controls.destination;
  const options = [...list.querySelectorAll('[role="option"]')];
  if (!options.length) return;
  const index = (state.activeSuggestions[kind] + increment + options.length) % options.length;
  state.activeSuggestions[kind] = index;
  options.forEach((option, itemIndex) => option.setAttribute("aria-selected", String(itemIndex === index)));
  input.setAttribute("aria-activedescendant", options[index].id);
  options[index].scrollIntoView({ block: "nearest" });
}

function bindCombobox(kind, getItems) {
  const input = kind === "origin" ? controls.origin : controls.destination;
  let requestId = 0;
  input.addEventListener("input", async () => {
    const query = input.value.trim();
    if (kind === "origin") state.origin = null;
    else state.destination = null;
    if (query.length < 2) return hideSuggestions(kind);
    const currentRequest = ++requestId;
    try {
      const items = await getItems(query);
      if (currentRequest === requestId) renderSuggestions(kind, items);
    } catch (error) {
      if (currentRequest === requestId) setFormError(errorMessage(error));
    }
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      moveSuggestion(kind, 1);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      moveSuggestion(kind, -1);
    } else if (event.key === "Enter" && state.activeSuggestions[kind] >= 0) {
      event.preventDefault();
      const list = kind === "origin" ? controls.originResults : controls.destinationResults;
      list.querySelectorAll('[role="option"]')[state.activeSuggestions[kind]]?.click();
    } else if (event.key === "Escape") {
      hideSuggestions(kind);
    }
  });
  input.addEventListener("blur", () => window.setTimeout(() => hideSuggestions(kind), 120));
}

function originFilters() {
  return {
    institutionType: $("#institution-type").value,
    foundationType: $("#foundation-type").value,
    educationOffice: $("#education-office").value,
    district: $("#district").value,
  };
}

async function reverseDestination(point) {
  try {
    const response = await api.reversePlace(point);
    if (!response.item) {
      setFormError("선택한 위치의 주소를 확인할 수 없습니다.");
      return;
    }
    controls.destination.value = response.item.name;
    renderSuggestions("destination", [response.item]);
    controls.destinationNote.textContent = "지도에서 선택한 주소입니다. 목록에서 확인해 선택하세요.";
    controls.destination.focus();
  } catch (error) {
    setFormError(errorMessage(error));
  }
}

function requestPayload() {
  return {
    originSiteId: state.origin.siteId,
    destination: {
      name: state.destination.name,
      address: state.destination.roadAddress || state.destination.lotAddress,
      latitude: state.destination.latitude,
      longitude: state.destination.longitude,
    },
    startsAt: `${$("#starts-date").value}T${$("#starts-time").value}:00+09:00`,
    returnsAt: `${$("#returns-date").value}T${$("#returns-time").value}:00+09:00`,
    policyProfile: controls.policy.value,
    vehicleUse: $("#vehicle-use").value,
    carAssumptions: {
      fuelType: $("#fuel-type").value,
      efficiencyKmPerLiter: Number($("#efficiency").value),
      parkingCostKrw: Number($("#parking-cost").value),
    },
    hasOtherLocalTripsToday: $("#other-trips").checked,
    previousAllowanceKrw: 0,
  };
}

function sortRoutes(routes) {
  const keys = {
    time: (route) => [route.durationSeconds, route.distanceMeters],
    distance: (route) => [route.distanceMeters, route.durationSeconds],
    cost: (route) => [route.mobilityCostKrw == null ? Infinity : route.mobilityCostKrw, route.durationSeconds],
  };
  return [...routes].sort((first, second) => {
    const [firstPrimary, firstSecondary] = keys[state.sort](first);
    const [secondPrimary, secondSecondary] = keys[state.sort](second);
    return firstPrimary - secondPrimary || firstSecondary - secondSecondary;
  });
}

function badgesFor(route) {
  return Object.entries(bestName)
    .filter(([key]) => state.preview.best[key] === route.id)
    .map(([, label]) => `<span class="route-badge">${label}</span>`)
    .join("");
}

function warningText(warning) {
  return {
    PARKING_COST_ESTIMATED: "주차비는 예상값입니다",
    DATA_UNAVAILABLE: "경로 일부 정보 확인 필요",
  }[warning] || "경로 정보 일부를 확인해 주세요";
}

function renderRoutes() {
  const routes = sortRoutes(state.preview.routes);
  controls.routeCount.textContent = `${routes.length}개`;
  controls.routeList.replaceChildren();
  routes.forEach((route) => {
    const card = document.createElement("button");
    const selected = route.id === state.activeRouteId;
    card.type = "button";
    card.className = `route-card mode-${route.mode.toLowerCase()}`;
    card.dataset.routeId = route.id;
    card.setAttribute("aria-current", String(selected));
    card.innerHTML = `
      <span class="route-top"><strong>${modeName[route.mode]} · ${formatDuration(route.durationSeconds)}</strong><span class="route-badges">${badgesFor(route)}</span></span>
      <span class="route-metrics"><span>소요시간 <b>${formatDuration(route.durationSeconds)}</b></span><span>거리 <b>${formatDistance(route.distanceMeters)}</b></span><span>예상 이동비 <b>${formatMoney(route.mobilityCostKrw)}</b></span></span>
      ${route.warnings.length ? `<span class="route-warning">⚠ ${route.warnings.map(warningText).join(" · ")}</span>` : ""}
      <span class="route-source">${route.source} 기준</span>`;
    card.addEventListener("click", () => selectRoute(route.id));
    controls.routeList.append(card);
  });
}

function selectRoute(routeId) {
  state.activeRouteId = routeId;
  map.setActiveRoute(routeId);
  renderRoutes();
}

function renderSummary() {
  const { coverage, classification, classificationDistanceMeters, mobilityCost, allowance } = state.preview;
  $("#coverage-status").textContent = coverage.status === "SEOUL" ? "서울 경계 내" : coverage.status;
  $("#classification-result").textContent = classification === "LOCAL" ? "관내출장" : "판정 보류";
  $("#classification-distance").textContent = classificationDistanceMeters == null ? "분류 경로 확인 필요" : `분류 왕복 ${formatDistance(classificationDistanceMeters)}`;
  $("#mobility-cost").textContent = formatMoney(mobilityCost.amountKrw);
  $("#mobility-status").textContent = mobilityCost.status === "ESTIMATED" ? "예상값" : "경로 기준";
  const profileNeedsReview = controls.policy.value === "NONPUBLIC_OR_UNKNOWN";
  const allowanceNeedsReview = profileNeedsReview || allowance.status === "REVIEW_REQUIRED" || allowance.amountKrw == null;
  $("#allowance-amount").textContent = allowanceNeedsReview ? "여비 판정 보류" : formatMoney(allowance.amountKrw);
  $("#allowance-status").textContent = allowanceNeedsReview ? "신분·규정을 확인하세요" : "지급 확정액 아님";
}

function renderPreview() {
  state.activeRouteId = state.preview.best.fastestRouteId || state.preview.routes[0]?.id || null;
  controls.results.hidden = false;
  renderSummary();
  renderRoutes();
  map.showRoutes({
    origin: state.preview.origin,
    destination: state.destination,
    routes: state.preview.routes,
    classificationPath: state.preview.classificationPath,
  });
  if (state.activeRouteId) map.setActiveRoute(state.activeRouteId);
}

async function calculate(event) {
  event.preventDefault();
  if (!state.origin || !state.destination || !controls.policy.value) {
    setFormError("출발 기관, 출장지, 적용 규정을 모두 선택하세요.");
    return;
  }
  if (!controls.form.checkValidity()) {
    setFormError("출발·복귀 일시와 자동차 이용 가정을 확인하세요.");
    return;
  }
  const button = $("#calculate-button");
  button.disabled = true;
  button.textContent = "경로를 계산하고 있습니다";
  setFormError();
  try {
    state.preview = await api.preview(requestPayload());
    renderPreview();
  } catch (error) {
    setFormError(errorMessage(error));
  } finally {
    button.disabled = false;
    button.textContent = "▣ 경로 계산";
  }
}

function bindSortTabs() {
  document.querySelectorAll("[data-sort]").forEach((tab) => {
    tab.addEventListener("click", () => {
      state.sort = tab.dataset.sort;
      document.querySelectorAll("[data-sort]").forEach((item) => item.setAttribute("aria-selected", String(item === tab)));
      renderRoutes();
    });
  });
}

function bindMapControls() {
  [["#seoul-layer", "seoul"], ["#support-layer", "support"]].forEach(([selector, name]) => {
    $(selector).addEventListener("change", (event) => map.setBoundary(name, event.target.checked, api.geodata));
  });
  controls.mapCollapse.addEventListener("click", () => {
    const expanded = controls.mapCollapse.getAttribute("aria-expanded") !== "true";
    controls.mapCollapse.setAttribute("aria-expanded", String(expanded));
    controls.mapCollapse.textContent = expanded ? "지도 접기" : "지도 펼치기";
    $(".map-stage").classList.toggle("is-expanded", expanded);
  });
}

async function initialize() {
  setDefaultDates();
  bindCombobox("origin", async (query) => (await api.institutions({ q: query, ...originFilters() })).items);
  bindCombobox("destination", async (query) => (await api.places(query)).items);
  ["#institution-type", "#foundation-type", "#education-office", "#district"].forEach((selector) => {
    $(selector).addEventListener("change", () => controls.origin.dispatchEvent(new Event("input")));
  });
  controls.filtersToggle.addEventListener("click", () => {
    const filters = $("#institution-filters");
    const expanded = controls.filtersToggle.getAttribute("aria-expanded") !== "true";
    filters.hidden = !expanded;
    controls.filtersToggle.setAttribute("aria-expanded", String(expanded));
    controls.filtersToggle.textContent = expanded
      ? "기관 검색 필터 닫기"
      : "기관 검색 필터 열기";
  });
  controls.form.addEventListener("submit", calculate);
  bindSortTabs();
  bindMapControls();
  try {
    const bootstrap = await api.bootstrap();
    await map.initialize(bootstrap.map.javascriptKey);
  } catch {
    map.setStatus("지도 설정을 확인하지 못했습니다. 입력과 경로 결과는 계속 사용할 수 있습니다.");
  }
}

initialize();
