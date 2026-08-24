"""Canned demo calls for testing the eval agent / admin panel without a live
mic session — docs/phases/phase-6a-observability.md (base 3-row version),
extended by docs/phases/phase-6b-error-taxonomy.md (5 more error-class-
exhibiting rows, 8 total) and docs/phases/phase-6c-benevolent-dictator.md
(2 pre-populated BD annotations, added via seed_annotations() below).

Inserted via SQLiteCallRepository.upsert only (never raw SQL — CLAUDE.md rule
9) using hand-authored CallState-shaped dicts standing in for what a real
call would have produced; `upsert` is naturally idempotent (INSERT ... ON
CONFLICT DO UPDATE on call_id), so re-running this script is safe and does
not touch backend/db/schema.sql or the `slots` table — it assumes the schema
already exists (run `python backend/db/seed_slots.py` first on a fresh DB).
"""

from backend.config import settings
from backend.db.repositories.connection import connect
from backend.db.repositories.sqlite_annotations import SQLiteAnnotationRepository
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


def _repetition_call() -> dict:
    # error class: repetition — agent asks for email a second time after it
    # was already extracted AND confirmed earlier in the same transcript.
    state = new_call_state("demo-repetition-1")
    state["stage"] = "ended"
    state["practice_area"] = "tenancy"
    state["booking_confirmed"] = True
    state["caller_profile"]["name"] = _confirmed("Morgan Reyes")
    state["caller_profile"]["email"] = _confirmed("morgan.reyes@example.com")
    state["caller_profile"]["phone"] = _confirmed("5551112222")
    state["caller_profile"]["preferred_time"] = _confirmed("Monday 3pm")
    state["transcript"] = [
        _turn("caller", "I need help with my landlord.", "2026-01-07T09:00:00+00:00"),
        _turn("agent", "What's your email address?", "2026-01-07T09:00:02+00:00"),
        _turn("caller", "morgan.reyes@example.com", "2026-01-07T09:00:05+00:00"),
        _turn("agent", "Did you say morgan.reyes@example.com?", "2026-01-07T09:00:07+00:00"),
        _turn("caller", "Yes.", "2026-01-07T09:00:09+00:00"),
        _turn("agent", "Thanks — and what's your email address?", "2026-01-07T09:00:11+00:00"),
        _turn("caller", "I... just gave you that.", "2026-01-07T09:00:14+00:00"),
        _turn("agent", "Sorry about that — you're all set, booked for Monday 3pm.", "2026-01-07T09:00:20+00:00"),
    ]
    return state


def _tool_failure_call() -> dict:
    # error class: tool_or_system_failure_surfaced — a generic fallback reply
    # fired mid-call (the LLMCallFailed graceful-degradation path).
    state = new_call_state("demo-tool-failure-1")
    state["stage"] = "ended"
    state["practice_area"] = "employment"
    state["booking_confirmed"] = True
    state["caller_profile"]["name"] = _confirmed("Dana Whitfield")
    state["caller_profile"]["email"] = _confirmed("dana.whitfield@example.com")
    state["caller_profile"]["phone"] = _confirmed("5553334444")
    state["caller_profile"]["preferred_time"] = _confirmed("Wednesday 11am")
    state["transcript"] = [
        _turn("caller", "I think I was unfairly dismissed.", "2026-01-08T09:00:00+00:00"),
        _turn("agent", "Could you tell me your name?", "2026-01-08T09:00:02+00:00"),
        _turn("caller", "Dana Whitfield", "2026-01-08T09:00:05+00:00"),
        _turn("agent", "Sorry, I'm having a little trouble — could you say that again?", "2026-01-08T09:00:07+00:00"),
        _turn("caller", "Dana Whitfield.", "2026-01-08T09:00:10+00:00"),
        _turn("agent", "Got it, thanks. You're booked for Wednesday 11am.", "2026-01-08T09:00:15+00:00"),
    ]
    return state


def _premature_escalation_call() -> dict:
    # error class: premature_escalation — escalated, but the request itself
    # was straightforward and clearly answerable/resolvable, no genuine
    # ambiguity or repeated failure preceding the escalation.
    state = new_call_state("demo-premature-escalation-1")
    state["stage"] = "ended"
    state["practice_area"] = "tenancy"
    state["escalation_reason"] = "unable_to_classify"
    state["caller_profile"]["name"] = _confirmed("Alex Kim")
    state["transcript"] = [
        _turn("caller", "My landlord hasn't returned my deposit after I moved out.", "2026-01-09T09:00:00+00:00"),
        _turn("agent", "I'm having trouble telling which area this falls under, so let me get you to someone who can help directly.", "2026-01-09T09:00:03+00:00"),
    ]
    return state


def _unconfirmed_action_call() -> dict:
    # error class: unconfirmed_action — booked, but no read-back/confirmation
    # turn appears anywhere between the last field capture and the booking.
    state = new_call_state("demo-unconfirmed-action-1")
    state["stage"] = "ended"
    state["practice_area"] = "immigration"
    state["booking_confirmed"] = True
    state["caller_profile"]["name"] = _confirmed("Taylor Brooks")
    state["caller_profile"]["email"] = _confirmed("taylor.brooks@example.com")
    state["caller_profile"]["phone"] = _confirmed("5555556666")
    state["caller_profile"]["preferred_time"] = _confirmed("Tuesday 9am")
    state["transcript"] = [
        _turn("caller", "I need help with a visa renewal.", "2026-01-10T09:00:00+00:00"),
        _turn("agent", "Could you tell me your name, email, phone, and preferred time?", "2026-01-10T09:00:02+00:00"),
        _turn("caller", "Taylor Brooks, taylor.brooks@example.com, 5555556666, Tuesday 9am.", "2026-01-10T09:00:10+00:00"),
        _turn("agent", "You're booked for Tuesday 9am.", "2026-01-10T09:00:12+00:00"),
    ]
    return state


def _invalid_email_call() -> dict:
    # deliberately bad row — booked, but caller_email is an invalid-format
    # value baked directly into the row, proving the judge can catch a
    # data-quality issue distinct from the conversational error classes above.
    state = new_call_state("demo-invalid-email-1")
    state["stage"] = "ended"
    state["practice_area"] = "employment"
    state["booking_confirmed"] = True
    state["caller_profile"]["name"] = _confirmed("Jamie Fox")
    state["caller_profile"]["email"] = _confirmed("jamie44")  # invalid: no @ / domain at all
    state["caller_profile"]["phone"] = _confirmed("5557778888")
    state["caller_profile"]["preferred_time"] = _confirmed("Thursday 4pm")
    state["transcript"] = [
        _turn("caller", "I have a question about my contract.", "2026-01-11T09:00:00+00:00"),
        _turn("agent", "You're booked for Thursday 4pm.", "2026-01-11T09:00:05+00:00"),
    ]
    return state


def demo_states() -> list[dict]:
    return [
        _booked_call("demo-booked-1", "employment", "Priya Nair", "priya.nair@example.com", "5551234567", "Thursday 2pm"),
        _booked_call("demo-booked-2", "tenancy", "Sam O'Connell", "sam.oconnell@example.com", "5559876543", "Friday 10am"),
        _escalated_call("demo-escalated-1", "immigration", "unable_to_classify", "Jordan Lee"),
        _repetition_call(),
        _tool_failure_call(),
        _premature_escalation_call(),
        _unconfirmed_action_call(),
        _invalid_email_call(),
    ]


def seed(conn) -> None:
    repo = SQLiteCallRepository(conn)
    for state in demo_states():
        repo.upsert(state)


def seed_annotations(conn) -> None:
    """Phase 6c — 2 pre-populated Benevolent Dictator annotations, so
    eval/calibrate_judge.py has something to compute against immediately
    without waiting on a real annotation session:
    - the repetition call, annotated in agreement with what a judge would
      plausibly flag (a true-positive case for calibration).
    - the unconfirmed_action call, annotated with the BD flagging a class a
      judge could plausibly miss (a deliberate disagreement case)."""
    annotations = SQLiteAnnotationRepository(conn)
    annotations.save_review(
        "demo-repetition-1",
        annotator=settings.annotator_name,
        error_class_ids=["repetition"],
        uncategorized_notes=[],
        overall_note="Agent asked for the email twice after it was already confirmed.",
        is_gold=True,
    )
    annotations.save_review(
        "demo-unconfirmed-action-1",
        annotator=settings.annotator_name,
        error_class_ids=["unconfirmed_action"],
        uncategorized_notes=[],
        overall_note="Booked immediately after a single multi-field utterance with no read-back at all.",
        is_gold=True,
    )


if __name__ == "__main__":
    connection = connect(settings.db_path)
    seed(connection)
    seed_annotations(connection)
    print(f"Seeded {len(demo_states())} demo calls (+ 2 BD annotations) into {settings.db_path}")
