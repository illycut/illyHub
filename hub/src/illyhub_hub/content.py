"""Content references and browse item shapes shared by the services, adapters and API.

"Browse once, play anywhere" (PRD review §2.1): the hub browses a streaming service through the
service's own API and plays on each ecosystem by constructing that device's playable reference
from the canonical service id. A :class:`ContentRef` is that canonical id; :class:`BrowseItem`
is what the client renders; :class:`Availability` says which sides can play it.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from .state import ArtRef, ContentKind, ContentRef, Service, service_label

__all__ = [
    "Availability",
    "NeedsClientConfigError",
    "BrowseItem",
    "BrowsePage",
    "Container",
    "ContentKind",
    "ContentNotFoundError",
    "ContentRef",
    "NeedsLinkError",
    "Service",
]


class Availability(BaseModel):
    """Which ecosystems can play a service's content, and why not when they cannot.

    ``reasons[vendor]`` is ``null`` when available, else one of ``unsupported`` (the vendor cannot
    play the service at all), ``not_linked`` (the vendor app lacks the service link) or
    ``not_in_account`` (Pandora: the station is not in that vendor's account). The router's
    messages remain the copy of record; the app uses the reason to pick the sentence before a
    tap (``GET /api/meta/messages``)."""

    heos: bool = False
    sonos: bool = False
    reasons: dict[str, str | None] = Field(
        default_factory=lambda: {"heos": None, "sonos": None},
        description="vendor -> null when available, else unsupported | not_linked | not_in_account",
    )

    @classmethod
    def build(
        cls, service: str, linked: dict[str, bool], *, present: dict[str, bool] | None = None
    ) -> Availability:
        """Derive flags and reasons for ``service``: unsupported vendors first, then link state,
        then (stations) presence in the vendor account."""
        from .messages import UNSUPPORTED_ON_VENDOR  # local import: messages imports state only

        flags: dict[str, bool] = {}
        reasons: dict[str, str | None] = {}
        for vendor in ("heos", "sonos"):
            if (vendor, service) in UNSUPPORTED_ON_VENDOR:
                flags[vendor], reasons[vendor] = False, "unsupported"
            elif not linked.get(vendor, False):
                flags[vendor], reasons[vendor] = False, "not_linked"
            elif present is not None and not present.get(vendor, False):
                flags[vendor], reasons[vendor] = False, "not_in_account"
            else:
                flags[vendor], reasons[vendor] = True, None
        return cls(heos=flags["heos"], sonos=flags["sonos"], reasons=reasons)


class BrowseItem(BaseModel):
    content_ref: ContentRef
    title: str
    subtitle: str | None = None
    art: ArtRef = Field(default_factory=ArtRef)
    duration_ms: int | None = None
    track_count: int | None = None
    availability: Availability = Field(default_factory=Availability)
    # Track-only fields, so a client can start an album at a track and show the number.
    index: int | None = None
    album_id: str | None = None
    artist: str | None = None
    album: str | None = None


class BrowsePage(BaseModel):
    items: list[BrowseItem]
    offset: int = 0
    limit: int = 50
    total: int | None = None
    next_offset: int | None = None
    # Per-ecosystem sources only (Pandora stations): true = that vendor's browse succeeded,
    # false = not linked / auth fault there, null = vendor absent or its browse errored.
    linked: dict[str, bool | None] | None = None


class Container(BaseModel):
    """An album or playlist with its tracks."""

    item: BrowseItem
    tracks: list[BrowseItem]
    # True when the service returned more tracks than the hub's cap (HUB_YTMUSIC_MAX_TRACKS)
    # and the list was cut; the client can say "first N tracks".
    truncated: bool = False


class NeedsLinkError(Exception):
    """The service is not linked to the hub (or not on that side)."""

    code = "needs_link"

    def __init__(self, service: str, message: str | None = None) -> None:
        super().__init__(message or f"{service_label(service)} is not connected.")
        self.service = service
        self.message = message or f"{service_label(service)} is not connected."


class NeedsClientConfigError(NeedsLinkError):
    """The hub itself lacks the OAuth client configuration for a service (owner action, not a
    household member's): surfaces as ``needs_client_config`` instead of ``needs_link``."""

    code = "needs_client_config"


class ContentNotFoundError(Exception):
    def __init__(self, ref: ContentRef) -> None:
        label = service_label(ref.service)
        super().__init__(f"{ref.kind} {ref.id} was not found on {label}.")
        self.ref = ref
        self.message = f"{ref.kind.title()} {ref.id} was not found on {label}."
