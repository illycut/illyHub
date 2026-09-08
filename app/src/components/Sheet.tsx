"use client";
import { useEffect, useId, useRef } from "react";
import { AnimatePresence, motion, useDragControls } from "framer-motion";
import { useReducedMotion } from "@/lib/reducedMotion";
import { useScrollLock } from "@/lib/scrollLock";

/**
 * Bottom sheet primitive (design system §4 radius-sheet, §6.5/§6.6). Never a modal dialog for
 * playback actions; this is a sheet with a scrim, dismissed by scrim tap, Escape, or a swipe down
 * started on the handle/title. The content scrolls inside with overscroll containment and the
 * page behind is scroll-locked while open.
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
  const drag = useDragControls();
  useScrollLock(open);
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    ref.current?.focus();
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);
  return (
    <AnimatePresence>
      {open ? (
        <>
          <motion.button
            type="button"
            aria-label="Close"
            className="fixed inset-0 z-40 bg-base/60"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.15 }}
            onClick={onClose}
          />
          <motion.div
            ref={ref}
            tabIndex={-1}
            role="dialog"
            aria-modal="false"
            aria-labelledby={titleId}
            data-testid={testId}
            className="fixed inset-x-0 bottom-0 z-40 flex max-h-[85dvh] flex-col rounded-t-sheet bg-raised pb-safe shadow-mini outline-none"
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
              <h2 id={titleId} className="screen-margin pb-2 pt-3 text-title-2 text-primary">
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
