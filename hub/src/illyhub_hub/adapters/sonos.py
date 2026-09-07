"""Sonos adapter over SoCo: discovery plus UPnP event subscriptions with renewal.

SoCo attribute access (``uid``, ``player_name``, ``is_visible``, group labels, volume) performs
synchronous SOAP calls. Everything the adapter needs is therefore captured into plain
:class:`ZoneSnapshot` / :class:`GroupSnapshot` records inside ``asyncio.to_thread`` by
:func:`snapshot_zones`; the adapter itself only ever reads snapshot fields.

Verified only against fakes of the SoCo objects; no physical Sonos player has been exercised.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..config import Settings
from ..state import (
    Art,
    Capabilities,
    GroupTopology,
    NowPlaying,
    Player,
    StateStore,
    side_id_for,
)
from ..tasks import stop_task
from .base import Backoff, SonosAdapter

SERVICES = ("avTransport", "renderingControl")
SUBSCRIPTION_TIMEOUT_S = 600
# Sonos music-service ids seen in stream URIs (``sid=``). Best-effort; unknown ids stay None.
SERVICE_IDS = {"174": "tidal", "236": "pandora", "284": "ytmusic"}


@dataclass(frozen=True)
class ZoneSnapshot:
    uid: str
    name: str
    ip: str
    model: str | None = None
    visible: bool = True
    volume: int = 0
    muted: bool = False
    transport_state: str | None = None


@dataclass(frozen=True)
class GroupSnapshot:
    uid: str
    coordinator_uid: str
    member_uids: tuple[str, ...]
    label: str | None = None


@dataclass
class Topology:
    zones: list[ZoneSnapshot] = field(default_factory=list)
    groups: list[GroupSnapshot] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)  # uid -> SoCo object, for subscribing


class SonosSubscription(Protocol):
    callback: Callable[[Any], None] | None
    auto_renew_fail: Callable[[Exception], None] | None

    async def unsubscribe(self, strict: bool = True) -> None: ...


SnapshotFn = Callable[[], Awaitable[Topology]]
GroupsFn = Callable[[Any], Awaitable[list[GroupSnapshot]]]
SubscribeFn = Callable[[Any, str, Callable[[Any], None]], Awaitable[SonosSubscription]]


def snapshot_zones(zones: Iterable[Any], *, include_state: bool = True) -> Topology:
    """Read every lazy SoCo property once. Call this from a worker thread, never the loop."""
    topo = Topology()
    seen_groups: set[str] = set()
    for z in zones:
        visible = bool(getattr(z, "is_visible", True))
        if not visible:
            continue
        snap = ZoneSnapshot(
            uid=z.uid,
            name=z.player_name,
            ip=z.ip_address,
            model=getattr(z, "model_name", None),
            visible=True,
            volume=int(z.volume) if include_state else 0,
            muted=bool(z.mute) if include_state else False,
            transport_state=(
                z.get_current_transport_info().get("current_transport_state")
                if include_state
                else None
            ),
        )
        topo.zones.append(snap)
        topo.raw[snap.uid] = z
        group = getattr(z, "group", None)
        if group is not None and group.uid not in seen_groups:
            seen_groups.add(group.uid)
            topo.groups.append(snapshot_group(group))
    return topo


def snapshot_group(group: Any) -> GroupSnapshot:
    members = tuple(m.uid for m in group.members if getattr(m, "is_visible", True))
    return GroupSnapshot(
        uid=group.uid,
        coordinator_uid=group.coordinator.uid,
        member_uids=members,
        label=getattr(group, "label", None),
    )


async def default_snapshot(settings: Settings) -> Topology:  # pragma: no cover
    import soco

    def work() -> Topology:
        zones = soco.discover(
            timeout=settings.ssdp_timeout_s, interface_addr=settings.sonos_listener_host
        )
        return snapshot_zones(zones or [])

    return await asyncio.to_thread(work)


async def default_groups(zone: Any) -> list[GroupSnapshot]:  # pragma: no cover
    return await asyncio.to_thread(lambda: [snapshot_group(g) for g in zone.all_groups])


async def default_subscribe(  # pragma: no cover
    zone: Any, service: str, callback: Callable[[Any], None]
) -> SonosSubscription:
    import soco.config
    import soco.events_asyncio

    soco.config.EVENTS_MODULE = soco.events_asyncio
    sub = soco.events_asyncio.Subscription(getattr(zone, service))
    sub.callback = callback  # set before subscribing so the initial event is not lost
    await sub.subscribe(requested_timeout=SUBSCRIPTION_TIMEOUT_S, auto_renew=True)
    return sub


def player_id(uid: str) -> str:
    return f"sonos-{uid}"


def group_id(uid: str) -> str:
    return f"sonos-g{uid}"


def _play_state(transport_state: str | None) -> str:
    return {"PLAYING": "play", "PAUSED_PLAYBACK": "pause", "STOPPED": "stop"}.get(
        transport_state or "", "unknown"
    )


def absolutize(uri: str | None, ip: str | None) -> str | None:
    if not uri:
        return None
    if uri.startswith(("http://", "https://")) or not ip:
        return uri
    return f"http://{ip}:1400{uri if uri.startswith('/') else '/' + uri}"


def source_from_uri(uri: str | None) -> str | None:
    if not uri or "sid=" not in uri:
        return None
    sid = uri.split("sid=", 1)[1].split("&", 1)[0]
    return SERVICE_IDS.get(sid)


class SoCoAdapter(SonosAdapter):
    def __init__(
        self,
        store: StateStore,
        settings: Settings,
        *,
        snapshot: SnapshotFn | None = None,
        groups: GroupsFn = default_groups,
        subscribe: SubscribeFn = default_subscribe,
    ) -> None:
        super().__init__(store)
        self.settings = settings
        self._snapshot = snapshot or (lambda: default_snapshot(settings))
        self._groups = groups
        self._subscribe = subscribe
        self._topology = Topology()
        self._subs: list[SonosSubscription] = []
        self._task: asyncio.Task[None] | None = None
        self._backoff = Backoff(cap=settings.reconnect_max_s)
        self._closing = False
        self._lost = asyncio.Event()

    async def connect(self) -> None:
        if self._task is not None:
            return
        self._closing = False
        self._task = asyncio.create_task(self._run(), name="sonos-connection")

    async def disconnect(self) -> None:
        self._closing = True
        await stop_task(self._task)
        self._task = None
        await self._cancel_tasks()
        await self._teardown()
        self._set_status("disconnected")

    async def _teardown(self) -> None:
        subs, self._subs = self._subs, []
        for sub in subs:
            try:
                await sub.unsubscribe(strict=False)
            except Exception:  # noqa: BLE001
                self.log.debug("unsubscribe raised", exc_info=True)

    async def _run(self) -> None:
        while not self._closing:
            try:
                self._lost = asyncio.Event()
                await self._connect_once()
                await self._lost.wait()
                if self._closing:
                    return
                if self._held_at_least(self.settings.backoff_reset_after_s):
                    self._backoff.reset()
                self.store.mark_vendor_offline("sonos")
                self._set_status("reconnecting", "subscription lost")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._set_status("reconnecting", str(exc))
                self.log.warning("sonos connect failed", extra={"extra": {"error": str(exc)}})
            await self._teardown()
            await asyncio.sleep(self._backoff.next())

    async def _connect_once(self) -> None:
        topo = await self._snapshot()
        if not topo.zones:
            raise ConnectionError("no Sonos players discovered")
        self._topology = topo
        for z in topo.zones:
            self.store.upsert_player(self._to_player(z))
        self.store.set_groups("sonos", self._to_groups(topo.groups))
        for z in topo.zones:
            for service in SERVICES:
                await self._add_subscription(z, service)
        await self._add_subscription(topo.zones[0], "zoneGroupTopology")
        self._mark_connected()
        self._set_status("connected")
        self._emit("connected", players=len(topo.zones))

    async def _add_subscription(self, zone: ZoneSnapshot, service: str) -> None:
        raw = self._topology.raw.get(zone.uid)
        sub = await self._subscribe(
            raw, service, lambda event, _z=zone, _s=service: self._on_event(_z, _s, event)
        )
        sub.auto_renew_fail = lambda exc: self._on_renew_fail(service, exc)
        self._subs.append(sub)

    def _on_renew_fail(self, service: str, exc: Exception) -> None:
        self.log.warning(
            "subscription renewal failed", extra={"extra": {"service": service, "error": str(exc)}}
        )
        self._lost.set()

    # -- topology -------------------------------------------------------------------

    def _to_player(self, z: ZoneSnapshot) -> Player:
        existing = self.store.state.players.get(player_id(z.uid))
        return Player(
            id=player_id(z.uid),
            name=z.name,
            vendor="sonos",
            ip=z.ip,
            model=z.model,
            online=True,
            volume=z.volume,
            muted=z.muted,
            play_state=_play_state(z.transport_state) if z.transport_state else "stop",  # type: ignore[arg-type]
            group_id=existing.group_id if existing else None,
            capabilities=Capabilities(supports_power=False),
        )

    @staticmethod
    def _to_groups(groups: Iterable[GroupSnapshot]) -> list[GroupTopology]:
        topo: list[GroupTopology] = []
        for g in groups:
            if len(g.member_uids) < 2:
                continue  # solo players are their own side; only real groups count
            topo.append(
                GroupTopology(
                    id=group_id(g.uid),
                    vendor="sonos",
                    coordinator_player_id=player_id(g.coordinator_uid),
                    member_ids=[player_id(u) for u in g.member_uids],
                    name=g.label,
                )
            )
        return topo

    # -- events ---------------------------------------------------------------------

    def _on_event(self, zone: ZoneSnapshot, service: str, event: Any) -> None:
        try:
            self._handle_event(zone, service, event)
        except Exception:  # noqa: BLE001 - a bad event must not kill the SoCo listener
            self.log.exception("sonos event failed", extra={"extra": {"service": service}})

    def _handle_event(self, zone: ZoneSnapshot, service: str, event: Any) -> None:
        variables: dict[str, Any] = getattr(event, "variables", {}) or {}
        p_id = player_id(zone.uid)
        if service == "renderingControl":
            self._apply_rendering(p_id, variables)
        elif service == "avTransport":
            self._apply_transport(p_id, zone, variables)
        elif service == "zoneGroupTopology":
            self._spawn(self._refresh_groups(zone))

    def _apply_rendering(self, p_id: str, variables: dict[str, Any]) -> None:
        fields: dict[str, Any] = {}
        vol = variables.get("volume")
        if isinstance(vol, dict) and "Master" in vol:
            fields["volume"] = int(vol["Master"])
        mute = variables.get("mute")
        if isinstance(mute, dict) and "Master" in mute:
            fields["muted"] = str(mute["Master"]) in ("1", "True", "true")
        if fields:
            self.store.update_player(p_id, **fields)
            self._emit("volume", player_id=p_id)

    def _apply_transport(self, p_id: str, zone: ZoneSnapshot, variables: dict[str, Any]) -> None:
        state = variables.get("transport_state")
        if state:
            self.store.update_player(p_id, play_state=_play_state(state))
            self._emit("play_state", player_id=p_id)
        meta = variables.get("current_track_meta_data")
        if meta:
            player = self.store.state.players.get(p_id)
            if not player:
                return
            self.store.set_now_playing(
                side_id_for(player), self._now_playing(meta, variables, zone)
            )
            self._emit("now_playing", player_id=p_id)

    @staticmethod
    def _now_playing(meta: Any, variables: dict[str, Any], zone: ZoneSnapshot) -> NowPlaying:
        get = meta.get if isinstance(meta, dict) else lambda k, d=None: getattr(meta, k, d)
        art = get("album_art_uri") or get("album_art")
        uri = variables.get("current_track_uri") or get("uri")
        duration = variables.get("current_track_duration")
        return NowPlaying(
            title=get("title"),
            artist=get("creator"),
            album=get("album"),
            art=Art(url=absolutize(art, zone.ip)),
            source=source_from_uri(uri),
            seekable="audioBroadcast" not in (get("item_class") or ""),
            duration_ms=_hms_to_ms(duration) if duration else None,
            track_id=uri or None,
        )

    async def _refresh_groups(self, zone: ZoneSnapshot) -> None:
        raw = self._topology.raw.get(zone.uid)
        try:
            groups = await self._groups(raw)
            self._topology.groups = groups
            self.store.set_groups("sonos", self._to_groups(groups))
            self._emit("topology")
        except Exception as exc:  # noqa: BLE001
            self.log.warning("group refresh failed", extra={"extra": {"error": str(exc)}})


def _hms_to_ms(value: str) -> int | None:
    """``H:MM:SS`` (Sonos) to milliseconds. Returns None for ``NOT_IMPLEMENTED`` etc."""
    parts = value.split(":")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return None
    h, m, s = (int(p) for p in parts)
    return ((h * 60 + m) * 60 + s) * 1000
