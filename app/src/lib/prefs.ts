/**
 * Per-viewer conveniences in localStorage: the last-used target (PRD Decision 4). Content ids
 * arrive in Phase 3; until then the key is the generic "last" slot. Every access is guarded
 * because storage can throw or be empty.
 */
const KEY = "illyhub.lastTarget.v1";

export function readLastTarget(contentKey = "last"): string[] | null {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return null;
    const map = JSON.parse(raw) as Record<string, string[]>;
    const v = map[contentKey];
    return Array.isArray(v) ? v : null;
  } catch {
    return null;
  }
}

export function writeLastTarget(targets: string[], contentKey = "last"): void {
  try {
    let map: Record<string, string[]> = {};
    try {
      const raw = localStorage.getItem(KEY);
      map = raw ? (JSON.parse(raw) as Record<string, string[]>) : {};
    } catch {
      map = {};
    }
    map[contentKey] = targets;
    localStorage.setItem(KEY, JSON.stringify(map));
  } catch {
    // storage unavailable; the pre-highlight is a convenience, not state
  }
}
