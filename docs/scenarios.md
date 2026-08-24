# Scenarios — shared fixtures for manual and automated testing

These are the conversation scenarios designed during planning: the original 6 (S1–S6), plus S7 added alongside Phase 8's case-research node. They're used two ways: as the live manual checks in each phase's Definition of Done, and — as of the cross-cutting regression suite (`backend/tests/test_scenarios.py`, required by Phase 6a) — as scripted, mocked-Claude automated tests exercising the full `dispatcher` → `graph` → `db` pipeline without needing a live mic or live API calls. Keeping one canonical definition means the automated version and the manual version can't silently drift apart. (Phase 6c's `eval/replay_scenarios.py` reuses these same fixtures against the real, unmocked pipeline for regression testing after prompt changes.)

For each scenario: the scripted caller utterances, the mocked tool responses that would occur if each of those Claude calls behaved as described, and the required final assertions.

---

### S1 — Info-only, no booking
- Utterances: `"I got let go from my job last week and I'm not sure if that was legal."` → `"Just info for now, thanks."`
- Mocked: `classify_practice_area` → `{"area": "employment", "confidence": 0.9}`
- Assert: `practice_area == "employment"`, call ends without reaching `booking` stage, `calls.outcome` is not `"booked"` or `"escalated"` (this is the one legitimate `"info_only"` case — see Phase 4's note that this outcome is only reachable via a graceful no-booking exit; if that exit isn't built yet, this scenario instead asserts the call simply never advances past `capture`/`routing` without erroring, and should be revisited once/if an explicit "no thanks" exit path is added to `node_capture`).

### S2 — Happy-path booking
- Utterances: name, email, "Thursday afternoon", accept the proposed slot.
- Mocked: `classify_practice_area` → tenancy/0.9; `extract_field` → high confidence for name/email; `extract_datetime` → a known-free seeded slot's date/window; `confirm_booking_answer` → `{"accepted": true}`.
- Assert: `stage == "ended"`, `booking_confirmed is True`, `calls.outcome == "booked"`, the corresponding `slots.is_booked` flipped to `1`.

### S3 — Slot-conflict booking
- Utterances: same as S2 but requests the deterministically pre-booked 10am/day-1 slot, then accepts the offered alternative.
- Mocked: `check_availability` → `None` for the requested slot; `suggest_alternative_slots` → one alternative; `confirm_booking_answer` → accepted on the second proposal.
- Assert: booking succeeds against the **alternative** slot's id, not the originally requested one; `declined_slot_ids` is empty (they accepted the first alternative offered, never declined).

### S4 — Low-confidence capture
- Utterances: garbled name, then a clear correction; garbled email at confidence `0.6`, then — since that's below `graph.LOW_CONFIDENCE_CONFIRM_THRESHOLD` (0.75) — a spelled-out re-attempt at confidence `0.9`, then a confirm-back "yes that's right."
- Mocked: `extract_field` for name → confidence `0.4` first, then a second call (post clarification) → confidence `0.9`; `extract_field` for email → confidence `0.6` first (rejected, re-asked to spell it out — see below), then `0.9` on the spelled-out attempt; `confirm_field_answer` → `{"confirmed": true}`.
- A well-formed email/phone value heard at low confidence is now treated the same as an invalid one — re-asked with a deterministic "please spell that out" prompt rather than proceeding to a confirm-back Claude might or might not actually spell out (added after a live session showed the soft "spell out if it would help" instruction in `CONFIRM_BACK_PROMPT` isn't reliably followed — see `docs/fixes/`). This refines, but doesn't reverse, `docs/DECISIONS.md`'s "email/phone are always confirmed back regardless of confidence" — that's still true once confidence clears this floor.
- Assert: `caller_profile.name.status` and `.email.status` both end as `"confirmed"`; the low-confidence turn's reply mentions spelling it out; `generate_confirm_back` was called at least once on the eventual high-confidence value (verifies the confirm-back path actually triggered, not just happened to pass).

### S5 — Model-judged escalation (multi-area)
- Utterance: an issue spanning employment + immigration.
- Mocked: `classify_practice_area` → `{"area": "multiple_areas", "confidence": 0.8}`.
- Assert: `stage == "ended"` after exactly **one** turn (no retry), `escalation_reason == "out_of_scope_multi_area"`, `docs/handoffs/{call_id}.md` exists.

### S6 — Explicit-request escalation
- Utterance: `"Can you just put me through to a real person?"` as the very first thing said.
- Mocked: none needed for classification — `is_explicit_human_request` is deterministic and should catch this phrase directly (it's in `heuristics.py`'s phrase list).
- Assert: escalation happens on the **first** `ask_supervisor` call, before `routing` or `capture` ever run; `escalation_reason == "explicit_request"`.

### S7 — Case research / statute citation (added Phase 8)
- Utterances: name, email, phone (tenancy); `"My landlord is trying to evict me tomorrow without giving me any notice."` (answers the research-intro follow-up); `"No, nothing in writing, they just showed up and told me to leave."` (answers the research node's own filler follow-up); Thursday afternoon; accept the proposed slot.
- Mocked: `classify_practice_area` → tenancy/0.9; `extract_field` → high confidence for name/email/phone; `search_statutes` (the research node's tool) → a fixed relevant-hit result keyed to a seeded `tenancy_statutes.json` entry about notice-before-eviction; `extract_datetime` → a known-free seeded slot; `confirm_booking_answer` → accepted.
- Assert: `stage` passes through `"research"` before reaching `"booking"`; the delivered reply after the research node's second turn contains the mocked citation's `citation` string and the fixed "general information, not legal advice" disclaimer line; `stage == "ended"` and `calls.outcome == "booked"` at the end, same as S2 — research must not block or alter the booking outcome, only add one extra pair of turns before it.
- A second variant of this scenario (same fixture file, a second test function) covers the **no-citation** path: an utterance with no relevant match in the corpus (e.g. a generic "just wanted to ask about your process" aside) — `search_statutes` mocked to return `None` — assert the delivered reply contains no citation text and the call proceeds to booking exactly as if the research node had never fired anything.

---

Each scenario in `backend/tests/test_scenarios.py` should be its own `test_scenario_s1_...` through `test_scenario_s7_...` function (not one giant parametrized blob) so a failure names the exact scenario that broke, and so the manual regression checklists can reference these by the same S1–S7 names. S7 was added alongside Phase 8 rather than during the original Phase 6a design pass — it lives in the same file/convention, just added later.
