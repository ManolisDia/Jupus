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


def test_invalid_email_forces_pending_despite_high_confidence():
    value, status = apply_extraction("email", "not-an-email", 0.95)
    assert status == "pending_confirm"


def test_valid_email_high_confidence_confirms():
    value, status = apply_extraction("email", "a@b.com", 0.9)
    assert value == "a@b.com"
    assert status == "confirmed"
