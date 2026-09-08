export function newCorrelationId(): string {
  const c = typeof crypto !== "undefined" ? crypto : undefined;
  if (c?.randomUUID) return c.randomUUID().replace(/-/g, "").slice(0, 16);
  return Math.random().toString(16).slice(2, 10) + Date.now().toString(16).slice(-8);
}
