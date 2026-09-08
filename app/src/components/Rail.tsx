"use client";
import type { ReactNode } from "react";

/**
 * Horizontal card rail: the one scroll container every home section uses.
 *
 * Home was a Recents rail followed by stacked 2-column grids, which is what the design system
 * asked for. In use on a phone that made the page very long: three sections of grid meant
 * scrolling past everything to reach the last one, and no section could be scanned without
 * committing to vertical travel. The owner asked for the streaming-service shape instead — one
 * row per section, swipe sideways within a row — so every section is now a rail and the page
 * height is bounded by the number of sections rather than the size of the library.
 *
 * Design system §4 and §6.2 are updated to match; do not reintroduce the grid without changing
 * them back, or the next person will implement the doc rather than the product.
 *
 * Mechanics that matter on touch:
 * - `BLEED` cancels the screen margin so the row runs to the physical edge and the next card
 *   peeks, which is the affordance that says "this scrolls". Without it a row ends flush with
 *   the text above and reads as a complete, non-scrolling set.
 * - `snap-x` with `scroll-padding-left` set to the screen margin lands each card at the text
 *   margin rather than the screen edge.
 * - `snap-proximity`, not `snap-mandatory`: a mandatory rail fights a fast flick across many
 *   cards, and these rows can be long.
 * - `no-scrollbar` hides the bar; the peeking card is the affordance.
 */
const BLEED = "-mx-[var(--screen-margin)] px-[var(--screen-margin)]";

export function Rail({
  children,
  testId,
  label,
}: {
  children: ReactNode;
  testId?: string;
  /** Labels the list for assistive tech when the section heading is not adjacent. */
  label?: string;
}) {
  return (
    <ul
      className={`no-scrollbar ${BLEED} flex snap-x snap-proximity gap-3 overflow-x-auto pb-1`}
      style={{ scrollPaddingLeft: "var(--screen-margin)" }}
      data-testid={testId}
      aria-label={label}
    >
      {children}
    </ul>
  );
}

/** One card slot in a rail: fixed card width, never shrinking, snapping to the text margin. */
export function RailItem({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <li className={`w-card shrink-0 snap-start ${className}`}>{children}</li>;
}

/** Skeleton row while a section loads. Holds the card width so the rail does not reflow. */
export function RailSkeleton({ count = 3, children }: { count?: number; children: ReactNode }) {
  return (
    <div className={`${BLEED} flex gap-3 overflow-hidden`} aria-hidden="true">
      {Array.from({ length: count }, (_, i) => (
        <div key={i} className="w-card shrink-0">
          {children}
        </div>
      ))}
    </div>
  );
}
