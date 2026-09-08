/**
 * Search view logic (ai-dev #78, docs/api.md "Search"). Pure so the header field, the results view
 * and the tests agree. Facts baked in: the hub fans out to the linked services with a soft deadline
 * and returns partial results with `errors` per service; queries under two characters are not sent;
 * the field debounces at 300 ms.
 */
import type { SearchResults, Service } from "./hub/library";
import messages from "./hub/messages.json";
import { SERVICE_LABEL } from "./services";

export const SEARCH_MIN_CHARS = 2;
export const SEARCH_DEBOUNCE_MS = 300;
export const SEARCH_PLACEHOLDER = "Search albums, playlists, tracks, stations";
export const SEARCH_EMPTY = "Nothing matched.";
/** The hub's own sentence for a too-short query (`templates.hub.search_too_short`), with a client fallback. */
export const SEARCH_TOO_SHORT: string = (messages as { templates?: { hub?: { search_too_short?: string } } }).templates?.hub?.search_too_short ?? "Type at least two characters to search.";

/** Whitespace-trimmed query, or null when too short to send. */
export function searchQuery(raw: string): string | null {
  const q = raw.trim().replace(/\s+/g, " ");
  return q.length >= SEARCH_MIN_CHARS ? q : null;
}

export type SearchGroupKey = "albums" | "playlists" | "tracks" | "stations";
export const SEARCH_GROUPS: { key: SearchGroupKey; title: string }[] = [
  { key: "albums", title: "Albums" },
  { key: "playlists", title: "Playlists" },
  { key: "tracks", title: "Tracks" },
  { key: "stations", title: "Stations" },
];

export function isEmptyResults(r: SearchResults | null | undefined): boolean {
  return !r || SEARCH_GROUPS.every((g) => r[g.key].length === 0);
}

export function resultCount(r: SearchResults | null | undefined): number {
  return r ? SEARCH_GROUPS.reduce((n, g) => n + r[g.key].length, 0) : 0;
}

/** One-line status for the live region: "12 results for harmonic" / "No results for zzz" / "Searching…". */
export function searchStatusLine(state: { phase: "idle" } | { phase: "loading"; q: string } | { phase: "results"; q: string; results: SearchResults } | { phase: "error"; q: string; message: string }): string {
  if (state.phase === "idle") return "";
  if (state.phase === "loading") return `Searching for ${state.q}…`;
  if (state.phase === "error") return state.message;
  const n = resultCount(state.results);
  return n === 0 ? `No results for ${state.q}` : `${n} result${n === 1 ? "" : "s"} for ${state.q}`;
}

/**
 * One caption line for per-service failures (§10: fact and fix in one sentence):
 * "Tidal didn't answer; showing YouTube Music results." when something else answered, or
 * "Tidal didn't answer." when nothing did.
 */
export function searchErrorCaption(r: SearchResults | null | undefined, linked?: readonly Service[]): string | null {
  if (!r) return null;
  const failed = (Object.keys(r.errors) as Service[]).filter((s) => r.errors[s]);
  if (failed.length === 0) return null;
  const names = (xs: Service[]) => xs.map((s) => SERVICE_LABEL[s]).join(" and ");
  // "Who answered" is whoever actually has items in the results (or the caller's list); the hub's
  // `services` says who was asked, which is not the same thing.
  const present = new Set<Service>();
  for (const g of SEARCH_GROUPS) for (const it of r[g.key]) present.add(it.content_ref.service);
  const answered = (linked ?? [...present]).filter((s) => !failed.includes(s));
  const head = `${names(failed)} didn't answer`;
  if (answered.length === 0 || isEmptyResults(r)) return `${head}.`;
  return `${head}; showing ${names(answered)} results.`;
}
