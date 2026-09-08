/**
 * App chrome state shared across routes: whether Now Playing is expanded, whether the zone
 * picker is open, and the "play this" request that drives the target picker in play mode
 * (PRD Decision 4: tapping content always opens the picker first).
 */
import { create } from "zustand";
import type { ContentRef } from "../hub/library";
import type { ArtRef } from "../hub/types";

export interface PlayRequest {
  content_ref: ContentRef;
  title: string;
  subtitle: string | null;
  art: ArtRef;
  /** Track offset for "play from this track" in a detail view. */
  start_index?: number;
  /** Side ids to pre-highlight (history first, localStorage second). */
  preferred: string[];
  /** Sides that can play this content; others render disabled with a reason. */
  availability?: { heos: boolean; sonos: boolean };
  /** Vendors whose app has not linked the content's service (Pandora), so the reason can name the fix. */
  unlinked_vendors?: ("heos" | "sonos")[];
  /** Extra line under the Sync Play note (e.g. launched from the Now Playing offer). */
  note?: string;
}

interface ChromeStore {
  npExpanded: boolean;
  zonesOpen: boolean;
  playRequest: PlayRequest | null;
  setNpExpanded(v: boolean): void;
  setZonesOpen(v: boolean): void;
  requestPlay(req: PlayRequest): void;
  clearPlayRequest(): void;
}

export const useChrome = create<ChromeStore>((set) => ({
  npExpanded: false,
  zonesOpen: false,
  playRequest: null,
  setNpExpanded: (v) => set({ npExpanded: v }),
  setZonesOpen: (v) => set({ zonesOpen: v }),
  requestPlay: (req) => set({ playRequest: req, zonesOpen: true }),
  clearPlayRequest: () => set({ playRequest: null }),
}));
