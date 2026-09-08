"""Verification links for the device-code sign-in flows.

A device-code response gives the user a URL to visit on another device. Services do not agree on
whether that URL carries a scheme: Google returns ``https://www.google.com/device``, while Tidal
returns ``link.tidal.com/ABC12`` -- no scheme, because the field is meant to be read aloud or
printed on a TV. ``tidalapi`` passes it through unchanged.

Handing a scheme-less string to the client is a trap: ``<a href="link.tidal.com/ABC12">`` is a
**relative** URL, so the browser resolves it against the page's own origin and the PWA's
catch-all route serves the app again. Observed on the hub as "the link cycles back to the hub
IP" -- it looked like a broken sign-in page and was actually a same-origin navigation.

The API contract is that a verification URL is absolute and openable, so normalise here rather
than in each client.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

# Device-code URLs are always public web pages, so https is the right assumption when a service
# omits the scheme. Never http: that would downgrade a sign-in page carrying a user code.
DEFAULT_SCHEME = "https"
SAFE_SCHEMES = ("http", "https")

_ABSOLUTE_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*)://")
_HOST_PORT_RE = re.compile(r"^[^/:]+:\d+(?:/|$)")


def absolute_verification_url(value: str | None, fallback: str = "") -> str:
    """Return ``value`` as an absolute ``http(s)`` URL, or ``fallback`` when it cannot be one.

    ``link.tidal.com/ABC12`` becomes ``https://link.tidal.com/ABC12``; anything already absolute
    is left alone. A value with some other scheme (``javascript:``, ``data:``) is refused rather
    than passed to a client that would put it in an ``href``.
    """
    raw = (value or "").strip()
    if not raw:
        return fallback
    # A scheme-relative URL ("//host/path") is absolute enough for a browser; give it a scheme
    # so it does not inherit the page's, which on a plain-http hub would be http.
    if raw.startswith("//"):
        return f"{DEFAULT_SCHEME}:{raw}"
    # Do not use urlsplit's scheme here: dots are legal in a scheme, so it reads
    # "link.tidal.com:443/A" as scheme "link.tidal.com" and the value looks absolute when it is
    # really a host with a port. Match the "scheme://" form explicitly instead.
    absolute = _ABSOLUTE_RE.match(raw)
    if absolute:
        return raw if absolute.group(1).lower() in SAFE_SCHEMES else fallback
    # A colon before the first slash and not a ":port" means some other scheme entirely
    # (javascript:, data:, mailto:), which must never reach a client's href.
    if ":" in raw.split("/", 1)[0] and not _HOST_PORT_RE.match(raw):
        return fallback
    # A leading single slash means the value is a path, not a host. Prefixing a scheme would
    # invent a hostname out of it ("/ABC12" -> "https://ABC12"), so refuse instead.
    if raw.startswith("/"):
        return fallback
    # No scheme. urlsplit puts everything in `path`, so "link.tidal.com/ABC12" arrives with an
    # empty netloc; prefixing the scheme is what turns it into a real URL.
    candidate = f"{DEFAULT_SCHEME}://{raw}"
    host = urlsplit(candidate).netloc.split(":", 1)[0]
    # A device-code URL is always a public web page, so the host must look like a domain. This
    # keeps a stray code or word from being served up to a client as a link.
    if "." not in host or host.startswith(".") or host.endswith("."):
        return fallback
    return candidate
