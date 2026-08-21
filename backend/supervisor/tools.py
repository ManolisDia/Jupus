"""Tool implementations for the LangGraph supervisor."""

import re

from backend.supervisor import prompts
from backend.supervisor.llm_utils import call_claude_json, call_claude_text

CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "area": {"type": "string", "enum": ["employment", "tenancy", "immigration", "unclear"]},
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
    },
    "required": ["confirmed", "corrected_value"],
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
