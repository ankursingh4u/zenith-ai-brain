"""Web access — search the open web and read a page.

This lifts the bot's hard ceiling: before this it could not check a price, read
a doc, or verify a version, and had to say so. Free and key-less by design:
DuckDuckGo for search, trafilatura for turning HTML into readable text.

SECURITY — this is the one tool where a user's words become an outbound request
from YOUR server, so the fetcher refuses anything that isn't a public http(s)
address. Cloud metadata endpoints (169.254.169.254), localhost and private LAN
ranges are blocked: on a Coolify box those would hand out real credentials.

Everything degrades: if a package is missing or a request fails, the tool
returns a plain explanation and the bot carries on.
"""
from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

try:
    from ddgs import DDGS
except ImportError:  # pragma: no cover - optional dependency
    DDGS = None

try:
    import trafilatura
except ImportError:  # pragma: no cover - optional dependency
    trafilatura = None

# A page bigger than this is a download, not something to read.
MAX_BYTES = 3_000_000
TIMEOUT = 15.0
UA = ("Mozilla/5.0 (compatible; ZenithBrain/1.0; personal assistant bot)")


def _is_public(host: str) -> tuple[bool, str]:
    """Resolve a hostname and refuse anything not on the public internet."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False, f"couldn't resolve '{host}'"
    for info in infos:
        ip = info[4][0]
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False, f"odd address for '{host}'"
        # link-local covers 169.254.169.254 — the cloud metadata endpoint.
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast):
            return False, (f"'{host}' points at a private/internal address "
                           f"({ip}) — refusing, that's this server's own network")
    return True, ""


def check_url(url: str) -> tuple[bool, str]:
    """Is this safe to fetch? (ok, reason-if-not)"""
    try:
        p = urlparse(url.strip())
    except ValueError:
        return False, "that isn't a readable URL"
    if p.scheme not in ("http", "https"):
        return False, "only http and https links can be read"
    if not p.hostname:
        return False, "that URL has no host in it"
    return _is_public(p.hostname)


def search(query: str, count: int = 5) -> list[dict]:
    """Free web search. Returns [{title, url, snippet}]. Raises on failure."""
    if DDGS is None:
        raise RuntimeError("the 'ddgs' package isn't installed on this server")
    out = []
    with DDGS() as ddgs:
        for r in ddgs.text(query, max_results=max(1, min(int(count or 5), 10))):
            out.append({
                "title": (r.get("title") or "").strip(),
                "url": (r.get("href") or r.get("url") or "").strip(),
                "snippet": (r.get("body") or "").strip(),
            })
    return out


def read(url: str, max_chars: int = 6000) -> str:
    """Fetch a page and return its readable text. Raises on failure."""
    ok, why = check_url(url)
    if not ok:
        raise ValueError(why)
    with httpx.Client(follow_redirects=True, timeout=TIMEOUT,
                      headers={"User-Agent": UA}) as client:
        with client.stream("GET", url) as resp:
            resp.raise_for_status()
            # Re-check after redirects: a public URL can bounce to an internal one.
            final = str(resp.url)
            ok, why = check_url(final)
            if not ok:
                raise ValueError(f"that link redirected somewhere unsafe — {why}")
            ctype = resp.headers.get("content-type", "")
            if ctype and not any(t in ctype for t in
                                 ("html", "text", "xml", "json")):
                raise ValueError(f"that's a {ctype.split(';')[0]} file, not a readable page")
            chunks, total = [], 0
            for chunk in resp.iter_bytes():
                total += len(chunk)
                if total > MAX_BYTES:
                    break
                chunks.append(chunk)
    html = b"".join(chunks).decode("utf-8", errors="replace")
    text = None
    if trafilatura is not None:
        text = trafilatura.extract(html, include_comments=False,
                                   include_tables=True, no_fallback=False)
    if not text:
        raise ValueError("couldn't pull any readable text out of that page")
    text = text.strip()
    limit = max(500, min(int(max_chars or 6000), 20000))
    if len(text) > limit:
        text = text[:limit] + "\n\n[…truncated]"
    return text
