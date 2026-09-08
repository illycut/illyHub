/**
 * Client-side latest-wins coalescer for slider drags (design system §6.5: at most ~10 commands
 * per second per target). The first value in a burst fires immediately, later values collapse
 * into one trailing send when the window closes. `flush` sends what is pending now and IS the
 * commit on pointer release. Default timers are arrow wrappers (no `this` binding issues).
 */
export type TimerHandle = unknown;

export class Coalescer<T> {
  private pending = new Map<string, T>();
  private timers = new Map<string, TimerHandle>();
  private lastSent = new Map<string, T>();
  private disposed = false;

  constructor(
    private readonly send: (key: string, value: T) => void,
    private readonly windowMs = 100,
    private readonly setTimer: (fn: () => void, ms: number) => TimerHandle = (fn, ms) => setTimeout(fn, ms),
    private readonly clearTimer: (t: TimerHandle) => void = (t) => clearTimeout(t as ReturnType<typeof setTimeout>),
  ) {}

  submit(key: string, value: T): void {
    if (this.disposed) return;
    if (this.timers.has(key)) {
      this.pending.set(key, value);
      return;
    }
    this.sendNow(key, value);
    this.arm(key);
  }

  /**
   * Pointer release: the commit. Sends the pending value if there is one; otherwise sends
   * `value` only when it differs from what was last sent, so a release never double-writes.
   */
  commit(key: string, value: T): void {
    if (this.disposed) return;
    if (this.flush(key)) return;
    if (this.lastSent.get(key) !== value) this.sendNow(key, value);
  }

  private sendNow(key: string, value: T): void {
    this.lastSent.set(key, value);
    this.send(key, value);
  }

  /** Send anything pending right now (e.g. on pointer-up). Returns whether something was sent. */
  flush(key?: string): boolean {
    const keys = key ? [key] : [...new Set([...this.pending.keys(), ...this.timers.keys()])];
    let sent = false;
    for (const k of keys) {
      const t = this.timers.get(k);
      if (t) this.clearTimer(t);
      this.timers.delete(k);
      const v = this.pending.get(k);
      this.pending.delete(k);
      if (v !== undefined) {
        this.sendNow(k, v);
        sent = true;
      }
    }
    return sent;
  }

  /** Whether a value for `key` is still waiting in the window. */
  hasPending(key: string): boolean {
    return this.pending.has(key);
  }

  /** Clear timers and drop pending values; call from effect cleanup. */
  dispose(): void {
    this.disposed = true;
    for (const t of this.timers.values()) this.clearTimer(t);
    this.timers.clear();
    this.pending.clear();
    this.lastSent.clear();
  }

  private arm(key: string): void {
    this.timers.set(
      key,
      this.setTimer(() => {
        this.timers.delete(key);
        const v = this.pending.get(key);
        this.pending.delete(key);
        if (v !== undefined && !this.disposed) {
          this.sendNow(key, v);
          this.arm(key);
        }
      }, this.windowMs),
    );
  }
}
