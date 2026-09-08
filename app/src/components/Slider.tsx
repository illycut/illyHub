"use client";
import { useCallback, useRef, useState } from "react";

/**
 * Touch slider for volume (design system §6.5): 4px track, 28px thumb, signal fill, 48px hit
 * zone. `onChange` fires during drag (callers coalesce), `onCommit` on release. Value is
 * announced to screen readers on commit only (§9).
 */
export function Slider({
  value,
  min = 0,
  max = 100,
  label,
  disabled = false,
  onChange,
  onCommit,
  fillClassName = "bg-signal",
  testId,
}: {
  value: number;
  min?: number;
  max?: number;
  label: string;
  disabled?: boolean;
  onChange?: (v: number) => void;
  onCommit?: (v: number) => void;
  fillClassName?: string;
  testId?: string;
}) {
  const trackRef = useRef<HTMLDivElement>(null);
  const [dragging, setDragging] = useState<number | null>(null);
  const shown = dragging ?? value;
  const pct = max > min ? ((shown - min) / (max - min)) * 100 : 0;

  const valueFromClientX = useCallback(
    (clientX: number) => {
      const el = trackRef.current;
      if (!el) return value;
      const r = el.getBoundingClientRect();
      if (r.width <= 0) return value;
      const t = Math.min(1, Math.max(0, (clientX - r.left) / r.width));
      return Math.round(min + t * (max - min));
    },
    [max, min, value],
  );

  const onPointerDown = (e: React.PointerEvent) => {
    if (disabled) return;
    try {
      (e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId);
    } catch {
      // capture is best-effort
    }
    const v = valueFromClientX(e.clientX);
    setDragging(v);
    onChange?.(v);
  };
  const onPointerMove = (e: React.PointerEvent) => {
    if (disabled || dragging === null) return;
    const v = valueFromClientX(e.clientX);
    setDragging(v);
    onChange?.(v);
  };
  const onPointerUp = () => {
    if (dragging === null) return;
    const v = dragging;
    setDragging(null);
    onCommit?.(v);
  };
  const onPointerCancel = () => {
    setDragging(null);
  };
  const onKeyDown = (e: React.KeyboardEvent) => {
    if (disabled) return;
    const step = e.shiftKey ? 10 : 1;
    let v = value;
    if (e.key === "ArrowRight" || e.key === "ArrowUp") v = Math.min(max, value + step);
    else if (e.key === "ArrowLeft" || e.key === "ArrowDown") v = Math.max(min, value - step);
    else if (e.key === "Home") v = min;
    else if (e.key === "End") v = max;
    else return;
    e.preventDefault();
    onChange?.(v);
    onCommit?.(v);
  };

  return (
    <div
      role="slider"
      tabIndex={disabled ? -1 : 0}
      aria-label={label}
      aria-valuemin={min}
      aria-valuemax={max}
      aria-valuenow={value}
      aria-valuetext={`${value}`}
      aria-disabled={disabled || undefined}
      data-testid={testId}
      className={`relative flex h-target w-full touch-none select-none items-center ${disabled ? "opacity-40" : ""}`}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerCancel}
      onKeyDown={onKeyDown}
    >
      <div ref={trackRef} className="relative h-slider-track w-full rounded-pill bg-stroke">
        <div className={`absolute inset-y-0 left-0 rounded-pill ${fillClassName}`} style={{ width: `${pct}%` }} />
        <div
          className="absolute top-1/2 h-slider-thumb w-slider-thumb -translate-x-1/2 -translate-y-1/2 rounded-pill bg-primary"
          style={{ left: `${pct}%` }}
          aria-hidden="true"
        />
      </div>
    </div>
  );
}
