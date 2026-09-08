/**
 * Library data store: home, browse details, settings. SWR-style: cached values render
 * immediately, a refresh runs on mount, on window focus/visibility, and after any play.
 * Errors keep the last good value and surface a message plus the hub's error code;
 * `needs_link` sections are data, not errors.
 *
 * Every request carries a deadline (library REQUEST_TIMEOUT_MS) so a hung hub can never leave a
 * loader spinning, and a forced refresh requested while a load is in flight is queued and runs
 * as soon as that load settles rather than being dropped.
 */
import { create } from "zustand";
import { library, refKey, LibraryError, REQUEST_TIMEOUT_MS, type CallOptions, type ContentRef, type Detail, type Fetcher, type Home, type Settings } from "../hub/library";

export const STALE_MS = 30_000;

export interface Loaded<T> {
  data: T | null;
  loadedAt: number;
  loading: boolean;
  error: string | null;
  /** Hub error code when the last load failed (e.g. `needs_link`), else null. */
  errorCode: string | null;
  /** A forced refresh arrived while loading; run it when the in-flight load settles. */
  forceQueued: boolean;
}

export interface LibraryStore {
  home: Loaded<Home>;
  details: Record<string, Loaded<Detail>>;
  settings: Loaded<Settings>;
  loadHome(force?: boolean): Promise<void>;
  loadDetail(ref: ContentRef, force?: boolean): Promise<void>;
  loadSettings(force?: boolean): Promise<void>;
  /** After a play: recents changed. */
  invalidateHome(): void;
  /** Options every call gets: the fetcher and the request deadline. */
  callOptions(): CallOptions;
  _deps: { fetcher: Fetcher; now: () => number; timeoutMs: number };
  _reset(): void;
}

export const fresh = <T,>(): Loaded<T> => ({ data: null, loadedAt: 0, loading: false, error: null, errorCode: null, forceQueued: false });

const defaultDeps = { fetcher: ((...a: Parameters<typeof fetch>) => fetch(...a)) as Fetcher, now: () => Date.now(), timeoutMs: REQUEST_TIMEOUT_MS };

const isTimeout = (e: unknown) => {
  const name = (e as { name?: unknown } | null)?.name;
  return name === "AbortError" || name === "TimeoutError";
};

export function errorMessage(e: unknown): string {
  if (e instanceof LibraryError) return e.message;
  if (isTimeout(e)) return "The hub took too long to answer.";
  return "Can't reach the hub. Check that the Mac is on the network.";
}

export function errorCode(e: unknown): string {
  if (e instanceof LibraryError) return e.code;
  if (isTimeout(e)) return "timeout";
  return "unreachable";
}

/**
 * Shared loader: staleness check, in-flight guard with force queuing, deadline via callOptions,
 * error capture. `read`/`write` address the slot so home, settings and each detail use one path.
 */
async function runLoad<T>(
  get: () => LibraryStore,
  read: () => Loaded<T>,
  write: (patch: Partial<Loaded<T>>) => void,
  fetchIt: (opts: CallOptions) => Promise<T>,
  force: boolean,
  staleMs: number,
): Promise<void> {
  const cur = read();
  if (cur.loading) {
    if (force && !cur.forceQueued) write({ forceQueued: true });
    return;
  }
  if (!force && cur.data && get()._deps.now() - cur.loadedAt < staleMs) return;
  write({ loading: true });
  try {
    const data = await fetchIt(get().callOptions());
    write({ data, loadedAt: get()._deps.now(), loading: false, error: null, errorCode: null });
  } catch (e) {
    write({ loading: false, error: errorMessage(e), errorCode: errorCode(e) });
  } finally {
    if (read().forceQueued) {
      write({ forceQueued: false });
      void runLoad(get, read, write, fetchIt, true, staleMs);
    }
  }
}

export const useLibrary = create<LibraryStore>((set, get) => ({
  home: fresh(),
  details: {},
  settings: fresh(),
  _deps: defaultDeps,

  callOptions() {
    const d = get()._deps;
    return { fetcher: d.fetcher, timeoutMs: d.timeoutMs };
  },

  loadHome(force = false) {
    return runLoad(
      get,
      () => get().home,
      (patch) => set((s) => ({ home: { ...s.home, ...patch } })),
      (opts) => library.home(opts),
      force,
      STALE_MS,
    );
  },

  loadDetail(ref, force = false) {
    const key = refKey(ref);
    return runLoad(
      get,
      () => get().details[key] ?? fresh<Detail>(),
      (patch) => set((s) => ({ details: { ...s.details, [key]: { ...(s.details[key] ?? fresh<Detail>()), ...patch } } })),
      (opts) => library.detail(ref, opts),
      force,
      STALE_MS * 10,
    );
  },

  loadSettings(force = false) {
    return runLoad(
      get,
      () => get().settings,
      (patch) => set((s) => ({ settings: { ...s.settings, ...patch } })),
      (opts) => library.settings(opts),
      force,
      STALE_MS,
    );
  },

  invalidateHome() {
    set((s) => ({ home: { ...s.home, loadedAt: 0 } }));
    void get().loadHome(true);
  },

  _reset() {
    set({ home: fresh(), details: {}, settings: fresh(), _deps: defaultDeps });
  },
}));

/** Call options for one-off requests from components (auth flow, restart). */
export function useCallOptions(): CallOptions {
  return useLibrary((s) => s.callOptions)();
}

/** Re-run `fn` when the tab regains focus or becomes visible. Returns the cleanup. */
export function onRefocus(fn: () => void): () => void {
  if (typeof window === "undefined") return () => {};
  const onVis = () => {
    if (document.visibilityState === "visible") fn();
  };
  window.addEventListener("focus", fn);
  document.addEventListener("visibilitychange", onVis);
  return () => {
    window.removeEventListener("focus", fn);
    document.removeEventListener("visibilitychange", onVis);
  };
}
