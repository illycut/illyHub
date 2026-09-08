# Android APK

The illyHub client is a PWA served by the hub. This document covers the second way onto an
Android phone: a small native shell (Capacitor) that bundles the same static export into an APK.
The browser PWA remains the zero-install option and behaves identically; the shell exists for
phones where "Add to Home Screen" is awkward (no HTTPS on the LAN yet, or a household member who
just wants an icon).

## Install on a phone

1. On the phone, open the repo's releases page and download `illyhub-android-debug.apk` from the
   **`android-latest`** pre-release. CI replaces that asset on every push to `main`; the release
   body says which app version and build number it is (the tag itself is not moved).
2. Open the download. Android asks to allow installs from this source (Chrome or Files); allow it
   once. The build is signed with a debug key, so the first install shows the usual "unknown
   developer" wording. Updating later means installing the new APK over the old one; the stored
   hub address survives.
3. Open **illyHub**. The first screen asks **Where is the hub?** Enter the address you use in a
   browser on the same Wi-Fi, for example `http://192.168.50.10:8080` or
   `http://hub-mac.local:8080` (a bare `192.168.50.10` also works; `http://` and `:8080` are
   added). Tap **Connect**. The app checks `/api/health` and shows the hub version, then
   **Use this address**.
4. That is it. Change or forget the address later under **Settings → Hub → Hub address**.

Plain HTTP on the LAN is fine for the shell: it is allowed to talk cleartext to private LAN
addresses and `.local` names, unlike a browser's install prompt which needs HTTPS. If the hub later serves HTTPS (runbook §3a, Tailscale
or mkcert), enter the `https://` address instead; the WebSocket follows the scheme automatically.

In principle nothing is unavailable in the shell: it is the PWA with a native window around it,
so Pandora Sync, Sync Play, search, the queue and the update row are all there. **On-device
verification is pending**; until the first phone run, treat that as expected, not confirmed.

Cleartext is permitted only to private LAN ranges (`10.0.0.0/8`, `172.16.0.0/12`,
`192.168.0.0/16`), `*.local` names and `localhost` (`res/xml/network_security_config.xml`); any
other address must be `https://`. Never treat the `android-latest` APK as a release build: it is
debug-signed and built from whatever is on `main`.

## How it works

- `app/capacitor.config.ts`: `appId com.illyhub.app`, `webDir out`, `androidScheme http` and
  `cleartext: true` (stable localStorage across builds, cleartext LAN allowed),
  `allowMixedContent`. The manifest also sets `android:usesCleartextTraffic="true"` and
  `INTERNET`.
- `app/android/`: the Capacitor Android project, checked in for reproducible builds. Generated
  with `npx cap add android` and `npx @capacitor/assets generate --android` from
  `app/assets/` (the app icon on a `bg-base` background). `versionName` follows
  `app/package.json`; `versionCode` is `ANDROID_VERSION_CODE` (CI run number) or 1.
- Runtime hub address: `app/src/lib/hub/hubBase.ts`. The stored address wins over the build-time
  `NEXT_PUBLIC_HUB_URL` and the page origin (`config.ts`). `NativeGate` shows the address screen
  only when `Capacitor.isNativePlatform()` is true (or `window.__ILLYHUB_NATIVE__` is set) and
  nothing is stored; a browser never sees it. Saving reloads the page so every module reads the
  new base.
- The service worker does not register in the shell: `localhost` counts as a secure context, so
  the registration is skipped explicitly (`ServiceWorker.tsx`, `isNativeShell()`). The export is
  on disk inside the APK, so a cached shell would only mask stale files.
- The stored address is used only inside the shell (`hubHttpBase()` checks `isNativeShell()`);
  a browser tab always talks to the origin that served it. The hub answers CORS for the shell's
  `http://localhost` origin (docs/api.md, "CORS").

## Build locally

Requirements: Node 20+, JDK 21, Android SDK (platform 35, build-tools 35). Android Studio's
SDK install is enough; set `ANDROID_HOME`.

```bash
cd app
npm ci
npm run android:build         # next build → cap sync android → gradlew assembleDebug
# → app/android/app/build/outputs/apk/debug/app-debug.apk
adb install -r android/app/build/outputs/apk/debug/app-debug.apk
```

`npm run android:release` produces an **unsigned** release APK at
`app/android/app/build/outputs/apk/release/app-release-unsigned.apk`; it cannot be installed
until signed (below). `npm run android:sync` alone refreshes the web assets into the project.

## CI

`.github/workflows/android.yml` runs on every push to `main` that touches `app/` (and on
demand): Node 20, Temurin JDK 21, the Android SDK packages the template expects, Gradle cache,
`npm ci`, `npm run android:build` with `ANDROID_VERSION_CODE=<run number>`, then uploads the APK
as the versioned `illyhub-android-debug` artifact (30 days) and replaces the
`illyhub-android-debug.apk` asset on the rolling `android-latest` pre-release. It is separate from `ci.yml` so the PR gate stays fast. Nothing here is
verified locally beyond YAML validity; the first push is the real test.

## Signing a real release (later)

A debug-signed APK is fine for a household. For a release build:

```bash
keytool -genkeypair -v -keystore ~/illyhub-release.jks -alias illyhub -keyalg RSA -keysize 4096 -validity 10000
```

Then in `app/android/app/build.gradle` add a `signingConfigs.release` block that reads the store
path, store password, key alias and key password from environment variables (never commit them),
point `buildTypes.release.signingConfig` at it, and run `npm run android:release`. In CI, keep the
keystore as a base64 secret decoded at build time. Keep the keystore backed up: losing it means
users must uninstall before installing a differently-signed build.

## Known limits

- The first screen cannot discover the hub by itself (no mDNS from a WebView). The address has to
  be typed once; the hub Mac's `.local` name works when the router resolves it.
- No iOS shell. iPhones use the PWA (Share → Add to Home Screen).
- The APK is built on Linux CI; the hub itself is not involved, so a hub update never requires a
  new APK unless the client changed.
