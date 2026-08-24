# matrix-transcribe-voice-messages

Fork of [florianherrengt/matrix-transcribe-voice-messages](https://github.com/florianherrengt/matrix-transcribe-voice-messages) with fixes and new features for self-hosted Matrix deployments using MAS (Matrix Authentication Service) and E2EE rooms.

## What's Changed

- **MAS login support** — Logs in via Matrix Authentication Service, then connects to Synapse. Required for homeservers using MAS for auth.
- **End-to-End Encryption** — Decrypts encrypted voice messages (AES-256-CTR) before sending to Whisper.
- **Auto-join on invite** — Bot automatically joins rooms when invited.
- **Device ID persistence** — Saves `device_id` to maintain crypto state across restarts.
- **Notice replies** — Sends transcriptions as `m.notice` to avoid bridges re-bridging bot messages.
- **Comprehensive logging** — Detailed logs for debugging message detection, download, decryption, and transcription.
- **Fixed dependencies** — Adds `unpaddedbase64`, `pycryptodome`, `base58`, `aiosqlite`, `python-olm`.

## Prerequisites

### Whisper Server

Run a Whisper server (e.g., [hwdsl2/whisper-server](https://github.com/hwdsl2/docker-whisper)):

```yaml
whisper:
  image: hwdsl2/whisper-server:latest
  environment:
    WHISPER_MODEL: medium
    WHISPER_LANGUAGE: ''  # Empty = auto-detect language per segment
    WHISPER_API_KEY: ''
  ports:
    - '9000:9000'
```

- `WHISPER_LANGUAGE: ''` — Auto-detects language (supports mixed German/Russian/English).
- `WHISPER_MODEL: medium` — Better multilingual accuracy than `small`.
- `WHISPER_API_KEY: ''` — Disables API key auth.

### Matrix Bot Account

1. Create a bot account on your homeserver (e.g., `@transcribe-bot:your.domain`).
2. If using MAS, note the MAS URL and Synapse URL separately.

## Docker Compose

```yaml
services:
  transcribe-bot:
    image: ghcr.io/mariocakedev/matrix-transcribe-voice-messages:latest
    environment:
      MATRIX_HOMESERVER: 'https://matrix.your.domain'     # Synapse URL
      MATRIX_MAS_URL: 'https://mas.your.domain'           # MAS URL (if using MAS)
      MATRIX_USER_ID: '@transcribe-bot:your.domain'
      MATRIX_PASSWORD: 'your-bot-password'
      PARAKEET_URL: 'http://whisper:9000'                 # Whisper server URL
    volumes:
      - transcribe-store:/app/store
    depends_on:
      - whisper

  whisper:
    image: hwdsl2/whisper-server:latest
    environment:
      WHISPER_MODEL: medium
      WHISPER_LANGUAGE: ''
      WHISPER_API_KEY: ''
    volumes:
      - whisper-data:/home/whisper

volumes:
  transcribe-store:
  whisper-data:
```

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `MATRIX_HOMESERVER` | Yes | Synapse server URL (e.g., `https://matrix.your.domain`) |
| `MATRIX_USER_ID` | Yes | Bot's Matrix user ID (e.g., `@transcribe-bot:your.domain`) |
| `MATRIX_PASSWORD` | Yes | Bot's password |
| `PARAKEET_URL` | Yes | Whisper server URL (e.g., `http://whisper:9000`) |
| `MATRIX_MAS_URL` | No | MAS URL if using Matrix Authentication Service |
| `MATRIX_DEVICE_ID` | No | Persist device ID across restarts (auto-generated if not set) |
| `STORE_PATH` | No | Path for crypto store (default: `./store`) |

## How It Works

1. Bot logs in via MAS (or directly to Synapse if no MAS).
2. Syncs with the homeserver, listening for `m.audio` messages.
3. When a voice message is detected:
   - Downloads the encrypted file from the Matrix content repository.
   - Decrypts using AES-256-CTR (if E2EE).
   - Sends audio to Whisper for transcription.
   - Replies with the transcription as `m.notice` (avoids bridge re-bridging).

## Supported Audio Formats

Whisper supports: mp3, mp4, mpeg, mpga, m4a, wav, webm, ogg, flac.

## Language Support

Whisper auto-detects the language per audio segment. Supports mixed-language voice messages (e.g., German + Russian + English in the same message).

## Usage

1. Invite `@transcribe-bot:your.domain` to a room.
2. Send a voice message.
3. Bot replies with the transcription.

## Building

```bash
docker build -t matrix-transcribe-voice-messages .
```

## Credits

Based on [florianherrengt/matrix-transcribe-voice-messages](https://github.com/florianherrengt/matrix-transcribe-voice-messages).
