import { useEffect } from "react";

let locks = 0;

/** Locks body scroll while `active`; reference-counted so nested surfaces (sheet over Now Playing) cooperate. */
export function useScrollLock(active: boolean): void {
  useEffect(() => {
    if (!active || typeof document === "undefined") return;
    locks += 1;
    document.body.classList.add("scroll-lock");
    return () => {
      locks = Math.max(0, locks - 1);
      if (locks === 0) document.body.classList.remove("scroll-lock");
    };
  }, [active]);
}

export function scrollLockCount(): number {
  return locks;
}
