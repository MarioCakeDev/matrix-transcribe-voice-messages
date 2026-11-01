import types

import pytest

from src.device_keys import (
    ensure_device_keys,
    mismatched_algorithms,
    published_identity,
)
from mautrix.types import KeyID

MXID = "@transcribe-bot:example.org"
DEVICE = "TRANSCRIBE"


def device_keys(curve25519, ed25519):
    return types.SimpleNamespace(
        keys={
            KeyID(algorithm="curve25519", key_id=DEVICE): curve25519,
            KeyID(algorithm="ed25519", key_id=DEVICE): ed25519,
        }
    )


class FakeAccount:
    def __init__(self, curve25519="local-curve", ed25519="local-ed"):
        self.identity_keys = {"curve25519": curve25519, "ed25519": ed25519}
        self.shared = True


class FakeCryptoStore:
    def __init__(self):
        self.saved = 0

    async def put_account(self, account):
        self.saved += 1


class FakeCrypto:
    def __init__(self):
        self.account = FakeAccount()
        self.share_calls = 0

    async def share_keys(self):
        self.share_calls += 1
        self.account.shared = True


class FakeClient:
    mxid = MXID
    device_id = DEVICE

    def __init__(self, published):
        self._published = published
        self.queried = 0

    async def query_keys(self, query):
        assert query == {MXID: [DEVICE]}
        self.queried += 1
        device = self._published
        return types.SimpleNamespace(
            device_keys={} if device is None else {MXID: {DEVICE: device}}
        )


class FailingClient(FakeClient):
    async def query_keys(self, query):
        raise RuntimeError("homeserver unreachable")


def test_published_identity_extracts_both_algorithms():
    keys = published_identity(device_keys("curve", "ed"))
    assert keys == {"curve25519": "curve", "ed25519": "ed"}


def test_mismatched_algorithms_flags_missing_keys():
    assert mismatched_algorithms({"curve25519": "c", "ed25519": "e"}, None) == [
        "curve25519",
        "ed25519",
    ]


def test_matching_keys_need_no_repair():
    account = FakeAccount()
    keys = published_identity(
        device_keys(account.identity_keys["curve25519"], account.identity_keys["ed25519"])
    )
    assert mismatched_algorithms(account.identity_keys, keys) == []


@pytest.mark.asyncio
async def test_matching_published_keys_do_nothing():
    crypto = FakeCrypto()
    store = FakeCryptoStore()
    keys = device_keys(crypto.account.identity_keys["curve25519"], crypto.account.identity_keys["ed25519"])
    client = FakeClient(keys)

    assert await ensure_device_keys(client, crypto, store) == "ok"
    assert store.saved == 0
    assert crypto.share_calls == 0
    assert crypto.account.shared is True


@pytest.mark.asyncio
async def test_stale_published_keys_are_republished():
    crypto = FakeCrypto()
    store = FakeCryptoStore()
    client = FakeClient(device_keys("stale-curve", "stale-ed"))

    assert await ensure_device_keys(client, crypto, store) == "repaired"
    # The automated form of `UPDATE crypto_account SET shared = 0`: mautrix only
    # uploads device keys while shared is False.
    assert store.saved == 1
    assert crypto.share_calls == 1
    assert crypto.account.shared is True


@pytest.mark.asyncio
async def test_missing_device_on_the_server_is_republished():
    crypto = FakeCrypto()
    store = FakeCryptoStore()
    client = FakeClient(None)

    assert await ensure_device_keys(client, crypto, store) == "repaired"
    assert crypto.share_calls == 1


@pytest.mark.asyncio
async def test_query_failure_is_not_fatal():
    crypto = FakeCrypto()
    store = FakeCryptoStore()
    client = FailingClient(device_keys("x", "y"))

    assert await ensure_device_keys(client, crypto, store) == "unknown"
    assert store.saved == 0
    assert crypto.share_calls == 0
