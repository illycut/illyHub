# illyhub — Multi-Room Audio Control (HEOS + Sonos)

Unified touch control for a mixed HEOS (Denon/Marantz) and Sonos home, delivered as a
**local hub service** on an always-on Mac plus a **touch PWA client** that talks only to the hub.
Owner: James. Status: PRD approved, pre-code. Personal/household product, not App Store.

## Source of truth
- `docs/multi-room-audio-PRD.md` — requirements with IDs (LIB-*, CTL-*, VOL-*, NP-*, HOME-*, PAN-*, ZON-*, SET-*), architecture, sync engine design, build phases 0–7, resolved decisions.
- `docs/multi-room-audio-design-system.md` — tokens, type scale, components, motion, states, voice.
- `docs/prd-review.md` — review findings and recommended plan changes (Sept 7, 2026). Read before starting Phase 0 or Phase 3 work.
- `docs/backlog.md` — mirror of the GitHub issue backlog, grouped by phase.
- Code lives in `illycut/illyHub` (this repo, public). The original 81-issue backlog lives in `illycut/ai-dev` issues #1–#81 with milestones `Phase 0 — Foundation` through `Cross-cutting`; close those as work lands. **New issues go in `illycut/illyHub`.** Labels used: `hub`, `app`, `spike`, `infra`, `design`, `P0`/`P1`/`P2`.

Reference PRD requirement IDs in issue titles, commit messages, and PR descriptions.

## Layout
```
illyHub/
  hub/        Python 3.12+, FastAPI + uvicorn, managed with uv. pyheos, SoCo, tidalapi, ytmusicapi, Pillow, SQLite.
  app/        Next.js PWA, TypeScript, React, Tailwind mapped to design tokens. Served as a static export by the hub.
  docs/       PRD, design system, review, backlog.
  ops/        launchd plist, deploy/update scripts, hub Mac runbook.
```
Directories appear as phases land. Check before assuming a file exists.

## Dev loop (agreed Sept 7, 2026)
Per phase: build → code review (code-review-analyst) → UX review for anything front-end (mobile-ux-designer / design-systems-architect) → remediate → unit tests → coverage gate **80% lines** → commit to `main` → close the matching `ai-dev` issues → next phase.
Hardware is not reachable from the dev machine. Build and test against the protocol fakes; anything verified only against fakes must say so in the commit message and issue comment, and the hub Mac runbook lists what to verify on the LAN.

## Architecture rules
- Client is dumb. All protocol logic, auth, art handling, and sync live in the hub. The app only renders hub state and sends normalized commands.
- One persistent HEOS TCP socket (port 1255) owned by the hub. Never open a second one.
- Sonos via UPnP/SOAP (port 1400) with event subscriptions; the hub runs the callback listener. Position is polled at 1 Hz.
- Denon power (`ZMON`/`ZMOFF`, `Z2ON`/`Z2OFF`) goes over the legacy telnet/HTTP API (port 23 / `/goform/formiPhoneAppDirect.xml`), not the HEOS CLI. Telnet allows one client at a time.
- State push over WebSocket: full snapshot on connect, deltas after. Commands over REST with correlation IDs so the UI can be optimistic and reconcile.
- "Browse once, play anywhere": browse via the service's own API (Tidal, YT Music), play on each device by constructing that device's ref from the canonical service ID. Sync Play keys on Tidal track IDs. SoCo MusicService browsing is a fallback, not the primary path.
- A "side" is one ecosystem's native group (HEOS group or Sonos group coordinator). The sync engine syncs two sides, never N players.
- Art is always proxied by the hub (CORS, resolution normalization, 30-day disk cache, pre-blurred backdrop variant, server-side accent extraction with contrast check). The client never computes color.
- Play history is hub-native SQLite. Neither vendor exposes listening history.
- No multi-user auth in v1. Trusted LAN + Tailscale is the boundary. Never expose the hub to the public internet.
- Seek is disabled for radio sources (Pandora). Capability flags travel with now-playing state.

## Design rules (from the design system)
- Dark only. Artwork carries color; chrome is neutral (`bg-base #101014`, `bg-raised #1A1A20`, `bg-overlay #24242C`).
- One accent, signal amber `#F0A63C`, means "live audio state." Never decorative. Sync Play is the only amber-filled button.
- General Sans self-hosted from the hub, tabular numerals wherever time or volume appears. Sentence case everywhere.
- Touch targets 48px minimum, 56px on the transport row, 8px minimum gap between adjacent targets.
- Service identity via official brand-kit logo chips, never text labels, never redrawn marks.
- Errors state facts and fixes, never apologize. Sync copy says "Synced" / "Adjusting" / "Sync lost", never "perfect."
- Tokens ship as CSS custom properties in `:root`; Tailwind maps to them. Design system lives in one file.

## Known constraints to remember
- Tidal and YouTube Music Premium may enforce one concurrent stream per account. Sync Play depends on the Phase 0 manual test passing (see review §1.1).
- Sonos position reports are whole-second; sub-second drift needs tick-edge interpolation.
- PWA service workers need HTTPS. Serve via `tailscale serve` or mkcert. No haptics on iOS Safari.
- FileVault and macOS auto-login are mutually exclusive on the hub Mac.
- YouTube Music on HEOS requires yt-dlp stream resolution plus a hub-side proxy; P1, time-boxed spike.

## Working conventions
- Python: `uv` for env and deps, `ruff` for lint/format, `pytest` with protocol fakes (no hardware needed for unit tests). Type hints everywhere, pydantic models for API and state.
- TypeScript: strict mode, ESLint, Playwright for E2E against a fake hub.
- Prefer small PRs scoped to one issue. Title format: `[hub|app] <PRD-ID> short description`.
- Hardware testing happens on the owner's LAN. Say explicitly when something was verified only against fakes.
- Do not commit tokens, OAuth secrets, or the auth vault. Config via `.env` (gitignored) or macOS Keychain.

## Repo note
This checkout may sit inside the `ai-dev` monorepo working tree at `ai-dev/illyhub/illyHub/`, but it is its own git repo with its own remote. Never run git commands against the parent `ai-dev` repo from here.
