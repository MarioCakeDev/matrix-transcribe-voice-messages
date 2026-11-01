"""The restart replay: why the sync token needs priming before share_keys().

``PgCryptoStore.put_account`` writes the ``sync_token`` column from an in-memory
cache that ``OlmMachine.load()`` never fills, and ``OlmMachine.share_keys`` calls
``put_account`` on every start that uploads keys. Reading the token first (what
``src.main`` now does) keeps the row intact; without it the bot starts from an
initial sync, which replays each room's recent timeline.
"""

import pytest

from mautrix.crypto.account import OlmAccount
from mautrix.crypto.store import PgCryptoStore
from mautrix.util.async_db import Database

ACCOUNT = "@transcribe-bot:example.org"
TOKEN = "s490083_469622_1295_0_6_2_1_2_1"


async def open_store(tmp_path):
    db = Database.create(
        f"sqlite:{tmp_path}/crypto.db", upgrade_table=PgCryptoStore.upgrade_table
    )
    await db.start()
    store = PgCryptoStore(account_id=ACCOUNT, pickle_key="pickle", db=db)
    await store.open()
    return db, store


@pytest.mark.asyncio
async def test_priming_preserves_the_sync_token_across_put_account(tmp_path):
    db, store = await open_store(tmp_path)
    try:
        await store.put_account(OlmAccount())
        await store.put_next_batch(TOKEN)
    finally:
        await db.stop()

    # A restart: fresh store over the same database.
    db, store = await open_store(tmp_path)
    try:
        # This is the line src.main runs before OlmMachine.load(): it fills the
        # cache that put_account() would otherwise persist as an empty token.
        assert await store.get_next_batch() == TOKEN
        account = await store.get_account()
        await store.put_account(account)
        assert await store.get_next_batch() == TOKEN
    finally:
        await db.stop()
