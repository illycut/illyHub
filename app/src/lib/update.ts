/**
 * Hub self-update row state machine (PRD SET-2, docs/api.md "Self-update"). Pure and unit-tested so
 * the Settings row can be reasoned about without the network.
 *
 * Facts baked in: `HUB_ALLOW_UPDATE` defaults to off (a root daemon pulling code is an RCE surface),
 * so `update_disabled` is a normal, non-tappable row state in the hub's own words, not an error;
 * `update_running` means adopt the live job; the hub exits on `succeeded` (launchd relaunches it), so
 * the row waits for the socket to bounce, but only once that is corroborated (the socket actually
 * left "open", or two consecutive status polls failed) so a single dropped poll never fakes a
 * restart; "already up to date" ends the job as `idle` with a message and no restart.
 */
import messages from "./hub/messages.json";
import type { UpdateCheck, UpdateStatus } from "./hub/library";

const hub = (messages as { templates?: { hub?: Record<string, string> } }).templates?.hub ?? {};

export const UPDATE_ROW_TITLE = "Check for updates";
export const UPDATE_UP_TO_DATE = "Up to date";
export const UPDATE_CHECKING = "Checking…";
export const UPDATE_APPLYING = "Updating the hub…";
export const UPDATE_RECONNECTING = "Restarting the hub. This screen reconnects on its own.";
export const UPDATE_FAILED = "The update didn't finish; the hub stays on the version it runs now.";
export const UPDATE_ROLLED_BACK = "The update failed; the hub went back to the previous version.";
export const UPDATE_LOST = "The hub didn't come back after the update. Check the Mac.";
/**
 * A failed check shows the hub's one sentence for it (`templates.hub.update_check_failed`), never the
 * check's `error` field verbatim (an older hub could still put git text there; that goes to the console).
 */
export const UPDATE_CHECK_FAILED: string = hub.update_check_failed ?? "Couldn't check for updates. Tap to try again.";
/** The hub's own sentence for a hub with updates turned off (`templates.hub.update_disabled`). */
export const UPDATE_DISABLED: string = hub.update_disabled ?? "Updates are turned off on this hub.";
/** How long the row waits for the socket to return once the restart is corroborated. */
export const UPDATE_RECONNECT_TIMEOUT_MS = 90_000;
/** Consecutive failed status polls that count as "the hub is restarting". */
export const UPDATE_POLL_FAILURES_TO_RECONNECT = 2;
/** A mount re-check is skipped within this window (one check per session every five minutes). */
export const UPDATE_CHECK_DEBOUNCE_MS = 5 * 60_000;

export type UpdateRowState =
  | { kind: "idle" }
  | { kind: "checking" }
  | { kind: "upToDate"; version: string | null }
  | { kind: "available"; summary: string | null; aheadBy: number }
  /** Updates are turned off on the hub: the hub's sentence, row not tappable, not an error tone. */
  | { kind: "disabled"; message: string }
  | { kind: "applying"; jobId: string | null; since: number; failedPolls: number }
  | { kind: "reconnecting"; since: number; sawClosed: boolean; versionBefore: string | null }
  | { kind: "succeeded"; versionBefore: string | null; version: string | null }
  | { kind: "failed"; log: string }
  | { kind: "rolledBack"; log: string }
  | { kind: "lost" }
  | { kind: "error"; message: string };

/** From a check response. */
export function fromCheck(c: UpdateCheck): UpdateRowState {
  if (c.error && !c.available) {
    if (process.env.NODE_ENV !== "production") console.warn(`[illyhub] update check: ${c.error}`);
    return { kind: "error", message: UPDATE_CHECK_FAILED };
  }
  return c.available ? { kind: "available", summary: c.remote.summary, aheadBy: c.remote.ahead_by } : { kind: "upToDate", version: c.current.version };
}

/** A refused apply, by the hub's code. */
export function fromApplyRefusal(code: string, message: string, now: number): UpdateRowState {
  if (code === "update_running") return { kind: "applying", jobId: null, since: now, failedPolls: 0 };
  if (code === "update_disabled") return { kind: "disabled", message: message || UPDATE_DISABLED };
  return { kind: "error", message };
}

/** Log text for a terminal job: the hub's one-line message first, then the tail. */
function jobLog(st: UpdateStatus): string {
  return [st.message, st.log_tail].filter(Boolean).join("\n");
}

/**
 * From a status poll while applying. `running` keeps applying (adopting the live job id); `failed`
 * settles with the log; `rolled_back` settles with the log (the hub relaunches on the restored
 * code; the reconnect banner covers that); `succeeded` means the hub is about to exit → wait for the
 * socket bounce; `up_to_date` (or `idle` for OUR job with a message) means it finished without a
 * restart → re-check; `idle` for another (or no) job after we started means the hub already
 * relaunched and forgot the job → wait for the bounce; `idle` for our job with nothing to say means
 * the job has not been registered yet → keep applying.
 */
export function fromStatus(cur: UpdateRowState, st: UpdateStatus, now: number, versionBefore: string | null): UpdateRowState {
  if (cur.kind !== "applying") return cur;
  const ours = cur.jobId === null || st.job_id === cur.jobId;
  switch (st.state) {
    case "running":
      return cur.jobId === null && st.job_id ? { ...cur, jobId: st.job_id, failedPolls: 0 } : cur.failedPolls ? { ...cur, failedPolls: 0 } : cur;
    case "failed":
      return { kind: "failed", log: jobLog(st) };
    case "rolled_back":
      return { kind: "rolledBack", log: jobLog(st) };
    case "succeeded":
      return { kind: "reconnecting", since: now, sawClosed: false, versionBefore };
    case "up_to_date":
      return { kind: "checking" };
    case "idle":
      if (ours && st.job_id) return st.message ? { kind: "checking" } : cur;
      if (ours && !st.job_id && cur.jobId === null) return cur;
      return { kind: "reconnecting", since: now, sawClosed: false, versionBefore };
  }
}

/**
 * A status poll that could not reach the hub: only repeated failures corroborate a restart, and even
 * then the socket still has to be seen leaving "open" and coming back before the row says "Updated"
 * (two dropped polls with a healthy socket must never fake a finished update).
 */
export function onPollUnreachable(cur: UpdateRowState, now: number, versionBefore: string | null): UpdateRowState {
  if (cur.kind !== "applying") return cur;
  const failedPolls = cur.failedPolls + 1;
  if (failedPolls >= UPDATE_POLL_FAILURES_TO_RECONNECT) return { kind: "reconnecting", since: now, sawClosed: false, versionBefore };
  return { ...cur, failedPolls };
}

/** The socket left "open" while applying: the hub exited, the restart is corroborated. */
export function onSocketClosedWhileApplying(cur: UpdateRowState, now: number, versionBefore: string | null): UpdateRowState {
  return cur.kind === "applying" ? { kind: "reconnecting", since: now, sawClosed: true, versionBefore } : cur;
}

export type Bounce = { kind: "waiting"; sawClosed: boolean; since: number } | { kind: "done" } | { kind: "timeout" };

/**
 * One rule for "wait for the socket to bounce", shared by the restart notice and the update row:
 * the socket must leave "open" and come back; past `timeoutMs` it is a timeout. Pure.
 */
export function waitForSocketBounce(cur: { sawClosed: boolean; since: number }, phaseOpen: boolean, now: number, timeoutMs: number): Bounce {
  if (now - cur.since >= timeoutMs) return { kind: "timeout" };
  if (!phaseOpen) return { kind: "waiting", sawClosed: true, since: cur.since };
  return cur.sawClosed ? { kind: "done" } : { kind: "waiting", sawClosed: false, since: cur.since };
}

/** Socket phase transitions while reconnecting; on return, report the version the hub now runs. */
export function onPhase(cur: UpdateRowState, phaseOpen: boolean, now: number, currentVersion: string | null): UpdateRowState {
  if (cur.kind !== "reconnecting") return cur;
  const b = waitForSocketBounce(cur, phaseOpen, now, UPDATE_RECONNECT_TIMEOUT_MS);
  if (b.kind === "timeout") return { kind: "lost" };
  if (b.kind === "done") return { kind: "succeeded", versionBefore: cur.versionBefore, version: currentVersion };
  return b.sawClosed === cur.sawClosed ? cur : { ...cur, sawClosed: b.sawClosed };
}

/** Row line text per state (§10: sentence case, facts). */
export function updateRowLine(s: UpdateRowState): string | null {
  switch (s.kind) {
    case "idle":
      return null;
    case "checking":
      return UPDATE_CHECKING;
    case "upToDate":
      return s.version ? `${UPDATE_UP_TO_DATE} · ${s.version}` : UPDATE_UP_TO_DATE;
    case "available":
      return s.summary ? `Update available · ${s.summary}` : "Update available";
    case "disabled":
      return s.message;
    case "applying":
      return UPDATE_APPLYING;
    case "reconnecting":
      return UPDATE_RECONNECTING;
    case "succeeded":
      if (s.versionBefore && s.version && s.versionBefore !== s.version) return `Updated ${s.versionBefore} → ${s.version}`;
      return s.version ? `Updated to ${s.version}` : "Updated";
    case "failed":
      return UPDATE_FAILED;
    case "rolledBack":
      return UPDATE_ROLLED_BACK;
    case "lost":
      return UPDATE_LOST;
    case "error":
      return s.message;
  }
}

/** Tappable to apply (available) or to re-check (settled states); never while busy or disabled. */
export function updateRowTappable(s: UpdateRowState): boolean {
  return s.kind !== "checking" && s.kind !== "applying" && s.kind !== "reconnecting" && s.kind !== "disabled";
}

/** Terminal states whose log is worth a disclosure. */
export function updateLog(s: UpdateRowState): string | null {
  return (s.kind === "failed" || s.kind === "rolledBack") && s.log.trim() ? s.log : null;
}

export function isTerminal(s: UpdateRowState): boolean {
  return s.kind === "failed" || s.kind === "rolledBack" || s.kind === "succeeded" || s.kind === "lost" || s.kind === "error";
}
