/**
 * WebSocket connection manager for `/ws`. Reconnects with backoff, answers hub pings, sends its
 * own ping every 20 s and derives a hub-clock offset from `pong.server_time_ms`, watches for a
 * silent socket, and routes frames to handlers. Timer and WebSocket implementations are
 * injectable for tests; the defaults are arrow wrappers so `setTimeout` is never invoked with a
 * `HubSocket` receiver (which throws "Illegal invocation" in browsers).
 */
import type { ClientMessage, ServerMessage } from "./types";

export type ConnectionPhase = "connecting" | "open" | "closed";

export interface SocketHandlers {
  onMessage(msg: ServerMessage): void;
  onPhase(phase: ConnectionPhase, attemptAt: number): void;
  /** Estimated `hubTime - localTime` in ms, refreshed on every pong. */
  onClockOffset?(offsetMs: number): void;
}

export type TimerHandle = unknown;

export interface SocketOptions {
  WebSocketImpl?: typeof WebSocket;
  now?: () => number;
  setTimer?: (fn: () => void, ms: number) => TimerHandle;
  clearTimer?: (t: TimerHandle) => void;
  /** Backoff schedule in ms; the last value repeats. */
  backoffMs?: number[];
  /** Close and reconnect when no frame arrives for this long (hub pings every 20 s). */
  silenceMs?: number;
  /** Client ping cadence for clock offset; 0 disables. */
  pingMs?: number;
}

const DEFAULT_BACKOFF = [1000, 2000, 4000, 8000, 15000, 30000];
export const PING_INTERVAL_MS = 20_000;

export class HubSocket {
  private ws: WebSocket | null = null;
  private attempt = 0;
  private reconnectTimer: TimerHandle | null = null;
  private silenceTimer: TimerHandle | null = null;
  private pingTimer: TimerHandle | null = null;
  private pingSentAt: number | null = null;
  private stopped = false;
  private readonly WS: typeof WebSocket;
  private readonly now: () => number;
  private readonly setTimer: (fn: () => void, ms: number) => TimerHandle;
  private readonly clearTimer: (t: TimerHandle) => void;
  private readonly backoff: number[];
  private readonly silenceMs: number;
  private readonly pingMs: number;

  constructor(
    private readonly url: string,
    private readonly handlers: SocketHandlers,
    opts: SocketOptions = {},
  ) {
    this.WS = opts.WebSocketImpl ?? WebSocket;
    this.now = opts.now ?? (() => Date.now());
    this.setTimer = opts.setTimer ?? ((fn, ms) => setTimeout(fn, ms));
    this.clearTimer = opts.clearTimer ?? ((t) => clearTimeout(t as ReturnType<typeof setTimeout>));
    this.backoff = opts.backoffMs ?? DEFAULT_BACKOFF;
    this.silenceMs = opts.silenceMs ?? 45_000;
    this.pingMs = opts.pingMs ?? PING_INTERVAL_MS;
  }

  start(): void {
    this.stopped = false;
    this.open();
  }

  stop(): void {
    this.stopped = true;
    this.clearTimers();
    const ws = this.ws;
    this.ws = null;
    ws?.close();
  }

  send(msg: ClientMessage): boolean {
    if (this.ws && this.ws.readyState === this.WS.OPEN) {
      try {
        this.ws.send(JSON.stringify(msg));
        return true;
      } catch {
        return false;
      }
    }
    return false;
  }

  resync(): boolean {
    return this.send({ type: "resync" });
  }

  private open(): void {
    // Never hold two sockets: a stale one is closed silently first.
    const stale = this.ws;
    this.ws = null;
    stale?.close();
    this.handlers.onPhase("connecting", this.now());
    let ws: WebSocket;
    try {
      ws = new this.WS(this.url);
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.ws = ws;
    ws.onopen = () => {
      if (this.ws !== ws) return;
      this.attempt = 0;
      this.handlers.onPhase("open", this.now());
      this.armSilence();
      this.armPing();
    };
    ws.onmessage = (ev: MessageEvent) => {
      if (this.ws !== ws) return;
      let msg: ServerMessage;
      try {
        msg = JSON.parse(String(ev.data)) as ServerMessage;
      } catch {
        return;
      }
      if (!msg || typeof msg !== "object" || typeof msg.type !== "string") return;
      // Protocol first, housekeeping after: a timer failure must never swallow a frame.
      if (msg.type === "ping") {
        this.send({ type: "pong" });
      } else if (msg.type === "pong") {
        this.onPong(msg.server_time_ms);
      } else {
        this.handlers.onMessage(msg);
      }
      this.armSilence();
    };
    ws.onclose = () => {
      if (this.ws !== ws) return;
      this.ws = null;
      this.handlers.onPhase("closed", this.now());
      this.scheduleReconnect();
    };
    ws.onerror = () => {
      // onclose follows; nothing to do here.
    };
  }

  private onPong(serverTimeMs: number): void {
    if (typeof serverTimeMs !== "number" || this.pingSentAt === null) return;
    const now = this.now();
    const rtt = now - this.pingSentAt;
    this.pingSentAt = null;
    // Server stamped the pong roughly mid-flight.
    const offset = serverTimeMs - (now - rtt / 2);
    this.handlers.onClockOffset?.(Math.round(offset));
  }

  private armPing(): void {
    if (this.pingMs <= 0) return;
    if (this.pingTimer) this.clearTimer(this.pingTimer);
    const tick = () => {
      this.pingTimer = null;
      if (!this.ws || this.stopped) return;
      this.pingSentAt = this.now();
      this.send({ type: "ping" });
      this.pingTimer = this.setTimer(tick, this.pingMs);
    };
    // First ping right after open, so the clock offset is known before the scrubber moves.
    tick();
  }

  private armSilence(): void {
    if (this.silenceTimer) this.clearTimer(this.silenceTimer);
    this.silenceTimer = this.setTimer(() => {
      this.silenceTimer = null;
      const ws = this.ws;
      this.ws = null;
      ws?.close();
      this.handlers.onPhase("closed", this.now());
      this.scheduleReconnect();
    }, this.silenceMs);
  }

  private scheduleReconnect(): void {
    if (this.silenceTimer) {
      this.clearTimer(this.silenceTimer);
      this.silenceTimer = null;
    }
    if (this.pingTimer) {
      this.clearTimer(this.pingTimer);
      this.pingTimer = null;
    }
    if (this.stopped) return;
    if (this.reconnectTimer) return;
    const delay = this.backoff[Math.min(this.attempt, this.backoff.length - 1)] ?? 30_000;
    this.attempt += 1;
    this.reconnectTimer = this.setTimer(() => {
      this.reconnectTimer = null;
      if (!this.stopped) this.open();
    }, delay);
  }

  private clearTimers(): void {
    for (const t of [this.reconnectTimer, this.silenceTimer, this.pingTimer]) if (t) this.clearTimer(t);
    this.reconnectTimer = null;
    this.silenceTimer = null;
    this.pingTimer = null;
  }
}
