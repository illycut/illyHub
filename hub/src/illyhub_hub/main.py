"""Entrypoint: ``uv run hub``."""

from __future__ import annotations

import os

import uvicorn

from .api import create_app
from .config import Settings
from .logsetup import configure_logging


def uvicorn_kwargs(settings: Settings) -> dict[str, object]:
    """Server options derived from settings. TLS (mkcert path) needs both cert and key; with
    only one set we refuse rather than silently serve plain HTTP."""
    kwargs: dict[str, object] = {
        "host": settings.host,
        "port": settings.port,
        "log_config": None,  # keep our JSON formatter
    }
    cert, key = settings.tls_cert_path, settings.tls_key_path
    if bool(cert) != bool(key):
        raise ValueError("HUB_TLS_CERT and HUB_TLS_KEY must be set together")
    if cert and key:
        for label, path in (("HUB_TLS_CERT", cert), ("HUB_TLS_KEY", key)):
            if not path.is_file():
                raise ValueError(f"{label} points at {path}, which is not a file")
        kwargs["ssl_certfile"] = str(cert)
        kwargs["ssl_keyfile"] = str(key)
    return kwargs


def run() -> None:  # pragma: no cover - exercised manually
    settings = Settings()
    configure_logging(settings.log_level, settings.log_file, settings.effective_stdout_level)
    # os._exit is wired only here: POST /api/hub/restart ends the process and launchd relaunches.
    app = create_app(settings, exit_fn=os._exit)
    uvicorn.run(app, **uvicorn_kwargs(settings))  # type: ignore[arg-type]


if __name__ == "__main__":  # pragma: no cover
    run()
