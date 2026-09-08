import { HubSocket, PING_INTERVAL_MS } from "./socket";
import type { ServerMessage } from "./types";

class FakeWS {
  static instances: FakeWS[] = [];
  static OPEN = 1;
  static CLOSED = 3;
  static throwOnConstruct = false;
  readyState = 0;
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(public url: string) {
    if (FakeWS.throwOnConstruct) throw new Error("SecurityError");
    FakeWS.instances.push(this);
  }
  send(d: string) {
    this.sent.push(d);
  }
  close() {
    this.readyState = FakeWS.CLOSED;
    this.onclose?.();
  }
  open() {
    this.readyState = FakeWS.OPEN;
    this.onopen?.();
  }
  receive(m: unknown) {
    this.onmessage?.({ data: typeof m === "string" ? m : JSON.stringify(m) });
  }
}

function harness(opts: { pingMs?: number } = {}) {
  FakeWS.instances = [];
  FakeWS.throwOnConstruct = false;
  const timers: { fn: () => void; ms: number; id: number }[] = [];
  let seq = 0;
  let now = 1000;
  const messages: ServerMessage[] = [];
  const phases: string[] = [];
  const offsets: number[] = [];
  const sock = new HubSocket(
    "ws://hub/ws",
    { onMessage: (m) => messages.push(m), onPhase: (p) => phases.push(p), onClockOffset: (o) => offsets.push(o) },
    {
      WebSocketImpl: FakeWS as unknown as typeof WebSocket,
      now: () => now,
      setTimer: (fn, ms) => {
        timers.push({ fn, ms, id: ++seq });
        return seq;
      },
      clearTimer: (id) => {
        const i = timers.findIndex((t) => t.id === id);
        if (i >= 0) timers.splice(i, 1);
      },
      backoffMs: [100, 200, 400],
      silenceMs: 5000,
      pingMs: opts.pingMs ?? 0,
    },
  );
  return { sock, timers, messages, phases, offsets, ws: () => FakeWS.instances.at(-1)!, advance: (ms: number) => (now += ms) };
}

describe("HubSocket", () => {
  it("opens, forwards frames, answers pings itself, ignores junk", () => {
    const h = harness();
    h.sock.start();
    expect(h.phases).toEqual(["connecting"]);
    h.ws().open();
    expect(h.phases).toEqual(["connecting", "open"]);
    h.ws().receive({ type: "snapshot", version: 1, state: {} });
    h.ws().receive({ type: "ping" });
    h.ws().receive("not json");
    h.ws().receive({ nope: 1 });
    expect(h.messages).toHaveLength(1);
    expect(h.ws().sent).toEqual([JSON.stringify({ type: "pong" })]);
    expect(h.sock.resync()).toBe(true);
    expect(h.ws().sent.at(-1)).toBe(JSON.stringify({ type: "resync" }));
  });

  it("reconnects with the backoff schedule and resets after a successful open", () => {
    const h = harness();
    h.sock.start();
    h.ws().close();
    expect(h.phases.at(-1)).toBe("closed");
    expect(h.timers.map((t) => t.ms)).toEqual([100]);
    h.timers.shift()!.fn();
    h.ws().close();
    expect(h.timers.map((t) => t.ms)).toEqual([200]);
    h.timers.shift()!.fn();
    h.ws().close();
    expect(h.timers.map((t) => t.ms)).toEqual([400]);
    h.timers.shift()!.fn();
    h.ws().close();
    expect(h.timers.map((t) => t.ms)).toEqual([400]); // last value repeats
    h.timers.shift()!.fn();
    h.ws().open();
    h.ws().close();
    expect(h.timers.map((t) => t.ms)).toEqual([100]); // reset after open
    expect(h.sock.send({ type: "ping" })).toBe(false);
  });

  it("schedules a reconnect when the WebSocket constructor throws", () => {
    const h = harness();
    FakeWS.throwOnConstruct = true;
    h.sock.start();
    expect(h.phases).toEqual(["connecting"]);
    expect(h.timers.map((t) => t.ms)).toEqual([100]);
    FakeWS.throwOnConstruct = false;
    h.timers.shift()!.fn();
    expect(FakeWS.instances).toHaveLength(1);
  });

  it("forces a reconnect when the hub goes silent", () => {
    const h = harness();
    h.sock.start();
    h.ws().open();
    const silence = h.timers.find((t) => t.ms === 5000);
    expect(silence).toBeDefined();
    const first = h.ws();
    silence!.fn();
    expect(first.readyState).toBe(FakeWS.CLOSED);
    expect(h.phases.at(-1)).toBe("closed");
    expect(h.timers.some((t) => t.ms === 100)).toBe(true);
  });

  it("pings on open and every 20 s, deriving the hub clock offset from pong", () => {
    const h = harness({ pingMs: PING_INTERVAL_MS });
    h.sock.start();
    h.ws().open();
    expect(h.ws().sent).toEqual([JSON.stringify({ type: "ping" })]);
    h.advance(100); // rtt 100 → mid-flight at local 1050
    h.ws().receive({ type: "pong", server_time_ms: 5000 });
    expect(h.offsets).toEqual([5000 - 1050]);
    expect(h.messages).toHaveLength(0); // pong is housekeeping, not state
    const next = h.timers.find((t) => t.ms === PING_INTERVAL_MS);
    expect(next).toBeDefined();
    next!.fn();
    expect(h.ws().sent.filter((s) => s.includes('"ping"'))).toHaveLength(2);
  });

  it("stop() closes silently, does not reconnect, and open() never holds two sockets", () => {
    const h = harness();
    h.sock.start();
    h.ws().open();
    const first = h.ws();
    h.sock.stop();
    expect(h.timers).toHaveLength(0);
    expect(first.readyState).toBe(FakeWS.CLOSED);
    expect(h.phases.at(-1)).toBe("open"); // stop is silent
    h.sock.start();
    h.sock.start();
    expect(FakeWS.instances.filter((w) => w.readyState !== FakeWS.CLOSED)).toHaveLength(1);
  });

  it("works with the REAL default timers (no injection) — regression for setTimeout receiver bug", async () => {
    FakeWS.instances = [];
    const messages: ServerMessage[] = [];
    const sock = new HubSocket("ws://hub/ws", { onMessage: (m) => messages.push(m), onPhase: () => {} }, {
      WebSocketImpl: FakeWS as unknown as typeof WebSocket,
      backoffMs: [10],
      silenceMs: 50,
      pingMs: 15,
    });
    sock.start();
    const ws = FakeWS.instances.at(-1)!;
    ws.open();
    ws.receive({ type: "snapshot", version: 1, state: {} });
    expect(messages).toHaveLength(1);
    await new Promise((r) => setTimeout(r, 30));
    expect(ws.sent.filter((s) => s.includes('"ping"')).length).toBeGreaterThanOrEqual(2);
    await new Promise((r) => setTimeout(r, 80));
    // silence watchdog fired with real timers and reconnected
    expect(FakeWS.instances.length).toBeGreaterThanOrEqual(2);
    sock.stop();
  });
});
