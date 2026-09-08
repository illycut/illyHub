"use client";
import { useEffect, useState } from "react";
import { useHub } from "@/lib/hub/store";
import { formatClock } from "@/lib/clock";

/**
 * Hub-unreachable banner (design system §8): full width under the header, error text on
 * bg-raised, retries automatically, shows last attempt time in micro.
 */
export function HubBanner() {
  const phase = useHub((s) => s.phase);
  const lastAttemptAt = useHub((s) => s.lastAttemptAt);
  const everConnected = useHub((s) => s.everConnected);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (phase === "open") return;
    const id = setInterval(() => setTick((t) => t + 1), 1000);
    return () => clearInterval(id);
  }, [phase]);
  // Give the first connection a moment before declaring the hub unreachable.
  const show = phase !== "open" && (everConnected || tick >= 3);
  if (!show) return null;
  return (
    <div role="alert" className="screen-margin bg-raised py-3">
      <p className="text-body text-error">Can&apos;t reach the hub. Check that the Mac is on the network.</p>
      {lastAttemptAt ? (
        <p className="mt-1 text-micro text-tertiary numeric">Last attempt {formatClock(lastAttemptAt)}</p>
      ) : null}
    </div>
  );
}
