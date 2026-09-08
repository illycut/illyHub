import { STALE_MS, errorCode, errorMessage, onRefocus, useLibrary } from "./store";
import { LibraryError } from "../hub/library";
import { jsonResponse } from "@/test/fixtures";

const ref = { service: "tidal" as const, kind: "album" as const, id: "1" };
const home = (items: unknown[] = []) => ({ recents: [], playlists: { items, needs_link: [] }, favorite_albums: { items: [], needs_link: [] }, stations: { items: [], needs_link: [] } });
const item = { content_ref: ref, title: "A", subtitle: null, art: { url: null, accent: null, accent_is_safe: false }, availability: { heos: true, sonos: true } };

function rig(responder: (url: string, init?: RequestInit) => Response | Promise<Response>, timeoutMs = 8000) {
  let now = 1_000_000;
  const fetcher = vi.fn(async (u: RequestInfo | URL, init?: RequestInit) => responder(String(u), init)) as unknown as typeof fetch;
  useLibrary.getState()._reset();
  useLibrary.setState({ _deps: { fetcher, now: () => now, timeoutMs } });
  return { fetcher: fetcher as unknown as ReturnType<typeof vi.fn>, advance: (ms: number) => (now += ms) };
}

describe("useLibrary", () => {
  it("loads home once, serves the cache while fresh, refetches when stale or forced", async () => {
    const { fetcher, advance } = rig(() => jsonResponse(home()));
    await useLibrary.getState().loadHome();
    expect(useLibrary.getState().home.data?.recents.items).toEqual([]);
    await useLibrary.getState().loadHome();
    expect(fetcher).toHaveBeenCalledTimes(1);
    advance(STALE_MS + 1);
    await useLibrary.getState().loadHome();
    expect(fetcher).toHaveBeenCalledTimes(2);
    await useLibrary.getState().loadHome(true);
    expect(fetcher).toHaveBeenCalledTimes(3);
  });

  it("keeps the last good home on error and exposes the message and code", async () => {
    let fail = false;
    const { advance } = rig(() => (fail ? jsonResponse({ code: "vendor_error", message: "Tidal isn't answering." }, 502) : jsonResponse(home([item]))));
    await useLibrary.getState().loadHome();
    fail = true;
    advance(STALE_MS + 1);
    await useLibrary.getState().loadHome();
    const h = useLibrary.getState().home;
    expect(h.data?.playlists.items[0]?.title).toBe("A");
    expect(h.error).toBe("Tidal isn't answering.");
    expect(h.errorCode).toBe("vendor_error");
    expect(h.loading).toBe(false);
  });

  it("a forced refresh during an in-flight load is queued and runs after it settles (A1)", async () => {
    let resolve: (r: Response) => void = () => {};
    let n = 0;
    const { fetcher } = rig(() => {
      n += 1;
      if (n === 1) return new Promise<Response>((r) => (resolve = r));
      return jsonResponse(home([item]));
    });
    const first = useLibrary.getState().loadHome();
    useLibrary.getState().invalidateHome(); // force while loading
    expect(useLibrary.getState().home.forceQueued).toBe(true);
    resolve(jsonResponse(home()));
    await first;
    await vi.waitFor(() => expect(fetcher).toHaveBeenCalledTimes(2));
    await vi.waitFor(() => expect(useLibrary.getState().home.data?.playlists.items[0]?.title).toBe("A"));
    expect(useLibrary.getState().home.forceQueued).toBe(false);
  });

  it("does not double-fetch while a non-forced load is in flight", async () => {
    let resolve: (r: Response) => void = () => {};
    const { fetcher } = rig(() => new Promise<Response>((r) => (resolve = r)));
    const a = useLibrary.getState().loadHome();
    const b = useLibrary.getState().loadHome();
    resolve(jsonResponse(home()));
    await Promise.all([a, b]);
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("a hung fetch is aborted by the deadline: loading clears with a timeout message, and a later load works (A1)", async () => {
    let calls = 0;
    rig((_u, init) => {
      calls += 1;
      if (calls === 1) {
        return new Promise<Response>((_res, rej) => {
          init?.signal?.addEventListener("abort", () => rej(init.signal!.reason ?? new Error("aborted")));
        });
      }
      return jsonResponse(home([item]));
    }, 30);
    await useLibrary.getState().loadHome();
    expect(useLibrary.getState().home.loading).toBe(false);
    expect(useLibrary.getState().home.error).toBe("The hub took too long to answer.");
    expect(useLibrary.getState().home.errorCode).toBe("timeout");
    await useLibrary.getState().loadHome(true);
    expect(useLibrary.getState().home.data?.playlists.items[0]?.title).toBe("A");
  });

  it("caches details per ref and records per-ref errors with codes", async () => {
    rig((u) => (u.includes("/album/1") ? jsonResponse({ item, tracks: [] }) : jsonResponse({ code: "needs_link", message: "Connect Tidal." }, 409)));
    await useLibrary.getState().loadDetail(ref);
    await useLibrary.getState().loadDetail({ ...ref, id: "2" });
    const d = useLibrary.getState().details;
    expect(d["tidal:album:1"]?.data?.item.title).toBe("A");
    expect(d["tidal:album:2"]?.error).toBe("Connect Tidal.");
    expect(d["tidal:album:2"]?.errorCode).toBe("needs_link");
  });

  it("loads settings and invalidateHome forces a home refetch", async () => {
    const { fetcher } = rig((u) => (u.includes("/api/settings") ? jsonResponse({ accounts: [{ service: "tidal", state: "unlinked", linked: false }], hub: {}, hardware: [] }) : jsonResponse(home())));
    await useLibrary.getState().loadSettings();
    expect(useLibrary.getState().settings.data?.accounts[0]?.state).toBe("unlinked");
    await useLibrary.getState().loadHome();
    useLibrary.getState().invalidateHome();
    await vi.waitFor(() => expect(fetcher.mock.calls.filter((c) => String(c[0]).includes("/api/home")).length).toBe(2));
  });

  it("maps errors to design-voice copy and codes", () => {
    expect(errorMessage(new LibraryError("x", "Hub said no.", 500))).toBe("Hub said no.");
    expect(errorMessage(new TypeError("Failed to fetch"))).toBe("Can't reach the hub. Check that the Mac is on the network.");
    const abort = new Error("aborted");
    abort.name = "TimeoutError";
    expect(errorMessage(abort)).toBe("The hub took too long to answer.");
    expect(errorCode(abort)).toBe("timeout");
    expect(errorCode(new TypeError("x"))).toBe("unreachable");
  });

  it("onRefocus fires on focus and on becoming visible, and cleans up", () => {
    const fn = vi.fn();
    const off = onRefocus(fn);
    window.dispatchEvent(new Event("focus"));
    Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
    document.dispatchEvent(new Event("visibilitychange"));
    expect(fn).toHaveBeenCalledTimes(2);
    off();
    window.dispatchEvent(new Event("focus"));
    expect(fn).toHaveBeenCalledTimes(2);
  });
});
