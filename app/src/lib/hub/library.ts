/**
 * Library, history, home, settings and auth client (Phase 3). Shapes are the hub's OpenAPI
 * contract (docs/api.md: Accounts, Browse, Play, History and home, Settings); the normalisers
 * here only fill defaults and coerce types. The one shape variation tolerated is a home section
 * arriving as a bare array (recents) versus `{items, needs_link}`. Unknown top-level keys are
 * logged in development so contract drift shows up early.
 */
import { hubHttpBase } from "./config";
import type { components } from "./openapi";
import type { AirPlayOutput, ArtRef } from "./types";

export type Service = "tidal" | "ytmusic" | "pandora";
export type ContentKind = "album" | "playlist" | "track" | "station";

export interface ContentRef {
  service: Service;
  kind: ContentKind;
  id: string;
}

export type AvailabilityReasonCode = "unsupported" | "not_linked" | "not_in_account" | "auth_fault";

export interface Availability {
  heos: boolean;
  sonos: boolean;
  /** Why a vendor cannot play it, per vendor; null when it can (or the hub did not say). */
  reasons?: { heos: AvailabilityReasonCode | null; sonos: AvailabilityReasonCode | null } | null;
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
  /** The service returned more tracks than the hub's cap and the list was cut. */
  truncated: boolean;
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

/**
 * Link state per key: true = browse ok, false = not linked / auth fault, null = errored or absent.
 * Library sections key by service (`tidal`, `ytmusic`); the stations section keys by vendor
 * (`heos`, `sonos`).
 */
export type LinkedMap = Record<string, boolean | null>;
/** Vendor-keyed view of a LinkedMap (stations section). */
export interface VendorLinked {
  heos: boolean | null;
  sonos: boolean | null;
}

export interface Section<T> {
  items: T[];
  /** Hub-linked services that contribute to this section and are currently unlinked; [] when none. */
  needs_link: Service[];
  /** Hub-side failure for this section only (the rest of home still rendered), or null. */
  error: string | null;
  /** Link state per service (library sections) or per vendor (stations); null when not reported. */
  linked: LinkedMap | null;
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
  /** Machine code for `last_error` (e.g. `needs_client_config`), or null. */
  last_error_code: string | null;
  /** Pandora only: which vendor apps have it linked. */
  linked_by_vendor: VendorLinked | null;
}

/** AirPlay bridge capability as Settings reports it (docs/api.md "AirPlay", Phase 7, experimental). */
export interface AirPlayInfo {
  /** HUB_AIRPLAY_ENABLED on the hub. Off means the hub never touches the Mac's Music app. */
  enabled: boolean;
  /** Enabled and the Music app answered; false with `reason` otherwise. */
  available: boolean;
  reason: string | null;
}

/** GET /api/airplay: the bridge state plus the Music app's AirPlay outputs (read-only in the app). */
export interface AirPlayStatus extends AirPlayInfo {
  outputs: AirPlayOutput[];
}

export interface HubInfo {
  address: string | null;
  version: string | null;
  uptime_s: number | null;
  https: boolean;
  fake_devices: boolean;
  airplay: AirPlayInfo;
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


// ---------------------------------------------------------------- Phase 8 shapes (queue, search, play mode, update, metrics)

/** One entry of a side's queue (docs/api.md "Queue"; `GET /api/queue/{side_id}`). */
export interface QueueItem {
  /** Hub canonical 0-based position; `POST /api/queue/jump` takes it as `index`. */
  index: number;
  title: string;
  artist: string | null;
  album: string | null;
  duration_ms: number | null;
  art: ArtRef;
  track_id: string | null;
  content_ref: ContentRef | null;
}

export interface Queue {
  items: QueueItem[];
  /** Index of the entry now playing, or null when the hub cannot tell. */
  current_index: number | null;
  /** Service badge for the queue as a whole (tidal, ytmusic, pandora), or null. */
  source: string | null;
  /** The hub capped the list (HUB_QUEUE_MAX). */
  truncated: boolean;
}

/** `Side.play_mode` and `POST /api/playmode` (docs/api.md "Play mode"): the generated schema. */
export type PlayMode = components["schemas"]["PlayMode"];
export type RepeatMode = PlayMode["repeat"];

/** `GET /api/search?q=` (docs/api.md "Search"): grouped hits plus per-service failures. */
export interface SearchResults {
  query: string;
  albums: LibraryItem[];
  playlists: LibraryItem[];
  tracks: TrackItem[];
  stations: LibraryItem[];
  /** Services the hub asked (linked at the time). */
  services: Service[];
  /** Services that did not answer within the hub's deadline, with the hub's sentence. */
  errors: Partial<Record<Service, string>>;
  /** True when at least one linked service failed. */
  partial: boolean;
}

/** `GET /api/hub/update/check` (docs/api.md "Update", PRD SET-2). */
export interface UpdateCheck {
  current: { version: string | null; commit: string | null; branch: string | null };
  /**
   * `summary` is the hub's list of commit subjects, joined for display; `tracking` names the branch
   * the hub compared against (the current branch, or `HUB_UPDATE_BRANCH` when it has no upstream).
   */
  remote: { commit: string | null; ahead_by: number; summary: string | null; tracking: string | null };
  available: boolean;
  last_checked_at: string | null;
  /** One sentence (`templates.hub.update_check_failed`) when git failed, or null. Never shown raw. */
  error: string | null;
}

/** `up_to_date` and `failed` leave the hub running; `succeeded` and `rolled_back` make it exit for launchd. */
export type UpdateJobState = components["schemas"]["UpdateStatus"]["state"];

/** `GET /api/hub/update/status` (docs/api.md "Update"). */
export interface UpdateStatus {
  state: UpdateJobState;
  /** The hub's `log_tail` lines, joined. */
  log_tail: string;
  started_at: string | null;
  finished_at: string | null;
  job_id: string | null;
  /** The hub's one-line outcome, when it gives one. */
  message: string | null;
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

/** GET that accepts JSON or plain text (the metrics summary is prose). */
async function getJsonOrText(path: string, opts: CallOptions): Promise<unknown> {
  const res = await (opts.fetcher ?? fetch)(hubHttpBase() + path, {
    headers: { accept: "application/json, text/plain" },
    signal: timeoutSignal(opts.timeoutMs ?? REQUEST_TIMEOUT_MS),
  });
  const text = await res.text();
  let body: unknown = text;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = text;
  }
  if (!res.ok) raise(res, typeof body === "string" ? null : body);
  return body;
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
const REASONS = new Set(["unsupported", "not_linked", "not_in_account", "auth_fault"]);
function asReason(x: unknown): AvailabilityReasonCode | null {
  return typeof x === "string" && REASONS.has(x) ? (x as AvailabilityReasonCode) : null;
}

function asAvailability(x: unknown): Availability {
  const a = (x ?? {}) as { heos?: boolean; sonos?: boolean; reasons?: Record<string, unknown> | null };
  const reasons = a.reasons && typeof a.reasons === "object" ? { heos: asReason(a.reasons.heos), sonos: asReason(a.reasons.sonos) } : null;
  return { heos: a.heos ?? true, sonos: a.sonos ?? true, reasons };
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

function asLinked(x: unknown): LinkedMap | null {
  if (!x || typeof x !== "object") return null;
  const out: LinkedMap = {};
  for (const [k, v] of Object.entries(x as Record<string, unknown>)) out[k] = typeof v === "boolean" ? v : null;
  return out;
}

function asVendorLinked(x: unknown): VendorLinked | null {
  const l = asLinked(x);
  return l ? { heos: l.heos ?? null, sonos: l.sonos ?? null } : null;
}

const SERVICES = new Set(["tidal", "ytmusic", "pandora"]);
/** `needs_link` is a list of services; a lone string (older hubs) and null both normalise. */
function asNeedsLink(x: unknown): Service[] {
  const arr = Array.isArray(x) ? x : x == null ? [] : [x];
  return arr.filter((s): s is Service => typeof s === "string" && SERVICES.has(s));
}

/** Home sections: `recents` is a bare array, the others are `{items, needs_link, error, linked}`. */
export function asSection<T>(x: unknown, map: (v: unknown, i: number) => T): Section<T> {
  if (Array.isArray(x)) return { items: x.map(map), needs_link: [], error: null, linked: null };
  const r = (x ?? {}) as Record<string, unknown>;
  warnUnknownKeys(r, ["items", "needs_link", "error", "linked"], "HomeSection");
  return {
    items: Array.isArray(r.items) ? (r.items as unknown[]).map(map) : [],
    needs_link: asNeedsLink(r.needs_link),
    error: (r.error as string | null | undefined) ?? null,
    linked: asLinked(r.linked),
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
  warnUnknownKeys(r, ["item", "tracks", "truncated"], "Container");
  return { item: asItem(r.item), tracks: Array.isArray(r.tracks) ? (r.tracks as unknown[]).map(asTrack) : [], truncated: r.truncated === true };
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
  warnUnknownKeys(r, ["service", "state", "linked", "account_name", "expires_at", "pending", "last_error", "last_error_code", "linked_by_vendor"], "AccountStatus");
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
    last_error_code: (r.last_error_code as string | null | undefined) ?? null,
    linked_by_vendor: asVendorLinked(r.linked_by_vendor),
  };
}

/** `hub.airplay` defaults to off when a hub predates the bridge. */
export function asAirPlayInfo(x: unknown): AirPlayInfo {
  const a = (x ?? {}) as Record<string, unknown>;
  warnUnknownKeys(a, ["enabled", "available", "reason", "outputs"], "AirPlayInfo");
  const enabled = a.enabled === true;
  return { enabled, available: enabled && a.available === true, reason: (a.reason as string | null | undefined) ?? null };
}

export function asAirPlayOutput(x: unknown, i: number): AirPlayOutput {
  const o = (x ?? {}) as Record<string, unknown>;
  warnUnknownKeys(o, ["id", "name", "kind", "kind_label", "selected", "active", "available", "volume"], "AirPlayOutput");
  return {
    id: String(o.id ?? o.name ?? i),
    name: String(o.name ?? o.id ?? "Output"),
    kind: (o.kind as string | null | undefined) ?? null,
    kind_label: (o.kind_label as string | null | undefined) ?? null,
    selected: o.selected === true,
    active: o.active === true,
    available: o.available !== false,
    volume: typeof o.volume === "number" ? o.volume : null,
  };
}

/** GET /api/airplay: `{enabled, available, reason, outputs}` (docs/api.md "AirPlay bridge"); `enabled` comes from the payload only. */
export function asAirPlayStatus(x: unknown): AirPlayStatus {
  const r = (x ?? {}) as Record<string, unknown>;
  const outputs = Array.isArray(r.outputs) ? (r.outputs as unknown[]).map(asAirPlayOutput) : [];
  return { ...asAirPlayInfo(r), outputs };
}

export function asSettings(x: unknown): Settings {
  const r = (x ?? {}) as Record<string, unknown>;
  warnUnknownKeys(r, ["accounts", "hub", "hardware"], "SettingsResponse");
  const h = (r.hub ?? {}) as Record<string, unknown>;
  warnUnknownKeys(h, ["address", "port", "https", "version", "uptime_s", "fake_devices", "airplay"], "HubInfo");
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
      airplay: asAirPlayInfo(h.airplay),
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


// ---------------------------------------------------------------- Phase 8 normalisers

export function asQueueItem(x: unknown, position: number): QueueItem {
  const r = (x ?? {}) as Record<string, unknown>;
  warnUnknownKeys(r, ["index", "title", "artist", "album", "duration_ms", "art", "track_id", "content_ref"], "QueueItem");
  return {
    index: typeof r.index === "number" ? r.index : position,
    title: String(r.title ?? ""),
    artist: (r.artist as string | null | undefined) ?? null,
    album: (r.album as string | null | undefined) ?? null,
    duration_ms: (r.duration_ms as number | null | undefined) ?? null,
    art: asArt(r.art),
    track_id: (r.track_id as string | null | undefined) ?? null,
    content_ref: (r.content_ref as ContentRef | null | undefined) ?? null,
  };
}

/** A queue payload (REST or the `queues.<side>` delta path); null/garbage reads as an empty queue. */
export function asQueue(x: unknown): Queue {
  const r = (x ?? {}) as Record<string, unknown>;
  // Matches the hub's QueueState (items, current_index, source, truncated, total, updated_at).
  warnUnknownKeys(r, ["items", "current_index", "source", "truncated", "total", "updated_at", "side_id"], "Queue");
  return {
    items: Array.isArray(r.items) ? (r.items as unknown[]).map(asQueueItem) : [],
    current_index: typeof r.current_index === "number" ? r.current_index : null,
    source: (r.source as string | null | undefined) ?? null,
    truncated: r.truncated === true,
  };
}

const REPEATS = new Set(["off", "one", "all"]);
/** `Side.play_mode`; absent reads as shuffle off / repeat off (a hub that predates play modes). */
export function asPlayMode(x: unknown): PlayMode {
  const r = (x ?? {}) as Record<string, unknown>;
  warnUnknownKeys(r, ["shuffle", "repeat"], "PlayMode");
  return { shuffle: r.shuffle === true, repeat: typeof r.repeat === "string" && REPEATS.has(r.repeat) ? (r.repeat as RepeatMode) : "off" };
}

export function asSearchResults(x: unknown): SearchResults {
  const r = (x ?? {}) as Record<string, unknown>;
  warnUnknownKeys(r, ["query", "albums", "playlists", "tracks", "stations", "services", "errors", "partial"], "SearchResults");
  const list = (v: unknown) => (Array.isArray(v) ? (v as unknown[]).map(asItem) : []);
  const errors: Partial<Record<Service, string>> = {};
  if (r.errors && typeof r.errors === "object") {
    for (const [k, v] of Object.entries(r.errors as Record<string, unknown>)) if (SERVICES.has(k) && typeof v === "string") errors[k as Service] = v;
  }
  const services = Array.isArray(r.services) ? (r.services as unknown[]).filter((x): x is Service => typeof x === "string" && SERVICES.has(x)) : [];
  return {
    query: String(r.query ?? ""),
    albums: list(r.albums),
    playlists: list(r.playlists),
    tracks: Array.isArray(r.tracks) ? (r.tracks as unknown[]).map(asTrack) : [],
    stations: list(r.stations),
    services,
    errors,
    partial: r.partial === true || Object.keys(errors).length > 0,
  };
}

export function asUpdateCheck(x: unknown): UpdateCheck {
  const r = (x ?? {}) as Record<string, unknown>;
  warnUnknownKeys(r, ["current", "remote", "available", "last_checked_at", "error"], "UpdateCheck");
  const cur = (r.current ?? {}) as Record<string, unknown>;
  const rem = (r.remote ?? {}) as Record<string, unknown>;
  warnUnknownKeys(cur, ["version", "commit", "branch"], "UpdateCheck.current");
  warnUnknownKeys(rem, ["commit", "ahead_by", "summary", "tracking"], "UpdateCheck.remote");
  const lines = Array.isArray(rem.summary) ? (rem.summary as unknown[]).map(String).filter(Boolean) : typeof rem.summary === "string" && rem.summary ? [rem.summary] : [];
  return {
    current: { version: (cur.version as string | null | undefined) ?? null, commit: (cur.commit as string | null | undefined) ?? null, branch: (cur.branch as string | null | undefined) ?? null },
    remote: { commit: (rem.commit as string | null | undefined) ?? null, ahead_by: typeof rem.ahead_by === "number" ? rem.ahead_by : 0, summary: lines.length ? lines.join(" · ") : null, tracking: (rem.tracking as string | null | undefined) ?? null },
    available: r.available === true,
    last_checked_at: (r.last_checked_at as string | null | undefined) ?? null,
    error: (r.error as string | null | undefined) ?? null,
  };
}

const JOB_STATES = new Set<string>(["idle", "running", "succeeded", "up_to_date", "failed", "rolled_back"] satisfies UpdateJobState[]);
export function asUpdateStatus(x: unknown): UpdateStatus {
  const r = (x ?? {}) as Record<string, unknown>;
  warnUnknownKeys(r, ["state", "log_tail", "started_at", "finished_at", "job_id", "message"], "UpdateStatus");
  return {
    state: typeof r.state === "string" && JOB_STATES.has(r.state) ? (r.state as UpdateJobState) : "idle",
    log_tail: typeof r.log_tail === "string" ? r.log_tail : Array.isArray(r.log_tail) ? (r.log_tail as unknown[]).map(String).join("\n") : "",
    started_at: (r.started_at as string | null | undefined) ?? null,
    finished_at: (r.finished_at as string | null | undefined) ?? null,
    job_id: (r.job_id as string | null | undefined) ?? null,
    message: (r.message as string | null | undefined) ?? null,
  };
}

/** `GET /api/metrics/summary`: either `{summary}` JSON or plain text. */
export function asMetricsSummary(x: unknown): string {
  if (typeof x === "string") return x.trim();
  const r = (x ?? {}) as Record<string, unknown>;
  return typeof r.summary === "string" ? r.summary.trim() : typeof r.text === "string" ? r.text.trim() : "";
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
  /** AirPlay bridge state and the Music app's outputs (Phase 7, read-only in the app). */
  airplay: async (opts: CallOptions = {}): Promise<AirPlayStatus> => asAirPlayStatus(await getJson("/api/airplay", opts)),
  /** A side's queue (Phase 8, docs/api.md "Queue"); live changes arrive on the `queues.<side>` delta path. */
  queue: async (sideId: string, opts: CallOptions = {}): Promise<Queue> => asQueue(await getJson(`/api/queue/${encodeURIComponent(sideId)}`, opts)),
  /** Fan-out search across linked services (Phase 8, docs/api.md "Search"); partial results carry `errors`. */
  search: async (q: string, opts: CallOptions & { limit?: number } = {}): Promise<SearchResults> =>
    asSearchResults(await getJson(`/api/search?q=${encodeURIComponent(q)}${opts.limit ? `&limit=${opts.limit}` : ""}`, opts)),
  /** Self-update (PRD SET-2, docs/api.md "Update"). */
  updateCheck: async (opts: CallOptions = {}): Promise<UpdateCheck> => asUpdateCheck(await getJson("/api/hub/update/check", opts)),
  updateApply: async (opts: CallOptions = {}): Promise<UpdateStatus> => asUpdateStatus(await postJson("/api/hub/update/apply", undefined, opts)),
  updateStatus: async (opts: CallOptions = {}): Promise<UpdateStatus> => asUpdateStatus(await getJson("/api/hub/update/status", opts)),
  /** One-paragraph hub stats (docs/api.md "Metrics"). */
  metricsSummary: async (opts: CallOptions = {}): Promise<string> => asMetricsSummary(await getJsonOrText("/api/metrics/summary", opts)),
};
