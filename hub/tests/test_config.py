from __future__ import annotations

import importlib

from illyhub_hub import config
from illyhub_hub.config import Settings


def test_settings_defaults_and_env_prefix(monkeypatch) -> None:
    monkeypatch.setenv("HUB_PORT", "9090")
    monkeypatch.setenv("HUB_FAKE_DEVICES", "1")
    monkeypatch.setenv("HUB_STATIC_DEVICES", '[{"id":"amp","vendor":"heos","ip":"10.0.0.5"}]')
    s = Settings(_env_file=None)
    assert s.port == 9090 and s.fake_devices is True
    assert s.static_devices[0].vendor == "heos" and s.host == "0.0.0.0"


def test_effective_stdout_level() -> None:
    assert Settings(_env_file=None).effective_stdout_level == "INFO"
    assert Settings(_env_file=None, log_level="DEBUG").effective_stdout_level == "DEBUG"
    assert Settings(_env_file=None, log_file="/tmp/x.log").effective_stdout_level == "WARNING"
    assert (
        Settings(_env_file=None, log_file="/tmp/x.log", stdout_level="ERROR").effective_stdout_level
        == "ERROR"
    )


def test_hub_env_file_is_honored(tmp_path, monkeypatch) -> None:
    env = tmp_path / "custom.env"
    env.write_text("HUB_PORT=7001\n")
    monkeypatch.setenv("HUB_ENV_FILE", str(env))
    monkeypatch.delenv("HUB_PORT", raising=False)
    try:
        importlib.reload(config)
        assert config.Settings.model_config["env_file"] == str(env)
        assert config.Settings().port == 7001
    finally:
        monkeypatch.delenv("HUB_ENV_FILE")
        importlib.reload(config)
    assert config.Settings.model_config["env_file"] == ".env"
