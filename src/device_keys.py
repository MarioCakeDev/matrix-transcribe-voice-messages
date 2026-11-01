"""Detect and repair "my published device keys are stale".

The failure this fixes, seen five times in production: the bot's Olm account is
fine locally, but the device keys the homeserver publishes for it no longer
match that account (a recreated account, a device reset, or a federation hiccup).
mautrix only uploads device keys when ``OlmAccount.shared`` is ``False``
(``OlmMachine._share_keys``), and ``shared`` lives in ``crypto_account`` as a
persisted "already published" flag — so once it is ``True`` the bot keeps
uploading one-time keys *only* and never re-publishes its identity keys. Other
clients then keep encrypting to the stale identity, the bot cannot decrypt
anything, and the only recovery used to be a hand-run

    UPDATE crypto_account SET shared = 0

followed by a restart. This module performs exactly that, automatically, only
when the server's keys and the local account actually disagree.
"""

from __future__ import annotations

import logging
from typing import Any, Literal, Mapping

logger = logging.getLogger(__name__)

#: ``ok`` — the server's keys match; ``repaired`` — keys were re-published;
#: ``unknown`` — the check could not run (never fatal at startup).
KeyStatus = Literal["ok", "repaired", "unknown"]

# Olm uses both; a mismatch in either makes inbound sessions undecryptable.
ALGORITHMS = ("curve25519", "ed25519")


def local_identity(account: Any) -> dict[str, str]:
    """Return ``{algorithm: key}`` for a local ``OlmAccount``."""
    return {str(name): str(value) for name, value in account.identity_keys.items()}


def published_identity(device_keys: Any) -> dict[str, str]:
    """Return ``{algorithm: key}`` from a mautrix ``DeviceKeys`` object."""
    out: dict[str, str] = {}
    for key_id, value in (getattr(device_keys, "keys", None) or {}).items():
        algorithm = getattr(key_id, "algorithm", None)
        if algorithm is None and isinstance(key_id, str) and ":" in key_id:
            algorithm = key_id.split(":", 1)[0]
        if algorithm in ALGORITHMS:
            out[str(algorithm)] = str(value)
    return out


def mismatched_algorithms(
    local: Mapping[str, str], published: Mapping[str, str] | None
) -> list[str]:
    """Return the algorithms whose published key is missing or different."""
    bad: list[str] = []
    for algorithm in ALGORITHMS:
        wanted = local.get(algorithm)
        if not wanted:
            continue
        got = None if published is None else published.get(algorithm)
        if got != wanted:
            bad.append(algorithm)
    return bad


async def fetch_published_identity(
    client: Any, mxid: str, device_id: str
) -> dict[str, str] | None:
    """Return the server's identity keys for one device, or ``None`` if absent."""
    response = await client.query_keys({mxid: [device_id]})
    device_keys = (getattr(response, "device_keys", None) or {}).get(mxid, {}).get(device_id)
    if device_keys is None:
        return None
    return published_identity(device_keys)


async def ensure_device_keys(
    client: Any,
    crypto: Any,
    crypto_store: Any,
    *,
    log: logging.Logger = logger,
) -> KeyStatus:
    """Re-publish this device's keys when they are missing or stale.

    Returns ``"repaired"`` when keys were re-published, ``"ok"`` when they
    already match, and ``"unknown"`` when the check could not run (never fatal
    at startup).
    """
    mxid = getattr(client, "mxid", None)
    device_id = getattr(client, "device_id", None)
    if not mxid or not device_id:
        log.warning("Cannot check the published device keys without an mxid and device_id")
        return "unknown"

    try:
        published = await fetch_published_identity(client, mxid, device_id)
    except Exception:
        log.warning(
            "Could not query the published device keys for %s/%s; skipping the key check",
            mxid,
            device_id,
            exc_info=True,
        )
        return "unknown"

    bad = mismatched_algorithms(local_identity(crypto.account), published)
    if not bad:
        log.info("Published device keys for %s match the local Olm account", device_id)
        return "ok"

    log.error(
        "Device %s has %s key(s) on the server that do not match the local Olm "
        "account; re-publishing identity keys (automated form of "
        "`UPDATE crypto_account SET shared=0`)",
        device_id,
        " and ".join(bad),
    )
    # mautrix only uploads device keys while `shared` is False.
    crypto.account.shared = False
    await crypto_store.put_account(crypto.account)
    await crypto.share_keys()
    log.warning("Re-published device keys for %s after a mismatch", device_id)
    return "repaired"


__all__ = [
    "ALGORITHMS",
    "KeyStatus",
    "ensure_device_keys",
    "fetch_published_identity",
    "local_identity",
    "mismatched_algorithms",
    "published_identity",
]
