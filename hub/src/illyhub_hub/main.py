"""Entrypoint: ``uv run hub``."""

from __future__ import annotations

import uvicorn

from .api import create_app
from .config import Settings
from .logsetup import configure_logging


def run() -> None:  # pragma: no cover - exercised manually
    settings = Settings()
    configure_logging(settings.log_level, settings.log_file, settings.effective_stdout_level)
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_config=None,  # keep our JSON formatter
    )


if __name__ == "__main__":  # pragma: no cover
    run()
