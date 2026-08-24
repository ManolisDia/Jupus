"""Deterministic (non-Claude) heuristics used by the dispatcher."""

EXPLICIT_REQUEST_PHRASES = [
    "speak to a person", "talk to a human", "real person",
    "representative", "talk to someone", "human agent",
    "speak with someone", "get me a person", "transfer me",
    "speak to someone else", "human being",
]


def is_explicit_human_request(utterance: str) -> bool:
    lowered = utterance.lower()
    return any(phrase in lowered for phrase in EXPLICIT_REQUEST_PHRASES)


# Phase 7 (optimistic capture) — used by node_capture_fast to decide whether
# it's safe to guess an utterance answers the currently-asked field, or
# whether to fall back to the real synchronous path. Deliberately
# conservative per that phase doc's Decision 1: a false positive here just
# costs a redundant fallback to today's already-correct behavior; a false
# negative produces a visibly wrong "next question" a beat later. Cheap,
# deterministic substring/prefix checks only — no LLM call on this path.
TANGENT_PREFIXES = [
    "what", "why", "how", "when", "where", "who", "which",
    "wait", "actually", "sorry", "hold on", "hang on", "um", "uh",
    "can you", "could you", "do you", "does it", "is it", "will it",
]


def looks_like_tangent(utterance: str) -> bool:
    lowered = utterance.strip().lower()
    if not lowered:
        return True  # empty/silence is never a plausible direct answer
    if lowered.endswith("?"):
        return True
    return any(lowered.startswith(prefix) for prefix in TANGENT_PREFIXES)


def looks_like_field_shape(field_name: str, utterance: str) -> bool:
    # A cheap RAW-UTTERANCE plausibility check, deliberately looser than
    # tools.validate_email/validate_phone — those validate an already
    # Claude-normalized value ("user@domain.com"), not natural speech
    # ("manos at gmail dot com"), so they'd reject almost every genuine
    # spoken email/phone if run against the raw utterance. This only asks
    # "is it safe to guess this utterance is even attempting the field",
    # not "is it a valid value" — an invalid-but-plausible attempt still
    # advances optimistically and gets caught for real by the background
    # verification, same as a valid one.
    lowered = utterance.lower()
    if field_name == "email":
        return "@" in utterance or " at " in lowered or " dot " in lowered
    if field_name == "phone":
        return any(ch.isdigit() for ch in utterance)
    return True  # name/preferred_time: no reliable shape signal, rely on looks_like_tangent alone


# Phase 8 (case research) — used by node_research_gather to decide whether
# the caller's answer to the research intro question is a genuine decline
# to elaborate, in which case the search is skipped entirely and the call
# goes straight to booking. Deliberately narrow substring match, same
# category as EXPLICIT_REQUEST_PHRASES above — no LLM call on this path.
RESEARCH_SKIP_PHRASES = [
    "let's just book", "lets just book", "just book me in", "can we just book",
    "rather just book", "rather not say", "rather not talk about it",
    "skip that", "no thanks", "not really", "prefer not to",
]


def looks_like_research_skip(utterance: str) -> bool:
    lowered = utterance.lower()
    return any(phrase in lowered for phrase in RESEARCH_SKIP_PHRASES)
