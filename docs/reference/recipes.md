# Recipes

Concrete task walkthroughs. Each lists every file you have to touch.

---

## Add a practice area

Say you want `family` law alongside employment, tenancy and immigration.

1. **`backend/supervisor/state.py`** — add it to the `practice_area` `Literal`.
2. **`backend/supervisor/tools.py`** — add it to `CLASSIFY_SCHEMA`'s `area` enum.
3. **`backend/supervisor/prompts.py`** — name it in `CLASSIFY_PRACTICE_AREA_PROMPT`. Say what belongs in it and what does not; ambiguity here shows up as `unclear` escalations.
4. **`backend/supervisor/graph.py`** — add entries to `RESEARCH_INTRO_QUESTIONS` and `RESEARCH_FILLER_QUESTIONS`. **Both dicts are indexed by area with no fallback**, so a missing key is a `KeyError` mid-call.
5. **`backend/supervisor/knowledge/family_statutes.json`** — the corpus. `load_corpus(area)` resolves `{area}_statutes.json` by convention; entries need `id`, `citation`, `jurisdiction`, `topic_tags`, `text`.
6. **`backend/db/seed_slots.py`** — add it to `AREAS` and re-seed. Slots are per-area; without this, availability always returns `None` and every call in the new area escalates with `no_acceptable_slot`.
7. **Tests** — a routing case, and a research case with a corpus hit.

> Re-seeding wipes the database. See "Reset the database" below.

---

## Add a captured field

Say `postcode`, after `phone`.

1. **`backend/supervisor/state.py`** — add it to `CallerProfile`, to `FIELD_PRIORITY` (order = the order it is asked), and to `new_call_state`'s profile.
2. **`backend/supervisor/graph.py`** — add a label to `FIELD_LABELS`. Decide whether it is high-stakes: if it must always be read back, add it to the `("email", "phone")` tuples in `apply_extraction`, `node_capture`, `_finish_fast_pass` and `_verify_field_in_background` — **there are several such tuples and they must agree.** If a wrong value is low-stakes, do nothing and it inherits `name`'s 0.75/0.4 bands. If it needs a spell-out re-ask, add it to `SPELL_OUT_REPLIES`.
3. **`backend/supervisor/tools.py`** — add a validator if the format is checkable, and call it through `traced_call`.
4. **`backend/supervisor/heuristics.py`** — add a `looks_like_field_shape` branch. Keep it **looser than the validator**: it runs against raw speech, not a normalised value.
5. **`backend/db/schema.sql`** and **`sqlite_calls.py`** — add a `caller_postcode` column and write it in `upsert`, remembering the confirmed-only rule.
6. **`backend/supervisor/tools.py`** — add a line to `_handoff_note_text` so escalations carry it.
7. **`backend/dispatcher.py`** — add it to `call_state_snapshot`'s field tuple.
8. **`client/index.html`** — a `.field-tile` with `data-field="postcode"`.
9. **Tests** — `test_capture_fast.py`, `test_capture_node.py`, and update the scenario scripts in **both** `test_scenarios.py` and `eval/replay_scenarios.py`, since every scenario now has one more turn.

---

## Add a graph node

1. **`state.py`** — add the stage to the `stage` `Literal`, plus any sub-phase field.
2. **`graph.py`** — add a branch to `route_by_stage`.
3. Write the node:

```python
def node_yours(state: CallState, config: RunnableConfig) -> dict:
    repos = _repos(config)
    call_id = state["call_id"]
    repos.trace.record_event(call_id, "node_entered", node="yours")
    try:
        result = call_claude_tool(repos.trace, call_id, "yours", "your_tool",
                                  tools.your_tool, state["transcript"])
    except LLMCallFailed:
        return _llm_failure_fallback(repos, state, "yours")
    reply = "..."
    repos.trace.record_event(call_id, "node_exited", node="yours",
                             stage_from="yours", stage_to="next", pending_reply=reply)
    return {"stage": "next", "consecutive_llm_failures": 0, **_agent_turn(reply)}
```

4. **`build_graph()`** — register it in the node list **and** the conditional entry point map. Both. The `add_edge(name, END)` is handled by the loop.
5. `node_exited` on **every** return path; `"consecutive_llm_failures": 0` on every successful one.
6. **Tests** — a `test_yours_node.py`, plus a `test_graph_transitions.py` case.

Remember: one invoke runs one node. If your node should ask a question that logically belongs to the next stage, ask it *in your node* rather than adding a chained turn — that is what `_enter_research` does.

---

## Change a prompt safely

Prompts are behaviour. Validate the change, do not eyeball it.

```bash
python eval/replay_scenarios.py --label baseline      # BEFORE the change — real API spend
# ... edit backend/supervisor/prompts.py ...
python eval/replay_scenarios.py --label after-tweak
python eval/compare_runs.py --baseline baseline --candidate after-tweak
```

`compare_runs` exits 1 if any error class regressed by more than the threshold (default 0.1). Also run `pytest backend/tests` — several tests assert on exact reply strings.

If the prompt drives a tool you are also thinking of moving to Haiku, this is the same procedure that gates that. See below.

---

## Move a tool to Haiku

Both existing Haiku tools followed this exactly, and a third should too.

1. Confirm the task shape fits: **closed-set selection or classification**, not free-text extraction. Haiku was rejected project-wide for extraction after live testing — see [`tool-catalog.md`](tool-catalog.md).
2. Pass `model=HAIKU_MODEL_ID` at the `call_claude_tool` site. Do not touch `MODEL_ID`.
3. Baseline, change, re-run, compare — the procedure above.
4. Ship only if no error class regressed. Cost accounting adapts automatically: `_record_usage` reads the model off the response, and `eval/pricing.py` already carries Haiku rates.

---

## Add an FAQ entry

`backend/supervisor/faq.py`:

```python
{"keywords": ["parking", "car park"], "answer": "There's paid parking on the same street."}
```

First match wins, so put narrower entries first. The answer is **prepended** to whatever the current node was going to say, so keep it to one short sentence that reads naturally before a question. Add a case to `test_faq.py`.

---

## Add a statute

Append to `backend/supervisor/knowledge/{area}_statutes.json`:

```json
{"id": "unique-slug", "citation": "Housing Act 1988 s.21",
 "jurisdiction": "England & Wales",
 "topic_tags": ["eviction", "notice", "section 21"],
 "text": "The actual statutory text..."}
```

`topic_tags` are concatenated with `text` for BM25 scoring, so they are the cheapest way to make an entry findable by the words a caller would actually use.

The corpus is cached per process — **restart the backend** after editing. Sanity-check that a realistic utterance clears `BM25_RELEVANCE_FLOOR = 2.0`:

```python
from backend.supervisor.knowledge import corpus, search
search.bm25_search("my landlord is evicting me without notice", corpus.load_corpus("tenancy"))
```

If genuine matches score below 2.0, tune the entry's `topic_tags` before touching the floor — the floor is calibrated against the whole corpus.

---

## Add an error class

See [`eval.md`](eval.md). Short version: append to `ERROR_CLASSES` with a new stable `id`, re-run the judge with `--calls all`, check `calibrate_judge.py`, update `docs/error_taxonomy.md`. Nothing else — the admin UI and the rate computation both read the registry.

**Never rename an existing `id`.** Historical flags reference it.

---

## Add a trace event

```python
repos.trace.record_event(call_id, "your_event", node="yours", some_field=value)
```

Flat, JSON-serialisable payload. If anything downstream reads it, teach `_stage_durations_for_call` or `_cost_for_call`, and read the payload via `_payload(event)` — the two repository implementations store it differently. Document it in [`tracing.md`](tracing.md).

---

## Add an endpoint

See [`api.md`](api.md). Take `repos: Repositories = Depends(get_repos)`, read only through repositories, register before any `{param}` route that could shadow it and before the `/admin` mount, and add a `TestClient` test with `dependency_overrides`.

---

## Add a repository method

1. Abstract method on the ABC in `base.py`.
2. Implementation in the owning `sqlite_*.py`.
3. **Implementation in the matching fake in `backend/tests/fakes.py`.** The fakes subclass the ABCs, so an unimplemented abstract method breaks every test that constructs one. This is the most common surprise after a data-layer change.
4. A repository test against temp SQLite.

---

## Regenerate the filler audio

Required whenever `FILLER_PHRASES`, `VOICE` or `VOICE_SPEED` changes in `backend/supervisor/fillers.py` — a mismatch between the live voice and the baked-in filler voice is audible.

```bash
python scripts/generate_filler_audio.py     # costs a few cents of OpenAI TTS
```

Writes `backend/transport/filler_audio/{key}_{index}.wav` as 16-bit mono PCM at 24kHz. **Commit the WAVs.** LiveKit decodes and resamples them at play time.

Watch for silent lead-in: an earlier clip had 890ms of silence baked into the front, which made the latency report meaningless until it was stripped.

---

## Reset the database

```bash
python backend/db/seed_slots.py        # DESTROYS everything: calls, traces, annotations, eval history
python backend/db/seed_demo_calls.py   # optional: 8 canned calls + 2 BD annotations
```

`seed_slots.py` calls `reset_schema`, which drops every table. There is no migration system. **If the eval history matters, copy `backend/db/calendar.db` first.**

Schema changes mean editing `schema.sql`, adding the table to `SCHEMA_TABLES` in `connection.py`, and re-seeding.

---

## Swap SQLite for Postgres

1. Write `Postgres*Repository` classes against the ABCs in `base.py`.
2. Add a `db_backend == "postgres"` branch to `get_repositories`.
3. Set `db_backend` and `DATABASE_URL`. Both settings already exist in `backend/config.py`.

Nothing outside `backend/db/repositories/` changes. Carry over two things that do not travel automatically: the `UNIQUE(call_id, seq)` trace constraint, and the `SCHEMA_TABLES` allowlist the DB viewer depends on.

---

## Debug a live call

1. **Get the `call_id`.** The caller page shows it in a chip; click to copy.
2. **Check the knowledge base first.** Grep [`../fixes/INDEX.md`](../fixes/INDEX.md) and [`../known-issues/INDEX.md`](../known-issues/INDEX.md) for the symptom. About twenty non-trivial bugs are already written up, several of them live-only.
3. **Read the trace.** `/admin` → the call → the trace tab. Or `GET /api/calls/{id}/trace`. Or the `check-backend-logs` skill, which tails `backend.log` and cross-references it against `trace_events` for you.
4. **Look for the diagnostic events.** `capture_fast_gate_fallback`, `capture_fast_delayed_failure_reask`, `capture_fast_pending_confirm_fallback`, `research_gather_bare_affirmation_fallback` — each explains exactly why the fast path bailed. `llm_retry` and `llm_call_failed` explain a slow or degraded turn.
5. **Watch it live next time.** `/admin/graph.html` shows the graph and the sub-state as it happens.
6. **Write it up.** Solved → an entry in `docs/fixes/` plus its index. Investigated but unsolved → `docs/known-issues/` with symptom, what was ruled out, failed workarounds, and the current best hypothesis. This is a standing requirement in [`CLAUDE.md`](../../CLAUDE.md), and the reason step 2 pays off as often as it does.
