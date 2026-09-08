import { expect, test, type Page } from "@playwright/test";

/**
 * Phase 3 against the fake hub with the canned Tidal library (HUB_FAKE_TIDAL=1): home, the
 * two-tap recents flow (PRD Decision 4), browse detail, settings link/unlink, connect cards.
 */
async function ensureLinked(page: Page) {
  await page.request.post("/api/dev/fake/link_tidal");
  await page.request.post("/api/dev/fake/link_ytmusic");
}

/** Select a side row in the play-mode picker without toggling it off if it is already pre-highlighted (Decision 4). */
async function ensureRowSelected(picker: import("@playwright/test").Locator, sideId: string) {
  const row = picker.getByTestId(`zone-row-${sideId}`).getByRole("button").first();
  await expect(row).toBeVisible();
  if ((await row.getAttribute("aria-pressed")) !== "true") await row.click();
  await expect(row).toHaveAttribute("aria-pressed", "true");
}

test.beforeEach(async ({ page }) => {
  await ensureLinked(page);
});

test("home renders the playlists, albums and Pandora stations grids from the canned libraries", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByTestId("home")).toBeVisible();
  await expect(page.getByTestId("playlists-grid").getByTestId("grid-card").first()).toBeVisible();
  // canned libraries: Tidal 6 albums + 4 playlists, YouTube Music 3 albums + 3 playlists (Phase 6, merged with badges)
  await expect(page.getByTestId("albums-grid").getByTestId("grid-card")).toHaveCount(9);
  await expect(page.getByTestId("playlists-grid").getByTestId("grid-card")).toHaveCount(7);
  await expect(page.getByTestId("albums-grid").getByRole("img", { name: "YouTube Music" })).toHaveCount(3);
  await expect(page.getByTestId("albums-grid").getByRole("img", { name: "Tidal" })).toHaveCount(6);
  // Phase 5: the fake hub runs with HUB_FAKE_PANDORA=1, so the stations section is present
  await expect(page.getByTestId("stations-section").getByTestId("grid-card").first()).toBeVisible();
  // every card carries a service badge chip
  await expect(page.getByTestId("albums-grid").getByRole("img", { name: "Tidal" }).first()).toBeVisible();
});

test("tapping an album opens the target picker in play mode; confirming plays, updates the mini-player, and lands in Recently played", async ({ page, request }) => {
  await page.goto("/");
  const card = page.getByTestId("albums-grid").getByTestId("grid-card").first();
  const title = (await card.locator(".text-body").first().textContent())!.trim();
  const artist = (await card.locator(".text-caption").first().textContent())!.trim();
  await card.getByRole("button", { name: /^Play / }).click();
  const picker = page.getByTestId("zone-picker");
  await expect(picker).toBeVisible();
  await expect(picker.getByRole("heading")).toContainText(`Play “${title}” on`);
  // choose the HEOS side and confirm
  const devices = await (await request.get("/api/devices")).json();
  const heosSide = (devices.sides as { id: string; vendor: string; name: string }[]).find((s) => s.vendor === "heos")!;
  await ensureRowSelected(picker, heosSide.id);
  await picker.getByTestId("confirm-play").click();
  await expect(picker).toBeHidden();
  // the hub starts the album's first track; the mini-player follows its now-playing
  await expect(page.getByTestId("mini-player")).toContainText(artist);
  // the hub logged the play: recents rail shows it
  await expect(page.getByTestId("recents-rail").getByTestId("recent-card").first()).toContainText(title);
  await expect
    .poll(async () => {
      const history = await (await request.get("/api/history?limit=5")).json();
      const items = (Array.isArray(history) ? history : history.items) as { title: string }[];
      return items[0]?.title;
    })
    .toBe(title);
});

test("recents: two fast taps (card, then the pre-highlighted confirm) reach the hub's ack quickly", async ({ page, request }) => {
  // The timing bound is a local requirement check (PRD Decision 4), not a CI gate: CI machines are
  // slow and shared. Motion is disabled so the measurement is the request path, not the spring.
  await page.emulateMedia({ reducedMotion: "reduce" });
  // seed one play so the rail has a card with a remembered target
  const home = await (await request.get("/api/home")).json();
  const album = home.favorite_albums.items[1];
  const devices = await (await request.get("/api/devices")).json();
  const sonosSide = (devices.sides as { id: string; vendor: string }[]).find((s) => s.vendor === "sonos")!;
  await request.post("/api/play", { data: { target: sonosSide.id, content_ref: album.content_ref }, headers: { "X-Illyhub": "1" } });

  await page.goto("/");
  const rail = page.getByTestId("recents-rail");
  const card = rail.getByTestId("recent-card").filter({ hasText: album.title }).first();
  await expect(card).toBeVisible();
  await card.getByRole("button", { name: /^Play / }).click();
  const confirm = page.getByTestId("confirm-play");
  // requirement: the last-used room is pre-highlighted, so the second tap is the confirm itself
  await expect(confirm).toBeEnabled();
  await expect(confirm).toContainText("Play on");
  await confirm.click();
  // the app marks confirm -> ack; read the measure instead of timing the test harness
  const measured = await page.waitForFunction(() => {
    const marks = performance.getEntriesByName("play:ack", "mark");
    if (marks.length === 0) return null;
    performance.measure("play", "play:confirm", "play:ack");
    return performance.getEntriesByName("play", "measure").at(-1)!.duration;
  });
  const ms = (await measured.jsonValue()) as number;
  expect(ms).toBeGreaterThan(0);
  if (!process.env.CI) expect(ms).toBeLessThan(1000);
});

test("browse detail: chevron opens the album, a track row plays from that index", async ({ page, request }) => {
  await page.goto("/");
  const card = page.getByTestId("albums-grid").getByTestId("grid-card").first();
  await card.getByTestId("card-detail").click();
  await expect(page).toHaveURL(/\/browse\?ref=tidal%3Aalbum%3A/);
  await expect(page.getByTestId("detail-title")).not.toBeEmpty();
  const rows = page.getByTestId("track-row");
  await expect(rows.first()).toBeVisible();
  const count = await rows.count();
  expect(count).toBeGreaterThan(2);
  const played = page.waitForRequest((r) => r.url().endsWith("/api/play") && r.method() === "POST");
  await rows.nth(2).click();
  const picker = page.getByTestId("zone-picker");
  const devices = await (await request.get("/api/devices")).json();
  const side = (devices.sides as { id: string }[])[0]!;
  await ensureRowSelected(picker, side.id);
  await picker.getByTestId("confirm-play").click();
  const req = await played;
  expect(req.postDataJSON()).toMatchObject({ target: side.id, start_index: 2 });
  await page.getByTestId("back").click();
  await expect(page.getByTestId("home")).toBeVisible();
});

test("settings: accounts, hub and zones render; unlink shows connect cards on home; relink through the device-code sheet", async ({ page, request }) => {
  await page.goto("/settings");
  await expect(page.getByTestId("settings")).toBeVisible();
  await expect(page.getByTestId("account-tidal")).toContainText("Connected");
  await expect(page.getByTestId("hub-status")).toContainText("Version");
  await expect(page.getByTestId("hardware-row").first()).toBeVisible();

  // unlink via the sheet
  await page.getByTestId("account-tidal").click();
  await expect(page.getByTestId("unlink-sheet")).toBeVisible();
  await page.getByTestId("confirm-unlink").click();
  await expect(page.getByTestId("account-tidal")).toContainText("Not connected");

  // home now offers Connect cards instead of content
  await page.goto("/");
  await expect(page.getByTestId("connect-card").first()).toContainText("Connect Tidal");

  // connect card deep-links into the flow; the fake hub approves via the dev scenario
  await page.getByTestId("connect-card").first().click();
  await expect(page).toHaveURL(/\/settings\?link=tidal/);
  await expect(page.getByTestId("user-code")).not.toBeEmpty();
  await expect(page.getByTestId("open-verification")).toHaveAttribute("href", /https?:\/\//);
  // the fake hub holds the code in `pending` until approved (dev scenario), like the real flow
  await request.post("/api/dev/fake/approve_tidal");
  await expect(page.getByTestId("link-done")).toContainText("Connected", { timeout: 10_000 });
  await page.getByRole("button", { name: "Done" }).click();
  await expect(page.getByTestId("account-tidal")).toContainText("Connected");
});
