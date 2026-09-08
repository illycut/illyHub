"use client";
import { memo } from "react";
import { motion } from "framer-motion";
import { PauseIcon, PlayIcon } from "./icons";
import { ZoneDots } from "./ZoneDots";
import { SyncChip } from "./SyncChip";
import { TextSkeleton } from "./Skeleton";
import { artUrl } from "@/lib/hub/config";
import { useHub } from "@/lib/hub/store";
import { resolveActiveSide, zoneDotsForState } from "@/lib/selectors";
import { useReducedMotion } from "@/lib/reducedMotion";
import { HERO_LAYOUT_TRANSITION } from "./NowPlaying";

export let miniPlayerRenders = 0;
export function resetMiniPlayerRenders(): void {
  miniPlayerRenders = 0;
}

/**
 * Persistent mini-player (design system §6.3): 64px, bg-raised, radius-sheet top corners, upward
 * shadow. 44px art (shared element with the Now Playing hero; unmounted while Now Playing is
 * expanded so only one `layoutId` holder exists), title/artist with marquee, play/pause, zone
 * dot cluster, sync chip. Tap anywhere expands. Reads narrow slices so 1 Hz position deltas do
 * not re-render it.
 */
export const MiniPlayer = memo(function MiniPlayer({ onExpand, hideArt = false }: { onExpand: () => void; hideArt?: boolean }) {
  miniPlayerRenders += 1;
  const side = useHub((s) => resolveActiveSide(s.state, s.activeSideId));
  const sideId = side?.id ?? null;
  const np = useHub((s) => (sideId ? s.state?.now_playing[sideId] : undefined));
  const sync = useHub((s) => s.state?.sync);
  const hasState = useHub((s) => s.state !== null);
  const dots = useHub((s) => zoneDotsForState(s.state));
  const transport = useHub((s) => s.transport);
  const reduced = useReducedMotion();
  const thumb = artUrl(np?.art, 96);
  const playing = side?.play_state === "play";
  const loading = !hasState || (!!side && !np);

  return (
    <div className="fixed inset-x-0 bottom-0 z-30 pb-safe" data-testid="mini-player">
      <div className="mx-auto flex h-mini max-w-[720px] items-center gap-3 rounded-t-sheet bg-raised px-3 shadow-mini">
        <button type="button" className="flex min-w-0 flex-1 items-center gap-3 text-left" onClick={onExpand} aria-label="Open Now Playing">
          {hideArt ? (
            <span className="h-thumb-art w-thumb-art shrink-0 rounded-art bg-overlay" aria-hidden="true" />
          ) : (
            <motion.div
              layoutId={reduced ? undefined : "hero-art"}
              transition={HERO_LAYOUT_TRANSITION}
              className="h-thumb-art w-thumb-art shrink-0 overflow-hidden rounded-art bg-overlay"
            >
              {thumb ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img src={thumb} alt="" draggable={false} className="h-full w-full object-cover" />
              ) : null}
            </motion.div>
          )}
          <div className="min-w-0 flex-1 overflow-hidden">
            {loading ? (
              <TextSkeleton lines={2} />
            ) : (
              <>
                <div className="text-body text-primary">
                  <span className={`inline-block whitespace-nowrap ${np?.title && np.title.length > 28 ? "marquee" : ""}`}>{np?.title ?? "Nothing playing"}</span>
                </div>
                <div className="clamp-1 text-caption text-secondary">{np?.artist ?? side?.name ?? ""}</div>
              </>
            )}
          </div>
        </button>
        <SyncChip sync={sync} />
        <button
          type="button"
          className="hit-target flex items-center justify-center rounded-control text-primary disabled:opacity-40"
          aria-label={playing ? "Pause" : "Play"}
          aria-pressed={playing}
          disabled={!side}
          onClick={() => side && void transport("toggle", side.id)}
        >
          {playing ? <PauseIcon size={26} /> : <PlayIcon size={26} />}
        </button>
        <div className="pr-1">
          <ZoneDots model={dots} />
        </div>
      </div>
    </div>
  );
});
