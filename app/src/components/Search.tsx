"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { SearchIcon } from "./icons";
import { ArtCard } from "./ArtCard";
import { ServiceBadge } from "./ServiceBadge";
import { TextSkeleton } from "./Skeleton";
import { formatTime } from "@/lib/format";
import { library, refKey, type LibraryItem, type SearchResults, type TrackItem } from "@/lib/hub/library";
import { useLibrary, errorMessage } from "@/lib/library/store";
import { roomsOnlyLabel } from "@/lib/services";
import { SEARCH_DEBOUNCE_MS, SEARCH_EMPTY, SEARCH_GROUPS, SEARCH_MIN_CHARS, SEARCH_PLACEHOLDER, SEARCH_TOO_SHORT, isEmptyResults, searchErrorCaption, searchQuery, searchStatusLine } from "@/lib/search";
import { stationSubtitle } from "@/lib/pandora";

export type SearchState = { phase: "idle" } | { phase: "loading"; q: string } | { phase: "results"; q: string; results: SearchResults } | { phase: "error"; q: string; message: string };

/**
 * Header search field (Phase 8, ai-dev #78): a magnifier button that expands to a full-width 48px
 * input with autofocus; Escape or Cancel collapses and clears. Debounced 300 ms, minimum two
 * characters, stale answers discarded by run id. Calls `onState` so the screen can swap its sections
 * for the results view while a query is active.
 */
export function SearchBar({ open, onOpenChange, onState, deps }: { open: boolean; onOpenChange: (open: boolean) => void; onState: (s: SearchState) => void; deps?: { debounceMs?: number } }) {
  const [text, setText] = useState("");
  const callOptions = useLibrary((s) => s.callOptions);
  const inputRef = useRef<HTMLInputElement>(null);
  const openerRef = useRef<HTMLButtonElement>(null);
  // Focus returns to the magnifier after Cancel/Escape (it re-mounts when the field collapses).
  const returnFocus = useRef(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const run = useRef(0);
  const debounceMs = deps?.debounceMs ?? SEARCH_DEBOUNCE_MS;

  const cancelPending = () => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = null;
    run.current += 1;
  };

  const close = useCallback(() => {
    cancelPending();
    returnFocus.current = true;
    onOpenChange(false);
    setText("");
    onState({ phase: "idle" });
  }, [onOpenChange, onState]);

  useEffect(() => {
    if (open) inputRef.current?.focus();
    else if (returnFocus.current) {
      returnFocus.current = false;
      openerRef.current?.focus();
    }
  }, [open]);

  useEffect(() => () => cancelPending(), []);

  const onChange = (raw: string) => {
    setText(raw);
    cancelPending();
    const q = searchQuery(raw);
    if (!q) {
      onState({ phase: "idle" });
      return;
    }
    const me = run.current;
    timer.current = setTimeout(async () => {
      if (run.current !== me) return;
      onState({ phase: "loading", q });
      try {
        const results = await library.search(q, callOptions());
        if (run.current === me) onState({ phase: "results", q, results });
      } catch (e) {
        if (run.current === me) onState({ phase: "error", q, message: errorMessage(e) });
      }
    }, debounceMs);
  };

  if (!open) {
    return (
      <button ref={openerRef} type="button" className="hit-target flex items-center justify-center rounded-control text-secondary" aria-label="Search" onClick={() => onOpenChange(true)} data-testid="open-search">
        <SearchIcon />
      </button>
    );
  }
  const tooShort = text.trim().length > 0 && text.trim().length < SEARCH_MIN_CHARS;
  return (
    <div className="flex min-w-0 flex-1 flex-col" data-testid="search-bar">
      <div className="flex items-center gap-2">
      <label className="flex h-target min-w-0 flex-1 items-center gap-2 rounded-control bg-raised px-3">
        <span className="text-secondary" aria-hidden="true">
          <SearchIcon size={20} />
        </span>
        <input
          ref={inputRef}
          type="search"
          value={text}
          placeholder={SEARCH_PLACEHOLDER}
          aria-label="Search"
          autoComplete="off"
          enterKeyHint="search"
          className="min-w-0 flex-1 bg-transparent text-body text-primary outline-none placeholder:text-tertiary"
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Escape") close();
          }}
          aria-describedby={tooShort ? "search-too-short" : undefined}
          data-testid="search-input"
        />
      </label>
      <button type="button" className="flex min-h-target items-center justify-center rounded-control px-2 text-caption text-secondary" onClick={close} data-testid="cancel-search">
        Cancel
      </button>
      </div>
      {tooShort ? (
        <p id="search-too-short" className="pt-1 text-micro text-secondary" data-testid="search-too-short">
          {SEARCH_TOO_SHORT}
        </p>
      ) : null}
    </div>
  );
}

/**
 * Results grouped Albums / Playlists / Tracks (/ Stations when returned): art cards for containers,
 * 56px rows for tracks, service badges and availability captions throughout. Every tap follows the
 * picker rule (Decision 4). Per-service failures are one caption line; nothing at all is "Nothing
 * matched." (§8, §10).
 */
export function SearchResultsView({
  state,
  onPlay,
  onPlayStation,
  onPlayTrack,
  onDetail,
}: {
  state: SearchState;
  onPlay: (item: LibraryItem) => void;
  /** Stations route through the Pandora rules (per-vendor reasons), like the home section. */
  onPlayStation: (item: LibraryItem) => void;
  onPlayTrack: (track: TrackItem) => void;
  onDetail: (item: LibraryItem) => void;
}) {
  if (state.phase === "idle") return null;
  // One polite status line announces each change once ("12 results for harmonic", "Searching…"); the
  // result groups themselves are not a live region.
  const status = (
    <p className="sr-only" role="status" aria-live="polite" data-testid="search-status">
      {searchStatusLine(state)}
    </p>
  );
  if (state.phase === "loading") {
    return (
      <div className="pt-4" data-testid="search-loading">
        {status}
        <TextSkeleton lines={4} />
      </div>
    );
  }
  if (state.phase === "error") {
    return (
      <div className="pt-4">
        {status}
        <p className="text-body text-error" role="alert" data-testid="search-error">
          {state.message}
        </p>
      </div>
    );
  }
  const { results } = state;
  const caption = searchErrorCaption(results);
  const empty = isEmptyResults(results);
  return (
    <div className="flex flex-col gap-8 pt-4" data-testid="search-results">
      {status}
      {caption ? (
        <p className="text-caption text-secondary" data-testid="search-caption">
          {caption}
        </p>
      ) : null}
      {empty ? (
        <p className="py-6 text-body text-secondary" data-testid="search-empty">
          {SEARCH_EMPTY}
        </p>
      ) : null}
      {SEARCH_GROUPS.map(({ key, title }) => {
        const items = results[key];
        if (items.length === 0) return null;
        const hid = `search-${key}-h`;
        return (
          <section key={key} aria-labelledby={hid} data-testid={`search-group-${key}`}>
            <h2 id={hid} className="text-title-2 text-primary">
              {title}
            </h2>
            {key === "tracks" ? (
              <ul className="mt-3 flex flex-col" data-testid="search-tracks">
                {(items as TrackItem[]).map((t) => {
                  const note = roomsOnlyLabel(t.availability);
                  return (
                    <li key={refKey(t.content_ref)} className="border-b border-stroke last:border-0">
                      <button type="button" className="flex min-h-row w-full items-center gap-3 rounded-control text-left" aria-label={`Play ${t.title}${t.artist ? `, ${t.artist}` : ""}`} onClick={() => onPlayTrack(t)} data-testid="search-track">
                        <span className="min-w-0 flex-1">
                          <span className="clamp-1 block text-body text-primary">{t.title}</span>
                          <span className="clamp-1 block text-caption text-secondary">{[t.artist ?? t.subtitle, t.album].filter(Boolean).join(" · ")}</span>
                        </span>
                        {note ? <span className="shrink-0 whitespace-nowrap text-micro text-secondary">{note}</span> : null}
                        <ServiceBadge source={t.content_ref.service} size={22} />
                        <span className="numeric shrink-0 text-micro text-tertiary">{t.duration_ms != null ? formatTime(t.duration_ms) : ""}</span>
                      </button>
                    </li>
                  );
                })}
              </ul>
            ) : (
              <div className="mt-3 grid grid-cols-2 gap-3 tablet:grid-cols-4" data-testid={`search-grid-${key}`}>
                {items.map((it) => {
                  const station = it.content_ref.kind === "station";
                  return (
                    <ArtCard
                      key={refKey(it.content_ref)}
                      payload={it}
                      title={it.title}
                      subtitle={station ? stationSubtitle(it.availability) : (roomsOnlyLabel(it.availability) ?? it.subtitle)}
                      art={it.art}
                      service={it.content_ref.service}
                      onPress={station ? onPlayStation : onPlay}
                      onDetail={station ? undefined : onDetail}
                      testId="search-card"
                    />
                  );
                })}
              </div>
            )}
          </section>
        );
      })}
    </div>
  );
}
