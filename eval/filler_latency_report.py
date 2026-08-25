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

For every turn where a filler played, it reports two durations taken from the
same trace, both anchored on the TURN's start (node_entered) rather than on any
one tool's:

  round trip     node_entered -> node_exited: the whole supervisor turn. This
                 is what the caller waits for and what Phase 13 moved. Phase 14
                 must leave it alone, and this is where that gets checked.

  time to audio  node_entered -> filler_spoken. What the caller actually
                 experiences as the wait before the line stops being silent.
                 Phase 14 moves this and nothing else.

Anchoring on the turn matters: a booking turn runs extract_datetime before it
reaches generate_confirmation_summary, so measuring from that tool's own start
would both understate the wait and miss the turn entirely (the filler fires
while the earlier tool is still running).

The gap between them is the dead air the phase removed. Turns with no filler
are counted but contribute no time-to-audio, since on those the caller is still
waiting the full round trip by design (Decision 2 scopes filler to three sites).
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from statistics import median

from backend.config import settings
from backend.db.repositories import get_repositories

# The tool each filler site is masking, so a turn's round trip is attributed to
# the call that actually made the caller wait.
FILLER_TOOLS = {
    "confirm_field": "confirm_field_answer",
    "confirm_booking": "confirm_booking_answer",
    "propose_slot": "generate_confirmation_summary",
}


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
    """One row per filler turn: which site, how long the caller waited to hear
    anything, and how long the whole supervisor turn actually took."""
    rows: list[dict] = []
    turn: dict | None = None

    for event in events:
        kind = event["event_type"]
        payload = _payload(event)

        if kind == "node_entered":
            turn = {"start": _ts(event["ts"]), "filler_at": None, "site": None}
        elif kind == "filler_spoken" and turn is not None and payload.get("step") == 0:
            # step 0 only: the second line fires seconds later by design and is
            # not when the caller first hears something.
            turn["filler_at"] = _ts(event["ts"])
            turn["site"] = payload.get("filler")
        elif kind == "node_exited" and turn is not None:
            if turn["filler_at"] is not None:
                rows.append(
                    {
                        "site": turn["site"],
                        "round_trip_ms": (_ts(event["ts"]) - turn["start"]).total_seconds() * 1000,
                        "time_to_audio_ms": (
                            turn["filler_at"] - turn["start"]
                        ).total_seconds()
                        * 1000,
                    }
                )
            turn = None

    return rows


def _p(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(int(pct / 100 * len(ordered)), len(ordered) - 1)
    return ordered[index]


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()

    repos = get_repositories(settings)
    call_ids = [row["call_id"] for row in repos.calls.list()]

    rows: list[dict] = []
    for call_id in call_ids:
        rows.extend(analyze_call(repos.trace.get_trace(call_id)))

    if not rows:
        print("No filler turns found. Run a live call over the LiveKit transport first:")
        print("  python eval/livekit_live_call.py --all")
        return 1

    header = f"{'filler site':<18}{'n':>4}{'round trip':>14}{'time to audio':>16}{'dead air removed':>19}"
    print(f"{len(rows)} filler turns across {len(call_ids)} calls\n")
    print(header)
    print("-" * len(header))

    for key in FILLER_TOOLS:
        site = [r for r in rows if r["site"] == key]
        if not site:
            continue
        trip = median(r["round_trip_ms"] for r in site)
        audio = median(r["time_to_audio_ms"] for r in site)
        print(f"{key:<18}{len(site):>4}{trip:>11.0f}ms{audio:>13.0f}ms{trip - audio:>16.0f}ms")

    trips = [r["round_trip_ms"] for r in rows]
    audios = [r["time_to_audio_ms"] for r in rows]
    print("-" * len(header))
    print(f"{'ALL (p50)':<18}{len(rows):>4}{median(trips):>11.0f}ms{median(audios):>13.0f}ms"
          f"{median(trips) - median(audios):>16.0f}ms")
    print(f"{'ALL (p95)':<18}{'':>4}{_p(trips, 95):>11.0f}ms{_p(audios, 95):>13.0f}ms"
          f"{_p(trips, 95) - _p(audios, 95):>16.0f}ms")
    print(
        "\nRound trip is the Anthropic call itself — Phase 13's number, which this phase "
        "does not touch.\nTime to audio is what the caller experiences, and is the only "
        "thing Phase 14 changes."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
