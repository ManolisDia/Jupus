"""Phase 14's headline measurement: perceived wait vs. actual round trip.

    python eval/filler_latency_report.py

The phase doc is emphatic that Phase 14 must not be sold as having reduced
anything Phase 13 reduced: LiveKit does not shrink the Anthropic API round
trip, because that number is fixed by the SDK call inside llm_utils.py and is
identical regardless of transport. This script exists so that claim is a
measurement rather than an assertion, and so both numbers are shown together.

(The phase doc states this by quoting the exact SDK method name. Paraphrased
here on purpose: scripts/check_architecture.py greps the staged diff for direct
Anthropic SDK usage outside llm_utils.py, and it is right not to try to tell a
real call from one quoted in a docstring.)

For every turn it reports two durations from the same trace, both anchored on
`ask_supervisor_received` — the moment the turn starts:

  round trip     -> `reply_ready`. The whole supervisor turn, every Claude call
                 in it. This is Phase 13's territory. Phase 14 must leave it
                 alone, and this is where that gets checked.

  time to audio  -> `first_audio`. When the caller actually HEARS something,
                 filler or reply. Phase 14 moves this and nothing else.

`first_audio` is a real playout signal (LiveKit's agent state entering
"speaking"), deliberately not the moment `say()` was called. An earlier version
of this script measured the latter and reported ~400ms for a turn whose filler
clip took 1.3s to make a sound, because 890ms of silence was baked into the
front of the WAV. Measuring scheduling instead of playout is exactly the kind of
flattering-but-wrong number this phase is not allowed to publish.

Turns are split by whether a filler played, because that split IS the
comparison: on a turn without one the caller waits the whole round trip in
silence, which is what the three filler sites looked like before this phase.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from statistics import median

from backend.config import settings
from backend.db.repositories import get_repositories

FILLER_SITES = ("confirm_field", "confirm_booking", "propose_slot")


def _ts(raw: str) -> datetime:
    return datetime.fromisoformat(raw)


def _payload(event: dict) -> dict:
    if "payload" in event:
        return event["payload"] or {}
    raw = event.get("payload_json")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def analyze_call(events: list[dict]) -> list[dict]:
    """One row per completed turn: time to audio, round trip, and whether a
    filler covered the gap. Turns missing either boundary are skipped rather
    than guessed at."""
    rows: list[dict] = []
    turns: dict[str, dict] = {}

    for event in events:
        kind = event["event_type"]
        payload = _payload(event)
        tool_call_id = payload.get("tool_call_id")

        if kind == "ask_supervisor_received":
            turns[tool_call_id] = {"start": _ts(event["ts"]), "site": None}
        elif kind == "filler_spoken":
            # filler_spoken carries no tool_call_id — it belongs to whichever
            # turn is open, and in practice only one is mid-filler at a time.
            for turn in turns.values():
                if turn["site"] is None:
                    turn["site"] = payload.get("filler")
        elif kind == "reply_ready" and tool_call_id in turns:
            turn = turns[tool_call_id]
            turn["round_trip_ms"] = (_ts(event["ts"]) - turn["start"]).total_seconds() * 1000
            turn["filler_played"] = bool(payload.get("filler_played"))
        elif kind == "first_audio" and tool_call_id in turns:
            turn = turns.pop(tool_call_id)
            if "round_trip_ms" not in turn:
                continue
            rows.append(
                {
                    "site": turn["site"],
                    "filler_played": turn["filler_played"],
                    "round_trip_ms": turn["round_trip_ms"],
                    "time_to_audio_ms": float(payload.get("ms_since_turn_start") or 0.0),
                }
            )

    return rows


def _p(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(int(pct / 100 * len(ordered)), len(ordered) - 1)]


def _row(label: str, subset: list[dict]) -> None:
    if not subset:
        return
    trip = median(r["round_trip_ms"] for r in subset)
    audio = median(r["time_to_audio_ms"] for r in subset)
    print(f"{label:<22}{len(subset):>4}{trip:>11.0f}ms{audio:>13.0f}ms{trip - audio:>15.0f}ms")


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()

    repos = get_repositories(settings)
    call_ids = [row["call_id"] for row in repos.calls.list()]

    rows: list[dict] = []
    for call_id in call_ids:
        rows.extend(analyze_call(repos.trace.get_trace(call_id)))

    if not rows:
        print("No completed turns found. Run a live call over the LiveKit transport first:")
        print("  python eval/livekit_live_call.py --all")
        return 1

    header = f"{'turns':<22}{'n':>4}{'round trip':>14}{'time to audio':>16}{'silence removed':>18}"
    print(f"{len(rows)} turns across {len(call_ids)} calls")
    print()
    print(header)
    print("-" * len(header))

    with_filler = [r for r in rows if r["filler_played"]]
    without = [r for r in rows if not r["filler_played"]]

    for site in FILLER_SITES:
        _row(f"  {site}", [r for r in with_filler if r["site"] == site])
    _row("WITH filler (p50)", with_filler)
    if with_filler:
        trips = [r["round_trip_ms"] for r in with_filler]
        audios = [r["time_to_audio_ms"] for r in with_filler]
        print(
            f"{'WITH filler (p95)':<22}{'':>4}{_p(trips, 95):>11.0f}ms"
            f"{_p(audios, 95):>13.0f}ms{_p(trips, 95) - _p(audios, 95):>15.0f}ms"
        )
    print("-" * len(header))
    _row("without filler", without)

    print()
    print("Round trip is the supervisor turn - Phase 13's territory, untouched here.")
    print("Time to audio is when the caller first HEARS something: a real playout")
    print("signal, not the moment say() was called.")
    print("The 'without filler' row is the same measurement on turns Decision 2")
    print("leaves alone. There the two columns converge - which is what the three")
    print("filler sites looked like before this phase.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
