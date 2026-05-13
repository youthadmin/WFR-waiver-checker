#!/usr/bin/env python3
"""
gmail_watcher.py — read-only Gmail fetch of new WFR waiver emails.

Authenticates with credentials.json + token.json (gmail.readonly scope —
generate with `python capture_gmail_auth.py`). The watcher never sends
or modifies email.

Public surface:
    fetch_new_waiver_messages(query=None) -> list[FetchedMessage]
    load_processed() -> dict[message_id, {processed_at, name_count}]
    mark_processed(message_id, name_count=0) -> None
    build_query() -> str   # exposed for logging / debug

A FetchedMessage carries the raw PDF attachment bytes (and plain-text
body as fallback). Name extraction stays in waiver_parser.parse() — the
watcher knows nothing about names. main.py wires the two together.

Run standalone for a smoke test (requires capture_gmail_auth.py first):
    python gmail_watcher.py
"""

from __future__ import annotations

import base64
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

load_dotenv()

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CREDENTIALS_FILE = Path(__file__).parent / "credentials.json"
TOKEN_FILE = Path(__file__).parent / "token.json"
PROCESSED_FILE = Path(__file__).parent / "processed.json"

MAX_MESSAGES_PER_FETCH = 20


@dataclass
class FetchedAttachment:
    filename: str
    mime_type: str
    data: bytes


@dataclass
class FetchedMessage:
    message_id: str
    thread_id: str
    subject: str
    sender: str
    internal_date_ms: int
    attachments: list[FetchedAttachment] = field(default_factory=list)
    body_text: str = ""
    body_html: str = ""

    @property
    def received_at(self) -> datetime:
        return datetime.fromtimestamp(self.internal_date_ms / 1000, tz=timezone.utc)


def _get_service():
    if not TOKEN_FILE.exists():
        raise RuntimeError(
            f"{TOKEN_FILE} not found. Run `python capture_gmail_auth.py` first."
        )
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), GMAIL_SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            TOKEN_FILE.write_text(creds.to_json())
        else:
            raise RuntimeError(
                "Gmail credentials are invalid and cannot be refreshed. "
                "Re-run capture_gmail_auth.py."
            )
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def build_query() -> str:
    sender = os.environ.get("WAIVER_SENDER", "nharrison@younglife.org")
    subject = os.environ.get("WAIVER_SUBJECT", "WFR waiver form")
    return f'from:{sender} subject:"{subject}" newer_than:2d'


def load_processed() -> dict:
    if not PROCESSED_FILE.exists():
        return {}
    try:
        data = json.loads(PROCESSED_FILE.read_text())
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def mark_processed(message_id: str, name_count: int = 0) -> None:
    data = load_processed()
    data[message_id] = {
        "processed_at": datetime.now(timezone.utc).isoformat(),
        "name_count": name_count,
    }
    PROCESSED_FILE.write_text(json.dumps(data, indent=2, sort_keys=True))


def _walk_parts(parts: list, into: FetchedMessage, service, msg_id: str) -> None:
    for part in parts or []:
        if part.get("parts"):
            _walk_parts(part["parts"], into, service, msg_id)
            continue

        mime = part.get("mimeType", "")
        filename = part.get("filename", "")
        body = part.get("body", {})

        if filename:
            if mime != "application/pdf":
                continue
            data = _decode_body_data(body, service, msg_id)
            if data is None:
                continue
            into.attachments.append(FetchedAttachment(filename=filename, mime_type=mime, data=data))
            continue

        if mime == "text/plain" and "data" in body:
            into.body_text += base64.urlsafe_b64decode(body["data"]).decode("utf-8", errors="replace")
        elif mime == "text/html" and "data" in body:
            into.body_html += base64.urlsafe_b64decode(body["data"]).decode("utf-8", errors="replace")


def _decode_body_data(body: dict, service, msg_id: str) -> Optional[bytes]:
    if body.get("attachmentId"):
        att = service.users().messages().attachments().get(
            userId="me", messageId=msg_id, id=body["attachmentId"]
        ).execute()
        if "data" in att:
            return base64.urlsafe_b64decode(att["data"])
        return None
    if "data" in body:
        return base64.urlsafe_b64decode(body["data"])
    return None


def _hydrate_message(service, msg_id: str) -> FetchedMessage:
    full = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
    headers = {h["name"].lower(): h["value"] for h in full.get("payload", {}).get("headers", [])}
    fm = FetchedMessage(
        message_id=msg_id,
        thread_id=full.get("threadId", ""),
        subject=headers.get("subject", ""),
        sender=headers.get("from", ""),
        internal_date_ms=int(full.get("internalDate", 0)),
    )

    payload = full.get("payload", {})
    if payload.get("parts"):
        _walk_parts(payload["parts"], fm, service, msg_id)
    else:
        mime = payload.get("mimeType", "")
        body = payload.get("body", {})
        if mime == "text/plain" and "data" in body:
            fm.body_text = base64.urlsafe_b64decode(body["data"]).decode("utf-8", errors="replace")
        elif mime == "text/html" and "data" in body:
            fm.body_html = base64.urlsafe_b64decode(body["data"]).decode("utf-8", errors="replace")

    return fm


def fetch_new_waiver_messages(query: Optional[str] = None) -> list[FetchedMessage]:
    """Return unprocessed messages matching the query, oldest-first."""
    service = _get_service()
    q = query or build_query()
    processed = load_processed()

    list_resp = service.users().messages().list(
        userId="me", q=q, maxResults=MAX_MESSAGES_PER_FETCH
    ).execute()
    refs = list_resp.get("messages", []) or []

    new = [r for r in refs if r["id"] not in processed]
    fetched = [_hydrate_message(service, r["id"]) for r in new]
    fetched.sort(key=lambda m: m.internal_date_ms)
    return fetched


def _smoke_test() -> int:
    q = build_query()
    print(f"Query: {q}")
    try:
        msgs = fetch_new_waiver_messages()
    except RuntimeError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    print(f"Found {len(msgs)} unprocessed message(s).")
    for m in msgs:
        print()
        print(f"  message_id:  {m.message_id}")
        print(f"  subject:     {m.subject!r}")
        print(f"  from:        {m.sender}")
        print(f"  received:    {m.received_at.isoformat()}")
        print(f"  attachments: {len(m.attachments)}")
        for a in m.attachments:
            print(f"    • {a.filename}  ({a.mime_type}, {len(a.data)} bytes)")
        if m.body_text:
            print(f"  body_text:   {len(m.body_text)} chars")
        if m.body_html:
            print(f"  body_html:   {len(m.body_html)} chars")
    print()
    print("(Smoke test does not mark messages processed; main.py owns that.)")
    return 0


if __name__ == "__main__":
    sys.exit(_smoke_test())
