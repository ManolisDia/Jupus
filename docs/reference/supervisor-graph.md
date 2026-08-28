# The Supervisor Graph

`backend/supervisor/graph.py`. Eight nodes, one router, no LLM-chosen edges.

---

## The shape

```python
g.set_conditional_entry_point(route_by_stage, {...})
for name, fn in NODES:
    g.add_node(name, fn)
    g.add_edge(name, END)      # every node goes straight to END
```

**One `GRAPH.invoke` runs exactly one node and produces exactly one reply.** There are no node-to-node edges. "Advancing a stage" means a node returning `{"stage": "booking"}`; the *next* turn's `route_by_stage` will then pick the booking node. The only exception is the greeting chain, and that is done by the dispatcher calling `invoke` twice, not by the graph.

This is why you will see nodes doing work that looks like it belongs to the next node — `_enter_research`, for example, asks the research intro question from *inside* the capture node, because the transitioning node already has everything it needs and a separate turn would cost a round trip for nothing.

### The router

```python
def route_by_stage(state):
    if stage == "ended":    return "escalation"      # + a warning log; shouldn't happen
    if stage == "capture":  return "capture_confirm" if capture_phase == "confirm" else "capture_fast"
    if stage == "research": return "research_deliver" if research_phase == "deliver" else "research_gather"
    return stage
```

Seven stages, eight nodes. `capture` and `research` each split into two nodes via a sub-phase field; `ended` is a defensive fallback that should never be reached (the dispatcher returns early for ended calls).

| Stage | Sub-phase | Node |
|---|---|---|
| `greeting` | — | `greeting` |
| `routing` | — | `routing` |
| `capture` | `capture_phase == "fast"` | `capture_fast` |
| `capture` | `capture_phase == "confirm"` | `capture_confirm` |
| `research` | `research_phase == "gather"` | `research_gather` |
| `research` | `research_phase == "deliver"` | `research_deliver` |
| `booking` | — | `booking` |
| `escalation` | — | `escalation` |

> `node_capture` is **not** a registered graph node. It is a plain function — the original synchronous capture logic — called by `node_capture_confirm` and by `_fallback_to_real_capture`. Node names in traces are `capture` for that function and `capture_fast` for the fast pass.

---

## Flow

```
        greeting ──(dispatcher chains immediately)──► routing
                                                        │
              ┌── unclear (1st) ──► routing             │ area classified
              │                                         ▼
              │                              capture_fast ◄──┐
              │                                   │          │ next field
              │                                   │ all fields asked
              │                                   ▼          │
              │                            capture_confirm ──┘ drain
              │                                   │
              │                                   │ nothing pending or missing
              │                                   ▼
              │                            research_gather ──► research_deliver
              │                                   │                   │
              │                                   │ skip phrase       │
              │                                   ▼                   ▼
              │                                booking ◄──────────────┘
              │                                   │  │
              │                                   │  └─ propose / offer alternatives (loops)
              │                                   ▼
              │                             ended (booked)
              ▼
        escalation ──► ended (escalated)
              ▲
              └── from ANY stage: explicit_request, system_error ×3
```

---

## Node reference

### `greeting`

| | |
|---|---|
| Entry | `stage == "greeting"` (first turn only) |
| Tools | none |
| Returns | `stage="routing"`, `consecutive_llm_failures=0` |
| Reply | **none** |

A content-blind stub. Realtime already delivered the spoken greeting itself, from its own instructions, without calling any tool. This node exists only to bump the stage so the caller's first real utterance — already sitting in this invocation's transcript — gets classified immediately rather than being discarded for a turn. The dispatcher chains straight into `routing` within the same turn. See `docs/fixes/2026-08-22-001.md`.

---

### `routing`

| | |
|---|---|
| Entry | `stage == "routing"` |
| Tools | `classify_practice_area` (Claude, full transcript) |
| Trace | `node_entered`, `node_exited` |

| `area` result | Outcome |
|---|---|
| `"employment"` / `"tenancy"` / `"immigration"` | `stage="capture"`, `practice_area=area`, `last_asked_field="name"`. Reply: *"Got it — this falls under {area} law. Let's start with your name — could you tell me that?"* |
| `"multiple_areas"` | Escalate, `out_of_scope_multi_area` |
| `"unclear"`, 1st time | `retry_counts["classification"]=1`, re-ask: *"...is this to do with your job, your home, or immigration?"* |
| `"unclear"`, 2nd time | Escalate, `unable_to_classify` |

`multiple_areas` and `unclear` are deliberately different: the prompt tells the model to use the former only when the issue itself is genuinely cross-cutting, never merely ambiguous.

Note that the success reply **already asks for the name**, which is why `last_asked_field` is set here — the Phase 7 fast pass starts at this node, not at the first `capture_fast` turn.

---

### `capture_fast`

| | |
|---|---|
| Entry | `stage == "capture"` and `capture_phase == "fast"` |
| Tools | **none on the happy path** |
| Trace | `node_entered`, plus one of `capture_fast_pending_confirm_fallback` / `capture_fast_gate_fallback` / `capture_fast_delayed_failure_reask`, then `node_exited` |

The zero-Claude-call sequencer. Checked in this order:

1. **Is `last_asked_field` already `pending_confirm`?** Then this utterance answers *that confirm-back*, not a fresh field question — regardless of what the gates below would say. A plain "no, it's Alex Smith" does not look like a tangent at all, but advancing on it would skip the confirmation entirely and leave the field pending forever. → `_fallback_to_real_capture`.
2. **Is `verification_failed_field` set?** → `_delayed_failure_reask`. Interrupt, re-ask that field by name, do not process this utterance.
3. **Do the gates fail?** `is_explicit_human_request` or `looks_like_tangent` or `not looks_like_field_shape(asked_field, utterance)` → `_fallback_to_real_capture`.
4. **Otherwise, advance optimistically.** `_next_fast_field(profile, asked_field)` finds the next still-`missing` field; reply *"Great — and what's your {label}?"*, set `last_asked_field` and `background_verify_field=asked_field`.
5. **No next field?** → `_finish_fast_pass`, then set `capture_phase="confirm"` unless the result already moved to `research`.

The gates are deliberately conservative. A false positive costs one redundant fallback to already-correct behaviour; a false negative produces a visibly wrong next question a beat later.

`_next_fast_field` is **profile-aware, not positional** — after a delayed-failure interrupt resumes the fast pass on an earlier field, the immediately-following field may already be resolved, so anything not `missing` is skipped.

---

### `_finish_fast_pass` (inside `capture_fast`)

The transition turn, run when the last field in `FIELD_PRIORITY` has just been answered. That field never got a background head start — there is no further question to run concurrently with it — so it is processed live here: `extract_field`, then format validation for email/phone, then `generate_confirm_back` for the first field still pending.

This is the **one turn** Phase 7's design accepts full latency on. Every earlier field already resolved in the background, so N per-field waits collapse to at most one.

It deliberately does **not** call `node_capture`: by this point earlier fields have very likely been merged in as `pending_confirm` by the background reconciliation, and `node_capture`'s first check is "is there a pending field?" — it would misread *this* utterance as a confirm/deny answer to that earlier field.

---

### `capture_confirm` → `node_capture`

| | |
|---|---|
| Entry | `stage == "capture"` and `capture_phase == "confirm"` |
| Tools | `confirm_field_answer`, `extract_and_confirm_field`, `generate_confirm_back`, `validate_email`, `validate_phone` |

`node_capture_confirm` is a thin wrapper: it checks `verification_failed_field` first (`node_capture` has no concept of the fast pass's background checks and would misattribute the utterance), then delegates to `node_capture`.

`node_capture` takes an `allowed_pending_field` argument that restricts which field this utterance may be treated as a confirm-back answer for:

- **`None`** (the drain phase) means "any field currently `pending_confirm`, in `FIELD_PRIORITY` order". Safe there, because the drain phase asks about pending fields strictly in that order.
- **Set** (from `_fallback_to_real_capture`, mid-fast-pass) restricts it to `last_asked_field` only. Without this, an utterance that merely happens to restate an earlier field's value — the caller repeating their email just as the fast pass moved on to the phone — gets read as answering that earlier field's *never-spoken* confirm-back and silently marks it confirmed. Confirmed live; `docs/fixes/2026-08-24-009.md`.

#### Branches

**A pending field exists** → `confirm_field_answer(utterance, field, candidate)`:

| Result | Outcome |
|---|---|
| `needs_clarification` | Repeat the last agent question verbatim (`_last_agent_reply`) |
| `confirmed` and format valid | `status="confirmed"` |
| `confirmed` but format invalid | `_deny_and_reprompt` — defence in depth; a "yes" must never confirm a value that can never validate |
| `corrected_value`, valid | `apply_extraction(field, corrected, 0.9)` |
| `corrected_value`, invalid | `_deny_and_reprompt` |
| neither | `_deny_and_reprompt` |

**No pending field** → find the first `missing` field. None left → `_enter_research`. Otherwise `extract_and_confirm_field(utterance, target)` — one Claude call producing both the value *and* the confirm-back phrasing (Phase 13 merged what used to be two round trips).

For email and phone this branch is handled fully explicitly rather than through `apply_extraction`'s thresholds: an empty value, an invalid format, or confidence below `LOW_CONFIDENCE_CONFIRM_THRESHOLD` (0.75) all produce a `SPELL_OUT_REPLIES` re-ask. For `name`, `apply_extraction`'s 0.75/0.4 bands apply.

`_deny_and_reprompt` increments `attempts` and **escalates with `capture_failed` at 3**. Every failure path goes through it — an explicit "no", a "yes" to an unvalidatable value, and a fresh invalid extraction alike — otherwise an unconfirmable value (an email with no `@` at all) would loop forever.

---

### `research_gather`

| | |
|---|---|
| Entry | `stage == "research"` and `research_phase == "gather"` |
| Tools | **none** |

Handles the answer to the research intro question that `_enter_research` already asked. Three outcomes:

| Check | Outcome |
|---|---|
| `looks_like_research_skip` | Straight to `booking`. No search ever spawned. |
| `looks_like_bare_affirmation`, 1st time | One re-ask: *"...can you tell me a bit more about what's actually been happening?"* |
| `looks_like_bare_affirmation`, 2nd time | Give up gracefully → `booking`. Never loops, never escalates. |
| otherwise | `research_phase="deliver"`, `background_search_query=utterance`, reply with the templated `RESEARCH_FILLER_QUESTIONS[area]` |

The bare-affirmation check exists because the capture→research handoff has no extra round trip for the caller to catch up on. A trailing "yep, that's correct" still reacting to the *phone* confirm-back landed here as the caller's landlord-situation description and burned the one shot at a citation (`docs/fixes/2026-08-24-008.md`).

The filler question is what buys the background search its time: the caller's answer to *it* is a whole extra turn of wall-clock before `research_deliver` runs, all at zero Claude cost.

---

### `research_deliver`

| | |
|---|---|
| Entry | `stage == "research"` and `research_phase == "deliver"` |
| Tools | none |

Reads `state["statute_citation"]`, merged in by `_reconcile_statute_search` immediately before the graph ran.

- Citation present → `spoken_framing` + `STATUTE_DISCLAIMER` + `BOOKING_INVITE_REPLY`
- Absent → just `BOOKING_INVITE_REPLY`

Either way: `stage="booking"`, `research_phase="gather"`. A still-in-flight search, a failed search and "nothing relevant found" are indistinguishable here and all handled identically. Research is best-effort enrichment; it must never block or alter the booking outcome.

---

### `booking`

| | |
|---|---|
| Entry | `stage == "booking"` |
| Tools | `extract_datetime`, `select_offered_slot` (Haiku), `confirm_booking_answer`, `generate_confirmation_summary`, `generate_alternative_offer` (deterministic), and three repository calls: `check_availability`, `suggest_alternatives`, `book` |

Three mutually exclusive branches, checked in this order:

**1. `offered_slots` is set** — alternatives are on the table.
`select_offered_slot(utterance, offered)` → `needs_clarification` repeats the question; a valid `selected_index` books it; anything else (declined all, null, or an out-of-range index) clears the offer, appends every offered id to `declined_slot_ids`, and asks what else would work.

The index is bounds-checked in code even though the prompt forbids an out-of-range answer — same defensive shape as the statute-grounding guard.

On success this path sets `proposed_slot_id` explicitly before ending, because `SQLiteCallRepository.upsert` reads that field as "the slot that got booked". Without it the persisted `booking_slot_id` would silently be NULL.

**2. `proposed_slot_id is None`** — no proposal yet.
`extract_datetime(utterance, today)`. If no date came back, or confidence < 0.4, re-ask — never fabricate a proposal from an empty date, because an empty string passes every `date(start_time) >= ?` filter and once produced a bogus proposal nobody asked for.

Then `check_availability(date, window, area, exact_time, declined_ids)`. A hit → `_propose_slot`. A miss → `suggest_alternatives` (up to 3). No alternatives at all → escalate `no_acceptable_slot`.

**3. Otherwise** — a slot is proposed, awaiting yes/no.
`confirm_booking_answer(utterance)`. Accepted → `repos.slots.book(...)` → `stage="ended"`, `booking_confirmed=True`. Declined → append to `declined_slot_ids`; **two declines escalate** with `no_acceptable_slot`; otherwise reset `proposed_slot_id=None` and ask what else would work, so the next turn re-enters branch 2 with a fresh `extract_datetime`.

#### Race handling

`repos.slots.book` is an atomic `UPDATE slots SET is_booked=1 WHERE id=? AND is_booked=0` and raises `SlotAlreadyBookedError` when someone took the slot between offer and pick. Both booking paths catch it and re-run availability, excluding everything already offered — rather than crashing the call or double-booking.

#### The one deterministic reply

`generate_alternative_offer` is **not** a Claude call. It formats a name and up to three exact times into a fixed sentence — no interpretation required, and a template is both simpler and safer than trusting a model to reproduce three precise times. It still goes through `traced_call`, per rule 8.

---

### `escalation`

| | |
|---|---|
| Entry | `stage == "escalation"` |
| Tools | `generate_call_summary` (Claude), `write_handoff_note`, `write_minimal_handoff_note` |
| Reply | *"I've passed this to our team, someone will follow up shortly."* |
| Returns | `stage="ended"` |

Writes `docs/handoffs/{call_id}.md` with the escalation reason, whatever caller details reached `confirmed`, and the generated summary. If `generate_call_summary` fails, it falls back to `write_minimal_handoff_note` — it deliberately does not try another Claude call right after one just failed.

---

## Escalation triggers

| Reason | Fires when | Checked where |
|---|---|---|
| `explicit_request` | `heuristics.is_explicit_human_request(utterance)` matches one of twelve phrases | `run_supervisor_turn`, **before the graph**, from any stage |
| `out_of_scope_multi_area` | `classify_practice_area` returns `multiple_areas` | `node_routing` |
| `unable_to_classify` | `unclear` twice | `node_routing` |
| `capture_failed` | a field's `attempts` reaches 3 | `node_capture`, `_finish_fast_pass` |
| `no_acceptable_slot` | no alternatives available, or 2 individual declines | `node_booking` |
| `system_error` | `consecutive_llm_failures` reaches 3; also set by the dispatcher's outer exception handler | `_llm_failure_fallback`, `run_supervisor_turn` |

Two of these can fire from anywhere and are not real graph states — that is what the "any stage, any time" node in [`../diagrams.md`](../diagrams.md) represents.

---

## Constants worth knowing

| Constant | Value | Where | Means |
|---|---|---|---|
| `LOW_CONFIDENCE_CONFIRM_THRESHOLD` | `0.75` | `graph.py` | Below this, an email/phone extraction is treated as invalid and re-asked with a spell-out prompt |
| `apply_extraction` bands | `0.75` / `0.4` | `graph.py` | confirm / pending_confirm / discard, for `name` only |
| capture `attempts` limit | `3` | `graph.py` | → `capture_failed` |
| `retry_counts["classification"]` limit | `2` | `graph.py` | → `unable_to_classify` |
| declined-slot limit | `2` | `graph.py` | → `no_acceptable_slot` (individual proposals only) |
| `consecutive_llm_failures` limit | `3` | `graph.py` | → `system_error` |
| `BM25_RELEVANCE_FLOOR` | `2.0` | `tools.py` | Below this, skip the grounding call entirely |
| `RETRY_BACKOFF_SECONDS` | `0.5` | `llm_utils.py` | One retry, then `LLMCallFailed` |

---

## Helper functions

| Helper | Does |
|---|---|
| `_agent_turn(reply)` | Returns `{"pending_reply": reply, "transcript": [{agent turn}]}`. Every reply goes through it. |
| `_enter_research(state)` | The capture→research transition, including asking the intro question in the same turn. |
| `_llm_failure_fallback(repos, state, node)` | The graceful-degradation path and the 3-strikes `system_error` escalation. |
| `_last_agent_reply(transcript, fallback)` | Finds the previous agent turn, skipping `transcript[-1]` (always the caller's current utterance). Used for `needs_clarification` repeats. |
| `_deny_and_reprompt(field, reply)` | Increments `attempts`, escalates at 3. |
| `_is_valid_format(field, value)` | Routes to `validate_email` / `validate_phone` through `traced_call`. |
| `_next_fast_field(profile, asked)` | Next still-`missing` field after `asked`, in `FIELD_PRIORITY` order. |
| `_sync_last_asked_field(result, current)` | Re-derives `last_asked_field` after a fallback, since `node_capture` has no concept of it and would leave it stale. |
| `_delayed_failure_reask(...)` | The explicit interrupt for a failed background verification. |
| `_propose_slot` / `_offer_alternatives` / `_escalate_no_slot` | The three booking outcomes, each handling the `declined_slot_ids` delta correctly. |

---

## If you add a node

1. Add the stage (or sub-phase) to `CallState` in `state.py`.
2. Add a branch to `route_by_stage`.
3. Write the node: take `(state, config)`, get repos via `_repos(config)`, record `node_entered`, do work, record `node_exited` with `stage_from`/`stage_to`/`pending_reply`, return a partial dict.
4. Register it in `build_graph()`'s list **and** in the conditional entry point map. Both. `add_edge(name, END)` is done by the loop.
5. Wrap every tool call in `traced_call` or `call_claude_tool`, and catch `LLMCallFailed` → `_llm_failure_fallback`.
6. Include `"consecutive_llm_failures": 0` in every successful return.
7. Add tests in the `backend/tests/test_*_node.py` style, plus a `test_graph_transitions.py` case for the routing.

See the "Add a graph node" recipe in [`recipes.md`](recipes.md).
