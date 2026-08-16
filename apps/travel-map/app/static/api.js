const API_ROOT = "/api/v1";

class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function request(path, options = {}) {
  const response = await fetch(`${API_ROOT}${path}`, {
    headers: { Accept: "application/json", ...options.headers },
    ...options,
  });
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    throw new ApiError(payload?.error?.code || "요청을 처리하지 못했습니다.", response.status);
  }
  return payload;
}

function queryString(values) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (value) query.set(key, value);
  }
  return query.toString();
}

export const api = {
  bootstrap: () => request("/bootstrap"),
  institutions: (filters) => request(`/institutions?${queryString({
    q: filters.q,
    limit: "20",
    institution_type: filters.institutionType,
    foundation_type: filters.foundationType,
    education_office: filters.educationOffice,
    district: filters.district,
  })}`),
  places: (query) => request(`/places?${queryString({ q: query })}`),
  reversePlace: ({ latitude, longitude }) => request(`/places/reverse?${queryString({ latitude, longitude })}`),
  preview: (payload) => request("/trips/preview", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  }),
  geodata: (name) => request(`/geodata/${name}`),
};

export { ApiError };
