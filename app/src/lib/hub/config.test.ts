import { artUrl, hubHttpBase, hubWsUrl } from "./config";

describe("hub config", () => {
  const original = process.env.NEXT_PUBLIC_HUB_URL;
  afterEach(() => {
    if (original === undefined) delete process.env.NEXT_PUBLIC_HUB_URL;
    else process.env.NEXT_PUBLIC_HUB_URL = original;
  });

  it("uses the page origin by default and derives the ws url", () => {
    delete process.env.NEXT_PUBLIC_HUB_URL;
    expect(hubHttpBase()).toBe(window.location.origin);
    expect(hubWsUrl()).toBe(window.location.origin.replace(/^http/, "ws") + "/ws");
  });

  it("honours NEXT_PUBLIC_HUB_URL (trailing slash stripped) for http and ws", () => {
    process.env.NEXT_PUBLIC_HUB_URL = "http://10.0.0.5:8080/";
    expect(hubHttpBase()).toBe("http://10.0.0.5:8080");
    expect(hubWsUrl()).toBe("ws://10.0.0.5:8080/ws");
    process.env.NEXT_PUBLIC_HUB_URL = "https://hub.tailnet.ts.net";
    expect(hubWsUrl()).toBe("wss://hub.tailnet.ts.net/ws");
  });

  it("builds art urls with the size parameter and tolerates missing art", () => {
    delete process.env.NEXT_PUBLIC_HUB_URL;
    expect(artUrl({ url: "/api/art/abc" }, 320)).toBe(`${window.location.origin}/api/art/abc?size=320`);
    expect(artUrl({ url: "https://cdn.example/x.jpg?foo=1" }, "backdrop")).toBe("https://cdn.example/x.jpg?foo=1&size=backdrop");
    expect(artUrl({ url: null }, 96)).toBeNull();
    expect(artUrl(undefined, 96)).toBeNull();
  });
});
