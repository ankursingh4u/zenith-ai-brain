# Zenith AI Brain — Telegram assistant bot
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

WORKDIR /app

# Install deps first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    # ddgs declares httpx>=0.28.1, which is impossible here: telegram pins
    # httpx~=0.27 and openai 1.54 dies on 0.28 ("unexpected keyword argument
    # 'proxies'"). Listing it in requirements.txt makes pip fail the whole
    # build with ResolutionImpossible, so install it WITHOUT its declared deps
    # — they're pinned in requirements.txt instead. It uses primp to make
    # requests and works fine on 0.27.2.
    && pip install --no-cache-dir --no-deps ddgs==9.14.4 \
    # Fail the BUILD, not every reply at runtime, if that ever stops holding.
    && python -c "import httpx, openai, trafilatura, rapidfuzz; \
from openai import OpenAI; OpenAI(api_key='build-check'); \
from ddgs import DDGS; DDGS(); \
assert httpx.__version__.startswith('0.27'), 'httpx moved: ' + httpx.__version__; \
print('dependency check OK — httpx', httpx.__version__)"
# NOTE: DDGS() is constructed on purpose. `from ddgs import DDGS` is a lazy
# stub that imports nothing; only constructing it loads the search engines and
# proves their transitive imports (h2 among them) are actually present.

# App code
COPY . .

# Folder for the SQLite database — mount a persistent volume here in Coolify
# and set DATABASE_URL=sqlite:////data/brain.db
RUN mkdir -p /data

# OAuth login callback server
EXPOSE 8000

CMD ["python", "main.py"]
