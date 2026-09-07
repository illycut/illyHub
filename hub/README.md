# illyHub hub service

Python 3.12 / FastAPI service that owns the HEOS socket, Sonos UPnP subscriptions,
and the Denon control link, and exposes one normalized state model over REST (and,
from Phase 1, WebSocket). See `../docs/multi-room-audio-PRD.md`.

## Setup

```
uv sync                # installs runtime + dev deps into .venv (Python 3.12 via uv)
cp .env.example .env   # edit as needed
```

System `python3` is not used; always run through `uv run`.

## Run

```
HUB_FAKE_DEVICES=1 uv run hub      # boots with protocol fakes, no hardware needed
uv run hub                         # real adapters (needs HUB_HEOS_HOST / HUB_DENON_HOST or SSDP)
```

Then `curl localhost:8080/api/health` and `curl localhost:8080/api/devices`.
OpenAPI docs at `http://localhost:8080/docs`.

## Test and coverage

```
uv run ruff check && uv run ruff format --check
uv run pytest --cov=illyhub_hub --cov-report=term-missing   # fails under 80% line coverage
```

## Layout

```
src/illyhub_hub/
  config.py        pydantic-settings (HUB_* env, .env)
  logsetup.py      JSON log formatter with correlation_id contextvar, optional rotating file
  state.py         HubState + sub-models, StateStore, diff()
  discovery.py     SSDP + static device registry
  adapters/        base interfaces, heos (pyheos), sonos (SoCo), denon (HTTP/telnet), fake
  tasks.py         tracked fire-and-forget tasks, cancellation-safe stop_task()
  api.py           FastAPI app factory: /api/health, /api/devices
  main.py          uvicorn entrypoint
```

## Verification status

Real adapters (`heos.py`, `sonos.py`, `denon.py`) are unit-tested against mocks only.
Nothing in this package has been run against physical hardware yet; see
`../ops/RUNBOOK.md` for the LAN verification checklist.
