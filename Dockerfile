# Zenith AI Brain — Telegram assistant bot
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

WORKDIR /app

# Install deps first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ddgs declares httpx>=0.28.1, which is impossible here: python-telegram-bot
# pins httpx~=0.27 and openai 1.54 dies on 0.28. Listing ddgs in
# requirements.txt makes pip fail the whole build with ResolutionImpossible,
# so install it without its declared deps - they are pinned in
# requirements.txt instead.
RUN pip install --no-cache-dir --no-deps ddgs==9.14.4

# Fail the BUILD rather than every reply at runtime if that stops holding.
COPY scripts/dep_check.py scripts/dep_check.py
RUN python scripts/dep_check.py

# App code
COPY . .

# Folder for the SQLite database — mount a persistent volume here in Coolify
# and set DATABASE_URL=sqlite:////data/brain.db
RUN mkdir -p /data

# OAuth login callback server
EXPOSE 8000

CMD ["python", "main.py"]
