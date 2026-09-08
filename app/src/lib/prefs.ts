/**
 * Per-viewer conveniences in localStorage: last-used targets (PRD Decision 4), remembered per
 * content ref plus a generic "last" slot, capped at the 50 most recent entries. Every access is
 * guarded because storage can throw or be empty.
 */
const KEY = "illyhub.lastTarget.v2";
export const GENERIC_KEY = "last";
export const MAX_ENTRIES = 50;

type Store = { order: string[]; map: Record<string, string[]> };

function read(): Store {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return { order: [], map: {} };
    const parsed = JSON.parse(raw) as Partial<Store>;
    return { order: Array.isArray(parsed.order) ? parsed.order : [], map: parsed.map && typeof parsed.map === "object" ? parsed.map : {} };
  } catch {
    return { order: [], map: {} };
  }
}

export function readLastTarget(contentKey = GENERIC_KEY): string[] | null {
  const v = read().map[contentKey];
  return Array.isArray(v) ? v : null;
}

/**
 * Remember `targets` for a content ref and as the generic last choice, in one storage write.
 * Least recently written content entries are evicted past MAX_ENTRIES; the generic slot never is.
 */
export function rememberTargets(targets: string[], contentKey?: string): void {
  try {
    const s = read();
    const keys = contentKey ? [contentKey, GENERIC_KEY] : [GENERIC_KEY];
    for (const k of keys) {
      s.map[k] = targets;
      if (k !== GENERIC_KEY) {
        s.order = s.order.filter((x) => x !== k);
        s.order.push(k);
      }
    }
    while (s.order.length > MAX_ENTRIES) {
      const evict = s.order.shift();
      if (evict) delete s.map[evict];
    }
    localStorage.setItem(KEY, JSON.stringify(s));
  } catch {
    // storage unavailable; the pre-highlight is a convenience, not state
  }
}

/** @deprecated use rememberTargets */
export function writeLastTarget(targets: string[], contentKey?: string): void {
  rememberTargets(targets, contentKey === GENERIC_KEY ? undefined : contentKey);
}
