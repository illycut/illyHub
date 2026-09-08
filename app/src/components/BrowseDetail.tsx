"use client";
import { useCallback, useEffect } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ChevronLeftIcon, PlayIcon } from "./icons";
import type { NowPlaying } from "@/lib/hub/types";
import { ServiceBadge } from "./ServiceBadge";
import { VirtualList } from "./VirtualList";
import { ArtSkeleton, TextSkeleton } from "./Skeleton";
import { artUrl } from "@/lib/hub/config";
import { formatTime } from "@/lib/format";
import { useHub } from "@/lib/hub/store";
import { useLibrary } from "@/lib/library/store";
import { useChrome } from "@/lib/ui/chrome";
import { readLastTarget } from "@/lib/prefs";
import { refKey, type ContentRef, type TrackItem } from "@/lib/hub/library";

export const TRACK_ROW_PX = 56;

/** Copy for a track the chosen side cannot play; null when playable everywhere we know about. */
export function trackAvailabilityNote(track: TrackItem, sideVendor: "heos" | "sonos" | null): string | null {
  if (!sideVendor) {
    if (!track.availability.heos && !track.availability.sonos) return "Not available";
    if (!track.availability.heos) return "Sonos only";
    if (!track.availability.sonos) return "HEOS only";
    return null;
  }
  const ok = sideVendor === "heos" ? track.availability.heos : track.availability.sonos;
  return ok ? null : `Not available on ${sideVendor === "heos" ? "HEOS" : "Sonos"}`;
}

const SERVICE_LABEL: Record<string, string> = { tidal: "Tidal", ytmusic: "YouTube Music", pandora: "Pandora" };

/** The track now playing on the active side: by id when the hub reports one, else title + artist (UX U10). */
export function isCurrentTrack(track: TrackItem, np: NowPlaying | null | undefined): boolean {
  if (!np) return false;
  if (np.track_id) return np.track_id === track.content_ref.id || np.track_id.endsWith(`:${track.content_ref.id}`);
  return !!np.title && np.title === track.title && (np.artist ?? null) === (track.artist ?? null);
}

/**
 * Album / playlist detail (ai-dev #49): hero art, title, meta, "Play on…" through the target
 * picker, track rows (body title, caption artist, tabular duration). Tapping a track plays from
 * its hub-canonical index. Long lists are virtualised. Unavailable tracks are flagged, never
 * hidden. A `needs_link` answer renders the §8 "Connect …" affordance instead of an error.
 */
export function BrowseDetail({ contentRef }: { contentRef: ContentRef }) {
  const router = useRouter();
  const key = refKey(contentRef);
  const entry = useLibrary((s) => s.details[key]);
  const loadDetail = useLibrary((s) => s.loadDetail);
  const requestPlay = useChrome((s) => s.requestPlay);
  const activeVendor = useHub((s) => (s.activeSideId ? (s.state?.sides[s.activeSideId]?.vendor ?? null) : null));
  const activeNp = useHub((s) => (s.activeSideId ? s.state?.now_playing[s.activeSideId] : undefined));

  useEffect(() => {
    void loadDetail(contentRef);
  }, [loadDetail, contentRef]);

  const detail = entry?.data ?? null;
  const item = detail?.item ?? null;
  const tracks = detail?.tracks ?? [];
  const hero = artUrl(item?.art, 1080);
  const needsLink = entry?.errorCode === "needs_link" ? contentRef.service : null;

  const play = useCallback(
    (track?: TrackItem) => {
      if (!item) return;
      requestPlay({
        content_ref: item.content_ref,
        title: track ? track.title : item.title,
        subtitle: track ? (track.artist ?? item.title) : item.subtitle,
        art: track?.art.url ? track.art : item.art,
        start_index: track?.index,
        preferred: readLastTarget(refKey(item.content_ref)) ?? [],
        availability: track?.availability ?? item.availability,
      });
    },
    [item, requestPlay],
  );

  return (
    <div className="min-h-dvh bg-base pb-[calc(var(--size-mini-player)+var(--safe-bottom)+var(--space-4))]" data-testid="browse-detail">
      <header className="screen-margin flex h-[calc(var(--size-target)+var(--space-2)+var(--safe-top))] items-end justify-between pt-safe">
        <button type="button" className="hit-target -ml-3 flex items-center justify-center rounded-control text-secondary" aria-label="Back" onClick={() => router.back()} data-testid="back">
          <ChevronLeftIcon />
        </button>
        {item ? <ServiceBadge source={item.content_ref.service} size={22} /> : null}
      </header>
      <main className="screen-margin flex flex-col gap-6 pt-2">
        <section className="flex flex-col gap-4 tablet:flex-row tablet:items-end">
          <div className="mx-auto w-full max-w-[320px] tablet:mx-0 tablet:w-[240px]">
            {hero ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img src={hero} alt="" draggable={false} className="aspect-square w-full rounded-art bg-overlay object-cover" data-testid="detail-art" />
            ) : (
              <ArtSkeleton />
            )}
          </div>
          <div className="min-w-0 flex-1">
            {item ? (
              <>
                <h1 className="clamp-2 text-title-1 text-primary" data-testid="detail-title">
                  {item.title}
                </h1>
                <p className="mt-1 clamp-1 text-caption text-secondary">
                  {[item.subtitle, item.track_count ? `${item.track_count} tracks` : tracks.length ? `${tracks.length} tracks` : null].filter(Boolean).join(" · ")}
                </p>
                <button
                  type="button"
                  className="mt-4 inline-flex h-target items-center justify-center gap-2 rounded-control bg-overlay px-5 text-body text-primary"
                  onClick={() => play()}
                  data-testid="detail-play"
                >
                  <PlayIcon size={20} />
                  Play on…
                </button>
              </>
            ) : needsLink ? (
              <div className="flex flex-col gap-3" data-testid="detail-needs-link">
                <p className="text-body text-secondary">{SERVICE_LABEL[needsLink] ?? needsLink} isn&apos;t connected.</p>
                <Link href={`/settings?link=${needsLink}`} className="inline-flex h-target items-center justify-center rounded-control bg-overlay px-5 text-body text-primary">
                  Connect {SERVICE_LABEL[needsLink] ?? needsLink}
                </Link>
              </div>
            ) : entry?.error ? (
              <p className="text-body text-error" role="alert">
                {entry.error}
              </p>
            ) : (
              <TextSkeleton lines={3} />
            )}
          </div>
        </section>
        <section aria-label="Tracks">
          {!detail && !entry?.error ? (
            <TextSkeleton lines={6} />
          ) : (
            <VirtualList
              items={tracks}
              rowHeight={TRACK_ROW_PX}
              testId="track-list"
              rowTestId="track-li"
              render={(t) => {
                const note = trackAvailabilityNote(t, activeVendor);
                const current = isCurrentTrack(t, activeNp);
                return (
                  <button
                    type="button"
                    className="flex h-full w-full items-center gap-3 rounded-control text-left"
                    onClick={() => play(t)}
                    aria-label={`Play from ${t.title}`}
                    aria-current={current ? "true" : undefined}
                    data-testid="track-row"
                    data-index={t.index}
                  >
                    <span className={`numeric flex w-6 shrink-0 items-center justify-end text-micro ${current ? "text-signal" : "text-tertiary"}`} aria-hidden="true">
                      {current ? <PlayIcon size={14} /> : t.index + 1}
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className={`clamp-1 block text-body ${current ? "text-signal" : note ? "text-secondary" : "text-primary"}`}>{t.title}</span>
                      <span className="clamp-1 block text-caption text-secondary">{note ? note : (t.artist ?? t.subtitle ?? "")}</span>
                    </span>
                    <span className="numeric shrink-0 text-micro text-tertiary">{t.duration_ms != null ? formatTime(t.duration_ms) : ""}</span>
                  </button>
                );
              }}
            />
          )}
          {detail && tracks.length === 0 ? <p className="py-6 text-body text-secondary">No tracks to show.</p> : null}
        </section>
      </main>
    </div>
  );
}
