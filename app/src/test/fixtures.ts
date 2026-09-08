import type { HubState, Player, Side } from "@/lib/hub/types";

const caps = (over: Partial<Player["capabilities"]> = {}) => ({
  supports_seek: true,
  supports_power: false,
  can_group: true,
  supports_next: true,
  supports_prev: true,
  ...over,
});

export function player(id: string, name: string, vendor: "heos" | "sonos", over: Partial<Player> = {}): Player {
  return {
    id,
    name,
    vendor,
    ip: "10.0.0.9",
    model: vendor === "heos" ? "AVR" : "One",
    online: true,
    volume: 30,
    muted: false,
    play_state: "stop",
    group_id: null,
    capabilities: caps(vendor === "heos" ? { supports_seek: false, supports_power: true } : {}),
    ...over,
  } as Player;
}

export function side(id: string, name: string, vendor: "heos" | "sonos", members: string[], over: Partial<Side> = {}): Side {
  return {
    id,
    vendor,
    coordinator_player_id: members[0]!,
    member_ids: members,
    name,
    play_state: "stop",
    volume: 30,
    muted: false,
    capabilities: caps(vendor === "heos" ? { supports_seek: false, supports_power: true } : {}),
    ...over,
  } as Side;
}

export function sampleState(nowMs = Date.now()): HubState {
  const reported = new Date(nowMs).toISOString();
  return {
    version: 10,
    players: {
      "heos-1": player("heos-1", "Living Room Amp", "heos", { play_state: "play", volume: 40 }),
      "sonos-K": player("sonos-K", "Kitchen", "sonos", { group_id: "sonos-gK", volume: 20 }),
      "sonos-P": player("sonos-P", "Patio", "sonos", { group_id: "sonos-gK", volume: 60, online: false }),
    },
    zones: {
      "denon-10.0.0.9:main": { id: "denon-10.0.0.9:main", key: "main", name: "Main zone", power: true, online: true, host: "10.0.0.9", device_id: "d1", player_ids: ["heos-1"] },
      "denon-10.0.0.9:zone2": { id: "denon-10.0.0.9:zone2", key: "zone2", name: "Zone 2", power: false, online: true, host: "10.0.0.9", device_id: "d1", player_ids: [] },
    },
    groups: {
      "sonos-gK": { id: "sonos-gK", vendor: "sonos", coordinator_player_id: "sonos-K", member_ids: ["sonos-K", "sonos-P"], name: "Kitchen + 1" },
    },
    sides: {
      "heos:heos-1": side("heos:heos-1", "Living Room Amp", "heos", ["heos-1"], { play_state: "play", volume: 40 }),
      "sonos:sonos-gK": side("sonos:sonos-gK", "Kitchen + 1", "sonos", ["sonos-K", "sonos-P"], { volume: 40 }),
    },
    now_playing: {
      "heos:heos-1": {
        title: "Blue in Green",
        artist: "Miles Davis",
        album: "Kind of Blue",
        art: { url: "/api/art/abc", accent: "#3a5a7a", accent_is_safe: true },
        source: "tidal",
        seekable: true,
        supports_next: true,
        supports_prev: true,
        duration_ms: 337_000,
        track_id: "tidal:1",
      },
      "sonos:sonos-gK": {
        title: "Chill Station",
        artist: "Pandora",
        album: null,
        art: { url: null, accent: null, accent_is_safe: false },
        source: "pandora",
        seekable: false,
        supports_next: true,
        supports_prev: false,
        duration_ms: null,
        track_id: null,
      },
    },
    positions: {
      "heos:heos-1": { position_ms: 60_000, reported_at: reported, confidence: 0.9 },
      "sonos:sonos-gK": { position_ms: 0, reported_at: reported, confidence: 1 },
    },
    sync: { status: "idle", side_ids: [], drift_ms: null, last_correction_at: null },
    connections: {
      heos: { state: "connected", last_error: null, since: reported },
      sonos: { state: "connected", last_error: null, since: reported },
      denon: { state: "connected", last_error: null, since: reported },
    },
  };
}

export function okAck(cid: string | undefined, over: Record<string, unknown> = {}) {
  return { correlation_id: cid ?? "cid", ok: true, action: "x", target: null, state_version: 11, error: null, partial: [], ...over };
}

export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}
