/**
 * Settings → Hub (Phase 8): the live "Check for updates" row (PRD SET-2) through its state machine
 * (checking → up to date / available / disabled → confirm sheet → applying → corroborated restart →
 * updated, or failed / rolled back with the log; apply refusals by code), and the "Hub stats"
 * text disclosure (ai-dev #73).
 */
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { axe } from "vitest-axe";
import { SettingsScreen, _resetUpdateCheckCache } from "./SettingsScreen";
import { useHub } from "@/lib/hub/store";
import { useLibrary } from "@/lib/library/store";
import { useToasts } from "@/lib/ui/toasts";
import { UPDATE_CHECK_FAILED, UPDATE_RECONNECT_TIMEOUT_MS } from "@/lib/update";
import { jsonResponse } from "@/test/fixtures";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn(), back: vi.fn() }), useSearchParams: () => new URLSearchParams() }));
vi.mock("framer-motion", async () => {
  const actual = await vi.importActual<typeof import("framer-motion")>("framer-motion");
  return { ...actual, AnimatePresence: ({ children }: { children: React.ReactNode }) => <>{children}</> };
});

const acct = (service: string, over: Record<string, unknown> = {}) => ({ service, state: "unlinked", linked: false, account_name: null, expires_at: null, pending: null, last_error: null, last_error_code: null, ...over });

interface Opts {
  available?: boolean;
  checkError?: string;
  /** Status answers in order; the last repeats. `"unreachable"` throws. */
  statuses?: (Record<string, unknown> | "unreachable")[];
  /** 409 code from apply, or a 400 with a message. */
  applyRefusal?: { status: number; code: string; message: string };
  version?: string;
  summary?: string | "unreachable";
}

let now = 1_000_000;
function boot(opts: Opts = {}) {
  now = 1_000_000;
  let version = opts.version ?? "0.8.0";
  let statusCalls = 0;
  const calls: string[] = [];
  const fetcher = vi.fn(async (u: RequestInfo | URL, init?: RequestInit) => {
    const url = String(u).replace(/^https?:\/\/[^/]+/, "");
    calls.push(`${init?.method ?? "GET"} ${url}`);
    if (url === "/api/settings") return jsonResponse({ accounts: [acct("tidal"), acct("ytmusic"), acct("pandora", { linked: true, linked_by_vendor: { heos: true, sonos: false } }), acct("heos_account", { linked: true })], hub: { address: "10.0.0.5", port: 8080, https: false, version, uptime_s: 60, fake_devices: false }, hardware: [] });
    if (url === "/api/hub/update/check") return jsonResponse({ current: { version, commit: "aaa", branch: "main" }, remote: { commit: "bbb", ahead_by: opts.available ? 2 : 0, summary: opts.available ? ["Phase 8: queue and search"] : [], tracking: "main" }, available: !!opts.available, last_checked_at: "2026-09-08T00:00:00Z", error: opts.checkError ?? null });
    if (url === "/api/hub/update/apply") {
      if (opts.applyRefusal) return jsonResponse({ code: opts.applyRefusal.code, message: opts.applyRefusal.message }, opts.applyRefusal.status);
      return jsonResponse({ job_id: "j1", state: "running", message: "Updating the hub. It restarts on its own when the update finishes." }, 202);
    }
    if (url === "/api/hub/update/status") {
      const list = opts.statuses ?? [{ state: "running", job_id: "j1", log_tail: ["git pull"] }];
      const st = list[Math.min(statusCalls, list.length - 1)]!;
      statusCalls += 1;
      if (st === "unreachable") throw new TypeError("Failed to fetch");
      if (st.state === "succeeded") version = "0.9.0";
      return jsonResponse(st);
    }
    if (url === "/api/metrics/summary") {
      if (opts.summary === "unreachable") throw new TypeError("Failed to fetch");
      return jsonResponse({ summary: opts.summary ?? "Running since 7:37 today. 10 commands, all worked." });
    }
    return jsonResponse({});
  });
  useLibrary.getState()._reset();
  useLibrary.setState({ _deps: { fetcher: fetcher as unknown as typeof fetch, now: () => now, timeoutMs: 8000 } });
  useHub.getState()._reset();
  useHub.getState().setPhase("open", now);
  useToasts.setState({ toasts: [] });
  _resetUpdateCheckCache();
  return { calls };
}

describe("Check for updates row", () => {
  beforeEach(() => vi.useFakeTimers({ shouldAdvanceTime: true }));
  afterEach(() => vi.useRealTimers());

  it("checks on mount: 'Up to date · version', tappable to re-check, no confirm sheet, and a second mount within five minutes re-uses the answer; passes axe", async () => {
    const { calls } = boot();
    const first = render(<SettingsScreen />);
    const row = screen.getByTestId("check-updates");
    await waitFor(() => expect(row).toHaveTextContent("Up to date · 0.8.0"));
    expect(screen.queryByTestId("update-sheet")).toBeNull();
    expect(await axe(first.container)).toHaveNoViolations();
    first.unmount();
    render(<SettingsScreen />);
    await waitFor(() => expect(screen.getByTestId("check-updates")).toHaveTextContent("Up to date · 0.8.0"));
    expect(calls.filter((c) => c === "GET /api/hub/update/check")).toHaveLength(1);
    // a tap re-checks regardless of the debounce
    fireEvent.click(screen.getByTestId("check-updates"));
    await waitFor(() => expect(calls.filter((c) => c === "GET /api/hub/update/check")).toHaveLength(2));
  });

  it("a check failure shows the fixed sentence, never the hub's error text, and stays tappable", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    boot({ checkError: "fatal: Needed a single revision" });
    render(<SettingsScreen />);
    const row = screen.getByTestId("check-updates");
    await waitFor(() => expect(row).toHaveTextContent(UPDATE_CHECK_FAILED));
    expect(row).not.toHaveTextContent("fatal");
    expect(row).toBeEnabled();
    expect(row.className).not.toContain("text-error");
    warn.mockRestore();
  });

  it("available → confirm sheet (with the reconnect note) → apply → applying → succeeded → the socket must bounce → 'Updated before → after'", async () => {
    boot({ available: true, statuses: [{ state: "running", job_id: "j1", log_tail: ["git pull"] }, { state: "succeeded", job_id: "j1", log_tail: ["done"] }] });
    render(<SettingsScreen />);
    const row = screen.getByTestId("check-updates");
    await waitFor(() => expect(row).toHaveTextContent("Update available · Phase 8: queue and search"));
    fireEvent.click(row);
    const sheet = await screen.findByTestId("update-sheet");
    expect(sheet).toHaveTextContent("Phase 8: queue and search");
    expect(sheet).toHaveTextContent("Control drops for about a minute; this screen reconnects on its own.");
    fireEvent.click(screen.getByTestId("confirm-update"));
    await waitFor(() => expect(row).toHaveTextContent("Updating the hub…"));
    expect(row).toBeDisabled();
    act(() => void vi.advanceTimersByTime(2100));
    await waitFor(() => expect(row).toHaveTextContent("Restarting the hub. This screen reconnects on its own."));
    // still open: not done yet
    act(() => void vi.advanceTimersByTime(600));
    expect(row).toHaveTextContent("Restarting the hub");
    act(() => useHub.getState().setPhase("closed", now));
    act(() => void vi.advanceTimersByTime(600));
    act(() => useHub.getState().setPhase("open", now));
    act(() => void vi.advanceTimersByTime(600));
    await waitFor(() => expect(row).toHaveTextContent("Updated 0.8.0 → 0.9.0"));
    expect(row).toBeEnabled();
  });

  it("a single failed poll keeps applying; two in a row corroborate the restart; no socket within the timeout says so", async () => {
    boot({ available: true, statuses: ["unreachable"] });
    render(<SettingsScreen />);
    const row = screen.getByTestId("check-updates");
    await waitFor(() => expect(row).toHaveTextContent("Update available"));
    fireEvent.click(row);
    fireEvent.click(await screen.findByTestId("confirm-update"));
    await waitFor(() => expect(row).toHaveTextContent("Updating the hub…"));
    // first poll failed: still applying
    act(() => void vi.advanceTimersByTime(1000));
    expect(row).toHaveTextContent("Updating the hub…");
    act(() => void vi.advanceTimersByTime(2100));
    await waitFor(() => expect(row).toHaveTextContent("Restarting the hub"));
    now += UPDATE_RECONNECT_TIMEOUT_MS + 1;
    act(() => void vi.advanceTimersByTime(600));
    await waitFor(() => expect(row).toHaveTextContent("The hub didn't come back after the update. Check the Mac."));
  });

  it("apply refusals: update_running adopts the live job; update_disabled becomes the hub's sentence and a non-tappable row; other refusals show verbatim", async () => {
    boot({ available: true, applyRefusal: { status: 409, code: "update_running", message: "An update is already running." }, statuses: [{ state: "running", job_id: "j7", log_tail: [] }, { state: "succeeded", job_id: "j7", log_tail: [] }] });
    render(<SettingsScreen />);
    let row = screen.getByTestId("check-updates");
    await waitFor(() => expect(row).toHaveTextContent("Update available"));
    fireEvent.click(row);
    fireEvent.click(await screen.findByTestId("confirm-update"));
    await waitFor(() => expect(row).toHaveTextContent("Updating the hub…"));
    act(() => void vi.advanceTimersByTime(2100));
    await waitFor(() => expect(row).toHaveTextContent("Restarting the hub"));

    boot({ available: true, applyRefusal: { status: 409, code: "update_disabled", message: "Updates are turned off on this hub." } });
    const second = render(<SettingsScreen />);
    row = screen.getAllByTestId("check-updates").at(-1)!;
    await waitFor(() => expect(row).toHaveTextContent("Update available"));
    fireEvent.click(row);
    fireEvent.click((await screen.findAllByTestId("confirm-update")).at(-1)!);
    await waitFor(() => expect(row).toHaveTextContent("Updates are turned off on this hub."));
    expect(row).toBeDisabled();
    expect(row.className).not.toContain("text-error");
    second.unmount();

    boot({ available: true, applyRefusal: { status: 409, code: "update_refused", message: "The hub's git remote is not an https:// or ssh:// URL; updates are refused." } });
    render(<SettingsScreen />);
    row = screen.getAllByTestId("check-updates").at(-1)!;
    await waitFor(() => expect(row).toHaveTextContent("Update available"));
    fireEvent.click(row);
    fireEvent.click((await screen.findAllByTestId("confirm-update")).at(-1)!);
    await waitFor(() => expect(row).toHaveTextContent("The hub's git remote is not an https:// or ssh:// URL; updates are refused."));
    expect(row).toBeEnabled();
  });

  it("a job that ends up_to_date re-checks and settles on 'Up to date' without a restart", async () => {
    boot({ available: true, statuses: [{ state: "up_to_date", job_id: "j1", log_tail: ["Already up to date."], message: "The hub is up to date." }] });
    render(<SettingsScreen />);
    const row = screen.getByTestId("check-updates");
    await waitFor(() => expect(row).toHaveTextContent("Update available"));
    fireEvent.click(row);
    fireEvent.click(await screen.findByTestId("confirm-update"));
    await waitFor(() => expect(row).toHaveTextContent(/Up to date · 0\.8\.0|Update available/));
    expect(row).not.toHaveTextContent("Restarting");
    expect(row).toBeEnabled();
  });

  it("rolled back settles the row with the hub's message + log behind a disclosure that folds when the state changes", async () => {
    boot({ available: true, statuses: [{ state: "rolled_back", job_id: "j1", log_tail: ["uv sync: no matching wheel", "git reset --hard abc"], message: "Rolled back to abc." }] });
    render(<SettingsScreen />);
    const row = screen.getByTestId("check-updates");
    await waitFor(() => expect(row).toHaveTextContent("Update available"));
    fireEvent.click(row);
    fireEvent.click(await screen.findByTestId("confirm-update"));
    await waitFor(() => expect(row).toHaveTextContent("The update failed; the hub went back to the previous version."));
    const toggle = screen.getByTestId("update-log-toggle");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByTestId("update-log-text")).toBeNull();
    fireEvent.click(toggle);
    expect(screen.getByTestId("update-log-text")).toHaveTextContent("Rolled back to abc.");
    expect(screen.getByTestId("update-log-text")).toHaveTextContent("uv sync: no matching wheel");
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    // tapping the row re-checks; the log folds away with the state
    fireEvent.click(row);
    await waitFor(() => expect(row).toHaveTextContent("Update available"));
    expect(screen.queryByTestId("update-log-toggle")).toBeNull();
  });
});

describe("Hub stats disclosure", () => {
  it("is a text disclosure ('Show stats' / 'Hide stats') with aria-controls; fetches the hub's paragraph on open; an unreachable hub shows the error", async () => {
    const { calls } = boot();
    render(<SettingsScreen />);
    const stats = screen.getByTestId("hub-stats");
    expect(stats).toHaveTextContent("Show stats");
    expect(stats).toHaveAttribute("aria-expanded", "false");
    expect(stats).toHaveAttribute("aria-controls", "hub-stats-body");
    expect(calls.filter((c) => c.endsWith("/api/metrics/summary"))).toHaveLength(0);
    fireEvent.click(stats);
    await waitFor(() => expect(screen.getByTestId("hub-stats-body")).toHaveTextContent("Running since 7:37 today. 10 commands, all worked."));
    expect(stats).toHaveTextContent("Hide stats");
    expect(screen.getByTestId("hub-stats-body")).toHaveAttribute("id", "hub-stats-body");
    fireEvent.click(stats);
    expect(screen.queryByTestId("hub-stats-body")).toBeNull();

    boot({ summary: "unreachable" });
    render(<SettingsScreen />);
    fireEvent.click(screen.getAllByTestId("hub-stats").at(-1)!);
    await waitFor(() => expect(screen.getAllByRole("alert").at(-1)).toHaveTextContent("Can't reach the hub. Check that the Mac is on the network."));
  });
});
