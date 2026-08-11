import { expect, test } from "@playwright/test";
import { completePublicOfficialTrip, installMockApi } from "./helpers";

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
