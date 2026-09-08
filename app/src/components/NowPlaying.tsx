"use client";
import { useEffect, useMemo, useRef, useState } from "react";
import { useShallow } from "zustand/react/shallow";
import { motion } from "framer-motion";
import { ChevronDownIcon, LayersIcon, VolumeIcon } from "./icons";
import { Scrubber } from "./Scrubber";
import { ServiceBadge } from "./ServiceBadge";
import { TransportRow } from "./TransportRow";
import { VolumeSheet } from "./VolumeSheet";
import { ZoneDots } from "./ZoneDots";
import { SyncChip } from "./SyncChip";
import { Slider } from "./Slider";
import { TextSkeleton } from "./Skeleton";
import { artUrl } from "@/lib/hub/config";
import { useHub } from "@/lib/hub/store";
import { useChrome } from "@/lib/ui/chrome";
import { resolveActiveSide, zoneDotsForState, zoneDotsLabel } from "@/lib/selectors";
import { useReducedMotion } from "@/lib/reducedMotion";
import { Coalescer } from "@/lib/coalesce";
import { scrubFill } from "@/lib/color";
import { STOP_SYNC, SYNC_BUTTON, SYNC_OFFER_NOTE, isSyncActive, offerContent, syncLabel, syncLabelShort, syncOffer, syncSideIds, type SyncOffer } from "@/lib/sync";
import type { PlayRequest } from "@/lib/ui/chrome";
import { readLastTarget } from "@/lib/prefs";
import { useLibrary } from "@/lib/library/store";
import { refKey } from "@/lib/hub/library";

/** Shared-element spring for the hero art (design system §7: 320ms). */
export const HERO_LAYOUT_TRANSITION = { layout: { type: "spring", duration: 0.32, bounce: 0.15 } } as const;

/**
 * Now Playing (design system §6.4). Backdrop: color-mix(accent 40%, bg-base) base layer, the
 * hub's pre-blurred art variant above it at ~0.45, a gradient last. Hero art is the shared
 * element with the mini-player thumb (§7). Landscape >= 700px: two columns, art left.
 */
export function NowPlaying({ onCollapse }: { onCollapse?: () => void }) {
  // Narrow selectors: 1 Hz position deltas re-render only what reads positions.
  const side = useHub((s) => resolveActiveSide(s.state, s.activeSideId));
  const sideId = side?.id ?? null;
  const np = useHub((s) => (sideId ? s.state?.now_playing[sideId] : undefined));
  const pos = useHub((s) => (sideId ? s.state?.positions[sideId] : undefined));
  const sync = useHub((s) => s.state?.sync);
  // One memoized view over sides/players/sync for the two sync-derived values (S11): recomputed
  // only when those collections change, never on 1 Hz position ticks.
  // useShallow needs flat primitives, so the offer is spread here and rebuilt below.
  const syncView = useHub(
    useShallow((s) => {
      const sides = s.state?.sides ?? {};
      const players = s.state?.players;
      const active = resolveActiveSide(s.state, s.activeSideId);
      const ref = active ? s.state?.now_playing[active.id]?.content_ref : null;
      const offer = syncOffer(active, ref, sides, (side) => players?.[side.coordinator_player_id]?.online ?? true, s.state?.sync, readLastTarget() ?? []);
      return {
        label: syncLabel(s.state?.sync, sides),
        shortLabel: syncLabelShort(s.state?.sync),
        offerHeos: offer?.heos ?? null,
        offerSonos: offer?.sonos ?? null,
        offerPartner: offer?.partnerName ?? null,
      };
    }),
  );
  const hasState = useHub((s) => s.state !== null);
  const dots = useHub((s) => zoneDotsForState(s.state));
  const transport = useHub((s) => s.transport);
  const syncStop = useHub((s) => s.syncStop);
  const syncRetry = useHub((s) => s.syncRetry);
  const seek = useHub((s) => s.seek);
  const skip = useHub((s) => s.skip);
  const setSideVolume = useHub((s) => s.setSideVolume);
  const hubNow = useHub((s) => s.hubNow);
  const reduced = useReducedMotion();
  const [volumeOpen, setVolumeOpen] = useState(false);
  const setZonesOpen = useChrome((s) => s.setZonesOpen);
  const requestPlay = useChrome((s) => s.requestPlay);

  // Sync Play (Phase 4): while a session is live the scrubber is read-only (the HEOS master
  // cannot seek), the indicator names both rooms, and Stop sync returns to independent control.
  const syncing = isSyncActive(sync) && !!sideId && syncSideIds(sync).includes(sideId);
  const syncingLabel = syncView.label;
  const offer: SyncOffer | null = useMemo(
    () => (syncView.offerHeos && syncView.offerSonos && syncView.offerPartner ? { heos: syncView.offerHeos, sonos: syncView.offerSonos, partnerName: syncView.offerPartner } : null),
    [syncView.offerHeos, syncView.offerSonos, syncView.offerPartner],
  );
  const lost = syncing && sync?.status === "lost";
  const offerSync = () => {
    if (!offer || !np?.title || !np.content_ref || !side) return;
    // Prefer the recent album/playlist this room was playing when its cached detail holds the
    // current track (start there); else the bare track ref (UX S6).
    const lib = useLibrary.getState();
    const details = Object.fromEntries(Object.entries(lib.details).map(([k, v]) => [k, v.data]));
    const chosen = offerContent(np.content_ref, side.id, lib.home.data?.recents.items, details, refKey);
    const req: PlayRequest = {
      content_ref: chosen.content_ref,
      title: chosen.title ?? np.title,
      subtitle: chosen.subtitle ?? np.artist,
      art: np.art,
      start_index: chosen.start_index,
      preferred: [offer.heos, offer.sonos],
      note: SYNC_OFFER_NOTE,
    };
    requestPlay(req);
  };

  const accentSafe = !!np?.art?.accent && np.art.accent_is_safe;
  const accent = accentSafe ? np!.art.accent! : "var(--bg-raised)";
  const fill = scrubFill(np?.art?.accent, !!np?.art?.accent_is_safe);
  const backdrop = artUrl(np?.art, "backdrop");
  const hero = artUrl(np?.art, 1080);

  const caps = {
    supports_seek: !!side && !syncing && (side.capabilities?.supports_seek ?? true) && (np?.seekable ?? true),
    supports_next: !!side && (side.capabilities?.supports_next ?? true) && (np?.supports_next ?? true),
    supports_prev: !!side && (side.capabilities?.supports_prev ?? true) && (np?.supports_prev ?? true),
  };

  const volCoalescer = useRef<Coalescer<number> | null>(null);
  if (volCoalescer.current === null) {
    volCoalescer.current = new Coalescer<number>((key, level) => void setSideVolume(key, level), 100);
  }
  useEffect(() => {
    const c = volCoalescer.current;
    return () => c?.dispose();
  }, []);

  const style = useMemo(() => ({ "--art-accent": accent, "--scrub-fill": fill }) as React.CSSProperties, [accent, fill]);
  const roomLabel = syncing && syncingLabel
    ? `${syncingLabel}. ${zoneDotsLabel(dots)}. Choose where to play`
    : side
      ? `Playing on ${side.name}. ${zoneDotsLabel(dots)}. Choose where to play`
      : "Choose where to play";
  const loading = !hasState || (!!side && !np);

  return (
    <section
      className="relative flex min-h-dvh flex-col bg-base pt-safe pb-safe"
      style={style}
      data-testid="now-playing"
      data-np-sync={syncing || offer ? "true" : undefined}
      aria-label="Now playing"
    >
      {/* Backdrop (U8): tinted base at 100%, blurred art above, gradient last. */}
      <div className="pointer-events-none absolute inset-0 overflow-hidden" aria-hidden="true">
        <div
          className="absolute inset-0"
          style={{
            background: accent,
            backgroundImage: `linear-gradient(color-mix(in srgb, ${accent} calc(var(--art-accent-alpha) * 100%), var(--bg-base)), color-mix(in srgb, ${accent} calc(var(--art-accent-alpha) * 100%), var(--bg-base)))`,
          }}
        />
        {backdrop ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img src={backdrop} alt="" draggable={false} className="absolute inset-0 h-full w-full object-cover opacity-[0.45]" />
        ) : null}
        <div className="absolute inset-0 bg-gradient-to-b from-transparent via-base/40 to-base" />
      </div>

      <header className="screen-margin relative z-10 flex h-target items-center justify-between">
        {onCollapse ? (
          <button type="button" className="hit-target -ml-3 flex items-center justify-center rounded-control text-secondary" aria-label="Collapse" onClick={onCollapse}>
            <ChevronDownIcon />
          </button>
        ) : (
          <span className="w-target" />
        )}
        <div className="flex min-w-0 flex-1 items-center justify-center gap-2 px-1">
          <button
            type="button"
            className="flex h-target min-w-0 items-center gap-2 rounded-control px-3 text-caption text-secondary"
            onClick={() => setZonesOpen(true)}
            aria-label={roomLabel}
            data-testid="target-indicator"
          >
            <span className="shrink-0" aria-hidden="true">
              <LayersIcon size={18} />
            </span>
            <span className="truncate">{syncing && syncView.shortLabel ? syncView.shortLabel : (side?.name ?? "Choose room")}</span>
            <ZoneDots model={dots} />
          </button>
          <SyncChip sync={sync} onRetry={() => void syncRetry()} onStop={() => void syncStop()} />
        </div>
        <span className="w-target" />
      </header>

      <div data-np-stack className="relative z-10 flex flex-1 flex-col justify-end gap-6 pb-6 np-landscape:grid np-landscape:grid-cols-[minmax(0,45vw)_minmax(0,1fr)] np-landscape:items-center np-landscape:gap-8 np-landscape:screen-margin">
        <div className="screen-margin np-landscape:px-0 np-landscape:self-center">
          <motion.div
            layoutId={reduced ? undefined : "hero-art"}
            transition={HERO_LAYOUT_TRANSITION}
            className="hero-size overflow-hidden rounded-art bg-raised"
            data-testid="hero-art"
          >
            {hero ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img src={hero} alt={np?.album ? `${np.album} cover` : ""} draggable={false} className="h-full w-full object-cover" />
            ) : null}
          </motion.div>
        </div>

        <div data-np-stack className="flex flex-col gap-6 np-landscape:min-w-0">
          <div className="screen-margin np-landscape:px-0 mx-auto w-full max-w-hero">
            {loading ? (
              <TextSkeleton lines={3} />
            ) : (
              <>
                <h2 data-np-title className="clamp-2 text-display text-primary" data-testid="np-title">
                  {np?.title ?? "Nothing playing"}
                </h2>
                <div data-np-meta>
                  <p className="mt-1 clamp-1 text-caption text-secondary">{[np?.artist, np?.album].filter(Boolean).join(" · ")}</p>
                  <div className="mt-2 flex min-h-target items-center gap-2">
                    <ServiceBadge source={np?.source} size={22} withLabel />
                    {syncing && !lost ? (
                      <button type="button" className="ml-auto min-h-target rounded-control px-3 text-caption text-secondary" onClick={() => void syncStop()} data-testid="stop-sync">
                        {STOP_SYNC}
                      </button>
                    ) : offer ? (
                      <button
                        type="button"
                        className="flex min-h-target items-center rounded-control bg-signal px-3 text-caption font-semibold text-base"
                        onClick={offerSync}
                        aria-label={`${SYNC_BUTTON} with ${offer.partnerName}`}
                        data-testid="offer-sync"
                      >
                        {SYNC_BUTTON}
                      </button>
                    ) : null}
                  </div>
                </div>
              </>
            )}
          </div>

          <div className="screen-margin np-landscape:px-0 mx-auto w-full max-w-hero">
            <Scrubber
              position={pos}
              playState={side?.play_state}
              durationMs={np?.duration_ms}
              seekable={caps.supports_seek}
              onSeek={(ms) => side && void seek(side.id, ms)}
              now={hubNow}
            />
          </div>

          <div className="transport-margin">
            <TransportRow
              playState={side?.play_state}
              caps={caps}
              disabled={!side}
              onPrev={() => side && void transport("prev", side.id)}
              onBack15={() => side && void skip(side.id, -15_000)}
              onToggle={() => side && void transport("toggle", side.id)}
              onForward15={() => side && void skip(side.id, 15_000)}
              onNext={() => side && void transport("next", side.id)}
            />
          </div>

          <div className="screen-margin np-landscape:px-0 mx-auto flex w-full max-w-hero items-center gap-3">
            <button
              type="button"
              className="hit-target flex items-center justify-center rounded-control text-secondary"
              aria-label="Open volume"
              onClick={() => setVolumeOpen(true)}
              data-testid="open-volume"
            >
              <VolumeIcon size={22} />
            </button>
            <div className="flex-1">
              <Slider
                label={side ? `${side.name} volume` : "Volume"}
                value={side?.volume ?? 0}
                disabled={!side}
                onChange={(v) => side && volCoalescer.current?.submit(side.id, v)}
                onCommit={(v) => side && volCoalescer.current?.commit(side.id, v)}
                testId="side-volume"
              />
            </div>
            <span className="w-8 text-right text-caption text-secondary numeric">{side?.volume ?? 0}</span>
          </div>
        </div>
      </div>

      <VolumeSheet open={volumeOpen} onClose={() => setVolumeOpen(false)} />
    </section>
  );
}
