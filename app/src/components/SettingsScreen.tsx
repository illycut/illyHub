"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { AmpIcon, ChevronLeftIcon, ChevronRightIcon, ExternalLinkIcon, HubIcon, RefreshIcon, SpeakerIcon } from "./icons";
import { ServiceBadge } from "./ServiceBadge";
import { Sheet } from "./Sheet";
import { TextSkeleton } from "./Skeleton";
import { library, LibraryError, type AccountStatus, type AuthStart, type Service } from "@/lib/hub/library";
import { useLibrary, errorMessage } from "@/lib/library/store";
import { useHub } from "@/lib/hub/store";
import { toast } from "@/lib/ui/toasts";

/** Fallback poll interval when the hub does not say (the start response carries `interval_s`). */
export const AUTH_POLL_FALLBACK_MS = 2000;
/** How long the reconnecting notice waits for the socket before giving up. */
export const RESTART_TIMEOUT_MS = 20_000;
export const RESTART_FAILED_COPY = "The hub didn't come back. Check the Mac.";

const LABEL: Record<AccountStatus["service"], string> = {
  tidal: "Tidal",
  ytmusic: "YouTube Music",
  pandora: "Pandora",
  heos_account: "HEOS account",
};

export const COMING_LATER = "Coming later";

export function accountStatusLine(a: AccountStatus): string {
  if (a.state === "restoring") return "Reconnecting…";
  if (a.state === "pending") return "Waiting for approval…";
  if (!a.linked) return a.last_error ? `Not connected · ${a.last_error}` : "Not connected";
  return a.account_name ? `Connected · ${a.account_name}` : "Connected";
}

export function formatUptime(s: number | null): string {
  if (s == null) return "";
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

export type LinkPhase =
  | { kind: "idle" }
  | { kind: "starting" }
  | { kind: "code"; start: AuthStart }
  | { kind: "expired" }
  | { kind: "linked"; name: string | null }
  | { kind: "error"; message: string };

/**
 * Restart notice state machine: armed on confirm, waits for the socket to leave "open" and come
 * back, or gives up after RESTART_TIMEOUT_MS. Pure so it can be unit-tested.
 */
export type RestartState = { kind: "idle" } | { kind: "waiting"; sawClosed: boolean; since: number } | { kind: "failed" };
export function nextRestartState(cur: RestartState, phaseOpen: boolean, now: number): RestartState {
  if (cur.kind !== "waiting") return cur;
  if (now - cur.since >= RESTART_TIMEOUT_MS) return { kind: "failed" };
  if (!phaseOpen) return cur.sawClosed ? cur : { ...cur, sawClosed: true };
  return cur.sawClosed ? { kind: "idle" } : cur;
}

/** Row inside a grouped bg-raised card (design system §6.9): 56px, hairline inset past the icon column. */
function Row({
  icon,
  title,
  line,
  onPress,
  destructive,
  disabled,
  muted,
  trailingLabel,
  testId,
  skeleton,
}: {
  icon: React.ReactNode;
  title: string;
  line?: string | null;
  onPress?: () => void;
  destructive?: boolean;
  /** Disabled rows keep full opacity: title text-secondary, reason text-tertiary, no chevron (UX U5). */
  disabled?: boolean;
  /** Offline hardware: the whole row in text-tertiary (UX U8). */
  muted?: boolean;
  /** Trailing non-truncating micro label, e.g. "Offline". */
  trailingLabel?: string | null;
  testId?: string;
  skeleton?: boolean;
}) {
  const titleTone = destructive ? "text-error" : disabled || muted ? "text-secondary" : "text-primary";
  const lineTone = disabled || muted ? "text-tertiary" : "text-secondary";
  const content = (
    <>
      <span className={`flex w-7 shrink-0 items-center justify-center ${muted ? "text-tertiary" : ""}`} aria-hidden="true">
        {icon}
      </span>
      <span className="flex min-h-row min-w-0 flex-1 items-center gap-3">
        <span className="min-w-0 flex-1">
          {skeleton ? (
            <TextSkeleton lines={1} />
          ) : (
            <>
              <span className={`clamp-1 block text-body ${titleTone}`}>{title}</span>
              {line ? <span className={`clamp-1 block text-caption ${lineTone}`}>{line}</span> : null}
            </>
          )}
        </span>
        {trailingLabel ? <span className="shrink-0 whitespace-nowrap text-micro text-tertiary">{trailingLabel}</span> : null}
        {onPress && !disabled ? <ChevronRightIcon size={20} className="text-tertiary" /> : null}
      </span>
    </>
  );
  // Hairline inset past the icon column: 16 pad + 28 icon + 12 gap = 56px (UX U4). Last row: none.
  const cls = "settings-row flex min-h-row w-full items-center gap-3 px-4 text-left";
  return (
    <li className="relative">
      {onPress || disabled ? (
        <button type="button" className={cls} onClick={onPress} disabled={disabled} aria-disabled={disabled || undefined} data-testid={testId}>
          {content}
        </button>
      ) : (
        <div className={cls} data-testid={testId}>
          {content}
        </div>
      )}
    </li>
  );
}

function Group({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section aria-label={title} className="flex flex-col gap-2">
      <h2 className="px-1 text-title-2 text-primary">{title}</h2>
      <ul className="overflow-hidden rounded-control bg-raised">{children}</ul>
    </section>
  );
}

function hostOf(url: string): string {
  try {
    const u = new URL(url);
    return u.host.replace(/^www\./, "") + (u.pathname !== "/" ? u.pathname : "");
  } catch {
    return url;
  }
}

/**
 * Settings (design system §6.9, PRD SET-1..3): Accounts, Hub, Zones. Full-screen push; grouped
 * cards; destructive actions confirm in a bottom sheet, never a modal. `initialLink` starts the
 * Tidal device-code flow immediately (deep link from a "Connect Tidal" card).
 */
export function SettingsScreen({ initialLink = null }: { initialLink?: Service | null }) {
  const router = useRouter();
  const settings = useLibrary((s) => s.settings);
  const loadSettings = useLibrary((s) => s.loadSettings);
  const invalidateHome = useLibrary((s) => s.invalidateHome);
  const callOptions = useLibrary((s) => s.callOptions);
  const now = useLibrary((s) => s._deps.now);
  const phase = useHub((s) => s.phase);
  const [link, setLink] = useState<LinkPhase>({ kind: "idle" });
  const [linkService, setLinkService] = useState<Service | null>(null);
  const [confirmUnlink, setConfirmUnlink] = useState<Service | null>(null);
  const [confirmRestart, setConfirmRestart] = useState(false);
  const [restart, setRestart] = useState<RestartState>({ kind: "idle" });
  const pollTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const runId = useRef(0);

  useEffect(() => {
    void loadSettings();
  }, [loadSettings]);

  const stopPolling = useCallback(() => {
    runId.current += 1;
    if (pollTimer.current) clearTimeout(pollTimer.current);
    pollTimer.current = null;
  }, []);

  const startLink = useCallback(
    async (service: Service) => {
      stopPolling();
      const me = runId.current;
      const alive = () => runId.current === me;
      setLinkService(service);
      setLink({ kind: "starting" });
      try {
        const start = await library.authStart(service, callOptions());
        if (!alive()) return;
        setLink({ kind: "code", start });
        const deadline = now() + start.expires_in_s * 1000;
        const intervalMs = start.interval_s * 1000;
        const poll = async () => {
          if (!alive()) return;
          if (now() >= deadline) {
            setLink({ kind: "expired" });
            return;
          }
          try {
            const st = await library.authStatus(service, callOptions());
            if (!alive()) return;
            if (st.linked) {
              setLink({ kind: "linked", name: st.account_name });
              await loadSettings(true);
              invalidateHome();
              return;
            }
            if (st.last_error && !st.pending) {
              setLink({ kind: "error", message: st.last_error });
              return;
            }
          } catch {
            // transient; keep polling until the deadline
          }
          if (alive()) pollTimer.current = setTimeout(() => void poll(), intervalMs);
        };
        pollTimer.current = setTimeout(() => void poll(), intervalMs);
      } catch (e) {
        if (alive()) setLink({ kind: "error", message: errorMessage(e) });
      }
    },
    [callOptions, invalidateHome, loadSettings, now, stopPolling],
  );

  useEffect(() => {
    // Deep link from a "Connect …" card: start the flow after mount, as if the row were tapped.
    const t = initialLink ? setTimeout(() => void startLink(initialLink), 0) : null;
    return () => {
      if (t) clearTimeout(t);
      stopPolling();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- run once for the deep link
  }, []);

  const closeLink = () => {
    stopPolling();
    setLink({ kind: "idle" });
    setLinkService(null);
  };

  const unlink = async (service: Service) => {
    setConfirmUnlink(null);
    try {
      await library.authUnlink(service, callOptions());
      await loadSettings(true);
      invalidateHome();
    } catch (e) {
      toast(errorMessage(e));
    }
  };

  const doRestart = async () => {
    setConfirmRestart(false);
    setRestart({ kind: "waiting", sawClosed: false, since: now() });
    try {
      await library.restart(callOptions());
    } catch (e) {
      if (e instanceof LibraryError) {
        // The hub refused (e.g. 409 restart_disabled): nothing is restarting.
        setRestart({ kind: "idle" });
        toast(e.message);
        return;
      }
      // A transport error is the expected outcome: the process exits mid-request.
    }
  };

  // Restart notice: advance on phase changes and on a timer while waiting.
  useEffect(() => {
    if (restart.kind !== "waiting") return;
    const tick = () => setRestart((cur) => nextRestartState(cur, useHub.getState().phase === "open", now()));
    tick();
    const id = setInterval(tick, 500);
    return () => clearInterval(id);
  }, [restart.kind, phase, now]);

  useEffect(() => {
    if (restart.kind === "idle") return;
    if (restart.kind === "failed") toast(RESTART_FAILED_COPY);
  }, [restart.kind]);

  useEffect(() => {
    // Back from a completed restart: settings (version, uptime) changed.
    if (restart.kind === "idle" && settings.data) void loadSettings(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- only on the waiting -> idle edge
  }, [restart.kind]);

  const data = settings.data;
  const accounts: AccountStatus[] = data?.accounts.length
    ? data.accounts
    : (["tidal", "ytmusic", "pandora", "heos_account"] as AccountStatus["service"][]).map((service) => ({
        service,
        state: "unlinked",
        linked: false,
        account_name: null,
        expires_at: null,
        pending: null,
        last_error: null,
        linked_by_vendor: null,
      }));

  return (
    <div className="min-h-dvh bg-base pb-[calc(var(--size-mini-player)+var(--safe-bottom)+var(--space-4))]" data-testid="settings">
      <header className="screen-margin flex h-[calc(var(--size-target)+var(--space-2)+var(--safe-top))] items-end gap-2 pt-safe">
        <button type="button" className="hit-target -ml-3 flex items-center justify-center rounded-control text-secondary" aria-label="Back" onClick={() => router.back()} data-testid="back">
          <ChevronLeftIcon />
        </button>
        <h1 className="pb-2 text-title-1 text-primary">Settings</h1>
      </header>
      <main className="screen-margin flex flex-col gap-6 pt-4">
        {restart.kind === "waiting" ? (
          <p className="rounded-control bg-raised px-4 py-3 text-caption text-secondary" role="status" data-testid="restarting">
            Restarting the hub. This screen reconnects on its own.
          </p>
        ) : null}
        {restart.kind === "failed" ? (
          <p className="rounded-control bg-raised px-4 py-3 text-caption text-error" role="alert" data-testid="restart-failed">
            {RESTART_FAILED_COPY}
          </p>
        ) : null}
        {settings.error && !data ? (
          <p className="text-caption text-error" role="alert">
            {settings.error}
          </p>
        ) : null}
        <Group title="Accounts">
          {!data && settings.loading ? (
            <li className="px-4 py-3">
              <TextSkeleton lines={4} />
            </li>
          ) : (
            accounts.map((a) => {
              const svc = a.service;
              const isService = svc === "tidal" || svc === "ytmusic" || svc === "pandora";
              const laterPhase = svc === "ytmusic" || svc === "pandora";
              const restoring = a.state === "restoring";
              // A disabled row reads its reason, never "Connected" (UX U6, U7).
              const line = laterPhase ? COMING_LATER : accountStatusLine(a);
              return (
                <Row
                  key={svc}
                  icon={isService ? <ServiceBadge source={svc} size={28} /> : <AmpIcon size={22} className="text-secondary" />}
                  title={LABEL[svc]}
                  line={line}
                  skeleton={restoring}
                  disabled={laterPhase || svc === "heos_account" || restoring}
                  onPress={isService && !laterPhase ? () => (a.linked ? setConfirmUnlink(svc) : void startLink(svc)) : undefined}
                  testId={`account-${svc}`}
                />
              );
            })
          )}
        </Group>
        <Group title="Hub">
          <Row
            icon={<HubIcon size={22} className="text-secondary" />}
            title={data?.hub.address ?? "Hub"}
            line={
              [data?.hub.version ? `Version ${data.hub.version}` : null, data?.hub.uptime_s != null ? `up ${formatUptime(data.hub.uptime_s)}` : null, data?.hub.https ? "HTTPS" : null, data?.hub.fake_devices ? "fake devices" : null]
                .filter(Boolean)
                .join(" · ") || null
            }
            testId="hub-status"
          />
          <Row icon={<RefreshIcon size={22} className="text-secondary" />} title="Check for updates" line={COMING_LATER} disabled testId="check-updates" />
          <Row icon={<span className="block h-5 w-5" />} title="Restart hub" destructive onPress={() => setConfirmRestart(true)} disabled={restart.kind === "waiting"} testId="restart-hub" />
        </Group>
        <Group title="Zones">
          {!data && settings.loading ? (
            <li className="px-4 py-3">
              <TextSkeleton lines={3} />
            </li>
          ) : data && data.hardware.length ? (
            data.hardware.map((d) => (
              <Row
                key={d.id}
                icon={d.vendor === "heos" || d.vendor === "denon" ? <AmpIcon size={22} /> : <SpeakerIcon size={22} />}
                title={d.name}
                line={[d.vendor === "heos" ? "HEOS" : d.vendor ? d.vendor.charAt(0).toUpperCase() + d.vendor.slice(1) : null, d.kind === "zone" ? "Zone" : null, d.model, d.ip].filter(Boolean).join(" · ") || null}
                muted={!d.online}
                trailingLabel={d.online ? null : "Offline"}
                testId="hardware-row"
              />
            ))
          ) : (
            <li className="px-4 py-4 text-body text-secondary">No hardware discovered yet.</li>
          )}
        </Group>
      </main>

      {/* Link flow: device code + URL; polls status at the hub's interval until linked or expired. */}
      <Sheet open={link.kind !== "idle"} onClose={closeLink} title={`Connect ${linkService ? LABEL[linkService] : ""}`} testId="link-sheet">
        {link.kind === "starting" ? <TextSkeleton lines={3} /> : null}
        {link.kind === "code" ? (
          <div className="flex flex-col gap-4 pb-2">
            <p className="text-body text-secondary">Enter this code at the link below, then come back here. This sheet updates on its own.</p>
            <p className="numeric text-center text-display text-primary tracking-wide" data-testid="user-code">
              {link.start.user_code}
            </p>
            <a
              className="flex h-target items-center justify-center gap-2 rounded-control bg-overlay text-body text-primary"
              href={link.start.verification_url}
              target="_blank"
              rel="noreferrer"
              data-testid="open-verification"
            >
              Open {hostOf(link.start.verification_url)}
              <ExternalLinkIcon size={18} />
            </a>
            <p className="text-center text-micro text-tertiary" role="status">
              Waiting for {linkService ? LABEL[linkService] : "the service"}…
            </p>
          </div>
        ) : null}
        {link.kind === "expired" ? (
          <div className="flex flex-col gap-4 pb-2">
            <p className="text-body text-secondary" role="alert" data-testid="link-expired">
              This code expired.
            </p>
            <button type="button" className="flex h-target items-center justify-center rounded-control bg-overlay text-body text-primary" onClick={() => linkService && void startLink(linkService)} data-testid="new-code">
              Get a new code
            </button>
          </div>
        ) : null}
        {link.kind === "linked" ? (
          <div className="flex flex-col gap-4 pb-2">
            <p className="text-body text-primary" role="status" data-testid="link-done">
              Connected{link.name ? ` · ${link.name}` : ""}
            </p>
            <button type="button" className="flex h-target items-center justify-center rounded-control bg-overlay text-body text-primary" onClick={closeLink}>
              Done
            </button>
          </div>
        ) : null}
        {link.kind === "error" ? (
          <div className="flex flex-col gap-4 pb-2">
            <p className="text-body text-error" role="alert">
              {link.message}
            </p>
            <button type="button" className="flex h-target items-center justify-center rounded-control bg-overlay text-body text-primary" onClick={() => linkService && void startLink(linkService)}>
              Try again
            </button>
          </div>
        ) : null}
      </Sheet>

      <Sheet open={confirmUnlink !== null} onClose={() => setConfirmUnlink(null)} title={`Disconnect ${confirmUnlink ? LABEL[confirmUnlink] : ""}?`} testId="unlink-sheet">
        <p className="pb-4 text-body text-secondary">Playlists and albums from this account leave the home screen until you connect it again.</p>
        <div className="flex flex-col gap-2 pb-2">
          <button type="button" className="flex h-target items-center justify-center rounded-control text-body text-error" onClick={() => confirmUnlink && void unlink(confirmUnlink)} data-testid="confirm-unlink">
            Disconnect
          </button>
          <button type="button" className="flex h-target items-center justify-center rounded-control bg-overlay text-body text-primary" onClick={() => setConfirmUnlink(null)}>
            Keep it
          </button>
        </div>
      </Sheet>

      <Sheet open={confirmRestart} onClose={() => setConfirmRestart(false)} title="Restart the hub?" testId="restart-sheet">
        <p className="pb-4 text-body text-secondary">Playback keeps going on the speakers. Control comes back in a few seconds.</p>
        <div className="flex flex-col gap-2 pb-2">
          <button type="button" className="flex h-target items-center justify-center rounded-control text-body text-error" onClick={() => void doRestart()} data-testid="confirm-restart">
            Restart
          </button>
          <button type="button" className="flex h-target items-center justify-center rounded-control bg-overlay text-body text-primary" onClick={() => setConfirmRestart(false)}>
            Not now
          </button>
        </div>
      </Sheet>
    </div>
  );
}
