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
    def __init__(self, client: "Client", transcriber: Transcriber):
        self.client = client
        self.transcriber = transcriber

    async def handle_message(self, event: MessageEvent) -> None:
        if event.sender == self.client.mxid:
            return

        content = event.content
        if not isinstance(content, MediaMessageEventContent):
            return

        if not is_voice_message(content):
            return

        logger.info("Voice message detected in %s from %s", event.room_id, event.sender)

        raw = content.serialize()
        logger.info("Voice message content keys: %s", list(raw.keys()))

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
                key_data = file_obj.key if isinstance(file_obj.key, dict) else file_obj.key
                key_b64 = key_data.get("k", "") if isinstance(key_data, dict) else ""
                logger.info("Key type=%s, k=%s", type(key_data), key_b64[:20] if key_b64 else "EMPTY")
                key_bytes = base64.urlsafe_b64decode(key_b64 + "==")
                iv_str = file_obj.iv if isinstance(file_obj.iv, str) else ""
                logger.info("IV=%s len=%d", iv_str[:20], len(iv_str))
                iv_bytes = base64.urlsafe_b64decode(iv_str + "==")
                logger.info("Key bytes=%d, IV bytes=%d", len(key_bytes), len(iv_bytes))
                cipher = AES.new(key_bytes, AES.MODE_CTR, nonce=iv_bytes[:8], initial_value=iv_bytes[8:])
                audio_data = cipher.decrypt(audio_data)
                logger.info("Decrypted %d bytes", len(audio_data))
        except Exception as e:
            logger.error("Failed to download/decrypt audio: %s", e)
            await self._send_reply(event.room_id, f"Failed to download audio: {e}", event.event_id)
            return

        filename = content.body or "audio.ogg"

        try:
            text = await self.transcriber.transcribe(audio_data, filename)
            await self._send_reply(event.room_id, f"Transcription:\n{text}", event.event_id)
        except Exception as e:
            logger.error("Failed to transcribe audio: %s", e)
            try:
                await self._send_reply(event.room_id, f"Failed to transcribe audio: {e}", event.event_id)
            except Exception:
                logger.exception("Failed to send error reply")

    async def _send_reply(self, room_id: str, text: str, reply_to_event_id: str) -> None:
        await self.client.send_text(
            room_id,
            text=text,
            relates_to=RelatesTo(in_reply_to={"event_id": reply_to_event_id}),
        )
