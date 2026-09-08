"use client";
import { useState } from "react";
import { AnimatePresence, LayoutGroup, motion, type PanInfo } from "framer-motion";
import { MiniPlayer } from "./MiniPlayer";
import { NowPlaying } from "./NowPlaying";
import { ZonePicker } from "./ZonePicker";
import { GearIcon, LayersIcon } from "./icons";
import { ArtSkeleton, ConnectCard, EmptyState } from "./Skeleton";
import { useHub } from "@/lib/hub/store";
import { useReducedMotion } from "@/lib/reducedMotion";
import { useScrollLock } from "@/lib/scrollLock";

export const COLLAPSE_OFFSET_PX = 80;
export const COLLAPSE_VELOCITY = 600;

/** Swipe-down rule for collapsing Now Playing: past 80px or faster than 600px/s, from scrollTop 0. */
export function shouldCollapse(info: Pick<PanInfo, "offset" | "velocity">, scrollTop: number): boolean {
  if (scrollTop > 0) return false;
  return info.offset.y > COLLAPSE_OFFSET_PX || info.velocity.y > COLLAPSE_VELOCITY;
}

/**
 * App shell. Phase 2: Now Playing is the front door (expanded by default); the home screen
 * below it is the Phase 3 slot with the §8 empty copy and "Connect" cards in grid positions.
 * The mini-player expand is the one signature motion (§7): shared-element art morph, 320ms
 * spring; crossfade under reduced motion. Swipe down on Now Playing collapses it.
 */
export function AppShell({ startExpanded = true }: { startExpanded?: boolean }) {
  const [expanded, setExpanded] = useState(startExpanded);
  const [zonesOpen, setZonesOpen] = useState(false);
  const loading = useHub((s) => s.state === null);
  const reduced = useReducedMotion();
  useScrollLock(expanded);

  return (
    <LayoutGroup>
      <div className="min-h-dvh bg-base pb-[calc(var(--size-mini-player)+var(--safe-bottom))]">
        <header className="screen-margin flex h-[calc(var(--size-target)+var(--safe-top))] items-end justify-between pt-safe">
          <h1 className="pb-2 text-title-1 text-primary">illyHub</h1>
          <div className="flex items-center gap-2">
            <button type="button" className="hit-target flex items-center justify-center rounded-control text-secondary" aria-label="Zones" onClick={() => setZonesOpen(true)}>
              <LayersIcon />
            </button>
            <button type="button" className="hit-target flex items-center justify-center rounded-control text-secondary" aria-label="Settings" disabled title="Settings arrives in Phase 3">
              <GearIcon />
            </button>
          </div>
        </header>
        <main className="screen-margin flex flex-col gap-8 pt-4">
          <section aria-labelledby="recents-h">
            <h2 id="recents-h" className="text-title-2 text-primary">Recently played</h2>
            {loading ? (
              <div className="mt-3 grid grid-cols-2 gap-3 tablet:grid-cols-4">
                {[0, 1].map((i) => <ArtSkeleton key={i} />)}
              </div>
            ) : (
              <EmptyState>Play something and it lands here.</EmptyState>
            )}
          </section>
          <section aria-labelledby="playlists-h">
            <h2 id="playlists-h" className="text-title-2 text-primary">Your playlists</h2>
            <div className="mt-3 grid grid-cols-2 gap-3 tablet:grid-cols-4">
              {loading ? (
                [0, 1, 2, 3].map((i) => <ArtSkeleton key={i} />)
              ) : (
                <>
                  <ConnectCard service="Tidal" />
                  <ConnectCard service="YouTube Music" />
                </>
              )}
            </div>
          </section>
        </main>
        <MiniPlayer onExpand={() => setExpanded(true)} hideArt={expanded} />
        <ZonePicker open={zonesOpen} onClose={() => setZonesOpen(false)} />
      </div>
      <AnimatePresence>
        {expanded ? (
          <motion.div
            key="np"
            className="fixed inset-0 z-40 overflow-y-auto overscroll-contain bg-base"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={reduced ? { duration: 0.15 } : { type: "spring", stiffness: 300, damping: 30, duration: 0.32 }}
            drag={reduced ? false : "y"}
            dragDirectionLock
            dragConstraints={{ top: 0, bottom: 0 }}
            dragElastic={{ top: 0, bottom: 0.35 }}
            onDragEnd={(_, info) => {
              const el = document.querySelector<HTMLElement>('[data-testid="now-playing-layer"]');
              if (shouldCollapse(info, el?.scrollTop ?? 0)) setExpanded(false);
            }}
            data-testid="now-playing-layer"
          >
            <NowPlaying onCollapse={() => setExpanded(false)} />
          </motion.div>
        ) : null}
      </AnimatePresence>
    </LayoutGroup>
  );
}
