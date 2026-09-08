"""Search across the linked services (ai-dev #78).

``GET /api/search?q=`` fans out to every hub-linked library service (Tidal, YouTube Music) and,
for stations, to the Pandora merge. Each service gets a soft deadline; a slow or failing one
lands in ``errors`` and the others still answer, so a search is never all-or-nothing. Results
are grouped by kind and carry the same :class:`~illyhub_hub.content.BrowseItem` shape (with
availability and reasons) as browsing, so the client plays them through the same picker.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from pydantic import BaseModel, Field

from ..content import BrowseItem, NeedsLinkError
from ..logsetup import get_logger
from ..messages import SEARCH_TOO_SHORT
from ..state import service_label

log = get_logger("services.search")

MIN_QUERY_LEN = 2
DEFAULT_LIMIT = 20
MAX_LIMIT = 50
DEFAULT_DEADLINE_S = 4.0  # HUB_SEARCH_DEADLINE_S; the vendor APIs regularly take 2-3 s

# Service searches run on their own bounded pool so a burst of typing cannot starve the default
# executor the adapters use for device I/O (SoCo, tidalapi, ytmusicapi are all blocking HTTP).
SEARCH_EXECUTOR = ThreadPoolExecutor(max_workers=6, thread_name_prefix="hub-search")


async def in_search_pool(fn: Callable[..., Any], *args: Any) -> Any:
    return await asyncio.get_running_loop().run_in_executor(SEARCH_EXECUTOR, fn, *args)


GROUPS = ("albums", "playlists", "tracks", "stations")

# One library source: name, "is linked", and the search coroutine factory.
SearchFn = Callable[[str, int], Awaitable[dict[str, list[BrowseItem]]]]
SearchSource = tuple[str, Callable[[], bool], SearchFn]


class SearchResponse(BaseModel):
    query: str
    albums: list[BrowseItem] = Field(default_factory=list)
    playlists: list[BrowseItem] = Field(default_factory=list)
    tracks: list[BrowseItem] = Field(default_factory=list)
    stations: list[BrowseItem] = Field(default_factory=list)
    # service -> a plain sentence when that service did not answer (timeout, error, unlinked)
    errors: dict[str, str] = Field(default_factory=dict)
    # services that contributed results
    services: list[str] = Field(default_factory=list)
    partial: bool = False


class SearchService:
    def __init__(
        self,
        sources: list[SearchSource],
        *,
        stations: Callable[[], Awaitable[list[BrowseItem]]] | None = None,
        deadline_s: float = DEFAULT_DEADLINE_S,
    ) -> None:
        self._sources = sources
        self._stations = stations
        self.deadline_s = deadline_s

    async def search(self, query: str, limit: int = DEFAULT_LIMIT) -> SearchResponse:
        q = " ".join(query.split())
        if len(q) < MIN_QUERY_LEN:
            raise ValueError(SEARCH_TOO_SHORT)
        limit = max(1, min(limit, MAX_LIMIT))
        response = SearchResponse(query=q)

        async def run(name: str, fn: Callable[[str, int], Awaitable[Any]]) -> tuple[str, Any]:
            try:
                return name, await asyncio.wait_for(fn(q, limit), timeout=self.deadline_s)
            except TimeoutError:
                return name, TimeoutError(f"{service_label(name)} didn't answer in time.")
            except NeedsLinkError as exc:
                return name, exc
            except Exception as exc:  # noqa: BLE001 - one service must not sink the search
                log.warning(
                    "search source failed", extra={"extra": {"service": name, "error": str(exc)}}
                )
                return name, exc

        jobs = [run(name, fn) for name, linked, fn in self._sources if linked()]
        for name, linked, _fn in self._sources:
            if not linked():
                response.errors[name] = f"{service_label(name)} is not connected."
        if self._stations is not None:
            jobs.append(run("pandora", lambda _q, _l: self._stations_matching(_q, _l)))
        per_service: dict[str, dict[str, list[BrowseItem]]] = {}
        for name, result in await asyncio.gather(*jobs):
            if isinstance(result, BaseException):
                response.errors[name] = (
                    str(result)
                    if isinstance(result, TimeoutError | NeedsLinkError)
                    else f"{service_label(name)} search failed."
                )
                response.partial = True
                continue
            response.services.append(name)
            per_service[name] = {g: list(result.get(g, [])) for g in GROUPS}
        # Interleave services round-robin per group before cutting to ``limit``, so one service
        # with many hits cannot push another off the list entirely.
        for group in GROUPS:
            lists = [per_service[n][group] for n in response.services if per_service[n][group]]
            setattr(response, group, _round_robin(lists)[:limit])
        return response

    async def _stations_matching(self, query: str, limit: int) -> dict[str, list[BrowseItem]]:  # noqa: E501
        assert self._stations is not None
        q = query.casefold()
        stations = await self._stations()
        return {"stations": [s for s in stations if q in s.title.casefold()][:limit]}


def _round_robin(lists: list[list[BrowseItem]]) -> list[BrowseItem]:
    out: list[BrowseItem] = []
    i = 0
    while any(i < len(items) for items in lists):
        for items in lists:
            if i < len(items):
                out.append(items[i])
        i += 1
    return out
