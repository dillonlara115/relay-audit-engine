"""The Gmail reader. No network: the API is a stub and the token never exists.

The properties worth pinning are the ones that keep this from becoming an inbox
reader or a send path, not the parsing.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone

import pytest

from app.tools import gmail
from app.tools.gmail import READONLY_SCOPE, SCOPES, Reply, fetch_replies, parse_message, strip_quoted


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def message(mid="m1", sender="Dave <dave@roofs.com>", subject="Re: a quick thought",
            body="Sure, give me a call Thursday.", when=None, thread="t1"):
    return {
        "id": mid, "threadId": thread,
        "internalDate": str(int((when or datetime(2026, 9, 15, tzinfo=timezone.utc)).timestamp() * 1000)),
        "snippet": body[:50],
        "payload": {
            "mimeType": "text/plain",
            "headers": [{"name": "From", "value": sender}, {"name": "Subject", "value": subject}],
            "body": {"data": b64(body)},
        },
    }


class FakeApi:
    """Stands in for the Gmail discovery client."""

    def __init__(self, messages=(), fail_on_list=False):
        self._messages = {m["id"]: m for m in messages}
        self.queries: list[str] = []
        self.fail_on_list = fail_on_list

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, userId, q, maxResults):
        self.queries.append(q)
        return _Exec({"messages": [{"id": i} for i in self._messages]})

    def get(self, userId, id, format):
        return _Exec(self._messages[id])


class _Exec:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        return self._payload


SINCE = datetime(2026, 9, 1, tzinfo=timezone.utc)


# ── The guarantees ────────────────────────────────────────────────────────────


def test_the_only_scope_is_read_only():
    assert SCOPES == (READONLY_SCOPE,)
    assert "readonly" in READONLY_SCOPE
    assert "send" not in READONLY_SCOPE and "compose" not in READONLY_SCOPE


def test_no_addresses_means_no_api_call_at_all():
    """The property that stops this being an inbox reader."""
    api = FakeApi([message()])
    assert fetch_replies([], since=SINCE, service=api) == []
    assert api.queries == []


def test_blank_addresses_are_not_a_wildcard():
    api = FakeApi([message()])
    assert fetch_replies(["", "   ", None], since=SINCE, service=api) == []
    assert api.queries == []


def test_the_search_names_every_address_and_nothing_else():
    api = FakeApi([message()])
    fetch_replies(["dave@roofs.com", "info@roofs.com"], since=SINCE, service=api)
    q = api.queries[0]
    assert "from:dave@roofs.com" in q and "from:info@roofs.com" in q
    assert "2026/09/01" in q


def test_a_sender_gmail_returned_but_we_did_not_ask_for_is_dropped():
    """Gmail's from: matching is fuzzy. The parsed address is checked again."""
    api = FakeApi([message(sender="someone@else.com")])
    assert fetch_replies(["dave@roofs.com"], since=SINCE, service=api) == []


def test_a_message_older_than_the_touch_is_not_a_reply_to_it():
    old = message(when=datetime(2026, 8, 20, tzinfo=timezone.utc))
    api = FakeApi([old])
    assert fetch_replies(["dave@roofs.com"], since=SINCE, service=api) == []


def test_a_real_reply_comes_back_parsed():
    api = FakeApi([message()])
    got = fetch_replies(["dave@roofs.com"], since=SINCE, service=api)
    assert len(got) == 1
    assert got[0].from_email == "dave@roofs.com"
    assert got[0].excerpt == "Sure, give me a call Thursday."


def test_a_token_that_is_missing_is_a_skipped_scan_not_a_crash(monkeypatch, tmp_path):
    monkeypatch.setattr(gmail, "token_path", lambda: tmp_path / "nope.json")
    with pytest.raises(gmail.GmailUnavailable) as exc:
        gmail._credentials()
    assert "gmail-connect" in str(exc.value)


def test_a_token_carrying_send_scope_is_refused(monkeypatch, tmp_path):
    """A credential that can send must not be used, even for reading."""
    path = tmp_path / "token.json"
    path.write_text("{}")
    monkeypatch.setattr(gmail, "token_path", lambda: path)

    class Creds:
        scopes = [READONLY_SCOPE, "https://www.googleapis.com/auth/gmail.send"]
        valid = True

    monkeypatch.setattr("google.oauth2.credentials.Credentials.from_authorized_user_file",
                        classmethod(lambda cls, *a, **k: Creds()))
    with pytest.raises(gmail.GmailUnavailable) as exc:
        gmail._credentials()
    assert "gmail.readonly" in str(exc.value)


# ── Reading what the person actually typed ────────────────────────────────────


def test_quoted_history_is_cut_off():
    body = ("No thanks, we're all set.\n\n"
            "On Mon, Sep 14 2026 at 9:02 AM Dillon wrote:\n"
            "> Nobody can book a time without waiting for a call back")
    assert strip_quoted(body) == "No thanks, we're all set."


def test_an_outlook_style_quote_is_cut_off():
    body = "Not interested.\n\n-----Original Message-----\nFrom: Dillon\nSubject: hi"
    assert strip_quoted(body) == "Not interested."


def test_a_signature_block_is_cut_off():
    assert strip_quoted("Call me Thursday.\n\n--\nDave\nRoofs Inc") == "Call me Thursday."


def test_a_reply_with_no_quoting_survives_whole():
    assert strip_quoted("Sounds good.") == "Sounds good."


def test_html_only_mail_is_still_readable():
    msg = {
        "id": "m2", "threadId": "t2", "internalDate": "1789000000000",
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [{"name": "From", "value": "dave@roofs.com"}],
            "parts": [{"mimeType": "text/html", "body": {"data": b64("<p>Call me <b>Thursday</b></p>")}}],
        },
    }
    assert "Call me Thursday" in parse_message(msg).excerpt


def test_plain_text_wins_over_html_when_both_are_present():
    msg = {
        "id": "m3", "threadId": "t3", "internalDate": "1789000000000",
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [{"name": "From", "value": "dave@roofs.com"}],
            "parts": [
                {"mimeType": "text/plain", "body": {"data": b64("the plain one")}},
                {"mimeType": "text/html", "body": {"data": b64("<p>the html one</p>")}},
            ],
        },
    }
    assert parse_message(msg).excerpt == "the plain one"


def test_a_message_with_no_sender_is_not_a_reply():
    assert parse_message({"id": "m4", "payload": {"headers": []}}) is None


def test_the_excerpt_is_capped():
    long_body = "word " * 800
    assert len(parse_message(message(body=long_body)).excerpt) <= gmail.EXCERPT_CHARS
