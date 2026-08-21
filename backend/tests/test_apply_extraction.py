from backend.supervisor.graph import apply_extraction


def test_high_confidence_confirms():
    value, status = apply_extraction("name", "Alex Smith", 0.9)
    assert value == "Alex Smith"
    assert status == "confirmed"


def test_medium_confidence_pending():
    value, status = apply_extraction("name", "Alex Smith", 0.6)
    assert status == "pending_confirm"


def test_low_confidence_discarded():
    value, status = apply_extraction("name", "garbled noise", 0.2)
    assert value is None
    assert status == "missing"


def test_email_high_confidence_still_pending_regardless_of_format():
    # apply_extraction itself does NOT check format validity for email/phone
    # (that check now lives in node_capture's _is_valid_format, which runs
    # before apply_extraction is ever reached for these two fields) — this
    # just confirms apply_extraction doesn't special-case an invalid-looking
    # value differently from a valid one; both always land on pending_confirm
    value, status = apply_extraction("email", "not-an-email", 0.95)
    assert status == "pending_confirm"


def test_valid_email_high_confidence_still_requires_confirmation():
    # email/phone are never auto-trusted regardless of validity/confidence —
    # a wrong value here means the firm can't reach the caller back
    value, status = apply_extraction("email", "a@b.com", 0.9)
    assert value == "a@b.com"
    assert status == "pending_confirm"


def test_phone_high_confidence_still_requires_confirmation():
    value, status = apply_extraction("phone", "5551234567", 0.9)
    assert value == "5551234567"
    assert status == "pending_confirm"


def test_email_zero_confidence_discarded():
    value, status = apply_extraction("email", None, 0)
    assert value is None
    assert status == "missing"
