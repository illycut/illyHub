import { expect, test, type Page } from "@playwright/test";

/**
 * Phase 7 Pandora Sync via the hub Mac's AirPlay bridge (experimental), against the fake hub with
 * HUB_FAKE_DEVICES=1 HUB_FAKE_PANDORA=1 HUB_AIRPLAY_ENABLED=1 (FakeAirPlayBridge): Settings row and
 * read-only outputs sheet; station → picker → "Pandora Sync (experimental)" → chip on the
 * mini-player and Now Playing → Stop → chip gone; never for non-station content.
 */
const H = { "X-Illyhub": "1" };

async function stopBridge(page: Page) {
  await page.request.post("/api/pandora-sync/stop", { headers: H }).catch(() => undefined);
}

async function bridgeActive(page: Page): Promise<boolean> {
  const res = await page.request.get("/api/pandora-sync");
  return res.ok() ? ((await res.json()) as { active: boolean }).active === true : false;
}

test.beforeEach(async ({ page }) => {
  await page.request.post("/api/dev/fake/link_pandora?vendor=heos", { headers: H });
  await page.request.post("/api/dev/fake/link_pandora?vendor=sonos", { headers: H });
  // Pandora Sync is hidden while a Sync Play session is live (S10): end any session another spec left.
  await page.request.post("/api/sync/stop", { headers: H }).catch(() => undefined);
  await stopBridge(page);
});

test.afterEach(async ({ page }) => {
  await stopBridge(page);
});

test("settings shows Pandora Sync (AirPlay bridge) as available with the experimental disclosure and a read-only outputs sheet", async ({ page, request }) => {
  const settings = await (await request.get("/api/settings")).json();
  expect(settings.hub.airplay.available).toBe(true);
  await page.goto("/settings");
  const row = page.getByTestId("airplay-row");
  await expect(row).toContainText("Pandora Sync (AirPlay bridge)");
  await expect(row).toContainText("Available");
  await expect(page.getByTestId("airplay-disclosure")).toContainText("Experimental. Pandora Sync points the Mac's AirPlay outputs");
  await page.getByTestId("airplay-outputs").click();
  const sheet = page.getByTestId("airplay-outputs-sheet");
  await expect(sheet).toBeVisible();
  const airplay = await (await request.get("/api/airplay")).json();
  await expect(sheet.getByTestId("airplay-output")).toHaveCount(airplay.outputs.length);
  // Read-only: no toggles inside the list.
  await expect(sheet.getByTestId("airplay-output").getByRole("button")).toHaveCount(0);
  await expect(page.locator("body")).not.toContainText(/Sync via AirPlay|Pandora via AirPlay/);
});

test("station -> picker -> Pandora Sync -> chip on the mini-player and Now Playing -> Stop -> chip gone", async ({ page }) => {
  await page.goto("/");
  const section = page.getByTestId("stations-section");
  await expect(section).toBeVisible();
  await section.getByTestId("grid-card").first().getByRole("button", { name: /^Play / }).click();
  const picker = page.getByTestId("zone-picker");
  await expect(picker).toBeVisible();
  const bridge = picker.getByTestId("pandora-sync");
  await expect(bridge).toBeVisible();
  await expect(bridge).toHaveText("Pandora Sync (experimental)");
  // Plain text, never amber (amber is reserved for Sync Play); the note is tied to the button.
  await expect(bridge).not.toHaveClass(/bg-signal/);
  await expect(bridge).toHaveAttribute("aria-describedby", "pandora-sync-note");
  await expect(picker.getByTestId("pandora-sync-note")).toContainText("The hub Mac plays this station to");
  // Choose exactly the Sonos room: the fake bridge has outputs for "Kitchen + Patio" and "Living Room
  // Amp" but not "Den", and the hub refuses (naming the room) when a chosen room has no output.
  const devices = await (await page.request.get("/api/devices")).json();
  const all = devices.sides as { id: string; vendor: string }[];
  for (const s of all) {
    const row = picker.getByTestId(`zone-row-${s.id}`).getByRole("button").first();
    const pressed = (await row.getAttribute("aria-pressed")) === "true";
    if (s.vendor === "sonos" && !pressed) await row.click();
    if (s.vendor !== "sonos" && pressed) await row.click();
  }
  await expect(picker.getByTestId(`zone-row-${all.find((s) => s.vendor === "sonos")!.id}`).getByRole("button").first()).toHaveAttribute("aria-pressed", "true");
  await expect(bridge).toBeEnabled();
  const started = page.waitForResponse((r) => r.url().endsWith("/api/pandora-sync/start") && r.request().method() === "POST");
  await bridge.click();
  const res = await started;
  expect(res.ok()).toBe(true);
  expect(res.request().postDataJSON()).toHaveProperty("side_ids");
  await expect(picker).toBeHidden();

  // The hub's state is the sync point; the chip follows the delta and the note is toasted once.
  await expect.poll(() => bridgeActive(page)).toBe(true);
  const mini = page.getByTestId("mini-player");
  await expect(mini.getByTestId("pandora-sync-chip")).toBeVisible();
  await expect(mini.getByTestId("pandora-sync-chip")).toHaveText("Pandora Sync");
  await expect(page.getByRole("status").filter({ hasText: /hub Mac/ })).toHaveCount(1);

  await mini.getByRole("button", { name: "Open Now Playing" }).click();
  const np = page.getByTestId("now-playing");
  await expect(np).toBeVisible();
  await expect(np).toHaveAttribute("data-np-bridged", "true");
  const indicator = np.getByTestId("target-indicator");
  await expect(indicator).toContainText("Pandora Sync ·");
  // The indicator text never collapses, even at 375px (U2).
  const textBox = await indicator.locator("span.truncate").boundingBox();
  expect(textBox?.width ?? 0).toBeGreaterThan(0);
  await expect(indicator.locator("span.truncate")).toBeVisible();
  await expect(np.getByTestId("pandora-sync-chip")).toHaveText("Pandora Sync");
  await expect(np.getByTestId("bridge-caption")).toHaveText("Press play on the hub Mac; the rooms follow. Controls are there while Pandora Sync is on.");
  await expect(np.getByTestId("play-toggle")).toBeDisabled();
  await expect(np.getByTestId("play-toggle")).toHaveAttribute("data-outlined", "true");
  await expect(np.getByTestId("side-volume")).not.toHaveAttribute("aria-disabled", "true");
  const stops = np.getByRole("button", { name: "Stop" });
  await expect(stops).toHaveCount(1);

  const stopped = page.waitForResponse((r) => r.url().endsWith("/api/pandora-sync/stop") && r.request().method() === "POST");
  await stops.click();
  expect((await stopped).ok()).toBe(true);
  await expect.poll(() => bridgeActive(page)).toBe(false);
  await expect(np.getByTestId("pandora-sync-chip")).toHaveCount(0);
  await expect(np.getByTestId("play-toggle")).toBeEnabled();
  await expect(page.locator("body")).not.toContainText(/Sync via AirPlay|Pandora via AirPlay/);
});

test("the affordance never appears for non-station content", async ({ page }) => {
  await page.goto("/");
  const albums = page.getByTestId("albums-grid");
  await expect(albums).toBeVisible();
  await albums.getByTestId("grid-card").first().getByRole("button", { name: /^Play / }).click();
  const picker = page.getByTestId("zone-picker");
  await expect(picker).toBeVisible();
  await expect(picker.getByTestId("pandora-sync")).toHaveCount(0);
});
