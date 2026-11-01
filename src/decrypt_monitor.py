"""Make undecryptable events observable instead of silently dropped.

mautrix's ``DecryptionDispatcher`` decrypts ``m.room.encrypted`` events and, on
failure, logs ``Failed to decrypt <event id>: <error>`` through ``crypto_log``
and returns — the handler never runs, so the bot never learns that a message
arrived. That is how the bot sat ``healthy`` for two days on 2026-10-31/11-01
while every voice message was dropped at the crypto layer.

Wrapping ``OlmMachine.decrypt_megolm_event`` is the narrowest seam that sees
every failure with the event's room and id, works for any dispatcher wiring, and
does not change mautrix's behaviour: the exception is re-raised, so the
dispatcher still logs and drops it exactly as before.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from mautrix.errors import DecryptionError

logger = logging.getLogger(__name__)

FailureHook = Callable[[str, str, BaseException], None]


def install_decrypt_failure_logging(crypto: Any, on_failure: FailureHook | None = None):
    """Wrap ``crypto.decrypt_megolm_event``; return an uninstall callable.

    Every ``DecryptionError`` is logged at ERROR with the room id and the event
    id — the evidence needed to find the dropped message — and reported to
    *on_failure* when given, before the original behaviour (re-raise) applies.
    """
    original = crypto.decrypt_megolm_event

    async def decrypt_megolm_event(event: Any):
        try:
            return await original(event)
        except DecryptionError as exc:
            room_id = str(getattr(event, "room_id", "") or "")
            event_id = str(getattr(event, "event_id", "") or "")
            sender = str(getattr(event, "sender", "") or "")
            logger.error(
                "Undecryptable event %s in %s (sender %s): %s — it did not reach "
                "handle_message; treat it as a potentially lost voice message",
                event_id or "?",
                room_id or "?",
                sender or "?",
                exc,
            )
            if on_failure is not None:
                try:
                    on_failure(room_id, event_id, exc)
                except Exception:  # pragma: no cover - defensive
                    logger.exception("Decrypt-failure hook raised")
            raise

    crypto.decrypt_megolm_event = decrypt_megolm_event

    def uninstall() -> None:
        crypto.decrypt_megolm_event = original

    return uninstall


__all__ = ["FailureHook", "install_decrypt_failure_logging"]
