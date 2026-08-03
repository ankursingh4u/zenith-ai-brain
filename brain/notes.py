"""The Obsidian layer: notes as real markdown, with wikilinks and backlinks.

Everything the bot writes here is a plain .md file with YAML frontmatter and
[[wikilinks]] — the format Obsidian already understands, so the graph, search
and backlink pane work without a plugin. integrations/vault.py decides WHERE
those files land; this module decides what they look like and keeps a local
index so recall doesn't depend on the network.

Two copies, on purpose:
  · the vault  — what Obsidian opens, the user's own files, human-editable
  · the index  — the same text in SQLite, embedded for meaning-based search

They're reconciled by sync(): files edited in Obsidian come back in, notes the
bot couldn't push (GitHub down, token expired) go out. Nothing here deletes a
file from the vault.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime

import config
import db
from brain import memory
from integrations import vault as vaultmod

log = logging.getLogger(__name__)

_WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
# A #tag, but not a markdown heading and not the # inside a URL fragment.
_TAG = re.compile(r"(?:(?<=\s)|^)#([A-Za-z][\w/-]{1,40})")
_FRONTMATTER = re.compile(r"^---\r?\n(.*?)\r?\n---\r?\n?", re.S)


# --- Markdown format ------------------------------------------------------
def parse(text: str) -> tuple[dict, str]:
    """Split a note file into (frontmatter dict, body).

    A deliberately small parser instead of a YAML dependency: notes we write
    only ever have flat string/list values, and a note a human hand-edited
    should still open even if their frontmatter is something we don't model.
    """
    meta: dict = {}
    m = _FRONTMATTER.match(text or "")
    if not m:
        return meta, (text or "").strip()
    for line in m.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, _, val = line.partition(":")
        key, val = key.strip().lower(), val.strip()
        if val.startswith("[") and val.endswith("]"):
            meta[key] = [v.strip().strip("'\"") for v in val[1:-1].split(",") if v.strip()]
        else:
            meta[key] = val.strip("'\"")
    return meta, text[m.end():].strip()


def render(title: str, body: str, tags: list[str] | None = None,
           created: str | None = None, updated: str | None = None,
           extra: dict | None = None) -> str:
    """One note file: frontmatter + body, exactly as Obsidian expects it."""
    tags = [t.strip().lstrip("#") for t in (tags or []) if t and t.strip()]
    lines = ["---", f"title: {title}"]
    lines.append(f"created: {created or date.today().isoformat()}")
    lines.append(f"updated: {updated or date.today().isoformat()}")
    if tags:
        lines.append("tags: [" + ", ".join(dict.fromkeys(tags)) + "]")
    for k, v in (extra or {}).items():
        lines.append(f"{k}: {v}")
    lines += ["---", "", body.strip(), ""]
    return "\n".join(lines)


def extract_links(body: str) -> list[str]:
    """[[Other note]] targets, aliases and heading anchors stripped off."""
    return list(dict.fromkeys(m.strip() for m in _WIKILINK.findall(body or "") if m.strip()))


def extract_tags(body: str, meta: dict | None = None) -> list[str]:
    tags = []
    front = (meta or {}).get("tags")
    if isinstance(front, list):
        tags += [str(t).lstrip("#") for t in front]
    elif isinstance(front, str):
        tags += [t.strip().lstrip("#") for t in front.split(",") if t.strip()]
    tags += _TAG.findall(body or "")
    return list(dict.fromkeys(t for t in (s.strip() for s in tags) if t))


def _filename(title: str) -> str:
    """Obsidian names a note by its filename, so the title IS the filename."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", (title or "").strip())
    name = re.sub(r"\s+", " ", name).strip(" .-") or "Untitled"
    return name[:120]


def path_for(title: str, folder: str | None = None) -> str:
    """Vault-relative path for a note title, e.g. 'DSA/Sliding Window.md'."""
    title = (title or "").strip()
    if title.lower().endswith(".md") and "/" in title:
        return vaultmod.clean_path(title)          # already a path
    folder = (folder or config.VAULT_DEFAULT_FOLDER or "").strip("/")
    name = _filename(title) + ".md"
    return vaultmod.clean_path(f"{folder}/{name}" if folder else name)


def daily_path(when: date | None = None) -> str:
    d = when or date.today()
    folder = (config.VAULT_DAILY_FOLDER or "Daily").strip("/")
    return vaultmod.clean_path(f"{folder}/{d.isoformat()}.md")


def inbox_path() -> str:
    folder = (config.VAULT_INBOX_FOLDER or "Inbox").strip("/")
    return vaultmod.clean_path(f"{folder}/Research Inbox.md")


def title_of(path: str) -> str:
    return re.sub(r"\.md$", "", (path or "").split("/")[-1], flags=re.I)


# --- Reading --------------------------------------------------------------
def _index(telegram_id: int, path: str, title: str, body: str,
           remote_sha: str | None = None, embed: bool = True,
           tags: list[str] | None = None):
    """Store/refresh one note in the local index (and its embedding).

    `tags` carries the ones that aren't discoverable from the body — the
    frontmatter of a file written in Obsidian, or tags passed to write().
    """
    tags = extract_tags(body, {"tags": tags} if tags else None)
    links = extract_links(body)
    vector = None
    if embed:
        try:
            vec = memory.embed(f"{title}\n{body[:4000]}")
            vector = json.dumps(vec) if vec else None
        except Exception:  # noqa: BLE001 — search quality, never a blocker
            vector = None
    return db.upsert_note(telegram_id, path, title, body,
                          ",".join(tags) or None, ",".join(links) or None,
                          vector, remote_sha)


def resolve(telegram_id: int, ref: str):
    """Find a note by path, exact title, or the user's rough words for it."""
    ref = (ref or "").strip()
    if not ref:
        return None
    for candidate in (ref, ref if ref.lower().endswith(".md") else ref + ".md"):
        try:
            hit = db.get_note(telegram_id, vaultmod.clean_path(candidate))
        except vaultmod.VaultError:
            hit = None
        if hit:
            return hit
    hits = db.find_notes(telegram_id, ref, limit=5)
    return hits[0] if hits else None


def read_note(telegram_id: int, ref: str) -> tuple[str, str] | None:
    """(path, body) for a note, pulling it from the vault if it isn't indexed."""
    hit = resolve(telegram_id, ref)
    if hit:
        return hit.path, hit.body
    # Not indexed — it may still exist in the vault (written in Obsidian).
    try:
        vault = vaultmod.backend(telegram_id)
        path = path_for(ref)
        text, sha = vault.read(path)
    except (vaultmod.VaultError, FileNotFoundError, OSError):
        return None
    meta, body = parse(text)
    _index(telegram_id, path, str(meta.get("title") or title_of(path)), body, sha,
           tags=extract_tags("", meta))
    return path, body


# --- Writing --------------------------------------------------------------
def path_by_title(telegram_id: int, title: str) -> str | None:
    """Where a note with this exact name already lives, wherever that is."""
    want = title_of(title).strip().lower()
    if not want:
        return None
    for n in db.all_notes(telegram_id):
        if (n.title or "").strip().lower() == want or title_of(n.path).lower() == want:
            return n.path
    return None


def write(telegram_id: int, title: str, content: str, folder: str | None = None,
          tags: list[str] | None = None, mode: str = "replace",
          path: str | None = None) -> dict:
    """Create, replace or append a note, then mirror it into the vault.

    Returns {'path', 'title', 'pushed', 'where', 'detail'}. A vault that can't
    be written to is reported, never swallowed — but the note is still saved
    locally so nothing the user dictated is lost.
    """
    # An existing title owns its path, even when the caller names a different
    # folder (or none). Otherwise "add this to Sliding Window" quietly starts a
    # second Sliding Window somewhere else — and in Obsidian two notes with one
    # name make every [[Sliding Window]] link ambiguous.
    if path:
        path = vaultmod.clean_path(path)
    else:
        path = path_by_title(telegram_id, title) or path_for(title, folder)
    note_title = (title or title_of(path)).strip() or title_of(path)
    existing = db.get_note(telegram_id, path)
    old_body = existing.body if existing else ""
    if not existing:
        # It may exist in the vault already (written in Obsidian) — read that
        # exact path, never a fuzzy match, or an append could land in the wrong
        # note and a replace could overwrite one the user is still writing.
        try:
            text, sha = vaultmod.backend(telegram_id).read(path)
            meta, old_body = parse(text)
            existing = _index(telegram_id, path,
                              str(meta.get("title") or title_of(path)), old_body,
                              sha or "local", embed=False)
        except (vaultmod.VaultError, FileNotFoundError, OSError):
            old_body = ""

    content = (content or "").strip()
    if mode == "append" and old_body:
        body = f"{old_body.rstrip()}\n\n{content}"
    elif mode == "prepend" and old_body:
        body = f"{content}\n\n{old_body.lstrip()}"
    else:
        body = content

    all_tags = extract_tags(body)
    for t in (tags or []):
        t = str(t).strip().lstrip("#")
        if t and t not in all_tags:
            all_tags.append(t)

    created = None
    if existing and existing.created_at:
        created = existing.created_at.date().isoformat()
    file_text = render(note_title, body, all_tags, created=created)

    pushed, where, detail = False, "", ""
    sha = None
    try:
        vault = vaultmod.backend(telegram_id)
        where = vault.describe()
        sha = vault.write(path, file_text, message=f"brain: {note_title}",
                          sha=existing.remote_sha if existing else None)
        # Local vaults have no sha; mark them pushed so sync() can still tell
        # a mirrored note from one that never made it out.
        sha = sha or "local"
        pushed = True
    except vaultmod.NotConnected as e:
        detail = str(e)
    except Exception as e:  # noqa: BLE001 — a vault outage must not lose the note
        detail = str(e)
        log.warning("vault push failed for %s: %s", path, e)

    _index(telegram_id, path, note_title, body, sha, tags=all_tags)
    # Set it either way: a note that failed to push must go back to "not in the
    # vault", or an edit made while GitHub was down would never be sent again.
    db.set_note_pushed(telegram_id, path, sha if pushed else None)
    return {"path": path, "title": note_title, "pushed": pushed,
            "where": where, "detail": detail}


def append_daily(telegram_id: int, line: str, heading: str | None = None,
                 when: date | None = None) -> dict:
    """Add a bullet to today's daily note, creating it if it's the first one."""
    path = daily_path(when)
    d = (when or date.today())
    stamp = datetime.now().strftime("%H:%M")
    entry = f"- {stamp} — {line.strip()}"
    got = read_note(telegram_id, path)
    body = got[1] if got else f"# {d.strftime('%A %d %B %Y')}\n"
    if heading:
        head = f"## {heading}"
        if head in body:
            # Slot the line under its heading rather than at the end of the file.
            parts = body.split(head, 1)
            rest = parts[1].split("\n## ", 1)
            block = rest[0].rstrip() + "\n" + entry
            tail = ("\n## " + rest[1]) if len(rest) > 1 else ""
            body = parts[0] + head + block + tail
        else:
            body = body.rstrip() + f"\n\n{head}\n{entry}"
    else:
        body = body.rstrip() + f"\n{entry}"
    return write(telegram_id, title_of(path), body, mode="replace", path=path)


# --- Research inbox (capture now, read on Sunday) -------------------------
_INBOX_ITEM = re.compile(r"^\s*-\s*\[( |x|X)\]\s*(.+?)\s*$")


def capture(telegram_id: int, text: str, doing: str | None = None) -> dict:
    """Park an off-topic urge in the inbox in one line, without losing focus."""
    path = inbox_path()
    got = read_note(telegram_id, path)
    body = got[1] if got else ("Dumped here the second it comes up. Read on Sunday, "
                               "not before.\n")
    stamp = date.today().isoformat()
    line = f"- [ ] {text.strip()} ({stamp}"
    line += f", while on {doing})" if doing else ")"
    body = body.rstrip() + "\n" + line
    return write(telegram_id, title_of(path), body, mode="replace", path=path)


def inbox_items(telegram_id: int, open_only: bool = True) -> list[tuple[int, str, bool]]:
    """[(line_number, text, done)] from the inbox note."""
    got = read_note(telegram_id, inbox_path())
    if not got:
        return []
    out = []
    for i, line in enumerate(got[1].splitlines()):
        m = _INBOX_ITEM.match(line)
        if not m:
            continue
        done = m.group(1).lower() == "x"
        if done and open_only:
            continue
        out.append((i, m.group(2), done))
    return out


def inbox_close(telegram_id: int, which: str | None = None) -> tuple[int, str]:
    """Tick off one item (matched by words) or all of them. (count, detail)"""
    got = read_note(telegram_id, inbox_path())
    if not got:
        return 0, "Inbox is empty."
    lines = got[1].splitlines()
    hit = 0
    closed = []
    for i, line in enumerate(lines):
        m = _INBOX_ITEM.match(line)
        if not m or m.group(1).lower() == "x":
            continue
        if which and which.lower() not in m.group(2).lower():
            continue
        lines[i] = line.replace("- [ ]", "- [x]", 1)
        closed.append(m.group(2))
        hit += 1
    if not hit:
        return 0, ("Nothing open matching that." if which else "Nothing open in the inbox.")
    write(telegram_id, title_of(inbox_path()), "\n".join(lines),
          mode="replace", path=inbox_path())
    return hit, "; ".join(c[:60] for c in closed[:5])


# --- Search ---------------------------------------------------------------
def search(telegram_id: int, query: str, limit: int = 8) -> list[dict]:
    """Notes closest to `query` by meaning, falling back to keyword search."""
    query = (query or "").strip()
    if not query:
        return []
    results: list[dict] = []
    vec = None
    try:
        vec = memory.embed(query)
    except Exception:  # noqa: BLE001
        vec = None
    if vec:
        for n in db.all_notes(telegram_id):
            if not n.vector:
                continue
            try:
                score = memory.cosine(vec, json.loads(n.vector))
            except (TypeError, ValueError):
                continue
            if score >= 0.25:
                results.append({"path": n.path, "title": n.title, "score": score,
                                "body": n.body, "tags": n.tags or ""})
        results.sort(key=lambda r: -r["score"])
    if not results:
        results = [{"path": n.path, "title": n.title, "score": 0.0,
                    "body": n.body, "tags": n.tags or ""}
                   for n in db.find_notes(telegram_id, query, limit=limit)]
    return results[:limit]


def backlinks(telegram_id: int, title: str) -> list[dict]:
    """Notes whose body links to [[title]] — Obsidian's backlink pane, in text."""
    want = title_of(title).strip().lower()
    out = []
    for n in db.all_notes(telegram_id):
        links = [l.strip().lower() for l in (n.links or "").split(",") if l.strip()]
        if want in links:
            out.append({"path": n.path, "title": n.title})
    return out


# --- Sync -----------------------------------------------------------------
def sync(telegram_id: int, limit: int = 500) -> str:
    """Reconcile the index with the vault, both directions.

    Pull wins on conflict: a file the user edited in Obsidian is their writing,
    and the bot's copy is only a mirror. Notes that never reached the vault
    (offline, bad token) are pushed on the way through.
    """
    vault = vaultmod.backend(telegram_id)          # raises NotConnected — caller reports it
    remote = vault.list()[:limit]
    pulled = pushed = skipped = 0
    seen = set()
    for path in remote:
        seen.add(path)
        try:
            text, sha = vault.read(path)
        except Exception as e:  # noqa: BLE001 — one bad file shouldn't stop the sync
            log.warning("vault read failed for %s: %s", path, e)
            skipped += 1
            continue
        meta, body = parse(text)
        local = db.get_note(telegram_id, path)
        if local and local.body.strip() == body.strip():
            continue
        _index(telegram_id, path, str(meta.get("title") or title_of(path)), body,
               sha or "local", tags=extract_tags("", meta))
        pulled += 1
    for n in db.all_notes(telegram_id):
        if n.path in seen or n.remote_sha:
            continue
        try:
            sha = vault.write(n.path, render(n.title, n.body,
                                             [t for t in (n.tags or "").split(",") if t]),
                              message=f"brain: {n.title}")
            db.upsert_note(telegram_id, n.path, n.title, n.body, n.tags, n.links,
                           None, sha or "local")
            pushed += 1
        except Exception as e:  # noqa: BLE001
            log.warning("vault push failed for %s: %s", n.path, e)
            skipped += 1
    return (f"🔄 Synced with {vault.describe()}: {pulled} note(s) pulled in, "
            f"{pushed} pushed out, {db.count_notes(telegram_id)} indexed"
            + (f", {skipped} skipped (see logs)" if skipped else "") + ".")
