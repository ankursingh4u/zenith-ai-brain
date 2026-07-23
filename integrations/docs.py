"""Per-user Google Docs: create, append to, and read documents."""
from __future__ import annotations

from googleapiclient.discovery import build

from integrations import client


def _docs(telegram_id: int, account: str):
    from integrations.client import _load_credentials
    return build("docs", "v1", credentials=_load_credentials(telegram_id, account))


def create_doc(telegram_id: int, account: str, title: str, content: str = "") -> str:
    """Create a Google Doc with optional initial text. Returns its URL."""
    svc = _docs(telegram_id, account)
    doc = svc.documents().create(body={"title": title}).execute()
    doc_id = doc["documentId"]
    if content:
        svc.documents().batchUpdate(
            documentId=doc_id,
            body={"requests": [
                {"insertText": {"location": {"index": 1}, "text": content}}
            ]},
        ).execute()
    return f"https://docs.google.com/document/d/{doc_id}/edit"


def append_to_doc(telegram_id: int, doc_id: str, text: str) -> str:
    """Append text to the end of an existing doc."""
    svc = _docs(telegram_id)
    doc = svc.documents().get(documentId=doc_id).execute()
    end = doc["body"]["content"][-1]["endIndex"] - 1
    svc.documents().batchUpdate(
        documentId=doc_id,
        body={"requests": [
            {"insertText": {"location": {"index": end}, "text": "\n" + text}}
        ]},
    ).execute()
    return "appended"


def read_doc(telegram_id: int, doc_id: str) -> str:
    """Return the plain text of a doc."""
    svc = _docs(telegram_id)
    doc = svc.documents().get(documentId=doc_id).execute()
    out = []
    for element in doc.get("body", {}).get("content", []):
        para = element.get("paragraph")
        if not para:
            continue
        for run in para.get("elements", []):
            txt = run.get("textRun", {}).get("content", "")
            out.append(txt)
    return "".join(out).strip()
