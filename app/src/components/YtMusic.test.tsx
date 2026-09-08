/**
 * Phase 6 YouTube Music UI: the Settings row is live and shares the device-code sheet with Tidal,
 * the connect card deep link starts the flow, merged grids carry the badge, the picker disables
 * vendors from the item's availability with the hub's own sentence, detail rows flag HEOS-only /
 * Sonos-only tracks. Nothing here hard-codes a vendor: flip `availability.heos` and HEOS rows open.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { axe } from "vitest-axe";
import { BrowseDetail, trackAvailabilityNote } from "./BrowseDetail";
import { CardGrid } from "./HomeScreen";
import { SettingsScreen } from "./SettingsScreen";
import { ZonePicker, unavailableReason } from "./ZonePicker";
import { useHub } from "@/lib/hub/store";
import { useLibrary } from "@/lib/library/store";
import { useChrome, type PlayRequest } from "@/lib/ui/chrome";
import { useToasts } from "@/lib/ui/toasts";
import { HUB_LINKED_SERVICES, SERVICE_LABEL, isHubLinked, serviceUnavailableCopy } from "@/lib/services";
import { unavailableCopy } from "@/lib/pandora";
import { jsonResponse, sampleState } from "@/test/fixtures";
import type { LibraryItem, TrackItem } from "@/lib/hub/library";

const push = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push, back: vi.fn() }), useSearchParams: () => new URLSearchParams() }));
vi.mock("framer-motion", async () => {
  const actual = await vi.importActual<typeof import("framer-motion")>("framer-motion");
  return { ...actual, AnimatePresence: ({ children }: { children: React.ReactNode }) => <>{children}</> };
});

const art = { url: "/api/art/aaaaaaaaaaaaaaaaaaaaaaaa", accent: null, accent_is_safe: false };
const ytAvail = { heos: false, sonos: true, reasons: { heos: "unsupported" as const, sonos: null } };
const ytPlaylist: LibraryItem = { content_ref: { service: "ytmusic", kind: "playlist", id: "PL1" }, title: "Focus", subtitle: "12 tracks", art, duration_ms: null, track_count: 12, availability: ytAvail };
const tidalAlbum: LibraryItem = { content_ref: { service: "tidal", kind: "album", id: "a1" }, title: "Kind of Blue", subtitle: "Miles Davis", art, duration_ms: null, track_count: 5, availability: { heos: true, sonos: true } };
const ytReq = (availability: LibraryItem["availability"] = ytAvail): PlayRequest => ({ content_ref: ytPlaylist.content_ref, title: ytPlaylist.title, subtitle: ytPlaylist.subtitle, art, preferred: [], availability });

type Acct = { service: string; state: string; linked: boolean; account_name: string | null; expires_at: string | null; pending: unknown; last_error: string | null; last_error_code?: string | null };
const acct = (service: string, over: Partial<Acct> = {}): Acct => ({ service, state: "unlinked", linked: false, account_name: null, expires_at: null, pending: null, last_error: null, last_error_code: null, ...over });

/** Settings harness: Tidal linked, YouTube Music not; the ytmusic status endpoint approves on the second poll. */
function bootSettings() {
  let ytLinked = false;
  let statusCalls = 0;
  const calls: string[] = [];
  const fetcher = vi.fn(async (u: RequestInfo | URL, init?: RequestInit) => {
    const url = String(u).replace(/^https?:\/\/[^/]+/, "");
    calls.push(`${init?.method ?? "GET"} ${url}`);
    if (url === "/api/settings")
      return jsonResponse({
        accounts: [acct("tidal", { state: "linked", linked: true, account_name: "james" }), acct("ytmusic", ytLinked ? { state: "linked", linked: true, account_name: "james@gmail.com" } : {}), acct("pandora", { state: "linked", linked: true }), acct("heos_account", { state: "linked", linked: true })],
        hub: { address: "10.0.0.5", port: 8080, https: false, version: "0.6.0", uptime_s: 60, fake_devices: false },
        hardware: [],
      });
    if (url === "/api/auth/ytmusic/start") return jsonResponse({ service: "ytmusic", user_code: "WXYZ-1234", verification_url: "https://www.google.com/device", expires_in_s: 300, interval_s: 1 });
    if (url === "/api/auth/ytmusic/status") {
      statusCalls += 1;
      if (statusCalls >= 2) ytLinked = true;
      return jsonResponse(ytLinked ? acct("ytmusic", { state: "linked", linked: true, account_name: "james@gmail.com" }) : acct("ytmusic", { state: "pending", pending: { user_code: "WXYZ-1234", verification_url: "https://www.google.com/device", expires_at: null } }));
    }
    if (url === "/api/auth/ytmusic/unlink") {
      ytLinked = false;
      return jsonResponse(acct("ytmusic"));
    }
    return jsonResponse({ recents: [], playlists: { items: [], needs_link: [] }, favorite_albums: { items: [], needs_link: [] }, stations: { items: [], needs_link: [] } });
  }) as unknown as typeof fetch;
  useHub.getState()._reset();
  useHub.getState().setPhase("open", Date.now());
  useLibrary.getState()._reset();
  useLibrary.setState({ _deps: { fetcher, now: () => Date.now(), timeoutMs: 8000 } });
  useToasts.setState({ toasts: [] });
  return { calls };
}

function bootPicker() {
  try {
    localStorage.clear();
  } catch {
    // jsdom without storage
  }
  useHub.getState()._reset();
  useHub.getState().onMessage({ type: "snapshot", version: 10, state: sampleState() });
  useChrome.setState({ npExpanded: false, zonesOpen: false, playRequest: null });
  useLibrary.getState()._reset();
}

describe("services", () => {
  it("Tidal and YouTube Music are hub-linked; Pandora is not", () => {
    expect([...HUB_LINKED_SERVICES]).toEqual(["tidal", "ytmusic"]);
    expect(isHubLinked("ytmusic")).toBe(true);
    expect(isHubLinked("pandora")).toBe(false);
    expect(SERVICE_LABEL.ytmusic).toBe("YouTube Music");
  });

  it("the unavailable sentence is the hub's, word for word (docs/api.md contract)", () => {
    const copy = serviceUnavailableCopy("ytmusic", "heos", "unsupported");
    expect(copy).toBe("YouTube Music isn't available on HEOS.");
    // the sentence and the labels are the hub's exported messages, not client prose
    const msgs = JSON.parse(readFileSync(resolve(__dirname, "../lib/hub/messages.json"), "utf8")) as { templates: { not_available: Record<string, string>; pandora_concurrent: string; sync_unsupported_content: string }; service_label: Record<string, string>; vendor_label: Record<string, string>; unsupported_on_vendor: string[][] };
    expect(copy).toBe(msgs.templates.not_available.unsupported!.replace("{service}", msgs.service_label.ytmusic!).replace("{vendor}", msgs.vendor_label.heos!));
    expect(msgs.unsupported_on_vendor).toContainEqual(["heos", "ytmusic"]);
    expect(SERVICE_LABEL).toEqual(msgs.service_label);
    // driven from the hub's reason on the item
    const withReason = { heos: false, sonos: true, reasons: { heos: "unsupported" as const, sonos: null } };
    expect(unavailableCopy("ytmusic", "heos", { availability: withReason })).toBe(copy);
    expect(serviceUnavailableCopy("ytmusic", "heos", "not_linked")).toBe("YouTube Music is set up in the HEOS app.");
    expect(serviceUnavailableCopy("pandora", "sonos", "not_in_account")).toBe("Not in the Sonos Pandora account.");
    // null / unknown reads as not linked
    expect(serviceUnavailableCopy("ytmusic", "sonos", null)).toBe("YouTube Music is set up in the Sonos app.");
    expect(serviceUnavailableCopy("ytmusic", "sonos", "whatever")).toBe("YouTube Music is set up in the Sonos app.");
  });
});

describe("Settings: YouTube Music row", () => {
  it("is live: tapping starts the ytmusic device-code flow in the shared sheet, polls until linked, refreshes; passes axe with both rows", async () => {
    const { calls } = bootSettings();
    const { container } = render(<SettingsScreen />);
    await waitFor(() => expect(screen.getByTestId("account-ytmusic")).toHaveTextContent("Not connected"));
    expect(screen.getByTestId("account-ytmusic")).not.toBeDisabled();
    expect(await axe(container)).toHaveNoViolations();
    await userEvent.click(screen.getByTestId("account-ytmusic"));
    await waitFor(() => expect(screen.getByTestId("link-sheet")).toHaveTextContent("Connect YouTube Music"));
    expect(calls).toContain("POST /api/auth/ytmusic/start");
    expect(screen.getByTestId("user-code")).toHaveTextContent("WXYZ-1234");
    expect(screen.getByTestId("open-verification")).toHaveAttribute("href", "https://www.google.com/device");
    await waitFor(() => expect(screen.getByTestId("link-done")).toHaveTextContent("Connected · james@gmail.com"), { timeout: 5000 });
    expect(calls.filter((c) => c === "GET /api/auth/ytmusic/status").length).toBe(2);
    expect(calls.filter((c) => c === "GET /api/settings").length).toBeGreaterThanOrEqual(2);
    await userEvent.click(screen.getByRole("button", { name: "Done" }));
    await waitFor(() => expect(screen.getByTestId("account-ytmusic")).toHaveTextContent("Connected · james@gmail.com"));
    // unlink goes through the confirm sheet, titled for the service
    await userEvent.click(screen.getByTestId("account-ytmusic"));
    expect(screen.getByTestId("unlink-sheet")).toHaveTextContent("Disconnect YouTube Music?");
    await userEvent.click(screen.getByTestId("confirm-unlink"));
    expect(calls).toContain("POST /api/auth/ytmusic/unlink");
  });

  it("the connect-card deep link (/settings?link=ytmusic) starts the YouTube Music flow on mount", async () => {
    const { calls } = bootSettings();
    render(<SettingsScreen initialLink="ytmusic" />);
    await waitFor(() => expect(calls).toContain("POST /api/auth/ytmusic/start"));
    await waitFor(() => expect(screen.getByTestId("link-sheet")).toHaveTextContent("Connect YouTube Music"));
    expect(calls).not.toContain("POST /api/auth/tidal/start");
  });
});

describe("Home grids: merged services", () => {
  it("YouTube Music and Tidal items sit in one grid, told apart by the badge", () => {
    render(<CardGrid section={{ items: [ytPlaylist, tidalAlbum], needs_link: [], error: null, linked: { tidal: true, ytmusic: true } }} loading={false} connect={[]} onPlay={() => {}} onDetail={() => {}} onConnect={() => {}} emptyCopy="x" testId="g" />);
    const cards = screen.getAllByTestId("grid-card");
    expect(cards).toHaveLength(2);
    expect(within(cards[0]!).getByRole("img", { name: "YouTube Music" })).toBeInTheDocument();
    expect(within(cards[1]!).getByRole("img", { name: "Tidal" })).toBeInTheDocument();
    expect(screen.queryByTestId("connect-card")).toBeNull();
  });
});

describe("Picker with YouTube Music content", () => {
  beforeEach(() => bootPicker());

  it("disables the vendor the item says cannot play it, with the hub's sentence in text-secondary; the other vendor is normal", async () => {
    const { container } = render(<ZonePicker open onClose={() => {}} play={ytReq()} onConfirm={() => {}} />);
    const heosRow = screen.getByTestId("zone-row-heos:heos-1").querySelector("button")!;
    expect(heosRow).toBeDisabled();
    expect(heosRow).toHaveAttribute("aria-disabled", "true");
    const reason = within(heosRow).getByText("YouTube Music isn't available on HEOS.");
    expect(reason.className).toContain("text-secondary");
    expect(screen.getByTestId("zone-row-sonos:sonos-gK").querySelector("button")!).not.toBeDisabled();
    expect(unavailableReason(useHub.getState().state!.sides["heos:heos-1"]!, ytReq())).toBe("YouTube Music isn't available on HEOS.");
    expect(unavailableReason(useHub.getState().state!.sides["sonos:sonos-gK"]!, ytReq())).toBeNull();
    expect(await axe(container)).toHaveNoViolations();
  });

  it("nothing is hard-coded per vendor: when the hub reports HEOS available, the row opens and both vendors show the disabled Sync Play with its reason", () => {
    render(<ZonePicker open onClose={() => {}} play={ytReq({ heos: true, sonos: true })} onConfirm={() => {}} />);
    const heosRow = screen.getByTestId("zone-row-heos:heos-1").querySelector("button")!;
    expect(heosRow).not.toBeDisabled();
    // sampleState pre-highlights the active HEOS side; add Sonos
    fireEvent.click(screen.getByTestId("zone-row-sonos:sonos-gK").querySelector("button")!);
    expect(screen.getByTestId("confirm-play")).toHaveAttribute("data-mode", "play");
    expect(screen.getByTestId("sync-play-disabled")).toBeDisabled();
    expect(screen.getByTestId("sync-reason")).toHaveTextContent("Sync Play works with Tidal content.");
  });
});

describe("Browse detail for a YouTube Music playlist", () => {
  it("shows the badge and flags HEOS-unavailable tracks with the same sentence as the picker, without hiding them", async () => {
    const tracks: TrackItem[] = [0, 1].map((i) => ({
      content_ref: { service: "ytmusic", kind: "track", id: `v${i}` },
      title: `Song ${i + 1}`,
      subtitle: null,
      artist: "Someone",
      album: "Focus",
      art,
      index: i,
      duration_ms: 200_000,
      track_count: null,
      availability: ytAvail,
    }));
    useHub.getState()._reset();
    useHub.getState().onMessage({ type: "snapshot", version: 10, state: sampleState() });
    useHub.getState().selectSide("heos:heos-1");
    useChrome.setState({ npExpanded: false, zonesOpen: false, playRequest: null });
    useLibrary.getState()._reset();
    useLibrary.setState({ _deps: { fetcher: (async () => jsonResponse({ item: ytPlaylist, tracks })) as unknown as typeof fetch, now: () => Date.now(), timeoutMs: 8000 } });
    render(<BrowseDetail contentRef={ytPlaylist.content_ref} />);
    await waitFor(() => expect(screen.getByText("Focus")).toBeInTheDocument());
    expect(screen.getByRole("img", { name: "YouTube Music" })).toBeInTheDocument();
    // uniform availability: one header caption, artists kept on every row, no per-row notes (UX U1 / S3)
    expect(screen.getByTestId("detail-meta")).toHaveTextContent("12 tracks · Sonos rooms only");
    expect(screen.queryAllByTestId("track-note")).toHaveLength(0);
    expect(screen.getAllByText("Someone")).toHaveLength(2);
    expect(screen.getByText("Song 1")).toBeInTheDocument();
    // the active side is HEOS, which cannot play these: titles read secondary, and the helper says why
    expect(screen.getByText("Song 1").className).toContain("text-secondary");
    expect(trackAvailabilityNote(tracks[0]!, "heos")).toBe("YouTube Music isn't available on HEOS.");
    expect(trackAvailabilityNote(tracks[0]!, null)).toBe("Sonos rooms only");
    expect(trackAvailabilityNote(tracks[0]!, "sonos")).toBeNull();
  });
});

describe("Settings: hub setup and polling isolation (Phase 6 nits)", () => {
  /** Fetcher whose ytmusic start endpoint refuses with the hub's needs_client_config sentence. */
  function bootNoCredentials() {
    const sentence = "YouTube Music needs HUB_YTMUSIC_CLIENT_ID and HUB_YTMUSIC_CLIENT_SECRET on the hub (a Google Cloud OAuth client of the TV and Limited Input type).";
    const fetcher = vi.fn(async (u: RequestInfo | URL, init?: RequestInit) => {
      const url = String(u).replace(/^https?:\/\/[^/]+/, "");
      if (url === "/api/settings")
        return jsonResponse({
          accounts: [acct("tidal", { state: "linked", linked: true, account_name: "james" }), { ...acct("ytmusic"), last_error: sentence, last_error_code: "needs_client_config" }, acct("pandora", { state: "linked", linked: true }), acct("heos_account", { state: "linked", linked: true })],
          hub: { address: "10.0.0.5", port: 8080, https: false, version: "0.6.0", uptime_s: 60, fake_devices: false },
          hardware: [],
        });
      if (url === "/api/auth/ytmusic/start" && init?.method === "POST") return jsonResponse({ code: "needs_client_config", message: sentence, service: "ytmusic" }, 409);
      return jsonResponse({ recents: [], playlists: { items: [], needs_link: [] }, favorite_albums: { items: [], needs_link: [] }, stations: { items: [], needs_link: [] } });
    }) as unknown as typeof fetch;
    useHub.getState()._reset();
    useHub.getState().setPhase("open", Date.now());
    useLibrary.getState()._reset();
    useLibrary.setState({ _deps: { fetcher, now: () => Date.now(), timeoutMs: 8000 } });
    useToasts.setState({ toasts: [] });
    return sentence;
  }

  it("needs_client_config: the row reads 'Hub setup needed', the sheet shows the hub's sentence verbatim with Close, never Try again (U4)", async () => {
    const sentence = bootNoCredentials();
    render(<SettingsScreen />);
    await waitFor(() => expect(screen.getByTestId("account-ytmusic")).toHaveTextContent("Not connected · Hub setup needed"));
    expect(screen.getByTestId("account-ytmusic")).not.toHaveTextContent("HUB_YTMUSIC_CLIENT_ID");
    await userEvent.click(screen.getByTestId("account-ytmusic"));
    await waitFor(() => expect(screen.getByTestId("link-error")).toHaveTextContent(sentence));
    expect(screen.getByTestId("link-close")).toHaveTextContent("Close");
    expect(screen.queryByTestId("link-retry")).toBeNull();
    // the terminal phase moved focus to the primary action (U6)
    expect(screen.getByTestId("link-close")).toHaveFocus();
    await userEvent.click(screen.getByTestId("link-close"));
    await waitFor(() => expect(screen.queryByTestId("link-sheet")).toBeNull());
  });

  it("switching from Tidal to YouTube Music mid-poll stops the Tidal poller (zero further tidal status calls); unlinking another account while polling resets the sheet and stops the poller", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const calls: string[] = [];
    let now = 1_000_000;
    const pandoraLinked = true;
    const fetcher = vi.fn(async (u: RequestInfo | URL, init?: RequestInit) => {
      const url = String(u).replace(/^https?:\/\/[^/]+/, "");
      calls.push(`${init?.method ?? "GET"} ${url}`);
      if (url === "/api/settings")
        return jsonResponse({
          accounts: [acct("tidal"), acct("ytmusic"), acct("pandora", { state: pandoraLinked ? "linked" : "unlinked", linked: pandoraLinked }), acct("heos_account", { state: "linked", linked: true })],
          hub: { address: "10.0.0.5", port: 8080, https: false, version: "0.6.0", uptime_s: 60, fake_devices: false },
          hardware: [],
        });
      const m = url.match(/^\/api\/auth\/(tidal|ytmusic)\/(start|status|unlink)$/);
      if (m) {
        const svc = m[1]!;
        if (m[2] === "start") return jsonResponse({ user_code: svc === "tidal" ? "TTTTT" : "YYYYY", verification_url: svc === "tidal" ? "https://link.tidal.com/" : "https://www.google.com/device", expires_in_s: 300, interval_s: 2 });
        if (m[2] === "status") return jsonResponse(acct(svc, { state: "pending", pending: { user_code: "x", verification_url: "https://x/", expires_at: null } }));
        return jsonResponse(acct(svc));
      }
      return jsonResponse({ recents: [], playlists: { items: [], needs_link: [] }, favorite_albums: { items: [], needs_link: [] }, stations: { items: [], needs_link: [] } });
    }) as unknown as typeof fetch;
    useHub.getState()._reset();
    useHub.getState().setPhase("open", Date.now());
    useLibrary.getState()._reset();
    useLibrary.setState({ _deps: { fetcher, now: () => now, timeoutMs: 8000 } });
    useToasts.setState({ toasts: [] });
    const tick = async (ms: number) => {
      await act(async () => {
        now += ms;
        await vi.advanceTimersByTimeAsync(ms);
      });
    };
    const statusCalls = (svc: string) => calls.filter((c) => c === `GET /api/auth/${svc}/status`).length;
    render(<SettingsScreen />);
    await waitFor(() => expect(screen.getByTestId("account-tidal")).toHaveTextContent("Not connected"));
    await userEvent.click(screen.getByTestId("account-tidal"));
    await waitFor(() => expect(screen.getByTestId("user-code")).toHaveTextContent("TTTTT"));
    await tick(2100);
    expect(statusCalls("tidal")).toBe(1);
    // Start YouTube Music straight from the Tidal sheet's row (the sheet is modal, so close it first): the Tidal poller must die.
    await userEvent.click(screen.getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByTestId("user-code")).toBeNull());
    await userEvent.click(screen.getByTestId("account-ytmusic"));
    await waitFor(() => expect(screen.getByTestId("user-code")).toHaveTextContent("YYYYY"));
    const tidalBefore = statusCalls("tidal");
    await tick(2100);
    await tick(2100);
    expect(statusCalls("tidal")).toBe(tidalBefore);
    expect(statusCalls("ytmusic")).toBe(2);
    // Unlinking while the YouTube Music poll runs is not reachable from the row (it is not linked); an unlink
    // of another account goes through the same `unlink()` and must reset the sheet and stop polling.
    await userEvent.click(screen.getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByTestId("user-code")).toBeNull());
    await userEvent.click(screen.getByTestId("account-ytmusic"));
    await waitFor(() => expect(screen.getByTestId("user-code")).toHaveTextContent("YYYYY"));
    const ytBefore = statusCalls("ytmusic");
    await tick(2100);
    expect(statusCalls("ytmusic")).toBe(ytBefore + 1);
    // close via the sheet's own control: polling stops
    await userEvent.click(screen.getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByTestId("link-sheet")).toBeNull());
    const ytAfterClose = statusCalls("ytmusic");
    await tick(4200);
    expect(statusCalls("ytmusic")).toBe(ytAfterClose);
    vi.useRealTimers();
  });

  it("unlink() while a code flow is polling closes the sheet and stops the poller", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const calls: string[] = [];
    let now = 1_000_000;
    const fetcher = vi.fn(async (u: RequestInfo | URL, init?: RequestInit) => {
      const url = String(u).replace(/^https?:\/\/[^/]+/, "");
      calls.push(`${init?.method ?? "GET"} ${url}`);
      if (url === "/api/settings")
        return jsonResponse({
          accounts: [acct("tidal", { state: "linked", linked: true, account_name: "james" }), acct("ytmusic"), acct("pandora", { state: "linked", linked: true }), acct("heos_account", { state: "linked", linked: true })],
          hub: { address: "10.0.0.5", port: 8080, https: false, version: "0.6.0", uptime_s: 60, fake_devices: false },
          hardware: [],
        });
      if (url === "/api/auth/ytmusic/start") return jsonResponse({ user_code: "YYYYY", verification_url: "https://www.google.com/device", expires_in_s: 300, interval_s: 2 });
      if (url === "/api/auth/ytmusic/status") return jsonResponse(acct("ytmusic", { state: "pending", pending: { user_code: "x", verification_url: "https://x/", expires_at: null } }));
      if (url === "/api/auth/tidal/unlink") return jsonResponse(acct("tidal"));
      return jsonResponse({ recents: [], playlists: { items: [], needs_link: [] }, favorite_albums: { items: [], needs_link: [] }, stations: { items: [], needs_link: [] } });
    }) as unknown as typeof fetch;
    useHub.getState()._reset();
    useHub.getState().setPhase("open", Date.now());
    useLibrary.getState()._reset();
    useLibrary.setState({ _deps: { fetcher, now: () => now, timeoutMs: 8000 } });
    useToasts.setState({ toasts: [] });
    render(<SettingsScreen />);
    await waitFor(() => expect(screen.getByTestId("account-ytmusic")).toHaveTextContent("Not connected"));
    await userEvent.click(screen.getByTestId("account-ytmusic"));
    await waitFor(() => expect(screen.getByTestId("user-code")).toHaveTextContent("YYYYY"));
    // The link sheet is modal; the Tidal row sits behind it. Drive the unlink the way the screen does after the confirm sheet.
    await userEvent.click(screen.getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByTestId("link-sheet")).toBeNull());
    await userEvent.click(screen.getByTestId("account-ytmusic"));
    await waitFor(() => expect(screen.getByTestId("user-code")).toHaveTextContent("YYYYY"));
    const before = calls.filter((c) => c === "GET /api/auth/ytmusic/status").length;
    // open the Tidal unlink confirm over the link sheet (both are sheets; the confirm is the later one) and confirm
    await userEvent.click(screen.getByRole("button", { name: "Close" }));
    await userEvent.click(screen.getByTestId("account-tidal"));
    await waitFor(() => expect(screen.getByTestId("unlink-sheet")).toBeVisible());
    await userEvent.click(screen.getByTestId("confirm-unlink"));
    await waitFor(() => expect(calls).toContain("POST /api/auth/tidal/unlink"));
    expect(screen.queryByTestId("link-sheet")).toBeNull();
    await act(async () => {
      now += 4200;
      await vi.advanceTimersByTimeAsync(4200);
    });
    expect(calls.filter((c) => c === "GET /api/auth/ytmusic/status").length).toBe(before);
    vi.useRealTimers();
  });
});

describe("Picker pre-selection (UX U2)", () => {
  it("Now Playing on a HEOS side with YouTube Music content: the only room that can play it (Sonos) is pre-selected", () => {
    bootPicker();
    useHub.getState().selectSide("heos:heos-1");
    render(<ZonePicker open onClose={() => {}} play={ytReq()} onConfirm={() => {}} />);
    expect(screen.getByTestId("zone-row-sonos:sonos-gK").querySelector("button")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("confirm-play")).toBeEnabled();
    expect(screen.getByTestId("confirm-play")).toHaveTextContent(/^Play on /);
  });
});
