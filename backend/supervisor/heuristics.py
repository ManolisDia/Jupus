"""Deterministic (non-Claude) heuristics used by the dispatcher."""

import re

# Shared by several checks below, so defined once up here rather than
# beside whichever one happens to come first in the file.
_WORD_RE = re.compile(r"[a-z]+")

# Checked on EVERY turn, before the graph runs (backend/dispatcher.py), and
# a miss is expensive: the caller has explicitly opted out of the automated
# intake and every further question is one they've already refused to
# answer. Confirmed live — "I just want to speak to a real human", said
# twice during booking, was answered both times with "what day and time
# would work for you?" until the caller hung up (docs/fixes/).
#
# These literals are kept exactly as they were, and matched first: they're
# proven, and several are bare nouns ("representative") that the structural
# patterns below deliberately don't reach.
EXPLICIT_REQUEST_PHRASES = [
    "speak to a person", "talk to a human", "real person",
    "representative", "talk to someone", "human agent",
    "speak with someone", "get me a person", "transfer me",
    "speak to someone else", "human being",
]

# The literals above carried "talk to a human" and "real person" but not
# "speak to a human" or "real human", which is how the live miss happened.
# The gap was never one missing phrase — a flat list has to enumerate a
# cross-product it can't finish: {speak,talk,chat} x {to,with} x
# {a, a real, an actual, a live, ...} x {human, person, someone, ...}.
# Matching the structure covers the whole product at once. Still plain
# regex: deterministic, no LLM on this path (CLAUDE.md rule 2).
#
# Deliberately EXCLUDES lawyer/solicitor/attorney/advisor-of-law: "I want to
# speak to a lawyer" is what every caller on this line wants, and it is what
# booking a consultation delivers. Escalating on it would hand every
# successful call to a human.
_HUMAN_NOUN = (
    r"(?:human(?:\s+being)?|person|people|someone|somebody"
    r"|agent|representative|operator|receptionist)"
)

_EXPLICIT_REQUEST_PATTERNS = [
    # "speak to a real human", "talk with someone", "chat to an actual person".
    # The bounded word gap absorbs qualifiers without letting the noun drift
    # into an unrelated later clause. Present tense only — "I spoke to
    # someone at the council last week" is a caller describing their
    # situation, not asking to be transferred.
    re.compile(rf"\b(?:speak|talk|chat)\s+(?:to|with)\s+(?:\w+\s+){{0,3}}{_HUMAN_NOUN}\b"),
    # "get me a person", "can I get a human", "give me a real person".
    # Requires the article, so it can't fire on "I need someone to help me
    # with my landlord" — a description of need, not a transfer request.
    re.compile(rf"\b(?:get|give)\s+(?:me\s+)?(?:a|an|the)\s+(?:\w+\s+){{0,2}}{_HUMAN_NOUN}\b"),
    # "transfer me", "connect me", "put me through". "put me" alone is NOT
    # enough — "put me down for Tuesday at ten" is a booking, not an exit.
    re.compile(r"\b(?:transfer|connect)\s+me\b"),
    re.compile(r"\bput\s+me\s+through\b"),
    # Bare qualifier + noun, no verb at all: "a real human", "an actual person".
    re.compile(rf"\b(?:real|actual|live)\s+{_HUMAN_NOUN}\b"),
]


def is_explicit_human_request(utterance: str) -> bool:
    lowered = utterance.lower()
    if any(phrase in lowered for phrase in EXPLICIT_REQUEST_PHRASES):
        return True
    return any(pattern.search(lowered) for pattern in _EXPLICIT_REQUEST_PATTERNS)


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


# A spoken name answer is short — "Manos", "it's Manos Diamantopoulos",
# "Manos, that's M A N O S". This ceiling is generous enough for the
# spelled-out form and still well under a sentence of narrative. Erring
# high is the safe direction: exceeding it only sends the utterance down
# the careful synchronous path, it never rejects anything outright.
MAX_SPOKEN_NAME_WORDS = 12


def _sounds_like_an_email(lowered: str) -> bool:
    # Spoken form ("manos at gmail dot com") or already-symbolic form.
    return "@" in lowered or (" at " in lowered and " dot " in lowered)


def _digit_count(text: str) -> int:
    return sum(1 for ch in text if ch.isdigit())


# A phone number read aloud carries no digit characters at all — "oh seven
# five seven seven six seven zero one zero one". Individually these words
# are ordinary English, so the test is the COUNT: a name answer essentially
# never contains five of them.
_NUMBER_WORDS = frozenset(
    "oh zero one two three four five six seven eight nine nought double triple".split()
)
_SPOKEN_DIGITS_FOR_A_NUMBER = 5


def _sounds_like_a_read_out_number(lowered: str) -> bool:
    return sum(1 for w in _WORD_RE.findall(lowered) if w in _NUMBER_WORDS) >= _SPOKEN_DIGITS_FOR_A_NUMBER


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
    if field_name == "name":
        # This used to return True unconditionally — "no reliable shape
        # signal, rely on looks_like_tangent alone". There is no reliable
        # POSITIVE signal for a name, which is what that reasoning was
        # about, but there are strong NEGATIVE ones, and without them the
        # fast path optimistically accepted literally any utterance as a
        # name. Live, a caller's late answer to the routing question
        # ("Yeah, it's about my home. He's basically trying to kick me out
        # with little notice.") was taken as their name, and a turn later
        # their spoken email address was too — see docs/fixes/.
        #
        # A false positive here is cheap by design (one fallback to the
        # careful synchronous path, which extracts correctly anyway), so
        # the bar for adding a negative signal is low.
        if (
            _sounds_like_an_email(lowered)
            or _digit_count(utterance) >= 7
            or _sounds_like_a_read_out_number(lowered)
        ):
            return False  # answering a different question, or a different field
        return len(lowered.split()) <= MAX_SPOKEN_NAME_WORDS
    return True  # preferred_time: no reliable shape signal, rely on looks_like_tangent alone


def looks_like_a_name(value: str) -> bool:
    """Does an EXTRACTED value plausibly hold a person\'s name? Unlike
    looks_like_field_shape above (raw speech, "is this worth guessing at"),
    this judges a value the model has already produced and is about to be
    written into caller_profile.

    Deliberately a negative check — it rejects things a name provably is
    not, rather than trying to define what one is. Names are far too
    varied across cultures for a positive pattern, and a false rejection
    costs a caller an extra re-ask, so the rules stay narrow: no "@", not a
    phone number, not a whole sentence.

    Lives here rather than beside tools.validate_email/validate_phone
    because apply_extraction — the single funnel every name extraction
    passes through, in all three call sites — is a pure function with no
    repos to trace a tools.py call with (CLAUDE.md rule 8). Putting it here
    keeps one check instead of three duplicated ones."""
    if not value or not value.strip():
        return False
    if "@" in value:
        return False  # an email address, however confidently it was heard
    if _digit_count(value) >= 7:
        return False  # a phone number
    return len(value.split()) <= MAX_SPOKEN_NAME_WORDS


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
