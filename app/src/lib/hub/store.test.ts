import { ACK_TIMEOUT_MS, useHub } from "./store";
import { useToasts } from "../ui/toasts";
import { jsonResponse, okAck, sampleState } from "@/test/fixtures";

type Timer = { fn: () => void; ms: number; id: number; cleared: boolean };

function fakeTimers() {
  const timers: Timer[] = [];
  let seq = 0;
  return {
    timers,
    setTimer: (fn: () => void, ms: number) => {
      const t = { fn, ms, id: ++seq, cleared: false };
      timers.push(t);
      return t.id;
    },
    clearTimer: (id: unknown) => {
      const t = timers.find((x) => x.id === id);
      if (t) t.cleared = true;
    },
    fire: () => {
      for (const t of timers.splice(0)) if (!t.cleared) t.fn();
    },
    live: () => timers.filter((t) => !t.cleared),
  };
}

const NOW = 1_700_000_000_000;

function install(fetcher: typeof fetch, now = () => NOW) {
  const ft = fakeTimers();
  useHub.getState()._reset();
  useHub.setState({ _deps: { fetcher, now, setTimer: ft.setTimer, clearTimer: ft.clearTimer } });
  useToasts.setState({ toasts: [] });
  const resync = vi.fn();
  useHub.getState().setResync(resync);
  return { ft, resync };
}

const cidOf = (init?: RequestInit) => (init?.headers as Record<string, string>)["x-correlation-id"];
const okFetcher = () => vi.fn(async (_u: RequestInfo | URL, init?: RequestInit) => jsonResponse(okAck(cidOf(init)))) as unknown as typeof fetch;
const flush = () => new Promise((r) => setTimeout(r, 0));

describe("hub store: protocol", () => {
  it("hydrates from a snapshot and applies deltas", () => {
    install(vi.fn());
    const s = sampleState(NOW);
    useHub.getState().onMessage({ type: "snapshot", version: s.version, state: s });
    expect(useHub.getState().state?.version).toBe(10);
    useHub.getState().onMessage({ type: "delta", from_version: 10, to_version: 11, changed: { "players.heos-1": { ...s.players["heos-1"], volume: 5 } } });
    expect(useHub.getState().state?.players["heos-1"]?.volume).toBe(5);
    expect(useHub.getState().state?.version).toBe(11);
  });

  it("requests a resync on a version gap and when a delta arrives before any snapshot", () => {
    const { resync } = install(vi.fn());
    useHub.getState().onMessage({ type: "delta", from_version: 3, to_version: 4, changed: {} });
    expect(resync).toHaveBeenCalledTimes(1);
    useHub.getState().onMessage({ type: "snapshot", version: 10, state: sampleState(NOW) });
    useHub.getState().onMessage({ type: "delta", from_version: 12, to_version: 13, changed: {} });
    expect(resync).toHaveBeenCalledTimes(2);
    expect(useHub.getState().state?.version).toBe(10);
  });

  it("tracks connection phase, everConnected, and the hub clock offset", () => {
    install(vi.fn());
    useHub.getState().setPhase("closed", 5);
    expect(useHub.getState().everConnected).toBe(false);
    useHub.getState().setPhase("open", 6);
    useHub.getState().setPhase("closed", 7);
    expect(useHub.getState().everConnected).toBe(true);
    expect(useHub.getState().lastAttemptAt).toBe(7);
    useHub.getState().setClockOffset(2500);
    expect(useHub.getState().hubNow()).toBe(NOW + 2500);
  });

  it("selection: selectSide, toggleTarget, setTargets", () => {
    install(vi.fn());
    useHub.getState().selectSide("a");
    useHub.getState().toggleTarget("a");
    useHub.getState().toggleTarget("b");
    useHub.getState().toggleTarget("a");
    expect(useHub.getState().activeSideId).toBe("a");
    expect(useHub.getState().selectedTargets).toEqual(["b"]);
    useHub.getState().setTargets(["x"]);
    expect(useHub.getState().selectedTargets).toEqual(["x"]);
  });
});

describe("hub store: optimistic commands", () => {
  it("applies the optimistic patch, sends with a correlation id, settles on the REST ack, keeps state", async () => {
    const fetcher = okFetcher();
    install(fetcher);
    useHub.getState().onMessage({ type: "snapshot", version: 10, state: sampleState(NOW) });
    const p = useHub.getState().setVolume("heos-1", 55);
    expect(useHub.getState().state?.players["heos-1"]?.volume).toBe(55);
    expect(Object.keys(useHub.getState().pending)).toHaveLength(1);
    const ack = await p;
    expect(ack.ok).toBe(true);
    const [url, init] = (fetcher as unknown as ReturnType<typeof vi.fn>).mock.calls[0] as [string, RequestInit];
    expect(url).toContain("/api/volume");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ target: "heos-1", level: 55 });
    expect(Object.keys(useHub.getState().pending)).toHaveLength(0);
    expect(useHub.getState().state?.players["heos-1"]?.volume).toBe(55);
  });

  it("on an error ack: toasts the hub's message and requests a resync instead of restoring stale collections", async () => {
    const fetcher = vi.fn(async (_u: RequestInfo | URL, init?: RequestInit) => {
      const cid = cidOf(init);
      return jsonResponse(okAck(cid, { ok: false, error: { code: "device_offline", message: "Patio is offline.", target: "sonos-P", correlation_id: cid } }), 409);
    }) as unknown as typeof fetch;
    const { resync } = install(fetcher);
    const s = sampleState(NOW);
    useHub.getState().onMessage({ type: "snapshot", version: 10, state: s });
    const p = useHub.getState().transport("play", "sonos:sonos-gK");
    // A newer delta lands while the command is in flight: it must survive the failure.
    useHub.getState().onMessage({ type: "delta", from_version: 10, to_version: 11, changed: { "players.heos-1": { ...s.players["heos-1"], volume: 99 } } });
    await p;
    expect(useHub.getState().state?.players["heos-1"]?.volume).toBe(99);
    expect(resync).toHaveBeenCalledTimes(1);
    expect(useToasts.getState().toasts.map((t) => t.message)).toEqual(["Patio is offline."]);
    // The resync snapshot is what removes the optimistic value.
    useHub.getState().onMessage({ type: "snapshot", version: 12, state: { ...s, version: 12 } });
    expect(useHub.getState().state?.sides["sonos:sonos-gK"]?.play_state).toBe("stop");
  });

  it("toasts each partial failure but keeps the optimistic state", async () => {
    const fetcher = vi.fn(async (_u: RequestInfo | URL, init?: RequestInit) =>
      jsonResponse(okAck(cidOf(init), { partial: [{ target: "heos:heos-1", code: "device_offline", message: "Living Room Amp is offline." }] })),
    ) as unknown as typeof fetch;
    install(fetcher);
    useHub.getState().onMessage({ type: "snapshot", version: 10, state: sampleState(NOW) });
    await useHub.getState().setMute("sonos:sonos-gK", true);
    expect(useHub.getState().state?.players["sonos-K"]?.muted).toBe(true);
    expect(useToasts.getState().toasts[0]?.message).toBe("Living Room Amp is offline.");
  });

  it("times out after 5 s: toast + resync; a late REST ack is ignored", async () => {
    let resolveFetch: (r: Response) => void = () => {};
    const fetcher = vi.fn(() => new Promise<Response>((r) => (resolveFetch = r))) as unknown as typeof fetch;
    const { ft, resync } = install(fetcher);
    useHub.getState().onMessage({ type: "snapshot", version: 10, state: sampleState(NOW) });
    const p = useHub.getState().seek("heos:heos-1", 120_000);
    expect(useHub.getState().state?.positions["heos:heos-1"]?.position_ms).toBe(120_000);
    expect(ft.live()[0]?.ms).toBe(ACK_TIMEOUT_MS);
    ft.fire();
    expect(resync).toHaveBeenCalledTimes(1);
    expect(useToasts.getState().toasts[0]?.message).toMatch(/didn't get confirmed/);
    expect(Object.keys(useHub.getState().pending)).toHaveLength(0);
    resolveFetch(jsonResponse(okAck("late")));
    await p;
    expect(resync).toHaveBeenCalledTimes(1);
    expect(useToasts.getState().toasts).toHaveLength(1);
  });

  it("settles via the WebSocket ack when it arrives first; the duplicate REST ack is a no-op", async () => {
    let cid = "";
    let resolveFetch: (r: Response) => void = () => {};
    const fetcher = vi.fn((_u: RequestInfo | URL, init?: RequestInit) => {
      cid = cidOf(init)!;
      return new Promise<Response>((r) => (resolveFetch = r));
    }) as unknown as typeof fetch;
    const { ft } = install(fetcher);
    useHub.getState().onMessage({ type: "snapshot", version: 10, state: sampleState(NOW) });
    const p = useHub.getState().zonePower("denon-10.0.0.9:zone2", true);
    await flush();
    useHub.getState().onMessage({ type: "ack", ...okAck(cid) } as never);
    expect(Object.keys(useHub.getState().pending)).toHaveLength(0);
    expect(ft.live()).toHaveLength(0);
    expect(useHub.getState().state?.zones["denon-10.0.0.9:zone2"]?.power).toBe(true);
    resolveFetch(jsonResponse(okAck(cid, { ok: false })));
    await p;
    expect(useToasts.getState().toasts).toHaveLength(0); // duplicate/late ack ignored
    useHub.getState().onMessage({ type: "ack", ...okAck("someone-else", { ok: false }) } as never);
    expect(useToasts.getState().toasts).toHaveLength(0);
  });

  it("a snapshot clears every pending command and its timer", async () => {
    let resolveFetch: (r: Response) => void = () => {};
    const fetcher = vi.fn(() => new Promise<Response>((r) => (resolveFetch = r))) as unknown as typeof fetch;
    const { ft, resync } = install(fetcher);
    const s = sampleState(NOW);
    useHub.getState().onMessage({ type: "snapshot", version: 10, state: s });
    const p = useHub.getState().setVolume("heos-1", 1);
    expect(ft.live()).toHaveLength(1);
    useHub.getState().onMessage({ type: "snapshot", version: 11, state: { ...s, version: 11 } });
    expect(Object.keys(useHub.getState().pending)).toHaveLength(0);
    expect(ft.live()).toHaveLength(0);
    expect(useHub.getState().state?.players["heos-1"]?.volume).toBe(40);
    ft.fire();
    expect(resync).not.toHaveBeenCalled();
    resolveFetch(jsonResponse(okAck("x")));
    await p;
  });

  it("network failure: toasts the hub-unreachable copy and requests a resync", async () => {
    const fetcher = vi.fn(async () => {
      throw new TypeError("Failed to fetch");
    }) as unknown as typeof fetch;
    const { resync } = install(fetcher);
    useHub.getState().onMessage({ type: "snapshot", version: 10, state: sampleState(NOW) });
    const ack = await useHub.getState().skip("heos:heos-1", 15_000);
    expect(ack.ok).toBe(false);
    expect(resync).toHaveBeenCalledTimes(1);
    expect(useToasts.getState().toasts[0]?.message).toBe("Can't reach the hub. Check that the Mac is on the network.");
  });

  it("builds the right request bodies for each command", async () => {
    const calls: { url: string; method: string; body: unknown }[] = [];
    const fetcher = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: String(url).replace(/^https?:\/\/[^/]+/, ""), method: init?.method ?? "", body: init?.body ? JSON.parse(init.body as string) : undefined });
      return jsonResponse(okAck(cidOf(init)));
    }) as unknown as typeof fetch;
    install(fetcher);
    useHub.getState().onMessage({ type: "snapshot", version: 10, state: sampleState(NOW) });
    const h = useHub.getState();
    await h.transport("next", "heos:heos-1");
    await h.skip("heos:heos-1", -15_000);
    await h.setSideVolume("sonos:sonos-gK", 33);
    await h.linkedVolume({ delta: -5 });
    await h.setMute("heos-1", true);
    await h.setGroup("sonos", "sonos-K", ["sonos-K", "sonos-P"]);
    await h.ungroup("sonos:sonos-gK");
    expect(calls.map((c) => c.url)).toEqual(["/api/transport/next", "/api/skip", "/api/volume", "/api/volume", "/api/mute", "/api/group", "/api/group/sonos%3Asonos-gK"]);
    expect(calls[1]!.body).toEqual({ target: "heos:heos-1", delta_ms: -15000 });
    expect(calls[2]!.body).toEqual({ target: "sonos:sonos-gK", level: 33 });
    expect(calls[3]!.body).toEqual({ linked: true, delta: -5 });
    expect(calls[6]!.method).toBe("DELETE");
    expect(useHub.getState().state?.players["sonos-P"]?.volume).toBe(33);
    expect(useHub.getState().state?.players["heos-1"]?.muted).toBe(true);
  });

  it("toggle flips play_state optimistically; skip extrapolates from the hub clock and clamps", async () => {
    const fetcher = okFetcher();
    install(fetcher, () => NOW + 4000); // 4 s after the position was reported
    useHub.getState().onMessage({ type: "snapshot", version: 10, state: sampleState(NOW) });
    const p1 = useHub.getState().transport("toggle", "heos-1");
    expect(useHub.getState().state?.sides["heos:heos-1"]?.play_state).toBe("pause");
    await p1;
    // side is now paused: no extrapolation
    const p2 = useHub.getState().skip("heos:heos-1", 15_000);
    expect(useHub.getState().state?.positions["heos:heos-1"]?.position_ms).toBe(75_000);
    await p2;
    useHub.getState().onMessage({ type: "snapshot", version: 12, state: sampleState(NOW) });
    const p3 = useHub.getState().skip("heos:heos-1", 15_000); // playing: 60s + 4s + 15s
    expect(useHub.getState().state?.positions["heos:heos-1"]?.position_ms).toBe(79_000);
    await p3;
    const p4 = useHub.getState().skip("heos:heos-1", 10_000_000);
    expect(useHub.getState().state?.positions["heos:heos-1"]?.position_ms).toBe(337_000);
    await p4;
  });

  it("linked volume patches every online player proportionally and the side averages", async () => {
    install(okFetcher());
    useHub.getState().onMessage({ type: "snapshot", version: 10, state: sampleState(NOW) });
    const p = useHub.getState().linkedVolume({ level: 20 }); // master 40 → 20: heos 40→20, kitchen 20→10, patio offline
    const st = useHub.getState().state!;
    expect(st.players["heos-1"]!.volume).toBe(20);
    expect(st.players["sonos-K"]!.volume).toBe(10);
    expect(st.players["sonos-P"]!.volume).toBe(60);
    expect(st.sides["heos:heos-1"]!.volume).toBe(20);
    expect(st.sides["sonos:sonos-gK"]!.volume).toBe(35);
    await p;
  });
});
