import { expect, test } from "@playwright/test";

/**
 * Smoke against the fake hub (HUB_FAKE_DEVICES=1) serving the static export. Full suite is
 * ai-dev #76. Runs in WebKit (iPhone 13) and Chromium (Pixel 7) via playwright.config.ts.
 */
const np = (page: import("@playwright/test").Page) => page.getByTestId("now-playing");

test("loads Now Playing from the hub snapshot and toggles playback", async ({ page }) => {
  await page.goto("/");
  await expect(np(page)).toBeVisible();
  await expect(page.getByTestId("target-indicator")).toContainText(/Living Room|Kitchen|Den|Patio/);
  await expect(page.getByTestId("np-title")).not.toBeEmpty();
  const toggle = page.getByTestId("play-toggle");
  await expect(toggle).toBeEnabled();
  const before = await toggle.getAttribute("aria-pressed");
  await toggle.click();
  await expect(toggle).not.toHaveAttribute("aria-pressed", before!);
});

test("volume sheet lists the master slider plus one slider per player", async ({ page, request }) => {
  const devices = await (await request.get("/api/devices")).json();
  const playerCount = (devices.players as unknown[]).length;
  await page.goto("/");
  await page.getByTestId("open-volume").click();
  const sheet = page.getByTestId("volume-sheet");
  await expect(sheet).toBeVisible();
  await expect(sheet.getByTestId("master-slider")).toBeVisible();
  await expect(sheet.getByRole("slider")).toHaveCount(1 + playerCount);
  expect(playerCount).toBeGreaterThanOrEqual(3);
});

test("hub scenario: a track change shows up in the Now Playing title without reload", async ({ page, request }) => {
  await page.goto("/");
  const title = np(page).getByTestId("np-title");
  await expect(title).not.toBeEmpty();
  // make sure the HEOS side (the one with tracks) is showing
  const before = await title.textContent();
  await request.post("/api/dev/fake/track_change");
  await expect.poll(async () => title.textContent(), { timeout: 5000 }).not.toBe(before);
});

test("WebSocket is live: a fake volume change lands in the volume sheet without reload", async ({ page, request }) => {
  await page.goto("/");
  await page.getByTestId("open-volume").click();
  const devices = await (await request.get("/api/devices")).json();
  const patio = (devices.players as { id: string; name: string; volume: number }[]).find((p) => p.name === "Patio")!;
  const slider = page.getByTestId(`slider-${patio.id}`);
  await expect(slider).toHaveAttribute("aria-valuenow", String(patio.volume));
  await request.post("/api/dev/fake/volume_change");
  await expect.poll(async () => slider.getAttribute("aria-valuenow"), { timeout: 5000 }).not.toBe(String(patio.volume));
});
