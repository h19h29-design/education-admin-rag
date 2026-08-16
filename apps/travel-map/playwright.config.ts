import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  reporter: "line",
  use: {
    baseURL: "http://127.0.0.1:4173",
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: "uv run --project . uvicorn app.main:app --host 127.0.0.1 --port 4173",
    url: "http://127.0.0.1:4173/healthz",
    reuseExistingServer: !process.env.CI,
    timeout: 30_000,
  },
});
