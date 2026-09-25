import os
from dataclasses import dataclass


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got {raw!r}")


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number, got {raw!r}")


def _env_int_min(name: str, default: int, minimum: int) -> int:
    value = _env_int(name, default)
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value!r}")
    return value


def _env_float_min(name: str, default: float, minimum: float) -> float:
    value = _env_float(name, default)
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value!r}")
    return value


def _env_float_positive(name: str, default: float) -> float:
    value = _env_float(name, default)
    if value <= 0:
        raise ValueError(f"{name} must be > 0, got {value!r}")
    return value


@dataclass
class Config:
    homeserver: str
    user_id: str
    password: str
    parakeet_url: str
    device_id: str | None
    store_path: str
    recovery_key: str | None
    mas_url: str | None
    mas_login_max_attempts: int = 10
    mas_login_base_delay: float = 1.0
    mas_login_max_delay: float = 60.0
    mas_login_timeout: float = 30.0

    @classmethod
    def from_env(cls) -> "Config":
        required = {
            "MATRIX_HOMESERVER": "homeserver",
            "MATRIX_USER_ID": "user_id",
            "MATRIX_PASSWORD": "password",
            "PARAKEET_URL": "parakeet_url",
        }
        missing = [k for k in required if not os.environ.get(k)]
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

        homeserver = os.environ["MATRIX_HOMESERVER"]
        if not homeserver.startswith(("http://", "https://")):
            homeserver = f"https://{homeserver}"

        mas_url = os.environ.get("MATRIX_MAS_URL")
        if mas_url and not mas_url.startswith(("http://", "https://")):
            mas_url = f"https://{mas_url}"

        return cls(
            homeserver=homeserver,
            user_id=os.environ["MATRIX_USER_ID"],
            password=os.environ["MATRIX_PASSWORD"],
            parakeet_url=os.environ["PARAKEET_URL"],
            device_id=os.environ.get("MATRIX_DEVICE_ID"),
            store_path=os.environ.get("STORE_PATH", "./store"),
            recovery_key=os.environ.get("MATRIX_RECOVERY_KEY"),
            mas_url=mas_url,
            mas_login_max_attempts=_env_int_min("MAS_LOGIN_MAX_ATTEMPTS", 10, 1),
            mas_login_base_delay=_env_float_min("MAS_LOGIN_BASE_DELAY", 1.0, 0.0),
            mas_login_max_delay=_env_float_min("MAS_LOGIN_MAX_DELAY", 60.0, 0.0),
            mas_login_timeout=_env_float_positive("MAS_LOGIN_TIMEOUT", 30.0),
        )
