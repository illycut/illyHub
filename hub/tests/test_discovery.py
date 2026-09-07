from __future__ import annotations

import asyncio

from illyhub_hub.config import Settings, StaticDevice
from illyhub_hub.discovery import (
    DiscoveryService,
    Registry,
    _Collector,
    build_msearch,
    parse_ssdp_response,
    ssdp_search,
    static_devices,
)

SONOS = (
    b"HTTP/1.1 200 OK\r\nCACHE-CONTROL: max-age=1800\r\nLOCATION: http://192.168.1.31:1400/xml/device_description.xml\r\n"
    b"ST: urn:schemas-upnp-org:device:ZonePlayer:1\r\nUSN: uuid:RINCON_ABC123::urn:schemas-upnp-org:device:ZonePlayer:1\r\n\r\n"
)
HEOS_LF_ONLY = (  # some stacks answer with bare LF line endings
    b"HTTP/1.1 200 OK\nLOCATION: http://192.168.1.20:60006/upnp/desc/aios_device/aios_device.xml\n"
    b"ST: urn:schemas-denon-com:device:ACT-Denon:1\nUSN: uuid:deadbeef-0000::urn:schemas-denon-com:device:ACT-Denon:1\n\n"
)
OTHER = b"HTTP/1.1 200 OK\r\nST: upnp:rootdevice\r\nUSN: uuid:x\r\n\r\n"


def test_msearch_packet_has_required_headers() -> None:
    pkt = build_msearch("urn:schemas-upnp-org:device:ZonePlayer:1").decode()
    assert pkt.startswith("M-SEARCH * HTTP/1.1\r\n")
    assert 'MAN: "ssdp:discover"' in pkt and "ST: urn:schemas-upnp-org:device:ZonePlayer:1" in pkt


def test_parse_sonos_and_heos_responses_with_either_line_ending() -> None:
    s = parse_ssdp_response(SONOS, ("192.168.1.31", 1900))
    assert s and s.vendor == "sonos" and s.id == "RINCON_ABC123" and s.ip == "192.168.1.31"
    h = parse_ssdp_response(HEOS_LF_ONLY, ("192.168.1.20", 1900))
    assert h and h.vendor == "heos" and h.id == "deadbeef-0000" and h.location.endswith(".xml")


def test_parse_ignores_other_devices_and_usn_less_ids_do_not_collide() -> None:
    assert parse_ssdp_response(OTHER, ("1.2.3.4", 1900)) is None
    a = parse_ssdp_response(
        b"HTTP/1.1 200 OK\r\nST: urn:schemas-upnp-org:device:ZonePlayer:1\r\nLOCATION: http://1.2.3.4:1400/a.xml\r\n\r\n",
        ("1.2.3.4", 1900),
    )
    b = parse_ssdp_response(
        b"HTTP/1.1 200 OK\r\nST: urn:schemas-upnp-org:device:ZonePlayer:1\r\nLOCATION: http://1.2.3.4:1401/b.xml\r\n\r\n",
        ("1.2.3.4", 1900),
    )
    c = parse_ssdp_response(
        b"HTTP/1.1 200 OK\r\nST: urn:schemas-upnp-org:device:ZonePlayer:1\r\n\r\n",
        ("1.2.3.4", 1900),
    )
    assert a and b and c
    assert a.id.startswith("sonos-1.2.3.4-1400-") and b.id.startswith("sonos-1.2.3.4-1401-")
    assert len({a.id, b.id, c.id}) == 3 and c.ip == "1.2.3.4"


def test_collector_dedupes_by_id() -> None:
    c = _Collector()
    c.datagram_received(SONOS, ("192.168.1.31", 1900))
    c.datagram_received(SONOS, ("192.168.1.31", 1900))
    c.datagram_received(OTHER, ("1.1.1.1", 1900))
    assert list(c.found) == ["RINCON_ABC123"]


async def test_ssdp_search_repeats_staggered_msearch(monkeypatch) -> None:
    sent: list[tuple[bytes, tuple[str, int]]] = []

    class FakeTransport:
        def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
            sent.append((data, addr))

        def close(self) -> None:
            pass

    async def fake_endpoint(factory, **_kw):
        proto = factory()
        proto.datagram_received(SONOS, ("192.168.1.31", 1900))
        return FakeTransport(), proto

    monkeypatch.setattr(asyncio.get_running_loop(), "create_datagram_endpoint", fake_endpoint)
    found = await ssdp_search(timeout_s=0, repeat=3, stagger_s=0)
    assert [d.id for d in found] == ["RINCON_ABC123"]
    assert len(sent) == 6 and all(addr == ("239.255.255.250", 1900) for _, addr in sent)


def test_static_devices_share_registry_shape() -> None:
    devs = static_devices([StaticDevice(id="heos-amp", vendor="heos", ip="10.0.0.5", name="Amp")])
    assert devs[0].source == "static" and devs[0].name == "Amp" and devs[0].vendor == "heos"


def test_registry_merge_reports_new_and_sorts() -> None:
    r = Registry()
    devs = static_devices(
        [StaticDevice(id="z", vendor="sonos", ip="1"), StaticDevice(id="a", vendor="heos", ip="2")]
    )
    assert r.merge(devs) == ["z", "a"] and r.merge(devs) == []
    assert [d.id for d in r.all()] == ["a", "z"] and [d.id for d in r.by_vendor("sonos")] == ["z"]
    assert r.get("a") is not None and r.get("nope") is None and r.dump()[0]["id"] == "a"


async def test_discovery_service_seeds_static_merges_ssdp_and_survives_errors() -> None:
    settings = Settings(
        _env_file=None, static_devices=[StaticDevice(id="amp", vendor="heos", ip="10.0.0.5")]
    )
    calls = {"n": 0}

    async def search():
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("multicast blocked")
        return [parse_ssdp_response(SONOS, ("192.168.1.31", 1900))]

    svc = DiscoveryService(settings, search=search)
    assert [d.id for d in svc.registry.all()] == ["amp"]  # static entries available before any SSDP
    assert await svc.discover_once() == []
    assert svc.last_error == "multicast blocked" and svc.last_run is not None
    assert await svc.discover_once() == ["RINCON_ABC123"]
    assert svc.last_error is None


async def test_discovery_service_periodic_loop_and_stop() -> None:
    settings = Settings(_env_file=None, discovery_interval_s=0.01)
    calls = {"n": 0}

    async def search():
        calls["n"] += 1
        return []

    svc = DiscoveryService(settings, search=search)
    await svc.start()
    await svc.start()  # idempotent
    await asyncio.sleep(0.05)
    await svc.stop()
    await svc.stop()
    assert calls["n"] >= 2
