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


# --- a confident extraction of the wrong KIND of thing --------------------


def test_an_email_is_never_stored_as_a_name():
    # Live (docs/fixes/2026-08-28-008.md): extract_field was asked for the
    # name field, given "manos at gmail dot com", and returned
    # "manos@gmail.com" at 0.6 — which the confidence bands happily filed as
    # the caller's name. Confidence answers "did I hear that right", never
    # "is that the kind of thing I asked for".
    assert apply_extraction("name", "manos@gmail.com", 0.6) == (None, "missing")
    assert apply_extraction("name", "manos@gmail.com", 0.99) == (None, "missing")


def test_a_phone_number_is_never_stored_as_a_name():
    assert apply_extraction("name", "07577670101", 0.9) == (None, "missing")


def test_a_sentence_is_never_stored_as_a_name():
    narrative = "he told me on Tuesday that I had to be out of the flat by the end of the week"
    assert apply_extraction("name", narrative, 0.9) == (None, "missing")


def test_real_names_still_pass_the_bands_unchanged():
    assert apply_extraction("name", "Manos", 0.9) == ("Manos", "confirmed")
    assert apply_extraction("name", "Manos", 0.5) == ("Manos", "pending_confirm")
    assert apply_extraction("name", "Manos", 0.2) == (None, "missing")
    # email/phone still short-circuit above the name check
    assert apply_extraction("email", "manos@gmail.com", 0.6) == ("manos@gmail.com", "pending_confirm")
