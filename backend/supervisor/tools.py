"""Tool implementations for the LangGraph supervisor."""

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from backend.supervisor import prompts
from backend.supervisor.knowledge import corpus as knowledge_corpus
from backend.supervisor.knowledge import search as knowledge_search
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

# Phase 13 (latency reduction) — merges extract_field + generate_confirm_back
# into one call/schema; see extract_and_confirm_field below.
EXTRACT_AND_CONFIRM_SCHEMA = {
    "type": "object",
    "properties": {
        "value": {"type": "string"},
        "confidence": {"type": "number"},
        "confirm_back_phrasing": {"type": "string"},
    },
    "required": ["value", "confidence", "confirm_back_phrasing"],
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

SELECT_OFFERED_SLOT_SCHEMA = {
    "type": "object",
    "properties": {
        "selected_index": {"type": ["integer", "null"]},
        "declined_all": {"type": "boolean"},
        "needs_clarification": {"type": "boolean"},
        # A caller who answers an offer with a time of their own is doing
        # neither of the other three things. Without somewhere to put that,
        # the model was forced to call a counter-proposal "clarification",
        # and node_booking answered a request for 3PM by re-reading the same
        # three morning slots back.
        "proposed_new_time": {"type": "boolean"},
    },
    "required": ["selected_index", "declined_all", "needs_clarification", "proposed_new_time"],
    "additionalProperties": False,
}


def _format_transcript(transcript: list[dict]) -> str:
    return "\n".join(f"{turn['role'].upper()}: {turn['text']}" for turn in transcript)


def _format_error_classes(error_classes: list[dict]) -> str:
    return "\n".join(f"- {c['id']} ({c['name']}): {c['description']}" for c in error_classes)


CLASSIFY_CALL_ERRORS_SCHEMA = {
    "type": "object",
    "properties": {
        "flags": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "error_class_id": {"type": "string"},
                    "confidence": {"type": "number"},
                    "evidence": {"type": "string"},
                },
                "required": ["error_class_id", "confidence", "evidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["flags"],
    "additionalProperties": False,
}

PROPOSE_TAXONOMY_UPDATES_SCHEMA = {
    "type": "object",
    "properties": {
        "suggestions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "suggestion_type": {
                        "type": "string",
                        "enum": ["new_class", "misclassification", "refine_existing"],
                    },
                    "call_id": {"type": ["string", "null"]},
                    "related_error_class_id": {"type": ["string", "null"]},
                    "suggested_name": {"type": ["string", "null"]},
                    "rationale": {"type": "string"},
                },
                "required": [
                    "suggestion_type", "call_id", "related_error_class_id", "suggested_name", "rationale",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["suggestions"],
    "additionalProperties": False,
}


def classify_practice_area(transcript: list[dict]) -> dict:
    return call_claude_json(
        system=prompts.CLASSIFY_PRACTICE_AREA_PROMPT,
        user_content=_format_transcript(transcript),
        json_schema=CLASSIFY_SCHEMA,
    )


def _previous_attempt_note(previous_attempt: str | None) -> str:
    """Empty string on a first attempt, so the prompt is byte-for-byte the
    one this tool has always sent. Only graph.py's retry paths ever pass a
    previous_attempt, and only for email/phone — see
    prompts.PREVIOUS_ATTEMPT_NOTE for why that stitching has to happen here
    rather than in the transport."""
    if not previous_attempt:
        return ""
    return prompts.PREVIOUS_ATTEMPT_NOTE.format(previous_attempt=previous_attempt)


def extract_field(utterance: str, field_name: str, previous_attempt: str | None = None) -> dict:
    return call_claude_json(
        system=prompts.EXTRACT_FIELD_PROMPT.format(
            field_name=field_name, previous_attempt_note=_previous_attempt_note(previous_attempt)
        ),
        user_content=utterance,
        json_schema=EXTRACT_SCHEMA,
    )


def extract_and_confirm_field(utterance: str, field_name: str, previous_attempt: str | None = None) -> dict:
    """Phase 13 (latency reduction) — replaces the extract_field +
    generate_confirm_back pair used by node_capture's fresh-extraction
    path with one call: the model extracts the value and drafts the
    confirm-back phrasing for it in the same response. Node logic still
    decides deterministically (CLAUDE.md rule 3) whether that phrasing is
    ever actually used — this only removes the round trip, not the
    threshold/format checks."""
    return call_claude_json(
        system=prompts.EXTRACT_AND_CONFIRM_FIELD_PROMPT.format(
            field_name=field_name, previous_attempt_note=_previous_attempt_note(previous_attempt)
        ),
        user_content=utterance,
        json_schema=EXTRACT_AND_CONFIRM_SCHEMA,
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
        system=prompts.EXTRACT_DATETIME_PROMPT.format(
            today=today.isoformat(),
            weekday=today.strftime("%A"),
            # Spelled out rather than left implicit: "Friday" said ON a Friday
            # is the one genuinely ambiguous case, and it came up on the first
            # real call that reached booking.
            next_same_weekday=(today + timedelta(days=7)).isoformat(),
        ),
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


def _format_time_list(items: list[str]) -> str:
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + f" and {items[-1]}"


def _format_alternative_slots(alternatives: list[dict]) -> str:
    parsed = [datetime.fromisoformat(s["start_time"]) for s in alternatives]
    if len({dt.date() for dt in parsed}) == 1:
        day_label = f"{parsed[0].strftime('%A %B')} {parsed[0].day}"
        times = _format_time_list([_format_time_of_day(dt.strftime("%H:%M")) for dt in parsed])
        return f"on {day_label} at {times}"
    return _format_time_list([_format_slot_time(s["start_time"]) for s in alternatives])


def generate_alternative_offer(
    caller_profile: dict, alternatives: list[dict], unavailable_requested_time: str = None
) -> str:
    # Deterministic, unlike generate_confirmation_summary's Claude call —
    # this is just formatting known data (a name and up to three exact
    # times) into a fixed sentence, with no interpretation required, so a
    # template is both simpler and safer than trusting an LLM to reproduce
    # three precise times correctly.
    name = caller_profile["name"]["value"]
    requested_str = f" at {_format_time_of_day(unavailable_requested_time)}" if unavailable_requested_time else ""
    return (
        f"Sorry {name} — that time{requested_str} is already booked. I do have availability "
        f"{_format_alternative_slots(alternatives)} — do any of those work for you?"
    )


def generate_offer_reprompt(alternatives: list[dict]) -> str:
    # The re-ask when the caller's answer to an offer wasn't understood.
    # Deliberately NOT a replay of the previous reply: that one opened with
    # "Sorry <name> — that time at 4PM is already booked", which is true once
    # and merely confusing on the repeat, and reads as the `repetition` error
    # class to boot. Same deterministic-template reasoning as
    # generate_alternative_offer above.
    return (
        f"Sorry — I have availability {_format_alternative_slots(alternatives)}. "
        "Do any of those work for you?"
    )


def select_offered_slot(utterance: str, offered_slots: list[dict]) -> dict:
    slot_list = "\n".join(f"{i}. {_format_slot_time(s['start_time'])}" for i, s in enumerate(offered_slots))
    return call_claude_json(
        system=prompts.SELECT_OFFERED_SLOT_PROMPT.format(count=len(offered_slots), slot_list=slot_list),
        user_content=utterance,
        json_schema=SELECT_OFFERED_SLOT_SCHEMA,
    )


# Phase 8 (case research) — a BM25 score below this floor means the top
# candidate shares essentially no relevant vocabulary with the corpus, so
# the grounding Claude call is skipped entirely (Decision 2,
# docs/phases/phase-8-legal-research.md) — calibrated against the actual
# corpus content: genuine matches score ~2-6, off-topic utterances mostly
# score 0, with occasional single-word coincidental overlap landing just
# under this floor.
BM25_RELEVANCE_FLOOR = 2.0

GROUND_STATUTE_SCHEMA = {
    "type": "object",
    "properties": {
        "selected_id": {"type": ["string", "null"]},
        "spoken_framing": {"type": ["string", "null"]},
    },
    "required": ["selected_id", "spoken_framing"],
    "additionalProperties": False,
}


def search_statute_candidates(area: str, query: str) -> list[dict]:
    return knowledge_search.bm25_search(query, knowledge_corpus.load_corpus(area), top_k=3)


def ground_statute_citation(utterance: str, candidates: list[dict]) -> dict:
    # Closed-set selection only (Decision 3) — the prompt forbids selecting
    # an id outside `candidates` or inventing citation text, and the caller
    # (backend.dispatcher._search_statutes_in_background) additionally
    # verifies the returned id defensively rather than trusting it blindly.
    user_content = json.dumps(
        {
            "caller_situation": utterance,
            "candidates": [
                {"id": c["id"], "citation": c["citation"], "text": c["text"]} for c in candidates
            ],
        }
    )
    return call_claude_json(
        system=prompts.GROUND_STATUTE_CITATION_PROMPT,
        user_content=user_content,
        json_schema=GROUND_STATUTE_SCHEMA,
    )


def classify_call_errors(call_row: dict, trace: list[dict], error_classes: list[dict]) -> dict:
    """Phase 6b — the LLM judge. Classifies one completed call against the
    editable error taxonomy (eval/error_classes.py), using its outcome/
    escalation_reason plus its full trace (not just the flat transcript) as
    evidence. Returns {"flags": [...]}; an empty list is valid and expected.
    """
    user_content = json.dumps(
        {
            "outcome": call_row.get("outcome"),
            "escalation_reason": call_row.get("escalation_reason"),
            "trace": trace,
        },
        default=str,
    )
    return call_claude_json(
        system=prompts.CLASSIFY_CALL_ERRORS_PROMPT.format(
            error_class_descriptions=_format_error_classes(error_classes)
        ),
        user_content=user_content,
        json_schema=CLASSIFY_CALL_ERRORS_SCHEMA,
        # Reasoning over a full call trace (not a single utterance) reliably
        # uses well over the default 512-token budget on extended thinking
        # alone before ever emitting an answer - confirmed live: a real
        # trace consumed exactly 512 thinking tokens and got cut off with
        # zero text output (stop_reason="max_tokens", no text content block
        # at all). 4096 leaves comfortable headroom (~1000 tokens used on
        # the same call once given room).
        max_tokens=4096,
    )


def propose_taxonomy_updates(
    batch_results: list[dict], human_annotations_by_call: dict[str, dict], error_classes: list[dict]
) -> dict:
    """Phase 6c — the taxonomy-critique pass. Takes this eval batch's own
    classify_call_errors output PLUS any Benevolent Dictator annotations for
    calls in the batch (human_annotations_by_call[call_id] is None for calls
    with no call_reviews row — most calls, especially early on, and that's
    fine). Returns {"suggestions": [...]}, each destined for a "pending"
    taxonomy_suggestions row — only a human's approval should ever precede a
    hand-edit to eval/error_classes.py.
    """
    user_content = json.dumps(
        {"batch_results": batch_results, "human_annotations_by_call": human_annotations_by_call},
        default=str,
    )
    return call_claude_json(
        system=prompts.PROPOSE_TAXONOMY_UPDATES_PROMPT.format(
            error_class_descriptions=_format_error_classes(error_classes)
        ),
        user_content=user_content,
        json_schema=PROPOSE_TAXONOMY_UPDATES_SCHEMA,
        # Same reasoning-budget issue as classify_call_errors above — this
        # reads an entire batch's worth of classification results at once,
        # an even larger input.
        max_tokens=4096,
    )
