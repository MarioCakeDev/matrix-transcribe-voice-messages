# matrix-transcribe-voice-messages-fix

Fix for [florianherrengt/matrix-transcribe-voice-messages](https://github.com/florianherrengt/matrix-transcribe-voice-messages) — adds the missing `unpaddedbase64` dependency that causes `ModuleNotFoundError` on startup.

## Usage

Replace the image in your docker-compose:

```yaml
transcribe-bot:
  image: MarioCakeDev/matrix-transcribe-voice-messages:latest
  # ... rest of config
```
