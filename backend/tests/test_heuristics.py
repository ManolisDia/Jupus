import pytest

from backend.supervisor.heuristics import (
    is_explicit_human_request,
    looks_like_acknowledgment,
    looks_like_bare_affirmation,
    looks_like_field_shape,
    looks_like_tangent,
)


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


@pytest.mark.parametrize(
    "utterance",
    [
        "yes",
        "Yep, that's correct.",
        "That's correct.",
        "Yeah, that's right.",
        "Correct.",
        "no",
        "Nope.",
        "",
        "   ",
    ],
)
def test_bare_affirmation_detected(utterance):
    assert looks_like_bare_affirmation(utterance) is True


@pytest.mark.parametrize(
    "utterance",
    [
        "My landlord is trying to evict me tomorrow without giving me any notice.",
        "Yeah, he just showed up and told me to leave.",
        "No, nothing in writing, they just showed up and told me to leave.",
        "Correct, he never gave me a written notice.",
    ],
)
def test_substantive_answers_not_flagged_as_bare_affirmation(utterance):
    assert looks_like_bare_affirmation(utterance) is False


# ---------------------------------------------------------------------------
# Phase 14 (Decision 3) — telling a backchannel over the filler from a real
# interruption. Dropping a real correction is a visible failure the caller has
# to repeat; treating a backchannel as substantive costs one harmless turn.
# So these tests lean hard on the negations and near-misses.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "utterance",
    [
        "mhm",
        "mm hmm",
        "uh huh",
        "okay",
        "Okay!",
        "yeah okay",
        "got it",
        "alright",
        "sure",
        "sounds good",
        "fair enough",
        "yep, thanks",
        "",
        "...",
    ],
)
def test_acknowledgment_detected(utterance):
    assert looks_like_acknowledgment(utterance) is True


@pytest.mark.parametrize(
    "utterance",
    [
        "actually it's Alesh with an H",
        "can we do Friday instead",
        "sorry, can you repeat that",
        "my email is bob at gmail dot com",
        "wait, that's the wrong number",
    ],
)
def test_substantive_interruption_not_flagged_as_acknowledgment(utterance):
    assert looks_like_acknowledgment(utterance) is False


@pytest.mark.parametrize("utterance", ["no", "Nope.", "nah", "no wait", "no thanks"])
def test_negations_are_never_acknowledgments(utterance):
    assert looks_like_acknowledgment(utterance) is False


@pytest.mark.parametrize("utterance", ["no", "Nope.", "nah"])
def test_bare_negation_diverges_from_bare_affirmation(utterance):
    # Deliberately divergent: looks_like_bare_affirmation DOES treat a bare
    # "no" as contentless (right for "did the caller answer the research
    # question?"), but over a filler "no" is a correction the caller needs
    # heard — swallowing it on the booking-confirm turn would silently drop a
    # decline. Same word, opposite correct answer, because the consequence of
    # being wrong differs.
    assert looks_like_bare_affirmation(utterance) is True
    assert looks_like_acknowledgment(utterance) is False


def test_acknowledgment_set_does_not_widen_bare_affirmation():
    # looks_like_bare_affirmation gates node_research_gather's "did the caller
    # actually answer" check; widening it would start swallowing real, terse
    # answers to "tell me what happened". The two sets must stay independent.
    assert looks_like_bare_affirmation("mhm") is False
    assert looks_like_acknowledgment("mhm") is True
