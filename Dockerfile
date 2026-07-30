# Zenith AI Brain — Telegram assistant bot
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

WORKDIR /app

# Install deps first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    # ddgs DECLARES httpx>=0.28 but routes its requests through `primp`, so it
    # runs fine on 0.27.2. pip resolves that declaration by upgrading httpx,
    # and httpx 0.28 breaks openai 1.54 ("unexpected keyword argument
    # 'proxies'") — which breaks EVERY reply. So pin it back, last word wins.
    && pip install --no-cache-dir httpx==0.27.2

# App code
COPY . .

# Folder for the SQLite database — mount a persistent volume here in Coolify
# and set DATABASE_URL=sqlite:////data/brain.db
RUN mkdir -p /data

# OAuth login callback server
EXPOSE 8000

CMD ["python", "main.py"]
