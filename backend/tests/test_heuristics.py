import pytest

from backend.supervisor.heuristics import is_explicit_human_request, looks_like_field_shape, looks_like_tangent


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


@pytest.mark.parametrize(
    "utterance",
    [
        "what do you need that for?",
        "wait, how long will this take",
        "actually, can I ask something first",
        "sorry, what was the question",
        "",
        "   ",
        "is this going to take long?",
    ],
)
def test_tangent_utterances_flagged(utterance):
    assert looks_like_tangent(utterance) is True


@pytest.mark.parametrize(
    "utterance",
    [
        "Manos",
        "manos@gmail.com",
        "07577670101",
        "yes that's right",
        "no, it's Alex Smith",
        "Thursday afternoon",
    ],
)
def test_plausible_direct_answers_not_flagged(utterance):
    assert looks_like_tangent(utterance) is False


def test_case_insensitive_prefix_match():
    assert looks_like_tangent("WAIT, one second") is True


@pytest.mark.parametrize(
    "utterance",
    ["manos@gmail.com", "manos at gmail dot com", "it's manos AT gmail DOT com"],
)
def test_email_shape_accepts_plausible_attempts(utterance):
    assert looks_like_field_shape("email", utterance) is True


@pytest.mark.parametrize("utterance", ["Manos", "yes that's right", "07577670101"])
def test_email_shape_rejects_non_email_looking_utterances(utterance):
    assert looks_like_field_shape("email", utterance) is False


@pytest.mark.parametrize("utterance", ["07577670101", "555-123-4567", "it's 555 1234"])
def test_phone_shape_accepts_digit_bearing_utterances(utterance):
    assert looks_like_field_shape("phone", utterance) is True


def test_phone_shape_rejects_utterances_with_no_digits():
    assert looks_like_field_shape("phone", "yes that's correct") is False


def test_name_field_has_no_shape_gate():
    assert looks_like_field_shape("name", "anything at all") is True
