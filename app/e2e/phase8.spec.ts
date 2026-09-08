import { expect, test, type Page } from "@playwright/test";

/**
 * Phase 8 (cross-cutting) against the fake hub: Up next queue jump on the Sonos side, search from
 * the home header, shuffle round-trip, and the live "Check for updates" row. Copy is checked
 * verbatim (design system §10).
 */
type Side = { id: string; vendor: string; name: string };
const H = { "X-Illyhub": "1" };

async function sides(page: Page): Promise<{ heos: Side; sonos: Side }> {
  const devices = await (await page.request.get("/api/devices")).json();
  const all = devices.sides as Side[];
  return { heos: all.find((s) => s.vendor === "heos" && s.id.endsWith("heos-1"))!, sonos: all.find((s) => s.vendor === "sonos")! };
}

/** Put a Tidal album on a side so it has a queue, then open Now Playing on that side. */
async function playAlbumOn(page: Page, sideId: string) {
  const home = await (await page.request.get("/api/home")).json();
  const album = (home.favorite_albums.items as { content_ref: { service: string } }[]).find((a) => a.content_ref.service === "tidal")!;
  const res = await page.request.post("/api/play", { data: { target: sideId, content_ref: album.content_ref }, headers: H });
  expect(res.ok()).toBe(true);
}

async function openNowPlayingOn(page: Page, side: Side) {
  await page.goto("/");
  await page.getByRole("button", { name: "Open Now Playing" }).click();
  const np = page.getByTestId("now-playing");
  await expect(np).toBeVisible();
  await np.getByTestId("target-indicator").click();
  const picker = page.getByTestId("zone-picker");
  await picker.getByTestId(`zone-row-${side.id}`).getByRole("button").first().click();
  await expect(picker).toBeHidden();
  await expect(np.getByTestId("target-indicator")).toContainText(side.name);
  return np;
}

test.beforeEach(async ({ page }) => {
  await page.request.post("/api/dev/fake/link_tidal", { headers: H });
  await page.request.post("/api/sync/stop", { headers: H }).catch(() => undefined);
  await page.request.post("/api/pandora-sync/stop", { headers: H }).catch(() => undefined);
});

test("Up next: the sheet lists the Sonos side's queue with the current entry, a tap jumps by the hub's index and the title follows", async ({ page }) => {
  const { sonos } = await sides(page);
  await playAlbumOn(page, sonos.id);
  const np = await openNowPlayingOn(page, sonos);
  const title = np.getByTestId("np-title");
  await expect(title).not.toBeEmpty();
  await np.getByTestId("open-queue").click();
  const sheet = page.getByTestId("queue-sheet");
  await expect(sheet).toBeVisible();
  const rows = sheet.getByTestId("queue-row");
  await expect.poll(async () => rows.count(), { timeout: 5000 }).toBeGreaterThan(1);
  await expect(sheet.locator("[data-current='true']")).toHaveCount(1);
  // jump to the last entry; the hub's index rides in the body
  const last = rows.last();
  const index = await last.getAttribute("data-index");
  const jumped = page.waitForRequest((r) => r.url().endsWith("/api/queue/jump") && r.method() === "POST");
  await last.getByRole("button").click();
  const req = await jumped;
  expect(req.postDataJSON()).toMatchObject({ target: sonos.id, index: Number(index) });
  await expect(sheet).toBeHidden();
  const wanted = (await (await page.request.get(`/api/queue/${encodeURIComponent(sonos.id)}`)).json()) as { items: { index: number; title: string }[] };
  const target = wanted.items.find((it) => it.index === Number(index))!;
  await expect(title).toHaveText(target.title, { timeout: 5000 });
});

test("search from the home header: 'Harmonic' returns canned results grouped with badges; a result opens the picker and plays", async ({ page }) => {
  const { sonos } = await sides(page);
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1, name: "illyHub" })).toBeVisible();
  await page.getByTestId("open-search").click();
  // the h1 stays in the outline for screen readers, visually hidden (U6)
  await expect(page.getByRole("heading", { level: 1, name: "Search illyHub" })).toHaveClass(/sr-only/);
  await expect(page.getByRole("heading", { level: 1, name: "illyHub", exact: true })).toHaveCount(0);
  const input = page.getByTestId("search-input");
  await expect(input).toBeFocused();
  await input.fill("Harmonic");
  const results = page.getByTestId("search-results");
  await expect(results).toBeVisible();
  await expect(results.getByTestId("search-empty")).toHaveCount(0);
  const hits = results.locator("[data-testid='search-card'], [data-testid='search-track']");
  await expect.poll(async () => hits.count()).toBeGreaterThan(0);
  await expect(results.getByRole("img", { name: /Tidal|YouTube Music|Pandora/ }).first()).toBeVisible();
  // the home sections are out of the way while a query is active
  await expect(page.getByTestId("playlists-grid")).toBeHidden();
  const played = page.waitForRequest((r) => r.url().endsWith("/api/play") && r.method() === "POST");
  // cards wrap their play button; track rows are the button
  await results.getByRole("button", { name: /^Play / }).first().click();
  const picker = page.getByTestId("zone-picker");
  await expect(picker).toBeVisible();
  const row = picker.getByTestId(`zone-row-${sonos.id}`).getByRole("button").first();
  if ((await row.getAttribute("aria-pressed")) !== "true") await row.click();
  await picker.getByTestId("confirm-play").click();
  const req = await played;
  expect(req.postDataJSON()).toMatchObject({ target: sonos.id });
  // Cancel restores home
  await page.getByTestId("cancel-search").click();
  await expect(page.getByRole("heading", { level: 1, name: "illyHub" })).toBeVisible();
  await expect(page.getByTestId("playlists-grid")).toBeVisible();
});

test("shuffle toggles round-trip through the hub and the repeat button cycles", async ({ page }) => {
  const { sonos } = await sides(page);
  await playAlbumOn(page, sonos.id);
  const np = await openNowPlayingOn(page, sonos);
  const shuffle = np.getByTestId("shuffle-toggle");
  const before = (await shuffle.getAttribute("aria-pressed")) === "true";
  const posted = page.waitForRequest((r) => r.url().endsWith("/api/playmode") && r.method() === "POST");
  await shuffle.click();
  expect((await posted).postDataJSON()).toMatchObject({ target: sonos.id, shuffle: !before });
  await expect(shuffle).toHaveAttribute("aria-pressed", String(!before));
  // the hub agrees
  await expect.poll(async () => {
    const devices = await (await page.request.get("/api/devices")).json();
    const side = (devices.sides as { id: string; play_mode?: { shuffle: boolean } }[]).find((s) => s.id === sonos.id);
    return side?.play_mode?.shuffle;
  }).toBe(!before);
  const repeat = np.getByTestId("repeat-toggle");
  await expect(repeat).toHaveAccessibleName(/^Repeat( all| one)?$/);
  const repeatBefore = await repeat.getAttribute("data-repeat");
  await repeat.click();
  await expect(repeat).not.toHaveAttribute("data-repeat", repeatBefore!);
  // put both back so other specs see the defaults
  await shuffle.click();
  await expect(shuffle).toHaveAttribute("aria-pressed", String(before));
  while ((await repeat.getAttribute("data-repeat")) !== repeatBefore) {
    await repeat.click();
    await expect.poll(async () => repeat.getAttribute("data-repeat")).not.toBeNull();
  }
});

test("Settings: the Check for updates row is live (reports 'Up to date', or the hub's check error verbatim); Hub stats opens", async ({ page, request }) => {
  // The hub runs a real `git fetch` in its checkout; on a branch with no upstream it reports an
  // error instead of a verdict, and the row must show that sentence rather than pretend.
  const check = (await (await request.get("/api/hub/update/check")).json()) as { available: boolean; error: string | null };
  await page.goto("/settings");
  const row = page.getByTestId("check-updates");
  // Positive verdicts only: the row says what the hub said, in the app's words (never git text).
  const expected = check.error ? /Couldn't check for updates/ : check.available ? "Update available" : "Up to date";
  await expect(row).toContainText(expected, { timeout: 15_000 });
  await expect(row).not.toContainText("fatal:");
  // never trigger the real apply against this checkout
  await expect(page.getByTestId("update-sheet")).toHaveCount(0);
  const stats = page.getByTestId("hub-stats");
  await stats.click();
  await expect(page.getByTestId("hub-stats-body")).not.toBeEmpty();
});
