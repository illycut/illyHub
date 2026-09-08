"""Content references and browse item shapes shared by the services, adapters and API.

"Browse once, play anywhere" (PRD review §2.1): the hub browses a streaming service through the
service's own API and plays on each ecosystem by constructing that device's playable reference
from the canonical service id. A :class:`ContentRef` is that canonical id; :class:`BrowseItem`
is what the client renders; :class:`Availability` says which sides can play it.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from .state import ArtRef, ContentKind, ContentRef, Service

__all__ = [
    "Availability",
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
    """Which ecosystems can play a service's content (the service is linked on that side)."""

    heos: bool = False
    sonos: bool = False


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


class NeedsLinkError(Exception):
    """The service is not linked to the hub (or not on that side)."""

    def __init__(self, service: str, message: str | None = None) -> None:
        super().__init__(message or f"{service.title()} is not connected.")
        self.service = service
        self.message = message or f"{service.title()} is not connected."


class ContentNotFoundError(Exception):
    def __init__(self, ref: ContentRef) -> None:
        super().__init__(f"{ref.kind} {ref.id} was not found on {ref.service}.")
        self.ref = ref
        self.message = f"{ref.kind.title()} {ref.id} was not found on {ref.service}."
