import logging

import aiohttp

logger = logging.getLogger(__name__)


class Transcriber:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    async def transcribe(self, audio_data: bytes, filename: str) -> str:
        url = f"{self.base_url}/v1/audio/transcriptions"
        data = aiohttp.FormData()
        data.add_field("file", audio_data, filename=filename, content_type="application/octet-stream")
        data.add_field("response_format", "json")

        logger.info("Sending %d bytes to whisper at %s", len(audio_data), url)
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=data) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise Exception(f"Transcription failed (HTTP {resp.status}): {text}")
                result = await resp.json()
                logger.info("Whisper response: %s", result)
                return result["text"]
