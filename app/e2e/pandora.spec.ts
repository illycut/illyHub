import { expect, test, type Page } from "@playwright/test";

/**
 * Phase 5 Pandora against the fake hub (HUB_FAKE_DEVICES=1 HUB_FAKE_TIDAL=1 HUB_FAKE_PANDORA=1):
 * the home stations section (HOME-5), station -> picker -> play with per-vendor availability,
 * the single-stream note, and Now Playing radio mode (no seek, no total duration).
 */
type Side = { id: string; vendor: string; name: string };
const H = { "X-Illyhub": "1" };

async function sides(page: Page): Promise<{ heos: Side; sonos: Side }> {
  const devices = await (await page.request.get("/api/devices")).json();
  const all = devices.sides as Side[];
  return { heos: all.find((s) => s.vendor === "heos")!, sonos: all.find((s) => s.vendor === "sonos")! };
}

async function ensureRowSelected(picker: import("@playwright/test").Locator, sideId: string) {
  const row = picker.getByTestId(`zone-row-${sideId}`).getByRole("button").first();
  await expect(row).toBeVisible();
  if ((await row.getAttribute("aria-pressed")) !== "true") await row.click();
  await expect(row).toHaveAttribute("aria-pressed", "true");
}

async function ensureRowDeselected(picker: import("@playwright/test").Locator, sideId: string) {
  const row = picker.getByTestId(`zone-row-${sideId}`).getByRole("button").first();
  if ((await row.getAttribute("aria-pressed")) === "true" && !(await row.isDisabled())) await row.click();
}

/**
 * Pandora is linked in the vendor apps; the fake exposes that per vendor as dev scenarios. The hub
 * scenarios drop the per-vendor station cache themselves (docs/api.md), so no refresh is needed.
 */
async function linkPandora(page: Page, vendor: "heos" | "sonos", linked: boolean) {
  await page.request.post(`/api/dev/fake/${linked ? "link_pandora" : "unlink_pandora"}?vendor=${vendor}`, { headers: H });
}

/** Leave both vendors linked for the next spec, whatever this one did. */
test.afterEach(async ({ page }) => {
  await linkPandora(page, "heos", true);
  await linkPandora(page, "sonos", true);
});

test.beforeEach(async ({ page }) => {
  await page.request.post("/api/dev/fake/link_tidal", { headers: H });
  await linkPandora(page, "heos", true);
  await linkPandora(page, "sonos", true);
  // stop anything a previous test left playing so "another room plays Pandora" starts false; the
  // fake applies transport on its next tick, so wait until no side reports play
  await page.request.post("/api/transport/stop", { data: { target: "all" }, headers: H });
  await expect
    .poll(async () => {
      const devices = await (await page.request.get("/api/devices")).json();
      return (devices.sides as { play_state: string }[]).filter((s) => s.play_state === "play").length;
    })
    .toBe(0);
});

test("home shows the Pandora stations section from the canned set: alphabetical, badged, no chevron", async ({ page, request }) => {
  const home = await (await request.get("/api/home")).json();
  const stations = (home.stations.items as { title: string }[]).map((s) => s.title);
  expect(stations.length).toBeGreaterThanOrEqual(3);
  await page.goto("/");
  const section = page.getByTestId("stations-section");
  await expect(section).toBeVisible();
  await expect(section.getByRole("heading", { name: "Pandora stations" })).toBeVisible();
  const cards = section.getByTestId("grid-card");
  await expect(cards).toHaveCount(stations.length);
  const shown = await cards.locator(".text-body").allTextContents();
  expect(shown).toEqual([...stations].sort((a, b) => a.localeCompare(b, undefined, { sensitivity: "base" })));
  await expect(section.getByRole("img", { name: "Pandora" })).toHaveCount(stations.length);
  await expect(section.getByTestId("card-detail")).toHaveCount(0);
  await expect(section.getByTestId("stations-note")).toHaveCount(0);
});

test("station -> picker -> play on Sonos: the mini-player follows and the recents card carries the Pandora badge; no Sync Play offered", async ({ page }) => {
  const { heos, sonos } = await sides(page);
  await page.goto("/");
  const section = page.getByTestId("stations-section");
  const card = section.getByTestId("grid-card").first();
  const title = (await card.locator(".text-body").first().textContent())!.trim();
  await card.getByRole("button", { name: /^Play / }).click();
  const picker = page.getByTestId("zone-picker");
  await expect(picker).toBeVisible();
  await expect(picker.getByRole("heading")).toContainText(`Play “${title}” on`);
  // both vendors selectable; both selected -> the single-stream note and a disabled Sync Play with the reason
  await ensureRowSelected(picker, heos.id);
  await ensureRowSelected(picker, sonos.id);
  await expect(picker.getByTestId("pandora-note")).toHaveText("Pandora usually allows one stream per account; the other room may pause.");
  await expect(picker.getByTestId("confirm-play")).toHaveAttribute("data-mode", "play");
  await expect(picker.getByTestId("sync-play-disabled")).toHaveCount(0);
  await expect(picker.getByTestId("station-sync-note")).toHaveText("Stations can't Sync Play: Pandora picks different songs for each room.");
  // play on Sonos only
  await ensureRowDeselected(picker, heos.id);
  await expect(picker.getByTestId("pandora-note")).toHaveCount(0);
  const played = page.waitForRequest((r) => r.url().endsWith("/api/play") && r.method() === "POST");
  await picker.getByTestId("confirm-play").click();
  const req = await played;
  expect(req.postDataJSON()).toMatchObject({ target: sonos.id, content_ref: { service: "pandora", kind: "station" } });
  await expect(picker).toBeHidden();
  // the mini-player follows the Sonos side's now-playing (design §6.3: title, no badge there)
  const mini = page.getByTestId("mini-player");
  await expect(mini).toBeVisible();
  await expect(mini).not.toContainText("Nothing playing");
  await expect(page.getByTestId("recents-rail").getByTestId("recent-card").first()).toContainText(title);
  await expect(page.getByTestId("recents-rail").getByTestId("recent-card").first().getByRole("img", { name: "Pandora" })).toBeVisible();
});

test("Pandora unlinked on HEOS: the section names the HEOS app and HEOS rows are disabled with the reason", async ({ page }) => {
  const { heos, sonos } = await sides(page);
  await linkPandora(page, "heos", false);
  await page.goto("/");
  const section = page.getByTestId("stations-section");
  await expect(section).toBeVisible();
  // room names come from the hub's HEOS sides, joined with " and "
  const devices = await (await page.request.get("/api/devices")).json();
  const heosRooms = (devices.sides as Side[]).filter((s) => s.vendor === "heos").map((s) => s.name).sort();
  const who = heosRooms.length > 1 ? `${heosRooms.slice(0, -1).join(", ")} and ${heosRooms[heosRooms.length - 1]}` : heosRooms[0]!;
  await expect(section.getByTestId("stations-note")).toContainText(`can't play these until Pandora is added in the HEOS app.`);
  await expect(section.getByTestId("stations-note")).toContainText(who.split(" and ")[0]!);
  await section.getByTestId("grid-card").first().getByRole("button", { name: /^Play / }).click();
  const picker = page.getByTestId("zone-picker");
  const heosRow = picker.getByTestId(`zone-row-${heos.id}`).getByRole("button").first();
  await expect(heosRow).toBeDisabled();
  await expect(heosRow).toContainText("Pandora is set up in the HEOS app.");
  await expect(picker.getByTestId(`zone-row-${sonos.id}`).getByRole("button").first()).toBeEnabled();
});

test("Pandora unlinked on both vendors: no stations section and no Connect card", async ({ page }) => {
  await linkPandora(page, "heos", false);
  await linkPandora(page, "sonos", false);
  await page.goto("/");
  await expect(page.getByTestId("albums-grid").getByTestId("grid-card").first()).toBeVisible();
  await expect(page.getByTestId("stations-section")).toHaveCount(0);
  await expect(page.getByText("Pandora stations")).toHaveCount(0);
  await expect(page.getByText(/Connect Pandora/)).toHaveCount(0);
});

test("Now Playing radio mode: read-only scrubber with no thumb, the track total, prev disabled, next enabled", async ({ page }) => {
  const { sonos } = await sides(page);
  const home = await (await page.request.get("/api/home")).json();
  const station = home.stations.items[0];
  await page.request.post("/api/play", { data: { target: sonos.id, content_ref: station.content_ref }, headers: H });
  await page.goto("/");
  await page.getByRole("button", { name: "Open Now Playing" }).click();
  const np = page.getByTestId("now-playing");
  await expect(np).toBeVisible();
  const scrubber = np.getByTestId("scrubber");
  await expect(scrubber).toHaveAttribute("data-seekable", "false");
  await expect(np.getByRole("slider", { name: "Playback position" })).toHaveAttribute("aria-readonly", "true");
  await expect(np.getByTestId("scrub-thumb")).toHaveCount(0);
  await expect(np.getByTestId("elapsed")).toBeVisible();
  // the fake's first station track is 180 000 ms (station_track n=0); the null-duration case is unit-tested
  await expect(np.getByTestId("total")).toHaveText("3:00");
  await expect(np.getByRole("button", { name: "Previous track" })).toBeDisabled();
  await expect(np.getByRole("button", { name: "Next track" })).toBeEnabled();
  await expect(np.getByTestId("offer-sync")).toHaveCount(0);
});
