import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import type { Page, Route } from "@playwright/test";

const fixtureDir = join(dirname(fileURLToPath(import.meta.url)), "fixtures");

function fixture(name: string): object {
  return JSON.parse(readFileSync(join(fixtureDir, name), "utf8"));
}

async function json(route: Route, name: string): Promise<void> {
  await route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(fixture(name)),
  });
}

export async function installMockApi(page: Page): Promise<void> {
  await page.addInitScript(() => {
    class MapFake {
      setBounds(): void {}
    }
    class OverlayFake {
      setMap(): void {}
    }
    class LatLngFake {
      constructor(public lat: number, public lng: number) {}
    }
    class BoundsFake {
      extend(): void {}
    }
    Object.assign(window, {
      kakao: {
        maps: {
          load: (callback: () => void) => callback(),
          Map: MapFake,
          Marker: OverlayFake,
          Polyline: OverlayFake,
          Polygon: OverlayFake,
          LatLng: LatLngFake,
          LatLngBounds: BoundsFake,
          MapTypeId: { ROADMAP: "ROADMAP" },
        },
      },
    });
  });
  await page.route("**/api/v1/bootstrap", (route) => json(route, "bootstrap.json"));
  await page.route("**/api/v1/institutions**", (route) => json(route, "institutions.json"));
  await page.route("**/api/v1/places**", (route) => json(route, "places.json"));
  await page.route("**/api/v1/trips/preview", (route) => json(route, "preview.json"));
}

export async function completePublicOfficialTrip(page: Page): Promise<void> {
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
  await page.getByRole("button", { name: "경로 계산" }).click();
}
