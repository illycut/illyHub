"use client";
import { memo, useEffect } from "react";
import { ChevronRightIcon, AmpIcon, SpeakerIcon } from "./icons";
import { ServiceBadge, serviceLabel } from "./ServiceBadge";
import { artUrl } from "@/lib/hub/config";
import type { ArtRef } from "@/lib/hub/types";

export let artCardRenders = 0;
export function resetArtCardRenders(): void {
  artCardRenders = 0;
}

export interface ArtCardProps<T> {
  /** Passed back to the stable handlers so parents need no per-item closures. */
  payload: T;
  title: string;
  subtitle?: string | null;
  art: ArtRef;
  service: string | null;
  /** Vendor glyph in the art corner: where this last played (recents rail, §6.2). */
  lastVendor?: "heos" | "sonos" | null;
  /** Room name for the accessible label ("Last played on Kitchen"). */
  lastRoom?: string | null;
  onPress: (payload: T) => void;
  /** Secondary affordance opening the detail view; omitted for items with no detail. */
  onDetail?: (payload: T) => void;
  testId?: string;
}

/** "Play {title}, {subtitle}. {Service}. Last played on {room}" (UX U9). */
export function artCardLabel(title: string, subtitle: string | null | undefined, service: string | null, lastRoom: string | null | undefined): string {
  const parts = [`Play ${title}${subtitle ? `, ${subtitle}` : ""}`];
  const svc = serviceLabel(service);
  if (svc) parts.push(svc);
  if (lastRoom) parts.push(`Last played on ${lastRoom}`);
  return parts.join(". ");
}

function ArtCardInner<T>({ payload, title, subtitle, art, service, lastVendor, lastRoom, onPress, onDetail, testId }: ArtCardProps<T>) {
  useEffect(() => {
    artCardRenders += 1; // test instrumentation: counts committed renders
  });
  const src = artUrl(art, 320);
  const Glyph = lastVendor === "heos" ? AmpIcon : lastVendor === "sonos" ? SpeakerIcon : null;
  return (
    <div className="flex shrink-0 flex-col" data-testid={testId}>
      <button
        type="button"
        className="block w-full rounded-art text-left outline-offset-2 transition-transform duration-press active:scale-[0.97]"
        onClick={() => onPress(payload)}
        aria-label={artCardLabel(title, subtitle, service, lastRoom)}
      >
        <span className="relative block aspect-square w-full overflow-hidden rounded-art bg-overlay">
          {src ? (
            // eslint-disable-next-line @next/next/no-img-element
            <img src={src} alt="" draggable={false} loading="lazy" className="h-full w-full object-cover" />
          ) : null}
          {service ? (
            <span className="absolute bottom-2 left-2">
              <ServiceBadge source={service} size={22} />
            </span>
          ) : null}
          {Glyph ? (
            <span className="absolute right-2 top-2 flex h-6 w-6 items-center justify-center rounded-pill bg-base/60 text-secondary" aria-hidden="true">
              <Glyph size={14} />
            </span>
          ) : null}
        </span>
      </button>
      {/* Text row is a full 48px target tall so the chevron sits inside the card box, >= 8px
          below the art button and never overhanging the gutter (UX U2). */}
      <div className="mt-2 flex min-h-target items-start gap-1">
        {/* The title block plays too. It used to be an inert div, so a tap on the words did
            nothing while the artwork right above worked — a dead 48px region that reads as
            broken. Not focusable and hidden from assistive tech on purpose: it is a second hit
            area for the art button's action, and that button already carries the full label,
            so announcing it twice would be noise. */}
        <button
          type="button"
          className="min-w-0 flex-1 pt-1 text-left"
          onClick={() => onPress(payload)}
          tabIndex={-1}
          aria-hidden="true"
          data-testid="card-text"
        >
          <div className="clamp-1 text-body text-primary">{title}</div>
          {subtitle ? <div className="clamp-1 text-caption text-secondary">{subtitle}</div> : null}
        </button>
        {onDetail ? (
          <button
            type="button"
            className="flex h-target w-target shrink-0 items-center justify-center rounded-control text-tertiary"
            aria-label={`Open ${title}`}
            onClick={() => onDetail(payload)}
            data-testid="card-detail"
          >
            <ChevronRightIcon size={20} />
          </button>
        ) : null}
      </div>
    </div>
  );
}

/**
 * Art card (design system §6.1): square artwork with radius-art, title in body, subtitle in
 * caption, service badge chip overlaid bottom-left at 8px inset. Pressed: scale 0.97 over
 * bg-overlay. No hover shadows. Tapping the artwork *or the title* plays (through the target
 * picker); the chevron opens the detail view. Memoised: with stable handlers it re-renders only when its item changes.
 */
export const ArtCard = memo(ArtCardInner) as typeof ArtCardInner;
