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
# Experimental AirPlay bridge (PRD §3.5 PAN-4). {reason} is a plain sentence from the bridge.
# Used only when the bridge gives a fragment; a full-sentence reason is shown on its own
# (see airplay_unavailable()).
AIRPLAY_UNAVAILABLE = "Pandora Sync isn't available on the hub right now: {reason}"
AIRPLAY_OFF = "Pandora Sync is turned off on this hub."  # the env var stays in logs/docs
AIRPLAY_DAEMON = "The hub runs as a system daemon; Pandora Sync needs the logged-in user's session."
AIRPLAY_NO_MATCH = (
    "No AirPlay outputs match {rooms}. Rename them to match in the Music app on the hub Mac, "
    "or choose outputs there."
)
AIRPLAY_NEEDS_OUTPUT = "Pandora Sync needs at least one AirPlay output."
AIRPLAY_RESTORE_FAILED = "Pandora Sync ended, but the Mac's outputs could not be restored."
AIRPLAY_NOTHING_TO_RESTORE = "Pandora Sync ended; the Mac's outputs were left as they are."
PANDORA_SYNC_STARTED = "Pandora opened on the hub Mac. Press play there; the rooms follow."
PANDORA_SYNC_OUTPUTS_ONLY = (
    "Pandora Sync set the AirPlay outputs. Open pandora.com on the hub Mac and press play there."
)
PANDORA_SYNC_STOPPED = "Pandora Sync ended; the Mac's outputs are back to how they were."
PANDORA_SYNC_IDLE = "Pandora Sync isn't running."
PANDORA_SYNC_BLOCKED_BY_SYNC = "Stop Sync Play first."
SYNC_BLOCKED_BY_PANDORA = "Stop Pandora Sync first."
# Phase 8: self-update, queue, search, play mode.
UPDATE_DISABLED = "Updates are turned off on this hub."
UPDATE_RUNNING = "An update is already running."
UPDATE_STARTED = "Updating the hub. It restarts on its own when the update finishes."
UPDATE_NONE = "The hub is up to date."
UPDATE_STALE = "The update did not finish; run ops/update.sh by hand on the hub Mac."
UPDATE_CHECK_FAILED = "Couldn't check for updates right now."
UPDATE_REMOTE_REFUSED = (
    "The hub's git remote is not an https:// or ssh:// URL; updates are refused."
)
QUEUE_UNAVAILABLE = "{side} can't show its queue from this hub."
SEARCH_TOO_SHORT = "Type at least two characters to search."
PLAY_MODE_LOCKED = "Shuffle and repeat are off during Sync Play."
# Reason → template the app should show for a disabled side before the user taps.
REASON_TEMPLATES: dict[str, str] = {
    "unsupported": NOT_AVAILABLE_UNSUPPORTED,
    "not_linked": NOT_AVAILABLE_NOT_LINKED,
    "not_in_account": STATION_NOT_IN_ACCOUNT,
}


def airplay_unavailable(reason: str | None) -> str:
    """One sentence for a bridge failure. A full-sentence reason stands alone; a fragment is
    wrapped in :data:`AIRPLAY_UNAVAILABLE` so "right now" never appears twice."""
    text = (reason or "").strip()
    if not text:
        return AIRPLAY_UNAVAILABLE.format(reason="Music didn't answer.")
    if text.endswith((".", "?", "!")):
        return text
    return AIRPLAY_UNAVAILABLE.format(reason=text)


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
            "hub": {
                "update_disabled": UPDATE_DISABLED,
                "update_running": UPDATE_RUNNING,
                "update_started": UPDATE_STARTED,
                "update_none": UPDATE_NONE,
                "update_stale": UPDATE_STALE,
                "update_check_failed": UPDATE_CHECK_FAILED,
                "update_remote_refused": UPDATE_REMOTE_REFUSED,
                "queue_unavailable": QUEUE_UNAVAILABLE,
                "search_too_short": SEARCH_TOO_SHORT,
                "play_mode_locked": PLAY_MODE_LOCKED,
            },
            "airplay": {
                "airplay_unavailable": AIRPLAY_UNAVAILABLE,
                "airplay_off": AIRPLAY_OFF,
                "airplay_daemon": AIRPLAY_DAEMON,
                "airplay_no_match": AIRPLAY_NO_MATCH,
                "airplay_needs_output": AIRPLAY_NEEDS_OUTPUT,
                "airplay_restore_failed": AIRPLAY_RESTORE_FAILED,
                "airplay_nothing_to_restore": AIRPLAY_NOTHING_TO_RESTORE,
                "pandora_sync_started": PANDORA_SYNC_STARTED,
                "pandora_sync_outputs_only": PANDORA_SYNC_OUTPUTS_ONLY,
                "pandora_sync_stopped": PANDORA_SYNC_STOPPED,
                "pandora_sync_idle": PANDORA_SYNC_IDLE,
                "pandora_sync_blocked_by_sync": PANDORA_SYNC_BLOCKED_BY_SYNC,
                "sync_blocked_by_pandora": SYNC_BLOCKED_BY_PANDORA,
            },
        },
    }


def main() -> None:
    json.dump(export(), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":  # pragma: no cover - CLI entry
    main()
