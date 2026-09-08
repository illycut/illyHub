# illyHub app

Touch PWA for the illyHub hub. Next.js static export, TypeScript, Tailwind mapped to the design
tokens in `src/styles/tokens.css`. The hub serves the export at `/`; the app talks to the hub on
the same origin over REST (`/api/*`) and WebSocket (`/ws`). Contract: `../docs/api.md`.

## Develop

```bash
npm install
npm run dev                      # http://localhost:3000, expects a hub on the same origin…
NEXT_PUBLIC_HUB_URL=http://127.0.0.1:8080 npm run dev   # …or point it at a hub elsewhere
```

Run a hub with fake devices for development:

```bash
cd ../hub && HUB_FAKE_DEVICES=1 uv run hub
```

`/dev/tokens` renders every design token for visual QA.

## Build and serve from the hub

```bash
npm run build                    # writes out/
cd ../hub && HUB_APP_DIR=../app/out uv run hub   # the hub mounts out/ at / with SPA fallback
```

The hub also serves `public/fonts/` (General Sans, Fontshare license, self-hosted) so the app
works with the internet down.

## Test

```bash
npm run typecheck                # tsc --noEmit
npm run lint                     # eslint
npm test                         # vitest, jsdom
npm run coverage                 # vitest --coverage; thresholds: lines 80, branches 75, functions 80
npm run e2e                      # playwright: builds, starts the fake hub, runs WebKit (iPhone 13) + Chromium (Pixel 7)
```

`src/styles/classes.test.ts` builds the real Tailwind output and fails if any class used in a
`className` produced no CSS (catches theme/token drift). `e2e/viewports.spec.ts` asserts the
transport row never needs scrolling on iPhone SE portrait, iPhone 13 landscape, iPad landscape,
and Pixel 7.

## Types

`src/lib/hub/openapi.d.ts` is generated from the hub's OpenAPI document:

```bash
npm run openapi                  # regenerates openapi.json + openapi.d.ts from ../hub
```

WebSocket-only shapes (`HubState`, `NowPlaying`, `Position`) are hand-mirrored in `src/lib/hub/types.ts`.

## Layout

```
src/app/            routes (App Router): / (shell), /dev/tokens
src/components/     UI per design system §6: NowPlaying, MiniPlayer, Scrubber, TransportRow,
                    VolumeSheet, ZonePicker, Sheet, Slider, Banner, Toasts, icons, ServiceBadge
src/lib/hub/        store (zustand), socket, commands (REST), state (delta apply), types, config
src/lib/            position interpolation, scrub math, coalescer, prefs, selectors, format
src/styles/         tokens.css (single source of truth), globals.css
e2e/                Playwright smoke
```
