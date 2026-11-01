import types

import pytest

from mautrix.types import MediaMessageEventContent, MessageType

from src import health
from src.matrix_client import MatrixTranscribeBot
from src.processed import ProcessedStore, database_path


class FakeClient:
    mxid = "@transcribe-bot:example.org"

    def __init__(self):
        self.sent = []
        self.downloads = 0

    async def download_media(self, url):
        self.downloads += 1
        return b"fake-ogg-bytes"

    async def send_text(self, room_id, text, msgtype, relates_to):
        self.sent.append({"room_id": room_id, "text": text, "relates_to": relates_to})
        return f"$reply{len(self.sent)}"


class FakeTranscriber:
    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail

    async def transcribe(self, audio_data, filename):
        self.calls += 1
        if self.fail:
            raise RuntimeError("whisper is down")
        return "hello from the recording"


def voice_event(event_id="$evt1", room_id="!room:example.org", sender="@mario:example.org"):
    content = MediaMessageEventContent(
        msgtype=MessageType.AUDIO,
        body="voice-message.ogg",
        url="mxc://example.org/audio",
    )
    return types.SimpleNamespace(
        event_id=event_id, room_id=room_id, sender=sender, content=content
    )


def text_event(event_id="$txt1"):
    content = MediaMessageEventContent(
        msgtype=MessageType.TEXT, body="just talking", url="mxc://example.org/none"
    )
    return types.SimpleNamespace(
        event_id=event_id,
        room_id="!room:example.org",
        sender="@mario:example.org",
        content=content,
    )


def make_bot(tmp_path, *, fail=False):
    client = FakeClient()
    transcriber = FakeTranscriber(fail=fail)
    store = ProcessedStore(database_path(tmp_path)).open()
    bot = MatrixTranscribeBot(
        client,
        transcriber,
        processed=store,
        health_state_path=health.state_path(tmp_path),
    )
    return bot, client, transcriber, store


@pytest.mark.asyncio
async def test_transcribes_once_and_records_the_event(tmp_path):
    bot, client, transcriber, store = make_bot(tmp_path)
    try:
        await bot.handle_message(voice_event())

        assert transcriber.calls == 1
        assert client.sent[0]["text"].startswith("Transcription:")
        assert store.status_of("$evt1") == "replied"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_the_same_event_delivered_twice_is_transcribed_once(tmp_path):
    bot, client, transcriber, store = make_bot(tmp_path)
    try:
        await bot.handle_message(voice_event())
        await bot.handle_message(voice_event())

        assert transcriber.calls == 1
        assert len(client.sent) == 1
        assert client.downloads == 1
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_restart_does_not_re_transcribe(tmp_path):
    bot, _client, transcriber, store = make_bot(tmp_path)
    await bot.handle_message(voice_event())
    store.close()

    # A restart builds a new bot over the same store — the replay is a no-op.
    restarted, client, new_transcriber, store = make_bot(tmp_path)
    try:
        await restarted.handle_message(voice_event())
        assert new_transcriber.calls == 0
        assert client.sent == []
    finally:
        store.close()


@pytest.mark.asyncio
async def test_distinct_events_are_both_transcribed(tmp_path):
    bot, _client, transcriber, store = make_bot(tmp_path)
    try:
        await bot.handle_message(voice_event("$a"))
        await bot.handle_message(voice_event("$b"))
        assert transcriber.calls == 2
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_failed_transcription_is_not_retried_forever(tmp_path):
    bot, client, transcriber, store = make_bot(tmp_path, fail=True)
    try:
        await bot.handle_message(voice_event())

        assert transcriber.calls == 1
        assert "Failed to transcribe" in client.sent[0]["text"]
        assert store.status_of("$evt1") == "failed"

        await bot.handle_message(voice_event())
        assert transcriber.calls == 1
    finally:
        store.close()


@pytest.mark.asyncio
async def test_text_messages_do_no_work_and_leave_no_record(tmp_path):
    bot, client, transcriber, store = make_bot(tmp_path)
    try:
        await bot.handle_message(text_event())
        assert transcriber.calls == 0
        assert client.sent == []
        assert store.count() == 0
    finally:
        store.close()


@pytest.mark.asyncio
async def test_own_messages_are_ignored(tmp_path):
    bot, client, transcriber, store = make_bot(tmp_path)
    try:
        await bot.handle_message(voice_event(sender=FakeClient.mxid))
        assert transcriber.calls == 0
        assert store.count() == 0
    finally:
        store.close()


@pytest.mark.asyncio
async def test_health_state_tracks_traffic_and_transcription(tmp_path):
    bot, _client, _transcriber, store = make_bot(tmp_path)
    try:
        await bot.handle_message(voice_event())
        state = health.read_state(health.state_path(tmp_path))
        assert state["last_message_at"] > 0
        assert state["last_transcription_at"] > 0
    finally:
        store.close()


@pytest.mark.asyncio
async def test_reply_is_a_notice_in_reply_to_the_event(tmp_path):
    bot, client, _transcriber, store = make_bot(tmp_path)
    try:
        await bot.handle_message(voice_event())
        relates = client.sent[0]["relates_to"]
        assert relates.in_reply_to["event_id"] == "$evt1"
    finally:
        store.close()
