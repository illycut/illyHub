/**
 * Hub state and protocol types. REST request/response shapes come from the generated OpenAPI
 * types; the WebSocket-only shapes (HubState and friends) mirror hub/src/illyhub_hub/state.py
 * and docs/api.md.
 */
import type { components } from "./openapi";
import type { ContentRef } from "./library";

export type Ack = components["schemas"]["Ack"];
export type ErrorEnvelope = components["schemas"]["ErrorEnvelope"];
export type PartialFailure = components["schemas"]["PartialFailure"];
export type Capabilities = components["schemas"]["Capabilities"];
export type Player = components["schemas"]["Player"];
export type Side = components["schemas"]["Side"];
export type Zone = components["schemas"]["Zone"];
export type ConnectionStatus = components["schemas"]["ConnectionStatus"];
export type HealthResponse = components["schemas"]["HealthResponse"];
export type DevicesResponse = components["schemas"]["DevicesResponse"];

export type Vendor = "heos" | "sonos";
export type PlayState = "play" | "pause" | "stop" | "unknown";
export type ConnState = "connected" | "reconnecting" | "disconnected" | "disabled";
export type SyncStatus =
  | "idle"
  | "resolving"
  | "priming"
  | "verifying"
  | "starting"
  | "locked"
  | "drifting"
  | "correcting"
  | "lost"
  | "stopped";
export type Source = "tidal" | "ytmusic" | "pandora" | (string & {});
export type TransportAction = "play" | "pause" | "toggle" | "stop" | "next" | "prev";

export interface ArtRef {
  url: string | null;
  accent: string | null;
  accent_is_safe: boolean;
  cache_key?: string | null;
}

export interface GroupTopology {
  id: string;
  vendor: Vendor;
  coordinator_player_id: string;
  member_ids: string[];
  name: string | null;
}

export interface NowPlaying {
  title: string | null;
  artist: string | null;
  album: string | null;
  art: ArtRef;
  source: Source | null;
  seekable: boolean;
  supports_next: boolean;
  supports_prev: boolean;
  duration_ms: number | null;
  track_id: string | null;
  /**
   * Canonical id for what is playing: a Tidal track (either vendor) or a Pandora station
   * (kind "station"; the track itself has no canonical id). Null when unknown.
   */
  content_ref: ContentRef | null;
}

export interface Position {
  position_ms: number;
  /** ISO-8601 timestamp from the hub. */
  reported_at: string;
  confidence: number;
}

/**
 * Sync Play session (docs/api.md "Sync Play"). HEOS is the clock master and cannot be seeked;
 * Sonos is the follower and the only side that is corrected (docs/prd-review.md §6a).
 */
export interface SyncState {
  status: SyncStatus;
  master_side: string | null;
  follower_side: string | null;
  content_ref: { service: string; kind: string; id: string } | null;
  drift_ms: number | null;
  start_delta_ms: number | null;
  last_correction_at: string | null;
  corrections: number;
  reason: string | null;
  started_at: string | null;
  session_id: string | null;
  title: string | null;
}

export interface HubState {
  version: number;
  players: Record<string, Player>;
  zones: Record<string, Zone>;
  groups: Record<string, GroupTopology>;
  sides: Record<string, Side>;
  now_playing: Record<string, NowPlaying>;
  positions: Record<string, Position>;
  sync: SyncState;
  connections: Record<string, ConnectionStatus>;
}

/** Top-level collections a delta path may address (depth two), plus the depth-one `sync`. */
export type DeltaCollection = Exclude<keyof HubState, "version" | "sync">;

export type ServerMessage =
  | { type: "snapshot"; version: number; state: HubState }
  | { type: "delta"; from_version: number; to_version: number; changed: Record<string, unknown> }
  | ({ type: "ack" } & Ack)
  | { type: "ping" }
  | { type: "pong"; server_time_ms: number }
  | { type: "error"; code: string; message?: string };

export type ClientMessage = { type: "ping" } | { type: "pong" } | { type: "resync" };

export const ERROR_CODES = [
  "unknown_target",
  "unsupported_action",
  "not_seekable",
  "adapter_disconnected",
  "device_offline",
  "invalid_argument",
  "vendor_error",
] as const;
export type ErrorCode = (typeof ERROR_CODES)[number];
