/* illyHub service worker: precache the shell, network-first for HTML, cache-first for hashed
   assets and fonts. Never touches /api or /ws. Version bumps on every build via ?v= in the
   registration URL (see ServiceWorker.tsx). */
const VERSION = new URL(self.location.href).searchParams.get("v") || "dev";
const SHELL = `illyhub-shell-${VERSION}`;
const ASSETS = `illyhub-assets`;
const PRECACHE = ["/", "/index.html", "/manifest.webmanifest", "/icons/icon-192.png", "/icons/icon-512.png",
  "/fonts/GeneralSans-500.woff2", "/fonts/GeneralSans-600.woff2", "/fonts/GeneralSans-700.woff2"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(SHELL).then((c) => Promise.allSettled(PRECACHE.map((u) => c.add(u)))).then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k.startsWith("illyhub-shell-") && k !== SHELL).map((k) => caches.delete(k)))).then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/") || url.pathname === "/ws") return;

  const isHashedAsset = url.pathname.startsWith("/_next/static/") || url.pathname.startsWith("/fonts/") || url.pathname.startsWith("/icons/");
  if (isHashedAsset) {
    event.respondWith(
      caches.open(ASSETS).then((c) =>
        c.match(req).then((hit) => hit || fetch(req).then((res) => {
          if (res.ok) c.put(req, res.clone());
          return res;
        })),
      ),
    );
    return;
  }

  // HTML and everything else: network first, fall back to the cached shell.
  event.respondWith(
    fetch(req)
      .then((res) => {
        if (res.ok && (req.mode === "navigate" || url.pathname === "/index.html")) {
          caches.open(SHELL).then((c) => c.put("/index.html", res.clone()));
        }
        return res;
      })
      .catch(() => caches.open(SHELL).then((c) => c.match("/index.html").then((hit) => hit || Response.error()))),
  );
});
