# Phase 4 — Booking (User Story 2)

## Goal

Replace the `booking` stub node with real logic against the seeded SQLite calendar: parse a preferred date/window, check availability, confirm details back to the caller before committing, offer alternatives on conflict, and escalate if the caller declines everything on offer. Also introduce durable call persistence (`calls` table), since this is the first phase where an outcome (booked vs not) is meaningful to record.

## Non-goals
- No escalation node *implementation* (still a stub) — this phase only needs to correctly set `stage="escalation"` + `escalation_reason` and hand off; Phase 5 builds the real escalation node and handoff notes.
- No admin panel / eval agent yet (Phase 6a+) — this phase only needs the `calls` table populated correctly, not surfaced anywhere.

## Prerequisite
Phase 3 DoD met — routing/capture confirmed working live, including the escalation paths for classification and capture failure. Also read `docs/architecture.md` — this phase is where `SQLiteCallRepository` and `SQLiteSlotRepository` get written; all SQL for `calls` and `slots` lives there, nowhere else.

---

## State model additions

### `backend/supervisor/state.py`
```python
class CallState(TypedDict):
    # ...(all Phase 2/3 fields, plus:)
    proposed_slot_id: Optional[int]
    declined_slot_ids: Annotated[list[int], operator.add]
```
`new_call_state()` initializes both to `None` / `[]`.

---

## `backend/db/repositories/sqlite_calls.py` — `SQLiteCallRepository.upsert`

```python
class SQLiteCallRepository(CallRepository):
    def upsert(self, state: CallState, outcome_override: Optional[str] = None) -> None:
        # INSERT OR REPLACE (or INSERT ... ON CONFLICT DO UPDATE) into `calls`.
        # started_at: only set on first write for this call_id — do not
        #   overwrite it on subsequent turns.
        # ended_at: set only when state["stage"] == "ended", else NULL.
        # outcome: if outcome_override is given, use it directly (only
        #   dispatcher.mark_call_abandoned passes "abandoned" — see
        #   docs/phases/cross-cutting.md). Otherwise NULL while
        #   stage != "ended"; when stage == "ended":
        #     "booked" if state["booking_confirmed"] is True,
        #     "escalated" if state["escalation_reason"] is not None,
        #     "info_only" otherwise (caller ended without booking or
        #     escalating — reachable if a future phase adds a graceful
        #     "no booking wanted" exit).
        # caller_name/email/phone: pulled from caller_profile[<field>]["value"]
        #   where status == "confirmed", else NULL.
        # transcript_json: json.dumps(state["transcript"]).
        ...
```
Called from `dispatcher.on_ask_supervisor` (via `repos.calls.upsert(updated)`) after every `GRAPH.invoke(...)` call — not just at the end of a call — so the admin panel (Phase 6a) can show in-progress calls too. This extends the Phase 2 dispatcher; note the addition inline in `dispatcher.py`, don't duplicate the whole function here. No raw SQL or `sqlite3` import appears anywhere outside this file, per `docs/architecture.md`.

---

## `backend/db/repositories/sqlite_slots.py` — `SQLiteSlotRepository`

```python
class SQLiteSlotRepository(SlotRepository):
    def check_availability(self, date: str, window: str, area: str) -> Optional[dict]:
        # Deterministic SQL. window="morning" -> start_time hour < 12,
        # "afternoon" -> hour >= 12, "any" -> no hour filter.
        # SELECT * FROM slots WHERE area=? AND is_booked=0
        #   AND date(start_time)=? [AND <window filter>]
        #   ORDER BY start_time LIMIT 1
        ...

    def suggest_alternatives(self, date: str, area: str, exclude_ids: list[int]) -> list[dict]:
        # Deterministic SQL. Same filters as above but date(start_time) >= date
        # (not just ==), id NOT IN exclude_ids, ORDER BY start_time LIMIT 3.
        ...

    def book(self, slot_id: int) -> int:
        # Deterministic. UPDATE slots SET is_booked=1 WHERE id=? AND is_booked=0
        # — check cursor.rowcount after the UPDATE: if 0 rows affected, raise
        # SlotAlreadyBookedError (someone/something else booked it between
        # check_availability and this call — node_booking must catch this
        # and re-run check_availability rather than assume success). Returns
        # slot_id as the booking reference on success.
        ...
```
`node_booking` calls these through `traced_call(repos.trace, call_id, "booking", "check_availability", repos.slots.check_availability, date, window, area)` (and similarly for the other two) — see `docs/phases/cross-cutting.md` section 0. There's no separate `tools.py` wrapper duplicating this SQL; the repository method *is* the tool.

---

## `backend/supervisor/tools.py` additions (Claude-backed only — the deterministic slot operations above live in the repository, not here)

```python
def extract_datetime(utterance: str, today: date) -> dict:
    # Claude call, forced tool_choice, schema:
    # {"date": "YYYY-MM-DD", "window": "morning"|"afternoon"|"any", "confidence": float}
    # `today` is passed into the prompt explicitly so relative phrases
    # ("Thursday", "next week") resolve correctly — never let the model
    # infer "today" on its own.
    ...

def generate_confirmation_summary(caller_profile: dict, slot: dict, area: str) -> str:
    # Claude call, short free text: "So that's John Smith,
    # j.smith@example.com, Thursday at 2pm for a tenancy matter — does
    # that sound right?"
    ...

def confirm_booking_answer(utterance: str) -> dict:
    # Claude call, forced tool_choice, schema: {"accepted": bool}
    ...
```

## `backend/supervisor/graph.py` — `node_booking` replacement

```
# node_booking(state, repos: Repositories, ...) — takes the Repositories
# bundle like every other node from this phase on. Every repos.slots.*
# call below is made through traced_call(repos.trace, call_id, "booking",
# <name>, repos.slots.<method>, ...) per cross-cutting.md section 0;
# shown bare here for readability.

if state["proposed_slot_id"] is None:
    parsed = extract_datetime(utterance, today=date.today())
    slot = repos.slots.check_availability(parsed["date"], parsed["window"], state["practice_area"])
    if slot:
        return {"proposed_slot_id": slot["id"],
                "pending_reply": generate_confirmation_summary(state["caller_profile"], slot, state["practice_area"]),
                "transcript": [...]}
    alternatives = repos.slots.suggest_alternatives(parsed["date"], state["practice_area"], state["declined_slot_ids"])
    if not alternatives:
        return {"stage": "escalation", "escalation_reason": "no_acceptable_slot", "pending_reply": "...", "transcript": [...]}
    return {"proposed_slot_id": alternatives[0]["id"],
            "pending_reply": f"<preferred slot unavailable, propose alternatives[0]>",
            "transcript": [...]}
else:
    answer = confirm_booking_answer(utterance)
    if answer["accepted"]:
        try:
            booking_id = repos.slots.book(state["proposed_slot_id"])
        except SlotAlreadyBookedError:
            # rare race — treat like a conflict and re-run the "propose"
            # branch above with the same proposed date/window rather than
            # crashing the call
            ...
        return {"stage": "ended", "booking_confirmed": True,
                "pending_reply": "You're booked — you'll get a confirmation shortly.",
                "transcript": [...]}
    else:
        declined = state["declined_slot_ids"] + [state["proposed_slot_id"]]
        if len(declined) >= 2:
            return {"stage": "escalation", "escalation_reason": "no_acceptable_slot",
                    "declined_slot_ids": [state["proposed_slot_id"]],  # reducer appends
                    "pending_reply": "...", "transcript": [...]}
        alternatives = repos.slots.suggest_alternatives(parsed_date_from_earlier_turn, state["practice_area"], declined)
        if not alternatives:
            return {"stage": "escalation", "escalation_reason": "no_acceptable_slot", ...}
        return {"proposed_slot_id": alternatives[0]["id"],
                "declined_slot_ids": [state["proposed_slot_id"]],
                "pending_reply": "<propose next alternative>", "transcript": [...]}
```
Note: the `else` branch needs the original requested date to keep suggesting alternatives near it — store the parsed `date`/`window` in `CallState` when first computed (add `requested_date: Optional[str]`, `requested_window: Optional[str]` fields alongside `proposed_slot_id`) rather than re-deriving it from the utterance a second time.

---

## Tests

### `backend/tests/test_sqlite_slot_repository.py` (against a temp SQLite DB seeded via `seed_slots.py`, no live Claude calls — these are all deterministic methods on `SQLiteSlotRepository`)
1. `test_check_availability_returns_free_slot_in_window` — query a known-free morning slot's date/area, assert a slot is returned with `start_time` hour `< 12`.
2. `test_check_availability_returns_none_when_fully_booked` — query the deterministically pre-booked 10am slot's exact date/area/window, assert... (careful: only that single slot is booked, others in the same window may be free — pick a test date/window where you've manually verified via the seed logic that zero slots remain, e.g. by booking all of them in the test setup first) → returns `None`.
3. `test_suggest_alternatives_excludes_declined_ids` — call with an `exclude_ids` list containing what would otherwise be the top result, assert it's not in the returned list.
4. `test_suggest_alternatives_returns_up_to_three_ordered_by_start_time` — assert list length `<= 3` and strictly increasing `start_time`.
5. `test_book_marks_slot_booked` — book a known-free slot, assert `is_booked` flips to `1` in the DB.
6. `test_book_raises_on_already_booked_slot` — book a slot, then attempt to book it again, assert `SlotAlreadyBookedError` is raised and the second call doesn't corrupt state.

### `backend/tests/test_booking_node.py` (Claude calls mocked: `extract_datetime`, `generate_confirmation_summary`, `confirm_booking_answer`; `repos.slots` is a fake or temp-SQLite-backed `SlotRepository` per `docs/architecture.md`)
1. `test_free_slot_proposes_and_awaits_confirmation` — fake `check_availability` returns a free slot; assert `proposed_slot_id` is set and `stage` stays `"booking"`.
2. `test_taken_slot_offers_first_alternative` — fake `check_availability` → `None`, `suggest_alternatives` → `[slot_a, slot_b]`; assert `proposed_slot_id == slot_a["id"]`.
3. `test_accepting_proposed_slot_books_and_ends_call` — `proposed_slot_id` set, mock `confirm_booking_answer` → `{"accepted": True}`; assert `stage == "ended"`, `booking_confirmed is True`, and `repos.slots.book` was called with the right `slot_id`.
4. `test_declining_first_alternative_offers_second` — mock `confirm_booking_answer` → `{"accepted": False}`, fake `suggest_alternatives` → `[slot_b]` (excluding the declined one); assert `proposed_slot_id == slot_b["id"]`, `declined_slot_ids` contains the first slot's id.
5. `test_declining_twice_escalates` — simulate two decline cycles; assert on the 2nd, `stage == "escalation"`, `escalation_reason == "no_acceptable_slot"`.
6. `test_no_alternatives_available_escalates_immediately` — fake `suggest_alternatives` → `[]` on the very first turn (nothing available near the requested date at all); assert immediate escalation, same reason.
7. `test_race_condition_slot_booked_between_check_and_confirm` — fake `repos.slots.book` to raise `SlotAlreadyBookedError`; assert the node handles it by re-attempting availability rather than propagating the exception up to the dispatcher/WebSocket handler.

### `backend/tests/test_repository.py`
1. `test_upsert_creates_row_on_first_turn` — call `upsert_call_record` on a fresh state, assert a `calls` row exists with `outcome IS NULL`.
2. `test_upsert_preserves_started_at_across_calls` — call twice with a small delay, assert `started_at` unchanged between the two writes.
3. `test_upsert_sets_ended_at_and_outcome_booked` — call with `stage="ended"`, `booking_confirmed=True`; assert `ended_at` is set and `outcome == "booked"`.
4. `test_upsert_sets_outcome_escalated` — call with `stage="ended"`, `escalation_reason="no_acceptable_slot"`; assert `outcome == "escalated"`.
5. `test_upsert_transcript_json_round_trips` — write a state with a multi-turn transcript, read the row back, `json.loads(transcript_json)` and assert it equals the original list.

---

## Definition of Done

- [x] `pytest backend/tests/test_sqlite_slot_repository.py backend/tests/test_booking_node.py backend/tests/test_repository.py` — all pass.
- [x] Manual live call (happy path): request a date/window you've confirmed via the seed data is free — confirm the agent proposes it, reads back name/email/slot/area for confirmation, and on "yes" says a clear booking confirmation. Verify in the DB: `slots.is_booked=1` for that slot, `calls` row has `outcome='booked'`.
- [x] Manual live call (conflict path): request the specific 10am slot on day 1 for one of the three areas (deterministically pre-booked by the seed script) — confirm the agent offers an alternative instead of silently failing or booking the taken slot.
- [x] Manual live call (decline-twice path): reject two consecutive proposed slots — confirm the call escalates gracefully (`no_acceptable_slot`) rather than looping indefinitely.

Confirmed live against the real OpenAI Realtime + Claude APIs. Several bugs surfaced only through live testing and were fixed within this phase branch (see commit history): `suggest_alternatives`' `id NOT IN (NULL)` silently matching zero rows, `extract_datetime` confidence being ignored (fabricated a proposal from an empty date), the capture-stage `preferred_time` field asking for a date the booking node immediately asked for again, and `confirm_field_answer`/`confirm_booking_answer` having no way to represent "I didn't understand" other than as a decline. The concurrent-double-booking manual check originally listed here was dropped by explicit product decision — the atomic `UPDATE ... WHERE is_booked=0` guard in `SQLiteSlotRepository.book` is still in place and covered by an `asyncio.gather` test matching the real single-event-loop deployment model.
