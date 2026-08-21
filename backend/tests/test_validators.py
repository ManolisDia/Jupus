from backend.supervisor.tools import validate_email, validate_phone


def test_validate_email_accepts_standard_address():
    assert validate_email("j.smith@example.com") is True


def test_validate_email_rejects_missing_at_symbol():
    assert validate_email("j.smith example.com") is False


def test_validate_email_rejects_missing_domain_dot():
    assert validate_email("j.smith@examplecom") is False


def test_validate_phone_accepts_plain_digits():
    assert validate_phone("5551234567") is True


def test_validate_phone_accepts_punctuated_number():
    assert validate_phone("(555) 123-4567") is True


def test_validate_phone_rejects_too_short():
    assert validate_phone("12345") is False


def test_validate_phone_rejects_non_numeric():
    assert validate_phone("call-me-maybe") is False
