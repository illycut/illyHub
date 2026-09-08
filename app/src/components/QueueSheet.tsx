"use client";
import { useEffect, useRef, useState } from "react";
import { VirtualList } from "./VirtualList";
import { PlayIcon } from "./icons";
import { Sheet } from "./Sheet";
import { TextSkeleton } from "./Skeleton";
import { ServiceBadge } from "./ServiceBadge";
import { formatTime } from "@/lib/format";
import { useHub } from "@/lib/hub/store";
import { UP_NEXT, isRadioSource, queueRowLabel, queueView } from "@/lib/queue";

/**
 * "Up next" sheet (Phase 8, ai-dev #77): the active side's queue with the playing entry highlighted
 * (amber title + `aria-current`, the detail-view rule from design §13 1.3), tap → jump. Live updates
 * come from the `queues.<side>` delta path; a hub that has not streamed one is asked once on open.
 * Radio sources get a sentence instead of a list. Rows are 56px with body title, caption artist and
 * tabular duration, like the detail view.
 */
export function QueueSheet({ open, sideId, onClose }: { open: boolean; sideId: string | null; onClose: () => void }) {
  const side = useHub((s) => (sideId ? s.state?.sides[sideId] : undefined));
  const np = useHub((s) => (sideId ? s.state?.now_playing[sideId] : undefined));
  const queue = useHub((s) => (sideId ? s.state?.queues?.[sideId] : undefined));
  const loadQueue = useHub((s) => s.loadQueue);
  const queueJump = useHub((s) => s.queueJump);
  const loading = useHub((s) => (sideId ? !!s.queueLoading[sideId] : false));
  const hasQueue = !!queue;
  const radio = isRadioSource(np);
  // One fetch per open per side when the hub has not streamed a queue (never for radio); a failed
  // fetch settles to the empty line instead of a skeleton that never ends.
  const asked = useRef<Set<string>>(new Set());
  useEffect(() => {
    if (!open) asked.current.clear();
  }, [open]);
  useEffect(() => {
    if (!open || !sideId || hasQueue || radio || asked.current.has(sideId)) return;
    asked.current.add(sideId);
    void loadQueue(sideId);
  }, [open, sideId, hasQueue, radio, loadQueue]);

  const view = queueView(side, np, queue, loading);
  // The sheet's scroll pane hosts the (possibly virtualised) list; the current row is centred on open.
  const [pane, setPane] = useState<HTMLElement | null>(null);
  const currentIndex = view.kind === "list" ? view.current : null;
  const centred = useRef<string | null>(null);
  useEffect(() => {
    if (!open) {
      centred.current = null;
      return;
    }
    const key = `${sideId}:${currentIndex}`;
    if (currentIndex === null || !pane || centred.current === key) return;
    const row = pane.querySelector<HTMLElement>(`[data-testid="queue-row"][data-index="${currentIndex}"]`);
    // Instant, never smooth: reduced-motion users get no animation either way.
    row?.scrollIntoView({ block: "center", behavior: "auto" });
    centred.current = key;
  }, [open, pane, currentIndex, sideId]);

  return (
    <Sheet open={open} onClose={onClose} title={UP_NEXT} testId="queue-sheet">
      {view.kind === "loading" ? (
        <div className="py-3">
          <TextSkeleton lines={4} />
        </div>
      ) : view.kind === "station" || view.kind === "empty" ? (
        <p className="py-6 text-body text-secondary" data-testid={view.kind === "station" ? "queue-station" : "queue-empty"}>
          {view.text}
        </p>
      ) : (
        <div ref={setPane} data-testid="queue-pane">
          {view.note ? (
            <p className="pb-2 text-micro text-tertiary" data-testid="queue-truncated">
              {view.note}
            </p>
          ) : null}
          <VirtualList
            items={view.items}
            rowHeight={56}
            testId="queue-list"
            rowTestId="queue-li"
            scrollRoot={pane?.parentElement ?? null}
            render={(it) => {
              const current = view.current === it.index;
              return (
                <div className="h-full" data-index={it.index} data-current={current ? "true" : undefined} data-testid="queue-row">
                  <button
                    type="button"
                    className="flex min-h-row w-full items-center gap-3 rounded-control text-left"
                    aria-label={queueRowLabel(it)}
                    aria-current={current ? "true" : undefined}
                    onClick={() => {
                      if (sideId) void queueJump(sideId, it.index);
                      onClose();
                    }}
                  >
                    <span className={`numeric flex w-6 shrink-0 items-center justify-end text-micro ${current ? "text-signal" : "text-tertiary"}`} aria-hidden="true">
                      {current ? <PlayIcon size={14} /> : it.index + 1}
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className={`clamp-1 block text-body ${current ? "text-signal" : "text-primary"}`}>{it.title}</span>
                      <span className="clamp-1 block text-caption text-secondary">{[it.artist, it.album].filter(Boolean).join(" · ")}</span>
                    </span>
                    {it.content_ref ? <ServiceBadge source={it.content_ref.service} size={22} /> : null}
                    <span className="numeric shrink-0 text-micro text-tertiary">{it.duration_ms != null ? formatTime(it.duration_ms) : ""}</span>
                  </button>
                </div>
              );
            }}
          />
        </div>
      )}
    </Sheet>
  );
}
