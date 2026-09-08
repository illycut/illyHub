"use client";
import { useEffect, useRef } from "react";
import { Sheet } from "./Sheet";
import { Slider } from "./Slider";
import { AmpIcon, MuteIcon, SpeakerIcon, VolumeIcon } from "./icons";
import { Coalescer } from "@/lib/coalesce";
import { useHub } from "@/lib/hub/store";
import { playersList } from "@/lib/selectors";

/**
 * Volume sheet (design system §6.5): linked master on top, hairline, one row per device
 * interleaved by room, glyph distinguishes HEOS (amp) from Sonos (speaker). Offline rows drop to
 * text-tertiary with "offline" and are never hidden (§8). Drags are coalesced; release flushes.
 */
export function VolumeSheet({ open, onClose }: { open: boolean; onClose: () => void }) {
  const players = useHub((s) => playersList(s.state));
  const setVolume = useHub((s) => s.setVolume);
  const linkedVolume = useHub((s) => s.linkedVolume);
  const setMute = useHub((s) => s.setMute);
  const online = players.filter((p) => p.online);
  const master = online.length ? Math.max(...online.map((p) => p.volume)) : 0;

  const coalescer = useRef<Coalescer<number> | null>(null);
  if (coalescer.current === null) {
    coalescer.current = new Coalescer<number>((key, level) => {
      if (key === "master") void linkedVolume({ level });
      else void setVolume(key, level);
    }, 100);
  }
  useEffect(() => {
    const c = coalescer.current;
    return () => c?.dispose();
  }, []);
  const c = coalescer.current;

  /** Release: the coalescer's commit sends the pending value, or the value itself if it never went out. */
  const commit = (key: string, v: number) => c.commit(key, v);

  return (
    <Sheet open={open} onClose={onClose} title="Volume" testId="volume-sheet">
      <div className="flex items-center gap-3">
        <span className="w-6 text-secondary" aria-hidden="true">
          <VolumeIcon size={20} />
        </span>
        <div className="flex-1">
          <div className="text-caption text-secondary">All rooms</div>
          <Slider
            label="Master volume"
            value={master}
            disabled={online.length === 0}
            onChange={(v) => c.submit("master", v)}
            onCommit={(v) => commit("master", v)}
            testId="master-slider"
          />
        </div>
        <span className="w-12 text-right text-caption text-secondary numeric">{master}</span>
      </div>
      <hr className="my-2 border-0 border-t border-stroke" />
      <ul className="flex flex-col">
        {players.map((p) => {
          const off = !p.online;
          const Glyph = p.vendor === "heos" ? AmpIcon : SpeakerIcon;
          return (
            <li key={p.id} className={`flex items-center gap-3 py-1 ${off ? "text-tertiary" : ""}`} data-testid={`volume-row-${p.id}`}>
              <span className={`w-6 ${off ? "text-tertiary" : "text-secondary"}`} aria-hidden="true">
                <Glyph size={20} />
              </span>
              <div className="flex-1">
                <div className={`text-caption ${off ? "text-tertiary" : "text-secondary"}`}>
                  {p.name}
                  {off ? <span className="ml-2 text-micro">offline</span> : null}
                </div>
                <Slider
                  label={`${p.name} volume`}
                  value={p.volume}
                  disabled={off}
                  onChange={(v) => c.submit(p.id, v)}
                  onCommit={(v) => commit(p.id, v)}
                  testId={`slider-${p.id}`}
                />
              </div>
              <button
                type="button"
                className={`hit-target flex items-center justify-center rounded-control ${p.muted ? "text-signal" : "text-secondary"} disabled:opacity-40`}
                aria-label={p.muted ? `Unmute ${p.name}` : `Mute ${p.name}`}
                aria-pressed={p.muted}
                disabled={off}
                onClick={() => void setMute(p.id, !p.muted)}
              >
                {p.muted ? <MuteIcon size={20} /> : <VolumeIcon size={20} />}
              </button>
            </li>
          );
        })}
      </ul>
      {players.length === 0 ? <p className="py-4 text-body text-secondary">No rooms yet. The hub is still discovering players.</p> : null}
    </Sheet>
  );
}
