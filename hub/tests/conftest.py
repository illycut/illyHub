from __future__ import annotations

import pytest

from illyhub_hub.config import Settings
from illyhub_hub.state import StateStore


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, fake_devices=True, reconnect_max_s=0.05, position_poll_s=0.02)


@pytest.fixture
def store() -> StateStore:
    return StateStore()
