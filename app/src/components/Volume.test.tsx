/**
 * Volume ownership (PRD §3.3 [1.2], docs/api.md Concepts): amplifier zones as volume targets, fixed-
 * output zones as power-only rows, no duplicate row for a zone that owns a player, quantised levels
 * rendered as the hub reports them, and the `unsupported_action` backstop.
 */
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { axe } from "vitest-axe";
import { VolumeSheet, playerlessZones } from "./VolumeSheet";
import { useHub } from "@/lib/hub/store";
import { useToasts } from "@/lib/ui/toasts";
import { jsonResponse, okAck, sampleState } from "@/test/fixtures";
import type { HubState, Zone } from "@/lib/hub/types";

vi.mock("framer-motion", async () => {
  const actual = await vi.importActual<typeof import("framer-motion")>("framer-motion");
  return { ...actual, AnimatePresence: ({ children }: { children: React.ReactNode }) => <>{children}</> };
});

const zone = (id: string, name: string, over: Partial<Zone> = {}): Zone => ({
  id,
  key: id.split(":")[1] ?? id,
  name,
  power: true,
  online: true,
  host: "10.0.0.9",
  device_id: "d1",
  player_ids: [],
  volume: 35,
  muted: false,
  supports_volume: true,
  supports_mute: true,
  ...over,
});

/** AVR main zone owns the HEOS player; Zone 2 drives an external amp with its own volume; Zone 3 is a fixed pre-out. */
function stateWithZones(): HubState {
  const s = sampleState();
  s.zones = {
    "denon-10.0.0.9:main": zone("denon-10.0.0.9:main", "Main zone", { player_ids: ["heos-1"], volume: 40 }),
    "denon-10.0.0.9:zone2": zone("denon-10.0.0.9:zone2", "Zone 2", { volume: 22 }),
    "denon-10.0.0.9:zone3": zone("denon-10.0.0.9:zone3", "Zone 3", { supports_volume: false, supports_mute: false, power: false, volume: 0 }),
  };
  return s;
}

const fetchMock = vi.fn();
const fetcher = fetchMock as unknown as typeof fetch;
const calls = () => fetchMock.mock.calls.map((c) => [String(c[0]).replace(/^https?:\/\/[^/]+/, ""), c[1]?.body ? JSON.parse(c[1].body as string) : undefined]);
const okFor = (init?: RequestInit) => jsonResponse(okAck((init?.headers as Record<string, string>)["x-correlation-id"]));

function boot(state = stateWithZones()) {
  useHub.getState()._reset();
  useToasts.setState({ toasts: [] });
  fetchMock.mockReset();
  fetchMock.mockImplementation(async (_u: RequestInfo | URL, init?: RequestInit) => okFor(init));
  useHub.setState({ _deps: { fetcher, now: () => Date.now(), setTimer: (fn, ms) => setTimeout(fn, ms), clearTimer: (t) => clearTimeout(t as ReturnType<typeof setTimeout>) } });
  useHub.getState().onMessage({ type: "snapshot", version: 10, state });
  useHub.getState().setPhase("open", Date.now());
}

describe("playerlessZones", () => {
  it("keeps zones that own no player, drops the ones represented by a player row", () => {
    const zs = Object.values(stateWithZones().zones);
    expect(playerlessZones(zs).map((z) => z.name)).toEqual(["Zone 2", "Zone 3"]);
    expect(playerlessZones([])).toEqual([]);
  });
});

describe("VolumeSheet with amplifier zones", () => {
  it("one row per player, a slider + mute row for the volume-capable playerless zone, a power-only row for the fixed-output zone, and no duplicate for the main zone; passes axe", async () => {
    boot();
    const { container } = render(<VolumeSheet open onClose={() => {}} />);
    const sheet = screen.getByTestId("volume-sheet");
    // players: heos-1, sonos-K, sonos-P (the main zone is represented by heos-1's mirrored volume)
    expect(within(sheet).getAllByTestId(/^volume-row-/)).toHaveLength(3);
    expect(within(sheet).queryByTestId("volume-zone-denon-10.0.0.9:main")).toBeNull();
    expect(within(sheet).queryByText("Main zone")).toBeNull();
    // Zone 2: slider targets the zone, mute available
    const z2 = within(sheet).getByTestId("volume-zone-denon-10.0.0.9:zone2");
    expect(z2).toHaveTextContent("Zone 2");
    expect(within(z2).getByRole("slider", { name: "Zone 2 volume" })).toHaveAttribute("aria-valuenow", "22");
    expect(within(z2).getByRole("button", { name: "Mute Zone 2" })).toBeEnabled();
    // Zone 3: fixed output → power state only, no slider, no mute
    const z3 = within(sheet).getByTestId("volume-zone-denon-10.0.0.9:zone3");
    expect(z3).toHaveAttribute("data-fixed-output", "true");
    expect(within(z3).queryByRole("slider")).toBeNull();
    expect(within(z3).queryByRole("button")).toBeNull();
    expect(within(z3).getByTestId("zone-power-state-denon-10.0.0.9:zone3")).toHaveTextContent("Off · fixed output");
    expect(await axe(container)).toHaveNoViolations();
  });

  it("the zone slider posts /api/volume with the zone id and mirrors optimistically onto the zone; mute posts /api/mute", async () => {
    boot();
    render(<VolumeSheet open onClose={() => {}} />);
    const z2 = screen.getByTestId("volume-zone-denon-10.0.0.9:zone2");
    const slider = within(z2).getByRole("slider", { name: "Zone 2 volume" });
    slider.focus();
    fireEvent.keyDown(slider, { key: "ArrowRight" });
    await waitFor(() => expect(calls().some(([p]) => p === "/api/volume")).toBe(true));
    const vol = calls().find(([p]) => p === "/api/volume")!;
    expect(vol[1]).toMatchObject({ target: "denon-10.0.0.9:zone2" });
    expect(useHub.getState().state?.zones["denon-10.0.0.9:zone2"]?.volume).toBe(vol[1].level);
    fireEvent.click(within(z2).getByRole("button", { name: "Mute Zone 2" }));
    await waitFor(() => expect(calls().some(([p]) => p === "/api/mute")).toBe(true));
    expect(calls().find(([p]) => p === "/api/mute")![1]).toEqual({ target: "denon-10.0.0.9:zone2", muted: true });
    expect(useHub.getState().state?.zones["denon-10.0.0.9:zone2"]?.muted).toBe(true);
  });

  it("a zone that owns a player mirrors its level onto that player when set by zone id", async () => {
    boot();
    await useHub.getState().setZoneVolume("denon-10.0.0.9:main", 55);
    expect(useHub.getState().state?.zones["denon-10.0.0.9:main"]?.volume).toBe(55);
    expect(useHub.getState().state?.players["heos-1"]?.volume).toBe(55);
    await useHub.getState().setMute("denon-10.0.0.9:main", true);
    expect(useHub.getState().state?.players["heos-1"]?.muted).toBe(true);
  });
});

describe("quantised levels", () => {
  it("send 96, the hub settles at 97: the slider shows 97, no toast, no resync", async () => {
    boot();
    const resync = vi.fn();
    useHub.getState().setResync(resync);
    render(<VolumeSheet open onClose={() => {}} />);
    await act(async () => {
      await useHub.getState().setVolume("heos-1", 96);
    });
    // The hub's mirrored level arrives on the socket a beat later.
    act(() =>
      useHub.getState().onMessage({
        type: "delta",
        from_version: 10,
        to_version: 11,
        changed: { "players.heos-1": { ...useHub.getState().state!.players["heos-1"]!, volume: 97 } },
      }),
    );
    expect(useHub.getState().state?.players["heos-1"]?.volume).toBe(97);
    expect(screen.getByRole("slider", { name: "Living Room Amp volume" })).toHaveAttribute("aria-valuenow", "97");
    expect(useToasts.getState().toasts).toHaveLength(0);
    expect(resync).not.toHaveBeenCalled();
  });
});

describe("unsupported_action backstop", () => {
  it("toasts the hub's sentence verbatim and drops the optimistic layer", async () => {
    boot();
    const resync = vi.fn();
    useHub.getState().setResync(resync);
    fetchMock.mockImplementation(async (_u: RequestInfo | URL, init?: RequestInit) =>
      jsonResponse({ ...okAck((init?.headers as Record<string, string>)["x-correlation-id"]), ok: false, error: { code: "unsupported_action", message: "Zone 3 is a fixed output; the receiver can't change its volume.", target: "denon-10.0.0.9:zone3", correlation_id: "x" } }, 409),
    );
    await useHub.getState().setZoneVolume("denon-10.0.0.9:zone3", 40);
    expect(useToasts.getState().toasts.map((t) => t.message)).toEqual(["Zone 3 is a fixed output; the receiver can't change its volume."]);
    expect(resync).toHaveBeenCalledTimes(1);
  });
});
