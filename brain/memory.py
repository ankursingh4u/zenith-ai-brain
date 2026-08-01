"""Semantic memory — recall by MEANING, not by shared keywords.

`db.search_turns` is a SQL LIKE scan, so "what did I decide about caching" finds
nothing if the message said "Redis cache-aside on 3 endpoints". Here each piece
of history is embedded once, and recall compares vectors instead of substrings.

Everything degrades safely: if the configured endpoint doesn't serve embeddings
(gateways often don't), `available()` flips to False after the first failure and
every caller silently falls back to keyword search. A memory feature must never
be able to break a reply.
"""
from __future__ import annotations

import json
import logging
import math

import config
import db
from brain import llm

log = logging.getLogger(__name__)

# None = not tried yet, True/False = settled after the first real call.
_available: bool | None = None
# Nothing shorter than this carries a recallable idea ("ok", "thanks", "haan").
_MIN_CHARS = 25


_client = None


def client():
    """The embeddings client — its own provider if configured, else chat's.

    Chat and embeddings are separate capabilities: a gateway can serve one and
    refuse the other (llms.codershive.in answers chat fine but returns 400
    "No credentials for embedding"), so these may point at different places.
    """
    global _client
    if _client is None:
        if config.EMBED_API_KEY or config.EMBED_BASE_URL:
            from openai import OpenAI
            kwargs = {"api_key": config.EMBED_API_KEY or config.LLM_API_KEY}
            if config.EMBED_BASE_URL:
                kwargs["base_url"] = config.EMBED_BASE_URL
            _client = OpenAI(**kwargs)
            log.info("Embeddings use their own endpoint: %s",
                     config.EMBED_BASE_URL or "api.openai.com")
        else:
            _client = llm.client()
    return _client


def available() -> bool:
    return bool(config.EMBED_ENABLED) and _available is not False


def _embed(text: str) -> list[float] | None:
    """One embedding, or None if embeddings aren't usable on this endpoint."""
    global _available
    if not available():
        return None
    try:
        resp = client().embeddings.create(
            model=config.EMBED_MODEL, input=text[:8000])
        vec = list(resp.data[0].embedding)
        if _available is not True:
            _available = True
            log.info("Semantic memory ON (model=%s, dim=%d)",
                     config.EMBED_MODEL, len(vec))
        return vec
    except Exception as e:  # noqa: BLE001 — never let memory break a reply
        if _available is None:
            log.warning("Semantic memory OFF, falling back to keyword recall: %s", e)
        _available = False
        return None


def selftest() -> bool:
    """Settle at startup whether embeddings work here, and say so in the log.

    Otherwise the answer only appears the first time a user happens to send a
    message, which means a misconfigured key looks identical to an idle bot.
    One tiny embedding at boot costs a fraction of a cent and removes the doubt.
    """
    if not config.EMBED_ENABLED:
        log.info("Semantic memory disabled by config (EMBED_ENABLED=0).")
        return False
    vec = _embed("startup check")
    if vec is None:
        log.warning("Semantic memory OFF — recall will use keyword search.")
        return False
    log.info("Semantic memory ON (model=%s, dim=%d, endpoint=%s).",
             config.EMBED_MODEL, len(vec),
             config.EMBED_BASE_URL or ("api.openai.com" if config.EMBED_API_KEY
                                       else config.LLM_BASE_URL or "api.openai.com"))
    return True


def remember(telegram_id: int, text: str, kind: str = "turn") -> bool:
    """Embed and store one line of history. Safe to call on every message."""
    text = (text or "").strip()
    if len(text) < _MIN_CHARS or not available():
        return False
    try:
        if db.memory_exists(telegram_id, text):
            return False
        vec = _embed(text)
        if vec is None:
            return False
        db.add_memory(telegram_id, kind, text, json.dumps(vec))
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("remember failed (ignored): %s", e)
        return False


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def search(telegram_id: int, query: str, limit: int = 8,
           min_score: float = 0.25) -> list[dict]:
    """Closest pieces of THIS user's history by meaning. [] if unavailable."""
    if not available():
        return []
    qvec = _embed(query)
    if qvec is None:
        return []
    try:
        rows = db.all_memories(telegram_id, config.MEMORY_SCAN_LIMIT)
    except Exception as e:  # noqa: BLE001
        log.warning("memory scan failed (ignored): %s", e)
        return []
    scored = []
    for r in rows:
        try:
            vec = json.loads(r["vector"])
        except (TypeError, ValueError):
            continue
        score = _cosine(qvec, vec)
        if score >= min_score:
            scored.append({"score": score, "text": r["text"],
                           "when": r["when"], "kind": r["kind"]})
    scored.sort(key=lambda x: -x["score"])
    return scored[:limit]


def backfill(telegram_id: int, limit: int = 300) -> tuple[int, int]:
    """Embed history that predates this feature. (embedded, skipped)"""
    if not available():
        return (0, 0)
    done = skipped = 0
    for row in db.recent_turns(telegram_id, limit=limit):
        text = (row.get("content") or "").strip()
        if len(text) < _MIN_CHARS:
            skipped += 1
            continue
        if remember(telegram_id, text):
            done += 1
        else:
            skipped += 1
        if not available():          # endpoint gave up mid-run — stop cleanly
            break
    return done, skipped
