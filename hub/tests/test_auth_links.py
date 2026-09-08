"""Verification-link normalisation for the device-code sign-in flows.

Tidal returns ``verificationUriComplete`` as ``link.tidal.com/ABC12`` with no scheme, because the
field is meant to be read aloud. Passing that to a client makes it a *relative* href, so the
browser resolves it against the page origin and the PWA's catch-all serves the app again. Seen on
the hub as "the account link cycles back to the hub IP" -- a broken-looking sign-in page that was
really a same-origin navigation.
"""

from __future__ import annotations

import pytest

from illyhub_hub.auth.links import absolute_verification_url


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # The bug: Tidal's real shape, scheme-less.
        ("link.tidal.com/ABC12", "https://link.tidal.com/ABC12"),
        ("link.tidal.com", "https://link.tidal.com"),
        # Already absolute: left exactly as it is.
        ("https://www.google.com/device", "https://www.google.com/device"),
        ("http://hub.example/device", "http://hub.example/device"),
        # Scheme-relative gets https rather than inheriting a plain-http page's scheme.
        ("//link.tidal.com/X", "https://link.tidal.com/X"),
        # A host with a port is a host, not a scheme. urlsplit reads "link.tidal.com" as a scheme
        # here because dots are legal in schemes, which is why this is matched explicitly.
        ("link.tidal.com:443/A", "https://link.tidal.com:443/A"),
        # Whitespace around the value is a service formatting artefact, not part of the URL.
        ("  link.tidal.com/ABC12  ", "https://link.tidal.com/ABC12"),
    ],
)
def test_scheme_is_added_only_where_it_is_missing(raw: str, expected: str) -> None:
    assert absolute_verification_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        None,
        # Never hand a client something it would put in an href and execute.
        "javascript:alert(1)",
        "data:text/html,<script>1</script>",
        "mailto:someone@example.com",
        "ftp://files.example/x",
        # A path is not a host: prefixing a scheme would invent "https://ABC12".
        "/ABC12",
        "ABC12",
        # A single label cannot be a public device-code host.
        "localhost/device",
    ],
)
def test_values_that_cannot_be_an_openable_web_url_fall_back(raw: str | None) -> None:
    assert absolute_verification_url(raw, "FALLBACK") == "FALLBACK"
    assert absolute_verification_url(raw) == ""  # default fallback is empty, never the raw value


def test_the_fallback_is_used_verbatim() -> None:
    """YouTube Music passes Google's documented device URL as the fallback."""
    google = "https://www.google.com/device"
    assert absolute_verification_url(None, google) == google
    assert absolute_verification_url("", google) == google
