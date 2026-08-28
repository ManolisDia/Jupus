# Tool Catalog

Every tool, its model, its caller, and the plumbing underneath.

> `docs/PLAN.md` also has a tool catalog. It predates the implementation and is out of date — it lists `update_caller_profile`, which does not exist, and a `preferred_time` field that was never built. **This table is the accurate one.**

---

## The tools

All of these live in `backend/supervisor/tools.py`. Every one is invoked through `traced_call` or `call_claude_tool`, never directly (rule 8).

### Claude-backed

| Tool | Called by | Model | Returns |
|---|---|---|---|
| `classify_practice_area(transcript)` | `node_routing` | Sonnet | `{area, confidence}` — area ∈ employment / tenancy / immigration / multiple_areas / unclear |
| `extract_field(utterance, field_name)` | `_finish_fast_pass`, `_verify_field_in_background` | Sonnet | `{value, confidence}` |
| `extract_and_confirm_field(utterance, field_name)` | `node_capture` (fresh-extraction branch) | Sonnet | `{value, confidence, confirm_back_phrasing}` |
| `generate_confirm_back(field_name, candidate_value)` | `node_capture`, `_finish_fast_pass` | Sonnet | free text |
| `confirm_field_answer(utterance, field_name, candidate_value)` | `node_capture` | Sonnet | `{confirmed, corrected_value, needs_clarification}` |
| `extract_datetime(utterance, today)` | `node_booking` | Sonnet | `{date, window, time, confidence}` |
| `generate_confirmation_summary(profile, slot, area, unavailable_time=None)` | `node_booking._propose_slot` | Sonnet | free text |
| `confirm_booking_answer(utterance)` | `node_booking` | Sonnet | `{accepted, needs_clarification}` |
| `select_offered_slot(utterance, offered_slots)` | `node_booking` | **Haiku 4.5** | `{selected_index, declined_all, needs_clarification}` |
| `ground_statute_citation(utterance, candidates)` | `_search_statutes_in_background` | **Haiku 4.5** | `{selected_id, spoken_framing}` |
| `generate_call_summary(state)` | `node_escalation` | Sonnet | free text |
| `classify_call_errors(call_row, trace, error_classes)` | `eval/insights_agent.py` | Sonnet, `max_tokens=4096` | `{flags: [...]}` |
| `propose_taxonomy_updates(batch_results, human_annotations, error_classes)` | `eval/insights_agent.py` | Sonnet, `max_tokens=4096` | `{suggestions: [...]}` |

### Deterministic — no LLM, but still traced

| Tool | Called by | Returns |
|---|---|---|
| `validate_email(email)` | `node_capture`, `_finish_fast_pass`, background verification | `bool` |
| `validate_phone(phone)` | same | `bool` |
| `generate_alternative_offer(profile, alternatives, unavailable_time)` | `node_booking._offer_alternatives` | a formatted sentence |
| `search_statute_candidates(area, query)` | `_search_statutes_in_background` | top-3 BM25 hits with scores |
| `write_handoff_note(call_id, state, summary)` | `node_escalation` | `Path` to the written markdown |
| `write_minimal_handoff_note(call_id, state, reason)` | `node_escalation`, dispatcher | `Path` |

### Repository calls, also traced as tools

`node_booking` traces `check_availability`, `suggest_alternative_slots` and `book_consultation` under those names, even though they are `SlotRepository` methods rather than `tools.py` functions. That keeps the trace readable as a single sequence of "things the supervisor did".

---

## Why the two deterministic ones stay deterministic

**`validate_email` / `validate_phone`** are rule 3. Never route these through a model.

```python
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")
def validate_phone(phone): digits = re.sub(r"\D", "", phone); return 7 <= len(digits) <= 15
```

The email regex is stricter than the naive `[^@\s]+\.[^@\s]+` on purpose: the domain half excludes dots from the character class, so `x@...com` is rejected. It is a documented simplification, not full RFC 5322 (`docs/fixes/2026-08-21-002.md`).

**`generate_alternative_offer`** formats a name and up to three exact times into a fixed sentence. There is no interpretation to do, and trusting a model to reproduce three precise times correctly is a worse bet than a template. Contrast `generate_confirmation_summary`, which *is* a Claude call because it has to sound natural while weaving in a name, an email, a day, a time and a practice area.

---

## `llm_utils.py` — the only place the Anthropic SDK is touched

```
call_claude_tool(trace_repo, call_id, node, tool_name, fn, *args, model=None, **kwargs)
  ├─ sets _model_override.value = model              (thread-local)
  ├─ _run_and_record_usage → traced_call → fn(...)
  │     └─ fn calls call_claude_json / call_claude_text
  │           └─ _client.messages.create(model=_resolve_model(), ...)
  │           └─ _record_usage(response) → _last_usage.value
  │     └─ emits llm_usage trace event
  ├─ on a retryable error: emit llm_retry, sleep 0.5s, try once more
  ├─ on a second failure: emit llm_call_failed, raise LLMCallFailed
  └─ finally: clear _model_override
```

### Models

```python
MODEL_ID       = "claude-sonnet-5"                # project default
HAIKU_MODEL_ID = "claude-haiku-4-5-20251001"      # per-call override only
```

Haiku was the *original* project-wide default and was **upgraded to Sonnet** after live testing showed it unreliably following precise instructions — converting spoken "at"/"dot" into `@`/`.` — even after repeated prompt tightening (`docs/DECISIONS.md`).

Haiku came back later as a **per-call override**, never a global default, and only for closed-set selection tasks — a materially different shape from free-text extraction. Two tools ship on it (`select_offered_slot`, `ground_statute_citation`), and each was shipped only after `eval/compare_runs.py` showed no error-class regression against the Sonnet baseline for that specific tool. **Follow that procedure if you move a third.**

### Retry policy

```python
RETRYABLE_ERRORS = (anthropic.APIError, json.JSONDecodeError, StopIteration)
RETRY_BACKOFF_SECONDS = 0.5
```

`JSONDecodeError` and `StopIteration` are in there deliberately: a truncated or malformed response is functionally the same failure as an API error from the caller's point of view, and letting it escape as an unhandled exception would kill the whole graph invocation (`docs/fixes/2026-08-21-003.md`).

### Prompt caching

Every system prompt is sent as an ephemeral cache block:

```python
def _cached_system_block(system):
    return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
```

Prompts are static per node and resent verbatim every turn, so this costs nothing in correctness. It was **measured and found to be a no-op** for this project's prompt sizes — they are short enough that the caching minimum is not reached — and kept anyway, since it is free and would start paying off if prompts grew. `docs/DECISIONS.md` records the measurement rather than assuming a win.

### Usage capture

`_record_usage` reads the model **off the response**, not off `MODEL_ID`, so cost accounting prices the right model's tokens at the right rate even when a per-call override was in effect. Cache write/read counts are read defensively with `getattr`, since a mocked response may not carry them.

### The two thread-locals

`_last_usage` and `_model_override` are `threading.local()`. See the concurrency section of [`architecture.md`](architecture.md) for exactly why that is safe. `_last_usage` is cleared **before every attempt**, not just after a success, so a failed attempt whose exception propagates cannot leave a stale value for the next unrelated call on the same pool thread.

---

## `prompts.py` — one prompt per tool

`backend/supervisor/prompts.py`. Each is a plain module-level string with `{}` placeholders filled by `.format()`.

Three carry hard-won wording that should not be casually edited:

**`EXTRACT_FIELD_PROMPT` / `EXTRACT_AND_CONFIRM_FIELD_PROMPT`** draw an explicit line between *transcription* and *invention*: converting a spoken "at" to `@` is fine because the caller said something that maps to it; adding a domain they never said is not. This is the supervisor-side counterpart to the transport-side verbatim-transcript fix.

**`CONFIRM_FIELD_ANSWER_PROMPT`**'s final paragraph — "`corrected_value` must be nothing more than the corrected value itself" — was added after real trace data showed output tokens ballooning to 200–350 across repeated attempts at a garbled email, eventually exceeding `max_tokens=512` mid-generation and producing truncated JSON, which forced a retry. That was the actual root cause of this tool's multi-second latency tail. The fix constrains verbosity at the source rather than raising the token ceiling, which would have let the wasteful generation succeed without making it fast (`docs/fixes/2026-08-25-002.md`).

**`GROUND_STATUTE_CITATION_PROMPT`** forbids selecting an id outside the candidate list and forbids inventing or paraphrasing a citation from general knowledge. The caller verifies the returned id anyway. Both halves are required: the prompt is the intent, the code check is the guarantee.

**`CLASSIFY_CALL_ERRORS_PROMPT`** instructs the judge to reason over the *trace*, not the transcript — two `tool_call_start` events for the same already-confirmed field is a far stronger repetition signal than guessing from surface text.

---

## `heuristics.py` — deterministic checks, no LLM

`backend/supervisor/heuristics.py`. All closed token sets or substring matches.

| Function | Used by | Catches |
|---|---|---|
| `is_explicit_human_request` | dispatcher, `capture_fast` | Twelve phrases: "talk to a human", "real person", "transfer me", ... |
| `looks_like_tangent` | `capture_fast` gate | Empty, ends with `?`, or starts with one of ~20 prefixes ("what", "wait", "actually", "can you", ...) |
| `looks_like_field_shape` | `capture_fast` gate | email: contains `@`, " at ", or " dot ". phone: contains a digit. name: always `True`. |
| `looks_like_research_skip` | `research_gather` | "let's just book", "rather not say", "skip that", ... |
| `looks_like_bare_affirmation` | `research_gather` | Utterance made **entirely** of acknowledgment words |
| `looks_like_acknowledgment` | filler-interrupt guard | Same idea, more generous token set |

### Two things about the token sets

**`looks_like_field_shape` is deliberately looser than the validators.** `validate_email` checks an already-normalised `"user@domain.com"`; the raw utterance is `"manos at gmail dot com"`. Running the validator against raw speech would reject almost every genuine spoken email. The gate only asks "is it safe to guess this utterance is even attempting the field" — an invalid-but-plausible attempt still advances optimistically and gets caught for real by the background verification.

**`_ACKNOWLEDGMENT_TOKENS` is built explicitly, not by extending `_BARE_AFFIRMATION_TOKENS`** — and it deliberately **excludes the negations**. "no" / "nope" / "nah" spoken over a filler is almost always a correction the caller needs heard, not a backchannel; real backchannels are affirmative by nature. Getting that wrong would swallow a decline on exactly the booking-confirm turn where a decline matters most.

Conversely, `_BARE_AFFIRMATION_TOKENS` must stay narrow: it gates whether the caller actually answered the research question, and widening it would start swallowing real, terse answers to "tell me what happened".

---

## `faq.py` — the side-question deflector

Four entries (weekend hours, office address, fees, consultation duration), each a keyword list plus a canned answer. `match_faq(utterance)` returns the first match or `None`.

Called **once per turn from the dispatcher**, unconditionally, against the raw utterance — the FAQ answer is prepended to whatever the node decided to say. That placement matters: no node-specific tool looks at anything but the part it cares about, so this is the only point where a tangent can be caught regardless of which node ran.

Deterministic on purpose. The knowledge base is tiny and fixed; an LLM round trip to classify against four entries would be pure latency.

---

## `knowledge/` — the statute corpus

```
search_statute_candidates(area, query)
  → bm25_search(query, load_corpus(area), top_k=3)
  → [{id, citation, jurisdiction, topic_tags, text, score}, ...]
```

**Retrieval** is Okapi BM25 in plain Python (`BM25_K1=1.5`, `BM25_B=0.75`), over `entry["text"] + entry["topic_tags"]`, with a short custom stopword list. Not embeddings, not a vector DB — at 8–12 entries per area that is the right-sized mechanism, and it adds no paid or heavy dependency. The stopwords are there because common function words ("to", "is", "my") occasionally overlapped enough with an unrelated query to push an entry above the relevance floor by chance.

**The floor** is `BM25_RELEVANCE_FLOOR = 2.0` in `tools.py`. Below it, the grounding Claude call is skipped entirely — no candidates means no citation, and no spend. Calibrated against the actual corpus: genuine matches score ~2–6, off-topic utterances mostly 0.

**Grounding** is closed-set selection only. `ground_statute_citation` may return an `id` from the candidate list or `null`, plus a one-or-two-sentence spoken framing grounded only in that candidate's own text. `_search_statutes_in_background` then verifies the returned id is genuinely in the candidate set before using it. **The agent cannot invent a citation** — that is a structural property, not a prompt hope.

Corpora are plain JSON, loaded once per process and cached. They are reference data in the same category as `eval/error_classes.py` — rule 9 does not apply.

---

## `fillers.py` — what the agent says while thinking

```python
FILLER_PHRASES = {
    "confirm_field":   ("Okay, one sec.",        "Still with you."),
    "confirm_booking": ("Let me check that.",    "Still checking."),
    "propose_slot":    ("Let me get that booked.", "Still working on it."),
}
FILLER_IDLE_DELAY_SECONDS = 0.4    # continuous idle before line [0]
FILLER_REPEAT_SECONDS     = 4.0    # before line [1]
VOICE, VOICE_SPEED = "marin", 1.1
```

`filler_for_state(state)` reads the **pre-turn** state and predicts which of three call sites the turn will reach: a pending confirm-back in the drain phase, a proposed slot awaiting yes/no, or a fresh time request that will end in a confirmation summary. Everything else returns `None`.

Three design points that are easy to undo by accident:

1. **The phrases are fixed, never model-generated.** Generating one would cost a 200–400ms round trip, defeating the purpose of a line meant to start immediately.
2. **They are pre-rendered to WAV**, not synthesized at call time, and committed. LiveKit's `session.say()` refuses text-only input under a `RealtimeModel` (the OpenAI plugin reports `supports_say = False`); the `audio=` branch bypasses that guard. Pre-rendering also keeps OpenAI Realtime as the session's only speech model — attaching a TTS plugin purely to unlock `say()` would have pulled a second voice into the call. **`VOICE` and `VOICE_SPEED` here are the single source of truth for both the live session and the WAVs; change either and you must re-run `scripts/generate_filler_audio.py`.**
3. **There are two lines, not one.** A spoken filler was built in Phase 2 and removed after live testing as *worse* than silence: a promise followed by dead air reads as more broken than a brief unannounced pause. That finding still stands. The second line answers it — the wait is re-acknowledged rather than promised once and abandoned — and is deliberately reassurance with no new promise in it ("Still with you.", not "Almost done!").

`_fallback_to_real_capture` can also reach `confirm_field_answer`, but nothing in the pre-turn state predicts it — that is the point of the fast path, it decides mid-turn. Those turns get no filler. Scoping to what is actually predictable is what keeps this a deterministic lookup rather than a guess.

---

## Adding a tool

1. Write the function in `tools.py`. If it calls Claude, use `call_claude_json` (with a JSON schema constant) or `call_claude_text` — **never the SDK directly**; pre-commit blocks it.
2. Add its system prompt to `prompts.py`.
3. Call it from the node via `call_claude_tool(repos.trace, call_id, node, "tool_name", tools.your_fn, ...)`, or `traced_call(...)` if it is deterministic. Both, never neither.
4. Wrap the call site in `try/except LLMCallFailed` → `_llm_failure_fallback`.
5. Only bind it in the node whose stage needs it (rule 5).
6. If you want it on Haiku, pass `model=HAIKU_MODEL_ID` **and** validate with `eval/compare_runs.py` before shipping.
7. Test it: a unit test for the function, and a node test asserting the branch it drives. See [`testing.md`](testing.md).
