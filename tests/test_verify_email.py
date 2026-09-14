"""Address verification. DNS is stubbed, so nothing here touches the network.

The case that matters most is the fourth status: a lookup we could not perform
has to come back UNKNOWN, because calling an unmeasured address dead would
quietly drop a real prospect off the call list.
"""

from __future__ import annotations

import pytest

from app.tools import verify_email
from app.tools.verify_email import INVALID, RISKY, UNKNOWN, VALID, verify


@pytest.fixture
def mx(monkeypatch):
    """Control what DNS appears to say. True/False/None per the real contract."""
    def answer(value):
        monkeypatch.setattr(verify_email, "domain_accepts_mail", lambda domain: value)
    return answer


def test_a_named_address_on_a_live_domain_is_valid(mx):
    mx(True)
    verdict = verify("dave@whitakerroofing.com")
    assert verdict.status == VALID
    assert verdict.usable is True


def test_a_shared_mailbox_is_risky_not_invalid(mx):
    mx(True)
    verdict = verify("info@whitakerroofing.com")
    assert verdict.status == RISKY
    assert verdict.usable is True
    assert "no named reader" in verdict.reason


def test_a_freemail_address_is_risky(mx):
    mx(True)
    assert verify("whitakerroofs@gmail.com").status == RISKY


def test_a_domain_that_cannot_receive_mail_is_invalid(mx):
    mx(False)
    verdict = verify("dave@whitakerroofing.com")
    assert verdict.status == INVALID
    assert verdict.usable is False


def test_a_dns_failure_is_unknown_never_invalid(mx):
    mx(None)
    verdict = verify("dave@whitakerroofing.com")
    assert verdict.status == UNKNOWN
    assert "Could not check" in verdict.reason


def test_unknown_is_not_treated_as_usable(mx):
    """An operator should see it, but nothing should auto-promote it."""
    mx(None)
    assert verify("dave@whitakerroofing.com").usable is False


def test_a_throwaway_provider_is_invalid_without_asking_dns(monkeypatch):
    def explode(domain):
        raise AssertionError("DNS should not be consulted for a known throwaway")
    monkeypatch.setattr(verify_email, "domain_accepts_mail", explode)
    assert verify("x@mailinator.com").status == INVALID


def test_a_malformed_address_is_invalid_without_asking_dns(monkeypatch):
    def explode(domain):
        raise AssertionError("DNS should not be consulted for a non-address")
    monkeypatch.setattr(verify_email, "domain_accepts_mail", explode)
    assert verify("not-an-email").status == INVALID


def test_a_junk_address_never_reaches_verification(monkeypatch):
    monkeypatch.setattr(verify_email, "domain_accepts_mail", lambda d: True)
    assert verify("noreply@whitakerroofing.com").status == INVALID


def test_verify_never_raises_on_junk_input(monkeypatch):
    monkeypatch.setattr(verify_email, "domain_accepts_mail", lambda d: True)
    for raw in (None, "", "@", "a@", "@b.com", "a b@c.com"):
        assert verify(raw).status == INVALID


def test_a_missing_resolver_downgrades_to_unknown(monkeypatch):
    """dnspython absent must not break a sweep."""
    monkeypatch.setattr(verify_email, "_resolver", lambda: None)
    verify_email.domain_accepts_mail.cache_clear()
    assert verify_email.domain_accepts_mail("whitakerroofing.com") is None
    verify_email.domain_accepts_mail.cache_clear()


def test_to_dict_carries_status_and_reason(mx):
    mx(True)
    row = verify("dave@whitakerroofing.com").to_dict()
    assert row == {"status": VALID, "reason": "Domain accepts mail."}
