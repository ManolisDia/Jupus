"""Tool implementations for the LangGraph supervisor."""

import re
from datetime import date, datetime
from pathlib import Path

from backend.supervisor import prompts
from backend.supervisor.llm_utils import call_claude_json, call_claude_text
from backend.utils import now_iso

CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "area": {
            "type": "string",
            "enum": ["employment", "tenancy", "immigration", "multiple_areas", "unclear"],
        },
        "confidence": {"type": "number"},
    },
    "required": ["area", "confidence"],
    "additionalProperties": False,
}

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "value": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["value", "confidence"],
    "additionalProperties": False,
}

CONFIRM_ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "confirmed": {"type": "boolean"},
        "corrected_value": {"type": ["string", "null"]},
        "needs_clarification": {"type": "boolean"},
    },
    "required": ["confirmed", "corrected_value", "needs_clarification"],
    "additionalProperties": False,
}

EXTRACT_DATETIME_SCHEMA = {
    "type": "object",
    "properties": {
        "date": {"type": "string"},
        "window": {"type": "string", "enum": ["morning", "afternoon", "any"]},
        "time": {"type": ["string", "null"]},
        "confidence": {"type": "number"},
    },
    "required": ["date", "window", "time", "confidence"],
    "additionalProperties": False,
}

CONFIRM_BOOKING_SCHEMA = {
    "type": "object",
    "properties": {
        "accepted": {"type": "boolean"},
        "needs_clarification": {"type": "boolean"},
    },
    "required": ["accepted", "needs_clarification"],
    "additionalProperties": False,
}


def _format_transcript(transcript: list[dict]) -> str:
    return "\n".join(f"{turn['role'].upper()}: {turn['text']}" for turn in transcript)


def classify_practice_area(transcript: list[dict]) -> dict:
    return call_claude_json(
        system=prompts.CLASSIFY_PRACTICE_AREA_PROMPT,
        user_content=_format_transcript(transcript),
        json_schema=CLASSIFY_SCHEMA,
    )


def extract_field(utterance: str, field_name: str) -> dict:
    return call_claude_json(
        system=prompts.EXTRACT_FIELD_PROMPT.format(field_name=field_name),
        user_content=utterance,
        json_schema=EXTRACT_SCHEMA,
    )


def generate_confirm_back(field_name: str, candidate_value: str) -> str:
    return call_claude_text(
        system=prompts.CONFIRM_BACK_PROMPT.format(field_name=field_name, candidate_value=candidate_value),
        user_content="Generate the confirm-back question now.",
    )


def confirm_field_answer(utterance: str, field_name: str, candidate_value: str) -> dict:
    return call_claude_json(
        system=prompts.CONFIRM_FIELD_ANSWER_PROMPT.format(field_name=field_name, candidate_value=candidate_value),
        user_content=utterance,
        json_schema=CONFIRM_ANSWER_SCHEMA,
    )


def generate_call_summary(state) -> str:
    return call_claude_text(
        system=prompts.GENERATE_CALL_SUMMARY_PROMPT,
        user_content=(
            f"Escalation reason: {state.get('escalation_reason')}\n\n"
            f"Transcript:\n{_format_transcript(state.get('transcript', []))}"
        ),
    )


HANDOFFS_DIR = Path(__file__).resolve().parents[2] / "docs" / "handoffs"


def _caller_field_line(profile: dict, field_name: str, label: str) -> str:
    field = profile[field_name]
    value = field["value"] if field["status"] == "confirmed" else None
    return f"- {label}: {value if value else 'not captured'}"


def _handoff_note_text(call_id: str, state, summary: str) -> str:
    profile = state["caller_profile"]
    lines = [
        f"# Escalation — {call_id}",
        f"Time: {now_iso()}",
        f"Practice area: {state.get('practice_area') or 'not yet determined'}",
        f"Reason: {state.get('escalation_reason')}",
        "",
        "## Caller details collected",
        _caller_field_line(profile, "name", "Name"),
        _caller_field_line(profile, "email", "Email"),
        _caller_field_line(profile, "phone", "Phone"),
        "",
        "## Summary",
        summary,
        "",
    ]
    return "\n".join(lines)


def write_handoff_note(call_id: str, state, summary: str) -> Path:
    HANDOFFS_DIR.mkdir(parents=True, exist_ok=True)
    path = HANDOFFS_DIR / f"{call_id}.md"
    path.write_text(_handoff_note_text(call_id, state, summary), encoding="utf-8")
    return path


def write_minimal_handoff_note(call_id: str, state, reason: str) -> Path:
    return write_handoff_note(call_id, state, summary=reason)


# Domain half is stricter than the local part: rejects a leading/trailing/
# repeated dot in the domain (e.g. "x@...com"), which a naive
# "[^@\s]+\.[^@\s]+" would wrongly accept since dots aren't excluded from
# that character class. Still a documented simplification, not full RFC 5322.
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")


def validate_email(email: str) -> bool:
    return bool(EMAIL_PATTERN.match(email))


def validate_phone(phone: str) -> bool:
    digits = re.sub(r"\D", "", phone)
    return 7 <= len(digits) <= 15


def extract_datetime(utterance: str, today: date) -> dict:
    return call_claude_json(
        system=prompts.EXTRACT_DATETIME_PROMPT.format(today=today.isoformat()),
        user_content=utterance,
        json_schema=EXTRACT_DATETIME_SCHEMA,
    )


def _format_slot_time(start_time: str) -> str:
    # avoid %-d/%-I (glibc-only, not portable to Windows) for the
    # no-leading-zero day/hour formatting
    dt = datetime.fromisoformat(start_time)
    time_str = dt.strftime("%I:%M%p").lstrip("0").replace(":00", "")
    return f"{dt.strftime('%A %B')} {dt.day} at {time_str}"


def _format_time_of_day(time_str: str) -> str:
    dt = datetime.strptime(time_str, "%H:%M")
    return dt.strftime("%I:%M%p").lstrip("0").replace(":00", "")


def generate_confirmation_summary(
    caller_profile: dict, slot: dict, area: str, unavailable_requested_time: str = None
) -> str:
    details = (
        f"Name: {caller_profile['name']['value']}\n"
        f"Email: {caller_profile['email']['value']}\n"
        f"Proposed time: {_format_slot_time(slot['start_time'])}\n"
        f"Practice area: {area}"
    )
    if unavailable_requested_time:
        details += (
            f"\nNote: the caller specifically asked for {_format_time_of_day(unavailable_requested_time)}, "
            "which is not available — explicitly say that time is taken before reading back the proposed time above."
        )
    return call_claude_text(
        system=prompts.CONFIRMATION_SUMMARY_PROMPT,
        user_content=details,
    )


def confirm_booking_answer(utterance: str) -> dict:
    return call_claude_json(
        system=prompts.CONFIRM_BOOKING_ANSWER_PROMPT,
        user_content=utterance,
        json_schema=CONFIRM_BOOKING_SCHEMA,
    )
