import { expect, test, type Page } from "@playwright/test";

/**
 * Phase 6 YouTube Music against the fake hub (HUB_FAKE_DEVICES=1 HUB_FAKE_TIDAL=1 HUB_FAKE_PANDORA=1
 * HUB_FAKE_YTMUSIC=1): the shared device-code flow from the connect card, merged playlists with the
 * YouTube Music badge, and a YT Music playlist that HEOS rooms cannot play (availability from the hub).
 */
type Side = { id: string; vendor: string; name: string };
const H = { "X-Illyhub": "1" };

async function sides(page: Page): Promise<{ heos: Side; sonos: Side }> {
  const devices = await (await page.request.get("/api/devices")).json();
  const all = devices.sides as Side[];
  return { heos: all.find((s) => s.vendor === "heos")!, sonos: all.find((s) => s.vendor === "sonos")! };
}

async function ytPlaylist(page: Page) {
  const home = await (await page.request.get("/api/home")).json();
  const items = home.playlists.items as { content_ref: { service: string }; title: string; availability: { heos: boolean; sonos: boolean } }[];
  return items.find((i) => i.content_ref.service === "ytmusic")!;
}

async function relinkBoth(request: import("@playwright/test").APIRequestContext) {
  await request.post("/api/dev/fake/link_tidal", { headers: H });
  await request.post("/api/dev/fake/link_ytmusic", { headers: H });
}

test.beforeEach(async ({ page }) => {
  await relinkBoth(page.request);
  await page.request.post("/api/transport/stop", { headers: { ...H, "content-type": "application/json" }, data: { target: "all" } });
});

// Every spec leaves the canned house linked on both services for whatever runs next.
test.afterEach(async ({ request }) => {
  await relinkBoth(request);
});

test("connect card → shared device-code sheet → approved → merged playlists carry the YouTube Music badge", async ({ page, request }) => {
  // Both hub-linked services unlinked: home offers both connect cards.
  await request.post("/api/dev/fake/unlink_tidal", { headers: H });
  await request.post("/api/dev/fake/unlink_ytmusic", { headers: H });
  await page.goto("/");
  // one card per unlinked service, once on the screen (B2)
  const yt = page.getByRole("button", { name: "Connect YouTube Music" });
  await expect(yt).toBeVisible();
  await expect(page.getByRole("button", { name: "Connect Tidal" })).toHaveCount(1);
  await yt.click();
  await expect(page).toHaveURL(/\/settings\?link=ytmusic/);
  await expect(page.getByTestId("link-sheet")).toContainText("Connect YouTube Music");
  await expect(page.getByTestId("user-code")).not.toBeEmpty();
  await expect(page.getByTestId("open-verification")).toHaveAttribute("href", /https?:\/\//);
  await request.post("/api/dev/fake/approve_ytmusic", { headers: H });
  await expect(page.getByTestId("link-done")).toContainText("Connected", { timeout: 10_000 });
  await page.getByRole("button", { name: "Done" }).click();
  await expect(page.getByTestId("account-ytmusic")).toContainText("Connected");

  await request.post("/api/dev/fake/link_tidal", { headers: H });
  await page.goto("/");
  await expect(page.getByTestId("connect-card")).toHaveCount(0);
  const grid = page.getByTestId("playlists-grid");
  await expect(grid).toBeVisible();
  const pl = await ytPlaylist(page);
  const card = grid.getByTestId("grid-card").filter({ hasText: pl.title }).first();
  await expect(card).toBeVisible();
  await expect(card.getByRole("img", { name: "YouTube Music" })).toBeVisible();
  await expect(grid.getByRole("img", { name: "Tidal" }).first()).toBeVisible();
});

test("a YouTube Music playlist: HEOS rows disabled with the hub's reason, plays on Sonos, mini-player shows it", async ({ page }) => {
  const { heos, sonos } = await sides(page);
  const pl = await ytPlaylist(page);
  expect(pl.availability.sonos).toBe(true);
  // HEOS has no YouTube Music (hub spike verdict: no-go); the hub says so per item
  expect(pl.availability.heos).toBe(false);
  expect((pl.availability as { reasons?: { heos: string | null } }).reasons?.heos).toBe("unsupported");
  await page.goto("/");
  const grid = page.getByTestId("playlists-grid");
  await grid.getByTestId("grid-card").filter({ hasText: pl.title }).first().getByRole("button", { name: /^Play / }).click();
  const picker = page.getByTestId("zone-picker");
  await expect(picker).toBeVisible();
  const heosRow = picker.getByTestId(`zone-row-${heos.id}`).getByRole("button").first();
  await expect(heosRow).toBeDisabled();
  await expect(heosRow).toContainText("YouTube Music isn't available on HEOS.");
  const sonosRow = picker.getByTestId(`zone-row-${sonos.id}`).getByRole("button").first();
  await expect(sonosRow).toBeEnabled();
  // the only room that can play it is pre-selected (UX U2): two taps
  await expect(sonosRow).toHaveAttribute("aria-pressed", "true");
  const acked = page.waitForResponse((r) => r.url().endsWith("/api/play") && r.request().method() === "POST");
  await picker.getByTestId("confirm-play").click();
  expect((await acked).ok()).toBe(true);
  // The mini-player shows the track now playing on that room (title + artist), not the container.
  await expect.poll(async () => ((await (await page.request.get("/api/devices")).json()).sides as (Side & { play_state: string })[]).find((s) => s.id === sonos.id)?.play_state, { timeout: 10_000 }).toBe("play");
  const mini = page.getByTestId("mini-player");
  await expect(mini).toBeVisible();
  await expect(mini).not.toHaveText("");
  await expect(mini.getByRole("button", { name: /pause/i }).first()).toBeVisible();
});

test("settings: the YouTube Music row unlinks through the confirm sheet and relinks from the row", async ({ page, request }) => {
  await page.goto("/settings");
  await expect(page.getByTestId("account-ytmusic")).toContainText("Connected");
  await page.getByTestId("account-ytmusic").click();
  await expect(page.getByTestId("unlink-sheet")).toContainText("Disconnect YouTube Music?");
  await page.getByTestId("confirm-unlink").click();
  await expect(page.getByTestId("account-ytmusic")).toContainText("Not connected");
  await page.getByTestId("account-ytmusic").click();
  await expect(page.getByTestId("link-sheet")).toContainText("Connect YouTube Music");
  await request.post("/api/dev/fake/approve_ytmusic", { headers: H });
  await expect(page.getByTestId("link-done")).toContainText("Connected", { timeout: 10_000 });
});
