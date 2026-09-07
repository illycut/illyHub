from __future__ import annotations

import asyncio

from illyhub_hub.state import (
    Capabilities,
    GroupTopology,
    HubState,
    NowPlaying,
    Player,
    Position,
    StateStore,
    SyncState,
    Zone,
    compute_sides,
    diff,
    side_id_for,
    side_id_for_group,
)


def p(pid: str, vendor: str = "sonos", group: str | None = None, **kw) -> Player:
    return Player(id=pid, name=pid.title(), vendor=vendor, group_id=group, **kw)  # type: ignore[arg-type]


def grp(gid: str, coord: str, members: list[str], vendor: str = "sonos") -> GroupTopology:
    return GroupTopology(id=gid, vendor=vendor, coordinator_player_id=coord, member_ids=members)  # type: ignore[arg-type]


def test_solo_players_are_their_own_sides() -> None:
    sides = compute_sides({"a": p("a", "heos"), "b": p("b")}, {})
    assert set(sides) == {"heos:a", "sonos:b"}
    assert sides["sonos:b"].coordinator_player_id == "b" and sides["sonos:b"].name == "B"


def test_group_becomes_one_side_with_coordinator_and_derived_state() -> None:
    players = {
        "k": p("k", volume=40, play_state="play"),
        "pt": p("pt", volume=10),
        "solo": p("solo"),
    }
    sides = compute_sides(players, {"g1": grp("g1", "k", ["k", "pt"])})
    assert set(sides) == {"sonos:g1", "sonos:solo"}
    side = sides["sonos:g1"]
    assert side.member_ids == ["k", "pt"] and side.name == "K + Pt"
    assert side.play_state == "play"  # from the coordinator
    assert side.volume == 25 and side.muted is False  # group volume is the member average
    players["k"] = players["k"].model_copy(update={"muted": True})
    players["pt"] = players["pt"].model_copy(update={"muted": True})
    sides = compute_sides(players, {"g1": grp("g1", "k", ["k", "pt"])})
    assert sides["sonos:g1"].muted is True and sides["sonos:solo"].volume == 0


def test_capabilities_flow_from_players_zones_and_now_playing() -> None:
    players = {"h": p("h", "heos", capabilities=Capabilities(supports_power=True)), "s": p("s")}
    zones = {"z": Zone(id="z", key="main", name="Main", player_ids=["s"])}
    np = {"heos:h": NowPlaying(seekable=False)}
    sides = compute_sides(players, {}, np, zones)
    assert sides["heos:h"].capabilities.supports_power is True
    assert sides["heos:h"].capabilities.supports_seek is False  # radio now playing
    assert sides["sonos:s"].capabilities.supports_power is True  # via zone player_ids
    assert sides["sonos:s"].capabilities.supports_seek is True


def test_group_with_unknown_coordinator_is_ignored() -> None:
    sides = compute_sides({"a": p("a")}, {"g": grp("g", "ghost", ["a"])})
    assert set(sides) == {"sonos:a"}


def test_side_id_helpers() -> None:
    assert side_id_for(p("a", "heos")) == "heos:a"
    assert side_id_for(p("a", "heos", group="g9")) == "heos:g9"
    assert side_id_for_group("sonos", "g1") == "sonos:g1"


def test_diff_reports_depth_two_paths_ignores_version_and_accepts_dicts() -> None:
    old = HubState(players={"a": p("a")})
    new = old.model_copy(deep=True)
    new.version = 99
    assert diff(old, new) == []
    new.players["a"].volume = 40
    new.zones["main"] = Zone(id="main", key="main", name="Main")
    new.sync = SyncState(status="locked")
    assert diff(old, new) == ["players.a", "sync", "zones.main"]
    assert diff(old.model_dump(mode="json"), new.model_dump(mode="json")) == diff(old, new)


def test_store_bumps_version_only_on_change_and_caches_snapshot(store: StateStore) -> None:
    assert store.upsert_player(p("a")) == ["players.a", "sides.sonos:a"]
    assert store.state.version == 1 and store.snapshot()["version"] == 1
    snap = store.snapshot()
    assert store.upsert_player(p("a")) == []
    assert store.snapshot() is snap  # unchanged commit reuses the cached dump
    assert snap["players"]["a"]["name"] == "A"


def test_update_remove_player_and_prune_side_entries(store: StateStore) -> None:
    store.upsert_player(p("a"))
    store.set_now_playing("sonos:a", NowPlaying(title="T"))
    store.set_position("sonos:a", Position(position_ms=5))
    store.update_player("a", volume=55, muted=True)
    assert store.state.players["a"].volume == 55 and store.state.players["a"].muted is True
    assert store.update_player("missing", volume=1) == []
    store.remove_player("a")
    s = store.state
    assert s.players == {} and s.sides == {} and s.now_playing == {} and s.positions == {}


def test_now_playing_for_unknown_side_is_pruned(store: StateStore) -> None:
    store.upsert_player(p("a"))
    store.set_now_playing("sonos:ghost", NowPlaying(title="x"))
    assert "sonos:ghost" not in store.state.now_playing


def test_set_groups_updates_player_group_ids_sides_and_prunes_old_side_data(
    store: StateStore,
) -> None:
    for pid in ("k", "pt", "solo"):
        store.upsert_player(p(pid))
    store.set_now_playing("sonos:k", NowPlaying(title="solo track"))
    store.set_groups("sonos", [grp("g1", "k", ["k", "pt"])])
    assert (
        store.state.players["k"].group_id == "g1" and store.state.players["solo"].group_id is None
    )
    assert set(store.state.sides) == {"sonos:g1", "sonos:solo"}
    assert "sonos:k" not in store.state.now_playing  # solo side vanished, entry pruned
    store.set_groups("sonos", [])
    assert store.state.players["k"].group_id is None
    assert set(store.state.sides) == {"sonos:k", "sonos:pt", "sonos:solo"}


def test_remove_player_dissolves_group_when_it_shrinks_or_loses_coordinator(
    store: StateStore,
) -> None:
    for pid in ("k", "pt", "x"):
        store.upsert_player(p(pid))
    store.set_groups("sonos", [grp("g1", "k", ["k", "pt", "x"])])
    store.remove_player("x")
    assert store.state.groups["g1"].member_ids == ["k", "pt"]
    store.remove_player("k")  # coordinator gone → group dissolved, pt is solo again
    assert "g1" not in store.state.groups and store.state.players["pt"].group_id is None
    assert set(store.state.sides) == {"sonos:pt"}


def test_set_groups_only_touches_its_vendor(store: StateStore) -> None:
    store.upsert_player(p("h", "heos"))
    store.set_groups("heos", [grp("hg", "h", ["h"], vendor="heos")])
    store.set_groups("sonos", [])
    assert "hg" in store.state.groups


def test_zone_sync_connection_helpers(store: StateStore) -> None:
    store.set_sync(SyncState(status="drifting", drift_ms=350))
    store.set_zone(Zone(id="denon-h:main", key="main", name="Main", power=True, host="h"))
    store.set_connection("heos", "reconnecting", "boom")
    s = store.state
    assert s.sync.drift_ms == 350 and s.zones["denon-h:main"].power is True
    assert s.connections["heos"].last_error == "boom"
    store.remove_zone("denon-h:main")
    assert s is not store.state and store.state.zones == {}


def test_connection_since_is_preserved_while_state_unchanged(store: StateStore) -> None:
    store.set_connection("heos", "connected")
    first = store.state.connections["heos"].since
    store.set_connection("heos", "connected")
    assert store.state.connections["heos"].since == first
    store.set_connection("heos", "disconnected")
    assert store.state.connections["heos"].since >= first


def test_mark_vendor_offline(store: StateStore) -> None:
    store.upsert_player(p("a"))
    store.upsert_player(p("h", "heos"))
    store.mark_vendor_offline("sonos")
    assert store.state.players["a"].online is False and store.state.players["h"].online is True


def test_subscribe_returns_snapshot_atomically_and_unsubscribes(store: StateStore) -> None:
    store.upsert_player(p("a"))
    seen: list[list[str]] = []
    snap, unsub = store.subscribe(lambda _s, changed: seen.append(changed))
    assert snap["version"] == 1 and snap is store.snapshot()
    store.update_player("a", volume=9)
    assert seen == [["players.a", "sides.sonos:a"]]
    unsub()
    unsub()
    store.update_player("a", volume=10)
    assert len(seen) == 1


def test_reentrant_mutation_from_listener_is_queued_in_order(store: StateStore) -> None:
    order: list[tuple[int, list[str]]] = []

    def listener(state: HubState, changed: list[str]) -> None:
        order.append((state.version, changed))
        if "players.a" in changed and state.players["a"].volume == 1:
            store.update_player("a", volume=2)  # re-entrant mutation

    store.subscribe(listener)
    store.upsert_player(p("a", volume=1))
    assert [v for v, _ in order] == [1, 2]
    assert store.state.players["a"].volume == 2


def test_bad_listener_does_not_break_store(store: StateStore) -> None:
    def bad(_s: HubState, _c: list[str]) -> None:
        raise RuntimeError("boom")

    good: list[int] = []
    store.subscribe(bad)
    store.subscribe(lambda s, _c: good.append(s.version))
    store.upsert_player(p("a"))
    assert good == [1]


async def test_async_listener_runs_and_aclose_cancels(store: StateStore) -> None:
    hit = asyncio.Event()
    started = asyncio.Event()

    async def async_listener(_state: HubState, _changed: list[str]) -> None:
        hit.set()

    async def slow_listener(_state: HubState, _changed: list[str]) -> None:
        started.set()
        await asyncio.sleep(10)

    store.subscribe(async_listener)
    store.subscribe(slow_listener)
    store.upsert_player(p("a"))
    await asyncio.wait_for(hit.wait(), 1)
    await asyncio.wait_for(started.wait(), 1)
    assert len(store._tasks) >= 1
    await store.aclose()
    assert store._tasks == set()


def test_async_listener_without_running_loop_is_dropped(store: StateStore) -> None:
    async def async_listener(_state: HubState, _changed: list[str]) -> None:
        pass  # pragma: no cover - never scheduled

    store.subscribe(async_listener)
    store.upsert_player(p("a"))  # no loop → coroutine closed, no warning raised
    assert store._tasks == set()
