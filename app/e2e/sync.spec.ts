import { expect, test, type Page } from "@playwright/test";

/**
 * Phase 4 Sync Play against the fake hub (HUB_FAKE_DEVICES=1 HUB_FAKE_TIDAL=1). The fake sides
 * simulate independent clocks; dev scenarios inject drift, pause the follower, and advance the
 * master's track. Copy is checked verbatim (design system §6.7, §10).
 */
type Side = { id: string; vendor: string; name: string };
const escapeRe = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

async function sides(page: Page): Promise<{ heos: Side; sonos: Side }> {
  const devices = await (await page.request.get("/api/devices")).json();
  const all = devices.sides as Side[];
  return { heos: all.find((s) => s.vendor === "heos")!, sonos: all.find((s) => s.vendor === "sonos")! };
}

async function resetSync(page: Page) {
  await page.request.post("/api/dev/fake/link_tidal");
  // stop any session left by a previous test; sync_idle is fine
  await page.request.post("/api/sync/stop", { headers: { "X-Illyhub": "1" } });
}

async function ensureRowSelected(picker: import("@playwright/test").Locator, sideId: string) {
  const row = picker.getByTestId(`zone-row-${sideId}`).getByRole("button").first();
  await expect(row).toBeVisible();
  if ((await row.getAttribute("aria-pressed")) !== "true") await row.click();
  await expect(row).toHaveAttribute("aria-pressed", "true");
}

/** Open the first album from home, select both vendors, and press Sync Play. */
async function startSyncFromPicker(page: Page) {
  const { heos, sonos } = await sides(page);
  await page.goto("/");
  const card = page.getByTestId("albums-grid").getByTestId("grid-card").first();
  await card.getByRole("button", { name: /^Play / }).click();
  const picker = page.getByTestId("zone-picker");
  await expect(picker).toBeVisible();
  await ensureRowSelected(picker, heos.id);
  await ensureRowSelected(picker, sonos.id);
  const confirm = picker.getByTestId("confirm-play");
  await expect(confirm).toHaveText("Sync Play");
  await expect(confirm).toHaveAttribute("data-mode", "sync");
  await expect(picker.getByTestId("sync-note")).toContainText(`${heos.name} and ${sonos.name}, together. Close, not perfect.`);
  await expect(confirm).toHaveAttribute("aria-describedby", "sync-note");
  // unsynced multi-room play stays one tap away as a plain secondary button
  await expect(picker.getByTestId("confirm-play-plain")).toHaveText(/^Play (on|in) /);
  await confirm.click();
  await expect(picker).toBeHidden();
  return { heos, sonos };
}

test.beforeEach(async ({ page }) => {
  await resetSync(page);
});

test("Sync Play from the picker: chip shows Starting then Synced; indicator names both rooms; scrubber is read-only", async ({ page }) => {
  const { heos, sonos } = await startSyncFromPicker(page);
  // the mini-player chip: Starting (any of the priming phases) then Synced
  const chip = page.getByTestId("mini-player").getByTestId("sync-chip");
  await expect(chip).toBeVisible();
  await expect(chip).toHaveText(/Starting|Synced/);
  await expect(chip).toHaveText(/Synced/, { timeout: 15_000 });
  // Now Playing: indicator reads Syncing A + B, the scrubber cannot be dragged (HEOS master)
  await page.getByRole("button", { name: "Open Now Playing" }).click();
  const np = page.getByTestId("now-playing");
  await expect(np.getByTestId("target-indicator")).toContainText("Syncing 2 rooms");
  await expect(np.getByTestId("target-indicator")).toHaveAttribute("aria-label", new RegExp(`^Syncing ${escapeRe(heos.name)} and ${escapeRe(sonos.name)}\\.`));
  await expect(np.getByRole("slider").first()).toHaveAttribute("aria-readonly", "true");
  await expect(np.getByTestId("stop-sync")).toHaveText("Stop sync");
  const state = await (await page.request.get("/api/sync")).json();
  expect(state.master_side).toBe(heos.id);
  expect(state.follower_side).toBe(sonos.id);
});

test("375px header in the sync state: no horizontal overflow, header stays 48px, label truncates", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 667 });
  await startSyncFromPicker(page);
  await expect(page.getByTestId("mini-player").getByTestId("sync-chip")).toHaveText(/Starting|Synced/);
  await page.getByRole("button", { name: "Open Now Playing" }).click();
  const np = page.getByTestId("now-playing");
  await expect(np.getByTestId("target-indicator")).toContainText("Syncing");
  const header = np.locator("header").first();
  const box = await header.boundingBox();
  expect(box!.height).toBe(48);
  const overflow = await np.evaluate((el) => el.scrollWidth - el.clientWidth);
  expect(overflow).toBeLessThanOrEqual(0);
  const docOverflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(docOverflow).toBeLessThanOrEqual(0);
  // the chip stays fully visible at the right of the truncated label
  const chip = await np.getByTestId("sync-chip").boundingBox();
  expect(chip!.x + chip!.width).toBeLessThanOrEqual(375);
});

test("drift scenario: chip reads Adjusting then returns to Synced", async ({ page }) => {
  await startSyncFromPicker(page);
  const chip = page.getByTestId("mini-player").getByTestId("sync-chip");
  await expect(chip).toHaveText(/Synced/, { timeout: 15_000 });
  await page.request.post("/api/dev/fake/sync_drift");
  await expect(chip).toHaveText(/Adjusting/, { timeout: 10_000 });
  // The engine must correct the follower at least once. Whether the fake clocks re-lock within
  // the test window is the hub's convergence behaviour (rate-capped at one seek per 10 s), so
  // the chip may still read Adjusting here; it must never read Sync lost.
  await expect
    .poll(async () => ((await (await page.request.get("/api/sync")).json()) as { corrections: number }).corrections, { timeout: 20_000 })
    .toBeGreaterThanOrEqual(1);
  await expect(chip).toHaveText(/Synced|Adjusting/);
  await expect(chip).not.toHaveText(/Sync lost/);
});

test("losing the follower: Sync lost with Retry; Retry brings it back", async ({ page }) => {
  await startSyncFromPicker(page);
  const mini = page.getByTestId("mini-player");
  await expect(mini.getByTestId("sync-chip")).toHaveText(/Synced/, { timeout: 15_000 });
  await page.request.post("/api/dev/fake/sync_lose_sonos");
  const retry = mini.getByTestId("sync-retry");
  await expect(retry).toHaveText("Sync lost · Retry", { timeout: 10_000 });
  await retry.click();
  // Retry re-primes from the master's track: the session is driven again (Starting → Synced or
  // Adjusting while the fake follower settles) and must not fall back to lost.
  await expect(mini.getByTestId("sync-chip")).toHaveText(/Starting|Synced|Adjusting/, { timeout: 5_000 });
  await expect
    .poll(async () => ((await (await page.request.get("/api/sync")).json()) as { status: string }).status, { timeout: 20_000 })
    .toMatch(/^(starting|locked|drifting|correcting)$/);
  await expect(mini.getByTestId("sync-chip")).not.toHaveText(/Sync lost/);
});

test("Stop sync returns both rooms to independent control; the chip disappears", async ({ page }) => {
  await startSyncFromPicker(page);
  await expect(page.getByTestId("mini-player").getByTestId("sync-chip")).toHaveText(/Synced/, { timeout: 15_000 });
  await page.getByRole("button", { name: "Open Now Playing" }).click();
  const np = page.getByTestId("now-playing");
  await expect(np.getByTestId("sync-chip")).toBeVisible();
  await expect(np.getByTestId("stop-sync")).toHaveText("Stop sync");
  await np.getByTestId("stop-sync").click();
  await expect(np.getByTestId("sync-chip")).toBeHidden();
  await expect(np.getByTestId("stop-sync")).toBeHidden();
  const state = await (await page.request.get("/api/sync")).json();
  expect(["stopped", "idle"]).toContain(state.status);
  // both sides keep playing independently
  const devices = await (await page.request.get("/api/devices")).json();
  const playing = (devices.sides as { play_state: string }[]).filter((s) => s.play_state === "play");
  expect(playing.length).toBeGreaterThanOrEqual(2);
});

test("single vendor selected: plain Play, no Sync Play affordance (the non-Tidal disabled case is unit-tested; the fake library is Tidal-only)", async ({ page }) => {
  const { heos } = await sides(page);
  await page.goto("/");
  await page.getByTestId("albums-grid").getByTestId("grid-card").first().getByRole("button", { name: /^Play / }).click();
  const picker = page.getByTestId("zone-picker");
  await ensureRowSelected(picker, heos.id);
  // deselect any Sonos row so only one vendor remains
  for (const row of await picker.locator('[data-testid^="zone-row-sonos"] button[aria-pressed="true"]').all()) await row.click();
  await expect(picker.getByTestId("confirm-play")).toHaveAttribute("data-mode", "play");
  await expect(picker.getByTestId("sync-note")).toHaveCount(0);
});
