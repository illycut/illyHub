"use client";
import { useShallow } from "zustand/react/shallow";
import { ArtCard } from "./ArtCard";
import { Rail, RailItem, RailSkeleton } from "./Rail";
import { ArtSkeleton, EmptyState } from "./Skeleton";
import { useHub } from "@/lib/hub/store";
import type { HistoryItem } from "@/lib/hub/library";
import { stationSubtitle } from "@/lib/pandora";

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
      <RailSkeleton count={3}>
        <ArtSkeleton />
      </RailSkeleton>
    );
  }
  if (items.length === 0) return <EmptyState>Play something and it lands here.</EmptyState>;
  return (
    <Rail testId="recents-rail">
      {items.map((it) => {
        const last = lastSideOf(it, sides);
        return (
          <RailItem key={`${it.content_ref.service}:${it.content_ref.kind}:${it.content_ref.id}`}>
            <ArtCard
              payload={it}
              title={it.title}
              subtitle={it.content_ref.kind === "station" && it.availability ? stationSubtitle(it.availability) : it.subtitle}
              art={it.art}
              service={it.content_ref.service}
              lastVendor={last.vendor}
              lastRoom={last.room}
              onPress={onPlay}
              onDetail={it.content_ref.kind === "album" || it.content_ref.kind === "playlist" ? onDetail : undefined}
              testId="recent-card"
            />
          </RailItem>
        );
      })}
    </Rail>
  );
}
