"use client";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useShallow } from "zustand/react/shallow";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ArtCard } from "./ArtCard";
import { Rail, RailItem, RailSkeleton } from "./Rail";
import { RecentsRail } from "./RecentsRail";
import { SearchBar, SearchResultsView, type SearchState } from "./Search";
import { GearIcon, LayersIcon } from "./icons";
import { ArtSkeleton, ConnectCard } from "./Skeleton";
import { useLibrary, onRefocus } from "@/lib/library/store";
import { useChrome, type PlayRequest } from "@/lib/ui/chrome";
import { stationSubtitle, stationsSectionModel, unlinkedVendors } from "@/lib/pandora";
import { useHub } from "@/lib/hub/store";
import { refKey, type AccountStatus, type ContentRef, type Home, type HistoryItem, type LibraryItem, type Section, type Service, type TrackItem } from "@/lib/hub/library";
import { HUB_LINKED_SERVICES, SERVICE_LABEL, isHubLinked } from "@/lib/services";

export function detailHref(ref: ContentRef): string {
  return `/browse?ref=${encodeURIComponent(refKey(ref))}`;
}

export function toPlayRequest(item: LibraryItem, preferred: string[] = [], unlinked_vendors?: PlayRequest["unlinked_vendors"]): PlayRequest {
  return { content_ref: item.content_ref, title: item.title, subtitle: item.subtitle, art: item.art, preferred, availability: item.availability, unlinked_vendors };
}

/**
 * Recents tap (UX U1): availability comes from the history item when the hub sends it, else from the
 * matching station in the home stations section; the picker then disables rooms that cannot play it.
 */
export function historyToPlayRequest(item: HistoryItem, home?: Home | null): PlayRequest {
  const key = refKey(item.content_ref);
  const fromHome = home?.stations.items.find((s) => refKey(s.content_ref) === key);
  const availability = item.availability ?? fromHome?.availability;
  const unlinked = item.content_ref.service === "pandora" ? unlinkedVendors(home?.stations.linked) : undefined;
  return { content_ref: item.content_ref, title: item.title, subtitle: item.subtitle, art: item.art, preferred: item.last_targets, availability, unlinked_vendors: unlinked };
}

/**
 * Which "Connect …" cards home shows, once per screen (UX U3 / B2): every hub-linked service that a
 * library section reports in `needs_link`, plus, belt and braces, any hub-linked account that
 * `/api/settings` reports unlinked. Never a card for a service a section reports as linked.
 */
export function connectServices(home: Home | null | undefined, accounts: readonly AccountStatus[] | null | undefined): Service[] {
  const out = new Set<Service>();
  const sections = home ? [home.playlists, home.favorite_albums] : [];
  for (const sec of sections) for (const svc of sec.needs_link) if (isHubLinked(svc)) out.add(svc);
  for (const a of accounts ?? []) if (isHubLinked(a.service) && !a.linked && a.state !== "restoring" && a.state !== "pending") out.add(a.service);
  for (const sec of sections) for (const [svc, linked] of Object.entries(sec.linked ?? {})) if (linked === true && isHubLinked(svc)) out.delete(svc);
  return HUB_LINKED_SERVICES.filter((s) => out.has(s));
}

/** Room names per vendor from the hub's sides, as primitives so a shallow selector stays stable. */
function useRoomsByVendor(): { heos: string[]; sonos: string[] } {
  const flat = useHub(useShallow((s) => Object.values(s.state?.sides ?? {}).map((side) => `${side.vendor}|${side.name}`)));
  return useMemo(() => {
    const out = { heos: [] as string[], sonos: [] as string[] };
    for (const f of flat) {
      const sep = f.indexOf("|");
      out[f.slice(0, sep) as "heos" | "sonos"].push(f.slice(sep + 1));
    }
    return out;
  }, [flat]);
}

/**
 * Rail of art cards with skeletons, connect cards for unlinked services, and the §8 empty copy.
 *
 * Was a 2-column grid. Stacked grids made the phone page as long as the library, so every
 * section is a horizontal rail now (see Rail.tsx for the reasoning and the touch mechanics).
 * The name is kept so call sites and tests do not churn; `CardGrid` is a rail.
 */
export function CardGrid({
  section,
  loading,
  connect,
  onPlay,
  onDetail,
  onConnect,
  emptyCopy,
  testId,
}: {
  section: Section<LibraryItem> | null;
  loading: boolean;
  /** "Connect …" cards to render in this grid (the screen decides which grid carries them, once). */
  connect: Service[];
  onPlay: (item: LibraryItem) => void;
  onDetail: (item: LibraryItem) => void;
  onConnect: (service: Service) => void;
  emptyCopy: string;
  testId: string;
}) {
  const items = section?.items ?? [];
  const needs = connect.length > 0;
  if (loading && !section) {
    return (
      <RailSkeleton count={3}>
        <ArtSkeleton />
      </RailSkeleton>
    );
  }
  if (items.length === 0 && !needs) {
    return (
      <p className={`py-6 text-body ${section?.error ? "text-error" : "text-secondary"}`} role={section?.error ? "alert" : undefined}>
        {section?.error ?? emptyCopy}
      </p>
    );
  }
  return (
    <Rail testId={testId}>
      {items.map((it) => (
        <RailItem key={refKey(it.content_ref)}>
          <ArtCard
            payload={it}
            title={it.title}
            subtitle={it.subtitle}
            art={it.art}
            service={it.content_ref.service}
            onPress={onPlay}
            onDetail={it.content_ref.kind === "station" ? undefined : onDetail}
            testId="grid-card"
          />
        </RailItem>
      ))}
      {connect.map((svc) => (
        <RailItem key={svc}>
          <ConnectCard service={SERVICE_LABEL[svc]} onPress={() => onConnect(svc)} />
        </RailItem>
      ))}
    </Rail>
  );
}

/**
 * Home screen (PRD §3.4a, §7a; design system §6.1, §6.2, §8). Sections: Recently played rail,
 * Your playlists (merged across Tidal and YouTube Music), Favorite albums (both services fold in,
 * badge tells them apart), Pandora stations (Phase 5, hidden
 * while empty). Every card plays through the target picker (Decision 4); the chevron opens the
 * detail view. Home refetches on focus and after any play. Nothing here subscribes to the hub's
 * 1 Hz position stream; the rail reads only side vendors through a shallow selector.
 */
export function HomeScreen() {
  const router = useRouter();
  const home = useLibrary((s) => s.home);
  const loadHome = useLibrary((s) => s.loadHome);
  const accounts = useLibrary((s) => s.settings.data?.accounts);
  const loadSettings = useLibrary((s) => s.loadSettings);
  const requestPlay = useChrome((s) => s.requestPlay);
  const setZonesOpen = useChrome((s) => s.setZonesOpen);

  useEffect(() => {
    void loadHome();
    // Account link state backs the Connect cards when a section cannot say (belt and braces).
    void loadSettings();
    return onRefocus(() => void loadHome());
  }, [loadHome, loadSettings]);

  const data = home.data;
  const loading = home.loading && !data;
  // Connect cards render once, in the first library grid.
  const connect = useMemo(() => connectServices(data, accounts), [data, accounts]);
  const roomsByVendor = useRoomsByVendor();
  const stations = useMemo(() => stationsSectionModel(data?.stations, roomsByVendor), [data?.stations, roomsByVendor]);
  // Station cards: subtitle says which vendor's rooms can play it (UX U9); none when both can.
  const stationsSection = useMemo<Section<LibraryItem> | null>(
    () => (data ? { ...data.stations, items: stations.items.map((it) => ({ ...it, subtitle: stationSubtitle(it.availability) })) } : null),
    [data, stations.items],
  );
  // Station rows in the picker name the fix per vendor from the section's link state (UX U3).
  const unlinked = useMemo(() => unlinkedVendors(data?.stations.linked), [data?.stations.linked]);
  const playStation = useCallback((item: LibraryItem) => requestPlay(toPlayRequest(item, [], unlinked)), [requestPlay, unlinked]);
  const playItem = useCallback((item: LibraryItem) => requestPlay(toPlayRequest(item)), [requestPlay]);
  const playRecent = useCallback((item: HistoryItem) => requestPlay(historyToPlayRequest(item, data)), [requestPlay, data]);
  const goDetail = useCallback((item: { content_ref: ContentRef }) => router.push(detailHref(item.content_ref)), [router]);
  const goConnect = useCallback((service: Service) => router.push(`/settings?link=${service}`), [router]);
  // Search (Phase 8): while a query is active the results replace the sections below the header.
  const [search, setSearch] = useState<SearchState>({ phase: "idle" });
  const [searchOpen, setSearchOpen] = useState(false);
  const playTrack = useCallback(
    (t: TrackItem) =>
      requestPlay({ content_ref: t.content_ref, title: t.title, subtitle: t.artist ?? t.subtitle, art: t.art, preferred: [], availability: t.availability }),
    [requestPlay],
  );
  // The field being open hides the header chrome; a query being active swaps the sections for results.
  const searching = search.phase !== "idle";
  // The sections give way to results while a query is active; the page scroll position is saved on
  // the way in and restored on the way out so Cancel lands where the user was.
  const savedScroll = useRef<number | null>(null);
  const wasSearching = useRef(false);
  useEffect(() => {
    if (searching && !wasSearching.current) {
      savedScroll.current = window.scrollY;
      window.scrollTo(0, 0);
    } else if (!searching && wasSearching.current && savedScroll.current !== null) {
      const y = savedScroll.current;
      savedScroll.current = null;
      requestAnimationFrame(() => window.scrollTo(0, y));
    }
    wasSearching.current = searching;
  }, [searching]);

  return (
    <div className="min-h-dvh bg-base pb-[calc(var(--size-mini-player)+var(--safe-bottom)+var(--space-4))]" data-testid="home">
      <header className="screen-margin flex h-[calc(var(--size-target)+var(--space-2)+var(--safe-top))] items-end justify-between gap-2 pt-safe">
        {/* The h1 stays in the outline while searching (visually hidden), so the page never loses its heading. */}
        <h1 className={searchOpen ? "sr-only" : "pb-2 text-title-1 text-primary"}>{searchOpen ? "Search illyHub" : "illyHub"}</h1>
        <div className={`flex items-center gap-2 pb-1 ${searchOpen ? "flex-1" : ""}`}>
          <SearchBar open={searchOpen} onOpenChange={setSearchOpen} onState={setSearch} />
          {searchOpen ? null : (
            <>
              <button type="button" className="hit-target flex items-center justify-center rounded-control text-secondary" aria-label="Zones" onClick={() => setZonesOpen(true)}>
                <LayersIcon />
              </button>
              <Link href="/settings" className="hit-target flex items-center justify-center rounded-control text-secondary" aria-label="Settings" data-testid="open-settings">
                <GearIcon />
              </Link>
            </>
          )}
        </div>
      </header>
      {home.error && !data ? (
        <p className="screen-margin pt-2 text-caption text-error" role="alert">
          {home.error}
        </p>
      ) : null}
      <main className="screen-margin flex flex-col gap-8 pt-4">
        {searching ? (
          <SearchResultsView state={search} onPlay={playItem} onPlayStation={playStation} onPlayTrack={playTrack} onDetail={goDetail} />
        ) : (
          <>
        <section aria-labelledby="recents-h">
          <h2 id="recents-h" className="text-title-2 text-primary">Recently played</h2>
          <div className="mt-3">
            <RecentsRail items={data?.recents.items ?? []} loading={loading} onPlay={playRecent} onDetail={goDetail} />
          </div>
        </section>
        <section aria-labelledby="playlists-h">
          <h2 id="playlists-h" className="text-title-2 text-primary">Your playlists</h2>
          <div className="mt-3">
            <CardGrid
              section={data?.playlists ?? null}
              loading={loading}
              connect={connect}
              onPlay={playItem}
              onDetail={goDetail}
              onConnect={goConnect}
              emptyCopy="Saved playlists show up here."
              testId="playlists-grid"
            />
          </div>
        </section>
        <section aria-labelledby="albums-h">
          <h2 id="albums-h" className="text-title-2 text-primary">Favorite albums</h2>
          <div className="mt-3">
            <CardGrid
              section={data?.favorite_albums ?? null}
              loading={loading}
              connect={[]}
              onPlay={playItem}
              onDetail={goDetail}
              onConnect={goConnect}
              emptyCopy="Favorite an album in Tidal or YouTube Music and it shows up here."
              testId="albums-grid"
            />
          </div>
        </section>
        {stations.visible && stationsSection ? (
          // Pandora stations (HOME-5): alphabetical, hidden while empty. Pandora is linked in the
          // vendor apps, never through the hub, so a vendor that lacks it gets a one-line fact-and-fix
          // note instead of a Connect card (design system §8, §10).
          <section aria-labelledby="stations-h" data-testid="stations-section">
            <h2 id="stations-h" className="text-title-2 text-primary">Pandora stations</h2>
            {stations.note ? (
              <p className="mt-1 text-caption text-secondary" data-testid="stations-note">
                {stations.note}
              </p>
            ) : null}
            <div className="mt-3">
              <CardGrid
                section={stationsSection}
                loading={false}
                connect={[]}
                onPlay={playStation}
                onDetail={goDetail}
                onConnect={goConnect}
                emptyCopy=""
                testId="stations-grid"
              />
            </div>
          </section>
        ) : null}
          </>
        )}
      </main>
    </div>
  );
}
