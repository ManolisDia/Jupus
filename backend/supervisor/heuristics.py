"""Deterministic (non-Claude) heuristics used by the dispatcher."""

import re

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


# Phase 8 (case research) — used by node_research_gather to catch a
# leftover reaction to the PREVIOUS question (e.g. confirming a field
# right before the capture->research handoff) getting misattributed to
# the NEW research intro question, since that handoff has no extra
# round-trip for the caller to "catch up" on a fresh question — confirmed
# live: a caller's trailing "yep, that's correct" (still reacting to the
# phone confirm-back) landed as node_research_gather's utterance and got
# treated as their landlord-situation description, burning the one shot
# at a real citation on content that was never actually about it. Unlike
# TANGENT_PREFIXES (which flags utterances that look like a QUESTION or
# aside), this flags utterances that are ENTIRELY made of acknowledgment/
# affirmation words and nothing else — a real answer to "tell me what
# happened" almost never consists purely of these, even a short one
# ("he just showed up" already has content words outside this set).
_BARE_AFFIRMATION_TOKENS = frozenset(
    "yes yeah yep yup correct right ok okay sure no nope nah thats that "
    "is it was true affirmative indeed exactly".split()
)
_WORD_RE = re.compile(r"[a-z]+")


def looks_like_bare_affirmation(utterance: str) -> bool:
    stripped = utterance.strip().lower().replace("'", "")
    if not stripped:
        return True  # empty/silence carries no substantive content either
    tokens = _WORD_RE.findall(stripped)
    if not tokens:
        return True
    return all(token in _BARE_AFFIRMATION_TOKENS for token in tokens)


# Phase 14 (filler/interrupt handling) — Decision 3 needs to tell a caller
# talking OVER the filler because they have something real to say ("actually
# it's Alesh with an H") from one merely acknowledging that they heard it
# ("mhm", "okay"). The first must reach the graph as this turn's real input;
# the second must be dropped, or every backchannel would reroute the turn.
#
# A deliberate sibling of _BARE_AFFIRMATION_TOKENS rather than an extension of
# it: that set is load-bearing for node_research_gather's "did the caller
# actually answer the research question" check, and widening it there would
# start swallowing real (if terse) answers to "tell me what happened". This
# set can be more generous precisely because its consequence is narrower —
# dropping a backchannel that interrupted a one-second filler, not skipping a
# call's one shot at a statute citation.
#
# Same closed-token-set mechanism as above, and for the same reason: no LLM
# call. Routing this through a model would reintroduce exactly the round trip
# the filler exists to hide.
# Built explicitly rather than by unioning _BARE_AFFIRMATION_TOKENS, so that
# set's negations are deliberately EXCLUDED here. "no" / "nope" / "nah" spoken
# over a filler is almost always a correction the caller needs heard ("no, wait
# —"), not a backchannel; real backchannels are affirmative by nature. Getting
# that wrong would silently swallow a decline on exactly the booking-confirm
# turn where a decline matters most.
_ACKNOWLEDGMENT_TOKENS = frozenset(
    "yes yeah yep yup ya correct right ok okay okays sure thing true indeed "
    "exactly mhm mm mmm hm hmm uh huh uhhuh mhmm aha ah oh gotcha got it "
    "alright cool fine great perfect good nice thanks thank you please "
    "sorry go ahead sounds well fair enough understood makes sense that is "
    "thats was".split()
)


def looks_like_acknowledgment(utterance: str) -> bool:
    """True when an utterance carries no new content — a backchannel, not a
    substantive interruption. Used only to decide whether a caller talking over
    a filler phrase should be handed to the graph as real input (Decision 3).

    Errs toward False (substantive) on anything with a content word in it: a
    dropped real correction is a visible failure the caller has to repeat,
    while a backchannel wrongly treated as substantive just costs one harmless
    extra turn.
    """
    stripped = utterance.strip().lower().replace("'", "")
    if not stripped:
        return True
    tokens = _WORD_RE.findall(stripped)
    if not tokens:
        return True
    return all(token in _ACKNOWLEDGMENT_TOKENS for token in tokens)
