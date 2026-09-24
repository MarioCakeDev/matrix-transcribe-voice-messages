"""Liveness heartbeat for the transcription bot.

The bot touches a small timestamp file whenever it makes progress (startup, a
MAS login attempt, the start of a sync, and every successful sync). The
container's ``HEALTHCHECK`` runs ``python -m src.health --check`` which exits
non-zero when that heartbeat has gone stale. A sync loop that is hung — or a
process stuck on a futex with no sockets, the original failure mode — therefore
stops beating and the container reports unhealthy instead of looking ``Up``.

This module intentionally has no third-party imports (stdlib only) so the
healthcheck cannot fail because of a broken E2EE/crypto dependency.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

HEARTBEAT_FILENAME = "sync_heartbeat"
DEFAULT_STORE_PATH = "./store"
DEFAULT_MAX_AGE = 120.0


def heartbeat_path(store_path: str) -> Path:
    """Return the heartbeat file path for a given store directory."""
    return Path(store_path) / HEARTBEAT_FILENAME


def write_beat(path: str | os.PathLike, now: float | None = None) -> float:
    """Atomically record the current time in the heartbeat file.

    Never raises: losing the heartbeat file must not take the bot down, it only
    makes the healthcheck report unhealthy.
    """
    path = Path(path)
    value = time.time() if now is None else now
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(repr(value))
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("Failed to write heartbeat %s: %s", path, exc)
    return value


def read_beat(path: str | os.PathLike) -> float | None:
    """Return the recorded timestamp, or ``None`` if missing/unreadable/corrupt."""
    try:
        raw = Path(path).read_text().strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def heartbeat_age(path: str | os.PathLike, now: float | None = None) -> float | None:
    """Seconds since the last heartbeat, or ``None`` if there was never one."""
    timestamp = read_beat(path)
    if timestamp is None:
        return None
    return (time.time() if now is None else now) - timestamp


def is_healthy(path: str | os.PathLike, max_age: float, now: float | None = None) -> bool:
    age = heartbeat_age(path, now=now)
    return age is not None and age <= max_age


def check_heartbeat(
    path: str | os.PathLike, max_age: float, now: float | None = None
) -> tuple[bool, str]:
    """Return ``(healthy, human_readable_message)`` for the heartbeat at *path*."""
    if max_age <= 0:
        raise ValueError("max_age must be > 0")
    age = heartbeat_age(path, now=now)
    if age is None:
        return False, f"unhealthy: no heartbeat at {path}"
    if age > max_age:
        return False, f"unhealthy: heartbeat is {age:.0f}s old (max {max_age:.0f}s)"
    return True, f"healthy: heartbeat {age:.0f}s old"


def _positive_float(raw: str, name: str) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number, got {raw!r}")
    if value <= 0:
        raise ValueError(f"{name} must be > 0, got {value!r}")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.health",
        description="Liveness check for the transcription bot.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 0 when the heartbeat is fresh, 1 when it is stale/missing",
    )
    parser.add_argument(
        "--store",
        default=os.environ.get("STORE_PATH", DEFAULT_STORE_PATH),
        help="store directory holding the heartbeat file (default: $STORE_PATH or ./store)",
    )
    parser.add_argument(
        "--max-age",
        dest="max_age",
        default=os.environ.get("HEALTH_MAX_AGE", str(DEFAULT_MAX_AGE)),
        help="maximum heartbeat age in seconds (default: $HEALTH_MAX_AGE or 120)",
    )
    args = parser.parse_args(argv)

    if not args.check:
        parser.error("nothing to do: pass --check")

    try:
        max_age = _positive_float(args.max_age, "max_age/HEALTH_MAX_AGE")
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    healthy, message = check_heartbeat(heartbeat_path(args.store), max_age)
    print(message, file=sys.stderr)
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
