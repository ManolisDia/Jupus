import pytest

from backend.supervisor.faq import match_faq


@pytest.mark.parametrize(
    "utterance",
    [
        "Are you open on the weekend?",
        "do you guys work Saturdays",
        "is anyone there on Sunday",
    ],
)
def test_matches_weekend_hours_question(utterance):
    assert match_faq(utterance) is not None


def test_matches_office_address_question():
    assert match_faq("Where's your office located?") is not None


def test_no_match_returns_none():
    assert match_faq("my landlord is trying to evict me") is None


def test_case_insensitive():
    assert match_faq("WHERE IS YOUR OFFICE") is not None
