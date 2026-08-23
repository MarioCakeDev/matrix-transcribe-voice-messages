import os
from dataclasses import dataclass


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
        )
