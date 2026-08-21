"""Canned demo calls for testing the eval agent / admin panel without a live
mic session — docs/phases/phase-6a-observability.md (base 3-row version).
6b extends this with 5 more error-class-exhibiting rows (8 total); 6c further
extends it with 2 pre-populated BD annotations.

Inserted via SQLiteCallRepository.upsert only (never raw SQL — CLAUDE.md rule
9) using hand-authored CallState-shaped dicts standing in for what a real
call would have produced; `upsert` is naturally idempotent (INSERT ... ON
CONFLICT DO UPDATE on call_id), so re-running this script is safe and does
not touch backend/db/schema.sql or the `slots` table — it assumes the schema
already exists (run `python backend/db/seed_slots.py` first on a fresh DB).
"""

from backend.config import settings
from backend.db.repositories.connection import connect
from backend.db.repositories.sqlite_calls import SQLiteCallRepository
from backend.supervisor.state import new_call_state


def _confirmed(value: str) -> dict:
    return {"value": value, "confidence": 0.95, "status": "confirmed", "attempts": 0, "validated": True}


def _turn(role: str, text: str, ts: str) -> dict:
    return {"role": role, "text": text, "ts": ts}


def _booked_call(call_id: str, practice_area: str, name: str, email: str, phone: str, preferred_time: str) -> dict:
    state = new_call_state(call_id)
    state["stage"] = "ended"
    state["practice_area"] = practice_area
    state["booking_confirmed"] = True
    state["caller_profile"]["name"] = _confirmed(name)
    state["caller_profile"]["email"] = _confirmed(email)
    state["caller_profile"]["phone"] = _confirmed(phone)
    state["caller_profile"]["preferred_time"] = _confirmed(preferred_time)
    state["transcript"] = [
        _turn("caller", f"I need some help with a {practice_area} matter.", "2026-01-05T09:00:00+00:00"),
        _turn("agent", f"Got it — this falls under {practice_area} law. Could you tell me your name?", "2026-01-05T09:00:02+00:00"),
        _turn("caller", name, "2026-01-05T09:00:10+00:00"),
        _turn("agent", "And your email address?", "2026-01-05T09:00:12+00:00"),
        _turn("caller", email, "2026-01-05T09:00:20+00:00"),
        _turn("agent", f"Did you say {email}?", "2026-01-05T09:00:22+00:00"),
        _turn("caller", "Yes, that's right.", "2026-01-05T09:00:25+00:00"),
        _turn("agent", "And your phone number?", "2026-01-05T09:00:27+00:00"),
        _turn("caller", phone, "2026-01-05T09:00:35+00:00"),
        _turn("agent", f"Did you say {phone}?", "2026-01-05T09:00:37+00:00"),
        _turn("caller", "Yes.", "2026-01-05T09:00:40+00:00"),
        _turn("agent", "What time works best for you to come in?", "2026-01-05T09:00:42+00:00"),
        _turn("caller", preferred_time, "2026-01-05T09:00:50+00:00"),
        _turn("agent", f"You're booked for {preferred_time}. See you then.", "2026-01-05T09:00:55+00:00"),
    ]
    return state


def _escalated_call(call_id: str, practice_area, escalation_reason: str, name: str) -> dict:
    state = new_call_state(call_id)
    state["stage"] = "ended"
    state["practice_area"] = practice_area
    state["escalation_reason"] = escalation_reason
    if name:
        state["caller_profile"]["name"] = _confirmed(name)
    state["transcript"] = [
        _turn("caller", "I've got a legal question but I'm honestly not sure who to ask.", "2026-01-06T14:00:00+00:00"),
        _turn("agent", "Could you tell me a bit more — is this to do with your job, your home, or immigration?", "2026-01-06T14:00:03+00:00"),
        _turn("caller", "It's kind of all three, it's complicated.", "2026-01-06T14:00:15+00:00"),
        _turn("agent", "I'm having trouble telling which area this falls under, so let me get you to someone who can help directly.", "2026-01-06T14:00:18+00:00"),
    ]
    return state


def demo_states() -> list[dict]:
    return [
        _booked_call("demo-booked-1", "employment", "Priya Nair", "priya.nair@example.com", "5551234567", "Thursday 2pm"),
        _booked_call("demo-booked-2", "tenancy", "Sam O'Connell", "sam.oconnell@example.com", "5559876543", "Friday 10am"),
        _escalated_call("demo-escalated-1", "immigration", "unable_to_classify", "Jordan Lee"),
    ]


def seed(conn) -> None:
    repo = SQLiteCallRepository(conn)
    for state in demo_states():
        repo.upsert(state)


if __name__ == "__main__":
    connection = connect(settings.db_path)
    seed(connection)
    print(f"Seeded {len(demo_states())} demo calls into {settings.db_path}")
