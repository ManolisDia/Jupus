import pytest

from backend.supervisor.heuristics import is_explicit_human_request


@pytest.mark.parametrize(
    "utterance",
    [
        "can I speak to a person",
        "I want a real person",
        "get me a representative",
        "let me talk to a human",
    ],
)
def test_common_phrases_detected(utterance):
    assert is_explicit_human_request(utterance) is True


@pytest.mark.parametrize(
    "utterance",
    [
        "I need help with my lease",
        "my email is john at gmail",
        "yes that's correct",
    ],
)
def test_unrelated_utterances_not_flagged(utterance):
    assert is_explicit_human_request(utterance) is False


def test_case_insensitive():
    assert is_explicit_human_request("SPEAK TO A PERSON") is True
