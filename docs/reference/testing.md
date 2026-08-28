# Testing

**406 tests, no live API calls, ~46 seconds.**

```bash
pytest backend/tests eval/tests                          # what pre-commit runs (402)
pytest backend/tests eval/tests backend/supervisor/knowledge/tests   # everything (406)
```

| Directory | Tests | Gated by pre-commit |
|---|---:|---|
| `backend/tests/` | 351 | yes |
| `eval/tests/` | 51 | yes |
| `backend/supervisor/knowledge/tests/` | 4 | **no** |

That third directory is a real gap: BM25 ranking changes will not be caught by a commit. Either run the full command yourself or widen the hook in `.pre-commit-config.yaml`.

`asyncio_mode = "auto"` is set in `pyproject.toml`, so `async def test_...` functions need no decorator.

---

## What covers what

### The conversation

| File | Covers |
|---|---|
| `test_scenarios.py` (599) | **The regression suite.** All seven canonical scenarios (eight test functions — S7 has two variants) end to end through the real dispatcher → graph → persistence path, Claude mocked. |
| `test_routing_node.py` | Classification, both escalation branches, the retry |
| `test_capture_node.py` | The synchronous drain path: confirm/deny/correct, validation, 3-strikes |
| `test_capture_fast.py` | The optimistic fast pass: gates, advancement, delayed-failure interrupt |
| `test_booking_node.py` | Propose, alternatives, decline, race handling |
| `test_escalation_node.py` | Handoff note content and the fallback |
| `test_case_research.py` | Skip, bare affirmation, BM25 floor, grounding, defensive id check |
| `test_graph_transitions.py` | `route_by_stage` for every stage and sub-phase |
| `test_apply_extraction.py` | The threshold bands and the email/phone carve-out |
| `test_validators.py`, `test_heuristics.py`, `test_faq.py`, `test_fillers.py` | The deterministic units |

### Plumbing

| File | Covers |
|---|---|
| `test_dispatcher_async.py` | Locking, concurrency for one call, reconcile steps, background spawning |
| `test_dispatcher_latency_events.py` | Latency boundary events are emitted correctly |
| `test_concurrency_stress.py` | Many independent calls, no cross-call leakage |
| `test_llm_utils.py`, `test_llm_utils_usage.py` | Retry, `LLMCallFailed`, model override, usage capture |
| `test_tracing.py` | `traced_call` on success and failure |
| `test_system_error_escalation.py` | The 3-consecutive-failure path |
| `test_schema_bootstrap.py` | Schema creation |

### Transport, API, data

| File | Covers |
|---|---|
| `test_livekit_agent.py` (714) | Verbatim-transcript precedence and the race, filler scheduling, the five interrupt cases, latency boundaries, usage capture |
| `test_livekit_token_endpoint.py` | Token minting and the 503 |
| `test_admin_routes.py`, `test_annotation_routes.py`, `test_trace_stream.py`, `test_access_gate.py` | The HTTP and WS surface |
| `test_repository.py`, `test_sqlite_*_repository.py` | Each repository against temp SQLite |
| `test_seed_slots.py`, `test_seed_demo_calls.py`, `test_config.py` | Seeding and settings |

### `eval/tests/`

`test_insights_agent.py` (439) covers all three passes plus latency and cost derivation; the rest mirror their scripts one-for-one.

---

## The two test doubles, and when to use which

### Fake repositories — `backend/tests/fakes.py`

In-memory dict-backed implementations of the ABCs: `FakeCallRepository`, `FakeSlotRepository`, `FakeTraceRepository`, `FakeEvalRepository`, `FakeAnnotationRepository`.

Use them for **logic tests** — nodes, dispatcher, routes. Faster and simpler than a temp file, and `FakeSlotRepository` lets you set `availability_result`, `alternatives_result` and `book_side_effect` directly instead of seeding a calendar to produce a conflict.

```python
@pytest.fixture
def repos():
    return Repositories(calls=FakeCallRepository(), slots=None, trace=FakeTraceRepository())
```

`slots=None` is fine — a test that never books never touches it. `evals`, `annotations` and `dev` are `Optional` for the same reason.

> **`FakeTraceRepository` stores `payload` as a dict; `SQLiteTraceRepository` stores `payload_json` as a string.** Both are valid `get_trace` results. `eval/insights_agent.py::_payload(event)` normalises them — read through it, never index either key directly.

### Real repositories over temp SQLite — `backend/db/repositories/testing.py`

```python
conn = create_in_memory_connection()   # :memory:, schema applied, check_same_thread=False
```

Use this when the **SQL itself** is what you are testing: the repository tests, schema tests, and anything going through `TestClient` (which runs the app in a different thread via anyio's portal, hence `check_same_thread=False`).

---

## Mocking Claude — two levels, deliberately different

**`tools.py`-level** — patch the individual tool functions. Use this for **business-logic fidelity**: the graph's real branching runs, only the model's answer is fixed.

```python
with patch("backend.supervisor.tools.classify_practice_area",
           return_value={"area": "tenancy", "confidence": 0.9}):
    reply = await run_supervisor_turn(repos, call_id, "t1", "I need help with my flat.")
```

**`GRAPH.invoke`-level** — patch the whole graph. Use this for **concurrency** tests, where the graph's internals are irrelevant and the point is the dispatcher's locking and task handling.

`eval/concurrency_stress_test.py` documents why the second form needs care: under true `asyncio.gather` concurrency you must use a **single** `side_effect` callable keyed off each turn's own input state, never N nested `with patch(...)` blocks. `unittest.mock.patch` mutates one shared global attribute, and N context managers entering and exiting out of strict LIFO order — guaranteed once the patched calls suspend on real `asyncio.to_thread` work — corrupt each other's restore state.

There is a related trap in ordinary tests: **a background verification task spawned by one turn can pick up a patch installed for a different tool on a later turn**, because both patch the same global `tools.extract_field` attribute. That is a real bug that reached live code, and it is why `background_verify_field` is an explicit signal rather than an inferred diff. If a capture test behaves inexplicably, check whether a background task is still in flight.

---

## Global state must be reset

`CALL_STATES` and `dispatcher.LOCKS` are module-level dicts that persist across tests. Every test file touching them uses an autouse fixture:

```python
@pytest.fixture(autouse=True)
def clear_dispatcher_state():
    CALL_STATES.clear()
    dispatcher.LOCKS.clear()
    yield
    CALL_STATES.clear()
    dispatcher.LOCKS.clear()
```

Forget it and you get order-dependent failures that pass in isolation. `FIELD_VERIFICATIONS` and `STATUTE_SEARCHES` are the same category — a test that spawns one should await it (see `_await_statute_search` in `test_scenarios.py`) rather than leaving it to resolve during someone else's test.

---

## Route tests

```python
app.dependency_overrides[get_repos] = lambda: repos
client = TestClient(app)
...
app.dependency_overrides.pop(get_repos, None)      # always, in a finally or fixture teardown
```

The `Depends(get_repos)` seam exists exactly for this. Always pop the override — a leaked one poisons every later test.

---

## The scenario suite

`backend/tests/test_scenarios.py` implements [`../scenarios.md`](../scenarios.md)'s S1–S6 plus S7's two variants — eight test functions for seven scenarios. **One test function per scenario**, so a failure names the exact scenario that broke, and so manual regression checklists can reference the same names.

Each turn goes through `run_supervisor_turn` — the real dispatch entry point — so the real dispatcher, lock and persistence path is exercised, not just node functions in isolation. Since Phase 14 the file is transport-agnostic: `run_supervisor_turn` returns its reply rather than pushing it at a transport, so nothing needs stubbing just to keep a delivery mechanism quiet.

**The same scenarios exist in three places, and they must stay in step:**

| Where | Claude | Transport |
|---|---|---|
| `backend/tests/test_scenarios.py` | mocked | bypassed |
| `eval/replay_scenarios.py` | **real** | bypassed |
| `eval/livekit_live_call.py` | **real** | **real**, with synthesized speech |

Change the number of turns a stage takes and all three desynchronise — every later scripted utterance ends up answering the wrong question. The mocked suite will usually fail first, which is the point.

---

## Writing a new test

- **A node branch** → add to that node's `test_*_node.py`. Build a `CallState` with `new_call_state()`, mutate what the branch needs, call the node with `{"configurable": {"repos": repos}}`, assert on the returned partial dict *and* on `repos.trace.events`.
- **A deterministic helper** → a plain unit test; no fixtures needed.
- **A dispatcher behaviour** → `test_dispatcher_async.py`, with the autouse reset fixture.
- **An endpoint** → `test_admin_routes.py` with `dependency_overrides`.
- **A conversation-level behaviour that spans stages** → a scenario test, and mirror it into `replay_scenarios.py`.

Assert on trace events, not just return values. The trace is the contract the admin panel and the eval judge both depend on, and a branch that returns the right dict but forgets its `node_exited` is a real bug.

---

## What is not covered

- **The browser client has no automated test coverage at all.** `client/app.js` and `client/livekit-transport.js` are verified by hand.
- **The transport boundary** is only exercised by `eval/livekit_live_call.py`, which is not part of the suite and needs a running backend plus real API spend. It exists because that is exactly the class of bug this codebase has already shipped once — the `ask_supervisor`/ASR race.
- **Nothing judges whether a call *sounded* natural.** Whether a filler landed well, whether barge-in felt right, whether the agent sounded like a receptionist — a human has to listen. `livekit_live_call.py` says so in its own docstring.
