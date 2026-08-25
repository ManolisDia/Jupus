"""Drive a REAL LiveKit call end to end, with no human at a microphone.

    # backend must be running with JUPUS_TRANSPORT=livekit
    python eval/livekit_live_call.py --scenario S1
    python eval/livekit_live_call.py --all --label livekit-live

Every other test in this repo stops at the transport boundary:
backend/tests/test_scenarios.py calls dispatcher functions directly, and
eval/replay_scenarios.py drives the real graph but still bypasses the
transport entirely (its own docstring says so). Neither can catch a bug that
lives in the transport — which is exactly the class of bug this codebase has
already shipped once (the ask_supervisor/ASR race, commit 43757bc).

This closes that gap. It joins the LiveKit room as the caller, publishes
synthesized speech, and lets the whole real stack run: OpenAI Realtime
transcribes it, decides to call ask_supervisor, the agent marshals that onto
the FastAPI loop, the LangGraph supervisor answers, and the reply comes back as
audio. The assertions are then made against the same CallState/DB rows the
offline scenario tests check, so "passes live" and "passes mocked" mean
comparable things.

It is NOT a substitute for a human listening to a call. It cannot judge whether
the agent sounded natural, whether a filler landed in the right place, or
whether barge-in felt right — the phase DoD still requires a real human call
for those. What it does give is a repeatable, unattended check that the
transport carries a full conversation correctly.

## Known limitation: scripted utterances can desynchronize

The scenario scripts in `eval/replay_scenarios.py` were written for MOCKED
extraction, where each turn's outcome is fixed in advance and the Nth scripted
utterance is guaranteed to be answering the question the agent actually asked.
Nothing guarantees that live.

Observed on the first full run (2026-08-25): S2's scripted phone `"5551234567"`
came back from the TTS→ASR round trip as `"555-1234-67"` with low enough
extraction confidence to trigger the deterministic "read it out one digit at a
time" re-ask. That re-ask is *correct* behaviour — but the next scripted line
(`"Yes, that's right."`) was written to answer the EMAIL confirm-back, so it
landed on a phone re-ask that isn't a yes/no question. From that point the rest
of the script is answering the wrong questions, and S7b duly escalated with
`capture_failed` after three failed attempts. The supervisor did exactly the
right thing; the script simply stopped being valid input.

So read a live run as: an outcome matching `docs/scenarios.md` is strong
evidence, and an outcome that doesn't is a prompt to read the transcript, not
an automatic failure. Making this fully reliable needs an ADAPTIVE caller that
picks its next line from what the agent actually just asked, rather than
replaying a fixed list — worth building if live scenario runs become routine.

Costs real OpenAI credit per run (TTS for the caller + a live Realtime session)
and real Anthropic credit (the supervisor turns), so it is a deliberate,
explicitly-invoked script rather than part of `pytest`.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from dataclasses import dataclass, field

import numpy as np
from livekit import api, rtc
from openai import OpenAI

from backend.config import settings
from eval.replay_scenarios import SCENARIOS

# The caller's synthetic voice. Deliberately NOT the agent's (marin) so a
# transcript is never ambiguous about who said what.
CALLER_VOICE = "ash"
CALLER_TTS_MODEL = "gpt-4o-mini-tts"
SAMPLE_RATE = 24_000
FRAME_MS = 10

# How long the agent must stay quiet before the caller says the next line.
# The supervisor's own round trip can be seconds (Phase 11/13's measurements),
# so this has to be generous enough not to talk over a slow-but-working turn.
AGENT_SILENCE_SECONDS = 2.5
AGENT_REPLY_TIMEOUT_SECONDS = 45.0

# Mean absolute PCM amplitude above which a frame counts as speech rather than
# the comfort noise LiveKit sends continuously.
SPEECH_AMPLITUDE = 200


@dataclass
class AgentAudioMonitor:
    """Tracks whether the agent is currently speaking, from its audio track."""

    loud_frames: int = 0
    total_frames: int = 0
    _last_loud_at: float = field(default=0.0)

    def note(self, frame: rtc.AudioFrame, now: float) -> None:
        self.total_frames += 1
        samples = np.frombuffer(frame.data, dtype=np.int16)
        if len(samples) and np.abs(samples).mean() > SPEECH_AMPLITUDE:
            self.loud_frames += 1
            self._last_loud_at = now

    def quiet_for(self, now: float) -> float:
        if self._last_loud_at == 0.0:
            return 0.0
        return now - self._last_loud_at

    def has_spoken(self) -> bool:
        return self.loud_frames > 0


def _mint_token(room_name: str) -> str:
    return (
        api.AccessToken(settings.livekit_api_key, settings.livekit_api_secret)
        .with_identity("caller")
        .with_grants(
            api.VideoGrants(
                room_join=True, room=room_name, can_publish=True, can_subscribe=True
            )
        )
        .to_jwt()
    )


def _synthesize(client: OpenAI, text: str) -> bytes:
    return client.audio.speech.create(
        model=CALLER_TTS_MODEL, voice=CALLER_VOICE, input=text, response_format="pcm"
    ).content


async def _publish_pcm(source: rtc.AudioSource, pcm: bytes) -> None:
    samples = np.frombuffer(pcm, dtype=np.int16)
    per_frame = SAMPLE_RATE // (1000 // FRAME_MS)
    for start in range(0, len(samples), per_frame):
        block = samples[start : start + per_frame]
        if len(block) < per_frame:
            block = np.pad(block, (0, per_frame - len(block)))
        # capture_frame self-paces against real time, so this streams the
        # utterance at natural speed rather than dumping it at once — which
        # matters, because the Realtime model's turn detection is listening for
        # a real speech cadence.
        await source.capture_frame(
            rtc.AudioFrame(block.tobytes(), SAMPLE_RATE, 1, per_frame)
        )


async def run_scenario(scenario_id: str, utterances: list[str], *, verbose: bool = True) -> str:
    call_id = f"live-{scenario_id.lower()}-{uuid.uuid4().hex[:8]}"
    client = OpenAI(api_key=settings.openai_api_key)
    room = rtc.Room()
    monitor = AgentAudioMonitor()
    loop = asyncio.get_running_loop()

    @room.on("track_subscribed")
    def _on_track(track: rtc.Track, *_args) -> None:
        if track.kind != rtc.TrackKind.KIND_AUDIO:
            return

        async def drain() -> None:
            async for event in rtc.AudioStream(track):
                monitor.note(event.frame, loop.time())

        asyncio.create_task(drain())

    await room.connect(settings.livekit_url, _mint_token(call_id))
    source = rtc.AudioSource(SAMPLE_RATE, 1)
    await room.local_participant.publish_track(
        rtc.LocalAudioTrack.create_audio_track("caller-mic", source),
        # REQUIRED. RoomIO filters incoming tracks by publication source and
        # silently ignores anything that isn't a microphone, so a track
        # published without this reaches other participants fine but is never
        # forwarded to the agent's model — the agent just sits there. See
        # docs/fixes/ for the full write-up.
        rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE),
    )
    if verbose:
        print(f"[{scenario_id}] joined room {call_id!r} as caller")

    try:
        for index, utterance in enumerate(utterances):
            await _publish_pcm(source, b"\x00\x00" * int(SAMPLE_RATE * 0.5))
            if verbose:
                print(f"[{scenario_id}] caller: {utterance}")
            await _publish_pcm(source, _synthesize(client, utterance))

            spoke_before = monitor.loud_frames
            deadline = loop.time() + AGENT_REPLY_TIMEOUT_SECONDS
            while loop.time() < deadline:
                # Keep the mic open with silence while waiting — a caller's
                # line does not go dead between turns, and the model's turn
                # detection wants continuous audio.
                await _publish_pcm(source, b"\x00\x00" * int(SAMPLE_RATE * 0.2))
                replied = monitor.loud_frames > spoke_before
                if replied and monitor.quiet_for(loop.time()) > AGENT_SILENCE_SECONDS:
                    break
            else:
                if verbose:
                    print(f"[{scenario_id}] WARNING: no agent reply to turn {index}")

        await _publish_pcm(source, b"\x00\x00" * int(SAMPLE_RATE * 1.0))
    finally:
        await room.disconnect()

    if verbose:
        print(f"[{scenario_id}] done — agent spoke: {monitor.has_spoken()}")
    return call_id


def report(call_id: str) -> None:
    """Print what the supervisor actually recorded, read through the same
    repositories the admin panel uses (never raw SQL — CLAUDE.md rule #9)."""
    from backend.db.repositories import get_repositories

    repos = get_repositories(settings)
    row = repos.calls.get(call_id)
    if row is None:
        print(f"  {call_id}: NO CALL RECORD — the supervisor was never reached")
        return
    print(
        f"  {call_id}: area={row['practice_area']} outcome={row['outcome']} "
        f"escalation={row['escalation_reason']} slot={row['booking_slot_id']}"
    )
    events = repos.trace.get_trace(call_id)
    fillers = [e for e in events if e["event_type"] == "filler_spoken"]
    print(f"    {len(events)} trace events, {len(fillers)} filler(s) spoken")


async def main_async(args: argparse.Namespace) -> int:
    ids = list(SCENARIOS) if args.all else [args.scenario]
    unknown = [s for s in ids if s not in SCENARIOS]
    if unknown:
        print(f"unknown scenario(s): {unknown}; known: {list(SCENARIOS)}")
        return 2

    results = []
    for scenario_id in ids:
        call_id = await run_scenario(scenario_id, SCENARIOS[scenario_id])
        results.append((scenario_id, call_id))
        if args.label:
            # Same tagging replay_scenarios.py uses, so a live batch and a
            # replayed batch are directly comparable through
            # eval/compare_runs.py rather than being two incomparable formats.
            from backend.db.repositories import get_repositories

            get_repositories(settings).evals.tag_eval_run(
                call_id, args.label, scenario_id=scenario_id
            )

    print("\n=== results ===")
    for scenario_id, call_id in results:
        print(f"{scenario_id}:")
        report(call_id)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", default="S1", help="scenario id, e.g. S1")
    parser.add_argument("--all", action="store_true", help="run every canonical scenario")
    parser.add_argument("--label", default=None, help="eval run label to tag these calls with")
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
