# illyHub

Unified touch control for a mixed HEOS (Denon/Marantz) + Sonos home: a local hub service on an
always-on Mac and a touch PWA it serves. Start with `CLAUDE.md`, then `docs/multi-room-audio-PRD.md`
(the requirements), `docs/api.md` (the contract), `hub/README.md` and `app/README.md`.

## CI

`.github/workflows/ci.yml` runs on pull requests and on pushes to `main` when `hub/`, `app/` or the
workflow itself changes (docs-only changes skip it):

- **hub** — `uv sync --frozen`, `ruff check`, `ruff format --check`, `pytest` with the 80% line
  coverage gate from `hub/pyproject.toml`; `coverage.xml` uploaded as an artifact.
- **app** — `npm ci`, `tsc --noEmit`, `eslint`, `vitest run --coverage` (thresholds in
  `app/vitest.config.ts`), `next build`; coverage uploaded as an artifact.
- **e2e** (after both) — installs WebKit and Chromium, then `playwright test`; the Playwright
  `webServer` builds the export and starts the fake hub (`HUB_FAKE_DEVICES=1` plus the fake
  services and `HUB_AIRPLAY_ENABLED=1`). Reports and traces are uploaded on failure only.

`HUB_DATA_DIR` points at the runner's temp directory so the fake hub never writes into the
checkout. Dependabot (`.github/dependabot.yml`) opens weekly grouped minor/patch updates for the
hub (pip), the app (npm) and the actions.

`android.yml` builds the Capacitor Android shell on every push to `main` and refreshes the `android-latest` pre-release; see `docs/android.md`.
