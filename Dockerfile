FROM florianherrengt/matrix-transcribe-voice-messages:latest

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ /app/src/
