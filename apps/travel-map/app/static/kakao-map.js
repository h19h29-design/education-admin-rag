const MAP_SCRIPT_ID = "kakao-map-sdk";
let sdkPromise;

function coordinate(point) {
  return new window.kakao.maps.LatLng(point.latitude, point.longitude);
}

function messageForMapError() {
  return "지도를 불러오지 못했습니다. 등록 도메인과 지도 JavaScript 키를 확인해 주세요.";
}

function flattenCoordinates(coordinates, output = []) {
  if (typeof coordinates?.[0] === "number") {
    output.push({ latitude: coordinates[1], longitude: coordinates[0] });
    return output;
  }
  coordinates?.forEach((item) => flattenCoordinates(item, output));
  return output;
}

function loadScript(key) {
  if (sdkPromise) return sdkPromise;
  sdkPromise = new Promise((resolve, reject) => {
    const existing = document.getElementById(MAP_SCRIPT_ID);
    if (existing) {
      existing.addEventListener("load", resolve, { once: true });
      existing.addEventListener("error", reject, { once: true });
      return;
    }
    const script = document.createElement("script");
    script.id = MAP_SCRIPT_ID;
    script.async = true;
    script.src = `https://dapi.kakao.com/v2/maps/sdk.js?autoload=false&appkey=${encodeURIComponent(key)}`;
    script.addEventListener("load", resolve, { once: true });
    script.addEventListener("error", reject, { once: true });
    document.head.append(script);
  });
  return sdkPromise;
}

export class KakaoMapController {
  constructor(element, statusElement, onMapClick) {
    this.element = element;
    this.statusElement = statusElement;
    this.onMapClick = onMapClick;
    this.map = null;
    this.routeLines = new Map();
    this.overlays = [];
    this.boundaries = [];
  }

  setStatus(message) {
    this.statusElement.textContent = message;
  }

  async initialize(key) {
    if (!key) {
      this.setStatus("등록된 지도 키가 없어 지도를 표시할 수 없습니다.");
      return false;
    }
    try {
      if (!window.kakao?.maps) await loadScript(key);
      await new Promise((resolve) => window.kakao.maps.load(resolve));
      const maps = window.kakao.maps;
      this.map = new maps.Map(this.element, {
        center: new maps.LatLng(37.5665, 126.978),
        level: 8,
        mapTypeId: maps.MapTypeId?.ROADMAP,
      });
      this.element.classList.add("is-ready");
      this.setStatus("지도에서 위치를 선택해 출장지 주소를 확인할 수 있습니다.");
      maps.event?.addListener?.(this.map, "click", async (event) => {
        const latitude = event.latLng.getLat();
        const longitude = event.latLng.getLng();
        await this.onMapClick({ latitude, longitude });
      });
      return true;
    } catch {
      this.setStatus(messageForMapError());
      return false;
    }
  }

  clearRouteOverlays() {
    this.overlays.forEach((overlay) => overlay.setMap?.(null));
    this.overlays = [];
    this.routeLines.clear();
    this.element.dataset.activeRoute = "";
  }

  showRoutes({ origin, destination, routes, classificationPath }) {
    if (!this.map || !window.kakao?.maps) return;
    this.clearRouteOverlays();
    const maps = window.kakao.maps;
    const bounds = new maps.LatLngBounds();
    const addPoint = (point) => bounds.extend(coordinate(point));
    [origin.coordinate, destination].forEach(addPoint);
    const makeMarker = (point, title) => {
      const marker = new maps.Marker({ map: this.map, position: coordinate(point), title });
      this.overlays.push(marker);
    };
    makeMarker(origin.coordinate, origin.name);
    makeMarker(destination, destination.name);
    routes.forEach((route, index) => {
      const line = new maps.Polyline({
        map: this.map,
        path: route.geometry.map((point) => {
          addPoint(point);
          return coordinate(point);
        }),
        strokeWeight: 5,
        strokeColor: ["#2d6cdf", "#4b9f88", "#8357cf"][index % 3],
        strokeOpacity: 0.65,
        strokeStyle: route.mode === "WALK" ? "shortdash" : "solid",
      });
      this.routeLines.set(route.id, line);
      this.overlays.push(line);
    });
    if (classificationPath?.geometry?.length) {
      const classificationLine = new maps.Polyline({
        map: this.map,
        path: classificationPath.geometry.map(coordinate),
        strokeWeight: 2,
        strokeColor: "#1f5fbf",
        strokeOpacity: 0.75,
        strokeStyle: "shortdash",
      });
      this.overlays.push(classificationLine);
    }
    this.map.setBounds?.(bounds);
  }

  setActiveRoute(routeId) {
    if (!this.map) return;
    this.routeLines.forEach((line, id) => {
      line.setOptions?.({
        strokeWeight: id === routeId ? 8 : 4,
        strokeOpacity: id === routeId ? 1 : 0.35,
      });
    });
    this.element.dataset.activeRoute = routeId;
  }

  async setBoundary(name, visible, fetchGeojson) {
    if (!this.map || !window.kakao?.maps) return;
    this.boundaries
      .filter((item) => item.datasetName === name)
      .forEach((item) => item.setMap?.(null));
    this.boundaries = this.boundaries.filter((item) => item.datasetName !== name);
    if (!visible) return;
    try {
      const geojson = await fetchGeojson(name);
      const maps = window.kakao.maps;
      const points = flattenCoordinates(geojson.features?.flatMap((feature) => feature.geometry?.coordinates));
      if (!points.length) return;
      const polygon = new maps.Polygon({
        map: this.map,
        path: points.map(coordinate),
        strokeWeight: 2,
        strokeColor: name === "seoul" ? "#2d6cdf" : "#3ea17a",
        strokeOpacity: 0.8,
        strokeStyle: "shortdash",
        fillOpacity: 0,
      });
      polygon.datasetName = name;
      this.boundaries.push(polygon);
    } catch {
      this.setStatus("보조 경계 데이터를 불러오지 못했습니다.");
    }
  }
}
