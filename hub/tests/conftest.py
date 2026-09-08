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


# --------------------------------------------------------------------------------------
# User-facing copy audit: every message a person can read (CommandError → error envelopes and
# partial failures, AckWarning, NeedsLinkError) is recorded across the whole session, and
# tests/test_zz_vendor_labels.py fails if any of them names an ecosystem by its raw id
# ("heos", "sonos") instead of its label ("HEOS", "Sonos").
# --------------------------------------------------------------------------------------

RECORDED_MESSAGES: list[tuple[str, str]] = []


@pytest.fixture(autouse=True, scope="session")
def _record_user_messages() -> None:
    from illyhub_hub import commands, content

    orig_cmd_init = commands.CommandError.__init__
    orig_link_init = content.NeedsLinkError.__init__
    orig_warning_post = commands.AckWarning.model_post_init

    def cmd_init(self, code, message, target=None):  # type: ignore[no-untyped-def]
        RECORDED_MESSAGES.append(("CommandError", message))
        orig_cmd_init(self, code, message, target)

    def link_init(self, service, message=None):  # type: ignore[no-untyped-def]
        orig_link_init(self, service, message)
        RECORDED_MESSAGES.append(("NeedsLinkError", self.message))

    def warning_post(self, ctx):  # type: ignore[no-untyped-def]
        RECORDED_MESSAGES.append(("AckWarning", self.message))
        orig_warning_post(self, ctx)

    commands.CommandError.__init__ = cmd_init  # type: ignore[method-assign]
    content.NeedsLinkError.__init__ = link_init  # type: ignore[method-assign]
    commands.AckWarning.model_post_init = warning_post  # type: ignore[method-assign]
