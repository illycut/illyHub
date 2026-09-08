/**
 * REST command client. Every call returns the hub's Ack (also broadcast on the WebSocket).
 * Non-2xx responses still carry an Ack body with `error` set; network failures throw.
 */
import { hubHttpBase } from "./config";
import { newCorrelationId } from "./correlation";
import type { Ack, TransportAction, Vendor } from "./types";

export type Fetcher = typeof fetch;

export interface CommandRequest {
  path: string;
  method?: "POST" | "DELETE";
  body?: unknown;
  correlationId?: string;
}

export const COMMAND_TIMEOUT_MS = 5000;

export async function sendCommand(req: CommandRequest, fetcher: Fetcher = fetch, timeoutMs = COMMAND_TIMEOUT_MS): Promise<Ack> {
  const correlationId = req.correlationId ?? newCorrelationId();
  const controller = typeof AbortController !== "undefined" ? new AbortController() : null;
  const timer = controller ? setTimeout(() => controller.abort(), timeoutMs) : null;
  let res: Response;
  try {
    res = await fetcher(hubHttpBase() + req.path, {
      method: req.method ?? "POST",
      // X-Illyhub forces a CORS preflight; the hub requires it on state-changing requests.
      headers: { "content-type": "application/json", "x-correlation-id": correlationId, "X-Illyhub": "1" },
      body: req.body === undefined ? undefined : JSON.stringify(req.body),
      signal: controller?.signal,
    });
  } finally {
    if (timer) clearTimeout(timer);
  }
  const text = await res.text();
  let ack: Ack | null = null;
  try {
    ack = text ? (JSON.parse(text) as Ack) : null;
  } catch {
    ack = null;
  }
  if (ack && typeof ack === "object" && !("correlation_id" in ack) && "code" in ack) {
    // A bare error envelope (e.g. needs_link on /api/play): surface the hub's own message.
    const env = ack as unknown as { code: string; message?: string };
    return {
      correlation_id: correlationId,
      ok: false,
      action: req.path,
      target: null,
      state_version: 0,
      error: { code: env.code, message: env.message ?? `The hub answered ${res.status}.`, target: null, correlation_id: correlationId },
      partial: [],
      latency_ms: 0,
    } as Ack;
  }
  if (!ack || typeof ack !== "object" || !("correlation_id" in ack)) {
    return {
      correlation_id: correlationId,
      ok: false,
      action: req.path,
      target: null,
      state_version: 0,
      error: {
        code: "vendor_error",
        message: res.ok ? "The hub returned an unexpected response." : `The hub answered ${res.status}.`,
        target: null,
        correlation_id: correlationId,
      },
      partial: [],
      latency_ms: 0,
    } as Ack;
  }
  return ack;
}

export const commands = {
  transport: (action: TransportAction, target: string, correlationId?: string): CommandRequest => ({
    path: `/api/transport/${action}`,
    body: { target },
    correlationId,
  }),
  seek: (target: string, position_ms: number, correlationId?: string): CommandRequest => ({
    path: "/api/seek",
    body: { target, position_ms: Math.max(0, Math.round(position_ms)) },
    correlationId,
  }),
  skip: (target: string, delta_ms: number, correlationId?: string): CommandRequest => ({
    path: "/api/skip",
    body: { target, delta_ms: Math.round(delta_ms) },
    correlationId,
  }),
  volume: (target: string, level: number, correlationId?: string): CommandRequest => ({
    path: "/api/volume",
    body: { target, level: clampLevel(level) },
    correlationId,
  }),
  linkedVolume: (arg: { level?: number; delta?: number }, correlationId?: string): CommandRequest => ({
    path: "/api/volume",
    body: {
      linked: true,
      ...(arg.level !== undefined ? { level: clampLevel(arg.level) } : {}),
      ...(arg.delta !== undefined ? { delta: Math.round(arg.delta) } : {}),
    },
    correlationId,
  }),
  mute: (target: string, muted: boolean, correlationId?: string): CommandRequest => ({
    path: "/api/mute",
    body: { target, muted },
    correlationId,
  }),
  zonePower: (zone_id: string, on: boolean, correlationId?: string): CommandRequest => ({
    path: "/api/zone/power",
    body: { zone_id, on },
    correlationId,
  }),
  group: (vendor: Vendor, coordinator_id: string, member_ids: string[], correlationId?: string): CommandRequest => ({
    path: "/api/group",
    body: { vendor, coordinator_id, member_ids },
    correlationId,
  }),
  ungroup: (side_id: string, correlationId?: string): CommandRequest => ({
    path: `/api/group/${encodeURIComponent(side_id)}`,
    method: "DELETE",
    correlationId,
  }),
  /** Sync Play (Phase 4, docs/api.md "Sync Play"): one HEOS side (master) + one Sonos side (follower). */
  syncPlay: (
    content_ref: { service: string; kind: string; id: string },
    heos_target: string | string[],
    sonos_target: string | string[],
    start_index?: number,
    correlationId?: string,
  ): CommandRequest => ({
    path: "/api/sync/play",
    body: { content_ref, heos_target, sonos_target, ...(start_index !== undefined ? { start_index } : {}) },
    correlationId,
  }),
  syncStop: (correlationId?: string): CommandRequest => ({ path: "/api/sync/stop", body: {}, correlationId }),
  syncRetry: (correlationId?: string): CommandRequest => ({ path: "/api/sync/retry", body: {}, correlationId }),
  /**
   * Pandora Sync via the hub Mac's AirPlay bridge (Phase 7, docs/api.md "Pandora Sync",
   * experimental). The app sends the chosen side ids; the hub matches rooms to AirPlay outputs and
   * refuses with `invalid_argument` (naming the rooms) when none match.
   */
  pandoraSyncStart: (side_ids: string[], correlationId?: string): CommandRequest => ({
    path: "/api/pandora-sync/start",
    body: { side_ids },
    correlationId,
  }),
  pandoraSyncStop: (correlationId?: string): CommandRequest => ({ path: "/api/pandora-sync/stop", body: {}, correlationId }),
  /** Jump to a queue entry by the hub's canonical index (Phase 8, docs/api.md "Queue"). */
  queueJump: (target: string, index: number, correlationId?: string): CommandRequest => ({
    path: "/api/queue/jump",
    body: { target, index: Math.max(0, Math.round(index)) },
    correlationId,
  }),
  /** Shuffle / repeat for a side (Phase 8, docs/api.md "Play mode"); only the given fields change. */
  playMode: (target: string, mode: { shuffle?: boolean; repeat?: "off" | "one" | "all" }, correlationId?: string): CommandRequest => ({
    path: "/api/playmode",
    body: { target, ...(mode.shuffle !== undefined ? { shuffle: mode.shuffle } : {}), ...(mode.repeat !== undefined ? { repeat: mode.repeat } : {}) },
    correlationId,
  }),
  /** Play library content on one target (Phase 3, docs/api.md "Play"). */
  play: (target: string, content_ref: { service: string; kind: string; id: string }, start_index?: number, correlationId?: string): CommandRequest => ({
    path: "/api/play",
    body: { target, content_ref, ...(start_index !== undefined ? { start_index } : {}) },
    correlationId,
  }),
};

export function clampLevel(level: number): number {
  return Math.max(0, Math.min(100, Math.round(level)));
}
