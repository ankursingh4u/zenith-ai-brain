"""Where the markdown actually goes — the Obsidian side of the second brain.

Obsidian is not a service. It is a desktop app pointed at a folder of .md files,
with no API to call, so a bot running in a container can never talk to it
directly. What it CAN do is write real markdown into a place the user's vault
already syncs from, and let Obsidian pick it up:

  github — a repo that is (or holds) the vault. The user runs the Obsidian Git
           plugin, which pulls on a timer. Works from anywhere, keeps history,
           survives the bot being redeployed. This is the one to use when the
           bot is hosted.
  local  — a folder on the machine running the bot. Right when the bot runs on
           the same box as the vault (or on a synced folder: Dropbox, Syncthing,
           iCloud, Drive desktop). Also what the tests run against.

Both backends are the same four operations, so brain/notes.py never knows which
one it's writing to. Nothing here deletes: a vault is the user's own writing,
and a bot that can silently remove a note is not one you'd trust with it.
"""
from __future__ import annotations

import base64
import os
import re

import httpx

import config
import crypto
import db

_API = "https://api.github.com"
_TIMEOUT = 20.0


class VaultError(Exception):
    """Something went wrong talking to the vault."""


class NotConnected(VaultError):
    """No vault is linked yet — the caller should tell the user how to link one."""


def clean_path(path: str) -> str:
    """A safe, vault-relative path. Refuses anything that climbs out of it."""
    p = (path or "").strip().replace("\\", "/")
    p = re.sub(r"/{2,}", "/", p).strip("/")
    if not p:
        raise VaultError("Empty note path.")
    if re.match(r"^[a-zA-Z]:", p) or p.startswith("~"):
        raise VaultError(f"Not a vault-relative path: {path}")
    parts = [seg for seg in p.split("/") if seg not in (".", "")]
    if any(seg == ".." for seg in parts):
        raise VaultError(f"Path escapes the vault: {path}")
    # Characters Windows/Obsidian can't have in a filename.
    parts = [re.sub(r'[<>:"|?*\x00-\x1f]', "-", seg).strip() for seg in parts]
    if not parts or not parts[-1]:
        raise VaultError(f"Bad note path: {path}")
    return "/".join(parts)


class LocalVault:
    """A folder on this machine (or any folder something else syncs)."""

    kind = "local"

    def __init__(self, base_dir: str, base_path: str | None = None):
        root = os.path.abspath(os.path.expanduser(base_dir))
        if base_path:
            root = os.path.join(root, clean_path(base_path))
        self.root = root

    def describe(self) -> str:
        return f"local folder {self.root}"

    def _full(self, path: str) -> str:
        full = os.path.abspath(os.path.join(self.root, clean_path(path)))
        # Belt and braces: clean_path already refuses "..", this catches symlink
        # trickery and anything else that would land outside the vault.
        if os.path.commonpath([full, self.root]) != self.root:
            raise VaultError(f"Path escapes the vault: {path}")
        return full

    def read(self, path: str) -> tuple[str, str | None]:
        full = self._full(path)
        if not os.path.exists(full):
            raise FileNotFoundError(path)
        with open(full, "r", encoding="utf-8") as fh:
            return fh.read(), None

    def exists(self, path: str) -> bool:
        return os.path.exists(self._full(path))

    def write(self, path: str, text: str, message: str = "",
              sha: str | None = None) -> str | None:
        full = self._full(path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        return None

    def list(self, prefix: str = "") -> list[str]:
        if not os.path.isdir(self.root):
            # Say so instead of reporting an empty vault — a typo'd path would
            # otherwise look exactly like a vault with nothing in it yet.
            raise VaultError(f"No such folder: {self.root}")
        base = self.root
        if prefix:
            base = os.path.join(self.root, clean_path(prefix))
        out: list[str] = []
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in filenames:
                if not name.lower().endswith(".md"):
                    continue
                rel = os.path.relpath(os.path.join(dirpath, name), self.root)
                out.append(rel.replace("\\", "/"))
        return sorted(out)


class GitHubVault:
    """A GitHub repo holding the vault, driven by the Contents API.

    Deliberately plain REST over httpx (already a dependency) rather than a git
    client: no working copy to keep on a container's disk, no merge state to get
    stuck in, and every write is an ordinary commit the user can read or revert.
    """

    kind = "github"

    def __init__(self, repo: str, token: str, branch: str = "main",
                 base_path: str | None = None):
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo or ""):
            raise VaultError(f"Repo should look like owner/name, got '{repo}'.")
        self.repo = repo
        self.branch = branch or "main"
        self.base_path = clean_path(base_path) if base_path else ""
        self._h = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def describe(self) -> str:
        where = f"{self.repo}@{self.branch}"
        return f"github {where}/{self.base_path}" if self.base_path else f"github {where}"

    def _full(self, path: str) -> str:
        p = clean_path(path)
        return f"{self.base_path}/{p}" if self.base_path else p

    def _url(self, path: str) -> str:
        return f"{_API}/repos/{self.repo}/contents/{self._full(path)}"

    def _explain(self, resp: httpx.Response) -> str:
        """Turn a GitHub error into something the user can act on. Needs the
        repo and branch, so it is NOT a staticmethod — it once was, and the
        404 branch (wrong repo name, wrong branch: the most likely mistake
        anyone makes at setup) died with a NameError instead of explaining."""
        try:
            msg = resp.json().get("message", "")
        except Exception:  # noqa: BLE001
            msg = resp.text[:200]
        if resp.status_code in (401, 403):
            return (f"GitHub refused the token ({msg}). It needs 'Contents: "
                    f"read and write' on that repo.")
        if resp.status_code == 404:
            return (f"GitHub can't find {self.repo} on branch {self.branch} "
                    f"({msg}). Check the name, the branch, and that the token "
                    f"can see it.")
        if resp.status_code == 409:
            return ("The repo is empty — make one commit in it (even a README) "
                    "and try again.")
        return f"GitHub error {resp.status_code}: {msg}"

    def read(self, path: str) -> tuple[str, str | None]:
        with httpx.Client(timeout=_TIMEOUT) as c:
            r = c.get(self._url(path), headers=self._h, params={"ref": self.branch})
        if r.status_code == 404:
            raise FileNotFoundError(path)
        if r.status_code >= 400:
            raise VaultError(self._explain(r))
        data = r.json()
        raw = base64.b64decode(data.get("content", "")).decode("utf-8", "replace")
        return raw, data.get("sha")

    def exists(self, path: str) -> bool:
        try:
            self.read(path)
            return True
        except FileNotFoundError:
            return False

    def write(self, path: str, text: str, message: str = "",
              sha: str | None = None) -> str | None:
        if sha is None:
            # Updating a file without its current sha is rejected, so look it up.
            try:
                _, sha = self.read(path)
            except FileNotFoundError:
                sha = None
        body = {
            "message": message or f"brain: update {self._full(path)}",
            "content": base64.b64encode(text.encode("utf-8")).decode(),
            "branch": self.branch,
        }
        if sha:
            body["sha"] = sha
        with httpx.Client(timeout=_TIMEOUT) as c:
            r = c.put(self._url(path), headers=self._h, json=body)
        if r.status_code >= 400:
            raise VaultError(self._explain(r))
        return (r.json().get("content") or {}).get("sha")

    def list(self, prefix: str = "") -> list[str]:
        url = f"{_API}/repos/{self.repo}/git/trees/{self.branch}"
        with httpx.Client(timeout=_TIMEOUT) as c:
            r = c.get(url, headers=self._h, params={"recursive": "1"})
        if r.status_code >= 400:
            raise VaultError(self._explain(r))
        want = self.base_path + "/" if self.base_path else ""
        if prefix:
            want += clean_path(prefix).rstrip("/") + "/"
        out = []
        for node in r.json().get("tree", []):
            if node.get("type") != "blob":
                continue
            p = node.get("path", "")
            if not p.lower().endswith(".md") or not p.startswith(want):
                continue
            out.append(p[len(self.base_path) + 1:] if self.base_path else p)
        return sorted(out)


def backend(telegram_id: int):
    """This user's vault, or NotConnected with instructions they can act on.

    Per user, never global — the same reason sheets and Google accounts are.
    VAULT_DIR is only a fallback for a self-hosted, single-person setup.
    """
    link = db.get_vault_link(telegram_id)
    if link is None:
        if config.VAULT_DIR:
            return LocalVault(config.VAULT_DIR, base_path=str(telegram_id)
                              if config.VAULT_PER_USER_SUBDIR else None)
        raise NotConnected(
            "No Obsidian vault linked yet. Run /vault in Telegram to connect one "
            "(a GitHub repo the Obsidian Git plugin syncs, or a folder on the "
            "machine running me)."
        )
    if link.kind == "github":
        if not link.token_enc:
            raise NotConnected("The GitHub vault link has no token. Re-run /vault github …")
        try:
            token = crypto.decrypt(link.token_enc)
        except Exception as e:  # noqa: BLE001
            raise VaultError(f"Can't read the stored GitHub token: {e}") from e
        return GitHubVault(link.repo or "", token, link.branch, link.base_path)
    return LocalVault(link.repo or config.VAULT_DIR or ".", link.base_path)


def status(telegram_id: int) -> str:
    """One line for /vault and vault_status — never leaks the token."""
    link = db.get_vault_link(telegram_id)
    if link is None:
        if config.VAULT_DIR:
            return f"Vault: server default folder ({config.VAULT_DIR}). Not linked to you."
        return "Vault: not linked."
    if link.kind == "github":
        where = f"{link.repo} (branch {link.branch})"
        if link.base_path:
            where += f", folder {link.base_path}"
        return f"Vault: GitHub — {where}"
    return f"Vault: local folder — {link.repo or config.VAULT_DIR}" + (
        f"/{link.base_path}" if link.base_path else "")


def check(telegram_id: int) -> tuple[bool, str]:
    """Prove the link really works, by listing the vault. Used at link time."""
    try:
        vault = backend(telegram_id)
        files = vault.list()
        return True, f"{vault.describe()} — {len(files)} markdown file(s) visible."
    except NotConnected as e:
        return False, str(e)
    except Exception as e:  # noqa: BLE001
        return False, str(e)
