import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { axe } from "vitest-axe";
import { COMING_LATER, RESTART_FAILED_COPY, RESTART_TIMEOUT_MS, SettingsScreen, accountStatusLine, formatUptime, nextRestartState } from "./SettingsScreen";
import { useHub } from "@/lib/hub/store";
import { useLibrary } from "@/lib/library/store";
import { useToasts } from "@/lib/ui/toasts";
import { jsonResponse } from "@/test/fixtures";
import type { AccountStatus } from "@/lib/hub/library";

const back = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn(), back }), useSearchParams: () => new URLSearchParams() }));

// jsdom never finishes framer-motion exit animations; make AnimatePresence unmount immediately.
vi.mock("framer-motion", async () => {
  const actual = await vi.importActual<typeof import("framer-motion")>("framer-motion");
  return { ...actual, AnimatePresence: ({ children }: { children: React.ReactNode }) => <>{children}</> };
});

type Acct = { service: string; state: string; linked: boolean; account_name: string | null; expires_at: string | null; pending: unknown; last_error: string | null };
const acct = (service: string, over: Partial<Acct> = {}): Acct => ({ service, state: "unlinked", linked: false, account_name: null, expires_at: null, pending: null, last_error: null, ...over });

const settings = (tidal: Partial<Acct>) => ({
  accounts: [acct("tidal", tidal), acct("ytmusic"), acct("pandora", { state: "linked", linked: true }), acct("heos_account", { state: "linked", linked: true, account_name: "james@home" })],
  hub: { address: "10.0.0.5", port: 8080, https: false, version: "0.3.0", uptime_s: 3700, fake_devices: false },
  hardware: [
    { id: "d1", name: "Living Room Amp", vendor: "heos", kind: "player", model: "AVR-X3700H", ip: "10.0.0.5", online: true },
    { id: "s1", name: "Kitchen", vendor: "sonos", kind: "player", model: "One", ip: "10.0.0.7", online: false },
  ],
});

interface Opts {
  linked?: boolean;
  /** Status calls before the fake approves; Infinity = never. */
  approveAfter?: number;
  expiresInS?: number;
  intervalS?: number;
  restartStatus?: number;
  tidalState?: Partial<Acct>;
}

function boot(opts: Opts = {}) {
  let linked = opts.linked ?? false;
  let statusCalls = 0;
  const calls: string[] = [];
  const fetcher = vi.fn(async (u: RequestInfo | URL, init?: RequestInit) => {
    const url = String(u).replace(/^https?:\/\/[^/]+/, "");
    calls.push(`${init?.method ?? "GET"} ${url}`);
    if (url === "/api/settings") return jsonResponse(settings(opts.tidalState ?? (linked ? { state: "linked", linked: true, account_name: "james" } : {})));
    if (url === "/api/auth/tidal/start") return jsonResponse({ service: "tidal", user_code: "ABCDE", verification_url: "https://link.tidal.com/", expires_in_s: opts.expiresInS ?? 300, interval_s: opts.intervalS ?? 2 });
    if (url === "/api/auth/tidal/status") {
      statusCalls += 1;
      if (opts.approveAfter !== undefined && statusCalls >= opts.approveAfter) linked = true;
      return jsonResponse(linked ? acct("tidal", { state: "linked", linked: true, account_name: "james" }) : acct("tidal", { state: "pending", pending: { user_code: "ABCDE", verification_url: "https://link.tidal.com/", expires_at: null } }));
    }
    if (url === "/api/auth/tidal/unlink") {
      linked = false;
      return jsonResponse(acct("tidal"));
    }
    if (url === "/api/hub/restart") {
      const st = opts.restartStatus ?? 202;
      return st === 409 ? jsonResponse({ code: "restart_disabled", message: "Restarting from the app is turned off on this hub." }, 409) : jsonResponse({ restarting: true }, 202);
    }
    return jsonResponse({ recents: [], playlists: { items: [], needs_link: null }, favorite_albums: { items: [], needs_link: null }, stations: { items: [], needs_link: null } });
  }) as unknown as typeof fetch;
  let now = 1_000_000;
  useHub.getState()._reset();
  useHub.getState().setPhase("open", Date.now());
  useLibrary.getState()._reset();
  useLibrary.setState({ _deps: { fetcher, now: () => now, timeoutMs: 8000 } });
  useToasts.setState({ toasts: [] });
  return { calls, advance: (ms: number) => (now += ms), headers: () => (fetcher as unknown as ReturnType<typeof vi.fn>).mock.calls.map((c) => (c[1] as RequestInit | undefined)?.headers as Record<string, string> | undefined) };
}

describe("helpers", () => {
  it("status line, uptime copy, and the restart state machine", () => {
    const base: AccountStatus = { service: "tidal", state: "unlinked", linked: false, account_name: null, expires_at: null, pending: null, last_error: null, linked_by_vendor: null };
    expect(accountStatusLine(base)).toBe("Not connected");
    expect(accountStatusLine({ ...base, last_error: "Token refresh failed" })).toBe("Not connected · Token refresh failed");
    expect(accountStatusLine({ ...base, state: "linked", linked: true, account_name: "james" })).toBe("Connected · james");
    expect(accountStatusLine({ ...base, state: "linked", linked: true })).toBe("Connected");
    expect(accountStatusLine({ ...base, state: "pending" })).toBe("Waiting for approval…");
    expect(accountStatusLine({ ...base, state: "restoring" })).toBe("Reconnecting…");
    expect(formatUptime(90061)).toBe("1d 1h");
    expect(formatUptime(3700)).toBe("1h 1m");
    expect(formatUptime(120)).toBe("2m");
    expect(formatUptime(null)).toBe("");
    // restart: waiting -> (still open) waiting -> (closed) sawClosed -> (open) idle; or timeout -> failed
    const w = { kind: "waiting" as const, sawClosed: false, since: 0 };
    expect(nextRestartState(w, true, 100)).toEqual(w);
    const closed = nextRestartState(w, false, 200);
    expect(closed).toEqual({ ...w, sawClosed: true });
    expect(nextRestartState(closed, true, 300)).toEqual({ kind: "idle" });
    expect(nextRestartState(w, true, RESTART_TIMEOUT_MS)).toEqual({ kind: "failed" });
  });
});

describe("SettingsScreen", () => {
  it("renders the three groups from /api/settings; later-phase rows are disabled without opacity and read 'Coming later' (U5–U7); offline hardware is muted with a trailing Offline label (U8); passes axe", async () => {
    boot({ linked: true });
    const { container } = render(<SettingsScreen />);
    await waitFor(() => expect(screen.getByTestId("account-tidal")).toHaveTextContent("Connected · james"));
    const yt = screen.getByTestId("account-ytmusic");
    expect(yt).toBeDisabled();
    expect(yt).toHaveAttribute("aria-disabled", "true");
    expect(yt).toHaveTextContent(COMING_LATER);
    expect(yt.className).not.toContain("opacity");
    expect(within(yt).queryByText("Connected")).toBeNull();
    // Pandora is "linked" per the hub but disabled this phase: the line is the reason, never "Connected" (U6)
    const pandora = screen.getByTestId("account-pandora");
    expect(pandora).toHaveTextContent(COMING_LATER);
    expect(pandora).not.toHaveTextContent("Connected");
    expect(screen.getByTestId("account-heos_account")).toHaveTextContent("Connected · james@home");
    expect(screen.getByTestId("hub-status")).toHaveTextContent("10.0.0.5:8080");
    expect(screen.getByTestId("hub-status")).toHaveTextContent("Version 0.3.0 · up 1h 1m");
    expect(screen.getByTestId("check-updates")).toHaveTextContent(COMING_LATER);
    const hw = screen.getAllByTestId("hardware-row");
    expect(hw).toHaveLength(2);
    expect(hw[0]).toHaveTextContent("HEOS · AVR-X3700H · 10.0.0.5");
    expect(hw[1]).toHaveTextContent("Offline");
    expect(within(hw[1]!).getByText("Offline").className).toContain("text-micro");
    expect(within(hw[1]!).getByText("Kitchen").className).toContain("text-secondary");
    expect(await axe(container)).toHaveNoViolations();
  });

  it("restoring account renders a skeleton row, not 'Not connected'; last_error shows on the row (A4/A10)", async () => {
    boot({ tidalState: { state: "restoring" } });
    const { unmount } = render(<SettingsScreen />);
    await waitFor(() => expect(screen.getByTestId("account-tidal")).toBeInTheDocument());
    expect(within(screen.getByTestId("account-tidal")).queryByText(/Not connected/)).toBeNull();
    expect(screen.getByTestId("account-tidal").querySelector("[data-skeleton]")).not.toBeNull();
    unmount();
    boot({ tidalState: { last_error: "Token refresh failed" } });
    render(<SettingsScreen />);
    await waitFor(() => expect(screen.getByTestId("account-tidal")).toHaveTextContent("Not connected · Token refresh failed"));
  });

  it("link flow polls at the hub's interval_s until linked, then refreshes settings and home (A4)", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { calls, advance } = boot({ approveAfter: 2, intervalS: 3 });
    render(<SettingsScreen />);
    await waitFor(() => expect(screen.getByTestId("account-tidal")).toHaveTextContent("Not connected"));
    await userEvent.click(screen.getByTestId("account-tidal"));
    await waitFor(() => expect(screen.getByTestId("user-code")).toHaveTextContent("ABCDE"));
    expect(screen.getByTestId("open-verification")).toHaveAttribute("href", "https://link.tidal.com/");
    expect(screen.getByTestId("open-verification")).toHaveTextContent("Open link.tidal.com");
    // 2 s (the default) is not enough: the hub said 3 s
    await act(async () => {
      advance(2100);
      await vi.advanceTimersByTimeAsync(2100);
    });
    expect(calls.filter((c) => c === "GET /api/auth/tidal/status").length).toBe(0);
    await act(async () => {
      advance(1000);
      await vi.advanceTimersByTimeAsync(1000);
    });
    await act(async () => {
      advance(3100);
      await vi.advanceTimersByTimeAsync(3100);
    });
    await waitFor(() => expect(screen.getByTestId("link-done")).toHaveTextContent("Connected · james"));
    expect(screen.getByTestId("link-done")).not.toHaveTextContent(/\.$/);
    expect(calls.filter((c) => c === "GET /api/auth/tidal/status").length).toBe(2);
    expect(calls.filter((c) => c === "GET /api/settings").length).toBeGreaterThanOrEqual(2);
    expect(calls.some((c) => c === "GET /api/home")).toBe(true);
    await userEvent.click(screen.getByRole("button", { name: "Done" }));
    await waitFor(() => expect(screen.queryByTestId("link-sheet")).toBeNull());
    vi.useRealTimers();
  });

  it("an unapproved code expires at expires_in_s: polling stops, 'Get a new code' starts a fresh code (A4/U1)", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { calls, advance } = boot({ expiresInS: 5, intervalS: 2 });
    render(<SettingsScreen />);
    await waitFor(() => expect(screen.getByTestId("account-tidal")).toBeInTheDocument());
    await userEvent.click(screen.getByTestId("account-tidal"));
    await waitFor(() => expect(screen.getByTestId("user-code")).toBeInTheDocument());
    for (let i = 0; i < 3; i++) {
      await act(async () => {
        advance(2100);
        await vi.advanceTimersByTimeAsync(2100);
      });
    }
    await waitFor(() => expect(screen.getByTestId("link-expired")).toHaveTextContent("This code expired."));
    const before = calls.filter((c) => c === "GET /api/auth/tidal/status").length;
    await act(async () => {
      advance(5000);
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(calls.filter((c) => c === "GET /api/auth/tidal/status").length).toBe(before); // polling stopped
    await userEvent.click(screen.getByTestId("new-code"));
    await waitFor(() => expect(calls.filter((c) => c === "POST /api/auth/tidal/start").length).toBe(2));
    await waitFor(() => expect(screen.getByTestId("user-code")).toBeInTheDocument());
    vi.useRealTimers();
  });

  it("unmounting mid-flow stops polling (A4)", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { calls, advance } = boot({ intervalS: 1 });
    const { unmount } = render(<SettingsScreen initialLink="tidal" />);
    await waitFor(() => expect(screen.getByTestId("user-code")).toBeInTheDocument());
    unmount();
    const before = calls.filter((c) => c === "GET /api/auth/tidal/status").length;
    await act(async () => {
      advance(5000);
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(calls.filter((c) => c === "GET /api/auth/tidal/status").length).toBe(before);
    vi.useRealTimers();
  });

  it("unlink confirms in a bottom sheet and refreshes", async () => {
    const { calls } = boot({ linked: true });
    render(<SettingsScreen />);
    await waitFor(() => expect(screen.getByTestId("account-tidal")).toHaveTextContent("Connected · james"));
    await userEvent.click(screen.getByTestId("account-tidal"));
    const sheet = screen.getByTestId("unlink-sheet");
    expect(sheet).toHaveAttribute("role", "dialog");
    expect(within(sheet).getByRole("heading")).toHaveTextContent("Disconnect Tidal?");
    await userEvent.click(screen.getByTestId("confirm-unlink"));
    await waitFor(() => expect(calls).toContain("POST /api/auth/tidal/unlink"));
    await waitFor(() => expect(screen.getByTestId("account-tidal")).toHaveTextContent("Not connected"));
  });

  it("restart: confirm posts with X-Illyhub, the notice waits for the socket to drop and return (A3)", async () => {
    const { calls, headers } = boot({ linked: true });
    render(<SettingsScreen />);
    await waitFor(() => expect(screen.getByTestId("restart-hub")).toBeInTheDocument());
    await userEvent.click(screen.getByTestId("restart-hub"));
    expect(screen.getByTestId("restart-sheet")).toBeInTheDocument();
    // production order: the socket is open when the user confirms
    await userEvent.click(screen.getByTestId("confirm-restart"));
    await waitFor(() => expect(calls).toContain("POST /api/hub/restart"));
    expect(headers().find((h) => h?.["X-Illyhub"])).toBeDefined();
    expect(screen.getByTestId("restarting")).toBeInTheDocument();
    // still open: the notice must not clear yet
    await act(async () => {
      await new Promise((r) => setTimeout(r, 600));
    });
    expect(screen.getByTestId("restarting")).toBeInTheDocument();
    act(() => useHub.getState().setPhase("closed", Date.now()));
    await act(async () => {
      await new Promise((r) => setTimeout(r, 600));
    });
    act(() => useHub.getState().setPhase("open", Date.now()));
    await waitFor(() => expect(screen.queryByTestId("restarting")).toBeNull(), { timeout: 3000 });
  });

  it("restart: the hub refusing (409 restart_disabled) clears the notice and toasts the hub's message (A2)", async () => {
    boot({ linked: true, restartStatus: 409 });
    render(<SettingsScreen />);
    await waitFor(() => expect(screen.getByTestId("restart-hub")).toBeInTheDocument());
    await userEvent.click(screen.getByTestId("restart-hub"));
    await userEvent.click(screen.getByTestId("confirm-restart"));
    await waitFor(() => expect(useToasts.getState().toasts.map((t) => t.message)).toContain("Restarting from the app is turned off on this hub."));
    expect(screen.queryByTestId("restarting")).toBeNull();
  });

  it("restart: if the socket never comes back within the timeout the notice becomes a failure (A3)", async () => {
    const { advance } = boot({ linked: true });
    render(<SettingsScreen />);
    await waitFor(() => expect(screen.getByTestId("restart-hub")).toBeInTheDocument());
    await userEvent.click(screen.getByTestId("restart-hub"));
    await userEvent.click(screen.getByTestId("confirm-restart"));
    await waitFor(() => expect(screen.getByTestId("restarting")).toBeInTheDocument());
    act(() => useHub.getState().setPhase("closed", Date.now()));
    advance(RESTART_TIMEOUT_MS + 1);
    await waitFor(() => expect(screen.getByTestId("restart-failed")).toHaveTextContent(RESTART_FAILED_COPY), { timeout: 3000 });
  });

  it("back button navigates back", async () => {
    boot();
    render(<SettingsScreen />);
    await userEvent.click(screen.getByTestId("back"));
    expect(back).toHaveBeenCalled();
  });
});
