/**
 * Contract tests: the normalisers are exercised with payloads shaped exactly like the hub's
 * OpenAPI schemas (src/lib/hub/openapi.json), so a hub change to a field name fails here first.
 */
import openapi from "./openapi.json";
import { asAccountStatus, asAuthStart, asDetail, asHistoryItem, asHome, asPage, asSection, asSettings, library, LibraryError, parseRefKey, refKey, timeoutSignal, warnUnknownKeys, type ContentRef } from "./library";
import { jsonResponse } from "@/test/fixtures";

type Schema = { properties?: Record<string, unknown>; required?: string[] };
const schemas = (openapi as { components: { schemas: Record<string, Schema> } }).components.schemas;
const keysOf = (name: string) => Object.keys(schemas[name]?.properties ?? {});

const ref: ContentRef = { service: "tidal", kind: "album", id: "101" };
const art = { url: "/api/art/aaaaaaaaaaaaaaaaaaaaaaaa", cache_key: "aaaaaaaaaaaaaaaaaaaaaaaa", accent: null, accent_is_safe: false };
/** A BrowseItem exactly as the hub documents it. */
const browseItem = {
  content_ref: ref,
  title: "Warm Glow",
  subtitle: "Analog Heart",
  art,
  duration_ms: 1_926_000,
  track_count: 9,
  availability: { heos: true, sonos: true },
  index: null,
  album_id: null,
  artist: "Analog Heart",
  album: null,
};
const track = { ...browseItem, content_ref: { ...ref, kind: "track", id: "t1" }, title: "Signal 1", index: 4, album_id: "101", duration_ms: 214_000 };
const historyItem = { content_ref: ref, title: "Warm Glow", subtitle: "Analog Heart", art, last_played_at: "2026-09-07T22:10:04.120Z", last_targets: ["heos:heos-1"], play_count: 3, sync: false };
const account = { service: "tidal", state: "linked", linked: true, account_name: "james", expires_at: "2026-09-08T00:00:00Z", pending: null, last_error: null };
const settingsBody = {
  accounts: [account, { service: "ytmusic", state: "unlinked", linked: false, account_name: null, expires_at: null, pending: null, last_error: null }],
  hub: { address: "192.168.1.10", port: 8080, https: false, version: "0.1.0", uptime_s: 4021.3, fake_devices: false },
  hardware: [
    { id: "heos-1", name: "Living Room Amp", vendor: "heos", kind: "player", model: "Denon AVR-X3700H", ip: "10.0.0.5", online: true },
    { id: "denon-10.0.0.5:main", name: "Main zone", vendor: "denon", kind: "zone", model: null, ip: "10.0.0.5", online: false },
  ],
};

describe("contract: fixtures match the OpenAPI schema key sets", () => {
  it.each([
    ["BrowseItem", browseItem],
    ["HistoryItem", historyItem],
    ["AccountStatus", account],
    ["HubInfo", settingsBody.hub],
    ["HardwareItem", settingsBody.hardware[0]!],
  ])("%s", (name, fixture: object) => {
    const documented = keysOf(name);
    if (documented.length === 0) return; // schema not exposed by this hub build
    for (const k of Object.keys(fixture)) if (k !== "state") expect(documented).toContain(k);
  });
});

describe("ref keys", () => {
  it("round-trips service:kind:id, including ids with colons", () => {
    expect(refKey(ref)).toBe("tidal:album:101");
    expect(parseRefKey("tidal:album:101")).toEqual(ref);
    expect(parseRefKey("tidal:playlist:uuid:with:colons")).toEqual({ service: "tidal", kind: "playlist", id: "uuid:with:colons" });
    expect(parseRefKey("garbage")).toBeNull();
    expect(parseRefKey(null)).toBeNull();
  });
});

describe("normalisers (documented shapes only)", () => {
  it("home: recents is a bare array; other sections are {items, needs_link, error}", () => {
    const h = asHome({ recents: [historyItem], playlists: { items: [browseItem], needs_link: [] }, favorite_albums: { items: [], needs_link: ["tidal"] }, stations: { items: [], needs_link: [], error: "Pandora timed out." } });
    expect(h.recents.items[0]?.last_targets).toEqual(["heos:heos-1"]);
    expect(h.playlists.items[0]?.title).toBe("Warm Glow");
    expect(h.favorite_albums.needs_link).toEqual(["tidal"]);
    // a lone string from an older hub and null both normalise to a list
    expect(asSection({ items: [], needs_link: "tidal" }, (x) => x).needs_link).toEqual(["tidal"]);
    expect(asSection({ items: [], needs_link: null }, (x) => x).needs_link).toEqual([]);
    expect(asSection({ items: [], linked: { tidal: true, ytmusic: null } }, (x) => x).linked).toEqual({ tidal: true, ytmusic: null });
    expect(h.stations.error).toBe("Pandora timed out.");
    expect(asSection(undefined, (x) => x)).toEqual({ items: [], needs_link: [], error: null, linked: null });
    expect(asSection({ items: [], linked: { heos: false, sonos: "x" } }, (x) => x).linked).toEqual({ heos: false, sonos: null });
  });

  it("browse page carries next_offset and total", () => {
    expect(asPage({ items: [browseItem], offset: 0, limit: 50, total: 6, next_offset: null }, (x) => x)).toMatchObject({ next_offset: null, total: 6 });
    expect(asPage({ items: [], offset: 50, limit: 50, total: 120, next_offset: 100 }, (x) => x).next_offset).toBe(100);
  });

  it("container: item + tracks[]; tracks keep the hub's canonical index", () => {
    const d = asDetail({ item: browseItem, tracks: [track] });
    expect(d.item.track_count).toBe(9);
    expect(d.tracks[0]).toMatchObject({ index: 4, title: "Signal 1", artist: "Analog Heart", album: null, duration_ms: 214_000 });
  });

  it("history item keeps last_played_at, last_targets and play_count", () => {
    expect(asHistoryItem(historyItem)).toMatchObject({ last_played_at: "2026-09-07T22:10:04.120Z", last_targets: ["heos:heos-1"], play_count: 3 });
  });

  it("availability defaults to true per ecosystem only when the hub omits the field", () => {
    const { availability, ...noAvail } = browseItem;
    void availability;
    expect(asHome({ recents: [], playlists: { items: [noAvail], needs_link: [] }, favorite_albums: { items: [], needs_link: [] }, stations: { items: [], needs_link: [] } }).playlists.items[0]?.availability).toEqual({ heos: true, sonos: true, reasons: null });
    expect(asHome({ recents: [], playlists: { items: [{ ...browseItem, availability: { heos: false, sonos: true } }], needs_link: [] }, favorite_albums: { items: [], needs_link: [] }, stations: { items: [], needs_link: [] } }).playlists.items[0]?.availability).toEqual({ heos: false, sonos: true, reasons: null });
    expect(asHome({ recents: [], playlists: { items: [{ ...browseItem, availability: { heos: false, sonos: true, reasons: { heos: "unsupported", sonos: null } } }], needs_link: [] }, favorite_albums: { items: [], needs_link: [] }, stations: { items: [], needs_link: [] } }).playlists.items[0]?.availability).toEqual({ heos: false, sonos: true, reasons: { heos: "unsupported", sonos: null } });
  });

  it("account status: state from the hub, pending as an object, last_error surfaced; derives state when absent", () => {
    expect(asAccountStatus(account)).toMatchObject({ state: "linked", linked: true, account_name: "james", pending: null });
    const pending = asAccountStatus({ service: "tidal", state: "pending", linked: false, account_name: null, expires_at: null, pending: { user_code: "ABCDE", verification_url: "https://link.tidal.com/", expires_at: "2026-09-07T23:00:00Z" }, last_error: null });
    expect(pending.state).toBe("pending");
    expect(pending.pending).toEqual({ user_code: "ABCDE", verification_url: "https://link.tidal.com/", expires_at: "2026-09-07T23:00:00Z" });
    expect(asAccountStatus({ service: "tidal", linked: false, pending: { user_code: "X", verification_url: "u" } }).state).toBe("pending");
    expect(asAccountStatus({ service: "tidal", linked: false, last_error: "Token refresh failed" })).toMatchObject({ state: "unlinked", last_error: "Token refresh failed" });
  });

  it("settings: address joins host and port; hardware keeps kind/ip/online", () => {
    const s = asSettings(settingsBody);
    // `airplay` (Phase 7) defaults to off when the hub omits it.
    expect(s.hub).toEqual({ address: "192.168.1.10:8080", version: "0.1.0", uptime_s: 4021.3, https: false, fake_devices: false, airplay: { enabled: false, available: false, reason: null } });
    expect(s.hardware[1]).toEqual({ id: "denon-10.0.0.5:main", name: "Main zone", vendor: "denon", kind: "zone", model: null, ip: "10.0.0.5", online: false });
    expect(s.accounts[1]?.state).toBe("unlinked");
  });

  it("auth start: expires_in_s and interval_s as documented, with safe fallbacks", () => {
    expect(asAuthStart({ service: "tidal", user_code: "ABCDE", verification_url: "https://link.tidal.com/", expires_in_s: 300, interval_s: 2 })).toEqual({ user_code: "ABCDE", verification_url: "https://link.tidal.com/", expires_in_s: 300, interval_s: 2 });
    expect(asAuthStart({ user_code: "X", verification_url: "u" })).toMatchObject({ expires_in_s: 300, interval_s: 2 });
  });

  it("warns (dev only) about unknown top-level keys instead of silently accepting aliases", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    warnUnknownKeys({ albums: [], recents: [] }, ["recents"], "HomeResponse");
    expect(warn).toHaveBeenCalledWith(expect.stringContaining("HomeResponse: unknown keys albums"));
    warn.mockClear();
    asHistoryItem({ ...historyItem, played_at: "legacy" });
    expect(warn).toHaveBeenCalledWith(expect.stringContaining("played_at"));
    // the legacy field is ignored, not read
    expect(asHistoryItem({ ...historyItem, last_played_at: undefined, played_at: "legacy" }).last_played_at).toBe("");
    warn.mockRestore();
  });
});

describe("calls", () => {
  const calls: { m: string; url: string; headers: Record<string, string> }[] = [];
  const fetcher = (async (url: RequestInfo | URL, init?: RequestInit) => {
    const u = String(url).replace(/^https?:\/\/[^/]+/, "");
    calls.push({ m: init?.method ?? "GET", url: u, headers: (init?.headers ?? {}) as Record<string, string> });
    if (u === "/api/home") return jsonResponse({ recents: [], playlists: { items: [browseItem], needs_link: [] }, favorite_albums: { items: [], needs_link: [] }, stations: { items: [], needs_link: [] } });
    if (u === "/api/browse/tidal/album/101") return jsonResponse({ item: browseItem, tracks: [track] });
    if (u === "/api/browse/tidal/album/missing") return jsonResponse({ code: "needs_link", message: "Tidal is not connected. Link it in Settings.", service: "tidal" }, 409);
    if (u === "/api/settings") return jsonResponse(settingsBody);
    if (u === "/api/auth/tidal/start") return jsonResponse({ service: "tidal", user_code: "ABCDE", verification_url: "https://link.tidal.com/", expires_in_s: 300, interval_s: 2 });
    if (u === "/api/auth/tidal/status") return jsonResponse(account);
    if (u === "/api/auth/tidal/unlink") return jsonResponse({ ...account, state: "unlinked", linked: false, account_name: null });
    if (u === "/api/hub/restart") return jsonResponse({ restarting: true }, 202);
    if (u === "/api/hub/update/apply") return jsonResponse({ job_id: "j1", state: "running", message: "Updating." }, 202);
    if (u === "/api/hub/update/check") return jsonResponse({ current: { version: "0.8.0", commit: "a", branch: "main" }, remote: { commit: "b", ahead_by: 1, summary: ["x"], tracking: "main" }, available: true, last_checked_at: "2026-09-08T00:00:00Z", error: null });
    if (u === "/api/hub/update/status") return jsonResponse({ state: "running", job_id: "j1", started_at: null, finished_at: null, log_tail: ["a", "b"], message: null });
    if (u === "/api/queue/jump") return jsonResponse({ correlation_id: "c", ok: true, action: "queue_jump", target: "sonos:x", state_version: 1, error: null, partial: [], latency_ms: 3 });
    if (u === "/api/playmode") return jsonResponse({ correlation_id: "c", ok: true, action: "playmode", target: "sonos:x", state_version: 1, error: null, partial: [], latency_ms: 3 });
    if (u === "/api/metrics/summary") return new Response("Running since 7:37 today.", { status: 200, headers: { "content-type": "text/plain" } });
    if (u === "/api/metrics/summary?json") return jsonResponse({ summary: "Ten commands today." });
    if (u === "/api/metrics/summary?fail") return new Response("nope", { status: 503, headers: { "content-type": "text/plain" } });
    return new Response("not json", { status: 500 });
  }) as typeof fetch;

  beforeEach(() => {
    calls.length = 0;
  });

  it("hits the documented paths, sends X-Illyhub on every POST, and normalises", async () => {
    expect((await library.home({ fetcher })).playlists.items[0]?.title).toBe("Warm Glow");
    expect((await library.detail(ref, { fetcher })).tracks[0]?.index).toBe(4);
    expect((await library.settings({ fetcher })).hub.address).toBe("192.168.1.10:8080");
    expect(await library.authStart("tidal", { fetcher })).toMatchObject({ user_code: "ABCDE", interval_s: 2 });
    expect((await library.authStatus("tidal", { fetcher })).linked).toBe(true);
    expect((await library.authUnlink("tidal", { fetcher })).linked).toBe(false);
    await library.restart({ fetcher });
    // Phase 8
    const chk = await library.updateCheck({ fetcher });
    expect(chk.remote.summary).toBe("x");
    expect(chk.remote.tracking).toBe("main");
    expect((await library.updateApply({ fetcher })).job_id).toBe("j1");
    expect((await library.updateStatus({ fetcher })).log_tail).toBe("a\nb");
    expect(calls.map((c) => `${c.m} ${c.url}`)).toEqual([
      "GET /api/home",
      "GET /api/browse/tidal/album/101",
      "GET /api/settings",
      "POST /api/auth/tidal/start",
      "GET /api/auth/tidal/status",
      "POST /api/auth/tidal/unlink",
      "POST /api/hub/restart",
      "GET /api/hub/update/check",
      "POST /api/hub/update/apply",
      "GET /api/hub/update/status",
    ]);
    expect(calls.filter((c) => c.m === "POST")).toHaveLength(4);
    for (const c of calls.filter((c) => c.m === "POST")) expect(c.headers["X-Illyhub"]).toBe("1");
  });

  it("metrics summary accepts plain text or {summary}, and a non-2xx plain-text answer raises http_<status>", async () => {
    expect(await library.metricsSummary({ fetcher })).toBe("Running since 7:37 today.");
    const f2 = (async () => jsonResponse({ summary: "Ten commands today." })) as typeof fetch;
    expect(await library.metricsSummary({ fetcher: f2 })).toBe("Ten commands today.");
    const f3 = (async () => new Response("nope", { status: 503 })) as typeof fetch;
    const err = await library.metricsSummary({ fetcher: f3 }).catch((e) => e);
    expect(err).toBeInstanceOf(LibraryError);
    expect(err.code).toBe("http_503");
  });

  it("surfaces the hub error envelope as LibraryError, and a non-JSON failure as http_<status>", async () => {
    await expect(library.detail({ ...ref, id: "missing" }, { fetcher })).rejects.toMatchObject({ code: "needs_link", message: "Tidal is not connected. Link it in Settings.", status: 409 });
    const err = await library.detail({ ...ref, id: "boom" }, { fetcher }).catch((e) => e);
    expect(err).toBeInstanceOf(LibraryError);
    expect(err.code).toBe("http_500");
  });

  it("every request carries an abort signal that fires as a TimeoutError after the deadline", async () => {
    vi.useFakeTimers();
    const sig = timeoutSignal(50)!;
    expect(sig.aborted).toBe(false);
    vi.advanceTimersByTime(60);
    expect(sig.aborted).toBe(true);
    expect((sig.reason as { name?: string }).name).toBe("TimeoutError");
    vi.useRealTimers();
    let seen: AbortSignal | null | undefined;
    const f = (async (_u: RequestInfo | URL, init?: RequestInit) => {
      seen = init?.signal;
      return jsonResponse(settingsBody);
    }) as typeof fetch;
    await library.settings({ fetcher: f, timeoutMs: 1000 });
    expect(seen).toBeInstanceOf(AbortSignal);
  });
});
