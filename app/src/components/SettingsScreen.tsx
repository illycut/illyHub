"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { AmpIcon, ChevronDownIcon, ChevronLeftIcon, ChevronRightIcon, ChevronUpIcon, HubIcon, RefreshIcon, SpeakerIcon } from "./icons";
import { UPDATE_CHECK_DEBOUNCE_MS, UPDATE_CHECK_FAILED, UPDATE_ROW_TITLE, fromApplyRefusal, fromCheck, fromStatus, onPhase as updateOnPhase, onPollUnreachable, onSocketClosedWhileApplying, updateLog, updateRowLine, updateRowTappable, waitForSocketBounce, type UpdateRowState } from "@/lib/update";
import { ServiceBadge } from "./ServiceBadge";
import { Sheet } from "./Sheet";
import { TextSkeleton } from "./Skeleton";
import { LinkSheet, NEEDS_CLIENT_CONFIG, type LinkPhase } from "./LinkSheet";
import { library, LibraryError, type AccountStatus, type AirPlayStatus, type Service } from "@/lib/hub/library";
import { VENDOR_APP, VENDOR_LABEL } from "@/lib/services";
import { SERVICE_LABEL, isHubLinked } from "@/lib/services";
import { AIRPLAY_DISCLOSURE, AIRPLAY_OUTPUTS_NOTE, AIRPLAY_OUTPUTS_TITLE, AIRPLAY_ROW_TITLE, airplayRowStatus } from "@/lib/airplay";
import { AirPlayGlyph } from "./PandoraSyncChip";
import { CheckIcon } from "./icons";
import { useLibrary, errorMessage } from "@/lib/library/store";
import { useHub } from "@/lib/hub/store";
import { toast } from "@/lib/ui/toasts";

/** How long the reconnecting notice waits for the socket before giving up. */
export const RESTART_TIMEOUT_MS = 20_000;
export const RESTART_FAILED_COPY = "The hub didn't come back. Check the Mac.";

const LABEL: Record<AccountStatus["service"], string> = { ...SERVICE_LABEL, heos_account: "HEOS account" };

export const HUB_SETUP_NEEDED = "Hub setup needed";

/**
 * Pandora is linked inside each vendor's app, never through the hub, so its row states where it is
 * linked ("Linked in the HEOS app · not in Sonos") or the hub's auth-fault sentence when one is set.
 */
export function pandoraStatusLine(a: AccountStatus): string {
  if (a.last_error) return a.last_error;
  const l = a.linked_by_vendor;
  if (!l) return a.linked ? "Linked in the vendor apps" : "Not linked in the HEOS or Sonos app";
  const linked = (["heos", "sonos"] as const).filter((v) => l[v] === true);
  const not = (["heos", "sonos"] as const).filter((v) => l[v] === false);
  if (linked.length === 0) return `Not linked in the ${VENDOR_APP.heos} or the ${VENDOR_APP.sonos}`;
  const head = `Linked in the ${linked.map((v) => VENDOR_APP[v]).join(" and the ")}`;
  return not.length ? `${head} · not in ${not.map((v) => VENDOR_LABEL[v]).join(" or ")}` : head;
}

export function accountStatusLine(a: AccountStatus): string {
  if (a.state === "restoring") return "Reconnecting…";
  if (a.state === "pending") return "Waiting for approval…";
  if (!a.linked) {
    // Missing OAuth client credentials on the hub: a short row line; the full sentence lives in the sheet (UX U4).
    if (a.last_error_code === NEEDS_CLIENT_CONFIG) return `Not connected · ${HUB_SETUP_NEEDED}`;
    return a.last_error ? `Not connected · ${a.last_error}` : "Not connected";
  }
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

export type { LinkPhase };

/**
 * Restart notice state machine: armed on confirm, waits for the socket to leave "open" and come
 * back, or gives up after RESTART_TIMEOUT_MS. Pure so it can be unit-tested.
 */
export type RestartState = { kind: "idle" } | { kind: "waiting"; sawClosed: boolean; since: number } | { kind: "failed" };
export function nextRestartState(cur: RestartState, phaseOpen: boolean, now: number): RestartState {
  if (cur.kind !== "waiting") return cur;
  const b = waitForSocketBounce(cur, phaseOpen, now, RESTART_TIMEOUT_MS);
  if (b.kind === "timeout") return { kind: "failed" };
  if (b.kind === "done") return { kind: "idle" };
  return b.sawClosed === cur.sawClosed ? cur : { ...cur, sawClosed: b.sawClosed };
}

/** Last successful update check per session (module-level): a mount within the window re-uses it. */
let lastCheck: { at: number; state: UpdateRowState } | null = null;
/** Test seam. */
export function _resetUpdateCheckCache(): void {
  lastCheck = null;
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
  wrapLine = false,
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
  /** Let a load-bearing line wrap instead of clamping (e.g. an AirPlay unavailability reason, UX U9). */
  wrapLine?: boolean;
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
              {line ? <span className={`${wrapLine ? "" : "clamp-1 "}block text-caption ${lineTone}`}>{line}</span> : null}
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


/**
 * Settings (design system §6.9, PRD SET-1..3): Accounts, Hub, Zones. Full-screen push; grouped
 * cards; destructive actions confirm in a bottom sheet, never a modal. `initialLink` starts that
 * service's device-code flow immediately (deep link from a "Connect …" card). Tidal and YouTube
 * Music share the one flow and the one sheet (`LinkSheet`); Pandora is linked in the vendor apps.
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
  // AirPlay outputs sheet (Phase 7, read-only): fetched when opened, never cached.
  const [outputsOpen, setOutputsOpen] = useState(false);
  const [outputs, setOutputs] = useState<{ loading: boolean; data: AirPlayStatus | null; error: string | null }>({ loading: false, data: null, error: null });
  const outputsRun = useRef(0);
  const openOutputs = async () => {
    // A re-open while a fetch is in flight must not let the stale answer land (runId guard).
    const me = ++outputsRun.current;
    setOutputsOpen(true);
    setOutputs({ loading: true, data: null, error: null });
    try {
      const data = await library.airplay(callOptions());
      if (outputsRun.current === me) setOutputs({ loading: false, data, error: null });
    } catch (e) {
      if (outputsRun.current === me) setOutputs({ loading: false, data: null, error: errorMessage(e) });
    }
  };
  const pollTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const runId = useRef(0);

  // Self-update (PRD SET-2, Phase 8): check on mount (debounced per session); tapping an available
  // update confirms in a sheet, then polls the job until the hub restarts and comes back (or fails /
  // rolls back). Updates off on the hub is a normal row state in the hub's own words.
  const [update, setUpdate] = useState<UpdateRowState>(() => (lastCheck && now() - lastCheck.at < UPDATE_CHECK_DEBOUNCE_MS ? lastCheck.state : { kind: "idle" }));
  const [confirmUpdate, setConfirmUpdate] = useState(false);
  // The log disclosure is keyed to the terminal state that produced it: leaving that state folds it away.
  const [showLog, setShowLog] = useState<string | null>(null);
  const updateRun = useRef(0);
  const prevUpdateKind = useRef<UpdateRowState["kind"]>(update.kind);
  // The version the hub ran before an update, read once when the job starts (not an effect dependency).
  const versionBefore = useRef<string | null>(null);
  const checkUpdate = useCallback(
    async (force = false) => {
      if (!force && lastCheck && now() - lastCheck.at < UPDATE_CHECK_DEBOUNCE_MS) {
        setUpdate(lastCheck.state);
        return;
      }
      const me = ++updateRun.current;
      setUpdate({ kind: "checking" });
      try {
        const c = await library.updateCheck(callOptions());
        if (updateRun.current !== me) return;
        const next = fromCheck(c);
        lastCheck = { at: now(), state: next };
        setUpdate(next);
      } catch (e) {
        if (updateRun.current !== me) return;
        // A hub with updates turned off answers 409 update_disabled on check too (PRD SET-2 [1.2]).
        if (e instanceof LibraryError && e.code === "update_disabled") {
          const next = fromApplyRefusal(e.code, e.message, now());
          lastCheck = { at: now(), state: next };
          setUpdate(next);
          return;
        }
        if (process.env.NODE_ENV !== "production") console.warn(`[illyhub] update check: ${errorMessage(e)}`);
        setUpdate({ kind: "error", message: UPDATE_CHECK_FAILED });
      }
    },
    [callOptions, now],
  );
  useEffect(() => {
    // Deferred like the deep-link effect: the check is network work, not a render-time state sync.
    const t = setTimeout(() => void checkUpdate(), 0);
    return () => clearTimeout(t);
  }, [checkUpdate]);
  const applyUpdate = async () => {
    setConfirmUpdate(false);
    const me = ++updateRun.current;
    versionBefore.current = useLibrary.getState().settings.data?.hub.version ?? null;
    setUpdate({ kind: "applying", jobId: null, since: now(), failedPolls: 0 });
    try {
      const st = await library.updateApply(callOptions());
      if (updateRun.current === me) setUpdate((cur) => (cur.kind === "applying" ? { ...cur, jobId: st.job_id } : cur));
    } catch (e) {
      if (updateRun.current !== me) return;
      if (e instanceof LibraryError) {
        // update_running → adopt the live job; update_disabled → the hub's sentence, not an error;
        // anything else (e.g. a refused remote URL) → the hub's message verbatim.
        const next = fromApplyRefusal(e.code, e.message, now());
        if (next.kind === "disabled") lastCheck = { at: now(), state: next };
        setUpdate(next);
        return;
      }
      // Transport error: the hub may already be restarting; the status poll below decides.
    }
  };
  // Poll the job while applying. A hub that cannot be reached twice in a row is restarting; the
  // socket leaving "open" says the same thing sooner.
  useEffect(() => {
    if (update.kind !== "applying") return;
    let cancelled = false;
    const tick = async () => {
      try {
        const st = await library.updateStatus(callOptions());
        if (!cancelled) setUpdate((cur) => fromStatus(cur, st, now(), versionBefore.current));
      } catch {
        if (!cancelled) setUpdate((cur) => onPollUnreachable(cur, now(), versionBefore.current));
      }
    };
    void tick();
    const id = setInterval(() => void tick(), 2000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [update.kind, callOptions, now]);
  useEffect(() => {
    if (update.kind === "applying" && phase !== "open") setUpdate((cur) => onSocketClosedWhileApplying(cur, now(), versionBefore.current));
    // eslint-disable-next-line react-hooks/exhaustive-deps -- reacts to the socket phase only
  }, [phase]);
  // While reconnecting, follow the socket: it must leave "open" (hub exited) and come back (new version).
  useEffect(() => {
    if (update.kind !== "reconnecting") return;
    const tick = () => setUpdate((cur) => updateOnPhase(cur, useHub.getState().phase === "open", now(), useLibrary.getState().settings.data?.hub.version ?? null));
    tick();
    const id = setInterval(tick, 500);
    return () => clearInterval(id);
  }, [update.kind, phase, now]);
  useEffect(() => {
    // Back from an update: settings (version, uptime) changed; read the version the hub now runs.
    if (update.kind === "succeeded") {
      lastCheck = null;
      void loadSettings(true).then(() => setUpdate((cur) => (cur.kind === "succeeded" ? { ...cur, version: useLibrary.getState().settings.data?.hub.version ?? cur.version } : cur)));
    }
    // A finished job that says "already up to date" comes back from the poll as `checking`: run the
    // check (only on that edge; a tap already runs its own check).
    if (update.kind === "checking" && prevUpdateKind.current === "applying") void checkUpdate(true);
    prevUpdateKind.current = update.kind;
    // eslint-disable-next-line react-hooks/exhaustive-deps -- only on the kind edges
  }, [update.kind]);
  const logKey = updateLog(update) ? `${update.kind}:${updateLog(update)}` : null;
  const logOpen = logKey !== null && showLog === logKey;

  // Hub stats (Phase 8, ai-dev #73): one paragraph from GET /api/metrics/summary behind a disclosure.
  const [stats, setStats] = useState<{ open: boolean; loading: boolean; text: string | null; error: string | null }>({ open: false, loading: false, text: null, error: null });
  const statsRun = useRef(0);
  const toggleStats = async () => {
    if (stats.open) {
      setStats((cur) => ({ ...cur, open: false }));
      return;
    }
    const me = ++statsRun.current;
    setStats({ open: true, loading: true, text: null, error: null });
    try {
      const text = await library.metricsSummary(callOptions());
      if (statsRun.current === me) setStats({ open: true, loading: false, text: text || "No stats yet.", error: null });
    } catch (e) {
      if (statsRun.current === me) setStats({ open: true, loading: false, text: null, error: errorMessage(e) });
    }
  };

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
        if (alive()) setLink({ kind: "error", message: errorMessage(e), code: e instanceof LibraryError ? e.code : null });
      }
    },
    [callOptions, invalidateHome, loadSettings, now, stopPolling],
  );

  // Deep link from a "Connect …" card: once settings are known, start the flow as if the row were
  // tapped, unless the account is already linked (then the row simply reads Connected).
  const deepLinked = useRef(false);
  const accountsData = settings.data?.accounts;
  useEffect(() => {
    if (!initialLink || deepLinked.current || !accountsData) return;
    const account = accountsData.find((a) => a.service === initialLink);
    if (account?.linked) {
      deepLinked.current = true;
      return;
    }
    const t = setTimeout(() => {
      deepLinked.current = true;
      void startLink(initialLink);
    }, 0);
    return () => clearTimeout(t);
  }, [initialLink, accountsData, startLink]);

  useEffect(() => () => stopPolling(), [stopPolling]);

  const closeLink = () => {
    stopPolling();
    setLink({ kind: "idle" });
    // `linkService` stays set so the sheet keeps its title through the exit animation.
  };

  const unlink = async (service: Service) => {
    setConfirmUnlink(null);
    // Unlinking ends any pending code flow for the service and clears the sheet.
    closeLink();
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
        last_error_code: null,
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
              // Live rows are the hub-linked services (Tidal, YouTube Music). Pandora is linked in the
              // vendor apps: nothing to link here, so its row is informational and says where it is linked.
              const live = isService && isHubLinked(svc);
              const vendorLinked = isService && !live;
              const restoring = a.state === "restoring";
              const line = vendorLinked ? pandoraStatusLine(a) : accountStatusLine(a);
              return (
                <Row
                  key={svc}
                  icon={isService ? <ServiceBadge source={svc} size={28} /> : <AmpIcon size={22} className="text-secondary" />}
                  title={LABEL[svc]}
                  line={line}
                  skeleton={restoring}
                  disabled={vendorLinked || svc === "heos_account" || restoring}
                  onPress={live ? () => (a.linked ? setConfirmUnlink(svc) : void startLink(svc)) : undefined}
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
          <Row
            icon={<RefreshIcon size={22} className="text-secondary" />}
            title={UPDATE_ROW_TITLE}
            line={updateRowLine(update)}
            wrapLine
            onPress={updateRowTappable(update) ? () => (update.kind === "available" ? setConfirmUpdate(true) : void checkUpdate(true)) : undefined}
            disabled={!updateRowTappable(update)}
            testId="check-updates"
          />
          {updateLog(update) ? (
            <li className="px-4 pb-3" data-testid="update-log">
              <button type="button" className="flex min-h-target items-center gap-1 text-caption text-secondary" aria-expanded={logOpen} aria-controls="update-log-text" onClick={() => setShowLog(logOpen ? null : logKey)} data-testid="update-log-toggle">
                {logOpen ? <ChevronUpIcon size={16} /> : <ChevronDownIcon size={16} />}
                {logOpen ? "Hide the log" : "Show the log"}
              </button>
              {logOpen ? (
                <pre id="update-log-text" className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap rounded-control bg-base p-3 text-micro text-secondary" data-testid="update-log-text">
                  {updateLog(update)}
                </pre>
              ) : null}
            </li>
          ) : null}
          <li className="px-4 pb-3" data-testid="hub-stats-row">
            <button type="button" className="flex min-h-target items-center gap-1 text-caption text-secondary" aria-expanded={stats.open} aria-controls="hub-stats-body" onClick={() => void toggleStats()} data-testid="hub-stats">
              {stats.open ? <ChevronUpIcon size={16} /> : <ChevronDownIcon size={16} />}
              {stats.open ? "Hide stats" : "Show stats"}
            </button>
            {stats.open ? (
              <div id="hub-stats-body" className="mt-1" data-testid="hub-stats-body">
                {stats.loading ? <TextSkeleton lines={2} /> : stats.error ? <p className="text-caption text-error" role="alert">{stats.error}</p> : <p className="text-caption text-secondary">{stats.text}</p>}
              </div>
            ) : null}
          </li>
          {/* Pandora Sync / AirPlay bridge (Phase 7, experimental): status only; nothing here starts playback. */}
          <Row
            icon={<AirPlayGlyph className="h-5 w-5 text-secondary" />}
            title={AIRPLAY_ROW_TITLE}
            line={data ? airplayRowStatus(data.hub.airplay) : null}
            skeleton={!data && settings.loading}
            wrapLine
            testId="airplay-row"
          />
          <li className="px-4 pb-3 pt-1" data-testid="airplay-disclosure">
            <p className="text-caption text-secondary">{AIRPLAY_DISCLOSURE}</p>
          </li>
          {data?.hub.airplay.available ? (
            <Row icon={<span className="block h-5 w-5" />} title="Outputs" line="What the hub Mac streams to" onPress={() => void openOutputs()} testId="airplay-outputs" />
          ) : null}
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

      {/* Link flow (shared by Tidal and YouTube Music): device code + URL; polls at the hub's interval until linked or expired. */}
      <LinkSheet service={linkService} phase={link} onClose={closeLink} onRetry={() => linkService && void startLink(linkService)} />

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

      {/* Read-only in v1: outputs are chosen on the hub Mac (Music app). Checks mirror `selected`. */}
      <Sheet open={outputsOpen} onClose={() => setOutputsOpen(false)} title={AIRPLAY_OUTPUTS_TITLE} testId="airplay-outputs-sheet">
        <p className="pb-3 text-caption text-secondary">{AIRPLAY_OUTPUTS_NOTE}</p>
        {outputs.loading ? (
          <div className="py-3">
            <TextSkeleton lines={3} />
          </div>
        ) : outputs.error ? (
          <p className="py-3 text-caption text-error" role="alert">
            {outputs.error}
          </p>
        ) : outputs.data && outputs.data.outputs.length ? (
          // Read-only rows (U3): neutral check glyph in text-secondary, no fill or ring, no controls.
          <div className="flex flex-col" role="list" aria-label={AIRPLAY_OUTPUTS_TITLE}>
            {outputs.data.outputs.map((o) => (
              <div key={o.id} role="listitem" className="flex min-h-row items-center gap-3 border-b border-stroke last:border-0" data-testid="airplay-output" data-selected={o.selected ? "true" : "false"}>
                <span className={`flex h-6 w-6 items-center justify-center ${o.selected ? "text-secondary" : "text-transparent"}`} aria-hidden="true">
                  <CheckIcon size={16} />
                </span>
                <span className="flex-1">
                  <span className="block text-body text-primary">{o.name}</span>
                  <span className="block text-micro text-secondary">
                    {[o.kind_label ?? o.kind, o.selected ? "Selected" : null, o.active ? "Playing" : null].filter(Boolean).join(" · ") || "Not selected"}
                  </span>
                </span>
              </div>
            ))}
          </div>
        ) : (
          <p className="py-3 text-body text-secondary">No AirPlay outputs yet. Open the Music app on the hub Mac.</p>
        )}
      </Sheet>

      <Sheet open={confirmUpdate} onClose={() => setConfirmUpdate(false)} title="Update the hub?" testId="update-sheet">
        <p className="pb-4 text-body text-secondary">
          {update.kind === "available" && update.summary ? `${update.summary}. ` : ""}
          The hub restarts when the update lands; playback keeps going on the speakers. Control drops for about a minute; this screen reconnects on its own. If anything fails it goes back to the version it runs now.
        </p>
        <div className="flex flex-col gap-2 pb-2">
          <button type="button" className="flex h-target items-center justify-center rounded-control bg-overlay text-body text-primary" onClick={() => void applyUpdate()} data-testid="confirm-update">
            Update now
          </button>
          <button type="button" className="flex h-target items-center justify-center rounded-control text-body text-secondary" onClick={() => setConfirmUpdate(false)}>
            Not now
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
