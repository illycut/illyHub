import { expect, test } from "@playwright/test";

/**
 * Smoke against the fake hub (HUB_FAKE_DEVICES=1) serving the static export. Full suite is
 * ai-dev #76. Runs in WebKit (iPhone 13) and Chromium (Pixel 7) via playwright.config.ts.
 */
const np = (page: import("@playwright/test").Page) => page.getByTestId("now-playing");

/** Home is the front door (PRD §7a); Now Playing expands from the mini-player. */
async function openNowPlaying(page: import("@playwright/test").Page) {
  await page.goto("/");
  await expect(page.getByTestId("home")).toBeVisible();
  await page.getByRole("button", { name: "Open Now Playing" }).click();
  await expect(np(page)).toBeVisible();
}

/** Point Now Playing at the HEOS side (the fake side with a track list) via the target picker. */
async function showHeosSide(page: import("@playwright/test").Page, request: import("@playwright/test").APIRequestContext) {
  const devices = await (await request.get("/api/devices")).json();
  const heos = (devices.sides as { id: string; vendor: string; name: string }[]).find((s) => s.vendor === "heos" && s.id.endsWith("heos-1"))!;
  await openNowPlaying(page);
  await np(page).getByTestId("target-indicator").click();
  const picker = page.getByTestId("zone-picker");
  await picker.getByTestId(`zone-row-${heos.id}`).getByRole("button").first().click();
  await expect(picker).toBeHidden();
  await expect(np(page).getByTestId("target-indicator")).toContainText(heos.name);
  return heos;
}

test("loads Now Playing from the hub snapshot and toggles playback", async ({ page, request }) => {
  await showHeosSide(page, request);
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
  await openNowPlaying(page);
  await page.getByTestId("open-volume").click();
  const sheet = page.getByTestId("volume-sheet");
  await expect(sheet).toBeVisible();
  await expect(sheet.getByTestId("master-slider")).toBeVisible();
  await expect(sheet.getByRole("slider")).toHaveCount(1 + playerCount);
  expect(playerCount).toBeGreaterThanOrEqual(3);
});

test("hub scenario: a track change shows up in the Now Playing title without reload", async ({ page, request }) => {
  await showHeosSide(page, request);
  const title = np(page).getByTestId("np-title");
  await expect(title).not.toBeEmpty();
  const before = await title.textContent();
  await request.post("/api/dev/fake/track_change");
  await expect.poll(async () => title.textContent(), { timeout: 5000 }).not.toBe(before);
});

test("WebSocket is live: a fake volume change lands in the volume sheet without reload", async ({ page, request }) => {
  await openNowPlaying(page);
  await page.getByTestId("open-volume").click();
  const devices = await (await request.get("/api/devices")).json();
  const patio = (devices.players as { id: string; name: string; volume: number }[]).find((p) => p.name === "Patio")!;
  const slider = page.getByTestId(`slider-${patio.id}`);
  await expect(slider).toHaveAttribute("aria-valuenow", String(patio.volume));
  await request.post("/api/dev/fake/volume_change");
  await expect.poll(async () => slider.getAttribute("aria-valuenow"), { timeout: 5000 }).not.toBe(String(patio.volume));
});
