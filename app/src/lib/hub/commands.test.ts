import { clampLevel, commands, sendCommand } from "./commands";
import { jsonResponse, okAck } from "@/test/fixtures";

describe("sendCommand", () => {
  it("posts JSON with the correlation header and returns the ack body even on non-2xx", async () => {
    const fetcher = vi.fn(async (_u: RequestInfo | URL, init?: RequestInit) => {
      const cid = (init?.headers as Record<string, string>)["x-correlation-id"];
      return jsonResponse(okAck(cid, { ok: false }), 409);
    }) as unknown as typeof fetch;
    const ack = await sendCommand(commands.transport("play", "all", "abc"), fetcher);
    expect(ack.correlation_id).toBe("abc");
    expect(ack.ok).toBe(false);
  });

  it("synthesises a vendor_error ack when the body is not an ack", async () => {
    const fetcher = vi.fn(async () => new Response("<html>", { status: 502 })) as unknown as typeof fetch;
    const ack = await sendCommand(commands.mute("x", true, "cid1"), fetcher);
    expect(ack.ok).toBe(false);
    expect(ack.error?.code).toBe("vendor_error");
    expect(ack.error?.message).toContain("502");
    const ok = vi.fn(async () => new Response("", { status: 200 })) as unknown as typeof fetch;
    const ack2 = await sendCommand(commands.mute("x", true, "cid2"), ok);
    expect(ack2.error?.message).toMatch(/unexpected response/);
  });

  it("builds bodies with clamping and rounding", () => {
    expect(commands.volume("p", 140).body).toEqual({ target: "p", level: 100 });
    expect(commands.volume("p", -3).body).toEqual({ target: "p", level: 0 });
    expect(commands.seek("s", -10).body).toEqual({ target: "s", position_ms: 0 });
    expect(commands.seek("s", 1234.6).body).toEqual({ target: "s", position_ms: 1235 });
    expect(commands.linkedVolume({ level: 50 }).body).toEqual({ linked: true, level: 50 });
    expect(commands.linkedVolume({ delta: 4.4 }).body).toEqual({ linked: true, delta: 4 });
    expect(commands.ungroup("a:b").path).toBe("/api/group/a%3Ab");
    expect(clampLevel(55.5)).toBe(56);
  });
});
