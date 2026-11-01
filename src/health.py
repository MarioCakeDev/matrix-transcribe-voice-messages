"""Liveness *and* correctness heartbeat for the transcription bot.

The bot touches a small timestamp file whenever it makes progress (startup, a
MAS login attempt, the start of a sync, and every successful sync). The
container's ``HEALTHCHECK`` runs ``python -m src.health --check`` which exits
non-zero when that heartbeat has gone stale. A sync loop that is hung — or a
process stuck on a futex with no sockets, the original failure mode — therefore
stops beating and the container reports unhealthy instead of looking ``Up``.

A fresh sync heartbeat is not enough, though: on 2026-10-31/11-01 the bot was
syncing happily and answering text messages while *every* encrypted voice
message was dropped at the crypto layer, so Docker reported ``healthy`` for days
while the bot transcribed nothing. ``health_state.json`` (written next to the
heartbeat, in the same ``$STORE_PATH``) carries the signals the heartbeat cannot
express:

* ``last_message_at`` / ``last_transcription_at`` — traffic flowing vs. work
  actually done;
* ``decrypt_failures`` — timestamps of recent events the crypto layer could not
  decrypt. An undecryptable event was very likely a voice message: it never
  reaches ``handle_message``, so this is the only place it can be counted;
* ``device_keys_ok`` — whether the device's published Olm keys match the local
  account (the state that used to require a manual ``shared=0`` reset);
* ``recovery_key_configured`` — whether key backup can be used at all.

``--check`` stays compatible with the old contract (a fresh heartbeat is enough
when no state file exists), and fails with a reason when the state says the bot
cannot decrypt.

This module intentionally has no third-party imports (stdlib only) so the
healthcheck cannot fail because of a broken E2EE/crypto dependency.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

HEARTBEAT_FILENAME = "sync_heartbeat"
STATE_FILENAME = "health_state.json"
DEFAULT_STORE_PATH = "./store"
DEFAULT_MAX_AGE = 120.0
DEFAULT_STALE_TRANSCRIBE_HOURS = 6.0
DEFAULT_DECRYPT_WINDOW_MINUTES = 60.0
DEFAULT_DECRYPT_THRESHOLD = 1
MAX_DECRYPT_FAILURES = 50

MISSING = object()


# ── sync heartbeat ──────────────────────────────────────────────────────────
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


# ── state file ──────────────────────────────────────────────────────────────
def state_path(store_path: str | os.PathLike) -> Path:
    """Return the health-state file path for a given store directory."""
    return Path(store_path) / STATE_FILENAME


def read_state(path: str | os.PathLike) -> dict:
    """Return the parsed state, or ``{}`` when missing/unreadable/corrupt."""
    try:
        raw = Path(path).read_text()
    except OSError:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        logger.warning("Corrupt health state at %s; ignoring it", path)
        return {}
    return parsed if isinstance(parsed, dict) else {}


def write_state(
    path: str | os.PathLike,
    now: float | None = None,
    **updates: object,
) -> dict:
    """Merge *updates* into the state file and return the result.

    Never raises: a health signal must not be able to take the bot down. Reads
    the current state first so callers can update one field without carrying the
    rest.
    """
    path = Path(path)
    value = time.time() if now is None else now
    state = read_state(path)
    state.update({key: val for key, val in updates.items() if val is not MISSING})
    state["updated_at"] = value
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(state, sort_keys=True))
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("Failed to write health state %s: %s", path, exc)
    return state


def record_message(path: str | os.PathLike, now: float | None = None) -> dict:
    """Record that a room message was seen (traffic is flowing)."""
    return write_state(path, now=now, last_message_at=time.time() if now is None else now)


def record_transcription(path: str | os.PathLike, now: float | None = None) -> dict:
    """Record that a voice message was transcribed and answered."""
    return write_state(path, now=now, last_transcription_at=time.time() if now is None else now)


def record_decrypt_failure(
    path: str | os.PathLike,
    room_id: str,
    event_id: str,
    *,
    now: float | None = None,
    window_minutes: float = DEFAULT_DECRYPT_WINDOW_MINUTES,
) -> dict:
    """Record an event the crypto layer could not decrypt.

    Keeps a bounded, decaying list of recent failures so the check can say *when*
    decryption stopped working, and the room/event id of the latest one — the
    evidence an operator needs to find the dropped voice message.
    """
    value = time.time() if now is None else now
    state = read_state(path)
    failures = [
        float(ts)
        for ts in state.get("decrypt_failures", [])
        if isinstance(ts, (int, float)) and value - float(ts) <= window_minutes * 60.0
    ]
    failures.append(value)
    failures = failures[-MAX_DECRYPT_FAILURES:]
    return write_state(
        path,
        now=value,
        decrypt_failures=failures,
        last_decrypt_failure_at=value,
        last_decrypt_failure_room=room_id,
        last_decrypt_failure_event=event_id,
    )


# ── evaluation ──────────────────────────────────────────────────────────────
def evaluate(
    store_path: str | os.PathLike,
    *,
    max_age: float = DEFAULT_MAX_AGE,
    state: dict | None = None,
    now: float | None = None,
    stale_transcribe_hours: float = DEFAULT_STALE_TRANSCRIBE_HOURS,
    decrypt_window_minutes: float = DEFAULT_DECRYPT_WINDOW_MINUTES,
    decrypt_threshold: int = DEFAULT_DECRYPT_THRESHOLD,
) -> tuple[bool, str]:
    """Return ``(healthy, message)`` for the whole health contract.

    A fresh heartbeat is necessary but not sufficient: the bot also reports
    unhealthy when it cannot decrypt (undecryptable traffic since its last
    successful transcription) or when its published device keys are known to be
    stale.
    """
    if max_age <= 0:
        raise ValueError("max_age must be > 0")
    if decrypt_threshold < 1:
        raise ValueError("decrypt_threshold must be >= 1")
    if stale_transcribe_hours <= 0:
        raise ValueError("stale_transcribe_hours must be > 0")

    value = time.time() if now is None else now
    healthy, message = check_heartbeat(heartbeat_path(store_path), max_age, now=value)
    if not healthy:
        return False, message

    if state is None:
        state = read_state(state_path(store_path))

    if state.get("device_keys_ok") is False:
        return False, (
            "unhealthy: the device keys published on the server do not match the "
            "local Olm account — inbound room keys cannot be decrypted"
        )

    recorded_failures = [
        float(ts)
        for ts in state.get("decrypt_failures", [])
        if isinstance(ts, (int, float))
    ]
    failures = [
        ts for ts in recorded_failures if value - ts <= decrypt_window_minutes * 60.0
    ]
    last_transcription = state.get("last_transcription_at")
    last_transcription_ts = (
        float(last_transcription) if isinstance(last_transcription, (int, float)) else None
    )
    if len(failures) >= decrypt_threshold:
        latest = max(failures)
        if last_transcription_ts is None or last_transcription_ts < latest:
            return False, (
                f"unhealthy: {len(failures)} undecryptable event(s) in the last "
                f"{decrypt_window_minutes:.0f} min and no transcription since "
                f"(latest room={state.get('last_decrypt_failure_room', '?')} "
                f"event={state.get('last_decrypt_failure_event', '?')})"
            )

    last_message = state.get("last_message_at")
    if isinstance(last_message, (int, float)):
        quiet_hours = (value - float(last_message)) / 3600.0
        if quiet_hours <= stale_transcribe_hours and last_transcription_ts is not None:
            stale_hours = (value - last_transcription_ts) / 3600.0
            if stale_hours > stale_transcribe_hours and recorded_failures:
                return False, (
                    f"unhealthy: no transcription for {stale_hours:.1f}h while "
                    "message traffic continued"
                )

    suffix = ""
    if last_transcription_ts is not None:
        suffix = f", last transcription {(value - last_transcription_ts) / 60.0:.0f} min ago"
    elif state:
        suffix = ", no transcription yet"
    return True, f"healthy: heartbeat {heartbeat_age(heartbeat_path(store_path), now=value):.0f}s old{suffix}"


def is_bot_healthy(
    store_path: str | os.PathLike,
    *,
    max_age: float = DEFAULT_MAX_AGE,
    now: float | None = None,
) -> bool:
    """Convenience wrapper: the full contract, ignoring the message."""
    return evaluate(store_path, max_age=max_age, now=now)[0]


# ── CLI ─────────────────────────────────────────────────────────────────────
def _positive_float(raw: str, name: str) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number, got {raw!r}")
    if value <= 0:
        raise ValueError(f"{name} must be > 0, got {value!r}")
    return value


def _positive_int(raw: str, name: str) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer, got {raw!r}")
    if value < 1:
        raise ValueError(f"{name} must be >= 1, got {value!r}")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.health",
        description="Liveness and decrypt-health check for the transcription bot.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 0 when the bot is healthy, 1 when it is stale or cannot decrypt",
    )
    parser.add_argument(
        "--store",
        default=os.environ.get("STORE_PATH", DEFAULT_STORE_PATH),
        help="store directory holding the heartbeat and health state (default: $STORE_PATH or ./store)",
    )
    parser.add_argument(
        "--max-age",
        dest="max_age",
        default=os.environ.get("HEALTH_MAX_AGE", str(DEFAULT_MAX_AGE)),
        help="maximum heartbeat age in seconds (default: $HEALTH_MAX_AGE or 120)",
    )
    parser.add_argument(
        "--stale-transcribe-hours",
        dest="stale_transcribe_hours",
        default=os.environ.get(
            "HEALTH_STALE_TRANSCRIBE_HOURS", str(DEFAULT_STALE_TRANSCRIBE_HOURS)
        ),
        help="hours without a transcription, while traffic flows, before reporting unhealthy",
    )
    parser.add_argument(
        "--decrypt-window-minutes",
        dest="decrypt_window_minutes",
        default=os.environ.get(
            "HEALTH_DECRYPT_WINDOW_MINUTES", str(DEFAULT_DECRYPT_WINDOW_MINUTES)
        ),
        help="window in minutes over which undecryptable events are counted",
    )
    parser.add_argument(
        "--decrypt-threshold",
        dest="decrypt_threshold",
        default=os.environ.get("HEALTH_DECRYPT_THRESHOLD", str(DEFAULT_DECRYPT_THRESHOLD)),
        help="undecryptable events within the window that make the bot unhealthy",
    )
    args = parser.parse_args(argv)

    if not args.check:
        parser.error("nothing to do: pass --check")

    try:
        max_age = _positive_float(args.max_age, "max_age/HEALTH_MAX_AGE")
        stale_hours = _positive_float(
            args.stale_transcribe_hours, "stale_transcribe_hours/HEALTH_STALE_TRANSCRIBE_HOURS"
        )
        window = _positive_float(
            args.decrypt_window_minutes, "decrypt_window_minutes/HEALTH_DECRYPT_WINDOW_MINUTES"
        )
        threshold = _positive_int(
            args.decrypt_threshold, "decrypt_threshold/HEALTH_DECRYPT_THRESHOLD"
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    healthy, message = evaluate(
        args.store,
        max_age=max_age,
        stale_transcribe_hours=stale_hours,
        decrypt_window_minutes=window,
        decrypt_threshold=threshold,
    )
    print(message, file=sys.stderr)
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
