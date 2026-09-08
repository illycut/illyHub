import type { NextConfig } from "next";

/**
 * Static export: the hub serves `out/` at `/` with SPA fallback (see docs/api.md, hub README).
 * API calls are same-origin in production; `NEXT_PUBLIC_HUB_URL` points dev builds at a hub
 * running on another port.
 */
const nextConfig: NextConfig = {
  output: "export",
  trailingSlash: false,
  images: { unoptimized: true },
  reactStrictMode: true,
};

export default nextConfig;
