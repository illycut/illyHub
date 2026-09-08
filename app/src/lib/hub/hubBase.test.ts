import { HUB_BASE_KEY, clearHubBase, isNativeShell, normalizeHubBase, probeHub, readHubBase, writeHubBase } from "./hubBase";
import { readHubBase as readAgain } from "./hubBase";
import { hubHttpBase, hubWsUrl } from "./config";

describe("hubBase: runtime hub address", () => {
  const originalEnv = process.env.NEXT_PUBLIC_HUB_URL;
  beforeEach(() => {
    localStorage.clear();
    delete process.env.NEXT_PUBLIC_HUB_URL;
    delete window.__ILLYHUB_NATIVE__;
    delete window.Capacitor;
  });
  afterEach(() => {
    if (originalEnv === undefined) delete process.env.NEXT_PUBLIC_HUB_URL;
    else process.env.NEXT_PUBLIC_HUB_URL = originalEnv;
  });

  it("normalises what people type: scheme, default port, trailing slash", () => {
    expect(normalizeHubBase("192.168.50.10")).toBe("http://192.168.50.10:8080");
    expect(normalizeHubBase("hub-mac.local:9000/")).toBe("http://hub-mac.local:9000");
    expect(normalizeHubBase("https://hub.tail1234.ts.net/")).toBe("https://hub.tail1234.ts.net");
    // Only the origin survives: paths, queries, fragments and credentials are dropped.
    expect(normalizeHubBase("  http://10.0.0.5:8080/index.html?x=1#y ")).toBe("http://10.0.0.5:8080");
    expect(normalizeHubBase("http://user:secret@10.0.0.5:8080/api")).toBe("http://10.0.0.5:8080");
    expect(normalizeHubBase("https://hub.tail1234.ts.net:8443/deep/path/")).toBe("https://hub.tail1234.ts.net:8443");
  });

  it("refuses empty and non-http addresses with one factual sentence", () => {
    expect(() => normalizeHubBase("")).toThrow("Enter the hub address.");
    expect(() => normalizeHubBase("ftp://x")).toThrow(/http:\/\/ or https:\/\//);
    expect(() => normalizeHubBase("http://")).toThrow(/doesn't look like an address/);
  });

  it("stores and clears the address; storage failures do not throw", () => {
    expect(readHubBase()).toBeNull();
    writeHubBase("192.168.50.10");
    expect(localStorage.getItem(HUB_BASE_KEY)).toBe("http://192.168.50.10:8080");
    expect(readHubBase()).toBe("http://192.168.50.10:8080");
    clearHubBase();
    expect(readHubBase()).toBeNull();
    const spy = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota");
    });
    expect(writeHubBase("10.0.0.1")).toBe(false);
    spy.mockRestore();
    expect(writeHubBase("10.0.0.1")).toBe(true);
  });

  it("validates the stored value on the way out: junk and non-http schemes read as null", () => {
    localStorage.setItem(HUB_BASE_KEY, "not an address at all ://");
    expect(readHubBase()).toBeNull();
    localStorage.setItem(HUB_BASE_KEY, "javascript:alert(1)");
    expect(readHubBase()).toBeNull();
    localStorage.setItem(HUB_BASE_KEY, "   ");
    expect(readHubBase()).toBeNull();
    localStorage.setItem(HUB_BASE_KEY, "192.168.50.10/some/path");
    expect(readAgain()).toBe("http://192.168.50.10:8080");
  });

  it("resolution order in the shell: stored address beats the build-time override, which beats the origin", () => {
    window.__ILLYHUB_NATIVE__ = true;
    expect(hubHttpBase()).toBe(window.location.origin);
    process.env.NEXT_PUBLIC_HUB_URL = "http://10.0.0.5:8080/";
    expect(hubHttpBase()).toBe("http://10.0.0.5:8080");
    writeHubBase("https://hub.tail1234.ts.net");
    expect(hubHttpBase()).toBe("https://hub.tail1234.ts.net");
    expect(hubWsUrl()).toBe("wss://hub.tail1234.ts.net/ws");
    clearHubBase();
    expect(hubHttpBase()).toBe("http://10.0.0.5:8080");
  });

  it("a browser tab ignores a stored address and keeps talking to the origin that served it", () => {
    writeHubBase("https://hub.tail1234.ts.net");
    expect(hubHttpBase()).toBe(window.location.origin);
    expect(hubWsUrl()).toBe(window.location.origin.replace(/^http/, "ws") + "/ws");
  });

  it("detects the native shell from the injected flag or Capacitor, never in a plain browser", () => {
    expect(isNativeShell()).toBe(false);
    window.__ILLYHUB_NATIVE__ = true;
    expect(isNativeShell()).toBe(true);
    delete window.__ILLYHUB_NATIVE__;
    window.Capacitor = { isNativePlatform: () => true };
    expect(isNativeShell()).toBe(true);
    window.Capacitor = { isNativePlatform: () => false };
    expect(isNativeShell()).toBe(false);
    window.Capacitor = {
      isNativePlatform: () => {
        throw new Error("boom");
      },
    };
    expect(isNativeShell()).toBe(false);
  });

  it("probes /api/health and reports the version, a non-hub answer, or unreachable", async () => {
    const ok = vi.fn(async () => new Response(JSON.stringify({ version: "0.1.0", status: "ok" }), { status: 200 }));
    await expect(probeHub("192.168.50.10", ok as unknown as typeof fetch)).resolves.toEqual({ ok: true, version: "0.1.0", address: "http://192.168.50.10:8080" });
    expect(ok).toHaveBeenCalledWith("http://192.168.50.10:8080/api/health", expect.objectContaining({ headers: { accept: "application/json" } }));

    const notHub = vi.fn(async () => new Response("<html>", { status: 200 }));
    await expect(probeHub("router.local", notHub as unknown as typeof fetch)).resolves.toEqual({ ok: false, message: "Something answered at http://router.local:8080, but it isn't the hub." });

    const http500 = vi.fn(async () => new Response("nope", { status: 503 }));
    const r = await probeHub("192.168.50.10", http500 as unknown as typeof fetch);
    expect(r).toMatchObject({ ok: false });
    expect((r as { message: string }).message).toContain("HTTP 503");

    const down = vi.fn(async () => {
      throw new TypeError("Failed to fetch");
    });
    await expect(probeHub("192.168.50.99", down as unknown as typeof fetch)).resolves.toEqual({
      ok: false,
      message: "Couldn't reach http://192.168.50.99:8080. Check the address and that you're on the same Wi-Fi.",
    });

    const aborted = vi.fn(async () => {
      const e = new Error("aborted");
      e.name = "AbortError";
      throw e;
    });
    const slow = await probeHub("192.168.50.99", aborted as unknown as typeof fetch);
    expect((slow as { message: string }).message).toMatch(/No answer from http:\/\/192\.168\.50\.99:8080/);

    await expect(probeHub("", ok as unknown as typeof fetch)).resolves.toEqual({ ok: false, message: "Enter the hub address." });
  });

  it("gives up after the probe budget and says the hub did not answer", async () => {
    vi.useFakeTimers();
    try {
      const never = vi.fn((_url: string, init?: { signal?: AbortSignal }) =>
        new Promise<Response>((_res, rej) => {
          init?.signal?.addEventListener("abort", () => {
            const e = new Error("aborted");
            e.name = "AbortError";
            rej(e);
          });
        }),
      );
      const p = probeHub("192.168.50.99", never as unknown as typeof fetch, 5000);
      await vi.advanceTimersByTimeAsync(4999);
      let settled = false;
      void p.then(() => (settled = true));
      await Promise.resolve();
      expect(settled).toBe(false);
      await vi.advanceTimersByTimeAsync(1);
      await expect(p).resolves.toEqual({ ok: false, message: "No answer from http://192.168.50.99:8080. Check that the Mac is on and you're on the same Wi-Fi." });
    } finally {
      vi.useRealTimers();
    }
  });
});
