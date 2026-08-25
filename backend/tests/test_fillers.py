"""Phase 14 — filler selection is a deterministic read of the pre-turn state.

Decision 2 scopes filler to exactly three call sites. These tests pin both
halves of that: the three turns that DO get one, and — just as importantly —
that nothing else does. A filler leaking onto a turn that already has a natural
next question would talk over Phase 7/8's existing latency-hiding.
"""

import wave
from pathlib import Path

import pytest

from backend.supervisor.fillers import FILLER_PHRASES, filler_for_state
from backend.supervisor.state import new_call_state

FILLER_AUDIO_DIR = Path(__file__).resolve().parents[1] / "transport" / "filler_audio"


def _state(stage: str, **overrides):
    state = new_call_state("call-1")
    state["stage"] = stage
    state.update(overrides)
    return state


# --- the three turns that get a filler -------------------------------------


def test_confirm_field_turn_gets_filler():
    state = _state("capture", capture_phase="confirm")
    state["caller_profile"]["email"]["status"] = "pending_confirm"

    assert filler_for_state(state) == "confirm_field"


def test_confirm_booking_turn_gets_filler():
    assert filler_for_state(_state("booking", proposed_slot_id=7)) == "confirm_booking"


def test_propose_slot_turn_gets_filler():
    # No slot proposed yet: this utterance carries the requested time, and the
    # turn goes on to generate_confirmation_summary.
    assert filler_for_state(_state("booking", proposed_slot_id=None)) == "propose_slot"


# --- everything else must NOT ----------------------------------------------


@pytest.mark.parametrize("stage", ["greeting", "routing", "research", "escalation", "ended"])
def test_other_stages_get_no_filler(stage):
    assert filler_for_state(_state(stage)) is None


def test_fast_capture_pass_gets_no_filler():
    # Phase 7's fast pass is zero-LLM by design — there is nothing to mask, and
    # its _fallback_to_real_capture exception isn't predictable from pre-turn
    # state (see filler_for_state's docstring).
    state = _state("capture", capture_phase="fast")
    state["caller_profile"]["email"]["status"] = "pending_confirm"

    assert filler_for_state(state) is None


def test_confirm_phase_without_pending_field_gets_no_filler():
    assert filler_for_state(_state("capture", capture_phase="confirm")) is None


def test_offered_alternatives_turn_gets_no_filler():
    # Resolves through select_offered_slot and books directly — not one of
    # Decision 2's three.
    state = _state("booking", proposed_slot_id=None, offered_slots=[{"id": 1}, {"id": 2}])

    assert filler_for_state(state) is None


def test_research_turn_gets_no_filler():
    # Phase 8 already hides this latency behind a real follow-up question; a
    # filler here would collide with it.
    assert filler_for_state(_state("research", research_phase="gather")) is None


# --- the phrases and their pre-rendered audio must stay in lockstep ---------


def _audio_paths():
    for key, phrases in FILLER_PHRASES.items():
        for index in range(len(phrases)):
            yield key, index, FILLER_AUDIO_DIR / f"{key}_{index}.wav"


def test_every_phrase_has_committed_audio():
    # A phrase added without regenerating the WAVs would raise at call time,
    # inside a live call, on the one path meant to prevent dead air.
    for key, index, path in _audio_paths():
        assert path.exists(), f"missing filler audio for {key!r} line {index}"


def test_every_site_has_a_long_wait_line():
    # The second line is what keeps a slow turn from reproducing the Phase 2
    # finding recorded in docs/DECISIONS.md — a spoken promise followed by dead
    # air. A site with only one line would silently regress to exactly that.
    for key, phrases in FILLER_PHRASES.items():
        assert len(phrases) >= 2, f"{key!r} has no follow-up line for a long wait"


def test_filler_audio_starts_immediately():
    """No silent lead-in on any clip.

    The old format test could not catch this and its comment claimed it
    guarded "the dead air this whole phase exists to remove". It didn't: an
    earlier confirm_field_0.wav opened with 890ms of digital silence and passed
    it. That silence is invisible to the trace-based measurement (filler_spoken
    is recorded when say() is called, not when sound arrives) and entirely
    audible to the caller — it made the reported ~400ms time-to-first-audio
    roughly a third of the truth at the most frequently used filler site.
    """
    import numpy as np

    for key, index, path in _audio_paths():
        with wave.open(str(path), "rb") as wav:
            samples = np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16)
            rate = wav.getframerate()
        loud = np.flatnonzero(np.abs(samples) > 200)
        assert len(loud) > 0, f"{key}_{index} is silent throughout"
        lead_in = loud[0] / rate
        assert lead_in < 0.10, f"{key}_{index} has {lead_in * 1000:.0f}ms of dead air at the front"


def test_filler_audio_is_the_format_the_generator_writes():
    # LiveKit's audio_frames_from_file decodes and resamples these to 48kHz on
    # the way out, so this isn't a hard runtime requirement — it's a guard that
    # the committed WAVs actually came from scripts/generate_filler_audio.py
    # (mono s16 @ 24kHz, straight from OpenAI's response_format="pcm") rather
    # than being hand-dropped in some other format that happens to decode.
    for _key, _index, path in _audio_paths():
        with wave.open(str(path), "rb") as wav:
            assert wav.getnchannels() == 1
            assert wav.getsampwidth() == 2
            assert wav.getframerate() == 24_000
            # A silent/truncated render would "play" instantly and leave the
            # dead air this whole phase exists to remove.
            assert wav.getnframes() > 24_000 * 0.3


def test_every_selectable_key_is_a_known_phrase():
    state_builders = [
        _state("booking", proposed_slot_id=7),
        _state("booking", proposed_slot_id=None),
    ]
    confirm = _state("capture", capture_phase="confirm")
    confirm["caller_profile"]["name"]["status"] = "pending_confirm"
    state_builders.append(confirm)

    for state in state_builders:
        assert filler_for_state(state) in FILLER_PHRASES
