import type { CapacitorConfig } from "@capacitor/cli";

/**
 * Android shell for the illyHub PWA (docs/android.md). The export in `out/` is bundled into the
 * APK; API and WebSocket calls go to the hub address the user enters on first run
 * (src/lib/hub/hubBase.ts), which is plain http on the LAN unless the hub serves HTTPS.
 *
 * `androidScheme: "http"` keeps localStorage stable across builds and lets the WebView talk to a
 * cleartext hub without mixed-content blocking; the manifest also sets usesCleartextTraffic.
 */
const config: CapacitorConfig = {
  appId: "com.illyhub.app",
  appName: "illyHub",
  webDir: "out",
  server: {
    androidScheme: "http",
    cleartext: true,
  },
  android: {
    // Art and fonts come from the hub over the same scheme as the page; kept on so an https hub
    // can still show http art from a device URL the proxy could not fetch.
    allowMixedContent: true,
    backgroundColor: "#101014",
  },
};

export default config;
