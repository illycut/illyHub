"""Art proxy: cache, variants, accent maths, resolver, sweep, API route, state patching."""

from __future__ import annotations

import asyncio
import gc
import io
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from PIL import Image

from illyhub_hub import art as artmod
from illyhub_hub.api import build_runtime, create_app
from illyhub_hub.art import (
    PLACEHOLDER_KEY,
    SIZES,
    ArtCache,
    ArtHelper,
    ArtHints,
    Thumbnail,
    accent_is_safe,
    analyse_accent,
    art_key,
    clamp_accent,
    contrast_ratio,
    dominant_color,
    etag_matches,
    make_variants,
    placeholder,
    placeholder_ref,
    resolve,
    synthetic_art,
    tidal_cover_url,
)
from illyhub_hub.config import Settings
from illyhub_hub.state import ArtRef, NowPlaying, Player, StateStore


def png(color: tuple[int, int, int], size: tuple[int, int] = (1200, 1200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


RED = png((220, 30, 30))
SMALL = png((30, 120, 220), (300, 200))
GREY = png((128, 128, 128))
BIG_BODY = b"\xff" * (64 * 1024)


def upstream(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/ok.png":
        return httpx.Response(200, content=RED, headers={"content-type": "image/png"})
    if path == "/small.png":
        return httpx.Response(200, content=SMALL, headers={"content-type": "image/png"})
    if path == "/grey.png":
        return httpx.Response(200, content=GREY, headers={"content-type": "image/png"})
    if path == "/notimage":
        return httpx.Response(
            200, content=b"<html>nope</html>", headers={"content-type": "text/html"}
        )
    if path == "/declared-big":
        return httpx.Response(200, content=b"x", headers={"content-length": str(10**9)})
    if path == "/streamed-big":
        return httpx.Response(200, content=BIG_BODY, headers={"content-type": "image/png"})
    return httpx.Response(404)


class Clock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def make_cache(tmp_path: Path, **kw) -> tuple[ArtCache, Clock]:
    clock = Clock()
    opts = dict(
        clock=clock,
        transport=httpx.MockTransport(upstream),
        retry_after_s=60,
        eviction_interval_s=3600,
        allow_fake=True,
    )
    opts.update(kw)
    return ArtCache(tmp_path / "art", **opts), clock


@pytest.fixture
async def cache(tmp_path: Path) -> AsyncIterator[tuple[ArtCache, Clock]]:
    c, clock = make_cache(tmp_path)
    await c.start()
    try:
        yield c, clock
    finally:
        await c.stop()


def read_variant(v) -> Image.Image:
    data = v.body if v.body is not None else v.path.read_bytes()
    return Image.open(io.BytesIO(data))


# -- keys and refs ------------------------------------------------------------------------


def test_key_prefers_service_and_content_id() -> None:
    assert art_key("http://a/x.jpg", "tidal", "123") == art_key("http://b/y.jpg", "tidal", "123")
    assert art_key("http://a/x.jpg") != art_key("http://b/y.jpg")
    assert art_key("http://a/x.jpg", "tidal", None) == art_key("http://a/x.jpg")
    assert artmod.KEY_RE.match(art_key("anything")) and artmod.KEY_RE.match(PLACEHOLDER_KEY)


async def test_register_returns_ref_and_fetches_variants(cache) -> None:
    c, _clock = cache
    ref = c.register("https://art.example/ok.png", service="tidal", content_id="42")
    assert ref.cache_key and ref.url == f"/api/art/{ref.cache_key}"
    assert ref.accent is None  # not analysed yet
    meta = await c.ensure(ref.cache_key)
    assert meta is not None and meta.fetched_at is not None and meta.error is None
    assert meta.bytes > 0 and not (c.root / ref.cache_key / "orig").exists()
    for size, px in SIZES.items():
        v = await c.variant(ref.cache_key, size)
        assert v is not None and not v.fallback and v.path is not None
        with read_variant(v) as img:
            assert img.size == (px, px)
    assert c.hits == len(SIZES) and c.misses == 0
    again = c.register("https://art.example/ok.png", service="tidal", content_id="42")
    assert again.accent and again.accent_is_safe is True
    stats = c.stats()
    assert stats.entries == 1 and stats.bytes == meta.bytes and stats.hits == len(SIZES)


async def test_variants_never_upscale_past_source(cache) -> None:
    c, _ = cache
    ref = c.register("https://art.example/small.png")  # 300x200 source
    for size, px in SIZES.items():
        with read_variant(await c.variant(ref.cache_key, size)) as img:
            assert img.size == (min(px, 200), min(px, 200))


async def test_etag_from_meta_and_matching(cache) -> None:
    c, _ = cache
    ref = c.register("https://art.example/ok.png")
    a = await c.variant(ref.cache_key, "320")
    b = await c.variant(ref.cache_key, "320")
    assert a.etag == b.etag and a.etag != (await c.variant(ref.cache_key, "96")).etag
    assert etag_matches(a.etag, a.etag)
    assert etag_matches(f"W/{a.etag}", a.etag)
    assert etag_matches(f'"other", {a.etag}', a.etag)
    assert etag_matches("*", a.etag)
    assert not etag_matches(None, a.etag) and not etag_matches('"nope"', a.etag)


async def test_unknown_key_is_none_and_bad_size_raises(cache) -> None:
    c, _ = cache
    assert await c.variant("0" * 23 + "f", "320") is None
    with pytest.raises(ValueError):
        await c.variant(PLACEHOLDER_KEY, "9999")
    v = await c.variant(PLACEHOLDER_KEY, "96")
    assert v.fallback and v.fallback_reason == "placeholder_key"


async def test_upstream_failure_serves_placeholder_and_retries_after_window(cache) -> None:
    c, clock = cache
    ref = c.register("https://art.example/missing.png")
    v = await c.variant(ref.cache_key, "320")
    assert v is not None and v.fallback and v.media_type == "image/png" and c.misses == 1
    with read_variant(v) as img:
        assert img.size == (320, 320) and img.getpixel((10, 10)) == artmod.BG_RAISED
    meta = await c.ensure(ref.cache_key)
    assert meta is not None and meta.attempts == 1 and "404" in (meta.error or "")
    await c.ensure(ref.cache_key)
    assert (await c.ensure(ref.cache_key)).attempts == 1  # inside the retry window
    clock.t += 61
    assert (await c.ensure(ref.cache_key)).attempts == 2


async def test_non_image_body_is_a_failure(cache) -> None:
    c, _ = cache
    ref = c.register("https://art.example/notimage")
    assert (await c.variant(ref.cache_key, "96")).fallback


async def test_oversized_bodies_are_refused(tmp_path: Path) -> None:
    c, _ = make_cache(tmp_path, max_body_bytes=16 * 1024)
    await c.start()
    try:
        declared = c.register("https://art.example/declared-big")
        streamed = c.register("https://art.example/streamed-big")
        for ref in (declared, streamed):
            v = await c.variant(ref.cache_key, "96")
            assert v.fallback
            assert "BodyTooLargeError" in (await c.ensure(ref.cache_key)).error
    finally:
        await c.stop()


async def test_scheme_allowlist_and_fake_gate(tmp_path: Path) -> None:
    c, _ = make_cache(tmp_path, allow_fake=False)
    await c.start()
    try:
        for url in ("ftp://art.example/x.jpg", "file:///etc/passwd", "fake://art/signal"):
            ref = c.register(url)
            assert (await c.variant(ref.cache_key, "96")).fallback
            assert "ValueError" in (await c.ensure(ref.cache_key)).error
    finally:
        await c.stop()


async def test_synthetic_fake_art_when_enabled(cache) -> None:
    c, _ = cache
    ref = c.register("fake://art/signal")
    meta = await c.ensure(ref.cache_key)
    assert meta is not None and meta.fetched_at and meta.accent
    assert synthetic_art("a") != synthetic_art("b") and synthetic_art("a") == synthetic_art("a")


def test_register_with_no_running_loop_writes_meta_only(tmp_path: Path) -> None:
    c = ArtCache(tmp_path / "art")
    ref = c.register("https://art.example/ok.png")
    assert c.known(ref.cache_key)
    assert c._read_meta(ref.cache_key).fetched_at is None


# -- sweep: TTL, in-flight safety, orphans, budget ------------------------------------------


async def test_ttl_eviction_with_fake_clock(cache) -> None:
    c, clock = cache
    ref = c.register("https://art.example/ok.png")
    await c.ensure(ref.cache_key)
    stale = c.register("https://art.example/missing.png")  # never fetched; ages from registration
    (c.root / ".DS_Store").write_bytes(b"junk")  # Finder noise must be ignored
    clock.t += 29 * 86_400
    assert c.evict_expired() == 0
    clock.t += 2 * 86_400
    assert c.sweep().expired == 2
    assert not c.known(ref.cache_key) and not c.known(stale.cache_key)
    assert await c.variant(ref.cache_key, "320") is None


async def test_eviction_skips_in_flight_fetch(tmp_path: Path) -> None:
    gate = asyncio.Event()

    async def slow(request: httpx.Request) -> httpx.Response:
        await gate.wait()
        return httpx.Response(200, content=RED, headers={"content-type": "image/png"})

    c, clock = make_cache(tmp_path, transport=httpx.MockTransport(slow))
    await c.start()
    try:
        ref = c.register("https://art.example/ok.png")
        task = asyncio.create_task(c.ensure(ref.cache_key))
        await asyncio.sleep(0.01)
        assert ref.cache_key in c._inflight
        clock.t += 31 * 86_400  # would be expired by TTL
        assert c.evict_expired() == 0 and c.known(ref.cache_key)
        gate.set()
        meta = await task
        assert meta.fetched_at is not None
        assert not (await c.variant(ref.cache_key, "320")).fallback
    finally:
        await c.stop()


async def test_stop_during_in_flight_fetch_leaves_no_unretrieved_future(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    gate = asyncio.Event()

    async def slow(request: httpx.Request) -> httpx.Response:
        await gate.wait()
        return httpx.Response(200, content=RED)

    c, _ = make_cache(tmp_path, transport=httpx.MockTransport(slow))
    await c.start()
    ref = c.register("https://art.example/ok.png")  # spawns the fetch
    await asyncio.sleep(0.01)
    fut = c._inflight[ref.cache_key]
    with caplog.at_level(logging.ERROR, logger="asyncio"):
        await c.stop()
        assert fut.cancelled()
        assert not c._inflight
        del fut
        gc.collect()
        await asyncio.sleep(0)
    assert "never retrieved" not in caplog.text


async def test_orphan_dirs_are_removed_after_grace(cache) -> None:
    c, clock = cache
    orphan = c.root / ("a" * 24)
    orphan.mkdir()
    (orphan / "320.jpg").write_bytes(b"x")
    old = clock() - 7200
    os.utime(orphan, (old, old))
    fresh = c.root / ("b" * 24)
    fresh.mkdir()
    os.utime(fresh, (clock() - 60, clock() - 60))
    result = c.sweep()
    assert result.orphans == 1 and not orphan.exists() and fresh.exists()


async def test_trim_to_budget_is_lru_by_fetched_at(tmp_path: Path) -> None:
    c, clock = make_cache(tmp_path)
    await c.start()
    try:
        refs = []
        for i in range(3):
            refs.append(c.register(f"https://art.example/ok.png?n={i}"))
            await c.ensure(refs[-1].cache_key)
            clock.t += 10
        per_entry = c._read_meta(refs[0].cache_key).bytes
        c.max_bytes = per_entry * 2 + 1  # room for two
        result = c.sweep()
        assert result.trimmed == 1
        assert not c.known(refs[0].cache_key)  # oldest went first
        assert c.known(refs[1].cache_key) and c.known(refs[2].cache_key)
        assert c.stats().entries == 2
    finally:
        await c.stop()


# -- accent maths -------------------------------------------------------------------------


def test_pure_red_is_clamped_to_design_limits() -> None:
    import colorsys

    accent = clamp_accent((255, 0, 0))
    _h, light, sat = colorsys.rgb_to_hls(*(c / 255 for c in accent))
    assert 0.35 - 0.01 <= light <= 0.60 + 0.01 and sat <= 0.70 + 0.01  # 8-bit rounding
    assert accent_is_safe(accent)


def test_dark_navy_lightness_is_raised() -> None:
    import colorsys

    accent = clamp_accent((10, 15, 60))
    assert colorsys.rgb_to_hls(*(c / 255 for c in accent))[1] >= 0.35 - 1e-6


def test_white_fails_contrast_until_clamped() -> None:
    assert accent_is_safe((255, 255, 255)) is False  # 4.46:1 over the 40 % tint
    assert accent_is_safe(clamp_accent((255, 255, 255))) is True
    assert contrast_ratio((0xF2, 0xF2, 0xF5), (0x10, 0x10, 0x14)) > 14


def test_dominant_color_ignores_near_black_and_white() -> None:
    img = Image.new("RGB", (64, 64), (250, 250, 250))
    img.paste(Image.new("RGB", (64, 20), (40, 120, 200)), (0, 0))
    img.paste(Image.new("RGB", (64, 10), (5, 5, 5)), (0, 54))
    picked = dominant_color(img)
    assert picked is not None and picked[2] > picked[0]  # the blue band, not white or black
    assert dominant_color(Image.new("RGB", (8, 8), (255, 255, 255))) == (255, 255, 255)


def test_analyse_accent_falls_back_for_greyscale_black_white_and_failures() -> None:
    for color in ((128, 128, 128), (0, 0, 0), (255, 255, 255), (120, 118, 124)):
        assert analyse_accent(Image.new("RGB", (32, 32), color)) == (None, False)
    hex_accent, safe = analyse_accent(Image.new("RGB", (32, 32), (40, 120, 200)))
    assert hex_accent and safe is True

    class Broken:
        def convert(self, _mode: str) -> Image.Image:
            raise OSError("cannot decode")

    assert analyse_accent(Broken()) == (None, False)  # type: ignore[arg-type]


def test_make_variants_and_placeholder_shapes() -> None:
    variants, (accent, safe) = make_variants(RED)
    assert set(variants) == set(SIZES) and accent and safe
    with Image.open(io.BytesIO(variants["backdrop"])) as img:
        assert img.size == (540, 540)
    with Image.open(io.BytesIO(placeholder("1080"))) as img:
        assert img.size == (1080, 1080)
    assert placeholder("1080") is placeholder("1080")  # lru-cached bytes
    _grey_variants, grey_accent = make_variants(GREY)
    assert grey_accent == (None, False)


def test_rgba_is_composited_onto_bg_raised() -> None:
    buf = io.BytesIO()
    Image.new("RGBA", (400, 400), (0, 0, 0, 0)).save(buf, "PNG")  # fully transparent
    variants, _ = make_variants(buf.getvalue())
    with Image.open(io.BytesIO(variants["96"])) as img:
        r, g, b = img.getpixel((48, 48))
        assert abs(r - 0x1A) <= 3 and abs(g - 0x1A) <= 3 and abs(b - 0x20) <= 3


# -- resolver -----------------------------------------------------------------------------


def test_resolver_strategy_table() -> None:
    cover = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
    r = resolve(ArtHints(device_url="http://dev/art.jpg", tidal_cover_id=cover))
    assert r and r.strategy == "tidal_cdn"
    assert r.source_url == tidal_cover_url(cover)
    # 1080 is the largest size Tidal's CDN serves; 1280 403s for every image.
    assert "/a1b2c3d4/e5f6/7890/abcd/ef1234567890/1080x1080.jpg" in r.source_url

    thumbs = [Thumbnail("s", 120), Thumbnail("m", 544), Thumbnail("l", 800), Thumbnail("xl", 1200)]
    r = resolve(ArtHints(device_url="http://dev/art.jpg", thumbnails=thumbs))
    assert r and r.strategy == "ytmusic_thumbnail" and r.source_url == "l"
    r = resolve(ArtHints(thumbnails=thumbs[:2]))
    assert r and r.source_url == "m"  # nothing >= 640: largest available

    r = resolve(ArtHints(device_url="http://dev/art.jpg"))
    assert r and r.strategy == "device"
    assert resolve(ArtHints()) is None


def test_helper_without_cache_serves_hub_placeholder_not_device_url() -> None:
    helper = ArtHelper()
    ref = helper.ref(ArtHints(device_url="http://192.168.1.50:1400/getaa?x"))
    assert ref == placeholder_ref() and ref.url == f"/api/art/{PLACEHOLDER_KEY}"
    assert "192.168" not in (ref.url or "")
    assert helper.ref(ArtHints()) == ArtRef()


# -- store patching -----------------------------------------------------------------------


async def test_accent_is_patched_into_now_playing(tmp_path: Path) -> None:
    store = StateStore()
    c, _ = make_cache(tmp_path, on_accent=store.update_art_accent)
    await c.start()
    try:
        store.upsert_player(Player(id="k", name="Kitchen", vendor="sonos"))
        store.upsert_player(Player(id="other", name="Den", vendor="heos"))
        helper = ArtHelper(c)
        ref = helper.ref(ArtHints(device_url="https://art.example/ok.png", service="tidal"))
        store.set_now_playing("sonos:k", NowPlaying(title="x", art=ref))
        store.set_now_playing(
            "heos:other", NowPlaying(title="y", art=ArtRef(url="/api/art/zzz", cache_key="zzz"))
        )
        assert store.state.now_playing["sonos:k"].art.accent is None
        await c.ensure(ref.cache_key)
        patched = store.state.now_playing["sonos:k"].art
        assert patched.accent and patched.accent_is_safe is True
        assert store.state.now_playing["heos:other"].art.accent is None
        v = store.state.version
        store.update_art_accent(ref.cache_key, patched.accent, True)  # idempotent
        assert store.state.version == v
    finally:
        await c.stop()


# -- API route ----------------------------------------------------------------------------


@pytest.fixture
async def client(settings: Settings):
    app = create_app(
        settings,
        runtime_factory=lambda s: build_runtime(s, art_transport=httpx.MockTransport(upstream)),
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as c:
            yield c, app.state.runtime


async def test_art_route_serves_cached_variants_and_etag(client) -> None:
    c, rt = client
    ref = rt.art.register("https://art.example/ok.png", service="tidal", content_id="7")
    r = await c.get(ref.url, params={"size": "96"})
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert "immutable" in r.headers["cache-control"] and r.headers["etag"]
    with Image.open(io.BytesIO(r.content)) as img:
        assert img.size == (96, 96)
    etag = r.headers["etag"]
    for header in (etag, f"W/{etag}", f'"stale", {etag}'):
        r304 = await c.get(ref.url, params={"size": "96"}, headers={"if-none-match": header})
        assert r304.status_code == 304 and "immutable" in r304.headers["cache-control"]
    assert (await c.get(ref.url)).status_code == 200  # default size 320
    r = await c.get("/api/art/" + "f" * 24)
    assert r.status_code == 404 and r.json()["code"] == "unknown_art" and r.json()["message"]
    r = await c.get("/api/art/not-a-key")
    assert r.status_code == 400 and r.json()["code"] == "invalid_key"
    r = await c.get(ref.url, params={"size": "77"})
    assert r.status_code == 400 and r.json()["code"] == "invalid_size"


async def test_art_route_fallback_is_200_png_with_header(client) -> None:
    c, rt = client
    ref = rt.art.register("https://art.example/missing.png")
    r = await c.get(ref.url, params={"size": "320"})
    assert r.status_code == 200 and r.headers["x-art-fallback"] == "upstream_error"
    assert r.headers["content-type"] == "image/png" and r.headers["cache-control"] == "no-store"
    assert "etag" not in r.headers


async def test_fake_now_playing_uses_proxy_and_gets_accent(client) -> None:
    c, rt = client
    np = next(iter(rt.store.state.now_playing.values()))
    assert np.art.url and np.art.url.startswith("/api/art/") and np.art.cache_key
    await rt.art.ensure(np.art.cache_key)
    r = await c.get(np.art.url, params={"size": "backdrop"})
    assert r.status_code == 200
    with Image.open(io.BytesIO(r.content)) as img:
        assert img.size == (540, 540)
    assert any(n.art.accent for n in rt.store.state.now_playing.values())
    health = (await c.get("/api/health")).json()
    assert health["art_cache"]["entries"] >= 1 and health["art_cache"]["bytes"] > 0
    assert health["static"] == {"fonts": False, "app": False}


async def test_art_proxy_disabled_serves_placeholders_not_device_urls(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        fake_devices=True,
        art_proxy=False,
        data_dir=str(tmp_path),
        app_dir=str(tmp_path / "x"),
        fonts_dir=str(tmp_path / "y"),
        position_poll_s=0.02,
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        rt = app.state.runtime
        assert rt.art is None
        np = next(iter(rt.store.state.now_playing.values()))
        assert np.art.url == f"/api/art/{PLACEHOLDER_KEY}" and "fake://" not in np.art.url
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as c:
            r = await c.get(np.art.url, params={"size": "96"})
            assert r.status_code == 200 and r.headers["x-art-fallback"] == "proxy_disabled"
            assert (await c.get("/api/health")).json()["art_cache"] is None
