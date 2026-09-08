"use client";
import { useEffect, useRef } from "react";
import { ExternalLinkIcon } from "./icons";
import { Sheet } from "./Sheet";
import { TextSkeleton } from "./Skeleton";
import type { AuthStart, Service } from "@/lib/hub/library";
import { SERVICE_LABEL } from "@/lib/services";

export type LinkPhase =
  | { kind: "idle" }
  | { kind: "starting" }
  | { kind: "code"; start: AuthStart }
  | { kind: "expired" }
  | { kind: "linked"; name: string | null }
  | { kind: "error"; message: string; code?: string | null };

/** The hub itself lacks OAuth client credentials for this service: nothing the user can retry. */
export const NEEDS_CLIENT_CONFIG = "needs_client_config";

export function hostOf(url: string): string {
  try {
    const u = new URL(url);
    return u.host.replace(/^www\./, "") + (u.pathname !== "/" ? u.pathname : "");
  } catch {
    return url;
  }
}

const primary = "flex h-target items-center justify-center gap-2 rounded-control bg-overlay text-body text-primary";

/**
 * Device-code link sheet shared by every hub-linked service (Tidal, YouTube Music). One component,
 * parameterised by `service`; the polling lives in the caller (`useLinkFlow` in SettingsScreen)
 * so this stays a pure view of a `LinkPhase`.
 */
export function LinkSheet({
  service,
  phase,
  onClose,
  onRetry,
}: {
  service: Service | null;
  phase: LinkPhase;
  onClose: () => void;
  /** Starts a fresh code for the same service (expired or failed). */
  onRetry: () => void;
}) {
  // The caller keeps `service` set through the exit animation, so the title never flashes "Connect ".
  const label = service ? SERVICE_LABEL[service] : "";
  // Terminal phases move focus to their primary action so a screen reader lands on the outcome (UX U6).
  const primaryRef = useRef<HTMLButtonElement | null>(null);
  useEffect(() => {
    if (phase.kind === "linked" || phase.kind === "expired" || phase.kind === "error") primaryRef.current?.focus();
  }, [phase.kind]);
  const unrecoverable = phase.kind === "error" && phase.code === NEEDS_CLIENT_CONFIG;
  return (
    <Sheet open={phase.kind !== "idle"} onClose={onClose} title={`Connect ${label}`} testId="link-sheet">
      {phase.kind === "starting" ? <TextSkeleton lines={3} /> : null}
      {phase.kind === "code" ? (
        <div className="flex flex-col gap-4 pb-2">
          <p className="text-body text-secondary">Enter this code at the link below, then come back here. This sheet updates on its own.</p>
          <p className="numeric text-center text-display text-primary tracking-wide" data-testid="user-code">
            {phase.start.user_code}
          </p>
          <a className={primary} href={phase.start.verification_url} target="_blank" rel="noreferrer" data-testid="open-verification">
            Open {hostOf(phase.start.verification_url)}
            <ExternalLinkIcon size={18} />
          </a>
          <p className="text-center text-micro text-tertiary" role="status">
            Waiting for {label || "the service"}…
          </p>
        </div>
      ) : null}
      {phase.kind === "expired" ? (
        <div className="flex flex-col gap-4 pb-2">
          <p className="text-body text-secondary" role="alert" data-testid="link-expired">
            This code expired.
          </p>
          <button ref={primaryRef} type="button" className={primary} onClick={onRetry} data-testid="new-code">
            Get a new code
          </button>
        </div>
      ) : null}
      {phase.kind === "linked" ? (
        <div className="flex flex-col gap-4 pb-2">
          <p className="text-body text-primary" role="status" data-testid="link-done">
            Connected{phase.name ? ` · ${phase.name}` : ""}
          </p>
          <button ref={primaryRef} type="button" className={primary} onClick={onClose} data-testid="link-close">
            Done
          </button>
        </div>
      ) : null}
      {phase.kind === "error" ? (
        <div className="flex flex-col gap-4 pb-2">
          <p className="text-body text-error" role="alert" data-testid="link-error">
            {phase.message}
          </p>
          {unrecoverable ? (
            // Hub setup, not a user action: the full hub sentence stays here, the row only says "Hub setup needed".
            <button ref={primaryRef} type="button" className={primary} onClick={onClose} data-testid="link-close">
              Close
            </button>
          ) : (
            <button ref={primaryRef} type="button" className={primary} onClick={onRetry} data-testid="link-retry">
              Try again
            </button>
          )}
        </div>
      ) : null}
    </Sheet>
  );
}
