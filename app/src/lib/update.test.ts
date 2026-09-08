import {
  UPDATE_APPLYING,
  UPDATE_CHECK_FAILED,
  UPDATE_DISABLED,
  UPDATE_LOST,
  UPDATE_RECONNECTING,
  UPDATE_RECONNECT_TIMEOUT_MS,
  UPDATE_ROLLED_BACK,
  UPDATE_UP_TO_DATE,
  fromApplyRefusal,
  fromCheck,
  fromStatus,
  isTerminal,
  onPhase,
  onPollUnreachable,
  onSocketClosedWhileApplying,
  updateLog,
  updateRowLine,
  updateRowTappable,
  waitForSocketBounce,
  type UpdateRowState,
} from "./update";
import { asUpdateCheck, asUpdateStatus } from "./hub/library";

const check = (available: boolean, over: Record<string, unknown> = {}) =>
  asUpdateCheck({ current: { version: "0.8.0", commit: "aaa", branch: "main" }, remote: { commit: "bbb", ahead_by: available ? 3 : 0, summary: available ? ["Phase 8: queue", "Phase 8: search"] : [], tracking: "main" }, available, last_checked_at: "2026-09-08T00:00:00Z", ...over });
const applying = (over: Partial<Extract<UpdateRowState, { kind: "applying" }>> = {}): UpdateRowState => ({ kind: "applying", jobId: "j1", since: 0, failedPolls: 0, ...over });

describe("update row state machine", () => {
  it("check → up to date / available (summaries joined); a check error shows the fixed sentence, never the hub's text", () => {
    expect(fromCheck(check(false))).toEqual({ kind: "upToDate", version: "0.8.0" });
    expect(updateRowLine(fromCheck(check(false)))).toBe(`${UPDATE_UP_TO_DATE} · 0.8.0`);
    const avail = fromCheck(check(true));
    expect(avail).toEqual({ kind: "available", summary: "Phase 8: queue · Phase 8: search", aheadBy: 3 });
    expect(updateRowLine(avail)).toBe("Update available · Phase 8: queue · Phase 8: search");
    expect(updateRowTappable(avail)).toBe(true);
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const failed = fromCheck(check(false, { error: "fatal: Needed a single revision" }));
    expect(failed).toEqual({ kind: "error", message: UPDATE_CHECK_FAILED });
    expect(updateRowLine(failed)).not.toContain("fatal");
    expect(warn).toHaveBeenCalledWith(expect.stringContaining("Needed a single revision"));
    warn.mockRestore();
    expect(updateRowTappable(failed)).toBe(true);
  });

  it("apply refusals by code: update_running adopts the live job, update_disabled is a non-tappable row in the hub's words, anything else is an error", () => {
    expect(fromApplyRefusal("update_running", "An update is already running.", 5)).toEqual({ kind: "applying", jobId: null, since: 5, failedPolls: 0 });
    const disabled = fromApplyRefusal("update_disabled", "Updates are turned off on this hub.", 5);
    expect(disabled).toEqual({ kind: "disabled", message: "Updates are turned off on this hub." });
    expect(updateRowLine(disabled)).toBe("Updates are turned off on this hub.");
    expect(updateRowTappable(disabled)).toBe(false);
    expect(fromApplyRefusal("update_disabled", "", 5)).toEqual({ kind: "disabled", message: UPDATE_DISABLED });
    expect(fromApplyRefusal("update_refused", "The hub's git remote is not an https:// or ssh:// URL; updates are refused.", 5)).toEqual({ kind: "error", message: "The hub's git remote is not an https:// or ssh:// URL; updates are refused." });
  });

  it("status while applying: running adopts the job id; failed / rolled back settle with message + log; succeeded waits for the bounce; idle handling uses the job id", () => {
    expect(updateRowLine(applying())).toBe(UPDATE_APPLYING);
    expect(updateRowTappable(applying())).toBe(false);
    // adopting the live job after update_running
    expect(fromStatus(applying({ jobId: null }), asUpdateStatus({ state: "running", job_id: "j9", log_tail: ["git pull"] }), 10, "0.8.0")).toEqual(applying({ jobId: "j9" }));
    // a successful poll clears the failure streak
    expect(fromStatus(applying({ failedPolls: 1 }), asUpdateStatus({ state: "running", job_id: "j1", log_tail: [] }), 10, "0.8.0")).toEqual(applying());
    const failed = fromStatus(applying(), asUpdateStatus({ state: "failed", job_id: "j1", log_tail: ["uv sync exploded"], message: "uv sync failed" }), 10, "0.8.0");
    expect(failed).toEqual({ kind: "failed", log: "uv sync failed\nuv sync exploded" });
    expect(updateLog(failed)).toBe("uv sync failed\nuv sync exploded");
    expect(isTerminal(failed)).toBe(true);
    const rolled = fromStatus(applying(), asUpdateStatus({ state: "rolled_back", job_id: "j1", log_tail: ["a", "b"] }), 10, "0.8.0");
    expect(rolled).toEqual({ kind: "rolledBack", log: "a\nb" });
    expect(updateRowLine(rolled)).toBe(UPDATE_ROLLED_BACK);
    expect(fromStatus(applying(), asUpdateStatus({ state: "succeeded", job_id: "j1", log_tail: [] }), 10, "0.8.0")).toEqual({ kind: "reconnecting", since: 10, sawClosed: false, versionBefore: "0.8.0" });
    // up_to_date: the job finished without a restart → re-check (the check row then says "Up to date")
    expect(fromStatus(applying(), asUpdateStatus({ state: "up_to_date", job_id: "j1", log_tail: [], message: "The hub is up to date." }), 10, "0.8.0")).toEqual({ kind: "checking" });
    // idle for OUR job with a message: finished without a restart (already up to date) → re-check
    expect(fromStatus(applying(), asUpdateStatus({ state: "idle", job_id: "j1", log_tail: [], message: "Already up to date." }), 10, "0.8.0")).toEqual({ kind: "checking" });
    // idle for our job with nothing to say: not registered yet, keep applying
    expect(fromStatus(applying(), asUpdateStatus({ state: "idle", job_id: "j1", log_tail: [] }), 10, "0.8.0")).toEqual(applying());
    // idle with a different / no job after we started: the hub already relaunched → wait for the bounce
    expect(fromStatus(applying(), asUpdateStatus({ state: "idle", job_id: null, log_tail: [] }), 10, "0.8.0").kind).toBe("reconnecting");
    expect(fromStatus(applying(), asUpdateStatus({ state: "idle", job_id: "other", log_tail: [] }), 10, "0.8.0").kind).toBe("reconnecting");
    expect(fromStatus({ kind: "idle" }, asUpdateStatus({ state: "succeeded", log_tail: [] }), 10, null)).toEqual({ kind: "idle" });
  });

  it("a restart needs corroboration: one failed poll is noted, two in a row (or the socket leaving open) enter the reconnect wait", () => {
    const one = onPollUnreachable(applying(), 20, "0.8.0");
    expect(one).toEqual(applying({ failedPolls: 1 }));
    expect(one.kind).toBe("applying");
    // polls alone never count as "saw the socket close": the bounce is still required
    expect(onPollUnreachable(one, 30, "0.8.0")).toEqual({ kind: "reconnecting", since: 30, sawClosed: false, versionBefore: "0.8.0" });
    expect(onSocketClosedWhileApplying(applying(), 40, "0.8.0")).toEqual({ kind: "reconnecting", since: 40, sawClosed: true, versionBefore: "0.8.0" });
    expect(onSocketClosedWhileApplying({ kind: "idle" }, 40, null)).toEqual({ kind: "idle" });
    expect(onPollUnreachable({ kind: "idle" }, 20, null)).toEqual({ kind: "idle" });
  });

  it("reconnecting: the socket must leave open and come back, then 'Updated before → after'; the timeout runs only inside the wait", () => {
    const start: UpdateRowState = { kind: "reconnecting", since: 0, sawClosed: false, versionBefore: "0.8.0" };
    expect(updateRowLine(start)).toBe(UPDATE_RECONNECTING);
    expect(onPhase(start, true, 100, "0.8.0")).toBe(start);
    const closed = onPhase(start, false, 200, null);
    expect(closed).toEqual({ ...start, sawClosed: true });
    expect(onPhase(closed, false, 300, null)).toBe(closed);
    const done = onPhase(closed, true, 400, "0.9.0");
    expect(done).toEqual({ kind: "succeeded", versionBefore: "0.8.0", version: "0.9.0" });
    expect(updateRowLine(done)).toBe("Updated 0.8.0 → 0.9.0");
    expect(updateRowLine({ kind: "succeeded", versionBefore: "0.9.0", version: "0.9.0" })).toBe("Updated to 0.9.0");
    expect(updateRowLine({ kind: "succeeded", versionBefore: null, version: null })).toBe("Updated");
    expect(onPhase(start, true, UPDATE_RECONNECT_TIMEOUT_MS, "0.8.0")).toEqual({ kind: "lost" });
    expect(updateRowLine({ kind: "lost" })).toBe(UPDATE_LOST);
    expect(onPhase({ kind: "idle" }, false, 0, null)).toEqual({ kind: "idle" });
  });

  it("waitForSocketBounce is the one rule the restart notice and the update row share", () => {
    const w = { sawClosed: false, since: 0 };
    expect(waitForSocketBounce(w, true, 10, 1000)).toEqual({ kind: "waiting", sawClosed: false, since: 0 });
    expect(waitForSocketBounce(w, false, 10, 1000)).toEqual({ kind: "waiting", sawClosed: true, since: 0 });
    expect(waitForSocketBounce({ ...w, sawClosed: true }, true, 10, 1000)).toEqual({ kind: "done" });
    expect(waitForSocketBounce(w, true, 1000, 1000)).toEqual({ kind: "timeout" });
  });
});
