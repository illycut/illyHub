"use client";
import { useCallback, useEffect, useMemo } from "react";
import { useShallow } from "zustand/react/shallow";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ArtCard } from "./ArtCard";
import { RecentsRail } from "./RecentsRail";
import { GearIcon, LayersIcon } from "./icons";
import { ArtSkeleton, ConnectCard } from "./Skeleton";
import { useLibrary, onRefocus } from "@/lib/library/store";
import { useChrome, type PlayRequest } from "@/lib/ui/chrome";
import { stationSubtitle, stationsSectionModel, unlinkedVendors } from "@/lib/pandora";
import { useHub } from "@/lib/hub/store";
import { refKey, type ContentRef, type Home, type HistoryItem, type LibraryItem, type Section, type Service } from "@/lib/hub/library";

export function detailHref(ref: ContentRef): string {
  return `/browse?ref=${encodeURIComponent(refKey(ref))}`;
}

const SERVICE_LABEL: Record<Service, string> = { tidal: "Tidal", ytmusic: "YouTube Music", pandora: "Pandora" };

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

/** Grid of art cards with skeletons, connect cards for unlinked services, and the §8 empty copy. */
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
  /** Services to offer "Connect …" cards for when the section needs a link. */
  connect: Service[];
  onPlay: (item: LibraryItem) => void;
  onDetail: (item: LibraryItem) => void;
  onConnect: (service: Service) => void;
  emptyCopy: string;
  testId: string;
}) {
  const items = section?.items ?? [];
  const needs = section?.needs_link ?? null;
  if (loading && !section) {
    return (
      <div className="grid grid-cols-2 gap-3 tablet:grid-cols-4" aria-hidden="true">
        {[0, 1, 2, 3].map((i) => (
          <ArtSkeleton key={i} />
        ))}
      </div>
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
    <div className="grid grid-cols-2 gap-3 tablet:grid-cols-4" data-testid={testId}>
      {items.map((it) => (
        <ArtCard
          key={refKey(it.content_ref)}
          payload={it}
          title={it.title}
          subtitle={it.subtitle}
          art={it.art}
          service={it.content_ref.service}
          onPress={onPlay}
          onDetail={it.content_ref.kind === "station" ? undefined : onDetail}
          testId="grid-card"
        />
      ))}
      {needs
        ? connect.map((svc) => <ConnectCard key={svc} service={SERVICE_LABEL[svc]} onPress={svc === "ytmusic" ? undefined : () => onConnect(svc)} note={svc === "ytmusic" ? "Coming in a later phase" : undefined} />)
        : null}
    </div>
  );
}

/**
 * Home screen (PRD §3.4a, §7a; design system §6.1, §6.2, §8). Sections: Recently played rail,
 * Your playlists (merged across services), Favorite albums, Pandora stations (Phase 5, hidden
 * while empty). Every card plays through the target picker (Decision 4); the chevron opens the
 * detail view. Home refetches on focus and after any play. Nothing here subscribes to the hub's
 * 1 Hz position stream; the rail reads only side vendors through a shallow selector.
 */
export function HomeScreen() {
  const router = useRouter();
  const home = useLibrary((s) => s.home);
  const loadHome = useLibrary((s) => s.loadHome);
  const requestPlay = useChrome((s) => s.requestPlay);
  const setZonesOpen = useChrome((s) => s.setZonesOpen);

  useEffect(() => {
    void loadHome();
    return onRefocus(() => void loadHome());
  }, [loadHome]);

  const data = home.data;
  const loading = home.loading && !data;
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

  return (
    <div className="min-h-dvh bg-base pb-[calc(var(--size-mini-player)+var(--safe-bottom)+var(--space-4))]" data-testid="home">
      <header className="screen-margin flex h-[calc(var(--size-target)+var(--space-2)+var(--safe-top))] items-end justify-between pt-safe">
        <h1 className="pb-2 text-title-1 text-primary">illyHub</h1>
        <div className="flex items-center gap-2">
          <button type="button" className="hit-target flex items-center justify-center rounded-control text-secondary" aria-label="Zones" onClick={() => setZonesOpen(true)}>
            <LayersIcon />
          </button>
          <Link href="/settings" className="hit-target flex items-center justify-center rounded-control text-secondary" aria-label="Settings" data-testid="open-settings">
            <GearIcon />
          </Link>
        </div>
      </header>
      {home.error && !data ? (
        <p className="screen-margin pt-2 text-caption text-error" role="alert">
          {home.error}
        </p>
      ) : null}
      <main className="screen-margin flex flex-col gap-8 pt-4">
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
              connect={["tidal", "ytmusic"]}
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
              connect={["tidal"]}
              onPlay={playItem}
              onDetail={goDetail}
              onConnect={goConnect}
              emptyCopy="Favorite an album in Tidal and it shows up here."
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
      </main>
    </div>
  );
}
