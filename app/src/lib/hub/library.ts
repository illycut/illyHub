/**
 * Library, history, home, settings and auth client (Phase 3). Shapes are the hub's OpenAPI
 * contract (docs/api.md: Accounts, Browse, Play, History and home, Settings); the normalisers
 * here only fill defaults and coerce types. The one shape variation tolerated is a home section
 * arriving as a bare array (recents) versus `{items, needs_link}`. Unknown top-level keys are
 * logged in development so contract drift shows up early.
 */
import { hubHttpBase } from "./config";
import type { ArtRef } from "./types";

export type Service = "tidal" | "ytmusic" | "pandora";
export type ContentKind = "album" | "playlist" | "track" | "station";

export interface ContentRef {
  service: Service;
  kind: ContentKind;
  id: string;
}

export interface Availability {
  heos: boolean;
  sonos: boolean;
}

export interface LibraryItem {
  content_ref: ContentRef;
  title: string;
  subtitle: string | null;
  art: ArtRef;
  duration_ms: number | null;
  track_count: number | null;
  availability: Availability;
}

export interface TrackItem extends LibraryItem {
  /** Hub canonical 0-based position in its container; used for "play from here". */
  index: number;
  artist: string | null;
  album: string | null;
}

export interface Page<T> {
  items: T[];
  next_offset: number | null;
  total: number | null;
}

export interface Detail {
  item: LibraryItem;
  tracks: TrackItem[];
}

export interface HistoryItem {
  content_ref: ContentRef;
  title: string;
  subtitle: string | null;
  art: ArtRef;
  /** Side ids of the most recent play; the target picker pre-highlights them. */
  last_targets: string[];
  last_played_at: string;
  play_count: number;
  /** Per-vendor availability when the hub knows it (stations); null otherwise. */
  availability: Availability | null;
}

/** Per-vendor link state: true = browse ok, false = not linked / auth fault, null = adapter absent or errored. */
export interface VendorLinked {
  heos: boolean | null;
  sonos: boolean | null;
}

export interface Section<T> {
  items: T[];
  /** Service that must be linked before this section has content, or null. */
  needs_link: Service | null;
  /** Hub-side failure for this section only (the rest of home still rendered), or null. */
  error: string | null;
  /** Per-vendor link state for services linked in the vendor apps (Pandora), else null. */
  linked: VendorLinked | null;
}

export interface Home {
  recents: Section<HistoryItem>;
  playlists: Section<LibraryItem>;
  favorite_albums: Section<LibraryItem>;
  stations: Section<LibraryItem>;
}

export type AccountState = "linked" | "unlinked" | "pending" | "restoring";

export interface PendingLink {
  user_code: string;
  verification_url: string;
  expires_at: string | null;
}

export interface AccountStatus {
  service: Service | "heos_account";
  state: AccountState;
  linked: boolean;
  account_name: string | null;
  expires_at: string | null;
  pending: PendingLink | null;
  last_error: string | null;
  /** Pandora only: which vendor apps have it linked. */
  linked_by_vendor: VendorLinked | null;
}

export interface HubInfo {
  address: string | null;
  version: string | null;
  uptime_s: number | null;
  https: boolean;
  fake_devices: boolean;
}

export interface Hardware {
  id: string;
  name: string;
  vendor: string;
  kind: string | null;
  model: string | null;
  ip: string | null;
  online: boolean;
}

export interface Settings {
  accounts: AccountStatus[];
  hub: HubInfo;
  hardware: Hardware[];
}

export interface AuthStart {
  verification_url: string;
  user_code: string;
  expires_in_s: number;
  interval_s: number;
}

export class LibraryError extends Error {
  code: string;
  status: number;
  constructor(code: string, message: string, status: number) {
    super(message);
    this.name = "LibraryError";
    this.code = code;
    this.status = status;
  }
}

export type Fetcher = typeof fetch;

export interface CallOptions {
  fetcher?: Fetcher;
  /** Per-request deadline; a hung hub must never leave a loader spinning. */
  timeoutMs?: number;
}

export const REQUEST_TIMEOUT_MS = 8000;

/** Header the hub requires on state-changing requests (forces a CORS preflight). */
export const HUB_HEADER = { "X-Illyhub": "1" } as const;

export function refKey(ref: ContentRef): string {
  return `${ref.service}:${ref.kind}:${ref.id}`;
}

export function parseRefKey(key: string | null | undefined): ContentRef | null {
  if (!key) return null;
  const m = /^([a-z]+):([a-z]+):(.+)$/.exec(key);
  if (!m) return null;
  return { service: m[1] as Service, kind: m[2] as ContentKind, id: m[3]! };
}

/** AbortSignal that fires after `ms` with a TimeoutError reason (portable: no AbortSignal.timeout needed). */
export function timeoutSignal(ms: number): AbortSignal | undefined {
  if (typeof AbortController === "undefined") return undefined;
  const c = new AbortController();
  const reason = typeof DOMException !== "undefined" ? new DOMException("The hub took too long to answer.", "TimeoutError") : new Error("TimeoutError");
  setTimeout(() => c.abort(reason), ms);
  return c.signal;
}

async function parseBody(res: Response): Promise<unknown> {
  const text = await res.text();
  try {
    return text ? JSON.parse(text) : null;
  } catch {
    return null;
  }
}

function raise(res: Response, body: unknown): never {
  const env = (body ?? {}) as { code?: string; message?: string; detail?: string };
  throw new LibraryError(env.code ?? `http_${res.status}`, env.message ?? env.detail ?? `The hub answered ${res.status}.`, res.status);
}

async function getJson<T>(path: string, opts: CallOptions): Promise<T> {
  const res = await (opts.fetcher ?? fetch)(hubHttpBase() + path, {
    headers: { accept: "application/json" },
    signal: timeoutSignal(opts.timeoutMs ?? REQUEST_TIMEOUT_MS),
  });
  const body = await parseBody(res);
  if (!res.ok) raise(res, body);
  return body as T;
}

async function postJson<T>(path: string, body: unknown, opts: CallOptions): Promise<T> {
  const res = await (opts.fetcher ?? fetch)(hubHttpBase() + path, {
    method: "POST",
    headers: { "content-type": "application/json", accept: "application/json", ...HUB_HEADER },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: timeoutSignal(opts.timeoutMs ?? REQUEST_TIMEOUT_MS),
  });
  const parsed = await parseBody(res);
  if (!res.ok) raise(res, parsed);
  return parsed as T;
}

// ---------------------------------------------------------------- normalisers

const EMPTY_ART: ArtRef = { url: null, accent: null, accent_is_safe: false };

/** Development-only contract drift alarm: keys the client does not know about. */
export function warnUnknownKeys(x: unknown, known: readonly string[], context: string): void {
  if (process.env.NODE_ENV === "production" || !x || typeof x !== "object" || Array.isArray(x)) return;
  const extra = Object.keys(x as Record<string, unknown>).filter((k) => !known.includes(k));
  if (extra.length) console.warn(`[illyhub] ${context}: unknown keys ${extra.join(", ")}`);
}

function asArt(x: unknown): ArtRef {
  if (!x || typeof x !== "object") return EMPTY_ART;
  const a = x as Partial<ArtRef>;
  return { url: a.url ?? null, accent: a.accent ?? null, accent_is_safe: !!a.accent_is_safe, cache_key: a.cache_key ?? null };
}

/**
 * Availability defaults to true per ecosystem when the hub omits it: the hub only sends `false`
 * when it knows the ecosystem lacks the service, so a missing field must not grey out a room.
 */
function asAvailability(x: unknown): Availability {
  const a = (x ?? {}) as Partial<Availability>;
  return { heos: a.heos ?? true, sonos: a.sonos ?? true };
}

const ITEM_KEYS = ["content_ref", "title", "subtitle", "art", "duration_ms", "track_count", "availability", "index", "album_id", "artist", "album"] as const;

export function asItem(x: unknown): LibraryItem {
  const r = (x ?? {}) as Record<string, unknown>;
  warnUnknownKeys(r, ITEM_KEYS, "BrowseItem");
  return {
    content_ref: r.content_ref as ContentRef,
    title: String(r.title ?? ""),
    subtitle: (r.subtitle as string | null | undefined) ?? null,
    art: asArt(r.art),
    duration_ms: (r.duration_ms as number | null | undefined) ?? null,
    track_count: (r.track_count as number | null | undefined) ?? null,
    availability: asAvailability(r.availability),
  };
}

export function asTrack(x: unknown, position: number): TrackItem {
  const r = (x ?? {}) as Record<string, unknown>;
  return {
    ...asItem(x),
    index: typeof r.index === "number" ? r.index : position,
    artist: (r.artist as string | null | undefined) ?? null,
    album: (r.album as string | null | undefined) ?? null,
  };
}

export function asPage<T>(x: unknown, map: (v: unknown, i: number) => T): Page<T> {
  const r = (x ?? {}) as Record<string, unknown>;
  warnUnknownKeys(r, ["items", "offset", "limit", "total", "next_offset"], "BrowsePage");
  const items = Array.isArray(r.items) ? (r.items as unknown[]).map(map) : [];
  return { items, next_offset: (r.next_offset as number | null | undefined) ?? null, total: (r.total as number | null | undefined) ?? null };
}

function asVendorLinked(x: unknown): VendorLinked | null {
  if (!x || typeof x !== "object") return null;
  const l = x as Record<string, unknown>;
  const one = (v: unknown) => (typeof v === "boolean" ? v : null);
  return { heos: one(l.heos), sonos: one(l.sonos) };
}

/** Home sections: `recents` is a bare array, the others are `{items, needs_link, error, linked}`. */
export function asSection<T>(x: unknown, map: (v: unknown, i: number) => T): Section<T> {
  if (Array.isArray(x)) return { items: x.map(map), needs_link: null, error: null, linked: null };
  const r = (x ?? {}) as Record<string, unknown>;
  warnUnknownKeys(r, ["items", "needs_link", "error", "linked"], "HomeSection");
  return {
    items: Array.isArray(r.items) ? (r.items as unknown[]).map(map) : [],
    needs_link: (r.needs_link as Service | null | undefined) ?? null,
    error: (r.error as string | null | undefined) ?? null,
    linked: asVendorLinked(r.linked),
  };
}

export function asHistoryItem(x: unknown): HistoryItem {
  const r = (x ?? {}) as Record<string, unknown>;
  warnUnknownKeys(r, ["content_ref", "title", "subtitle", "art", "last_played_at", "last_targets", "play_count", "sync", "availability"], "HistoryItem");
  return {
    content_ref: r.content_ref as ContentRef,
    title: String(r.title ?? ""),
    subtitle: (r.subtitle as string | null | undefined) ?? null,
    art: asArt(r.art),
    last_targets: Array.isArray(r.last_targets) ? (r.last_targets as string[]) : [],
    last_played_at: String(r.last_played_at ?? ""),
    play_count: typeof r.play_count === "number" ? r.play_count : 0,
    availability: r.availability && typeof r.availability === "object" ? asAvailability(r.availability) : null,
  };
}

export function asDetail(x: unknown): Detail {
  const r = (x ?? {}) as Record<string, unknown>;
  warnUnknownKeys(r, ["item", "tracks"], "Container");
  return { item: asItem(r.item), tracks: Array.isArray(r.tracks) ? (r.tracks as unknown[]).map(asTrack) : [] };
}

export function asHome(x: unknown): Home {
  const r = (x ?? {}) as Record<string, unknown>;
  warnUnknownKeys(r, ["recents", "playlists", "favorite_albums", "stations"], "HomeResponse");
  return {
    recents: asSection(r.recents, asHistoryItem),
    playlists: asSection(r.playlists, asItem),
    favorite_albums: asSection(r.favorite_albums, asItem),
    stations: asSection(r.stations, asItem),
  };
}

function asPending(x: unknown): PendingLink | null {
  if (!x || typeof x !== "object") return null;
  const p = x as Record<string, unknown>;
  return { user_code: String(p.user_code ?? ""), verification_url: String(p.verification_url ?? ""), expires_at: (p.expires_at as string | null | undefined) ?? null };
}

export function asAccountStatus(x: unknown): AccountStatus {
  const r = (x ?? {}) as Record<string, unknown>;
  warnUnknownKeys(r, ["service", "state", "linked", "account_name", "expires_at", "pending", "last_error", "linked_by_vendor"], "AccountStatus");
  const linked = !!r.linked;
  const pending = asPending(r.pending);
  const state = (r.state as AccountState | undefined) ?? (linked ? "linked" : pending ? "pending" : "unlinked");
  return {
    service: String(r.service) as AccountStatus["service"],
    state,
    linked,
    account_name: (r.account_name as string | null | undefined) ?? null,
    expires_at: (r.expires_at as string | null | undefined) ?? null,
    pending,
    last_error: (r.last_error as string | null | undefined) ?? null,
    linked_by_vendor: asVendorLinked(r.linked_by_vendor),
  };
}

export function asSettings(x: unknown): Settings {
  const r = (x ?? {}) as Record<string, unknown>;
  warnUnknownKeys(r, ["accounts", "hub", "hardware"], "SettingsResponse");
  const h = (r.hub ?? {}) as Record<string, unknown>;
  warnUnknownKeys(h, ["address", "port", "https", "version", "uptime_s", "fake_devices"], "HubInfo");
  const address = (h.address as string | null | undefined) ?? null;
  const hw = Array.isArray(r.hardware) ? (r.hardware as Record<string, unknown>[]) : [];
  return {
    accounts: Array.isArray(r.accounts) ? (r.accounts as unknown[]).map(asAccountStatus) : [],
    hub: {
      address: address ? (h.port ? `${address}:${h.port}` : address) : null,
      version: (h.version as string | null | undefined) ?? null,
      uptime_s: (h.uptime_s as number | null | undefined) ?? null,
      https: !!h.https,
      fake_devices: !!h.fake_devices,
    },
    hardware: hw.map((d, i) => {
      warnUnknownKeys(d, ["id", "name", "vendor", "kind", "model", "ip", "online"], "HardwareItem");
      return {
        id: String(d.id ?? i),
        name: String(d.name ?? "Device"),
        vendor: String(d.vendor ?? ""),
        kind: (d.kind as string | null | undefined) ?? null,
        model: (d.model as string | null | undefined) ?? null,
        ip: (d.ip as string | null | undefined) ?? null,
        online: d.online !== false,
      };
    }),
  };
}

export function asAuthStart(x: unknown): AuthStart {
  const r = (x ?? {}) as Record<string, unknown>;
  warnUnknownKeys(r, ["service", "user_code", "verification_url", "expires_in_s", "interval_s"], "AuthStart");
  return {
    verification_url: String(r.verification_url ?? ""),
    user_code: String(r.user_code ?? ""),
    expires_in_s: typeof r.expires_in_s === "number" ? r.expires_in_s : 300,
    interval_s: typeof r.interval_s === "number" && r.interval_s > 0 ? r.interval_s : 2,
  };
}

// ---------------------------------------------------------------- calls

export const library = {
  home: async (opts: CallOptions = {}): Promise<Home> => asHome(await getJson("/api/home", opts)),
  detail: async (ref: ContentRef, opts: CallOptions = {}): Promise<Detail> =>
    asDetail(await getJson(`/api/browse/${ref.service}/${ref.kind}/${encodeURIComponent(ref.id)}`, opts)),
  settings: async (opts: CallOptions = {}): Promise<Settings> => asSettings(await getJson("/api/settings", opts)),
  authStart: async (service: Service, opts: CallOptions = {}): Promise<AuthStart> => asAuthStart(await postJson(`/api/auth/${service}/start`, undefined, opts)),
  authStatus: async (service: Service, opts: CallOptions = {}): Promise<AccountStatus> => asAccountStatus(await getJson(`/api/auth/${service}/status`, opts)),
  authUnlink: async (service: Service, opts: CallOptions = {}): Promise<AccountStatus> => asAccountStatus(await postJson(`/api/auth/${service}/unlink`, undefined, opts)),
  restart: async (opts: CallOptions = {}): Promise<void> => {
    await postJson("/api/hub/restart", undefined, opts);
  },
};
