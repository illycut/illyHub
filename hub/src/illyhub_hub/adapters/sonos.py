"""Sonos adapter over SoCo: discovery plus UPnP event subscriptions with renewal.

SoCo attribute access (``uid``, ``player_name``, ``is_visible``, group labels, volume) performs
synchronous SOAP calls. Everything the adapter needs is therefore captured into plain
:class:`ZoneSnapshot` / :class:`GroupSnapshot` records inside ``asyncio.to_thread`` by
:func:`snapshot_zones`; the adapter itself only ever reads snapshot fields.

Commands run the SoCo method on the coordinator (transport, seek) or the member (volume, mute)
inside ``asyncio.to_thread``. Position is polled from ``get_current_track_info`` while a side's
coordinator is playing, at instants chosen by the :class:`~illyhub_hub.positions.PositionTracker`
so the whole-second reports converge to a sub-second estimate.

Verified only against fakes of the SoCo objects; no physical Sonos player has been exercised.
"""

from __future__ import annotations

import asyncio
import re
import threading
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import quote, unquote

from ..art import ArtHelper, ArtHints
from ..config import Settings
from ..content import ContentRef
from ..positions import PositionTracker
from ..state import (
    Capabilities,
    GroupTopology,
    NowPlaying,
    Player,
    PlayMode,
    QueueEntry,
    QueueState,
    StateStore,
    optimistic_position,
    side_id_for,
)
from ..tasks import stop_task
from .base import (
    Backoff,
    ContentUnavailableError,
    PlayableTrack,
    PrimedQueue,
    ServiceAuthError,
    SonosAdapter,
    VendorStation,
)

SERVICES = ("avTransport", "renderingControl")
SUBSCRIPTION_TIMEOUT_S = 600
RESNAPSHOT_MIN_GAP_S = 2.0  # topology events re-run discovery at most this often
# Sonos music-service ids seen in stream URIs (``sid=``). Best-effort; unknown ids stay None.
SERVICE_IDS = {"174": "tidal", "236": "pandora", "284": "ytmusic"}


QUEUE_DEBOUNCE_S = 0.5  # coalesce AVTransport bursts into one queue re-read

# SoCo PLAY_MODES: name -> (shuffle, repeat) where repeat is False | True | "ONE".
_SONOS_MODES: dict[tuple[bool, str], str] = {
    (False, "off"): "NORMAL",
    (True, "off"): "SHUFFLE_NOREPEAT",
    (True, "all"): "SHUFFLE",
    (False, "all"): "REPEAT_ALL",
    (True, "one"): "SHUFFLE_REPEAT_ONE",
    (False, "one"): "REPEAT_ONE",
}
_MODES_TO_HUB: dict[str, tuple[bool, str]] = {v: k for k, v in _SONOS_MODES.items()}


def play_mode_from_sonos(mode: str | None) -> PlayMode | None:
    """``NORMAL`` … ``SHUFFLE_REPEAT_ONE`` → hub shuffle/repeat; None for anything unknown."""
    hit = _MODES_TO_HUB.get(str(mode or "").upper())
    if hit is None:
        return None
    shuffle, repeat = hit
    return PlayMode(shuffle=shuffle, repeat=repeat)  # type: ignore[arg-type]


def play_mode_to_sonos(mode: PlayMode) -> str:
    return _SONOS_MODES[(mode.shuffle, mode.repeat)]


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
    """Zones, groups, raw SoCo handles, and the household's Tidal account serial (``sn``)."""

    zones: list[ZoneSnapshot] = field(default_factory=list)
    groups: list[GroupSnapshot] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)  # uid -> SoCo object, for subscribing
    tidal_sn: str | None = None  # Sonos account serial for Tidal, from the household accounts
    pandora_sn: str | None = None  # same for Pandora (Phase 5)
    ytmusic_sn: str | None = None  # same for YouTube Music (Phase 6)


class SonosSubscription(Protocol):
    callback: Callable[[Any], None] | None
    auto_renew_fail: Callable[[Exception], None] | None

    async def unsubscribe(self, strict: bool = True) -> None: ...


SnapshotFn = Callable[[], Awaitable[Topology]]
GroupsFn = Callable[[Any], Awaitable[list[GroupSnapshot]]]
SubscribeFn = Callable[[Any, str, Callable[[Any], None]], Awaitable[SonosSubscription]]
RunSync = Callable[..., Awaitable[Any]]


def ms_to_hms(position_ms: int) -> str:
    """Milliseconds to Sonos ``H:MM:SS`` (seconds truncated)."""
    total = max(0, position_ms) // 1000
    h, rem = divmod(total, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}"


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
        topo = snapshot_zones(zones or [])
        first = next(iter(zones), None) if zones else None
        topo.tidal_sn = discover_tidal_sn(first)
        topo.pandora_sn = discover_pandora_sn(first)
        topo.ytmusic_sn = discover_ytmusic_sn(first)
        return topo

    return await asyncio.to_thread(work)


def _soco_account_class() -> Any:  # pragma: no cover - import of the real SoCo class
    from soco.music_services.accounts import Account

    return Account


def discover_service_sn(
    zone: Any, service_type: str, account_class: Any | None = None
) -> str | None:
    """Read a service's account serial (``sn``) from the household's ``/status/accounts`` via
    SoCo. Sonos service types are ``sid*256 + 7``. Returns None when the service is not linked in
    the Sonos app (or the lookup fails). ``account_class`` is injectable so the mapping is
    testable without SoCo's network code."""
    if zone is None:
        return None
    try:
        account = account_class or _soco_account_class()
        for serial, acct in account.get_accounts(zone).items():
            if str(getattr(acct, "service_type", "")) == service_type:
                return str(serial)
    except Exception:  # noqa: BLE001 - best effort
        return None
    return None


def discover_tidal_sn(zone: Any, account_class: Any | None = None) -> str | None:
    """Tidal's ``sn`` (service type 44551); ``HUB_SONOS_TIDAL_SN`` is the manual fallback."""
    return discover_service_sn(zone, TIDAL_SERVICE_TYPE, account_class)


def discover_pandora_sn(zone: Any, account_class: Any | None = None) -> str | None:
    """Pandora's ``sn`` (service type 60423); ``HUB_SONOS_PANDORA_SN`` is the manual fallback."""
    return discover_service_sn(zone, PANDORA_SERVICE_TYPE, account_class)


def discover_ytmusic_sn(zone: Any, account_class: Any | None = None) -> str | None:
    """YouTube Music's ``sn`` (service type 72711); ``HUB_SONOS_YTMUSIC_SN`` is the fallback."""
    return discover_service_sn(zone, YTMUSIC_SERVICE_TYPE, account_class)


# -- Tidal on Sonos: refs built from the canonical Tidal id (docs/spikes/tidal-refs.md) -------

TIDAL_SID = 174
TIDAL_SERVICE_TYPE = str(TIDAL_SID * 256 + 7)  # "44551"
TIDAL_DESC = f"SA_RINCON{TIDAL_SERVICE_TYPE}_X_#Svc{TIDAL_SERVICE_TYPE}-0-Token"
TIDAL_PROTOCOL_INFO = "sonos.com-http:*:audio/flac:*"


def tidal_track_uri(track_id: str, sn: str) -> str:
    return f"x-sonos-http:track/{track_id}.flac?sid={TIDAL_SID}&flags=8224&sn={sn}"


def tidal_item_id(track_id: str) -> str:
    return f"10032020track/{track_id}"


def tidal_parent_id(album_id: str | None, playlist_id: str | None) -> str:
    if playlist_id:
        return f"1006006cplaylist/{playlist_id}"
    if album_id:
        return f"1004206calbum/{album_id}"
    return "10032020"


TIDAL_URI_RE = re.compile(r"^x-sonos-http:track/(\d+)\.flac")


def tidal_ref_from_uri(uri: str | None) -> ContentRef | None:
    """Canonical identity of a Sonos Tidal track: ``x-sonos-http:track/{id}.flac?…`` → id."""
    if not uri:
        return None
    m = TIDAL_URI_RE.match(uri)
    return ContentRef(service="tidal", kind="track", id=m.group(1)) if m else None


def build_tidal_didl(track: PlayableTrack, sn: str) -> Any:
    """A SoCo ``DidlMusicTrack`` the player accepts for a Tidal track (metadata carries the
    ``desc`` token that tells the player which account to stream with)."""
    from soco.data_structures import DidlMusicTrack, DidlResource

    res = DidlResource(uri=tidal_track_uri(track.track_id, sn), protocol_info=TIDAL_PROTOCOL_INFO)
    return DidlMusicTrack(
        title=track.title,
        parent_id=tidal_parent_id(track.album_id, track.playlist_id),
        item_id=tidal_item_id(track.track_id),
        resources=[res],
        desc=TIDAL_DESC,
        creator=track.artist,
        album=track.album,
    )


# -- YouTube Music on Sonos: refs built from the YouTube videoId (docs/spikes/ytmusic-sonos.md,
# **unverified on hardware**). The URI template is a setting so the first LAN run can correct it
# without a code change. ---------------------------------------------------------------------

YTMUSIC_SID = 284
YTMUSIC_SERVICE_TYPE = str(YTMUSIC_SID * 256 + 7)  # "72711"
YTMUSIC_DESC = f"SA_RINCON{YTMUSIC_SERVICE_TYPE}_X_#Svc{YTMUSIC_SERVICE_TYPE}-0-Token"
YTMUSIC_PROTOCOL_INFO = "sonos.com-http:*:audio/mp4:*"
YTMUSIC_URI_TEMPLATE = (
    "x-sonos-http:sonos-track:{id}.mp4?sid=" + str(YTMUSIC_SID) + "&flags=8232&sn={sn}"
)


def ytmusic_track_uri(video_id: str, sn: str, template: str = YTMUSIC_URI_TEMPLATE) -> str:
    return template.format(id=quote(video_id, safe=""), sn=sn)


def ytmusic_item_id(video_id: str) -> str:
    return f"00032020sonos-track:{quote(video_id, safe='')}"


def ytmusic_parent_id(album_id: str | None, playlist_id: str | None) -> str:
    if playlist_id:
        return f"0006206cplaylist:{quote(playlist_id, safe='')}"
    if album_id:
        return f"0004206calbum:{quote(album_id, safe='')}"
    return "00032020"


# scheme, optional "sonos-track" separator (":" or percent-encoded), exactly 11 id chars, then
# the end of the path (".mp4", "?" or end of string) so a prefix is never captured as the id.
YTMUSIC_URI_RE = re.compile(
    r"^x-sonos(?:api)?-[a-z-]+:(?:sonos-track(?::|%3[aA]))?([A-Za-z0-9_-]{11})(?=\.|\?|$)"
)
YTMUSIC_SID_RE = re.compile(rf"[?&]sid={YTMUSIC_SID}(?:&|$)")


def ytmusic_ref_from_uri(uri: str | None) -> ContentRef | None:
    """Canonical identity of a Sonos YouTube Music track: the 11-char videoId in the URI, only
    when the URI carries the YouTube Music service id (``sid=284`` as a whole parameter)."""
    if not uri or not YTMUSIC_SID_RE.search(uri):
        return None
    m = YTMUSIC_URI_RE.match(uri)
    return ContentRef(service="ytmusic", kind="track", id=m.group(1)) if m else None


def build_ytmusic_didl(track: PlayableTrack, sn: str, template: str = YTMUSIC_URI_TEMPLATE) -> Any:
    """A SoCo ``DidlMusicTrack`` for a YouTube Music track (``desc`` names the account)."""
    from soco.data_structures import DidlMusicTrack, DidlResource

    res = DidlResource(
        uri=ytmusic_track_uri(track.track_id, sn, template), protocol_info=YTMUSIC_PROTOCOL_INFO
    )
    return DidlMusicTrack(
        title=track.title,
        parent_id=ytmusic_parent_id(track.album_id, track.playlist_id),
        item_id=ytmusic_item_id(track.track_id),
        resources=[res],
        desc=YTMUSIC_DESC,
        creator=track.artist,
        album=track.album,
    )


# -- Pandora on Sonos: SMAPI stations (docs/spikes/pandora-refs.md, unverified on hardware) ----

PANDORA_SID = 236
PANDORA_SERVICE_TYPE = str(PANDORA_SID * 256 + 7)  # "60423"
PANDORA_DESC = f"SA_RINCON{PANDORA_SERVICE_TYPE}_X_#Svc{PANDORA_SERVICE_TYPE}-0-Token"
PANDORA_PROTOCOL_INFO = "sonos.com-http:*:*:*"
PANDORA_SERVICE_NAME = "Pandora"  # SoCo MusicService name (get_all_music_services_names)
STATION_ITEM_TYPES = {"stream", "program", "station"}


def pandora_station_uri(item_id: str, sn: str) -> str:
    """Programmed-radio URI the player resolves through the service (``getMediaURI``)."""
    return f"x-sonosapi-radio:{quote(item_id, safe='')}?sid={PANDORA_SID}&flags=8300&sn={sn}"


def pandora_item_id(item_id: str) -> str:
    return f"F00092020{quote(item_id, safe='')}"


PANDORA_URI_RE = re.compile(r"^x-sonosapi-radio:([^?]+)\?")


def pandora_item_from_uri(uri: str | None) -> str | None:
    """The SMAPI station id inside a playing Pandora URI, for matching now-playing to a station."""
    if not uri:
        return None
    m = PANDORA_URI_RE.match(uri)
    return m.group(1) if m else None


def build_station_didl(station: VendorStation, sn: str) -> tuple[str, Any]:
    """URI plus a SoCo ``DidlAudioBroadcast`` carrying the Pandora account descriptor."""
    from soco.data_structures import DidlAudioBroadcast, DidlResource

    item_id = station.ids.get("id") or ""
    uri = pandora_station_uri(item_id, sn)
    res = DidlResource(uri=uri, protocol_info=PANDORA_PROTOCOL_INFO)
    didl = DidlAudioBroadcast(
        title=station.name,
        parent_id="R:0/0",
        item_id=pandora_item_id(item_id),
        resources=[res],
        desc=PANDORA_DESC,
    )
    return uri, didl


_FIRST_CAP_RE = re.compile("(.)([A-Z][a-z]+)")
_ALL_CAP_RE = re.compile("([a-z0-9])([A-Z])")


def snake(key: str) -> str:
    """SoCo's ``camel_to_underscore`` (``albumArtURI`` → ``album_art_uri``), reproduced so the
    hub reads ``MusicServiceItem.metadata`` by the keys SoCo actually stores."""
    return _ALL_CAP_RE.sub(r"\1_\2", _FIRST_CAP_RE.sub(r"\1_\2", key)).lower()


def _md(item: Any, key: str, default: Any = None) -> Any:
    """Read a SMAPI field off a SoCo ``MusicServiceItem`` / ``MetadataDictBase`` or a raw dict.

    SoCo stores the SOAP fields **snake_cased** in ``.metadata`` (and coerces booleans), so both
    spellings are tried on dicts and only the snake form as an attribute."""
    snake_key = snake(key)
    if isinstance(item, dict):
        if key in item:
            return item[key]
        return item.get(snake_key, default)
    meta = getattr(item, "metadata", None)
    if isinstance(meta, dict):
        if snake_key in meta:
            return meta[snake_key]
        if key in meta:
            return meta[key]
    return getattr(item, snake_key, default)


def _truthy(value: Any) -> bool:
    return value is True or str(value).lower() == "true"


def _station_art(item: Any) -> str | None:
    """Collections carry ``albumArtURI``; programs/streams carry it in ``streamMetadata.logo`` or
    ``trackMetadata.albumArtURI`` (both nested ``MetadataDictBase`` objects)."""
    art = _md(item, "albumArtURI")
    if art:
        return str(art)
    stream = _md(item, "streamMetadata")
    logo = _md(stream, "logo") if stream is not None else None
    if logo:
        return str(logo)
    track = _md(item, "trackMetadata")
    art = _md(track, "albumArtURI") if track is not None else None
    return str(art) if art else None


def _is_smapi_auth_fault(exc: Exception) -> bool:
    """SoCo raises ``MusicServiceAuthException`` for expired/missing tokens and
    ``MusicServiceException`` for other SOAP faults; matched by class name so tests can raise
    look-alikes."""
    return any(k.__name__ == "MusicServiceAuthException" for k in type(exc).__mro__)


def _is_smapi_fault(exc: Exception) -> bool:
    return any(k.__name__ == "MusicServiceException" for k in type(exc).__mro__)


def collect_smapi_stations(
    service: Any, *, depth: int = 2, count: int = 100
) -> list[VendorStation]:
    """Walk a SMAPI service tree from ``root`` and return the playable stations (Pandora lists
    them under a "My Stations"/"Stations" container; a flat root is handled too). Runs inside
    ``asyncio.to_thread``; every ``get_metadata`` is a SOAP call.

    Items are SoCo ``MediaCollection`` (containers: ``item_type``, ``can_enumerate``,
    ``can_play`` as real bools) and ``MediaMetadata`` (playables: ``item_type`` only, art nested).
    A station is any item whose ``item_type`` is in :data:`STATION_ITEM_TYPES`, or a playable
    non-enumerable collection. Auth faults propagate as :class:`ServiceAuthError`; other SOAP
    faults as ``RuntimeError`` so the caller retries instead of caching an empty list."""
    out: list[VendorStation] = []
    seen: set[str] = set()

    def browse(item_id: str) -> list[Any]:
        try:
            result = service.get_metadata(item_id, index=0, count=count)
        except Exception as exc:  # noqa: BLE001 - classify SoCo's SOAP faults
            if _is_smapi_auth_fault(exc):
                raise ServiceAuthError(
                    "Pandora on Sonos needs to be signed in again in the Sonos app"
                ) from exc
            if _is_smapi_fault(exc):
                raise RuntimeError(f"Sonos Pandora browse fault: {exc}") from exc
            raise
        items = getattr(result, "items", None)
        if items is None:
            items = result if isinstance(result, list) else []
        return list(items)

    def visit(item_id: str, level: int) -> None:
        for it in browse(item_id):
            iid = _md(it, "id")
            if not iid or iid in seen:
                continue
            itype = str(_md(it, "itemType") or "").lower()
            can_enum = _truthy(_md(it, "canEnumerate", False))
            can_play = _truthy(_md(it, "canPlay", False))
            if itype in STATION_ITEM_TYPES or (can_play and not can_enum):
                seen.add(str(iid))
                out.append(
                    VendorStation(
                        vendor="sonos",
                        name=str(_md(it, "title") or "Station"),
                        ids={"id": str(iid)},
                        art_url=_station_art(it),
                        subtitle=str(_md(it, "summary") or "") or None,
                    )
                )
            elif can_enum and level < depth:
                visit(str(iid), level + 1)

    visit("root", 0)
    return out


def default_music_service(name: str, device: Any) -> Any:  # pragma: no cover - real SoCo
    from soco.music_services import MusicService

    return MusicService(name, device=device)


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


def _play_state(transport_state: str | None) -> str | None:
    """Map a Sonos transport state, or None when it carries no play/pause/stop meaning.

    Sonos reports ``TRANSITIONING`` for about a second while a stream buffers (measured on a
    Sonos Amp: ``t+01s TRANSITIONING`` then ``t+02s PLAYING``). Callers keep the state they
    already have rather than writing "unknown", because that clobbers the client's optimistic
    play and makes a tapped play button flick back for a second before it takes.
    """
    return {"PLAYING": "play", "PAUSED_PLAYBACK": "pause", "STOPPED": "stop"}.get(
        transport_state or ""
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
        run_sync: RunSync = asyncio.to_thread,
        clock: Callable[[], float] | None = None,
        art: ArtHelper | None = None,
        music_service: Callable[[str, Any], Any] = default_music_service,
    ) -> None:
        super().__init__(store, art=art)
        self.settings = settings
        self._music_service = music_service
        self._smapi: Any | None = None  # cached SoCo MusicService (SOAP session), per adapter
        self._smapi_lock = threading.Lock()
        # side id -> (station ref, SMAPI station id): the ref rides on now_playing.content_ref
        # while the playing URI still carries that station id.
        self._station_playing: dict[str, tuple[ContentRef, str]] = {}
        self._snapshot = snapshot or (lambda: default_snapshot(settings))
        self._groups = groups
        self._subscribe = subscribe
        self._run_sync = run_sync
        self._clock = clock or (lambda: asyncio.get_running_loop().time())
        self._trackers: dict[str, PositionTracker] = {}
        self._queue_refresh: dict[str, asyncio.Task[None]] = {}  # side id -> debounced re-read
        self._pollers: dict[str, asyncio.Task[None]] = {}
        self._last_snapshot_at: float | None = None
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
        for task in list(self._pollers.values()):
            await stop_task(task)
        self._pollers.clear()
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
        self._last_snapshot_at = self._clock()
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
        self._sync_pollers()
        with self._smapi_lock:
            self._smapi = None  # new topology, new SOAP session
        if self.pandora_sn:
            self._spawn(self._prewarm_smapi())

    async def _prewarm_smapi(self) -> None:
        """Fetch SoCo's music-services data (one SOAP round trip) against a known zone at connect,
        so the first station browse is not the one that discovers a broken session."""
        raw_zone = next(iter(self._topology.raw.values()), None)
        if raw_zone is None:
            return
        try:
            await self._run_sync(lambda: self._service_for(raw_zone))
        except Exception as exc:  # noqa: BLE001 - best effort; list_stations surfaces the cause
            self.log.warning("smapi prewarm failed", extra={"extra": {"error": str(exc)}})

    def _service_for(self, raw_zone: Any) -> Any:
        """The cached SoCo ``MusicService`` for Pandora (worker thread only)."""
        with self._smapi_lock:
            if self._smapi is None:
                self._smapi = self._music_service(PANDORA_SERVICE_NAME, raw_zone)
            return self._smapi

    # -- commands (Phase 1) ---------------------------------------------------------

    def _raw(self, player_id_: str) -> Any:
        uid = player_id_.removeprefix("sonos-")
        raw = self._topology.raw.get(uid)
        if raw is None:
            raise ConnectionError(f"Sonos player {player_id_!r} is not available")
        return raw

    async def play(self, player_id: str) -> None:
        await self._run_sync(self._raw(player_id).play)

    async def pause(self, player_id: str) -> None:
        await self._run_sync(self._raw(player_id).pause)

    async def stop(self, player_id: str) -> None:
        await self._run_sync(self._raw(player_id).stop)

    async def next(self, player_id: str) -> None:
        await self._run_sync(self._raw(player_id).next)

    async def previous(self, player_id: str) -> None:
        await self._run_sync(self._raw(player_id).previous)

    async def seek(self, player_id: str, position_ms: int) -> None:
        raw = self._raw(player_id)
        await self._run_sync(raw.seek, ms_to_hms(position_ms))
        player = self.store.state.players.get(player_id)
        if player is not None:
            side = side_id_for(player)
            tracker = self._trackers.get(side)
            if tracker is not None:
                tracker.reset()
            # Optimistic: the scrubber lands where the user put it until the next poll confirms.
            if side in self.store.state.sides:
                self.store.set_position(side, optimistic_position(position_ms))

    async def set_volume(self, player_id: str, level: int) -> None:
        raw = self._raw(player_id)
        await self._run_sync(setattr, raw, "volume", level)

    async def set_mute(self, player_id: str, muted: bool) -> None:
        raw = self._raw(player_id)
        await self._run_sync(setattr, raw, "mute", muted)

    def _current_members(self, player_id_: str) -> list[str]:
        """Members of the native group ``player_id_`` is in right now (itself when solo)."""
        state = self.store.state
        p = state.players.get(player_id_)
        if p is None or p.group_id is None or p.group_id not in state.groups:
            return [player_id_]
        return list(state.groups[p.group_id].member_ids)

    async def set_group(self, member_ids: list[str]) -> None:
        """Reconcile the coordinator's group to ``member_ids``: leavers ``unjoin`` first, then
        newcomers ``join``. A single id ``unjoin``s that player."""
        if not member_ids:
            raise ValueError("set_group needs at least one member")
        coordinator_id = member_ids[0]
        coordinator = self._raw(coordinator_id)
        if len(member_ids) == 1:
            await self._run_sync(coordinator.unjoin)
            return
        current = self._current_members(coordinator_id)
        wanted = set(member_ids)
        for leaver in current:
            if leaver not in wanted and leaver != coordinator_id:
                await self._run_sync(self._raw(leaver).unjoin)
        for member in member_ids[1:]:
            if member not in current:
                await self._run_sync(self._raw(member).join, coordinator)

    async def dissolve(self, coordinator_id: str, member_ids: list[str]) -> None:
        """Sonos has no group object to delete: every non-coordinator ``unjoin``s."""
        for member in member_ids:
            if member != coordinator_id:
                await self._run_sync(self._raw(member).unjoin)

    # -- position polling -------------------------------------------------------------

    # -- content (Phase 3) ----------------------------------------------------------

    @property
    def tidal_sn(self) -> str | None:
        return self._topology.tidal_sn or self.settings.sonos_tidal_sn

    @property
    def pandora_sn(self) -> str | None:
        return self._topology.pandora_sn or self.settings.sonos_pandora_sn

    @property
    def ytmusic_sn(self) -> str | None:
        return self._topology.ytmusic_sn or self.settings.sonos_ytmusic_sn

    def service_linked(self, service: str) -> bool:
        if service == "tidal":
            return bool(self.tidal_sn)
        if service == "pandora":
            return bool(self.pandora_sn)
        if service == "ytmusic":
            return bool(self.ytmusic_sn)
        return False

    def _queue_builder(self, service: str) -> Callable[[PlayableTrack], Any]:
        """Per-service DIDL builder bound to the account serial; raises when the service is not
        linked in the Sonos app (or the hub cannot play it there at all)."""
        if service == "tidal":
            sn = self.tidal_sn
            if not sn:
                raise ContentUnavailableError("Tidal is not linked in the Sonos app")
            return lambda t: build_tidal_didl(t, sn)
        if service == "ytmusic":
            sn = self.ytmusic_sn
            if not sn:
                raise ContentUnavailableError("YouTube Music is not linked in the Sonos app")
            template = self.settings.sonos_ytmusic_uri or YTMUSIC_URI_TEMPLATE
            return lambda t: build_ytmusic_didl(t, sn, template)
        raise ContentUnavailableError(f"{service} is not playable on Sonos from the hub")

    # -- radio stations (Phase 5) -----------------------------------------------------

    async def list_stations(self, service: str) -> list[VendorStation]:
        """SMAPI browse through SoCo ``MusicService`` on any zone of the household. **Unverified
        on hardware** (docs/spikes/pandora-refs.md): Sonos moved Pandora to app-link auth, and
        SoCo's SMAPI client may need the household's token; ``HUB_SONOS_PANDORA_SN`` only
        covers the account serial."""
        if service != "pandora":
            raise ContentUnavailableError(f"{service} stations are not available on Sonos")
        if not self.pandora_sn:
            raise ContentUnavailableError("Pandora is not linked in the Sonos app")
        raw_zone = next(iter(self._topology.raw.values()), None)
        if raw_zone is None:
            raise ContentUnavailableError("no Sonos player is reachable")

        def work() -> list[VendorStation]:
            try:
                svc = self._service_for(raw_zone)
            except Exception as exc:  # noqa: BLE001 - SoCo's service lookup itself can fault
                with self._smapi_lock:
                    self._smapi = None
                if _is_smapi_auth_fault(exc):
                    raise ServiceAuthError(
                        "Pandora on Sonos needs to be signed in again in the Sonos app"
                    ) from exc
                raise RuntimeError(f"Sonos music service lookup failed: {exc}") from exc
            try:
                return collect_smapi_stations(svc)
            except ServiceAuthError:
                with self._smapi_lock:
                    self._smapi = None  # drop the broken session; the next call rebuilds it
                raise

        return await self._run_sync(work)

    async def play_station(self, player_id: str, ref: ContentRef, station: VendorStation) -> None:
        """``SetAVTransportURI`` with the station's programmed-radio URI and broadcast DIDL, then
        play (SoCo ``play_uri``). Pandora chooses the tracks; ``next`` skips."""
        if ref.service != "pandora":
            raise ContentUnavailableError(f"{ref.service} stations are not playable on Sonos")
        sn = self.pandora_sn
        if not sn:
            raise ContentUnavailableError("Pandora is not linked in the Sonos app")
        if not station.ids.get("id"):
            raise ContentUnavailableError(f"{station.name} has no Sonos station id")
        raw = self._raw(player_id)
        side = side_id_for(self.store.state.players[player_id])
        previous = self.store.state.now_playing.get(side)
        previous_station = self._station_playing.get(side)
        # Optimistic first, like the other commands: the UI lands on the station immediately and
        # the transport event confirms (or the failure below puts things back).
        self._station_playing[side] = (ref, str(station.ids["id"]))
        self._trackers.pop(side, None)
        self.store.set_now_playing(side, _station_now_playing(self.art, station, ref))
        if side in self.store.state.sides:
            self.store.set_position(side, optimistic_position(0))
            for member in self.store.state.sides[side].member_ids:
                self.store.update_player(member, play_state="play")

        def work() -> None:
            from soco.data_structures import to_didl_string

            uri, didl = build_station_didl(station, sn)
            raw.play_uri(uri, meta=to_didl_string(didl), title=station.name, start=True)

        try:
            await self._run_sync(work)
        except Exception:
            if previous_station is None:
                self._station_playing.pop(side, None)
            else:
                self._station_playing[side] = previous_station
            if previous is not None:
                self.store.set_now_playing(side, previous)
            raise

    async def play_content(
        self, player_id: str, ref: ContentRef, tracks: list[PlayableTrack], start_index: int = 0
    ) -> None:
        """Replace the coordinator's queue with tracks built from canonical ids (Tidal or
        YouTube Music), then play from ``start_index``. **Unverified on hardware** (ai-dev #10,
        docs/spikes/ytmusic-sonos.md); every SoCo call runs in a worker thread."""
        build = self._queue_builder(ref.service)
        raw = self._raw(player_id)
        self._station_playing.pop(side_id_for(self.store.state.players[player_id]), None)

        if not 0 <= start_index < len(tracks):
            raise IndexError(f"start_index {start_index} out of range for {len(tracks)} tracks")

        def work() -> None:
            items = [build(t) for t in tracks]
            raw.clear_queue()
            raw.add_multiple_to_queue(items)  # SoCo chunks the SOAP calls itself (16 per call)
            raw.play_from_queue(start_index)

        await self._run_sync(work)
        side = side_id_for(self.store.state.players[player_id])
        self._trackers.pop(side, None)
        self.store.set_position(side, optimistic_position(0))

    async def prime_content(
        self, player_id: str, ref: ContentRef, tracks: list[PlayableTrack], start_index: int = 0
    ) -> PrimedQueue:
        """Sync Play priming: replace the queue, then ``play_from_queue(index, start=False)``,
        which selects the queue and positions it **without playing**. Returns the queued item's
        title for verification. **Unverified on hardware** (docs/sync-engine.md)."""
        build = self._queue_builder(ref.service)
        raw = self._raw(player_id)
        self._station_playing.pop(side_id_for(self.store.state.players[player_id]), None)
        if not 0 <= start_index < len(tracks):
            raise IndexError(f"start_index {start_index} out of range for {len(tracks)} tracks")

        def work() -> Any:
            items = [build(t) for t in tracks]
            raw.clear_queue()
            raw.add_multiple_to_queue(items)
            raw.play_from_queue(start_index, start=False)
            try:
                raw.pause()  # belt and braces: some firmware starts anyway
            except Exception:  # noqa: BLE001 - pausing an already-stopped transport errors
                pass
            queued = raw.get_queue(start_index, 1)
            return queued[0] if queued else None

        item = await self._run_sync(work)
        side = side_id_for(self.store.state.players[player_id])
        self._trackers.pop(side, None)
        if side in self.store.state.sides:
            self.store.set_position(side, optimistic_position(0))
        expected = tracks[start_index]
        return PrimedQueue(
            track_id=expected.track_id,
            title=str(getattr(item, "title", None) or expected.title),
        )

    async def start_primed(self, player_id: str, start_index: int = 0) -> None:
        raw = self._raw(player_id)
        await self._run_sync(raw.play)

    async def snapshot_queue(self, player_id: str) -> Any | None:
        """SoCo ``Snapshot(device, snapshot_queue=True)`` captures queue, transport and position
        so a failed Sync Play start can put the room back. **Unverified on hardware.**"""
        raw = self._raw(player_id)

        def work() -> Any:
            from soco.snapshot import Snapshot

            snap = Snapshot(raw, snapshot_queue=True)
            snap.snapshot()
            return snap

        try:
            return await self._run_sync(work)
        except Exception as exc:  # noqa: BLE001 - a missing snapshot only weakens the rollback
            self.log.warning("sonos queue snapshot failed", extra={"extra": {"error": str(exc)}})
            return None

    async def restore_queue(self, player_id: str, snapshot: Any) -> None:
        if snapshot is None:
            return
        await self._run_sync(snapshot.restore)

    # -- queue and play mode (Phase 8) -----------------------------------------------------

    async def get_queue(self, player_id: str, limit: int = 200) -> tuple[list[QueueEntry], int]:
        """SoCo ``get_queue(start, max_items)`` (DIDL tracks) plus ``queue_size``; the coordinator
        holds the group's queue. Runs in a worker thread; only plain data comes back."""
        raw = self._raw(player_id)
        uid = player_id.removeprefix("sonos-")
        zone_ip = next((z.ip for z in self._topology.zones if z.uid == uid), None)

        def work() -> tuple[list[dict[str, Any]], int]:
            items = raw.get_queue(0, limit, full_album_art_uri=True)
            total = getattr(items, "total_matches", None)
            if total is None:
                try:
                    total = int(raw.queue_size)
                except Exception:  # noqa: BLE001 - optional
                    total = len(items)
            out: list[dict[str, Any]] = []
            for it in items:
                res = getattr(it, "resources", None) or []
                uri = getattr(res[0], "uri", None) if res else None
                duration = getattr(res[0], "duration", None) if res else None
                out.append(
                    {
                        "title": getattr(it, "title", None),
                        "creator": getattr(it, "creator", None),
                        "album": getattr(it, "album", None),
                        "album_art_uri": getattr(it, "album_art_uri", None),
                        "uri": uri,
                        "duration": duration,
                    }
                )
            return out, int(total)

        rows, total = await self._run_sync(work)
        entries = [
            QueueEntry(
                index=i,
                title=r["title"],
                artist=r["creator"],
                album=r["album"],
                duration_ms=_hms_to_ms(str(r["duration"])) if r["duration"] else None,
                art=self.art.ref(
                    ArtHints(
                        device_url=absolutize(r["album_art_uri"], zone_ip) if zone_ip else None,
                        service=source_from_uri(r["uri"]),
                    )
                ),
                track_id=r["uri"],
                content_ref=tidal_ref_from_uri(r["uri"]) or ytmusic_ref_from_uri(r["uri"]),
            )
            for i, r in enumerate(rows[:limit])
        ]
        return entries, total

    async def play_queue_index(self, player_id: str, index: int) -> None:
        """``play_from_queue(index)`` after a ``queue_size`` bound check; a UPnP refusal for a
        position the player does not have is reported as ``IndexError`` (→ invalid_argument)."""
        raw = self._raw(player_id)

        def work() -> None:
            try:
                size = int(raw.queue_size)
            except Exception:  # noqa: BLE001 - optional on some firmware
                size = None
            if size is not None and not 0 <= index < size:
                raise IndexError(f"queue index {index} out of range for {size} tracks")
            try:
                raw.play_from_queue(index)
            except Exception as exc:  # noqa: BLE001 - SoCoUPnPException 7xx for a bad position
                if "SoCoUPnPException" in type(exc).__name__ or "UPnP" in type(exc).__name__:
                    raise IndexError(f"queue index {index} refused by the player: {exc}") from exc
                raise

        await self._run_sync(work)

    async def get_play_mode(self, player_id: str) -> PlayMode:
        raw = self._raw(player_id)
        mode = await self._run_sync(lambda: raw.play_mode)
        return play_mode_from_sonos(mode) or PlayMode()

    async def set_play_mode(self, player_id: str, mode: PlayMode) -> None:
        raw = self._raw(player_id)
        target = play_mode_to_sonos(mode)

        def work() -> None:
            raw.play_mode = target

        await self._run_sync(work)
        self.store.set_play_mode(player_id, mode)

    def _move_queue_pointer(self, p_id: str, current_track: Any) -> None:
        """AVTransport ``CurrentTrack`` is the 1-based queue position; keep ``current_index``
        in step without re-reading the whole queue."""
        player = self.store.state.players.get(p_id)
        if player is None:
            return
        side = side_id_for(player)
        queued = self.store.state.queues.get(side)
        try:
            idx = int(str(current_track)) - 1
        except (TypeError, ValueError):
            return
        if queued is None or idx < 0 or queued.current_index == idx:
            return
        self.store.set_queue(side, queued.model_copy(update={"current_index": idx}))

    def _schedule_queue_refresh(self, p_id: str) -> None:
        player = self.store.state.players.get(p_id)
        if player is None:
            return
        side = side_id_for(player)
        pending = self._queue_refresh.get(side)
        if pending is not None and not pending.done():
            pending.cancel()
        task = self._spawn(self._refresh_queue(side, p_id))
        if task is not None:
            self._queue_refresh[side] = task

    async def _refresh_queue(self, side: str, p_id: str) -> None:
        await asyncio.sleep(QUEUE_DEBOUNCE_S)
        try:
            entries, total = await self.get_queue(p_id)
        except Exception as exc:  # noqa: BLE001 - keep the last snapshot
            self.log.debug("queue re-read failed", extra={"extra": {"error": str(exc)}})
            return
        if side not in self.store.state.sides:
            return
        np = self.store.state.now_playing.get(side)
        current = None
        if np is not None and np.track_id:
            current = next((e.index for e in entries if e.track_id == np.track_id), None)
        self.store.set_queue(
            side,
            QueueState(
                items=entries,
                current_index=current,
                source=np.source if np else None,
                total=total,
                truncated=total > len(entries),
            ),
        )
        self._emit("queue", player_id=p_id)

    def _sync_pollers(self) -> None:
        """One poller per side whose coordinator is playing; stop the rest."""
        state = self.store.state
        wanted = {
            sid: side.coordinator_player_id
            for sid, side in state.sides.items()
            if side.vendor == "sonos" and side.play_state == "play"
        }
        for sid in list(self._pollers):
            if sid not in wanted:
                self._pollers.pop(sid).cancel()
                tracker = self._trackers.get(sid)
                if tracker is not None:
                    tracker.set_playing(False, self._clock())
                    pos = tracker.to_position(self._clock())
                    if pos is not None and sid in state.sides:
                        self.store.set_position(sid, pos)
        for sid, coordinator in wanted.items():
            if sid not in self._pollers:
                tracker = self._trackers.setdefault(sid, PositionTracker())
                tracker.set_playing(True, self._clock())
                task = self._spawn(self._poll_position(sid, coordinator))
                if task is not None:
                    self._pollers[sid] = task

    async def _poll_once(self, side_id: str, coordinator: str) -> None:
        """One ``GetPositionInfo`` read folded into the side's tracker and written to state."""
        tracker = self._trackers[side_id]
        info = await self._run_sync(self._raw(coordinator).get_current_track_info)
        reported = _hms_to_ms(str(info.get("position", "")))
        now_s = self._clock()
        if reported is not None:
            tracker.observe(reported, now_s)
            pos = tracker.to_position(now_s)
            if pos is not None and side_id in self.store.state.sides:
                self.store.set_position(side_id, pos)

    async def _poll_position(self, side_id: str, coordinator: str) -> None:
        tracker = self._trackers[side_id]
        base = self.settings.position_poll_s
        while True:
            try:
                await self._poll_once(side_id, coordinator)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep polling through transient errors
                self.log.debug("position poll failed", extra={"extra": {"error": str(exc)}})
            await asyncio.sleep(tracker.next_poll_in(self._clock(), base))

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
            play_state=(_play_state(z.transport_state) or "stop") if z.transport_state else "stop",  # type: ignore[arg-type]
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
        mode = variables.get("current_play_mode")
        if mode:
            mapped_mode = play_mode_from_sonos(str(mode))
            if mapped_mode is not None:
                self.store.set_play_mode(p_id, mapped_mode)
        # The queue itself changed (add/remove/reorder): re-read. A plain track advance only
        # moves the pointer, which ``current_track`` (1-based) gives us for free.
        if any(k in variables for k in ("number_of_tracks", "queue_update_id")):
            self._schedule_queue_refresh(p_id)
        elif "current_track" in variables:
            self._move_queue_pointer(p_id, variables.get("current_track"))
        state = variables.get("transport_state")
        if state:
            mapped = _play_state(state)
            if mapped is not None:  # TRANSITIONING: hold the known state, do not blank it
                self.store.update_player(p_id, play_state=mapped)
            self._emit("play_state", player_id=p_id)
            self._sync_pollers()
        meta = variables.get("current_track_meta_data")
        if meta:
            player = self.store.state.players.get(p_id)
            if not player:
                return
            side = side_id_for(player)
            previous = self.store.state.now_playing.get(side)
            np = self._now_playing(meta, variables, zone, side)
            self.store.set_now_playing(side, np)
            if previous is None or previous.track_id != np.track_id:
                tracker = self._trackers.get(side)
                if tracker is not None:
                    tracker.reset()
                if side in self.store.state.sides:
                    self.store.set_position(side, optimistic_position(0))
            self._emit("now_playing", player_id=p_id)

    def _now_playing(
        self, meta: Any, variables: dict[str, Any], zone: ZoneSnapshot, side: str | None = None
    ) -> NowPlaying:
        get = meta.get if isinstance(meta, dict) else lambda k, d=None: getattr(meta, k, d)
        art = get("album_art_uri") or get("album_art")
        uri = variables.get("current_track_uri") or get("uri")
        duration = variables.get("current_track_duration")
        broadcast = "audioBroadcast" in (get("item_class") or "")
        source = source_from_uri(uri)
        station_ref: ContentRef | None = None
        stored = self._station_playing.get(side) if side else None
        if stored is not None:
            station_ref, station_id = stored
            playing_id = pandora_item_from_uri(uri)
            if source != station_ref.service or (
                playing_id is not None and unquote(playing_id) != station_id
            ):
                # The room moved on: another service, or another station from the Sonos app.
                self._station_playing.pop(side or "", None)
                station_ref = None
        radio = broadcast or source == "pandora"
        return NowPlaying(
            title=get("title"),
            artist=get("creator"),
            album=get("album"),
            art=self.art.ref(ArtHints(device_url=absolutize(art, zone.ip), service=source)),
            source=source,
            seekable=not radio,
            supports_next=True if source == "pandora" else not broadcast,  # Pandora skips
            supports_prev=not radio,
            duration_ms=_hms_to_ms(duration) if duration else None,
            track_id=uri or None,
            content_ref=tidal_ref_from_uri(uri) or ytmusic_ref_from_uri(uri) or station_ref,
        )

    async def _refresh_groups(self, zone: ZoneSnapshot) -> None:
        """Topology changed. Re-run discovery (throttled) so new or returning players enter the
        store and get subscriptions; otherwise just re-read the groups."""
        try:
            now_s = self._clock()
            last = self._last_snapshot_at
            if last is None or now_s - last >= RESNAPSHOT_MIN_GAP_S:
                await self._resnapshot()
            else:
                raw = self._topology.raw.get(zone.uid)
                groups = await self._groups(raw)
                self._topology.groups = groups
                self.store.set_groups("sonos", self._to_groups(groups))
            self._emit("topology")
            self._sync_pollers()
        except Exception as exc:  # noqa: BLE001
            self.log.warning("group refresh failed", extra={"extra": {"error": str(exc)}})

    async def _resnapshot(self) -> None:
        topo = await self._snapshot()
        self._last_snapshot_at = self._clock()
        if not topo.zones:
            return  # transient discovery miss; keep what we have
        known = {z.uid for z in self._topology.zones}
        for z in topo.zones:
            self._topology.raw[z.uid] = topo.raw.get(z.uid)
            if z.uid not in known:
                self.store.upsert_player(self._to_player(z))
                for service in SERVICES:
                    await self._add_subscription(z, service)
                self.log.info("sonos player joined", extra={"extra": {"player": z.name}})
            else:
                self.store.update_player(player_id(z.uid), online=True, ip=z.ip, name=z.name)
        self._topology.zones = topo.zones
        self._topology.groups = topo.groups
        self.store.set_groups("sonos", self._to_groups(topo.groups))


def _hms_to_ms(value: str) -> int | None:
    """``H:MM:SS`` (Sonos) to milliseconds. Returns None for ``NOT_IMPLEMENTED`` etc."""
    parts = value.split(":")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return None
    h, m, s = (int(p) for p in parts)
    return ((h * 60 + m) * 60 + s) * 1000


def _station_now_playing(art: ArtHelper, station: VendorStation, ref: ContentRef) -> NowPlaying:
    return NowPlaying(
        title=station.name,
        art=art.ref(ArtHints(device_url=station.art_url, service=ref.service)),
        source=ref.service,
        seekable=False,
        supports_next=True,
        supports_prev=False,
        duration_ms=None,
        content_ref=ref,
    )
