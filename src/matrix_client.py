import base64
import hashlib
import logging
from typing import TYPE_CHECKING

from Crypto.Cipher import AES
from mautrix.types import (
    MediaMessageEventContent,
    MessageEvent,
    MessageType,
    RelatesTo,
)

if TYPE_CHECKING:
    from mautrix.client import Client

from src.health import record_message, record_transcription
from src.processed import STATUS_FAILED, ProcessedStore
from src.transcriber import Transcriber

logger = logging.getLogger(__name__)


def is_voice_message(content: MediaMessageEventContent) -> bool:
    if content.msgtype == MessageType.AUDIO:
        return True
    raw = content.serialize()
    if raw.get("msgtype") == "m.text" and "m.voice" in raw:
        return True
    return False


class MatrixTranscribeBot:
    """Transcribe voice messages, exactly once each.

    The daemon-level sync token can be lost (a crypto reset, or mautrix writing
    a blank token when the account is saved before the token is read), and a
    Matrix initial sync replays the recent timeline. *processed* is what keeps a
    replay from producing a second transcription: claim before the download, and
    record the outcome once the reply is on the wire.
    """

    def __init__(
        self,
        client: "Client",
        transcriber: Transcriber,
        *,
        processed: ProcessedStore | None = None,
        health_state_path: "os.PathLike | str | None" = None,
    ):
        self.client = client
        self.transcriber = transcriber
        self.processed = processed
        self.health_state_path = health_state_path

    async def handle_message(self, event: MessageEvent) -> None:
        if event.sender == self.client.mxid:
            return

        # Traffic signal for the healthcheck: messages are flowing, so "no
        # transcription for N hours" becomes meaningful rather than idle.
        if self.health_state_path is not None:
            record_message(self.health_state_path)

        content = event.content
        raw = content.serialize() if hasattr(content, 'serialize') else {}
        msgtype = raw.get("msgtype", "unknown")
        logger.info("Message from %s: msgtype=%s, has_file=%s, has_url=%s",
                     event.sender, msgtype,
                     "file" in raw, "url" in raw)

        if not isinstance(content, MediaMessageEventContent):
            return

        if not is_voice_message(content):
            return

        logger.info("Voice message detected in %s from %s", event.room_id, event.sender)

        event_id = getattr(event, "event_id", "") or ""
        # Durable dedupe: a restart (or a replayed initial-sync window) must not
        # transcribe this event again. The row is written before any work so two
        # concurrent deliveries of the same event cannot both start.
        if self.processed is not None and event_id:
            if not self.processed.claim(event_id, str(event.room_id)):
                logger.info(
                    "Skipping voice message %s in %s: already handled by an earlier run",
                    event_id,
                    event.room_id,
                )
                return

        raw = content.serialize()

        encrypted_file = content.file
        if not encrypted_file:
            file_raw = raw.get("file")
            if file_raw and isinstance(file_raw, dict):
                encrypted_file = file_raw

        mxc_url = content.url
        if not mxc_url and encrypted_file:
            mxc_url = encrypted_file.get("url") if isinstance(encrypted_file, dict) else getattr(encrypted_file, "url", None)
        if not mxc_url:
            logger.warning("No URL in voice message content: %s", raw)
            return

        try:
            audio_data = await self.client.download_media(mxc_url)
            if encrypted_file:
                logger.info("Decrypting encrypted voice message")
                file_obj = encrypted_file if not isinstance(encrypted_file, dict) else type("Obj", (), encrypted_file)()
                key_b64 = file_obj.key.key
                key_bytes = base64.urlsafe_b64decode(key_b64 + "==")
                iv_str = file_obj.iv if isinstance(file_obj.iv, str) else ""
                iv_bytes = base64.urlsafe_b64decode(iv_str + "==")
                cipher = AES.new(key_bytes, AES.MODE_CTR, nonce=iv_bytes[:8], initial_value=iv_bytes[8:])
                audio_data = cipher.decrypt(audio_data)
                logger.info("Decrypted %d bytes", len(audio_data))
        except Exception as e:
            logger.error("Failed to download/decrypt audio: %s", e)
            if self.processed is not None and event_id:
                self.processed.complete(event_id, status=STATUS_FAILED)
            await self._send_reply(event.room_id, f"Failed to download audio: {e}", event.event_id)
            return

        filename = content.body or "audio.ogg"

        try:
            text = await self.transcriber.transcribe(audio_data, filename)
            logger.info("Transcription result: %s", text[:100] if text else "empty")
            await self._send_reply(event.room_id, f"Transcription:\n{text}", event.event_id)
            if self.processed is not None:
                self.processed.complete(event_id)
            if self.health_state_path is not None:
                record_transcription(self.health_state_path)
            logger.info("Reply sent to %s", event.room_id)
        except Exception as e:
            logger.error("Failed to transcribe/send: %s", e, exc_info=True)
            if self.processed is not None:
                self.processed.complete(event_id, status=STATUS_FAILED)
            try:
                await self._send_reply(event.room_id, f"Failed to transcribe: {e}", event.event_id)
            except Exception:
                logger.exception("Failed to send error reply")

    async def _send_reply(self, room_id: str, text: str, reply_to_event_id: str):
        return await self.client.send_text(
            room_id,
            text=text,
            msgtype=MessageType.NOTICE,
            relates_to=RelatesTo(in_reply_to={"event_id": reply_to_event_id}),
        )
