import logging
import types

import pytest

from mautrix.errors import DecryptionError

from src.decrypt_monitor import install_decrypt_failure_logging


def encrypted_event(room_id="!room:example.org", event_id="$enc1", sender="@mario:example.org"):
    return types.SimpleNamespace(room_id=room_id, event_id=event_id, sender=sender)


class FakeCrypto:
    def __init__(self, error=None):
        self.error = error
        self.calls = 0

    async def decrypt_megolm_event(self, event):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return "decrypted"


@pytest.mark.asyncio
async def test_successful_decryption_passes_through():
    crypto = FakeCrypto()
    seen = []
    install_decrypt_failure_logging(crypto, lambda *args: seen.append(args))

    result = await crypto.decrypt_megolm_event(encrypted_event())

    assert result == "decrypted"
    assert seen == []


@pytest.mark.asyncio
async def test_failure_is_reported_with_room_and_event_id(caplog):
    crypto = FakeCrypto(DecryptionError("no session with given ID abc found"))
    seen = []
    install_decrypt_failure_logging(
        crypto, lambda room, event, exc: seen.append((room, event, str(exc)))
    )

    with caplog.at_level(logging.ERROR, logger="src.decrypt_monitor"):
        with pytest.raises(DecryptionError):
            await crypto.decrypt_megolm_event(encrypted_event())

    assert seen == [
        ("!room:example.org", "$enc1", "no session with given ID abc found")
    ]
    assert "$enc1" in caplog.text
    assert "!room:example.org" in caplog.text
    assert "voice message" in caplog.text


@pytest.mark.asyncio
async def test_a_raising_hook_does_not_change_the_outcome(caplog):
    crypto = FakeCrypto(DecryptionError("boom"))

    def hook(*_args):
        raise RuntimeError("hook exploded")

    install_decrypt_failure_logging(crypto, hook)
    with caplog.at_level(logging.ERROR):
        with pytest.raises(DecryptionError):
            await crypto.decrypt_megolm_event(encrypted_event())


@pytest.mark.asyncio
async def test_uninstall_restores_the_original():
    crypto = FakeCrypto()
    original = crypto.decrypt_megolm_event
    uninstall = install_decrypt_failure_logging(crypto)
    assert crypto.decrypt_megolm_event is not original
    uninstall()
    assert crypto.decrypt_megolm_event == original
