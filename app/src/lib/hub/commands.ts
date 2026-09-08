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
      headers: { "content-type": "application/json", "x-correlation-id": correlationId },
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
};

export function clampLevel(level: number): number {
  return Math.max(0, Math.min(100, Math.round(level)));
}
