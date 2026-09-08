"use client";
import { useShallow } from "zustand/react/shallow";
import { ArtCard } from "./ArtCard";
import { ArtSkeleton, EmptyState } from "./Skeleton";
import { useHub } from "@/lib/hub/store";
import type { HistoryItem } from "@/lib/hub/library";

/** `vendor|name` per side id: primitive values so a shallow-compared selector stays stable. */
export type SideInfoMap = Record<string, string>;
export const sideInfo = (vendor: "heos" | "sonos", name: string): string => `${vendor}|${name}`;

/** Vendor and room of the first remembered side, for the corner glyph and label (§6.2, UX U9). */
export function lastSideOf(item: HistoryItem, sides: SideInfoMap): { vendor: "heos" | "sonos" | null; room: string | null } {
  for (const id of item.last_targets) {
    const s = sides[id];
    if (s) {
      const sep = s.indexOf("|");
      return { vendor: s.slice(0, sep) as "heos" | "sonos", room: s.slice(sep + 1) };
    }
    if (id.startsWith("heos:")) return { vendor: "heos", room: null };
    if (id.startsWith("sonos:")) return { vendor: "sonos", room: null };
  }
  return { vendor: null, room: null };
}

/** side id -> "vendor|name", shallow-compared so 1 Hz position deltas never re-render the rail. */
function useSideInfo(): SideInfoMap {
  return useHub(
    useShallow((s) => {
      const out: SideInfoMap = {};
      for (const side of Object.values(s.state?.sides ?? {})) out[side.id] = sideInfo(side.vendor, side.name);
      return out;
    }),
  );
}

/** Bleed the rail to the screen edge using the same token as the screen margin (UX U11). */
const BLEED = "-mx-[var(--screen-margin)] px-[var(--screen-margin)]";

/**
 * Recently played rail (design system §6.2): horizontal scroll of art cards at --size-card with
 * the next card always peeking, a small target glyph per card, resume through the target picker.
 * Handlers are stable so the memoised cards only re-render when their item changes.
 */
export function RecentsRail({
  items,
  loading,
  onPlay,
  onDetail,
}: {
  items: HistoryItem[];
  loading: boolean;
  onPlay: (item: HistoryItem) => void;
  onDetail: (item: HistoryItem) => void;
}) {
  const sides = useSideInfo();
  if (loading && items.length === 0) {
    return (
      <div className={`${BLEED} flex gap-3 overflow-hidden`} aria-hidden="true">
        {[0, 1, 2].map((i) => (
          <div key={i} className="w-card shrink-0">
            <ArtSkeleton />
          </div>
        ))}
      </div>
    );
  }
  if (items.length === 0) return <EmptyState>Play something and it lands here.</EmptyState>;
  return (
    <ul className={`no-scrollbar ${BLEED} flex snap-x snap-mandatory gap-3 overflow-x-auto pb-1`} style={{ scrollPaddingLeft: "var(--screen-margin)" }} data-testid="recents-rail">
      {items.map((it) => {
        const last = lastSideOf(it, sides);
        return (
          <li key={`${it.content_ref.service}:${it.content_ref.kind}:${it.content_ref.id}`} className="w-card shrink-0 snap-start">
            <ArtCard
              payload={it}
              title={it.title}
              subtitle={it.subtitle}
              art={it.art}
              service={it.content_ref.service}
              lastVendor={last.vendor}
              lastRoom={last.room}
              onPress={onPlay}
              onDetail={it.content_ref.kind === "album" || it.content_ref.kind === "playlist" ? onDetail : undefined}
              testId="recent-card"
            />
          </li>
        );
      })}
    </ul>
  );
}
