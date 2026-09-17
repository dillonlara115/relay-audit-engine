"""The operator's own mailbox: reading replies, and sending one email at a time.

Criteria section 6 needs to know whether a touch was answered, so replies are
read here. Hard rule 4, as amended on Sep 17, 2026, lets the console send an
email a person has read and pressed Send on, so sending lives here too, in
one function, with the scope to match.

Four things are enforced here rather than asked for:

1. **The scopes are `gmail.readonly` and `gmail.send`, and the module refuses
   to build a client on anything wider.** A token from before the amendment,
   read only, still reads; asking it to send fails with a sentence saying to
   reconnect. Nothing else (compose, modify, full mail) is ever accepted.

2. **Search is always scoped to addresses we already hold.** There is no code
   path that lists a mailbox, and a caller that passes no addresses gets an
   empty list rather than an inbox. The operator's mail to their accountant is
   none of this system's business.

3. **Only an excerpt is stored.** The classifier needs enough to judge intent
   and the console needs enough to recognise the message. Neither needs the
   whole thread, so the whole thread is never written down.

4. **`send_message` sends exactly one message to the addresses it is given.**
   No list, no loop, no schedule. The console route is its only caller, and a
   test greps the package so a second one is a visible edit.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from app.config import REPO_ROOT, get_config

# The scopes this module will accept, and no others. Widening this further is
# a rule 4 amendment and should be a visible edit here, not a config value.
READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
SCOPES = (READONLY_SCOPE, SEND_SCOPE)
# A token stored before the Sep 17 amendment carries only the read scope. It
# keeps working for what it could always do.
ACCEPTED_SCOPE_SETS = (frozenset({READONLY_SCOPE}), frozenset(SCOPES))

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


def _credentials(*, require_send: bool = False) -> Any:
    """Load the stored refresh token and assert its scope.

    Raises rather than downgrading: a credential carrying a scope outside the
    accepted sets is refused outright, because continuing with it while it
    still reads would leave the extra capability sitting in the process.
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

    granted = frozenset(creds.scopes or ())
    if granted not in ACCEPTED_SCOPE_SETS:
        raise GmailUnavailable(
            f"the stored token carries {sorted(granted) or 'no scopes'}, and this "
            f"module only accepts gmail.readonly with gmail.send (or gmail.readonly "
            f"alone, for reading). Delete {path} and reconnect."
        )
    if require_send and SEND_SCOPE not in granted:
        raise GmailUnavailable(
            "the stored Gmail token is read only and cannot send. Reconnect with: "
            "python -m app.cli gmail-connect --force"
        )

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            try:
                path.write_text(creds.to_json())
            except OSError:
                # A Secret Manager mount on Cloud Run is read only. The refresh
                # token is unchanged, so the next process refreshes again.
                pass
        else:
            raise GmailUnavailable("the stored Gmail token cannot be refreshed. Reconnect.")
    return creds


def _service(*, require_send: bool = False) -> Any:
    try:
        from googleapiclient.discovery import build
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise GmailUnavailable(f"google-api-python-client is not installed: {exc}") from exc
    return build("gmail", "v1", credentials=_credentials(require_send=require_send),
                 cache_discovery=False)


@dataclass(frozen=True)
class SentMessage:
    message_id: str
    thread_id: str


_URL = re.compile(r"https?://[^\s<>\"]+")


def text_to_html(body: str, logo_url: str = "") -> str:
    """The same words as the text part, as paragraphs, with links clickable
    and the logo under the signature. No styles beyond a font and a width, no
    tracking, nothing the text part does not say: the HTML exists so Gmail
    does not re-wrap the lines and so the logo can appear at all."""
    from html import escape

    def para(block: str) -> str:
        lines = []
        for line in block.split("\n"):
            parts, last = [], 0
            for m in _URL.finditer(line):
                parts.append(escape(line[last:m.start()]))
                url = m.group(0).rstrip(".,;:)")
                parts.append(f'<a href="{escape(url, quote=True)}">{escape(url)}</a>')
                parts.append(escape(m.group(0)[len(url):]))
                last = m.end()
            parts.append(escape(line[last:]))
            lines.append("".join(parts))
        return "<p>" + "<br>".join(lines) + "</p>"

    text = body.replace("\r\n", "\n").strip()
    blocks = [b for b in re.split(r"\n{2,}", text) if b.strip()]
    html = "\n".join(para(b) for b in blocks)
    if logo_url:
        html += (f'\n<p><img src="{escape(logo_url, quote=True)}" width="40" height="40" '
                 f'alt="" style="display:block;border:0"></p>')
    return ('<div style="font-family:Arial,Helvetica,sans-serif;font-size:15px;'
            'line-height:1.5;color:#16120E;max-width:640px">' + html + "</div>")


def build_raw(*, to: str, subject: str, body: str, in_reply_to: str | None = None,
              logo_url: str = "") -> str:
    """One RFC 5322 message, base64url as the API wants it: the text as the
    first part, the same text as simple HTML as the alternative. A client
    that prefers text shows the words unchanged."""
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = " ".join(subject.split())
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    text = body.replace("\r\n", "\n")
    msg.set_content(text)
    msg.add_alternative(text_to_html(text, logo_url), subtype="html")
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


def send_message(*, to: str, subject: str, body: str, thread_id: str | None = None,
                 in_reply_to: str | None = None, logo_url: str = "",
                 service: Any = None) -> SentMessage:
    """Send one email from the connected mailbox to one address, now.

    Called from exactly one place, the console route behind the Send button,
    after suppression, the sequence state, the copy checks and the daily cap
    have all passed. It does not loop and takes no list. `thread_id` puts a
    follow-up under the first email in the recipient's client.
    """
    to = (to or "").strip()
    if not to or "@" not in to or any(c in to for c in " ,;\n"):
        raise ValueError(f"send_message needs one address, got {to!r}")
    api = service or _service(require_send=True)
    payload: dict[str, Any] = {"raw": build_raw(to=to, subject=subject, body=body,
                                                in_reply_to=in_reply_to, logo_url=logo_url)}
    if thread_id:
        payload["threadId"] = thread_id
    sent = api.users().messages().send(userId="me", body=payload).execute()
    return SentMessage(message_id=str(sent.get("id") or ""),
                       thread_id=str(sent.get("threadId") or thread_id or ""))


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
