import asyncio
import logging
import os
import signal
from pathlib import Path
from typing import NoReturn

from dotenv import load_dotenv
from aiohttp import ClientSession
from mautrix.client import Client, InternalEventType
from mautrix.crypto import OlmMachine
from mautrix.crypto.store import PgCryptoStore, PgCryptoStateStore
from mautrix.types import EventType, LoginType, MatrixUserIdentifier, Membership
from mautrix.util.async_db import Database

from src import health
from src.config import Config
from src.mas_login import post_login_with_retry
from src.matrix_client import MatrixTranscribeBot
from src.retry import retry_async
from src.sync_supervisor import run_sync_forever
from src.transcriber import Transcriber

logger = logging.getLogger(__name__)

DEVICE_ID_FILE = "device_id"

# Grace given to the sync loop to return after `client.stop()` before it is
# cancelled. Covers the case where the signal lands while the supervisor is in
# its reconnect backoff (up to `max_delay`), which `client.stop()` cannot
# interrupt. Kept below Docker's default 10s stop grace so a clean `db.stop()`
# still runs before SIGKILL.
SHUTDOWN_GRACE_SECONDS = 5.0


def _fail_fast(reason: str) -> NoReturn:
    logger.critical("Fatal: %s", reason)
    logging.shutdown()
    os._exit(1)


async def main():
    load_dotenv()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    config = Config.from_env()
    transcriber = Transcriber(config.parakeet_url)

    # Liveness heartbeat: touched on startup, login retries and every successful
    # sync. The Docker HEALTHCHECK (`python -m src.health --check`) goes stale
    # when the sync loop stops making progress.
    heartbeat_file = health.heartbeat_path(config.store_path)
    health.write_beat(heartbeat_file)

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

    mas_url = config.mas_url or config.homeserver.replace("matrix.", "mas.")

    # Load persisted device_id if available
    device_id_file = Path(config.store_path) / DEVICE_ID_FILE
    device_id = config.device_id
    if not device_id and device_id_file.exists():
        device_id = device_id_file.read_text().strip()
        logger.info("Loaded persisted device_id: %s", device_id)

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
        if device_id:
            login_payload["device_id"] = device_id

        try:
            login_data = await post_login_with_retry(
                session,
                mas_url,
                login_payload,
                max_attempts=config.mas_login_max_attempts,
                base_delay=config.mas_login_base_delay,
                max_delay=config.mas_login_max_delay,
                timeout=config.mas_login_timeout,
                on_retry=lambda *_: health.write_beat(heartbeat_file),
            )
            access_token = login_data["access_token"]
        except Exception as exc:
            _fail_fast(f"MAS login failed after retries: {exc!r}")

    device_id = login_data.get("device_id")

    # Persist device_id for next restart
    os.makedirs(config.store_path, exist_ok=True)
    device_id_file.write_text(device_id or "")
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

    # Synapse may still be booting when we get here; retry key sharing with the
    # same backoff budget as the login, then fail loud so the restart policy
    # reaps a container that cannot connect.
    try:
        await retry_async(
            crypto.share_keys,
            max_attempts=config.mas_login_max_attempts,
            base_delay=config.mas_login_base_delay,
            max_delay=config.mas_login_max_delay,
            description="Synapse key sharing",
            on_retry=lambda *_: health.write_beat(heartbeat_file),
        )
    except Exception as exc:
        _fail_fast(f"Synapse key sharing failed after retries: {exc!r}")

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

    @client.on(EventType.ROOM_MEMBER)
    async def on_member(evt):
        if evt.content.membership == Membership.INVITE and evt.state_key == config.user_id:
            logger.info("Invited to room %s, joining...", evt.room_id)
            try:
                await client.join_room(evt.room_id)
                logger.info("Joined room %s", evt.room_id)
            except Exception:
                logger.exception("Failed to join room %s", evt.room_id)

    @client.on(InternalEventType.SYNC_STARTED)
    async def on_sync_started(_data):
        health.write_beat(heartbeat_file)

    @client.on(InternalEventType.SYNC_SUCCESSFUL)
    async def on_sync_successful(_data):
        health.write_beat(heartbeat_file)

    stop_event = asyncio.Event()
    stopping = False

    def handle_signal():
        nonlocal stopping
        stopping = True
        stop_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal)

    logger.info("Bot started. Listening for voice messages...")

    sync_task = asyncio.ensure_future(run_sync_forever(client, lambda: stopping))

    await stop_event.wait()
    client.stop()

    logger.info("Shutting down...")
    done, _ = await asyncio.wait({sync_task}, timeout=SHUTDOWN_GRACE_SECONDS)
    if not done:
        logger.warning(
            "Sync loop did not stop within %.0fs; cancelling", SHUTDOWN_GRACE_SECONDS
        )
        sync_task.cancel()
    try:
        await sync_task
    except asyncio.CancelledError:
        pass
    await db.stop()


if __name__ == "__main__":
    asyncio.run(main())
