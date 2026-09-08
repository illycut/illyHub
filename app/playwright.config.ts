import { defineConfig, devices } from "@playwright/test";

/**
 * E2E against the fake hub. The web server builds the export and starts the hub, which serves
 * `out/` at `/`. Full suite is ai-dev #76; `e2e/*.spec.ts` are the seed.
 */
const PORT = 8080;

export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  retries: process.env.CI ? 1 : 0,
  // One hub, one fake library, one history log: the projects share that state, so run serially.
  workers: 1,
  fullyParallel: false,
  reporter: [["list"]],
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: "retain-on-failure",
  },
  projects: [
    { name: "webkit-iphone13", use: { ...devices["iPhone 13"] } },
    { name: "chromium-pixel7", use: { ...devices["Pixel 7"] } },
  ],
  webServer: {
    command: `npm run build && HUB_FAKE_DEVICES=1 HUB_FAKE_TIDAL=1 HUB_FAKE_PANDORA=1 HUB_FAKE_YTMUSIC=1 HUB_PORT=${PORT} HUB_APP_DIR=../app/out uv run --project ../hub hub`,
    url: `http://127.0.0.1:${PORT}/api/health`,
    reuseExistingServer: !process.env.CI,
    timeout: 180_000,
  },
});
