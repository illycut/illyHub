from __future__ import annotations

from pathlib import Path

import pytest

from illyhub_hub.config import Settings
from illyhub_hub.state import StateStore


@pytest.fixture(autouse=True)
def _isolate_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests that build Settings() directly must never write the art cache into the repo or
    pick up a developer's app export."""
    monkeypatch.setenv("HUB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HUB_APP_DIR", str(tmp_path / "no-app"))
    monkeypatch.setenv("HUB_FONTS_DIR", str(tmp_path / "no-fonts"))


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        fake_devices=True,
        reconnect_max_s=0.05,
        position_poll_s=0.02,
        command_coalesce_s=0.0,
        data_dir=str(tmp_path / "data"),  # never write the art cache into the repo
        app_dir=str(tmp_path / "no-app"),
        fonts_dir=str(tmp_path / "no-fonts"),
    )


@pytest.fixture
def store() -> StateStore:
    return StateStore()
