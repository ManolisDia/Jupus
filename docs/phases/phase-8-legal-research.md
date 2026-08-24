# Phase 8 — Case Research (Statute Citation)

## Goal

Insert a new stage between `capture` and `booking`: once the caller's contact details are confirmed, the agent asks a real follow-up question about what's actually happened, searches a small hand-authored knowledge base of statutes/regulations for each of the three practice areas, and — when something genuinely relevant exists — cites the specific provision back to the caller before moving on to booking. A caller who says "my landlord is trying to evict me tomorrow without giving me any notice" should hear the actual notice-period provision that applies, not a generic "that sounds like a tenancy issue." Like Phase 7, the *retrieval* latency (a keyword search plus one grounding Claude call) is hidden behind a natural follow-up question rather than a synchronous pause — by the time the caller finishes answering that follow-up, the citation (or the decision that there isn't one) is already resolved.

This phase promotes and replaces the "Real statute citation (tenancy)" optional stretch that used to live in what is now `docs/phases/phase-9-polish-submission.md` — expanded from one practice area to all three, and from a synchronous tool call to the same background-latency-hiding pattern Phase 7 established for field capture.

## Prerequisite

Phase 7 (optimistic capture) DoD met, on top of Phases 1–6a/6b/6c. Builds on top of `backend/supervisor/graph.py`'s `node_capture_confirm` (this phase changes its terminal transition target), `backend/supervisor/state.py`'s `CallState`, and `backend/dispatcher.py`'s background-task pattern (`FIELD_VERIFICATIONS`, `_reconcile_field_verifications`) — read all three in full, plus `docs/phases/phase-7-optimistic-capture.md` in full, before starting. This phase is deliberately the same shape as Phase 7's fast/background/drain pattern, reused rather than reinvented, so read that doc as the reference implementation of "how do we hide a background Claude call behind a filler turn" before writing anything here.

## Why this exists

The user stories the brief grades explicitly want confidence handling and routing to feel like a real domain expert, not a keyword-triggered form. A caller describing a real legal problem in their own words is exactly the moment where citing something concrete and checkable — a real statute, not a paraphrase of what tenancy law is generally about — is the most convincing single thing this prototype can do. It's also a second, independent thing to demonstrate beyond field capture: a genuine retrieval step over a structured local corpus, gated by a relevance floor so the agent doesn't force a citation where none applies, wired through the same tracing/tool-scoping doctrine as everything else.

## Non-goals

- **Not full-text legal research.** The corpus is 8–12 hand-authored entries per practice area (~30 total), not a general legal database. Retrieval only ever selects among what's in the corpus — it never lets an LLM invent or paraphrase a citation from its own training knowledge (see Decision 3).
- **Not a second confidence-threshold/retry system.** Unlike `capture`'s 3-strikes escalation, a failed or empty search here is a silent no-op — nothing about this stage ever escalates a call or blocks booking (Decision 4). It is best-effort enrichment, not a required field.
- **Not touching `node_capture_confirm`'s own field-confirmation logic.** The only change to that node is its terminal transition target once every field is confirmed (`"booking"` → `"research"`) — its drain-order/re-ask/escalation behavior for name/email/phone is exactly as Phase 7 left it.
- **Not a new tool visible to Realtime.** `search_statute_candidates`/`ground_statute_citation` are backend-only, called from the `research` node exactly like every other tool in the catalog — rule #1 is untouched.
- **Not re-running research if the caller circles back to a new topic later** (e.g. brings up a second issue during booking). One research pass per call, tied to the practice area determined at routing.

## Decisions made, not left open for the implementer

**1. Retrieval is keyword search (BM25) over a small local corpus, not embeddings, not a vector DB.** At 8–12 entries per area, a proper information-retrieval ranking function is enough to be "smart" (rank by relevance, don't linearly scan/dump the whole corpus into a prompt) without adding a paid or heavy dependency. `backend/supervisor/knowledge/search.py` implements plain-Python BM25 (Okapi BM25, `k1=1.5, b=0.75`, standard defaults) — no new entry in `pyproject.toml`. This is exactly the retrieval approach the superseded stretch in `docs/phases/phase-9-polish-submission.md` already called for; this phase just applies it to all three areas instead of one.

**2. A BM25 relevance floor gates whether an LLM call happens at all.** If the top BM25 score for a query is below a fixed floor (tuned during implementation against the worked corpus, expect roughly "top hit shares almost no vocabulary with any entry"), the search short-circuits to "no citation" without ever calling Claude — most caller utterances during this stage (small talk, a vague non-answer, a question back at the agent) shouldn't cost an LLM call just to confirm they're not a legal question. Only utterances that already look like they're *about* a real fact pattern reach the grounding call.

**3. Grounding is closed-set selection, never open generation — the hallucination guard.** The one Claude call in this flow (`ground_statute_citation`) is handed only the top BM25 candidates' `{id, citation, text}` and must return either one of those exact `id`s or `null` — never freeform citation text. Code that consumes the result must additionally verify the returned `id` is actually one of the candidates it was given (defensive: if a malformed/hallucinated id ever came back, treat it as `null`, don't trust it blindly). This is the single most important property of this feature for a legal-adjacent demo: the agent can be wrong about *whether* something is relevant, but it must never be able to invent a citation that doesn't exist in the corpus.

**4. A failed or still-pending background search degrades to "no citation," never to an escalation or a stall.** `call_claude_tool`'s normal retry/failure handling still applies to the grounding call for tracing consistency, but a caught `LLMCallFailed` here does **not** increment `consecutive_llm_failures` or count toward the 3-strikes `system_error` escalation (`docs/phases/cross-cutting.md` section 1) — that machinery exists to protect the call's actual required outcomes (capture, booking), and this stage produces neither. Same treatment as Phase 7's accepted residual limitation for a background task still in flight at drain time: if `research`'s delivery turn arrives before the background search has resolved, it is treated as "nothing found," not awaited synchronously. Given the search gets a full caller turn's worth of head start (the filler question below) before delivery, this is expected to essentially never bite in practice, same reasoning Phase 7 documented for its own version of this tradeoff.

**5. One fixed jurisdiction, stated plainly, for a demo, not a production legal product.** All three corpora are England & Wales law, hand-authored from general knowledge during implementation (not scraped, not generated at runtime, not independently verified against primary sources) — the same caveat the superseded stretch already carried. Every delivered citation is followed by a fixed spoken disclaimer (Decision 6) and `docs/DECISIONS.md` gets an entry stating the jurisdiction and corpus provenance plainly.

**6. The disclaimer is code, not model discretion.** Whenever `node_research_deliver` has a citation to speak, the reply is built by template — `f"{citation['spoken_framing']} {STATUTE_DISCLAIMER}"` — never left to an LLM to remember to add. `STATUTE_DISCLAIMER` is a fixed constant: *"Just so you know — this is general information, not legal advice, but it's worth mentioning to the attorney."*

**7. Not decided now — a flagged future consideration, not a DoD item.** BM25 over 8–12 hand-authored entries per area is the right-sized retrieval mechanism for this corpus today (Decision 1). If the corpus grows substantially later (more areas, many more entries per area, denser statute text) or keyword match starts missing paraphrased situations that don't share vocabulary with the relevant entry, a local sentence-transformer embedding + cosine search is the natural next step — still no vector DB needed until the corpus is far larger than "a few dozen entries per area." Logged here (and in `docs/DECISIONS.md`) as something to revisit deliberately later, not something this phase needs to build toward.

---

## Corpus

`backend/supervisor/knowledge/{employment,tenancy,immigration}_statutes.json`, one file per practice area, each entry:

```json
{
  "id": "tenancy-poe1977-s5",
  "citation": "Protection from Eviction Act 1977, s.5",
  "jurisdiction": "England & Wales",
  "topic_tags": ["eviction", "notice", "notice to quit", "landlord", "residential tenancy"],
  "text": "A notice to quit given by either landlord or tenant of premises let as a dwelling is not valid unless it is in writing and given not less than four weeks before the date on which it is to take effect."
}
```

`topic_tags` exist purely to give BM25 extra vocabulary to match against beyond the raw statute text (callers describe situations in plain language — "kicked out," "landlord," "no warning" — not statutory phrasing); they are indexed as part of the searchable text, not surfaced to the caller.

**Illustrative examples for this doc** (the real corpus — 8–12 entries per area — is authored during implementation, not written out in full here):

- **Tenancy**: notice periods before eviction (Protection from Eviction Act 1977 s.5, shown above), the Housing Act 1988 s.21/s.8 grounds-for-possession distinction, deposit protection requirements, harassment/illegal-eviction offences.
- **Employment**: statutory minimum notice periods (Employment Rights Act 1996 s.86), unfair dismissal qualifying period (s.94/s.108), redundancy consultation requirements, whistleblowing protection (s.103A).
- **Immigration**: overstay consequences and re-entry bans under the Immigration Rules, the right to work / right to rent checks, basic asylum-claim timing rules, EU Settlement Scheme late-application grounds.

Each file lives entirely outside the repository pattern (`docs/architecture.md`) — it's static reference data loaded at process start, the same category as `eval/error_classes.py`, not a SQLite table, so nothing here needs a new repository or touches rule #9.

## Retrieval — `backend/supervisor/knowledge/`

```python
# corpus.py
class StatuteEntry(TypedDict):
    id: str
    citation: str
    jurisdiction: str
    topic_tags: list[str]
    text: str

def load_corpus(area: str) -> list[StatuteEntry]:
    # loads and caches backend/supervisor/knowledge/{area}_statutes.json once per process
    ...
```

```python
# search.py — plain-Python BM25, no new dependency
BM25_K1 = 1.5
BM25_B = 0.75

def bm25_search(query: str, corpus: list[StatuteEntry], top_k: int = 3) -> list[dict]:
    # tokenize query + each entry's (text + " ".join(topic_tags)), score with
    # standard Okapi BM25, return the top_k entries each merged with a "score"
    # key, highest first. An empty/all-zero-score result is a valid, expected
    # return (most utterances during this stage won't be about any statute).
    ...
```

## Tools (`backend/supervisor/tools.py` additions)

```python
BM25_RELEVANCE_FLOOR = <tuned during implementation>

def search_statute_candidates(area: str, query: str) -> list[dict]:
    return search.bm25_search(query, corpus.load_corpus(area), top_k=3)

GROUND_STATUTE_SCHEMA = {
    "type": "object",
    "properties": {
        "selected_id": {"type": ["string", "null"]},
        "spoken_framing": {"type": ["string", "null"]},
    },
    "required": ["selected_id", "spoken_framing"],
    "additionalProperties": False,
}

def ground_statute_citation(utterance: str, candidates: list[dict]) -> dict:
    # prompts.GROUND_STATUTE_CITATION_PROMPT instructs Claude: given the
    # caller's own words plus ONLY the provided candidates' {id, citation,
    # text}, either pick the single one that genuinely applies (selected_id)
    # with a short spoken_framing sentence grounded in that entry's text, or
    # return {selected_id: null, spoken_framing: null} if none genuinely fit
    # — explicitly forbidden from citing anything not in the candidate list.
    return call_claude_json(
        system=prompts.GROUND_STATUTE_CITATION_PROMPT,
        user_content=json.dumps({"caller_situation": utterance, "candidates": candidates}),
        json_schema=GROUND_STATUTE_SCHEMA,
    )
```

`search_statute_candidates` is deterministic (goes through `traced_call` directly, rule #8); `ground_statute_citation` is Claude-backed (goes through `call_claude_tool`, rules #7/#8).

## State additions (`backend/supervisor/state.py`)

```python
class CallState(TypedDict):
    ...
    stage: Literal["greeting", "routing", "capture", "research", "booking", "escalation", "ended"]
    # Phase 8 (case research) — only meaningful while stage == "research".
    research_phase: Literal["gather", "deliver"]
    # Set once the background search resolves (found or not); None while
    # still pending or if research never ran (e.g. call escalated earlier).
    statute_citation: Optional[dict]   # {citation, text, spoken_framing} | None
    # Transient — set ONLY by node_research_gather, popped by dispatcher.py
    # right after GRAPH.invoke returns, same pattern as Phase 7's
    # background_verify_field. Never left set across turns.
    background_search_query: Optional[str]
```

`new_call_state` additions: `research_phase="gather"`, `statute_citation=None`, `background_search_query=None`.

```python
# backend/dispatcher.py — mirrors FIELD_VERIFICATIONS
STATUTE_SEARCHES: dict[str, asyncio.Task] = {}   # call_id -> background search task (one in flight at a time)
```

## Graph changes

### `node_capture_confirm` — one-line change to its terminal transition

Where today, once every field in `FIELD_PRIORITY` is `"confirmed"`, it returns `{"stage": "booking", ...}` — it now returns `{"stage": "research", "research_phase": "gather", **_agent_turn(RESEARCH_INTRO_QUESTIONS[area])}` instead. Everything else about that node (drain order, re-ask, 3-strikes escalation) is unchanged. The intro question is asked in the *same* turn/return as the stage transition — no separate "intro" node or chained empty turn is needed (contrast with `greeting`→`routing`'s chaining trick in `dispatcher.py`, which exists only because `node_greeting` is content-blind; here the transitioning node already has everything it needs to ask the question directly).

```python
RESEARCH_INTRO_QUESTIONS = {
    "employment": "Before we get you booked in — can you tell me a bit more about what's been going on at work?",
    "tenancy": "Before we get you booked in — can you tell me a bit more about what's been happening with your landlord?",
    "immigration": "Before we get you booked in — can you walk me through a bit more of what's going on with your case?",
}
```

### `node_research_gather` (new) — handles the turn answering the intro question

```python
RESEARCH_FILLER_QUESTIONS = {
    "employment": "Got it — did they give you a reason, in writing or otherwise?",
    "tenancy": "Got it — did they give you anything in writing, or was it just said to you?",
    "immigration": "Got it — do you know what type of visa or status you're currently on?",
}

BOOKING_INVITE_REPLY = "What day and time works for you?"


def node_research_gather(state: CallState, config: RunnableConfig) -> dict:
    utterance = state["transcript"][-1]["text"]
    area = state["practice_area"]
    if heuristics.looks_like_research_skip(utterance):
        # caller explicitly doesn't want to elaborate -> straight to booking,
        # no search ever spawned. Speaks BOOKING_INVITE_REPLY rather than
        # nothing — silence here would leave the caller with no cue that
        # booking has started (correction made during implementation: an
        # earlier draft of this doc had this branch reply with nothing,
        # which is a real dead-air bug, not a "no filler" stylistic choice
        # — those are different things). (Explicit "get me a human"
        # requests are already caught earlier, centrally, in
        # dispatcher.process_supervisor_call.)
        return {"stage": "booking", "research_phase": "gather", **_agent_turn(BOOKING_INVITE_REPLY)}
    return {
        "research_phase": "deliver",
        "background_search_query": utterance,
        **_agent_turn(RESEARCH_FILLER_QUESTIONS[area]),
    }
```

No Claude call on this turn at all — the filler question is templated, exactly like `node_capture_fast`'s zero-LLM-call fast path. This is what buys the search its time: the caller's answer to the filler question is itself a whole extra turn of wall-clock time before `node_research_deliver` ever runs.

### `node_research_deliver` (new) — handles the turn after the filler question

```python
STATUTE_DISCLAIMER = "Just so you know — this is general information, not legal advice, but it's worth mentioning to the attorney."

def node_research_deliver(state: CallState, config: RunnableConfig) -> dict:
    citation = state.get("statute_citation")   # merged in by dispatcher's reconciliation step, see below
    if citation:
        reply = f"{citation['spoken_framing']} {STATUTE_DISCLAIMER} {BOOKING_INVITE_REPLY}"
    else:
        reply = BOOKING_INVITE_REPLY
    return {"stage": "booking", "research_phase": "gather", **_agent_turn(reply)}
```

If `citation` is `None` — genuinely nothing relevant found, search failed, or the background task hadn't resolved yet (Decision 4) — the reply is silent on the *statute* topic (no citation, no disclaimer) but still speaks `BOOKING_INVITE_REPLY` to move the caller into booking; this turn must never leave the caller with dead air and no next question, which is a different concern from `docs/DECISIONS.md`'s "no filler acknowledgment while waiting" entry (that's about not narrating a wait, not about skipping a real question the conversation needs).

### `route_by_stage`

Extend to dispatch `stage == "research"` + `research_phase == "gather"` → `node_research_gather`, `research_phase == "deliver"` → `node_research_deliver`.

### Background search (dispatcher-owned)

```python
# backend/dispatcher.py

async def _search_statutes_in_background(repos: Repositories, call_id: str, area: str, utterance: str) -> Optional[dict]:
    candidates = traced_call(
        repos.trace, call_id, "research", "search_statute_candidates",
        tools.search_statute_candidates, area, utterance,
    )
    if not candidates or candidates[0]["score"] < tools.BM25_RELEVANCE_FLOOR:
        return None
    try:
        grounded = await asyncio.to_thread(
            call_claude_tool, repos.trace, call_id, "research", "ground_statute_citation",
            tools.ground_statute_citation, utterance, candidates,
        )
    except LLMCallFailed:
        return None   # best-effort enrichment; never escalates (Decision 4)
    candidate_ids = {c["id"] for c in candidates}
    if not grounded["selected_id"] or grounded["selected_id"] not in candidate_ids:
        return None   # includes the defensive guard from Decision 3
    entry = next(c for c in candidates if c["id"] == grounded["selected_id"])
    return {"citation": entry["citation"], "text": entry["text"], "spoken_framing": grounded["spoken_framing"]}
```

Spawned right after `GRAPH.invoke` returns, keyed off `background_search_query`, mirroring exactly how `dispatcher.py` spawns `_verify_field_in_background` off `background_verify_field` (Phase 7):

```python
if query := updated.get("background_search_query"):
    STATUTE_SEARCHES[call_id] = asyncio.create_task(
        _search_statutes_in_background(repos, call_id, updated["practice_area"], query)
    )
    updated["background_search_query"] = None
```

### Reconciliation (dispatcher-owned, non-blocking)

Mirrors `_reconcile_field_verifications` exactly — checked immediately before every `GRAPH.invoke`, cheap no-op whenever `STATUTE_SEARCHES` doesn't have an entry for this `call_id`:

```python
def _reconcile_statute_search(state: CallState, call_id: str) -> None:
    task = STATUTE_SEARCHES.get(call_id)
    if task is not None and task.done():
        STATUTE_SEARCHES.pop(call_id)
        try:
            state["statute_citation"] = task.result()
        except Exception:
            logger.exception("background statute search crashed call_id=%s", call_id)
            state["statute_citation"] = None
```

Never awaits an in-flight task — if it isn't done yet by the time `node_research_deliver` runs, `state["statute_citation"]` simply stays whatever it already was (`None` by default), which `node_research_deliver` already treats as "nothing to say" (Decision 4).

---

## Worked example

1. Capture confirms name/email/phone as usual (Phase 7's fast+confirm flow, unchanged). Last confirm-back resolves → `node_capture_confirm` transitions straight into asking: *"Before we get you booked in — can you tell me a bit more about what's been happening with your landlord?"* — **zero extra turn spent just switching stages.**
2. Caller: *"My landlord is trying to evict me tomorrow without giving me any notice."* → `node_research_gather`: not a skip phrase, spawns the background search on this utterance, replies instantly (templated, no LLM call): *"Got it — did they give you anything in writing, or was it just said to you?"* — **zero visible wait**, and the search now has the caller's entire next turn to resolve.
3. While the caller is answering that filler question (e.g. *"No, nothing in writing, they just showed up and told me to leave"*), the background task runs: BM25 over the tenancy corpus against utterance 2's text scores the notice-period entry highest, clears the relevance floor, the grounding Claude call selects it and produces a short spoken framing.
4. `node_research_deliver` runs on this turn: the search has typically already resolved by now, so `statute_citation` is populated. Reply: *"Under the Protection from Eviction Act 1977, landlords generally have to give at least four weeks' written notice before starting eviction proceedings against a residential tenant — that doesn't sound like what happened here. Just so you know — this is general information, not legal advice, but it's worth mentioning to the attorney."* → `stage` moves to `"booking"`, which proceeds exactly as today.

A caller who instead says *"honestly I'd rather just get booked in"* at step 2 skips straight to booking with no search ever spawned.

---

## Tests

New file `backend/tests/test_case_research.py`, plus `backend/supervisor/knowledge/tests/test_search.py` for the retrieval unit itself:

1. `test_bm25_search_ranks_relevant_entry_top` — query text describing an eviction-without-notice situation against the tenancy corpus; assert the notice-period entry is the top-scored result.
2. `test_bm25_search_low_score_for_irrelevant_query` — an unrelated query (e.g. asking about opening hours) scores below `BM25_RELEVANCE_FLOOR` against every entry in a given area's corpus.
3. `test_search_statute_candidates_is_traced` — calling it through `traced_call` records a `tool_call_start`/`tool_call_end` pair.
4. `test_ground_statute_citation_rejects_id_not_in_candidates` — mock a Claude response whose `selected_id` isn't among the candidates passed in; assert `_search_statutes_in_background` treats this as `None`, not as a citation (Decision 3's defensive guard).
5. `test_capture_confirm_transitions_to_research_not_booking` — once every field is confirmed, `node_capture_confirm` now returns `stage="research"`, `research_phase="gather"`, with the area-specific intro question as the reply.
6. `test_research_gather_spawns_background_search_with_zero_llm_calls` — `node_research_gather` itself makes no `call_claude_tool`/`call_claude_json` call; it only sets `background_search_query` and returns the templated filler question.
7. `test_research_gather_skip_phrase_goes_straight_to_booking` — an utterance matching `looks_like_research_skip` transitions directly to `stage="booking"` with no `background_search_query` set (assert `STATUTE_SEARCHES` never gains an entry for this call).
8. `test_research_deliver_includes_citation_and_disclaimer_when_found` — `state["statute_citation"]` pre-populated with a mock result; assert the delivered reply contains both the mock `spoken_framing` and the exact `STATUTE_DISCLAIMER` text, and `stage` moves to `"booking"`.
9. `test_research_deliver_silent_when_no_citation_found` — `state["statute_citation"] is None`; assert the reply contains no citation/disclaimer text and `stage` still moves cleanly to `"booking"`.
10. `test_research_deliver_treats_unresolved_background_task_as_no_citation` — a `STATUTE_SEARCHES` entry that is not yet `.done()` by the time `node_research_deliver` runs; assert this is treated identically to "not found" (Decision 4), not as a block/wait.
11. `test_search_failure_does_not_count_toward_system_error_escalation` — `ground_statute_citation` raising `LLMCallFailed` inside `_search_statutes_in_background` does not touch `consecutive_llm_failures`.
12. Full end-to-end: `test_scenario_s7_case_research_with_citation` and `test_scenario_s7_case_research_no_citation` (`backend/tests/test_scenarios.py`, per `docs/scenarios.md` S7) — both variants confirm `stage` passes through `"research"` and the call still reaches `stage == "ended"` / `calls.outcome == "booked"`, i.e. this stage never changes the booking outcome, only adds two turns before it.

## Definition of Done

- [x] `backend/supervisor/knowledge/{employment,tenancy,immigration}_statutes.json` authored — 9/10/9 entries respectively, all England & Wales, each with a real (hand-written-from-general-knowledge, not scraped) `citation`/`text`.
- [x] `backend/supervisor/knowledge/search.py`'s BM25 implementation, `node_research_gather`, `node_research_deliver`, `looks_like_research_skip`, and the dispatcher's `_search_statutes_in_background`/`_reconcile_statute_search`/`STATUTE_SEARCHES` implemented per the design above. `BM25_RELEVANCE_FLOOR` set to `2.0` after calibrating against genuine vs. off-topic queries live during implementation — see `tools.py`'s comment for the actual score ranges observed. A basic stopword list was added to `search.py`'s tokenizer after calibration surfaced short common-word coincidental matches inflating scores for clearly irrelevant queries.
- [x] `pytest` (full suite) passes with zero failures (278 passed), including every test listed above and both new `test_scenarios.py` S7 variants. `node_research_gather`'s skip-phrase branch and `node_research_deliver`'s no-citation branch both speak `BOOKING_INVITE_REPLY` rather than nothing — see the corrected code snippets above; an earlier draft of this doc had them reply with silence, a real dead-air bug caught before merge, not a deliberate "no filler" choice.
- [ ] Manual, live: describe the eviction-without-notice situation in a real browser/mic session; confirm the agent asks the intro question, then the filler question with no audible gap, then delivers the real Protection from Eviction Act citation plus the disclaimer on the very next turn.
- [ ] Manual, live: give a vague/generic answer with nothing statute-relevant in it; confirm the call proceeds silently to booking with no citation forced in.
- [ ] Manual, live: say something matching `looks_like_research_skip` at the intro question; confirm the call skips straight to booking with no extra turns.
- [ ] `eval/replay_scenarios.py`'s S7 (both variants) re-run live against the real, unmocked pipeline, confirming the same real outcomes as the mocked test.
- [x] `docs/DECISIONS.md` entry added: jurisdiction (England & Wales), corpus provenance (hand-authored from general knowledge for this take-home, not independently verified against primary sources), and the BM25-floor + closed-set-grounding design as the two things that keep this feature from ever inventing a citation.
- [x] `admin/graph.js`/`admin/graph.html` (the optional live "supervisor mind" visualization, `docs/phases/phase-9-polish-submission.md`) updated to show the new `research` stage — a "Research" box between "Capture (confirm)" and "Booking", `node_research_gather`/`node_research_deliver` both mapped onto it, new edges/loop, and `describeTransition` branches for the capture→research handoff and both research outcomes (citation found / not found / caller skipped). Not itself required by this phase's DoD, but was left stale after the initial build and fixed afterward rather than left silently misleading.
