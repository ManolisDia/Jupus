"""Pre-render the Phase 14 canned filler phrases to WAV, once, offline.

    python scripts/generate_filler_audio.py

Why pre-rendered rather than synthesized at call time — the two reasons, in
order of importance:

1. LiveKit's `session.say()` REFUSES text-only input when the LLM is a
   RealtimeModel: the OpenAI Realtime plugin reports
   `RealtimeCapabilities.supports_say = False`, and agent_activity.say() raises
   RuntimeError unless a TTS plugin is attached or `audio=` is supplied. Passing
   pre-rendered audio takes the `audio=` branch, so OpenAI Realtime stays the
   session's only speech model — attaching a TTS plugin purely to unlock say()
   would have pulled a second voice into the call for no benefit.

2. Decision 1 (docs/phases/phase-14-livekit-transport.md) rejects a
   model-generated filler because even a fast model costs a 200-400ms round
   trip, "which defeats the purpose of a filler meant to start speaking
   immediately." Pre-rendering takes that to a literal zero: at call time this
   is a local file read, not a network call.

Output is raw PCM wrapped in a WAV container — signed 16-bit mono at 24kHz,
which is exactly what OpenAI's `response_format="pcm"` returns. LiveKit's own
`utils.audio.audio_frames_from_file` decodes and resamples to 48kHz at play
time, so no custom streaming code is needed on the agent side.

The WAVs are committed. Re-run this only when FILLER_PHRASES, VOICE, or
VOICE_SPEED changes in backend/supervisor/fillers.py — a mismatch between the
call's live voice and the filler's baked-in voice is audible.

Costs a few cents of OpenAI TTS per full run (three short phrases).
"""

import wave
from pathlib import Path

from openai import OpenAI

from backend.config import settings
from backend.supervisor.fillers import FILLER_PHRASES, VOICE, VOICE_SPEED

# gpt-4o-mini-tts is the only TTS model exposing the Realtime voices (marin,
# cedar) — tts-1/tts-1-hd reject them, which would force a voice mismatch.
TTS_MODEL = "gpt-4o-mini-tts"
SAMPLE_RATE = 24_000
OUTPUT_DIR = Path(__file__).resolve().parents[1] / "backend" / "transport" / "filler_audio"


def generate() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    client = OpenAI(api_key=settings.openai_api_key)

    for key, phrase in FILLER_PHRASES.items():
        response = client.audio.speech.create(
            model=TTS_MODEL,
            voice=VOICE,
            speed=VOICE_SPEED,
            input=phrase,
            response_format="pcm",
        )
        pcm = response.content
        path = OUTPUT_DIR / f"{key}.wav"
        with wave.open(str(path), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(SAMPLE_RATE)
            out.writeframes(pcm)
        seconds = len(pcm) / 2 / SAMPLE_RATE
        print(f"{key:18} {seconds:5.2f}s  {phrase!r} -> {path.name}")


if __name__ == "__main__":
    generate()
