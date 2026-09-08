/** Where the hub lives. Production is same-origin (the hub serves the export at `/`). */
export function hubHttpBase(): string {
  const override = process.env.NEXT_PUBLIC_HUB_URL;
  if (override) return override.replace(/\/$/, "");
  if (typeof window !== "undefined") return window.location.origin;
  return "";
}

export function hubWsUrl(): string {
  const base = hubHttpBase();
  if (!base) return "/ws";
  return base.replace(/^http/, "ws") + "/ws";
}

export function artUrl(ref: { url?: string | null } | null | undefined, size: 96 | 320 | 1080 | "backdrop"): string | null {
  const url = ref?.url;
  if (!url) return null;
  const abs = url.startsWith("/") ? hubHttpBase() + url : url;
  try {
    const u = new URL(abs, hubHttpBase() || "http://localhost");
    u.searchParams.set("size", String(size));
    return u.toString();
  } catch {
    return abs;
  }
}
