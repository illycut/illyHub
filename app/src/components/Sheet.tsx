"use client";
import { useEffect, useId, useRef } from "react";
import { AnimatePresence, motion, useDragControls } from "framer-motion";
import { useReducedMotion } from "@/lib/reducedMotion";
import { useScrollLock } from "@/lib/scrollLock";

const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/** Keep Tab cycling inside `root`. Exported for tests. */
export function trapTab(root: HTMLElement, e: KeyboardEvent): void {
  if (e.key !== "Tab") return;
  // Everything inside an open sheet is on screen; only skip hidden or aria-hidden nodes (no layout
  // probing, which jsdom cannot answer and real browsers do not need here).
  const nodes = Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE)).filter((n) => !n.hidden && n.getAttribute("aria-hidden") !== "true" && !n.closest("[aria-hidden='true']"));
  if (nodes.length === 0) {
    e.preventDefault();
    root.focus();
    return;
  }
  const first = nodes[0]!;
  const last = nodes[nodes.length - 1]!;
  const active = document.activeElement as HTMLElement | null;
  if (e.shiftKey && (active === first || active === root || !root.contains(active))) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && (active === last || active === root || !root.contains(active))) {
    e.preventDefault();
    first.focus();
  }
}

/**
 * Bottom sheet primitive (design system §4 radius-sheet, §6.5/§6.6). Never a modal dialog for
 * playback actions in the visual sense (a sheet with a scrim, dismissed by scrim tap, Escape, or a
 * swipe down started on the handle/title), but it does hold focus: Tab cycles inside it, the
 * opener gets focus back on close, and `aria-modal` says so (UX U3). Sheets stack above the Now
 * Playing overlay (z-40) so the target picker and volume sheet open from it.
 */
export function Sheet({
  open,
  onClose,
  title,
  children,
  testId,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  children: React.ReactNode;
  testId?: string;
}) {
  const reduced = useReducedMotion();
  const titleId = useId();
  const ref = useRef<HTMLDivElement>(null);
  const opener = useRef<HTMLElement | null>(null);
  const onCloseRef = useRef(onClose);
  useEffect(() => {
    onCloseRef.current = onClose;
  });
  const drag = useDragControls();
  useScrollLock(open);
  useEffect(() => {
    if (!open) return;
    opener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        onCloseRef.current();
        return;
      }
      if (ref.current) trapTab(ref.current, e);
    };
    window.addEventListener("keydown", onKey);
    ref.current?.focus();
    return () => {
      window.removeEventListener("keydown", onKey);
      const back = opener.current;
      opener.current = null;
      // After the sheet has left the DOM, hand focus back to whatever opened it (UX U3).
      if (back) setTimeout(() => document.contains(back) && back.focus(), 0);
    };
  }, [open]);
  return (
    <AnimatePresence>
      {open ? (
        <>
          <motion.button
            type="button"
            aria-label="Close"
            className="fixed inset-0 z-50 bg-base/60"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.15 }}
            onClick={onClose}
            tabIndex={-1}
          />
          <motion.div
            ref={ref}
            tabIndex={-1}
            role="dialog"
            aria-modal="true"
            aria-labelledby={titleId}
            data-testid={testId}
            className="fixed inset-x-0 bottom-0 z-50 flex max-h-[85dvh] flex-col rounded-t-sheet bg-raised pb-safe shadow-mini outline-none"
            initial={reduced ? { opacity: 0 } : { y: "100%" }}
            animate={reduced ? { opacity: 1 } : { y: 0 }}
            exit={reduced ? { opacity: 0 } : { y: "100%" }}
            transition={reduced ? { duration: 0.15 } : { type: "spring", stiffness: 380, damping: 36 }}
            drag={reduced ? false : "y"}
            dragControls={drag}
            dragListener={false}
            dragConstraints={{ top: 0, bottom: 0 }}
            dragElastic={{ top: 0, bottom: 0.6 }}
            onDragEnd={(_, info) => {
              if (info.offset.y > 80 || info.velocity.y > 600) onClose();
            }}
          >
            <div
              className="shrink-0 cursor-grab touch-none select-none"
              data-testid="sheet-grip"
              onPointerDown={(e) => {
                if (!reduced) drag.start(e);
              }}
            >
              <div className="mx-auto mt-2 h-1 w-sheet-handle rounded-pill bg-stroke" aria-hidden="true" />
              <h2 id={titleId} className="screen-margin clamp-1 pb-2 pt-3 text-title-2 text-primary">
                {title}
              </h2>
            </div>
            <div className="screen-margin min-h-0 flex-1 overflow-y-auto overscroll-contain pb-4">{children}</div>
          </motion.div>
        </>
      ) : null}
    </AnimatePresence>
  );
}
