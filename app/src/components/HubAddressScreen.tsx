"use client";
import { useEffect, useId, useRef, useState } from "react";
import { HUB_ADDRESS_HINT, HUB_ADDRESS_PLACEHOLDER, HUB_ADDRESS_SAVE_FAILED, clearHubBase, normalizeHubBase, probeHub, readHubBase, writeHubBase, type ProbeResult } from "@/lib/hub/hubBase";

export type HubAddressPhase =
  | { kind: "idle" }
  | { kind: "probing" }
  | { kind: "found"; address: string; version: string | null }
  | { kind: "error"; message: string };

export const HUB_ADDRESS_TITLE = "Where is the hub?";
export const HUB_ADDRESS_CONNECT = "Connect";
export const HUB_ADDRESS_USE = "Use this address";
export const HUB_ADDRESS_FORGET = "Forget this address";
export const HUB_ADDRESS_CANCEL = "Cancel";

/** What the found line says: version when the hub reports one, otherwise the address alone. */
export function foundLine(address: string, version: string | null): string {
  return version ? `Found the hub at ${address} · version ${version}` : `Found the hub at ${address}`;
}

/**
 * First-run and Settings screen for the hub address (docs/android.md). One field, a Connect
 * button that probes /api/health, one factual line for the result, then a confirm. Only the
 * Android shell shows it on first run; in a browser the hub is the page origin.
 */
export function HubAddressScreen({
  onSaved,
  onCancel,
  onForget,
  fetcher,
  initial,
  embedded = false,
}: {
  /** Called after the address is stored. The shell reloads so every module picks it up. */
  onSaved: (address: string) => void;
  /** Present when opened from Settings; absent on first run (nothing to go back to). */
  onCancel?: () => void;
  /** Present when an address is stored; clears it. */
  onForget?: () => void;
  fetcher?: typeof fetch;
  initial?: string | null;
  /** Inside a Settings sheet: no full-height centring or safe-area padding. */
  embedded?: boolean;
}) {
  const [text, setText] = useState(initial ?? readHubBase() ?? "");
  const [phase, setPhase] = useState<HubAddressPhase>({ kind: "idle" });
  const run = useRef(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const hintId = useId();
  const statusId = useId();

  useEffect(() => {
    // First run: the field is the only thing on screen. From Settings the sheet owns focus.
    if (!embedded) inputRef.current?.focus();
  }, [embedded]);

  async function connect() {
    const me = ++run.current;
    setPhase({ kind: "probing" });
    const result: ProbeResult = await probeHub(text, fetcher);
    if (me !== run.current) return;
    setPhase(result.ok ? { kind: "found", address: result.address, version: result.version } : { kind: "error", message: result.message });
  }

  function save() {
    if (phase.kind !== "found") return;
    if (!writeHubBase(phase.address)) {
      setPhase({ kind: "error", message: HUB_ADDRESS_SAVE_FAILED });
      return;
    }
    onSaved(phase.address);
  }

  function forget() {
    clearHubBase();
    onForget?.();
  }

  const canConnect = text.trim().length > 0 && phase.kind !== "probing";
  let preview: string | null = null;
  try {
    preview = text.trim() ? normalizeHubBase(text) : null;
  } catch {
    preview = null;
  }

  return (
    <section className={embedded ? "flex flex-col gap-6" : "screen-margin flex min-h-dvh flex-col justify-center gap-6 pb-safe pt-safe"} data-testid="hub-address" aria-labelledby={embedded ? undefined : `${hintId}-title`} aria-label={embedded ? HUB_ADDRESS_TITLE : undefined}>
      <div className="flex flex-col gap-2">
        {embedded ? null : (
          <h1 id={`${hintId}-title`} className="text-title-1 text-primary">
            {HUB_ADDRESS_TITLE}
          </h1>
        )}
        <p id={hintId} className="text-caption text-secondary">
          {HUB_ADDRESS_HINT}
        </p>
      </div>

      <form
        className="flex flex-col gap-3"
        onSubmit={(e) => {
          e.preventDefault();
          if (phase.kind === "found") save();
          else if (canConnect) void connect();
        }}
      >
        <label className="flex flex-col gap-2">
          <span className="text-caption text-secondary">Hub address</span>
          <input
            ref={inputRef}
            type="url"
            inputMode="url"
            autoCapitalize="none"
            autoCorrect="off"
            autoComplete="off"
            spellCheck={false}
            enterKeyHint="go"
            value={text}
            placeholder={HUB_ADDRESS_PLACEHOLDER}
            aria-describedby={`${hintId} ${statusId}`}
            aria-invalid={phase.kind === "error" || undefined}
            className="min-h-target w-full rounded-control bg-raised px-4 text-body text-primary outline-none placeholder:text-tertiary focus-visible:ring-2 focus-visible:ring-signal"
            onChange={(e) => {
              setText(e.target.value);
              if (phase.kind !== "idle") setPhase({ kind: "idle" });
            }}
            data-testid="hub-address-input"
          />
        </label>
        {preview && preview !== text.trim() && phase.kind === "idle" ? (
          <p className="text-micro text-tertiary numeric" data-testid="hub-address-preview">
            Will connect to {preview}
          </p>
        ) : null}

        <p id={statusId} className={`min-h-[22px] text-caption ${phase.kind === "error" ? "text-error" : "text-secondary"}`} role={phase.kind === "error" ? "alert" : "status"} data-testid="hub-address-status">
          {phase.kind === "probing" ? "Looking for the hub…" : null}
          {phase.kind === "found" ? foundLine(phase.address, phase.version) : null}
          {phase.kind === "error" ? phase.message : null}
        </p>

        {phase.kind === "found" ? (
          <button type="submit" className="flex h-target w-full items-center justify-center rounded-control bg-overlay text-body font-semibold text-primary" data-testid="hub-address-use">
            {HUB_ADDRESS_USE}
          </button>
        ) : (
          <button type="submit" className="flex h-target w-full items-center justify-center rounded-control bg-overlay text-body font-semibold text-primary disabled:text-secondary" disabled={!canConnect} aria-disabled={!canConnect || undefined} data-testid="hub-address-connect">
            {phase.kind === "probing" ? "Looking for the hub…" : HUB_ADDRESS_CONNECT}
          </button>
        )}

        {(onCancel || onForget) ? (
          <div className="flex items-center justify-between gap-gap-min pt-2">
            {onCancel ? (
              <button type="button" className="flex h-target items-center rounded-control px-3 text-body text-secondary" onClick={onCancel} data-testid="hub-address-cancel">
                {HUB_ADDRESS_CANCEL}
              </button>
            ) : (
              <span />
            )}
            {onForget ? (
              <button type="button" className="flex h-target items-center rounded-control px-3 text-body text-error" onClick={forget} data-testid="hub-address-forget">
                {HUB_ADDRESS_FORGET}
              </button>
            ) : null}
          </div>
        ) : null}
      </form>
    </section>
  );
}
