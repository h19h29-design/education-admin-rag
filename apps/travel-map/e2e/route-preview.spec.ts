import { expect, test } from "@playwright/test";
import {
  completePublicOfficialTrip,
  installMockApi,
  mapState,
  readFixture,
  triggerMapClick,
} from "./helpers";

test("keeps institution filters available without crowding the initial form", async ({
  page,
}) => {
  await installMockApi(page);
  await page.goto("/");

  await expect(
    page.getByRole("button", { name: "기관 검색 필터 열기" }),
  ).toBeVisible();
  await expect(page.getByLabel("기관유형")).toBeHidden();

  await page.getByRole("button", { name: "기관 검색 필터 열기" }).click();
  await expect(page.getByLabel("기관유형")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "기관 검색 필터 닫기" }),
  ).toBeVisible();
});

test("selects a private school origin and shows route rankings", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/");
  await page.getByLabel("출발 기관").fill("샘물");
  await page
    .getByRole("option", { name: /샘물사립고등학교.*사립.*강남구/ })
    .click();
  await page.getByLabel("출장지").fill("서울시청");
  await page
    .getByRole("option", { name: /서울특별시청.*세종대로 110/ })
    .click();
  await page.getByLabel("적용 규정").selectOption("NONPUBLIC_OR_UNKNOWN");
  await page.getByRole("button", { name: "경로 계산" }).click();

  await expect(page.getByText("최단시간")).toBeVisible();
  await expect(page.getByText("최단거리")).toBeVisible();
  await expect(page.getByText("최저비용")).toBeVisible();
  await expect(page.getByText("여비 판정 보류")).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "예상 이동비" }),
  ).toBeVisible();
});

test("selecting a route updates the emphasized polyline", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/");
  await completePublicOfficialTrip(page);
  await page.getByRole("button", { name: /도보.*35분/ }).click();

  await expect(page.locator("[data-route-id='walk-1']")).toHaveAttribute(
    "aria-current",
    "true",
  );
  await expect(page.locator("#map")).toHaveAttribute("data-active-route", "walk-1");
});

test("keeps localized time controls readable on mobile", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await installMockApi(page);
  await page.goto("/");

  expect(
    await page
      .locator("#returns-time")
      .evaluate((node) => node.getBoundingClientRect().width >= 116),
  ).toBeTruthy();
});

test("keeps localized date controls readable in the input rail", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/");

  expect(
    await page
      .locator("#returns-date")
      .evaluate((node) => node.getBoundingClientRect().width >= 200),
  ).toBeTruthy();
});

test("shows the input rail, rankings, and collapsible map without mobile overflow", async ({
  page,
}) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await installMockApi(page);
  await page.goto("/");
  await expect(page.getByLabel("출발 기관")).toBeVisible();
  await expect(page.getByLabel("출장지")).toBeVisible();
  await expect(page.getByLabel("적용 규정")).toBeVisible();

  await completePublicOfficialTrip(page);
  await expect(page.getByRole("heading", { name: "추천 경로" })).toBeVisible();
  await page.getByRole("button", { name: "지도 펼치기" }).click();
  await expect(page.locator("#map")).toBeVisible();
  expect(
    await page
      .locator("html")
      .evaluate((node) => node.scrollWidth <= window.innerWidth),
  ).toBeTruthy();
});

test("shows a route-level warning for partial route data", async ({ page }) => {
  await installMockApi(page);
  await page.goto("/");
  await completePublicOfficialTrip(page);

  await expect(page.getByText("주차비는 예상값입니다")).toBeVisible();
});

test("renders route source data as inert text", async ({ page }) => {
  const preview = readFixture<{
    routes: Array<{ source: string }>;
  }>("preview.json");
  preview.routes[0].source =
    '<img src=x onerror="window.__task8Xss=\'executed\'">';
  await installMockApi(page, { preview });
  await page.goto("/");
  await completePublicOfficialTrip(page);

  await expect(page.locator("#route-list img")).toHaveCount(0);
  expect(await page.locator(".route-source").allTextContents()).toContain(
    '<img src=x onerror="window.__task8Xss=\'executed\'"> 기준',
  );
  await expect
    .poll(() => page.evaluate(() => window.__task8Xss))
    .toBeUndefined();
});

test("map click invalidates the selected destination until its result is confirmed", async ({
  page,
}) => {
  let previewPosts = 0;
  page.on("request", (request) => {
    if (
      request.method() === "POST" &&
      new URL(request.url()).pathname === "/api/v1/trips/preview"
    ) {
      previewPosts += 1;
    }
  });
  await installMockApi(page);
  await page.goto("/");
  await expect(page.locator("#map-status")).toHaveText(
    "지도에서 위치를 선택해 출장지 주소를 확인할 수 있습니다.",
  );
  await page.getByLabel("출발 기관").fill("샘물");
  await page
    .getByRole("option", { name: /샘물공립초등학교.*공립.*중구/ })
    .click();
  await page.getByLabel("출장지").fill("서울시청");
  await page
    .getByRole("option", { name: /서울특별시청.*세종대로 110/ })
    .click();
  await page
    .getByLabel("적용 규정")
    .selectOption("SEOUL_EDU_PUBLIC_OFFICIAL_CONFIRMED");

  await triggerMapClick(page);

  await expect(
    page.getByRole("option", { name: /서울특별시청 지도 선택.*세종대로 110/ }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "경로 계산" })).toBeDisabled();
  await page.locator("#trip-form").evaluate((form) => form.requestSubmit());
  await page.waitForTimeout(100);
  expect(previewPosts).toBe(0);

  await page
    .getByRole("option", { name: /서울특별시청 지도 선택.*세종대로 110/ })
    .click();
  const previewRequest = page.waitForRequest(
    (request) =>
      request.method() === "POST" &&
      new URL(request.url()).pathname === "/api/v1/trips/preview",
  );
  await page.getByRole("button", { name: "경로 계산" }).click();
  const payload = JSON.parse((await previewRequest).postData() || "{}");
  expect(payload.destination.name).toBe("서울특별시청 지도 선택");
});

test("mobile map exposes a collapse control above the expanded canvas", async ({
  page,
}) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await installMockApi(page);
  await page.goto("/");

  await page.getByRole("button", { name: "지도 펼치기" }).click();
  await expect(page.getByRole("button", { name: "지도 접기" })).toBeVisible();
  await page.getByRole("button", { name: "지도 접기" }).click();
  await expect(page.getByRole("button", { name: "지도 펼치기" })).toBeVisible();
  await expect(page.locator("#map")).toBeHidden();
});

test("map polyline, cleanup, and boundary effects reach the Kakao adapter", async ({
  page,
}) => {
  await installMockApi(page);
  await page.goto("/");
  await completePublicOfficialTrip(page);
  await expect(page.locator("[data-route-id='car-1']")).toBeVisible();

  const initialMapState = await mapState(page);
  expect(
    initialMapState.created.filter(({ kind }) => kind === "PolylineFake"),
  ).toHaveLength(4);
  expect(
    initialMapState.events.some(
      ({ options, type }) =>
        type === "setOptions" && options?.strokeWeight === 8,
    ),
  ).toBe(true);

  await page.getByRole("button", { name: /도보.*35분/ }).click();
  await expect
    .poll(async () =>
      (await mapState(page)).events.some(
        ({ options, type }) =>
          type === "setOptions" && options?.strokeWeight === 8,
      ),
    )
    .toBe(true);

  await page.getByLabel("서울 경계").check();
  await expect
    .poll(async () =>
      (await mapState(page)).created.filter(({ kind }) => kind === "PolygonFake")
        .length,
    )
    .toBe(1);
  await page.getByLabel("서울 경계").uncheck();
  await expect
    .poll(async () =>
      (await mapState(page)).events.filter(
        ({ kind, map, type }) =>
          kind === "PolygonFake" && map === null && type === "setMap",
      ).length,
    )
    .toBe(1);

  await page.getByRole("button", { name: "경로 계산" }).click();
  await expect
    .poll(async () =>
      (await mapState(page)).events.filter(
        ({ map, type }) => map === null && type === "setMap",
      ).length,
    )
    .toBeGreaterThanOrEqual(6);
});

test("keyboard option selection authorizes calculation while free text does not", async ({
  page,
}) => {
  await installMockApi(page);
  await page.goto("/");

  await page.getByLabel("출발 기관").fill("샘물");
  await page.getByLabel("출발 기관").press("ArrowDown");
  await page.getByLabel("출발 기관").press("Enter");
  await expect(page.getByLabel("출발 기관")).toHaveValue("샘물공립초등학교");
  await page.getByLabel("출장지").fill("서울시청");
  await page
    .getByLabel("적용 규정")
    .selectOption("SEOUL_EDU_PUBLIC_OFFICIAL_CONFIRMED");
  await expect(page.getByRole("button", { name: "경로 계산" })).toBeDisabled();

  await page.getByLabel("출장지").press("ArrowDown");
  await page.getByLabel("출장지").press("Enter");
  await expect(page.getByRole("button", { name: "경로 계산" })).toBeEnabled();
});
