/**
 * Up next / queue view logic (ai-dev #77, docs/prd-review.md §4). Pure functions so the sheet,
 * Now Playing and the store agree.
 *
 * Facts baked in (docs/api.md "Queue"): the hub exposes one queue per side, capped at HUB_QUEUE_MAX
 * with `truncated`; `current_index` is the hub's canonical position and `POST /api/queue/jump` takes
 * that index. Radio sources have no queue: a Pandora station gets a sentence instead of a list.
 * Live changes arrive on the `queues.<side>` delta path; a hub that predates queues never sends
 * one, so the sheet falls back to `GET /api/queue/{side}` when it opens.
 */
import type { NowPlaying, Side } from "./hub/types";
import type { Queue, QueueItem } from "./hub/library";

export const UP_NEXT = "Up next";
export const QUEUE_EMPTY = "Nothing queued.";
export const QUEUE_STATION = "Stations don't have a queue.";
export const QUEUE_JUMP_LABEL = "Play from";

/** A radio source (Pandora station) has no queue to show. */
export function isRadioSource(np: NowPlaying | null | undefined): boolean {
  return np?.content_ref?.kind === "station" || np?.source === "pandora";
}

/** "Showing the first N tracks." when the hub cut the list. */
export function truncatedNote(queue: Queue | null | undefined): string | null {
  return queue?.truncated ? `Showing the first ${queue.items.length} tracks.` : null;
}

/**
 * The entry that is playing: the hub's `current_index` when it names one, else the entry whose
 * track id or title+artist matches now-playing (a hub that cannot tell which queue slot is live).
 */
export function currentQueueIndex(queue: Queue | null | undefined, np: NowPlaying | null | undefined): number | null {
  if (!queue) return null;
  if (queue.current_index !== null && queue.items.some((it) => it.index === queue.current_index)) return queue.current_index;
  if (!np) return null;
  // Matching is only trusted when exactly one entry matches (repeated tracks in a playlist are ambiguous).
  const unique = (xs: QueueItem[]) => (xs.length === 1 ? xs[0]!.index : null);
  if (np.track_id) {
    const byId = queue.items.filter((it) => it.track_id === np.track_id || (it.content_ref && (np.track_id === it.content_ref.id || np.track_id!.endsWith(`:${it.content_ref.id}`))));
    const hit = unique(byId);
    if (hit !== null) return hit;
  }
  if (np.title) {
    const hit = unique(queue.items.filter((it) => it.title === np.title && (it.artist ?? null) === (np.artist ?? null)));
    if (hit !== null) return hit;
  }
  return null;
}

/** Row label for assistive tech: "Play from So What, Miles Davis". */
export function queueRowLabel(item: QueueItem): string {
  return `${QUEUE_JUMP_LABEL} ${item.title}${item.artist ? `, ${item.artist}` : ""}`;
}

export type QueueView = { kind: "station"; text: string } | { kind: "empty"; text: string } | { kind: "loading" } | { kind: "list"; items: QueueItem[]; current: number | null; note: string | null };

/**
 * What the sheet shows for a side: the station sentence for radio, a skeleton while the first fetch
 * runs, the §8-style empty line, or the list with the current entry and the truncation note.
 */
export function queueView(side: Side | null | undefined, np: NowPlaying | null | undefined, queue: Queue | null | undefined, loading: boolean): QueueView {
  if (!side) return { kind: "empty", text: QUEUE_EMPTY };
  if (isRadioSource(np)) return { kind: "station", text: QUEUE_STATION };
  if (!queue) return loading ? { kind: "loading" } : { kind: "empty", text: QUEUE_EMPTY };
  if (queue.items.length === 0) return loading ? { kind: "loading" } : { kind: "empty", text: QUEUE_EMPTY };
  return { kind: "list", items: queue.items, current: currentQueueIndex(queue, np), note: truncatedNote(queue) };
}
