"""Reading replies out of the operator's own mailbox. Read only, by construction.

Criteria section 6 needs to know whether a touch was answered, and hard rule 4
says nothing in this codebase sends. Reading satisfies both: a reply is an
observation, not an outreach action.

Three things are enforced here rather than asked for:

1. **The scope is `gmail.readonly` and the module refuses to build a client on
   anything wider.** A token that cannot send means no code path can send,
   including code nobody has written yet. That is a stronger guarantee than the
   import check in the console tests, which is a string match.

2. **Search is always scoped to addresses we already hold.** There is no code
   path that lists a mailbox, and a caller that passes no addresses gets an
   empty list rather than an inbox. The operator's mail to their accountant is
   none of this system's business.

3. **Only an excerpt is stored.** The classifier needs enough to judge intent
   and the console needs enough to recognise the message. Neither needs the
   whole thread, so the whole thread is never written down.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from app.config import REPO_ROOT, get_config

# The only scope this module will accept. Widening it is a rule 4 amendment and
# should be a visible edit here, not a config value someone can flip.
READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
SCOPES = (READONLY_SCOPE,)

EXCERPT_CHARS = 1200

# Quoted history and signature blocks. A reply that says "no thanks" on top of
# the original message would otherwise classify against our own copy.
_QUOTE_MARKERS = (
    re.compile(r"^\s*On .{0,120}\bwrote:\s*$", re.M),
    re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}\s*$", re.M | re.I),
    re.compile(r"^\s*From:\s.+$", re.M),
    re.compile(r"^\s*_{10,}\s*$", re.M),
)
_SIGNATURE = re.compile(r"^\s*--\s*$", re.M)


class GmailUnavailable(RuntimeError):
    """No usable credential. A missing mailbox is a skipped scan, not a crash."""


@dataclass(frozen=True)
class Reply:
    """One inbound message, reduced to what the ledger needs."""

    message_id: str
    thread_id: str
    from_email: str
    subject: str
    received_at: datetime
    excerpt: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "thread_id": self.thread_id,
            "from_email": self.from_email,
            "subject": self.subject,
            "received_at": self.received_at,
            "excerpt": self.excerpt,
        }


def token_path() -> Path:
    cfg = get_config()
    return Path(cfg.gmail_token_path or (REPO_ROOT / ".gmail-token.json"))


def _credentials() -> Any:
    """Load the stored refresh token and assert its scope.

    Raises rather than downgrading: a credential with send scope attached is a
    rule 4 problem, and continuing with it because reading still works would
    leave the capability sitting in the process.
    """
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise GmailUnavailable(f"google-auth is not installed: {exc}") from exc

    path = token_path()
    if not path.exists():
        raise GmailUnavailable(
            f"no Gmail token at {path}. Run: python -m app.cli gmail-connect"
        )

    creds = Credentials.from_authorized_user_file(str(path), list(SCOPES))

    granted = set(creds.scopes or ())
    if granted != set(SCOPES):
        raise GmailUnavailable(
            f"the stored token carries {sorted(granted) or 'no scopes'}, and this "
            f"module only accepts {READONLY_SCOPE}. Delete {path} and reconnect."
        )

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            path.write_text(creds.to_json())
        else:
            raise GmailUnavailable("the stored Gmail token cannot be refreshed. Reconnect.")
    return creds


def _service() -> Any:
    try:
        from googleapiclient.discovery import build
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise GmailUnavailable(f"google-api-python-client is not installed: {exc}") from exc
    return build("gmail", "v1", credentials=_credentials(), cache_discovery=False)


def strip_quoted(body: str) -> str:
    """The part of a reply the person actually typed.

    Everything from the first quote marker or signature separator onward is
    our own message coming back, and classifying against it would read our
    copy rather than their answer.
    """
    cut = len(body)
    for pattern in (*_QUOTE_MARKERS, _SIGNATURE):
        match = pattern.search(body)
        if match and match.start() < cut:
            cut = match.start()
    return body[:cut].strip()


def _decode(data: str | None) -> str:
    if not data:
        return ""
    try:
        return base64.urlsafe_b64decode(data.encode()).decode("utf-8", "replace")
    except (ValueError, TypeError):
        return ""


def _plain_body(payload: dict[str, Any]) -> str:
    """Prefer text/plain. Fall back to stripping tags off text/html."""
    stack = [payload]
    html_fallback = ""
    while stack:
        part = stack.pop(0)
        mime = part.get("mimeType") or ""
        body = _decode((part.get("body") or {}).get("data"))
        if mime == "text/plain" and body:
            return body
        if mime == "text/html" and body and not html_fallback:
            html_fallback = body
        stack.extend(part.get("parts") or [])
    if html_fallback:
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html_fallback))
    return ""


def _header(headers: Sequence[dict[str, Any]], name: str) -> str:
    lowered = name.lower()
    return next((h.get("value") or "" for h in headers
                 if (h.get("name") or "").lower() == lowered), "")


def _address_of(raw: str) -> str:
    match = re.search(r"[\w.%+\-]+@[\w.\-]+\.\w+", raw or "")
    return match.group(0).lower() if match else ""


def parse_message(message: dict[str, Any]) -> Reply | None:
    """One Gmail message resource into a Reply. None when it is not usable."""
    payload = message.get("payload") or {}
    headers = payload.get("headers") or []
    from_email = _address_of(_header(headers, "From"))
    if not from_email:
        return None

    received = message.get("internalDate")
    when = (datetime.fromtimestamp(int(received) / 1000, tz=timezone.utc)
            if received else datetime.now(timezone.utc))

    body = strip_quoted(_plain_body(payload)) or (message.get("snippet") or "")
    return Reply(
        message_id=str(message.get("id") or ""),
        thread_id=str(message.get("threadId") or ""),
        from_email=from_email,
        subject=_header(headers, "Subject"),
        received_at=when,
        excerpt=re.sub(r"\s+", " ", body).strip()[:EXCERPT_CHARS],
    )


def _query(addresses: Sequence[str], since: datetime) -> str:
    """Gmail search scoped to known addresses and to time.

    `after:` takes a date, so this is deliberately a day wider than the caller
    asked for. The caller filters precisely; this only has to narrow the
    request enough that we are not pulling a mailbox.
    """
    senders = " OR ".join(f"from:{a}" for a in addresses)
    return f"({senders}) after:{since.strftime('%Y/%m/%d')} -in:chats"


def fetch_replies(addresses: Iterable[str], *, since: datetime,
                  limit: int = 25, service: Any = None) -> list[Reply]:
    """Replies from these addresses since this moment. Never lists a mailbox.

    An empty address list returns an empty result and makes no API call, which
    is the property that keeps this from becoming an inbox reader.
    """
    known = [a.strip().lower() for a in addresses if a and a.strip()]
    if not known:
        return []

    api = service or _service()
    listed = (api.users().messages()
              .list(userId="me", q=_query(known, since), maxResults=limit)
              .execute())

    out: list[Reply] = []
    for stub in listed.get("messages") or []:
        raw = (api.users().messages()
               .get(userId="me", id=stub["id"], format="full").execute())
        reply = parse_message(raw)
        # Gmail's `from:` matching is fuzzy about plus-addressing and display
        # names, so the membership test is repeated here on the parsed address.
        if reply and reply.from_email in known and reply.received_at >= since:
            out.append(reply)
    return out
