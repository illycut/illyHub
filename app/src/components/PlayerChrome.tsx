"use client";
import { useCallback } from "react";
import { AnimatePresence, LayoutGroup, motion, type PanInfo } from "framer-motion";
import { MiniPlayer } from "./MiniPlayer";
import { NowPlaying } from "./NowPlaying";
import { ZonePicker } from "./ZonePicker";
import { useHub } from "@/lib/hub/store";
import { resolveActiveSide } from "@/lib/selectors";
import { useLibrary } from "@/lib/library/store";
import { useChrome } from "@/lib/ui/chrome";
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
 * Chrome shared by every route: the persistent mini-player, the Now Playing overlay (the one
 * signature motion, §7: shared-element art morph, 320ms spring, crossfade under reduced motion,
 * swipe down collapses), and the zone/target picker in both modes. Content tapped anywhere in the
 * app arrives here as a play request (Decision 4) and is dispatched after the picker confirms.
 */
export function PlayerChrome({ children }: { children: React.ReactNode }) {
  const expanded = useChrome((s) => s.npExpanded);
  const setExpanded = useChrome((s) => s.setNpExpanded);
  const zonesOpen = useChrome((s) => s.zonesOpen);
  const setZonesOpen = useChrome((s) => s.setZonesOpen);
  const playRequest = useChrome((s) => s.playRequest);
  const clearPlayRequest = useChrome((s) => s.clearPlayRequest);
  const play = useHub((s) => s.play);
  const selectSide = useHub((s) => s.selectSide);
  const invalidateHome = useLibrary((s) => s.invalidateHome);
  const reduced = useReducedMotion();
  useScrollLock(expanded);

  const closePicker = useCallback(() => {
    setZonesOpen(false);
    clearPlayRequest();
  }, [setZonesOpen, clearPlayRequest]);

  /** Expanding pins whichever side is showing, so Now Playing stays on it while the user acts. */
  const expand = useCallback(() => {
    const hub = useHub.getState();
    if (!hub.activeSideId) {
      const side = resolveActiveSide(hub.state, null);
      if (side) selectSide(side.id);
    }
    setExpanded(true);
  }, [selectSide, setExpanded]);

  const confirmPlay = useCallback(
    (targets: string[]) => {
      if (!playRequest) return;
      const req = playRequest;
      // Timing marks for the two-tap requirement (PRD Decision 4): confirm tap -> hub ack.
      performance.mark?.("play:confirm");
      void play(targets, { content_ref: req.content_ref, title: req.title, subtitle: req.subtitle, art: req.art, start_index: req.start_index }).then((acks) => {
        performance.mark?.("play:ack");
        if (acks.some((a) => a.ok)) invalidateHome();
      });
    },
    [playRequest, play, invalidateHome],
  );

  return (
    <LayoutGroup>
      {children}
      <MiniPlayer onExpand={expand} hideArt={expanded} />
      <ZonePicker open={zonesOpen} onClose={closePicker} play={playRequest} onConfirm={confirmPlay} />
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
