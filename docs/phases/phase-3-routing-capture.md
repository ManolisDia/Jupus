# Phase 3 — Routing + Capture (User Stories 1 & 3)

## Goal

Replace the `routing` and `capture` stub nodes from Phase 2 with real logic: Claude-driven practice-area classification, single-field-at-a-time extraction, confidence-threshold reprompting, and deterministic post-extraction validation that can override a high-confidence-but-invalid extraction.

## Non-goals
- No booking logic (Phase 4).
- No escalation *implementation detail* beyond setting `stage="escalation"` and `escalation_reason` — the `escalation` node itself stays a stub until Phase 5.
- Only one field is targeted per conversational turn — never attempt to extract or confirm two fields in the same turn, even if the caller volunteers multiple pieces of information in one sentence. (See `docs/DECISIONS.md` — this is a deliberate simplicity/testability tradeoff; if the caller says "I'm John, my email's john@x.com" in one breath, this system extracts `name` this turn and will ask for email next turn even though it was already spoken. Document this as a known limitation in Phase 7, don't try to fix it now.)

## Prerequisite
Phase 2 DoD met — stub round trip confirmed working live.

---

## State model changes (supersedes the flat `CallerProfile` shown in the Phase 2 doc)

### `backend/supervisor/state.py`
```python
class FieldCapture(TypedDict):
    value: Optional[str]
    confidence: float
    status: Literal["missing", "pending_confirm", "confirmed"]
    attempts: int
    validated: bool          # always True for name/preferred_time; meaningful for email/phone

class CallerProfile(TypedDict):
    name: FieldCapture
    email: FieldCapture
    phone: FieldCapture
    preferred_time: FieldCapture

FIELD_PRIORITY: list[str] = ["name", "email", "phone", "preferred_time"]
```
`CallState.retry_counts` (from Phase 2) is used **only** for classification retries in this phase: `{"classification": int}`. Per-field retry state lives in `FieldCapture.attempts`, not in `retry_counts` — don't conflate the two.

`new_call_state()` must initialize every `CallerProfile` field to `{"value": None, "confidence": 0.0, "status": "missing", "attempts": 0, "validated": True}`.

---

## `backend/supervisor/prompts.py`

Define these as plain string constants (or small template functions), one per Claude call site — keep each prompt short and single-purpose, per the "thin, scoped" doctrine:

- `CLASSIFY_PRACTICE_AREA_PROMPT` — instructs Claude to read the conversation so far and return one of `employment | tenancy | immigration | unclear`, with a confidence score. Explicitly instruct: *"If the issue spans multiple areas or doesn't clearly fit one, return unclear — do not guess."*
- `EXTRACT_FIELD_PROMPT` — parameterized by `field_name` (`"name"`, `"email"`, `"phone"`, `"preferred_time"`). Instructs Claude to extract only that field from the caller's last utterance, with a confidence score reflecting transcription/extraction certainty (not politeness/formatting concerns). Explicitly instruct: *"If the utterance doesn't contain this field at all, return confidence 0."*
- `CONFIRM_BACK_PROMPT` — parameterized by `field_name` and `candidate_value`. Generates a short natural confirm-back question, e.g. "Did you say your email is j.smith@example.com?" — for email/phone specifically, instruct it to spell out ambiguous characters if useful.
- `CONFIRM_FIELD_ANSWER_PROMPT` — parameterized by `field_name` and `candidate_value`. Interprets the caller's reply to a confirm-back question: did they confirm, deny, or provide a correction?

## `backend/supervisor/tools.py` additions

```python
def classify_practice_area(transcript: list[dict]) -> dict:
    # Claude call, forced tool_choice, schema:
    # {"area": "employment"|"tenancy"|"immigration"|"unclear", "confidence": float}
    ...

def extract_field(utterance: str, field_name: str) -> dict:
    # Claude call, forced tool_choice, schema: {"value": str, "confidence": float}
    ...

def generate_confirm_back(field_name: str, candidate_value: str) -> str:
    # Claude call, free text (short), used as pending_reply
    ...

def confirm_field_answer(utterance: str, field_name: str, candidate_value: str) -> dict:
    # Claude call, forced tool_choice, schema:
    # {"confirmed": bool, "corrected_value": Optional[str]}
    ...

def validate_email(email: str) -> bool:
    # Deterministic, no LLM. Pattern: r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
    # Documented simplification, not full RFC 5322 — noted in Phase 7 limitations.
    ...

def validate_phone(phone: str) -> bool:
    # Deterministic, no LLM. Strip all non-digit characters, accept if
    # resulting digit count is between 7 and 15 inclusive.
    ...
```

---

## `backend/supervisor/graph.py` — node replacements

### `node_routing`
```
result = classify_practice_area(state["transcript"])
if result["area"] == "unclear":
    attempts = state["retry_counts"].get("classification", 0) + 1
    if attempts >= 2:
        return {"stage": "escalation", "escalation_reason": "unable_to_classify",
                "retry_counts": {**state["retry_counts"], "classification": attempts},
                "pending_reply": "...", "transcript": [...]}
    return {"retry_counts": {**state["retry_counts"], "classification": attempts},
            "pending_reply": "<ask a clarifying question about their issue>",
            "transcript": [...]}
            # stage stays "routing" — omit "stage" from the returned dict
            # entirely so LangGraph doesn't overwrite it, or explicitly
            # return "routing" — confirm which is correct for the
            # installed LangGraph version and be consistent everywhere.
else:
    return {"stage": "capture", "practice_area": result["area"],
            "pending_reply": "<area-appropriate acknowledgment + first capture question>",
            "transcript": [...]}
```

### `node_capture`
```
profile = state["caller_profile"]
pending_field = next((f for f in FIELD_PRIORITY if profile[f]["status"] == "pending_confirm"), None)

if pending_field:
    answer = confirm_field_answer(utterance, pending_field, profile[pending_field]["value"])
    if answer["confirmed"]:
        new_status = "confirmed"
        new_value = profile[pending_field]["value"]
        # still re-run validation here for email/phone in case a prior
        # invalid value somehow reached pending_confirm — defense in depth
    elif answer["corrected_value"]:
        # re-process as if freshly extracted, with confidence forced high
        # since the caller just explicitly restated it under confirmation
        new_value, new_status = apply_extraction(pending_field, answer["corrected_value"], confidence=0.9)
    else:
        attempts = profile[pending_field]["attempts"] + 1
        if attempts >= 3:
            return {"stage": "escalation", "escalation_reason": "capture_failed", ...}
        return {"caller_profile": {**profile, pending_field: {**profile[pending_field],
                "status": "missing", "attempts": attempts}},
                "pending_reply": "<plain re-ask for this field>", ...}
    # apply new_status/new_value via apply_extraction() helper (see below),
    # then fall through to "what's next" logic

else:
    target_field = next((f for f in FIELD_PRIORITY if profile[f]["status"] == "missing"), None)
    if target_field is None:
        return {"stage": "booking", "pending_reply": "<transition to booking>", ...}
    extracted = extract_field(utterance, target_field)
    new_value, new_status = apply_extraction(target_field, extracted["value"], extracted["confidence"])
    # fall through to "what's next" logic

# "what's next" logic, shared by both branches above:
if new_status == "pending_confirm":
    reply = generate_confirm_back(target_or_pending_field, new_value)
elif all remaining FIELD_PRIORITY fields are "confirmed":
    stage = "booking"; reply = "<transition to booking>"
else:
    next_target = next missing field in FIELD_PRIORITY
    reply = "<plain question for next_target>"
```

### `apply_extraction(field_name, value, confidence) -> (value, status)` — helper, not a Claude call
```
if field_name in ("email", "phone"):
    valid = validate_email(value) if field_name == "email" else validate_phone(value)
    if not valid:
        # deterministic override: invalid format forces pending_confirm
        # regardless of how confident the extraction was
        return value, "pending_confirm"  # validated=False
if confidence >= 0.75:
    return value, "confirmed"
elif confidence >= 0.4:
    return value, "pending_confirm"
else:
    return None, "missing"   # treat as noise, don't retain a low-confidence guess
```
This function is the concrete, testable home of the confidence thresholds and the "deterministic assertion overrides probabilistic confidence" rule from `CLAUDE.md`.

---

## Tests

### `backend/tests/test_validators.py`
1. `test_validate_email_accepts_standard_address` — `"j.smith@example.com"` → `True`.
2. `test_validate_email_rejects_missing_at_symbol` — `"j.smith example.com"` → `False`.
3. `test_validate_email_rejects_missing_domain_dot` — `"j.smith@examplecom"` → `False`.
4. `test_validate_phone_accepts_plain_digits` — `"5551234567"` → `True`.
5. `test_validate_phone_accepts_punctuated_number` — `"(555) 123-4567"` → `True`.
6. `test_validate_phone_rejects_too_short` — `"12345"` → `False`.
7. `test_validate_phone_rejects_non_numeric` — `"call-me-maybe"` → `False`.

### `backend/tests/test_apply_extraction.py`
1. `test_high_confidence_confirms` — `confidence=0.9`, non-email/phone field → status `"confirmed"`.
2. `test_medium_confidence_pending` — `confidence=0.6` → status `"pending_confirm"`.
3. `test_low_confidence_discarded` — `confidence=0.2` → status `"missing"`, value `None`.
4. `test_invalid_email_forces_pending_despite_high_confidence` — field `"email"`, `confidence=0.95`, value `"not-an-email"` → status `"pending_confirm"` (the key deterministic-override test).
5. `test_valid_email_high_confidence_confirms` — field `"email"`, `confidence=0.9`, value `"a@b.com"` → status `"confirmed"`.

### `backend/tests/test_capture_node.py` (all Claude calls mocked via `unittest.mock.patch` on `tools.py` functions — zero live API calls)
1. `test_field_priority_order_targets_name_first` — all fields `"missing"`, assert `extract_field` is called with `field_name="name"`.
2. `test_confirmed_field_advances_to_next_target` — mock `extract_field` to return high confidence for `"name"`; after one call, assert `name.status == "confirmed"` and the *next* invocation targets `"email"`.
3. `test_medium_confidence_triggers_pending_and_confirm_back_reply` — mock confidence `0.6`; assert status `"pending_confirm"`, `generate_confirm_back` was called, and `pending_reply` is non-empty.
4. `test_three_failed_attempts_on_same_field_escalates` — simulate three consecutive turns where the field never reaches `"confirmed"` (mix of low confidence and denied confirmations); assert by the 3rd, `stage == "escalation"` and `escalation_reason == "capture_failed"`.
5. `test_confirmation_yes_accepts_pending_field` — field in `pending_confirm`, mock `confirm_field_answer` → `{"confirmed": True}`; assert status becomes `"confirmed"`.
6. `test_confirmation_no_without_correction_reprompts` — mock `confirm_field_answer` → `{"confirmed": False, "corrected_value": None}`; assert `attempts` incremented, status back to `"missing"`, reply re-asks.
7. `test_confirmation_with_correction_reextracts_and_validates` — mock `confirm_field_answer` → `{"confirmed": False, "corrected_value": "not-an-email"}` for field `"email"`; assert the correction still goes through `apply_extraction`'s validation path and does **not** get accepted as `"confirmed"` just because it came from a "correction."
8. `test_all_fields_confirmed_transitions_to_booking` — pre-seed all four fields as `"confirmed"`; assert next invocation returns `stage == "booking"`.

### `backend/tests/test_routing_node.py`
1. `test_confident_classification_sets_area_and_advances` — mock `classify_practice_area` → `{"area": "tenancy", "confidence": 0.9}`; assert `practice_area == "tenancy"`, `stage == "capture"`.
2. `test_unclear_classification_reprompts_once` — mock → `{"area": "unclear", ...}`; assert `stage` stays `"routing"`, `retry_counts["classification"] == 1`.
3. `test_unclear_classification_twice_escalates` — call twice with `"unclear"`; assert on the 2nd, `stage == "escalation"`, `escalation_reason == "unable_to_classify"`.

---

## Definition of Done

- [x] `pytest backend/tests/test_validators.py backend/tests/test_apply_extraction.py backend/tests/test_capture_node.py backend/tests/test_routing_node.py` — all pass, zero live Anthropic API calls made during the run (verify by checking no `ANTHROPIC_API_KEY`-dependent network activity, e.g. run once with the env var unset and confirm tests still pass because everything's mocked).
- [x] Manual live call #1: state an employment-flavored issue — confirm the follow-up questions and any info given are employment-specific.
- [x] Manual live call #2 (separate call): state a tenancy-flavored issue — confirm the follow-up questions are visibly different from call #1's.
- [x] Manual live call #3: deliberately mumble or garble your email — confirm the agent asks a "did you say...?" confirm-back rather than silently accepting it.
- [x] Manual live call #4: give an invalid-format email clearly and confidently (e.g. say "my email is john at gmail" with no way to form a valid address) three times in a row — confirm the call escalates with `capture_failed` rather than looping forever or silently accepting garbage.
- [x] Manual live call #5: state a genuinely ambiguous/multi-area issue — confirm it escalates with `unable_to_classify` after one reprompt attempt, rather than guessing an area.
- [x] Backend log for each manual call shows the expected `FieldCapture.status` transitions matching what was said out loud.
