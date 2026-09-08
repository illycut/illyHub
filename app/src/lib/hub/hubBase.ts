/**
 * Runtime hub address (docs/android.md). The Android shell bundles the export, so the page origin
 * is the WebView's own (`http://localhost`) and cannot be the hub. The user enters the hub address
 * once; it is kept in localStorage and consulted before the build-time override and the origin.
 *
 * Resolution order (see config.ts): stored override → NEXT_PUBLIC_HUB_URL → window.location.origin.
 * Every storage access is guarded; storage can throw or be empty (private mode, cleared data).
 */
export const HUB_BASE_KEY = "illyhub.hubBase.v1";

/** Placeholders shown in the address field; the mDNS form works when the Mac's name is known. */
export const HUB_ADDRESS_PLACEHOLDER = "http://192.168.50.10:8080";
export const HUB_ADDRESS_HINT = "The address you use in the browser, for example http://192.168.50.10:8080 or http://hub-mac.local:8080.";

declare global {
  interface Window {
    /** Set by the Android shell (or tests) so the app knows it is not running in a browser. */
    __ILLYHUB_NATIVE__?: boolean;
    Capacitor?: { isNativePlatform?: () => boolean };
  }
}

/** True inside the Capacitor shell (or when a test sets the flag). Never true for a browser tab. */
export function isNativeShell(): boolean {
  if (typeof window === "undefined") return false;
  if (window.__ILLYHUB_NATIVE__ === true) return true;
  try {
    return window.Capacitor?.isNativePlatform?.() === true;
  } catch {
    return false;
  }
}

/** The stored hub address, validated on the way out (http/https, normalized); junk reads as null. */
export function readHubBase(): string | null {
  try {
    const v = localStorage.getItem(HUB_BASE_KEY);
    if (!v || !v.trim()) return null;
    return normalizeHubBase(v);
  } catch {
    return null;
  }
}

/** Store the address; false when storage is unavailable so the caller can say so. */
export function writeHubBase(base: string): boolean {
  try {
    const value = normalizeHubBase(base);
    localStorage.setItem(HUB_BASE_KEY, value);
    return localStorage.getItem(HUB_BASE_KEY) === value;
  } catch {
    return false;
  }
}

export const HUB_ADDRESS_SAVE_FAILED = "This phone can't save the address. Turn on site data for illyHub.";

export function clearHubBase(): void {
  try {
    localStorage.removeItem(HUB_BASE_KEY);
  } catch {
    // nothing to clear
  }
}

/**
 * Accept what a person types: a bare host or host:port gets `http://`; trailing slashes go; the
 * default hub port is added when none is given and the scheme is http. Throws on anything that is
 * not an http(s) URL so the screen can show one factual line.
 */
export function normalizeHubBase(input: string): string {
  let s = input.trim();
  if (!s) throw new Error("Enter the hub address.");
  if (!/^[a-z]+:\/\//i.test(s)) s = `http://${s}`;
  let u: URL;
  try {
    u = new URL(s);
  } catch {
    throw new Error("That doesn't look like an address. Try http://192.168.50.10:8080.");
  }
  if (u.protocol !== "http:" && u.protocol !== "https:") throw new Error("The address must start with http:// or https://.");
  if (!u.port && u.protocol === "http:") u.port = "8080";
  // Only the origin matters: the hub serves its API at the root. Credentials in a URL would be
  // sent with every request and are never wanted.
  u.username = "";
  u.password = "";
  u.pathname = "/";
  u.search = "";
  u.hash = "";
  return u.origin;
}

export type ProbeResult = { ok: true; version: string | null; address: string } | { ok: false; message: string };

/**
 * GET {base}/api/health with a short timeout. Returns the hub version on success or one factual
 * sentence on failure; never throws.
 */
export async function probeHub(base: string, fetcher: typeof fetch = fetch, timeoutMs = 5000): Promise<ProbeResult> {
  let address: string;
  try {
    address = normalizeHubBase(base);
  } catch (e) {
    return { ok: false, message: e instanceof Error ? e.message : "That doesn't look like an address." };
  }
  const ctrl = typeof AbortController !== "undefined" ? new AbortController() : null;
  const timer = ctrl ? setTimeout(() => ctrl.abort(), timeoutMs) : null;
  try {
    const res = await fetcher(`${address}/api/health`, { signal: ctrl?.signal, headers: { accept: "application/json" } });
    if (!res.ok) return { ok: false, message: `Something answered at ${address}, but it isn't the hub (HTTP ${res.status}).` };
    // The body read shares the abort budget: a hub that answers headers and then stalls still fails.
    const body = (await res.json().catch(() => null)) as { version?: unknown } | null;
    if (!body || typeof body !== "object" || !("version" in body)) {
      return { ok: false, message: `Something answered at ${address}, but it isn't the hub.` };
    }
    return { ok: true, version: typeof body.version === "string" ? body.version : null, address };
  } catch (e) {
    const aborted = e instanceof Error && e.name === "AbortError";
    return {
      ok: false,
      message: aborted
        ? `No answer from ${address}. Check that the Mac is on and you're on the same Wi-Fi.`
        : `Couldn't reach ${address}. Check the address and that you're on the same Wi-Fi.`,
    };
  } finally {
    if (timer) clearTimeout(timer);
  }
}
