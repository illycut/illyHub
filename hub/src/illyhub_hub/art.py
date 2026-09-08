"""Artwork proxy: fetch, cache, resize, blur, and colour-analyse album art.

Why the hub owns artwork (PRD §3.4, design system §2.2/§11):
- device-reported art varies wildly in resolution and is served from LAN hosts the PWA cannot
  fetch without CORS/mixed-content trouble;
- the design system needs a pre-blurred backdrop and an art-derived accent colour that has
  already passed a contrast check, and the client must never compute colour itself.

Layout on disk (``HUB_DATA_DIR/art/<key>/``)::

    meta.json                 source_url, service, content_id, timestamps, accent, error, bytes
    96.jpg 320.jpg 1080.jpg   square-fit variants (never upscaled past the source)
    backdrop.jpg              540px, blurred (60px-equivalent at 1080)

``register()`` is synchronous and cheap: it derives the cache key, writes ``meta.json`` for a new
key, schedules the fetch when a loop is running, and returns an :class:`ArtRef`. Decoding,
resizing and disk writes run in a worker thread. The accent is filled in asynchronously;
``on_accent`` lets the caller patch state once it is known.

Accent rules (design §2.2): ``accent`` is ``None`` and ``accent_is_safe`` is ``False`` when
extraction fails, when the artwork is effectively greyscale (a grey tint is pointless), or when
the clamped colour still fails the 4.5:1 contrast check. Clients fall back to ``bg-raised``.
"""

from __future__ import annotations

import asyncio
import colorsys
import contextlib
import functools
import hashlib
import io
import json
import os
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from PIL import Image, ImageFilter, ImageOps

from .logsetup import get_logger
from .state import ArtRef
from .tasks import cancel_all, spawn

log = get_logger("art")

# Decompression-bomb guard: 50 MP is far above any album cover (a 7000x7000 scan).
Image.MAX_IMAGE_PIXELS = 50_000_000

SIZES: dict[str, int] = {"96": 96, "320": 320, "1080": 1080, "backdrop": 540}
BACKDROP_BLUR_RADIUS = 30  # 60px at 1080 ≈ 30px at 540
JPEG_QUALITY = 85
BG_BASE = (0x10, 0x10, 0x14)
BG_RAISED = (0x1A, 0x1A, 0x20)
TEXT_PRIMARY = (0xF2, 0xF2, 0xF5)
ACCENT_BLEND = 0.40
MIN_CONTRAST = 4.5
MIN_SATURATION = 0.10  # below this the art is greyscale: no tint
ART_URL_PREFIX = "/api/art"
KEY_RE = re.compile(r"^[0-9a-f]{24}$")
PLACEHOLDER_KEY = "0" * 24  # what ArtRef.cache_key holds when the proxy is disabled
MAX_BODY_BYTES = 16 * 1024 * 1024
MAX_REDIRECTS = 3
ALLOWED_SCHEMES = ("http://", "https://")
ORPHAN_GRACE_S = 3600.0  # meta-less dirs older than this are deleted by the sweep

AccentCallback = Callable[[str, str | None, bool], Any]


# --------------------------------------------------------------------------------------
# Colour maths
# --------------------------------------------------------------------------------------


def _hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def _relative_luminance(rgb: tuple[int, int, int]) -> float:
    def channel(c: int) -> float:
        v = c / 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(fg: tuple[int, int, int], bg: tuple[int, int, int]) -> float:
    l1, l2 = sorted((_relative_luminance(fg), _relative_luminance(bg)), reverse=True)
    return (l1 + 0.05) / (l2 + 0.05)


def blend(
    top: tuple[int, int, int], bottom: tuple[int, int, int], alpha: float
) -> tuple[int, int, int]:
    return tuple(round(t * alpha + b * (1 - alpha)) for t, b in zip(top, bottom, strict=True))  # type: ignore[return-value]


def saturation(rgb: tuple[int, int, int]) -> float:
    return colorsys.rgb_to_hls(*(c / 255 for c in rgb))[2]


def clamp_accent(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    """Design system §2.2: lightness forced into 35–60 %, saturation capped at 70 %."""
    hue, light, sat = colorsys.rgb_to_hls(*(c / 255 for c in rgb))
    light = min(0.60, max(0.35, light))
    sat = min(0.70, sat)
    r, g, b = colorsys.hls_to_rgb(hue, light, sat)
    return round(r * 255), round(g * 255), round(b * 255)


def accent_is_safe(accent: tuple[int, int, int]) -> bool:
    """``text-primary`` over the accent tinted at 40 % on ``bg-base`` must clear 4.5:1."""
    return contrast_ratio(TEXT_PRIMARY, blend(accent, BG_BASE, ACCENT_BLEND)) >= MIN_CONTRAST


def dominant_color(img: Image.Image) -> tuple[int, int, int] | None:
    """Most frequent colour of a 64px thumbnail, ignoring near-black and near-white."""
    thumb = img.convert("RGB").copy()
    thumb.thumbnail((64, 64))
    quantized = thumb.quantize(colors=16, method=Image.Quantize.MEDIANCUT).convert("RGB")
    colors = quantized.getcolors(64 * 64) or []
    if not colors:
        return None
    ranked = sorted(colors, key=lambda c: c[0], reverse=True)
    for _count, rgb in ranked:
        if max(rgb) < 40 or min(rgb) > 215:
            continue
        return tuple(rgb)  # type: ignore[return-value]
    return tuple(ranked[0][1])  # type: ignore[return-value]


def analyse_accent(img: Image.Image) -> tuple[str | None, bool]:
    """``(accent_hex, accent_is_safe)``.

    ``(None, False)`` when extraction fails, when the dominant colour is effectively greyscale
    (saturation under 10 % before clamping), or when the clamped colour fails the contrast check.
    A safe accent is always returned with ``True``; the two never disagree.
    """
    try:
        raw = dominant_color(img)
    except Exception:  # noqa: BLE001 - Pillow quantize can fail on exotic modes
        log.debug("accent extraction failed", exc_info=True)
        return None, False
    if raw is None or saturation(raw) < MIN_SATURATION:
        return None, False
    accent = clamp_accent(raw)
    if not accent_is_safe(accent):
        return None, False
    return _hex(accent), True


# --------------------------------------------------------------------------------------
# Image variants
# --------------------------------------------------------------------------------------


def _to_jpeg(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=JPEG_QUALITY, optimize=True)
    return buf.getvalue()


def _flatten(img: Image.Image) -> Image.Image:
    """RGBA/LA/P-with-alpha composited onto ``bg-raised`` (not black) so transparent covers
    match the card surface."""
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, (*BG_RAISED, 255))
        return Image.alpha_composite(bg, rgba).convert("RGB")
    return img.convert("RGB")


def make_variants(orig: bytes) -> tuple[dict[str, bytes], tuple[str | None, bool]]:
    """Square-fit 96/320/1080 plus a blurred backdrop, never upscaled past the source's shorter
    side, and the accent analysis."""
    with Image.open(io.BytesIO(orig)) as img:
        img.load()
        base = _flatten(ImageOps.exif_transpose(img) or img)
        shorter = min(base.size)
        out: dict[str, bytes] = {}
        for name, px in SIZES.items():
            target = min(px, shorter)
            fitted = ImageOps.fit(base, (target, target), Image.Resampling.LANCZOS)
            if name == "backdrop":
                radius = BACKDROP_BLUR_RADIUS * target / SIZES["backdrop"]
                fitted = fitted.filter(ImageFilter.GaussianBlur(radius))
            out[name] = _to_jpeg(fitted)
        return out, analyse_accent(base)


@functools.lru_cache(maxsize=len(SIZES))
def placeholder(size: str) -> bytes:
    """A neutral ``bg-raised`` square (PNG, so the colour is exact) so the UI never renders a
    broken image. Cached per size: four PNGs for the life of the process."""
    px = SIZES.get(size, 320)
    buf = io.BytesIO()
    Image.new("RGB", (px, px), BG_RAISED).save(buf, "PNG", optimize=True)
    return buf.getvalue()


def synthetic_art(seed: str, px: int = 1080) -> bytes:
    """Deterministic gradient artwork for the protocol fakes (``fake://art/<seed>``).

    Built as a 1x256 gradient strip and resized, so it costs milliseconds. Runs in the same
    worker thread as :func:`make_variants`.
    """
    digest = hashlib.sha1(seed.encode()).digest()
    c1 = (digest[0], digest[1], digest[2])
    c2 = (digest[3], digest[4], digest[5])
    strip = Image.new("RGB", (1, 256))
    strip.putdata(
        [
            tuple(round(a * (1 - t / 255) + b * (t / 255)) for a, b in zip(c1, c2, strict=True))
            for t in range(256)
        ]
    )
    img = strip.resize((px, px), Image.Resampling.BILINEAR)
    # a darker block so the gradient reads as "album art" rather than a swatch
    block = Image.new("RGB", (px // 2, px // 2), tuple(c // 2 for c in c2))
    img.paste(block, (px // 4, px // 4))
    return _to_jpeg(img)


# --------------------------------------------------------------------------------------
# Resolver: which URL should we fetch?
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Thumbnail:
    url: str
    width: int
    height: int = 0


@dataclass(frozen=True)
class ArtHints:
    """Everything an adapter knows about the current artwork. Phase 3 supplies the service ids."""

    device_url: str | None = None
    service: str | None = None
    content_id: str | None = None
    tidal_cover_id: str | None = None  # Tidal cover UUID, e.g. "a1b2c3d4-…"
    thumbnails: Sequence[Thumbnail] | None = None  # ytmusicapi thumbnails


@dataclass(frozen=True)
class Resolved:
    source_url: str
    strategy: str


# 1080x1080 is the largest size Tidal's CDN actually serves for cover art. 1280x1280 was
# requested here and 403s for every image (verified against resources.tidal.com: 160, 320, 640,
# 750 and 1080 return 200; 80 and 1280 return 403), so every Tidal item fell back to a
# placeholder square. The hub downsizes to its own variants anyway, so 1080 loses nothing.
TIDAL_CDN = "https://resources.tidal.com/images/{path}/1080x1080.jpg"
YT_MIN_WIDTH = 640


def tidal_cover_url(cover_id: str) -> str:
    return TIDAL_CDN.format(path=cover_id.replace("-", "/"))


def resolve(hints: ArtHints) -> Resolved | None:
    """Service-direct art beats device-reported art. Returns ``None`` when nothing is known."""
    if hints.tidal_cover_id:
        return Resolved(tidal_cover_url(hints.tidal_cover_id), "tidal_cdn")
    if hints.thumbnails:
        big = [t for t in hints.thumbnails if t.width >= YT_MIN_WIDTH]
        pick = (
            min(big, key=lambda t: t.width) if big else max(hints.thumbnails, key=lambda t: t.width)
        )
        return Resolved(pick.url, "ytmusic_thumbnail")
    if hints.device_url:
        return Resolved(hints.device_url, "device")
    return None


# --------------------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------------------


@dataclass
class Meta:
    source_url: str
    service: str | None = None
    content_id: str | None = None
    registered_at: float = 0.0
    fetched_at: float | None = None
    last_attempt_at: float | None = None
    attempts: int = 0
    accent: str | None = None
    accent_is_safe: bool = False
    error: str | None = None
    content_type: str | None = None
    bytes: int = 0  # total size of the variants on disk

    def dump(self) -> dict[str, Any]:
        return self.__dict__.copy()

    @classmethod
    def load(cls, data: dict[str, Any]) -> Meta:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class Variant:
    """A servable artwork variant: a cached file, or placeholder bytes when ``fallback``."""

    etag: str
    path: Path | None = None
    body: bytes | None = None
    fallback: bool = False
    fallback_reason: str | None = None
    media_type: str = "image/jpeg"


@dataclass(frozen=True)
class SweepResult:
    expired: int = 0
    trimmed: int = 0
    orphans: int = 0

    @property
    def total(self) -> int:
        return self.expired + self.trimmed + self.orphans


@dataclass(frozen=True)
class CacheStats:
    entries: int
    bytes: int
    hits: int
    misses: int


class BodyTooLargeError(Exception):
    pass


def art_key(source_url: str, service: str | None = None, content_id: str | None = None) -> str:
    """Stable cache key: service + canonical id when both are known, else the source URL."""
    basis = f"{service}:{content_id}" if service and content_id else source_url
    return hashlib.sha1(basis.encode()).hexdigest()[:24]


def art_url(key: str, size: str | None = None) -> str:
    return f"{ART_URL_PREFIX}/{key}" + (f"?size={size}" if size else "")


def placeholder_ref() -> ArtRef:
    """The hub-relative ref handed out when the proxy is disabled: always renders a neutral
    square, never leaks a device URL to the client."""
    return ArtRef(url=art_url(PLACEHOLDER_KEY), cache_key=PLACEHOLDER_KEY)


def placeholder_variant(key: str, size: str, reason: str) -> Variant:
    return Variant(
        etag=f'"{key}-{size}-fallback"',
        body=placeholder(size),
        fallback=True,
        fallback_reason=reason,
        media_type="image/png",
    )


def etag_matches(header: str | None, etag: str) -> bool:
    """``If-None-Match`` handling: comma lists, weak ``W/`` prefixes, and ``*``."""
    if not header:
        return False
    for raw in header.split(","):
        candidate = raw.strip()
        if candidate == "*":
            return True
        if candidate.startswith("W/"):
            candidate = candidate[2:]
        if candidate == etag:
            return True
    return False


def _atomic_write(path: Path, data: bytes | str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    if isinstance(data, str):
        tmp.write_text(data)
    else:
        tmp.write_bytes(data)
    os.replace(tmp, path)


def _retrieve_exception(fut: asyncio.Future[Any]) -> None:
    """Done-callback so a future nobody awaited never logs "exception was never retrieved"."""
    if not fut.cancelled():
        fut.exception()


class ArtCache:
    """Disk-backed artwork cache with resize/blur variants and accent analysis."""

    def __init__(
        self,
        root: Path,
        *,
        ttl_days: float = 30.0,
        max_mb: float = 500.0,
        retry_after_s: float = 60.0,
        fetch_timeout_s: float = 5.0,
        clock: Callable[[], float] = time.time,
        transport: httpx.AsyncBaseTransport | None = None,
        on_accent: AccentCallback | None = None,
        eviction_interval_s: float = 86_400.0,
        allow_fake: bool = False,
        max_body_bytes: int = MAX_BODY_BYTES,
    ) -> None:
        self.root = root
        self.ttl_s = ttl_days * 86_400
        self.max_bytes = int(max_mb * 1024 * 1024)
        self.retry_after_s = retry_after_s
        self.fetch_timeout_s = fetch_timeout_s
        self.clock = clock
        self._transport = transport
        self.on_accent = on_accent
        self.eviction_interval_s = eviction_interval_s
        self.allow_fake = allow_fake
        self.max_body_bytes = max_body_bytes
        self._client: httpx.AsyncClient | None = None
        self._inflight: dict[str, asyncio.Future[Meta]] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self.hits = 0
        self.misses = 0

    # -- lifecycle ----------------------------------------------------------------------

    async def start(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._client = httpx.AsyncClient(
            transport=self._transport,
            timeout=self.fetch_timeout_s,
            follow_redirects=True,
            max_redirects=MAX_REDIRECTS,
        )
        spawn(self._sweep_loop(), self._tasks)

    async def stop(self) -> None:
        await cancel_all(self._tasks)
        for fut in list(self._inflight.values()):
            fut.cancel()
        self._inflight.clear()
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _sweep_loop(self) -> None:
        while True:
            try:
                result = await asyncio.to_thread(self.sweep)
                if result.total:
                    log.info(
                        "art cache swept",
                        extra={
                            "extra": {
                                "expired": result.expired,
                                "trimmed": result.trimmed,
                                "orphans": result.orphans,
                            }
                        },
                    )
            except Exception:  # noqa: BLE001 - the sweep must never kill the loop
                log.exception("art sweep failed")
            await asyncio.sleep(self.eviction_interval_s)

    # -- sweep: TTL, orphans, size budget --------------------------------------------------

    def _entries(self) -> list[Path]:
        if not self.root.exists():
            return []
        return [p for p in self.root.iterdir() if p.is_dir() and KEY_RE.match(p.name)]

    def _remove_entry(self, entry: Path) -> None:
        for f in entry.iterdir():
            f.unlink()
        entry.rmdir()

    def evict_expired(self) -> int:
        """Drop entries whose fetch (or registration, if never fetched) is older than the TTL.
        Keys with a fetch in flight are left alone."""
        now = self.clock()
        removed = 0
        for entry in self._entries():
            if entry.name in self._inflight:
                continue
            meta = self._read_meta(entry.name)
            if meta is None:
                continue
            stamp = meta.fetched_at or meta.registered_at
            if now - stamp > self.ttl_s:
                self._remove_entry(entry)
                removed += 1
        return removed

    def evict_orphans(self) -> int:
        """Directories without a readable ``meta.json`` (crashed mid-write) older than an hour."""
        now = self.clock()
        removed = 0
        for entry in self._entries():
            if entry.name in self._inflight or self._read_meta(entry.name) is not None:
                continue
            try:
                age = now - entry.stat().st_mtime
            except OSError:  # pragma: no cover - vanished between listing and stat
                continue
            if age > ORPHAN_GRACE_S:
                self._remove_entry(entry)
                removed += 1
        return removed

    def trim_to_budget(self) -> int:
        """LRU by ``fetched_at`` until the variants on disk fit ``max_bytes``."""
        sized: list[tuple[float, int, Path]] = []
        total = 0
        for entry in self._entries():
            meta = self._read_meta(entry.name)
            if meta is None or meta.fetched_at is None:
                continue
            total += meta.bytes
            sized.append((meta.fetched_at, meta.bytes, entry))
        removed = 0
        for _stamp, size, entry in sorted(sized):
            if total <= self.max_bytes:
                break
            if entry.name in self._inflight:
                continue
            self._remove_entry(entry)
            total -= size
            removed += 1
        return removed

    def sweep(self) -> SweepResult:
        return SweepResult(
            expired=self.evict_expired(),
            orphans=self.evict_orphans(),
            trimmed=self.trim_to_budget(),
        )

    def stats(self) -> CacheStats:
        entries = 0
        total = 0
        for entry in self._entries():
            meta = self._read_meta(entry.name)
            if meta is not None and meta.fetched_at is not None:
                entries += 1
                total += meta.bytes
        return CacheStats(entries=entries, bytes=total, hits=self.hits, misses=self.misses)

    # -- registration -------------------------------------------------------------------

    def register(
        self, source_url: str, *, service: str | None = None, content_id: str | None = None
    ) -> ArtRef:
        """Return an :class:`ArtRef` for ``source_url``; fetch in the background if new."""
        key = art_key(source_url, service, content_id)
        meta = self._read_meta(key)
        if meta is None:
            meta = Meta(
                source_url=source_url,
                service=service,
                content_id=content_id,
                registered_at=self.clock(),
            )
            self._write_meta(key, meta)
        if meta.fetched_at is None and key not in self._inflight:
            spawn(self.ensure(key), self._tasks)
        return ArtRef(
            url=art_url(key),
            cache_key=key,
            accent=meta.accent,
            accent_is_safe=meta.accent_is_safe,
        )

    # -- serving ------------------------------------------------------------------------

    def known(self, key: str) -> bool:
        return (self._dir(key) / "meta.json").exists()

    async def ensure(self, key: str) -> Meta | None:
        """Make sure ``key`` has been fetched (or retried); ``None`` when the key is unknown."""
        meta = self._read_meta(key)
        if meta is None:
            return None
        if meta.fetched_at is not None:
            return meta
        pending = self._inflight.get(key)
        if pending is not None:
            return await pending
        now = self.clock()
        if meta.last_attempt_at is not None and now - meta.last_attempt_at < self.retry_after_s:
            return meta
        fut: asyncio.Future[Meta] = asyncio.get_running_loop().create_future()
        fut.add_done_callback(_retrieve_exception)
        self._inflight[key] = fut
        try:
            meta = await self._fetch(key, meta)
        except asyncio.CancelledError:
            fut.cancel()
            raise
        except Exception as exc:  # pragma: no cover - _fetch handles its own failures
            fut.set_exception(exc)
            raise
        finally:
            self._inflight.pop(key, None)
        fut.set_result(meta)
        return meta

    async def variant(self, key: str, size: str) -> Variant | None:
        """The servable variant for ``size``; ``None`` for an unknown key; a fallback placeholder
        when the fetch failed. Raises ``ValueError`` for an unknown size."""
        if size not in SIZES:
            raise ValueError(f"unknown art size {size!r}")
        if key == PLACEHOLDER_KEY:
            return placeholder_variant(key, size, "placeholder_key")
        meta = await self.ensure(key)
        if meta is None:
            return None
        if meta.fetched_at is not None:
            etag = f'"{key}-{size}-{int(meta.fetched_at)}"'  # from meta: no file read needed
            path = self._dir(key) / f"{size}.jpg"
            if path.is_file():
                self.hits += 1
                return Variant(etag=etag, path=path)
        self.misses += 1
        log.warning(
            "art unavailable, serving placeholder",
            extra={"extra": {"key": key, "error": meta.error}},
        )
        return placeholder_variant(key, size, "upstream_error")

    # -- internals ----------------------------------------------------------------------

    def _dir(self, key: str) -> Path:
        return self.root / key

    def _read_meta(self, key: str) -> Meta | None:
        path = self._dir(key) / "meta.json"
        try:
            return Meta.load(json.loads(path.read_text()))
        except (OSError, ValueError, TypeError):
            return None

    def _write_meta(self, key: str, meta: Meta) -> None:
        d = self._dir(key)
        d.mkdir(parents=True, exist_ok=True)
        _atomic_write(d / "meta.json", json.dumps(meta.dump()))

    async def _fetch_bytes(self, url: str) -> tuple[bytes | None, str | None]:
        """Upstream bytes, or ``(None, None)`` for a ``fake://`` URL the worker will synthesise."""
        if url.startswith("fake://"):
            if not self.allow_fake:
                raise ValueError("fake:// art is only rendered when HUB_FAKE_DEVICES=1")
            return None, "image/jpeg"
        if not url.startswith(ALLOWED_SCHEMES):
            raise ValueError(f"refusing art URL scheme: {url[:16]!r}")
        if self._client is None:
            raise RuntimeError("ArtCache is not started")
        async with self._client.stream("GET", url) as resp:
            resp.raise_for_status()
            declared = resp.headers.get("content-length")
            if declared and int(declared) > self.max_body_bytes:
                raise BodyTooLargeError(f"content-length {declared} exceeds {self.max_body_bytes}")
            chunks: list[bytes] = []
            total = 0
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > self.max_body_bytes:
                    raise BodyTooLargeError(f"body exceeds {self.max_body_bytes} bytes")
                chunks.append(chunk)
            return b"".join(chunks), resp.headers.get("content-type")

    def _process_and_store(
        self, key: str, source_url: str, body: bytes | None
    ) -> tuple[int, tuple[str | None, bool]]:
        """Worker thread: synthesise if needed, decode, resize, write atomically. Returns the
        bytes written and the accent analysis."""
        if body is None:
            body = synthetic_art(source_url)
        variants, accent = make_variants(body)
        d = self._dir(key)
        d.mkdir(parents=True, exist_ok=True)  # eviction may have raced us; recreate
        written = 0
        for name, data in variants.items():
            _atomic_write(d / f"{name}.jpg", data)
            written += len(data)
        return written, accent

    async def _fetch(self, key: str, meta: Meta) -> Meta:
        meta.attempts += 1
        meta.last_attempt_at = self.clock()
        try:
            body, ctype = await self._fetch_bytes(meta.source_url)
            written, (accent, safe) = await asyncio.to_thread(
                self._process_and_store, key, meta.source_url, body
            )
        except Exception as exc:  # noqa: BLE001 - upstream, decode and disk failures are expected
            meta.error = f"{type(exc).__name__}: {exc}"[:200]
            with contextlib.suppress(OSError):
                self._write_meta(key, meta)
            log.warning(
                "art fetch failed",
                extra={"extra": {"key": key, "url": meta.source_url, "error": meta.error}},
            )
            return meta
        meta.fetched_at = self.clock()
        meta.error = None
        meta.content_type = ctype
        meta.bytes = written
        meta.accent, meta.accent_is_safe = accent, safe
        self._write_meta(key, meta)
        log.info(
            "art cached",
            extra={
                "extra": {"key": key, "accent": accent, "accent_is_safe": safe, "bytes": written}
            },
        )
        if self.on_accent is not None:
            try:
                self.on_accent(key, accent, safe)
            except Exception:  # noqa: BLE001
                log.exception("on_accent callback failed")
        return meta


@dataclass
class ArtHelper:
    """What adapters hold: an optional cache plus the resolver.

    Without a cache (``HUB_ART_PROXY=0``) every ref points at the hub's placeholder so the
    client never receives a device URL; the art payload stays hub-relative either way.
    """

    cache: ArtCache | None = None

    def ref(self, hints: ArtHints) -> ArtRef:
        found = resolve(hints)
        if found is None:
            return ArtRef()
        log.debug("art resolved", extra={"extra": {"strategy": found.strategy}})
        if self.cache is None:
            return placeholder_ref()
        return self.cache.register(
            found.source_url, service=hints.service, content_id=hints.content_id
        )
