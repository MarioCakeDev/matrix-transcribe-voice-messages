import logging
from typing import TYPE_CHECKING

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

        mxc_url = content.url
        if not mxc_url and hasattr(content, 'file') and content.file:
            mxc_url = content.file.url
        if not mxc_url:
            file_info = raw.get("file")
            if file_info and isinstance(file_info, dict):
                mxc_url = file_info.get("url")
        if not mxc_url:
            logger.warning("No URL in voice message content: %s", raw)
            return

        try:
            audio_data = await self.client.download_media(mxc_url)
        except Exception as e:
            logger.error("Failed to download audio: %s", e)
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
