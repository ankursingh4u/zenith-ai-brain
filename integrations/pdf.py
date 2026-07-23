"""Extract text from PDF bytes (bank/credit-card statements)."""
from __future__ import annotations

import io

from pypdf import PdfReader


class EncryptedPDF(Exception):
    """Raised when a statement PDF is password-protected and no/ wrong password given."""


def extract_text(content: bytes, password: str | None = None, max_chars: int = 12000) -> str:
    reader = PdfReader(io.BytesIO(content))
    if reader.is_encrypted:
        ok = False
        try:
            ok = bool(reader.decrypt(password or ""))
        except Exception:  # noqa: BLE001
            ok = False
        if not ok:
            raise EncryptedPDF("This statement PDF is password-protected.")
    text = "\n".join((page.extract_text() or "") for page in reader.pages)
    return text[:max_chars].strip()
