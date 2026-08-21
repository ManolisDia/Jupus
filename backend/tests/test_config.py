import pytest

from backend.config import Settings


def test_settings_raises_when_openai_key_missing(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    with pytest.raises(Exception):
        Settings(_env_file=None)


def test_settings_loads_from_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    settings = Settings(_env_file=None)
    assert settings.openai_api_key == "sk-openai-test"
    assert settings.anthropic_api_key == "sk-ant-test"
