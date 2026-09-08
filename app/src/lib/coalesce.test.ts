import { Coalescer } from "./coalesce";

function timers() {
  const q: { fn: () => void; id: number }[] = [];
  let seq = 0;
  return {
    q,
    set: (fn: () => void) => {
      q.push({ fn, id: ++seq });
      return seq as unknown;
    },
    clear: (id: unknown) => {
      const i = q.findIndex((t) => t.id === id);
      if (i >= 0) q.splice(i, 1);
    },
    fire: () => q.splice(0).forEach((t) => t.fn()),
  };
}

describe("Coalescer", () => {
  it("sends the first value immediately and the latest at the end of the window", () => {
    const sent: [string, number][] = [];
    const t = timers();
    const c = new Coalescer<number>((k, v) => sent.push([k, v]), 100, t.set, t.clear);
    c.submit("a", 1);
    c.submit("a", 2);
    c.submit("a", 3);
    expect(sent).toEqual([["a", 1]]);
    expect(c.hasPending("a")).toBe(true);
    t.fire();
    expect(sent).toEqual([["a", 1], ["a", 3]]);
    t.fire();
    expect(sent).toHaveLength(2);
  });
  it("keys are independent; flush sends pending now and reports whether it did", () => {
    const sent: [string, number][] = [];
    const t = timers();
    const c = new Coalescer<number>((k, v) => sent.push([k, v]), 100, t.set, t.clear);
    c.submit("a", 1);
    c.submit("b", 1);
    c.submit("a", 9);
    expect(c.flush("a")).toBe(true);
    expect(c.flush("a")).toBe(false);
    expect(sent).toEqual([["a", 1], ["b", 1], ["a", 9]]);
    c.submit("b", 5);
    expect(c.flush()).toBe(true);
    expect(sent.at(-1)).toEqual(["b", 5]);
    expect(t.q).toHaveLength(0);
  });
  it("commit on release never double-writes: pending value wins, else only an unsent value goes out", () => {
    const sent: number[] = [];
    const t = timers();
    const c = new Coalescer<number>((_k, v) => sent.push(v), 100, t.set, t.clear);
    c.submit("a", 5); // sent immediately (keyboard step: change then commit)
    c.commit("a", 5); // same value already sent → nothing
    expect(sent).toEqual([5]);
    c.submit("a", 6); // new burst after the commit closed the window → immediate
    c.submit("a", 7); // pending
    c.commit("a", 7); // flushes pending 7, nothing more
    expect(sent).toEqual([5, 6, 7]);
    c.commit("a", 7); // repeat release → nothing
    expect(sent).toEqual([5, 6, 7]);
    c.commit("a", 9); // release on a value that never went out → sent once
    expect(sent).toEqual([5, 6, 7, 9]);
  });
  it("dispose clears timers and drops pending values; later submits are ignored", () => {
    const sent: [string, number][] = [];
    const t = timers();
    const c = new Coalescer<number>((k, v) => sent.push([k, v]), 100, t.set, t.clear);
    c.submit("a", 1);
    c.submit("a", 2);
    c.dispose();
    expect(t.q).toHaveLength(0);
    t.fire();
    c.submit("a", 3);
    expect(sent).toEqual([["a", 1]]);
  });
  it("works with the REAL default timers (regression for setTimeout receiver bug)", async () => {
    const sent: number[] = [];
    const c = new Coalescer<number>((_k, v) => sent.push(v), 10);
    c.submit("k", 1);
    c.submit("k", 2);
    await new Promise((r) => setTimeout(r, 30));
    expect(sent).toEqual([1, 2]);
    c.dispose();
  });
});
