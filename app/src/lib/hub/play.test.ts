/**
 * play() fan-out (A7): one POST per side, sequential; a "nothing was touched" failure on the first
 * side short-circuits the rest with one actionable toast and no resync; a per-side failure toasts
 * once naming the room and keeps the other side's state.
 */
import { useHub } from "./store";
import { useToasts } from "@/lib/ui/toasts";
import { jsonResponse, okAck, sampleState } from "@/test/fixtures";

const item = { content_ref: { service: "tidal" as const, kind: "album" as const, id: "101" }, title: "Warm Glow", subtitle: "Analog Heart", art: { url: "/api/art/k", accent: null, accent_is_safe: false } };
const cidOf = (init?: RequestInit) => (init?.headers as Record<string, string>)["x-correlation-id"];

function rig(responder: (target: string, init: RequestInit) => Response) {
  const posts: { target: string; headers: Record<string, string> }[] = [];
  const fetcher = vi.fn(async (_u: RequestInfo | URL, init?: RequestInit) => {
    const body = JSON.parse(init!.body as string) as { target: string };
    posts.push({ target: body.target, headers: init!.headers as Record<string, string> });
    return responder(body.target, init!);
  }) as unknown as typeof fetch;
  const resync = vi.fn();
  useHub.getState()._reset();
  useToasts.setState({ toasts: [] });
  useHub.setState({ _deps: { fetcher, now: () => Date.now(), setTimer: (fn, ms) => setTimeout(fn, ms), clearTimer: (t) => clearTimeout(t as ReturnType<typeof setTimeout>) } });
  useHub.getState().onMessage({ type: "snapshot", version: 10, state: sampleState() });
  useHub.getState().setResync(resync);
  return { posts, resync };
}

const envelope = (code: string, message: string, status = 409) => jsonResponse({ code, message, service: "tidal" }, status);

describe("play()", () => {
  it("happy path: one POST per side with X-Illyhub, optimistic now-playing on every side, acks returned", async () => {
    const { posts, resync } = rig((_t, init) => jsonResponse(okAck(cidOf(init), { applied: ["x"] })));
    const acks = await useHub.getState().play(["heos:heos-1", "sonos:sonos-gK"], item);
    expect(acks.map((a) => a.ok)).toEqual([true, true]);
    expect(posts.map((p) => p.target)).toEqual(["heos:heos-1", "sonos:sonos-gK"]);
    for (const p of posts) expect(p.headers["X-Illyhub"]).toBe("1");
    const s = useHub.getState().state!;
    expect(s.now_playing["sonos:sonos-gK"]?.title).toBe("Warm Glow");
    expect(s.sides["sonos:sonos-gK"]?.play_state).toBe("play");
    expect(resync).not.toHaveBeenCalled();
    expect(useToasts.getState().toasts).toEqual([]);
  });

  it("needs_link on the first side: stops before the second POST, one toast with a Connect action, no resync, optimistic entries undone", async () => {
    const { posts, resync } = rig(() => envelope("needs_link", "Tidal is not connected. Link it in Settings."));
    const before = useHub.getState().state!;
    const acks = await useHub.getState().play(["heos:heos-1", "sonos:sonos-gK"], item);
    expect(posts.length).toBe(1);
    expect(acks.length).toBe(1);
    expect(acks[0]?.error?.code).toBe("needs_link");
    const toasts = useToasts.getState().toasts;
    expect(toasts.length).toBe(1);
    expect(toasts[0]?.message).toBe("Tidal is not connected. Link it in Settings.");
    expect(toasts[0]?.action).toEqual({ label: "Connect Tidal", href: "/settings?link=tidal" });
    expect(resync).not.toHaveBeenCalled();
    const after = useHub.getState().state!;
    expect(after.sides["heos:heos-1"]).toEqual(before.sides["heos:heos-1"]);
    expect(after.now_playing["heos:heos-1"]).toEqual(before.now_playing["heos:heos-1"]);
    expect(after.now_playing["sonos:sonos-gK"]).toEqual(before.now_playing["sonos:sonos-gK"]);
    expect(useHub.getState().pending).toEqual({});
  });

  it("not_available_on_side on one side: one toast naming the room, that side's optimistic state undone, the other side kept", async () => {
    const { posts, resync } = rig((target, init) =>
      target === "heos:heos-1"
        ? jsonResponse({ ...okAck(cidOf(init)), ok: false, error: { code: "not_available_on_side", message: "Living Room Amp has no Tidal account.", target, correlation_id: cidOf(init) } }, 409)
        : jsonResponse(okAck(cidOf(init))),
    );
    const before = useHub.getState().state!;
    const acks = await useHub.getState().play(["heos:heos-1", "sonos:sonos-gK"], item);
    expect(posts.map((p) => p.target)).toEqual(["heos:heos-1", "sonos:sonos-gK"]);
    expect(acks.map((a) => a.ok)).toEqual([false, true]);
    const toasts = useToasts.getState().toasts;
    expect(toasts.length).toBe(1);
    expect(toasts[0]?.message).toBe("Living Room Amp has no Tidal account.");
    expect(toasts[0]?.action).toBeNull();
    const after = useHub.getState().state!;
    expect(after.now_playing["heos:heos-1"]).toEqual(before.now_playing["heos:heos-1"]);
    expect(after.now_playing["sonos:sonos-gK"]?.title).toBe("Warm Glow");
    expect(after.sides["sonos:sonos-gK"]?.play_state).toBe("play");
    expect(resync).not.toHaveBeenCalled();
  });

  it("an ok ack with partial failures toasts each partial once", async () => {
    rig((_t, init) => jsonResponse(okAck(cidOf(init), { partial: [{ target: "sonos-P", code: "device_offline", message: "Patio is offline." }] })));
    await useHub.getState().play(["sonos:sonos-gK"], item);
    expect(useToasts.getState().toasts.map((t) => t.message)).toEqual(["Patio is offline."]);
  });
});
