"""Phase 14 — canned filler phrases for the turns with no natural next question.

Decision 1 (docs/phases/phase-14-livekit-transport.md): the filler is a fixed,
hand-written phrase per node, never model-generated — generating it would cost a
network round trip (200-400ms), which defeats the purpose of a line meant to
start playing immediately.

This module goes one step further than the phase doc assumed, for a reason
discovered at implementation time: LiveKit's `session.say()` refuses text-only
input when the LLM is a RealtimeModel, because the OpenAI Realtime plugin
reports `RealtimeCapabilities.supports_say = False` (verified in
livekit-agents 1.7.0, voice/agent_activity.py). The documented escape hatch is
`say(text=..., audio=...)`, which bypasses that guard entirely.

Since the phrases are a fixed closed set anyway, they're PRE-RENDERED to audio
once by scripts/generate_filler_audio.py and committed as WAVs. That turns
Decision 1's ~0ms goal into a literal 0ms: no synthesis at call time at all,
just a local file read. It also keeps OpenAI Realtime as the only speech model
(the phase doc's explicit non-goal about not swapping in a chained pipeline) —
adding a TTS plugin to the session purely to unlock say() would have been the
obvious alternative and would have pulled a second voice into the call.

The audio is rendered with the same voice and speed the Realtime session itself
uses (see VOICE / VOICE_SPEED below), so the filler is indistinguishable from
the rest of the agent's speech. Those two constants are the single source of
truth for both transports — if the call's voice or speed changes, the committed
filler WAVs must be regenerated to match, which is why they live here rather
than being duplicated at each call site.

Selection is a plain deterministic read of the PRE-TURN CallState — CLAUDE.md
rule #2, never an LLM deciding. Same house style as graph.py's
RESEARCH_FILLER_QUESTIONS / SPELL_OUT_REPLIES lookups.
"""

from typing import Optional

from backend.supervisor.state import CallState, FIELD_PRIORITY

# The call's voice identity, shared by the Realtime session and the pre-rendered
# filler audio so the two are indistinguishable. Regenerate the committed WAVs
# (scripts/generate_filler_audio.py) after changing either.
VOICE = "marin"
VOICE_SPEED = 1.1

# One phrase per target site, not the "one or two" Decision 1 allows: a single
# phrase per site keeps selection fully deterministic with no per-call counter
# to carry around, and these turns are rare enough within one call that a
# caller is unlikely to hear the same line twice.
#
# Keys are also the WAV basenames under backend/transport/filler_audio/.
FILLER_PHRASES: dict[str, str] = {
    "confirm_field": "Okay, one sec.",
    "confirm_booking": "Let me check that.",
    "propose_slot": "Let me get that booked.",
}


def filler_for_state(state: CallState) -> Optional[str]:
    """Which filler (if any) this turn should open with, keyed on the state as
    it stands BEFORE the graph runs. Returns a key into FILLER_PHRASES, or None
    for every other turn.

    Decision 2 scopes filler to the three calls with no natural next question
    to hide latency behind: confirm_field_answer, confirm_booking_answer, and
    generate_confirmation_summary. The phase doc justifies that set with "the
    reply *is* the answer," which is literally true only for
    generate_confirmation_summary — the other two are classifiers whose turns
    produce a reply from elsewhere (see docs/DECISIONS.md). The doc's intent
    holds regardless: on these turns the caller has just answered and is left
    waiting in silence with nothing else being asked.

    Because of that, the filler can't hang off the tool function — by the time
    a tool is invoked the turn is already underway. It's selected instead from
    the pre-turn state, which deterministically predicts which of those three
    call sites the turn will reach.

    Deliberately NOT covered: node_capture_fast's `_fallback_to_real_capture`
    path can also reach confirm_field_answer, but only as an exception to
    Phase 7's zero-LLM fast pass, and nothing in the pre-turn state predicts it
    (that's the whole point of the fast path — it decides mid-turn). Those
    turns keep today's behavior. Scoping to what's actually predictable is
    what keeps this a deterministic lookup rather than a guess.
    """
    stage = state.get("stage")

    if stage == "capture":
        # node_capture's confirm-back branch: a field is sitting at
        # pending_confirm and this utterance is the caller's yes/no/correction
        # about it (graph.py's node_capture -> confirm_field_answer).
        if state.get("capture_phase") == "confirm":
            profile = state.get("caller_profile") or {}
            if any(profile.get(f, {}).get("status") == "pending_confirm" for f in FIELD_PRIORITY):
                return "confirm_field"
        return None

    if stage == "booking":
        if state.get("offered_slots"):
            # The alternatives branch resolves through select_offered_slot and
            # books directly — not one of Decision 2's three, so untouched.
            return None
        if state.get("proposed_slot_id") is not None:
            # A slot is on the table; this utterance is the yes/no about it
            # (node_booking -> confirm_booking_answer).
            return "confirm_booking"
        # No proposal yet: this utterance carries the requested time, and the
        # turn goes look it up and generate a spoken summary
        # (node_booking -> _propose_slot -> generate_confirmation_summary).
        return "propose_slot"

    return None
