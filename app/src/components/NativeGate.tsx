"use client";
import { useSyncExternalStore } from "react";
import { HubAddressScreen } from "@/components/HubAddressScreen";
import { isNativeShell, readHubBase } from "@/lib/hub/hubBase";

/** Full page reload so every module (socket, REST client, art URLs) reads the new address. */
export function reloadForHubBase(): void {
  if (typeof window !== "undefined") window.location.reload();
}

/** Re-check once the page has fully loaded: the Capacitor bridge script may land after hydration. */
const subscribe = (cb: () => void) => {
  if (typeof window === "undefined") return () => {};
  window.addEventListener("load", cb);
  return () => window.removeEventListener("load", cb);
};
const decide = (): "app" | "ask" => (isNativeShell() && !readHubBase() ? "ask" : "app");
const serverSnapshot = (): "app" | "ask" => "app";

/** Native-shell facts for components that render differently there (server snapshot: browser). */
export function useNativeShell(): boolean {
  return useSyncExternalStore(subscribe, isNativeShell, () => false);
}
export function useStoredHubBase(): string | null {
  return useSyncExternalStore(subscribe, readHubBase, () => null);
}

/**
 * Inside the Android shell with no stored hub address, show the address screen instead of the
 * app (there is nothing to connect to yet). In a browser, or once an address is stored, render
 * children unchanged. The static export prerenders "app"; the client decides on hydration, and
 * saving reloads the page so the decision is made once.
 */
export function NativeGate({ children }: { children: React.ReactNode }) {
  const state = useSyncExternalStore(subscribe, decide, serverSnapshot);
  if (state === "ask") return <HubAddressScreen onSaved={reloadForHubBase} />;
  return <>{children}</>;
}
