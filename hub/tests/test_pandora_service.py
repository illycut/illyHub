"""PandoraService: per-vendor station lists merged by name, availability, cache, errors."""

from __future__ import annotations

from typing import Any

import pytest

from illyhub_hub.adapters.base import ContentUnavailableError, VendorStation
from illyhub_hub.art import ArtHelper
from illyhub_hub.content import ContentNotFoundError, ContentRef, NeedsLinkError
from illyhub_hub.services.pandora import PandoraService, station_key


class Status:
    def __init__(self, state: str) -> None:
        self.state = state


class Src:
    """Stands in for a PlaybackAdapter: only list_stations/status are used by the service."""

    def __init__(self, vendor: str, stations: list[VendorStation] | Exception | None) -> None:
        self.vendor = vendor
        self.stations = stations
        self.calls = 0
        self.connected = True

    def status(self) -> Status:
        return Status("connected" if self.connected else "reconnecting")

    async def list_stations(self, service: str) -> list[VendorStation]:
        self.calls += 1
        assert service == "pandora"
        if self.stations is None:
            raise ContentUnavailableError("not linked")
        if isinstance(self.stations, Exception):
            raise self.stations
        return self.stations


def st(vendor: str, name: str, ident: str, art: str | None = None) -> VendorStation:
    ids = {"sid": "1", "cid": "c", "mid": ident} if vendor == "heos" else {"id": ident}
    return VendorStation(vendor=vendor, name=name, ids=ids, art_url=art)


def service(sources: dict[str, Any], clock: list[float] | None = None) -> PandoraService:
    ticks = clock or [0.0]
    return PandoraService(
        lambda: sources,
        art=ArtHelper(None),
        is_connected=lambda v: v in sources and sources[v].connected,
        cache_ttl_s=300,
        clock=lambda: ticks[0],
    )


def test_station_key_normalises_names() -> None:
    assert station_key("Chill Radio") == "chill-radio"
    assert station_key("  90's Alternative!! ") == "90-s-alternative"
    assert station_key("Émilie") == "emilie"
    assert station_key("***").startswith("station-") and len(station_key("***")) == 16
    ContentRef(service="pandora", kind="station", id=station_key("Jazz / Blues"))  # valid id


async def test_merge_by_name_with_availability_and_art_fallback() -> None:
    heos = Src(
        "heos",
        [st("heos", "Chill Radio", "h1", "http://heos/chill.jpg"), st("heos", "Only HEOS", "h2")],
    )
    sonos = Src(
        "sonos",
        [st("sonos", "chill radio", "s1"), st("sonos", "Only Sonos", "s2", "http://sonos/os.jpg")],
    )
    svc = service({"heos": heos, "sonos": sonos})
    items = await svc.stations()
    assert [i.title for i in items] == ["Chill Radio", "Only HEOS", "Only Sonos"]
    chill = items[0]
    assert chill.content_ref == ContentRef(service="pandora", kind="station", id="chill-radio")
    assert chill.availability.heos is True and chill.availability.sonos is True
    assert chill.subtitle == "Pandora station"
    only_heos, only_sonos = items[1], items[2]
    assert (only_heos.availability.heos, only_heos.availability.sonos) == (True, False)
    assert (only_sonos.availability.heos, only_sonos.availability.sonos) == (False, True)
    refs = svc.vendor_stations(chill.content_ref)
    assert refs["heos"].ids["mid"] == "h1" and refs["sonos"].ids["id"] == "s1"
    assert svc.vendor_stations(only_sonos.content_ref).keys() == {"sonos"}
    assert svc.linked == {"heos": True, "sonos": True}


async def test_same_vendor_name_collision_gets_a_suffix() -> None:
    heos = Src("heos", [st("heos", "Mix", "a"), st("heos", "MIX", "b")])
    svc = service({"heos": heos})
    items = await svc.stations()
    assert [i.content_ref.id for i in items] == ["mix", "mix-2"]
    assert svc.vendor_stations(items[1].content_ref)["heos"].ids["mid"] == "b"


async def test_cache_ttl_and_refresh() -> None:
    heos = Src("heos", [st("heos", "A", "a")])
    clock = [0.0]
    svc = service({"heos": heos}, clock)
    await svc.stations()
    await svc.stations()
    assert heos.calls == 1
    clock[0] = 301.0
    await svc.stations()
    assert heos.calls == 2
    assert svc.refresh() == 2  # both vendors' entries (sonos cached as 'not linked')
    await svc.stations()
    assert heos.calls == 3


async def test_needs_link_when_no_vendor_is_linked_or_connected() -> None:
    heos, sonos = Src("heos", None), Src("sonos", [st("sonos", "A", "a")])
    sonos.connected = False
    svc = service({"heos": heos, "sonos": sonos})
    with pytest.raises(NeedsLinkError) as exc:
        await svc.stations()
    assert exc.value.service == "pandora"
    assert sonos.calls == 0  # a disconnected vendor is never asked
    assert svc.linked == {"heos": False, "sonos": None}  # not linked / not connected
    sonos.connected = True
    svc.refresh()
    assert [i.title for i in await svc.stations()] == ["A"]
    assert svc.linked == {"heos": False, "sonos": True}


async def test_one_vendor_error_is_skipped_and_not_cached_both_errors_raise() -> None:
    heos = Src("heos", RuntimeError("timeout"))
    sonos = Src("sonos", [st("sonos", "A", "a")])
    svc = service({"heos": heos, "sonos": sonos})
    items = await svc.stations()
    assert [i.title for i in items] == ["A"] and items[0].availability.heos is False
    assert svc.debug() == {"stations": 1, "errors": {"heos": "RuntimeError: timeout"}}
    await svc.stations()
    assert heos.calls == 2  # failures are retried, not cached
    sonos.stations = RuntimeError("down")
    svc.refresh()
    with pytest.raises(RuntimeError, match="heos: RuntimeError: timeout"):
        await svc.stations()


async def test_station_lookup_builds_map_and_rejects_unknown() -> None:
    heos = Src("heos", [st("heos", "Chill Radio", "h1")])
    svc = service({"heos": heos})
    ref = ContentRef(service="pandora", kind="station", id="chill-radio")
    item = await svc.station(ref)  # builds the map on demand
    assert item.title == "Chill Radio" and heos.calls == 1
    with pytest.raises(ContentNotFoundError):
        await svc.station(ContentRef(service="pandora", kind="station", id="nope"))
    with pytest.raises(ContentNotFoundError):
        await svc.station(ContentRef(service="tidal", kind="album", id="1"))
    assert svc.vendor_stations(ContentRef(service="pandora", kind="station", id="nope")) == {}


# --------------------------------------------------------------------------------------
# Review remediation: key folding, auth faults, refresh, wedge sequence, single-flight, linked map
# --------------------------------------------------------------------------------------


def test_station_key_folds_accents_and_handles_non_latin_and_long_names() -> None:
    assert station_key("Café") == station_key("Cafe") == "cafe"
    assert station_key("Naïve Jazz") == "naive-jazz"
    cjk_a, cjk_b = station_key("爵士乐"), station_key("古典音乐")
    assert cjk_a.startswith("station-") and cjk_b.startswith("station-") and cjk_a != cjk_b
    assert station_key("🎵🎵") != station_key("🎸")
    long = "A" * 300
    key = station_key(long)
    assert len(key) <= 180
    ContentRef(service="pandora", kind="station", id=key)  # valid id, no 500


async def test_cjk_names_on_both_vendors_do_not_merge_but_accented_ones_do() -> None:
    heos = Src("heos", [st("heos", "爵士乐", "h1"), st("heos", "Café", "h2")])
    sonos = Src("sonos", [st("sonos", "古典音乐", "s1"), st("sonos", "Cafe", "s2")])
    svc = service({"heos": heos, "sonos": sonos})
    items = await svc.stations()
    keys = {i.content_ref.id: i for i in items}
    assert len(items) == 3  # two hashed CJK keys + one merged "cafe"
    cafe = keys["cafe"]
    assert cafe.availability.heos and cafe.availability.sonos and cafe.title == "Café"


async def test_auth_fault_is_needs_link_with_a_distinct_message_and_not_an_empty_account() -> None:
    from illyhub_hub.adapters.base import ServiceAuthError

    heos = Src("heos", None)
    sonos = Src("sonos", ServiceAuthError("Pandora on Sonos needs to be signed in again"))
    svc = service({"heos": heos, "sonos": sonos})
    with pytest.raises(NeedsLinkError) as exc:
        await svc.stations()
    assert "signed in again" in exc.value.message
    assert svc.linked == {"heos": False, "sonos": False}
    assert svc.last_error == "Sonos: Pandora on Sonos needs to be signed in again"
    # linked on the other side: the list comes from HEOS and the fault stays visible
    heos.stations = [st("heos", "A", "a")]
    svc.refresh()
    items = await svc.stations()
    assert [i.title for i in items] == ["A"] and svc.linked == {"heos": True, "sonos": False}
    assert svc.last_error and svc.last_error.startswith("Sonos:")
    # an empty account is linked-and-empty, not an auth problem
    sonos.stations = []
    svc.refresh()
    await svc.stations()
    assert svc.linked == {"heos": True, "sonos": True} and svc.last_error is None


async def test_refresh_clears_the_merged_map_and_station_refetches_when_cold() -> None:
    heos = Src("heos", [st("heos", "A", "a")])
    clock = [0.0]
    svc = service({"heos": heos}, clock)
    ref = ContentRef(service="pandora", kind="station", id="a")
    await svc.station(ref)
    assert svc.vendor_stations(ref) and heos.calls == 1
    svc.refresh()
    assert svc.vendor_stations(ref) == {} and svc.linked == {"heos": None, "sonos": None}
    await svc.station(ref)
    assert heos.calls == 2
    # Cold cache with a known key still refetches (a station added later must be playable).
    clock[0] = 1000.0
    heos.stations = [st("heos", "A", "a"), st("heos", "New", "n")]
    await svc.station(ContentRef(service="pandora", kind="station", id="new"))
    assert heos.calls == 3


async def test_wedge_sequence_error_then_unlinked_yields_needs_link_not_500() -> None:
    heos = Src("heos", RuntimeError("timeout"))
    svc = service({"heos": heos})
    with pytest.raises(RuntimeError):
        await svc.stations()
    assert svc.linked == {"heos": None, "sonos": None} and svc.last_error
    heos.stations = None  # now the vendor reports "not linked": the old error must not linger
    with pytest.raises(NeedsLinkError) as exc:
        await svc.stations()
    assert exc.value.message == "Pandora is not linked in the HEOS or Sonos app."
    assert svc.last_error is None
    # adapter disappearing (discovery lost it) also clears the remembered error
    heos.stations = RuntimeError("again")
    svc.refresh()  # the "unlinked" outcome above was cached; errors never are
    with pytest.raises(RuntimeError):
        await svc.stations()
    svc2 = service({})  # no adapters at all
    with pytest.raises(NeedsLinkError):
        await svc2.stations()
    assert svc2.linked == {"heos": None, "sonos": None}


async def test_concurrent_callers_share_one_fetch_per_vendor() -> None:
    import asyncio

    class Slow(Src):
        async def list_stations(self, service: str) -> list[VendorStation]:
            await asyncio.sleep(0.02)
            return await super().list_stations(service)

    heos = Slow("heos", [st("heos", "A", "a")])
    sonos = Slow("sonos", [st("sonos", "B", "b")])
    svc = service({"heos": heos, "sonos": sonos})
    results = await asyncio.gather(*(svc.stations() for _ in range(5)))
    assert all(len(r) == 2 for r in results)
    assert heos.calls == 1 and sonos.calls == 1


async def test_vendor_lists_are_sorted_before_keying_and_subtitle_is_used() -> None:
    heos = Src(
        "heos",
        [
            VendorStation(vendor="heos", name="Mix", ids={"mid": "b"}),
            VendorStation(vendor="heos", name="mix", ids={"mid": "a"}, subtitle="Your mix"),
        ],
    )
    svc = service({"heos": heos})
    items = await svc.stations()
    # Sorted by (name, ids) first, so "mix"/a gets the bare key deterministically.
    assert [i.content_ref.id for i in items] == ["mix", "mix-2"]
    assert svc.vendor_stations(items[0].content_ref)["heos"].ids == {"mid": "a"}
    assert items[0].subtitle == "Your mix" and items[1].subtitle == "Pandora station"
