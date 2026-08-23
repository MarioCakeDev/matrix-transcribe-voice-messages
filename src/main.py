import asyncio
import logging
import signal

from dotenv import load_dotenv
from aiohttp import ClientSession
from mautrix.client import Client, InternalEventType
from mautrix.crypto import OlmMachine
from mautrix.crypto.store import PgCryptoStore, PgCryptoStateStore
from mautrix.types import EventType, LoginType, MatrixUserIdentifier
from mautrix.util.async_db import Database

from src.config import Config
from src.matrix_client import MatrixTranscribeBot
from src.transcriber import Transcriber

logger = logging.getLogger(__name__)


async def main():
    load_dotenv()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    config = Config.from_env()
    transcriber = Transcriber(config.parakeet_url)

    db = Database.create(
        f"sqlite:{config.store_path}/crypto.db",
        upgrade_table=PgCryptoStore.upgrade_table,
    )
    await db.start()
    await PgCryptoStateStore.upgrade_table.upgrade(db)

    crypto_store = PgCryptoStore(
        account_id=config.user_id,
        pickle_key=f"{config.user_id}:{config.device_id or 'default'}",
        db=db,
    )
    await crypto_store.open()

    state_store = PgCryptoStateStore(db)

    mas_url = getattr(config, "mas_url", None) or config.homeserver.replace("matrix.", "mas.")

    # Login via MAS using raw aiohttp (Synapse delegates auth to MAS)
    async with ClientSession() as session:
        login_payload = {
            "type": "m.login.password",
            "identifier": {
                "type": "m.id.user",
                "user": config.user_id.split(":")[0][1:],
            },
            "password": config.password,
        }
        if config.device_id:
            login_payload["device_id"] = config.device_id

        async with session.post(
            f"{mas_url}/_matrix/client/v3/login", json=login_payload
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"MAS login failed ({resp.status}): {body}")
            login_data = await resp.json()

    access_token = login_data["access_token"]
    device_id = login_data.get("device_id")
    logger.info("Logged in via %s (device_id=%s)", mas_url, device_id)

    # Create client pointing at Synapse
    client = Client(
        base_url=config.homeserver,
        mxid=config.user_id,
        token=access_token,
        device_id=device_id,
        sync_store=crypto_store,
        state_store=state_store,
    )

    crypto = OlmMachine(client, crypto_store, state_store)
    await crypto.load()
    client.crypto = crypto

    await crypto.share_keys()

    if config.recovery_key:
        try:
            await crypto.verify_with_recovery_key(config.recovery_key)
            logger.info("Cross-signing verified via recovery key")
        except Exception:
            logger.exception("Cross-signing verification failed")

    bot = MatrixTranscribeBot(client, transcriber)

    @client.on(EventType.ROOM_MESSAGE)
    async def on_message(evt):
        await bot.handle_message(evt)

    stop_event = asyncio.Event()

    def handle_signal():
        stop_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal)

    logger.info("Bot started. Listening for voice messages...")

    sync_task = asyncio.ensure_future(client.start(None))

    await stop_event.wait()
    client.stop()

    logger.info("Shutting down...")
    await sync_task
    await db.stop()


if __name__ == "__main__":
    asyncio.run(main())
