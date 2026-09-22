import pytest

from src.config import Config

REQUIRED = {
    "MATRIX_HOMESERVER": "https://matrix.example",
    "MATRIX_USER_ID": "@bot:example",
    "MATRIX_PASSWORD": "pw",
    "PARAKEET_URL": "http://whisper:5092",
}


def set_required(monkeypatch):
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)


def test_login_retry_defaults(monkeypatch):
    set_required(monkeypatch)
    for key in (
        "MAS_LOGIN_MAX_ATTEMPTS",
        "MAS_LOGIN_BASE_DELAY",
        "MAS_LOGIN_MAX_DELAY",
        "MAS_LOGIN_TIMEOUT",
    ):
        monkeypatch.delenv(key, raising=False)

    config = Config.from_env()

    assert config.mas_login_max_attempts == 10
    assert config.mas_login_base_delay == 1.0
    assert config.mas_login_max_delay == 60.0
    assert config.mas_login_timeout == 30.0


def test_login_retry_overrides(monkeypatch):
    set_required(monkeypatch)
    monkeypatch.setenv("MAS_LOGIN_MAX_ATTEMPTS", "4")
    monkeypatch.setenv("MAS_LOGIN_BASE_DELAY", "0.5")
    monkeypatch.setenv("MAS_LOGIN_MAX_DELAY", "12")
    monkeypatch.setenv("MAS_LOGIN_TIMEOUT", "7.5")

    config = Config.from_env()

    assert config.mas_login_max_attempts == 4
    assert config.mas_login_base_delay == 0.5
    assert config.mas_login_max_delay == 12.0
    assert config.mas_login_timeout == 7.5


def test_invalid_login_retry_value_raises(monkeypatch):
    set_required(monkeypatch)
    monkeypatch.setenv("MAS_LOGIN_MAX_ATTEMPTS", "not-a-number")

    with pytest.raises(ValueError):
        Config.from_env()


@pytest.mark.parametrize("value", ["0", "-1"])
def test_max_attempts_below_one_raises(monkeypatch, value):
    set_required(monkeypatch)
    monkeypatch.setenv("MAS_LOGIN_MAX_ATTEMPTS", value)

    with pytest.raises(ValueError):
        Config.from_env()


@pytest.mark.parametrize("name", ["MAS_LOGIN_BASE_DELAY", "MAS_LOGIN_MAX_DELAY"])
def test_negative_delay_raises(monkeypatch, name):
    set_required(monkeypatch)
    monkeypatch.setenv(name, "-0.5")

    with pytest.raises(ValueError):
        Config.from_env()
