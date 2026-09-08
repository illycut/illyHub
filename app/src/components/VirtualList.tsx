"use client";
import { useEffect, useRef, useState, type ReactNode } from "react";

/** Window of rows to render for a fixed-height list scrolled by the page. Pure, unit-tested. */
export function windowRange(count: number, rowHeight: number, listTop: number, scrollY: number, viewportHeight: number, overscan = 6): { start: number; end: number } {
  if (count === 0) return { start: 0, end: 0 };
  const firstVisible = Math.floor((scrollY - listTop) / rowHeight);
  const visibleCount = Math.ceil(viewportHeight / rowHeight);
  const start = Math.min(count, Math.max(0, firstVisible - overscan));
  const end = Math.min(count, Math.max(0, firstVisible) + visibleCount + overscan);
  return { start, end: Math.max(start, end) };
}

/**
 * Fixed-row-height list virtualised against the page scroll (playlists can run to hundreds of
 * tracks; a wall tablet should not lay them all out). Below `threshold` rows it renders plainly.
 * Rows are always direct `<li>` children of the `<ul>` (absolutely positioned when virtual) so
 * the list stays valid for assistive tech; the component owns the `<li>` and its divider, the
 * caller renders the row's content.
 */
export function VirtualList<T>({
  items,
  rowHeight,
  render,
  threshold = 60,
  testId,
  rowTestId,
  scrollRoot,
}: {
  items: T[];
  rowHeight: number;
  /** Row content; the surrounding `<li>` is rendered here. `index` is the array position. */
  render: (item: T, index: number) => ReactNode;
  threshold?: number;
  testId?: string;
  rowTestId?: string;
  /** A scrolling ancestor (a sheet's content pane) to window against instead of the page. */
  scrollRoot?: HTMLElement | null;
}) {
  const ref = useRef<HTMLUListElement>(null);
  const [range, setRange] = useState({ start: 0, end: Math.min(items.length, threshold) });
  const virtual = items.length > threshold;
  const count = items.length;

  useEffect(() => {
    if (!virtual) return;
    const root = scrollRoot ?? null;
    const scrollY = () => (root ? root.scrollTop : window.scrollY);
    const viewport = () => (root ? root.clientHeight : window.innerHeight);
    const update = () => {
      const listRect = ref.current?.getBoundingClientRect();
      const rootTop = root ? root.getBoundingClientRect().top : 0;
      const top = (listRect?.top ?? 0) - rootTop + scrollY();
      setRange((cur) => {
        const next = windowRange(count, rowHeight, top, scrollY(), viewport());
        return next.start === cur.start && next.end === cur.end ? cur : next;
      });
    };
    update();
    const target: HTMLElement | Window = root ?? window;
    target.addEventListener("scroll", update, { passive: true });
    window.addEventListener("resize", update);
    // Layout above the list can change without a resize (art loading, fonts): watch the document.
    const ro = typeof ResizeObserver !== "undefined" ? new ResizeObserver(update) : null;
    ro?.observe(root ?? document.documentElement);
    return () => {
      target.removeEventListener("scroll", update);
      window.removeEventListener("resize", update);
      ro?.disconnect();
    };
  }, [virtual, count, rowHeight, scrollRoot]);

  const divider = (i: number) => (i === count - 1 ? "" : "border-b border-stroke");

  if (!virtual) {
    return (
      <ul ref={ref} className="flex flex-col" data-testid={testId}>
        {items.map((it, i) => (
          <li key={i} className={divider(i)} style={{ height: rowHeight }} data-testid={rowTestId}>
            {render(it, i)}
          </li>
        ))}
      </ul>
    );
  }
  const { start, end } = range;
  return (
    <ul ref={ref} className="relative" style={{ height: count * rowHeight }} data-testid={testId} data-virtual="true">
      {items.slice(start, end).map((it, k) => {
        const i = start + k;
        return (
          <li key={i} className={divider(i)} style={{ position: "absolute", top: i * rowHeight, left: 0, right: 0, height: rowHeight }} data-testid={rowTestId}>
            {render(it, i)}
          </li>
        );
      })}
    </ul>
  );
}
