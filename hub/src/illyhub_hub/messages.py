"""User-facing copy the hub owns, in one place, exported for the app.

The router's error messages are the single source of copy (design system §10: facts and fixes,
labels never raw ids). The app reads the same tables so its copy tests compare against the hub,
not against docs prose:

    uv run python -m illyhub_hub.messages > ../app/src/lib/hub/messages.json

or ``GET /api/meta/messages`` at runtime. ``Availability.reasons`` (``unsupported`` |
``not_linked`` | ``not_in_account``) tells the app which template applies *before* a tap.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Literal

from .state import SERVICE_LABEL, VENDOR_LABEL, service_label, vendor_label

Reason = Literal["unsupported", "not_linked", "not_in_account"]
REASONS: tuple[str, ...] = ("unsupported", "not_linked", "not_in_account")

VENDOR_APP: dict[str, str] = {"heos": "HEOS app", "sonos": "Sonos app"}

# Services an ecosystem cannot play at all from the hub, regardless of account linkage. HEOS has
# no YouTube Music source (PRD §3.1 LIB-5; docs/spikes/ytmusic-heos.md).
UNSUPPORTED_ON_VENDOR: frozenset[tuple[str, str]] = frozenset({("heos", "ytmusic")})

# Templates. Placeholders: {service} (label), {vendor} (label), {vendor_app}, {side} (room name).
NOT_AVAILABLE_UNSUPPORTED = "{service} isn't available on {vendor}."
NOT_AVAILABLE_NOT_LINKED = "{side} can't play {service}; link it in the {vendor_app}."
STATION_NOT_IN_ACCOUNT = (
    "That station isn't in the {vendor} {service} account, so {side} can't play it."
)
STATION_AUTH_FAULT = "{side} can't reach {service} right now; sign in again in the {vendor_app}."
PANDORA_CONCURRENT_MSG = "Pandora usually allows one stream per account; the other room may pause."
SYNC_UNSUPPORTED_CONTENT = "Sync Play works with Tidal content."
# Reason → template the app should show for a disabled side before the user taps.
REASON_TEMPLATES: dict[str, str] = {
    "unsupported": NOT_AVAILABLE_UNSUPPORTED,
    "not_linked": NOT_AVAILABLE_NOT_LINKED,
    "not_in_account": STATION_NOT_IN_ACCOUNT,
}


def vendor_app(vendor: str) -> str:
    return VENDOR_APP.get(vendor, f"{vendor_label(vendor)} app")


def not_available_message(reason: str, *, service: str, vendor: str, side: str) -> str:
    """Render a reason for one side with labels (never raw ids)."""
    return REASON_TEMPLATES[reason].format(
        service=service_label(service),
        vendor=vendor_label(vendor),
        vendor_app=vendor_app(vendor),
        side=side,
    )


def export() -> dict[str, Any]:
    """The machine-readable copy bundle (also served at ``GET /api/meta/messages``)."""
    return {
        "service_label": dict(SERVICE_LABEL),
        "vendor_label": {k: v for k, v in VENDOR_LABEL.items() if k in ("heos", "sonos")},
        "vendor_app": dict(VENDOR_APP),
        "unsupported_on_vendor": sorted([v, s] for v, s in UNSUPPORTED_ON_VENDOR),
        "reasons": list(REASONS),
        "templates": {
            "not_available": {
                "unsupported": NOT_AVAILABLE_UNSUPPORTED,
                "not_linked": NOT_AVAILABLE_NOT_LINKED,
                "not_in_account": STATION_NOT_IN_ACCOUNT,
                "auth_fault": STATION_AUTH_FAULT,
            },
            "pandora_concurrent": PANDORA_CONCURRENT_MSG,
            "sync_unsupported_content": SYNC_UNSUPPORTED_CONTENT,
        },
    }


def main() -> None:
    json.dump(export(), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":  # pragma: no cover - CLI entry
    main()
