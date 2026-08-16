import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import type { Page, Route } from "@playwright/test";

const fixtureDir = join(dirname(fileURLToPath(import.meta.url)), "fixtures");

export type MockApiOptions = {
  preview?: object;
  previewForPayload?: (payload: Record<string, unknown>) => object;
  reverse?: object;
};

type MapEvent = {
  kind: string;
  map?: "map" | null;
  options?: Record<string, unknown>;
  type: string;
};

type MapState = {
  created: Array<{ kind: string; options: Record<string, unknown> }>;
  events: MapEvent[];
};

export function readFixture<T extends object>(name: string): T {
  return JSON.parse(readFileSync(join(fixtureDir, name), "utf8")) as T;
}

async function json(route: Route, payload: object): Promise<void> {
  await route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(payload),
  });
}

export async function installMockApi(
  page: Page,
  options: MockApiOptions = {},
): Promise<void> {
  await page.addInitScript(() => {
    const state = {
      created: [] as Array<{ kind: string; options: Record<string, unknown> }>,
      events: [] as Array<{
        kind: string;
        map?: "map" | null;
        options?: Record<string, unknown>;
        type: string;
      }>,
      clickHandler: null as ((event: { latLng: LatLngFake }) => Promise<void> | void) | null,
      async triggerClick(latitude: number, longitude: number): Promise<void> {
        await this.clickHandler?.({ latLng: new LatLngFake(latitude, longitude) });
      },
    };

    class MapFake {
      setBounds(): void {
        state.events.push({ kind: "Map", type: "setBounds" });
      }
    }

    class OverlayFake {
      kind: string;

      constructor(options: Record<string, unknown> = {}) {
        this.kind = this.constructor.name;
        state.created.push({ kind: this.kind, options });
      }

      setMap(map: unknown): void {
        state.events.push({
          kind: this.kind,
          map: map === null ? null : "map",
          type: "setMap",
        });
      }

      setOptions(options: Record<string, unknown>): void {
        state.events.push({ kind: this.kind, options, type: "setOptions" });
      }
    }

    class MarkerFake extends OverlayFake {}
    class PolylineFake extends OverlayFake {}
    class PolygonFake extends OverlayFake {}

    class LatLngFake {
      constructor(
        public lat: number,
        public lng: number,
      ) {}

      getLat(): number {
        return this.lat;
      }

      getLng(): number {
        return this.lng;
      }
    }

    class BoundsFake {
      extend(): void {
        state.events.push({ kind: "LatLngBounds", type: "extend" });
      }
    }

    Object.assign(window, {
      __task8Map: state,
      kakao: {
        maps: {
          load: (callback: () => void) => callback(),
          Map: MapFake,
          Marker: MarkerFake,
          Polyline: PolylineFake,
          Polygon: PolygonFake,
          LatLng: LatLngFake,
          LatLngBounds: BoundsFake,
          MapTypeId: { ROADMAP: "ROADMAP" },
          event: {
            addListener: (
              _map: MapFake,
              eventName: string,
              callback: (event: { latLng: LatLngFake }) => Promise<void> | void,
            ) => {
              if (eventName === "click") state.clickHandler = callback;
            },
          },
        },
      },
    });
  });
  await page.route("**/api/v1/bootstrap", (route) =>
    json(route, readFixture("bootstrap.json")),
  );
  await page.route("**/api/v1/institutions**", (route) => {
    const requestUrl = new URL(route.request().url());
    const payload = readFixture<{ items: Array<Record<string, string>> }>("institutions.json");
    const items = payload.items.filter((item) => (
      (!requestUrl.searchParams.get("institution_type") || item.institutionType === requestUrl.searchParams.get("institution_type"))
      && (!requestUrl.searchParams.get("foundation_type") || item.foundationType === requestUrl.searchParams.get("foundation_type"))
    ));
    return json(route, { items });
  });
  await page.route("**/api/v1/places**", (route) =>
    json(route, readFixture("places.json")),
  );
  await page.route("**/api/v1/places/reverse**", (route) =>
    json(route, options.reverse ?? readFixture("reverse.json")),
  );
  await page.route("**/api/v1/geodata/seoul", (route) =>
    json(route, readFixture("seoul.geojson")),
  );
  await page.route("**/api/v1/geodata/support", (route) =>
    json(route, readFixture("support.geojson")),
  );
  await page.route("**/api/v1/trips/preview", (route) => {
    const payload = JSON.parse(route.request().postData() || "{}") as Record<string, unknown>;
    return json(
      route,
      options.previewForPayload?.(payload) ?? options.preview ?? readFixture("preview.json"),
    );
  });
}

export async function mapState(page: Page): Promise<MapState> {
  return page.evaluate(() => {
    const task8Window = window as typeof window & {
      __task8Map: MapState;
    };
    return {
      created: task8Window.__task8Map.created,
      events: task8Window.__task8Map.events,
    };
  });
}

export async function triggerMapClick(
  page: Page,
  latitude = 37.5663,
  longitude = 126.9779,
): Promise<void> {
  await page.evaluate(
    async ({ latitude: pointLatitude, longitude: pointLongitude }) => {
      const task8Window = window as typeof window & {
        __task8Map: {
          triggerClick(latitude: number, longitude: number): Promise<void>;
        };
      };
      await task8Window.__task8Map.triggerClick(pointLatitude, pointLongitude);
    },
    { latitude, longitude },
  );
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
