# Call State

`backend/supervisor/state.py`. The single in-memory object that represents a conversation in progress. Every node reads it, every node returns a partial update to it, and the DB row is derived from it.

Get this wrong and the symptoms are subtle: a field silently confirmed without ever being read back, an utterance attributed to the wrong question, a value overwritten by a background task from three turns ago. Several such bugs are written up in [`docs/fixes/`](../fixes/INDEX.md).

---

## The nested types

```python
class FieldCapture(TypedDict):
    value: Optional[str]
    confidence: float
    status: Literal["missing", "pending_confirm", "confirmed"]
    attempts: int
    validated: bool

class CallerProfile(TypedDict):
    name: FieldCapture
    email: FieldCapture
    phone: FieldCapture

FIELD_PRIORITY = ["name", "email", "phone"]
```

`FIELD_PRIORITY` is the canonical order for everything: the fast pass asks in this order, the drain phase confirms in this order, and when two background verifications fail in the same reconcile pass the earliest in this order is surfaced first.

### `FieldCapture` field by field

| Key | Meaning |
|---|---|
| `value` | The extracted value, or `None`. Only meaningful when `status != "missing"`. |
| `confidence` | Extraction confidence from the model. **Note:** the nodes set `value` and `status` on the profile but generally do *not* write `confidence` back — it stays `0.0` for most of a call. It is carried in the background-verification result dict and surfaced in the client snapshot, but no branch reads it from the profile. Do not rely on it. |
| `status` | The real state machine. `missing` → nothing usable captured. `pending_confirm` → a value is held but has not been assented to. `confirmed` → read back and agreed, or auto-confirmed above threshold. |
| `attempts` | Failed capture attempts for this field. **Three escalates** with `capture_failed`. Only incremented by foreground paths — a background verification that fails was never spoken to the caller, so it is not an "attempt". |
| `validated` | Initialised `True` and never written again. Vestigial; format validity is expressed through `status` instead. |

### The status transition rules — `apply_extraction`

```python
def apply_extraction(field_name, value, confidence):
    if field_name in ("email", "phone"):
        if confidence <= 0 or value is None: return None, "missing"
        return value, "pending_confirm"          # NEVER auto-confirmed
    if confidence >= 0.75:  return value, "confirmed"
    elif confidence >= 0.4: return value, "pending_confirm"
    else:                   return None, "missing"
```

**Email and phone can never reach `confirmed` without an explicit read-back-and-assent turn**, regardless of confidence. A wrong value there means the firm cannot reach the caller. `name` is different by design: a wrong name is low-stakes, so ≥0.75 auto-confirms.

There is a second, stricter gate on top for email and phone in the nodes: `LOW_CONFIDENCE_CONFIRM_THRESHOLD = 0.75`. A well-formed value heard *below* that confidence is treated exactly like an invalid one — re-asked with a deterministic "please spell that out" prompt rather than sent to a confirm-back whose wording depends on the model choosing to spell things out. Live testing showed that soft prompt instruction was not reliably followed (`docs/fixes/2026-08-24-007.md`).

---

## `CallState` — every field

### Identity and position

| Field | Type | Written by | Notes |
|---|---|---|---|
| `call_id` | `str` | `new_call_state` | The LiveKit room name, minted by the browser. Immutable. |
| `stage` | `"greeting" \| "routing" \| "capture" \| "research" \| "booking" \| "escalation" \| "ended"` | every node; `run_supervisor_turn` (explicit escalation) | The primary router input. |
| `practice_area` | `"employment" \| "tenancy" \| "immigration" \| None` | `node_routing` | Selects the statute corpus, the slot pool, and the research question wording. |
| `transcript` | `Annotated[list[dict], operator.add]` | dispatcher (caller turns), `_agent_turn` (agent turns) | **Appends, never replaces.** Each turn is `{"role", "text", "ts"}`. |

### Capture

| Field | Type | Written by | Notes |
|---|---|---|---|
| `caller_profile` | `CallerProfile` | `node_capture`, `node_capture_fast`, `_finish_fast_pass`, `_reconcile_field_verifications` | Always replaced wholesale with a merged copy, never mutated in place inside a node. |
| `capture_phase` | `"fast" \| "confirm"` | `node_capture_fast`, `_delayed_failure_reask` | Only meaningful while `stage == "capture"`. Splits one stage into two node entry points. |
| `last_asked_field` | `Optional[str]` | `node_routing`, `node_capture_fast`, `_sync_last_asked_field`, `_delayed_failure_reask` | **The field the caller's next utterance is presumed to answer.** Set to `"name"` by `node_routing`, because that node's own reply already asks for it. |

### Booking

| Field | Type | Written by | Notes |
|---|---|---|---|
| `proposed_slot_id` | `Optional[int]` | `node_booking` | A single slot on the table awaiting yes/no. Also read by `SQLiteCallRepository.upsert` as "the slot that got booked" when `booking_confirmed` is true — which is why the offered-alternatives path sets it explicitly before ending. |
| `offered_slots` | `Optional[list[dict]]` | `node_booking` | Up to three **full slot dicts**, not just ids, so the next turn can hand them straight to `select_offered_slot` with no DB round trip. `None` whenever no offer is outstanding. |
| `declined_slot_ids` | `Annotated[list[int], operator.add]` | `node_booking` | **Appends.** Return only the *delta*, never the whole list, or entries duplicate. Two declines of individually-proposed slots escalate with `no_acceptable_slot`; declining a whole batch of alternatives loops instead, excluding them from the next search. |
| `requested_date` | `Optional[str]` | `node_booking` | ISO date from `extract_datetime`. |
| `requested_window` | `Optional[str]` | `node_booking` | `"morning"`, `"afternoon"`, or `"any"`. |
| `booking_confirmed` | `bool` | `node_booking` | Drives `outcome == "booked"`. |

### Research

| Field | Type | Written by | Notes |
|---|---|---|---|
| `research_phase` | `"gather" \| "deliver"` | `_enter_research`, `node_research_gather`, `node_research_deliver` | Same two-entry-points-one-stage trick as `capture_phase`. |
| `statute_citation` | `Optional[dict]` | `_reconcile_statute_search` **only** | `{"citation", "text", "spoken_framing"}`. Found / not found / failed / still running all collapse to a falsy result here — `node_research_deliver` cannot tell them apart and does not need to. |

### Control and failure

| Field | Type | Written by | Notes |
|---|---|---|---|
| `pending_reply` | `Optional[str]` | every node, via `_agent_turn` | What the agent says this turn. Returned by `run_supervisor_turn`. |
| `retry_counts` | `dict[str, int]` | `node_routing` (`"classification"`), `node_research_gather` (`"research_gather"`) | Per-concern counters. Replaced wholesale with `{**old, key: n}`. |
| `escalation_reason` | `Optional[str]` | any escalating branch; dispatcher | One of seven values — see [`supervisor-graph.md`](supervisor-graph.md). Drives `outcome == "escalated"`. |
| `consecutive_llm_failures` | `int` | `_llm_failure_fallback` (increment); every successful node (reset to 0) | **Three consecutive escalates** with `system_error`. Note that nearly every successful return dict includes `"consecutive_llm_failures": 0` — that is the reset, and a new branch must not forget it. |

---

## The three transient fields

These are the sharpest edge in the codebase. All three are **declared `CallState` fields** (LangGraph silently drops any key not in the schema), set by exactly one producer, consumed within one turn, and reset to `None` rather than deleted.

| Field | Set by | Consumed by | Contract |
|---|---|---|---|
| `verification_failed_field` | `dispatcher._reconcile_field_verifications`, immediately before `GRAPH.invoke` | `node_capture_fast` / `node_capture_confirm`, at the top | "A background check for *some earlier* field came back a genuine failure." **Never set by a graph node.** Cleared at the start of every reconcile pass. |
| `background_verify_field` | `node_capture_fast`'s "advance to the next field" branch **only** | `dispatcher`, right after `GRAPH.invoke` returns | "Spawn the real extraction for this field." Never left set across turns. |
| `background_search_query` | `node_research_gather` **only** | `dispatcher`, right after `GRAPH.invoke` returns | "Spawn the statute search with this utterance." Never left set across turns. |

### Why they are signals and not diffs

The dispatcher could, in principle, infer "a field was advanced past" from a before/after diff of `last_asked_field`. It must not, and this is not hypothetical:

- `_fallback_to_real_capture` also changes `last_asked_field` — for example when resolving a pending confirmation and moving to the next already-known field. But that utterance was already fully, synchronously processed. Spawning a background task for it too would reuse the same utterance a second time, and its result could land on a **later** turn and overwrite an already-correct value with something extracted from unrelated text. This happened.
- Similarly, `verification_failed_field` is deliberately **not** gated on `field == last_asked_field`. By construction it never can match: a field only gets a background check once `node_capture_fast` has already advanced past it, so by the time that check resolves, `last_asked_field` always points at a later field. An earlier version had that gate, and the signal could effectively never fire — a caller's later utterance got silently misattributed to a stale field (`docs/fixes/2026-08-24-005.md`).

### What a delayed failure actually does

`_delayed_failure_reask` interrupts explicitly. It re-asks the failed field by name and **deliberately does not process this turn's utterance as an answer to anything** — the caller will have to repeat whatever they just said. `last_asked_field` moves to the failed field and `capture_phase` is force-reset to `"fast"`. Reusing the current utterance to re-extract the failed field is exactly the bug this replaced.

---

## LangGraph merge semantics — three things to remember

1. **A node returns a partial dict, not the whole state.** Keys you omit are left alone.
2. **`Annotated[..., operator.add]` fields append.** That is `transcript` and `declined_slot_ids` only. Return the delta; returning the full list duplicates it.
3. **Keys outside the `CallState` schema are silently dropped.** No error, no warning. If you invent an ad-hoc key to pass information between the graph and the dispatcher, it will simply vanish. This is why all three transient fields are declared.

---

## Lifecycle

```python
CALL_STATES: dict[str, CallState] = {}

def get_or_create_state(call_id): ...   # new_call_state on first sight
```

- Created lazily on the first `ask_supervisor` for a call id.
- Mutated only under `dispatcher.get_lock(call_id)`.
- **Never removed.** `CALL_STATES` grows for the process's lifetime. Fine for a local prototype; a real deployment would need eviction.
- Not persisted. A restart loses every in-flight call, and the admin live view for those calls goes dark (the DB row and the trace survive).

### Initial values

`stage="greeting"`, `capture_phase="fast"`, `research_phase="gather"`, `booking_confirmed=False`, `consecutive_llm_failures=0`, all three fields `missing`, every optional `None`, `transcript` and `declined_slot_ids` empty.

---

## The read-only projection

`dispatcher.call_state_snapshot(state)` produces the display-only dict published to the browser data channel and rendered by the admin live-graph page. It exposes stage, practice area, escalation reason, booking flags, and per-field `{value, confidence, status}`.

It is strictly one-way. Nothing a client does with it can reach back into the call. It exists to make the *internal* sub-state of the `capture` and `booking` nodes visible without promoting that sub-state to real graph nodes — which rule 2 would not want anyway.
