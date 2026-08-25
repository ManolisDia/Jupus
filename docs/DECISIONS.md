# Decisions

Short log of *why*, not *what* — so a later session doesn't quietly re-decide something already ruled out. Add an entry whenever a non-obvious architecture call is made or revisited.

---

### `ask_supervisor` is one coarse dispatch tool, not many fine-grained tools on Realtime
Realtime natively supports tool-calling, so it could call `book_consultation`/`check_availability`/etc. directly. We chose a single dispatch tool instead because Realtime models are tuned for naturalness/speed, not for reliably sequencing multi-step business logic (confidence handling, retries, booking conflict resolution) — that's exactly what this brief grades. Routing all of that through a separate Claude-driven LangGraph supervisor makes the logic deterministic, testable, and debuggable, at the cost of one extra hop of latency per "real" turn — mitigated with async dispatch (see below) rather than blocking.

### email/phone are always confirmed back, regardless of confidence — supersedes PLAN.md's threshold-only description
`apply_extraction` (`backend/supervisor/graph.py`) never returns `"confirmed"` for email/phone
on first extraction, no matter how high the confidence or how well-formed the value looks — it
always goes to `pending_confirm` and gets read back to the caller, only becoming `"confirmed"`
once they explicitly say yes. `name`/`preferred_time` keep the original confidence-threshold
behavior (≥0.75 auto-confirms). Decided after repeated live-testing failures during Phase 3: both
the Realtime model's own transcription-to-argument pipeline and (independently) Claude's
extraction confidence proved unreliable for catching malformed emails/phone numbers — see the
entry below on `last_caller_utterance`. Since a wrong email/phone means the firm can't reach the
caller back, the cost of always confirming (one extra turn per field) is worth it; a wrong
name/time is comparatively low-stakes and doesn't need the same treatment.

**Refined, not reversed, after a live session surfaced a gap this entry didn't cover**: "always
confirmed back regardless of confidence" originally meant every non-empty, correctly-formatted
email/phone value proceeds straight to a Claude-generated confirm-back (`generate_confirm_back`,
`CONFIRM_BACK_PROMPT`'s soft "spell out ambiguous characters if it would help" instruction). Live
testing showed that soft instruction isn't reliable — a caller reported the agent read back their
phone number spelled out but not their email, on the same call. `graph.LOW_CONFIDENCE_CONFIRM_
THRESHOLD` (0.75, same bar `apply_extraction` already uses elsewhere) now gates this: below it, a
well-formed email/phone value is treated the same as an invalid one — re-asked with a fixed,
deterministic "please spell that out" reply (`SPELL_OUT_REPLIES`), never left to Claude's
discretion — instead of proceeding to `pending_confirm`. Once confidence clears that floor
(whether on the first attempt or a spelled-out retry), the original behavior described above is
unchanged: always confirmed back, never auto-trusted. Same lesson as the Haiku→Sonnet entry below:
a "the model should reliably do X when it matters" instruction needs enforcing in code, not left
to prompt phrasing, once there's live evidence it doesn't hold reliably enough.

### `last_caller_utterance` is authored by the Realtime model, not a raw ASR transcript
Discovered during Phase 3 live testing: `ask_supervisor`'s `last_caller_utterance` argument is
not a passthrough of what OpenAI's speech recognition literally heard — it's a string the
Realtime *model itself* generates when constructing the tool call, same as any other function
argument. Observed live: a caller said "manos44" for their email and the value that reached
`extract_field` was `manos44@example.com` — the model had invented a plausible domain and
inserted an `@` neither said nor implied. This meant a supposedly `capture_failed`-triggering
malformed email got silently "fixed" upstream of anything `backend/supervisor/tools.py` or its
prompts could see or control, defeating the confidence-threshold/validation pipeline entirely.

Initially "fixed" via a stricter tool-schema `description` and a matching `SUPERVISOR_INSTRUCTIONS`
rule — but this remained unreliable (prompting the Realtime model is not a guarantee). Properly
closed instead by not depending on the model's argument at all: `client/app.js` now enables
`session.audio.input.transcription` (`gpt-transcribe`) and captures
`conversation.item.input_audio_transcription.completed` events into `lastVerbatimTranscript`,
which is sent to `/bridge` as `last_caller_utterance` in place of the model-authored argument
whenever available. This is real ASR output, not something an LLM can "helpfully" edit.

### Supervisor's Claude calls use `claude-sonnet-5`, not Haiku — upgraded from an initial Haiku choice
Originally set to `claude-haiku-4-5` for latency (these calls are still synchronous/blocking per
turn until Phase 5's async dispatcher exists) and because the tasks looked simple enough — 4-way
classification, single-field extraction, a short confirm-back question. Live Phase 3 testing
showed this was the wrong tradeoff: Haiku repeatedly failed to reliably follow a precise
instruction (converting spoken "at"/"dot" into `@`/`.` symbols) even after several rounds of
prompt tightening — the extraction would sometimes pass through the literal words unconverted,
failing `validate_email`/`validate_phone` and eventually escalating a call that should have
succeeded. Upgraded to `claude-sonnet-5`, which followed the same instruction correctly. Revisit
again (up to Opus) if Sonnet also proves unreliable; the model id is still a one-line change.

### Graph edges are code conditionals, not LLM choice
Deciding *which node runs next* (routing → capture → booking → escalation) is plain `if/else` on `CallState`, never an LLM decision. This keeps the flow testable with unit tests and predictable in the video demo. LLM judgment is scoped narrowly to *what a specific field/classification is*, never to *what should happen next*.

### Semantic turn detection: OpenAI Realtime's built-in `semantic_vad`, not a custom model, not a third-party package
Originally rejected building a custom turn-detection model from scratch for this scope — that call stands, still too much effort/risk for a one-week build. But "simple silence-duration VAD" isn't the only built-in option: OpenAI Realtime supports a `semantic_vad` turn-detection mode that uses the model's own judgment of utterance completion rather than a fixed silence timeout — it's what actually solves "caller says umm and pauses to think" without a bespoke model. Third-party turn-detection packages (LiveKit's open-source turn-detector, Pipecat's smart-turn) were considered and rejected — they're designed for pipelines where you own the raw audio stream directly, and plugging one in would mean disabling Realtime's own turn handling and rebuilding the chained-pipeline turn-taking we deliberately ruled out when choosing Realtime over a chained architecture in the first place. Femca also goes through OpenAI Realtime directly, not a third-party layer, so this stays consistent with existing usage.

**Core scope (Phase 1):** `semantic_vad` turned on with one fixed eagerness setting for the whole call.
**Stretch (Phase 5, only if time allows):** dynamic per-stage eagerness — patient during field capture/spelling, snappier during yes/no confirmations — pushed via `session.update` whenever the LangGraph stage changes. Genuine engineering judgment on top of the platform feature, not required for the core submission.

### No telephony, no Docker, no Railway hosting in v1
The brief explicitly doesn't require telephony and explicitly requires local runnability on a normal laptop. Docker was considered for "easy to run" but rejected because containerizing local mic/speaker access is a known cross-platform pain point, and a browser/WebRTC client already solves "easy to run" more simply. Railway hosting is a convenience add-on only, not a replacement for local runnability — a public endpoint tied to a paid Realtime key is also a real cost/abuse risk if left running, so it's explicitly last-priority and must be gated (access token + spend cap) if attempted at all.

### Async dispatcher defers/drops stale results instead of always speaking them
The requirement was that the caller can keep talking while a supervisor call is in flight — not just a "please hold" filler. The dispatcher fires supervisor calls as background asyncio tasks and never blocks on them. When a result resolves, it's only spoken immediately if the caller isn't mid-speech; otherwise it's queued and delivered on the next natural gap. Results are also staleness-checked against current call state before delivery — if the caller has moved the conversation on, a queued result is dropped rather than spoken out of context.

### Realtime's system instructions always defer to `ask_supervisor`, never answer substantively on their own
Drafted explicitly in Phase 2 (not left as a vague "be helpful" prompt) because this is the one piece of prompt content that gates whether the entire observability/eval stack (traces, error taxonomy, admin panel) sees anything at all — if Realtime free-styles a legal-sounding answer without calling the tool, nothing downstream ever knows it happened. The instructions explicitly forbid stating legal information, confirming bookings, or inventing firm details on its own, and require calling `ask_supervisor` even when it "thinks it already knows" the answer.

### No filler acknowledgment ("let me check that" / "one moment") while waiting on `ask_supervisor` — reversed after live testing
Originally the instructions allowed a short filler ("let me check that for you") while waiting on the supervisor, and Phase 2's DoD confirmed the Realtime model genuinely could speak that acknowledgment and call the tool in the same turn — so this wasn't a technical limitation. It was removed anyway: the actual caller experience of a spoken promise ("one moment") followed by dead air until the real reply eventually lands reads as *more* broken than a brief, unannounced pause. The fix isn't a better filler phrase, it's not narrating the wait at all — when the supervisor's reply arrives, it's delivered as the agent's next natural conversational turn, not as the payoff to an earlier promise. `semantic_vad` (Phase 1) and the non-blocking dispatcher (Phase 5) are what keep the actual gap feeling human-paced; the instructions no longer try to paper over it verbally.

### `interrupt_response: true` kept — a guarded retry, not disabling interruption, fixes dropped tool calls
The Phase 1 fix for `semantic_vad` misdetecting background noise as speech (see the flagship-model
entry below) lowered `eagerness` and added `near_field` noise reduction, but kept
`interrupt_response: true` for barge-in. During live Phase 5 testing, the same false-trigger
pattern resurfaced with a worse consequence: `interrupt_response: true` cancels the in-flight
response the instant any further speech is detected — and when that response was mid-way through
building an `ask_supervisor` function call, the cancellation silently dropped the tool call
entirely (confirmed via captured `response.function_call_arguments.delta` events that never
reached a `.done`, and zero corresponding backend/Claude activity in `backend.log`). This read to
the caller as the agent saying its canned line, then going dead — nothing said, nothing asked
again, no recovery.

First fix attempt was `interrupt_response: false`, on the reasoning that the Phase 5 async
requirement doesn't need mid-response interruption anyway (by the time the caller speaks a
follow-up, the agent has usually finished talking and is idle waiting on the backend). Reverted
almost immediately: with interruption disabled, a genuine overlapping utterance (caller starts
talking again before the previous response has technically reached `response.done`) still gets
its own auto-created response from `create_response: true`, but nothing cancels the old one to
make room — the Realtime API hard-rejects the second `response.create` ("Conversation already has
an active response in progress") and the session errors out, tearing down the call. That's a
worse failure than the one being fixed, and directly breaks the scenario the async dispatcher
exists to support.

Kept `interrupt_response: true`. Second fix attempt added a `response.done` handler that retried
via a bare `response.create` when a response ended `cancelled`/`incomplete` with no completed
function call — guarded by a `responseActive` flag (set on `response.created`, cleared on
`response.done`) meant to skip the retry when the caller's own next utterance had already gotten
its own auto-created response. Also reverted: `responseActive` is a single shared boolean, not
scoped per response ID. If the interrupting utterance's `response.created` arrives before the
cancelled response's own `response.done` — plausible, event ordering isn't guaranteed — the `done`
handler clears the flag to `false` even though a *different*, newer response is genuinely active,
and the retry fires `response.create` on top of it, reproducing the exact same
"already has an active response in progress" error live testing kept hitting.

Removed the retry entirely rather than attempt per-response-ID tracking blind (this session has no
way to observe the actual client-side event stream live, only reconstruct it after the fact from
what the user reports and a manually-armed capture — not reliable enough to get a racy fix right).
Net position: `interrupt_response: true` for real barge-in, no client-side retry-on-cancel at all.
Losing an occasional turn to a rare spurious `semantic_vad` cancellation (caller has to repeat
themselves) is a far smaller failure than the retry's own risk of crashing the whole call.

### Small static FAQ knowledge base — a deliberate scope addition beyond the original Phase 5 spec
Live testing surfaced a real usability problem the original design didn't account for: a caller
side-question that doesn't extract to whatever field/classification is currently pending (e.g.
"are you open on weekends?" asked mid-booking) was silently discarded — the relevant node just
re-asked its own question verbatim, reading as the agent ignoring the caller outright. Two scope
options were considered: (a) a small hand-authored FAQ list with deterministic keyword matching, or
(b) a much larger rearchitecture toward free-flowing conversation (an LLM deciding how to handle
arbitrary tangents). Option (b) was explicitly rejected — it conflicts with this project's core
architecture doctrine (deterministic graph edges, no LLM picking what happens next) and is a much
bigger change than a take-home warrants. Went with (a): `backend/supervisor/faq.py`, a handful of
static entries (hours, address, fees, consultation length) matched via plain keyword substring
checks — no Claude call, same reasoning as `heuristics.py`'s `is_explicit_human_request`. Checked
centrally in `dispatcher.process_supervisor_call` against every caller utterance, regardless of
whether the node's own logic succeeded or failed that turn — a caller can tack a genuine aside onto
an otherwise-answerable utterance in the same breath, and nothing node-specific (`extract_field`,
`classify_practice_area`, etc.) ever looks at anything but the part it's asking about. Explicitly a
narrow deflect-and-return mechanism, not general Q&A — anything outside the fixed list still falls
back to the original (silent) reprompt behavior, which is a documented, accepted limitation, not
something this change attempts to solve.

### No cap on session/call duration
Considered a hard server-side timeout on call length (protects against a stuck loop or an abandoned tab with a live mic). Explicitly decided against — not part of this build. If cost or runaway-session risk becomes a real concern later (e.g. if a hosted demo is ever stood up), address it there specifically rather than constraining every local test call now.

### Realtime model: staying on flagship `gpt-realtime-2.1`, tried and reverted from `gpt-realtime-2.1-mini`
During Phase 1 live testing, replies sometimes took 7-8 seconds and felt sluggish. First
hypothesis was model choice, so `gpt-realtime-2.1-mini` (OpenAI's faster/cheaper sibling,
released alongside flagship in July 2026) was tried. The actual root cause turned out to be
unrelated to the model: background noise (e.g. a sniff) was being misdetected as speech by
`semantic_vad`, triggering false interruptions and re-generated responses that read as "long/
slow" turns. Fixed by adding `session.audio.input.noise_reduction: {type: "near_field"}` and
lowering `turn_detection.eagerness` to `"low"` (see `client/app.js`'s `sendSessionUpdate`) — not
by a model swap. With the real cause fixed, reverted to flagship `gpt-realtime-2.1` since it was
already working well once retested and there was no remaining evidence pointing at mini being
needed.

Rule 1 above still means Realtime never does classification, extraction, confidence scoring, or
booking logic — all business reasoning is offloaded to Claude via the LangGraph supervisor — so
mini remains a plausible cost/latency optimization worth trying again later if flagship proves
slower than needed once real tool-call round trips exist (Phase 2+). Not revisited now — this
was a live A/B, not a settled rejection; if revisited, `backend/app.py`'s `REALTIME_MODEL` is a
one-line change.

### Case research (Phase 8) — jurisdiction, corpus provenance, and why it can never invent a citation
Three static per-practice-area statute corpora (`backend/supervisor/knowledge/{employment,tenancy,immigration}_statutes.json`,
8-10 entries each) are all **England & Wales** law, hand-authored during
implementation from general knowledge for this take-home — not scraped, not
generated at runtime by an LLM, and not independently verified against
primary legal sources. This is stated plainly here, and every delivered
citation is followed by a fixed spoken disclaimer ("this is general
information, not legal advice, but it's worth mentioning to the attorney")
precisely because of that provenance — this is a demonstration of a
retrieval mechanism, not a production legal research tool.

Two things keep the feature from ever inventing a citation, deliberately
layered rather than relying on either alone: (1) a BM25 relevance floor
(`tools.BM25_RELEVANCE_FLOOR`) means most utterances during the research
stage never even reach an LLM call — nothing to ground means nothing to
risk hallucinating; (2) when the floor is cleared, the one Claude call
(`ground_statute_citation`) is closed-set selection only — it's handed the
top BM25 candidates' exact `{id, citation, text}` and must return one of
those exact ids or `null`, never freeform text, and the caller
(`dispatcher._search_statutes_in_background`) additionally verifies the
returned id is actually one of the candidates it was given before trusting
it. See `docs/phases/phase-8-legal-research.md` for the full design.

Retrieval itself is plain-Python BM25 over the corpus, not embeddings or a
vector DB — right-sized for 8-10 entries per area today. Flagged as a
deliberate future consideration, not decided now: if the corpus grows
substantially (more areas, many more entries, denser text) or keyword
match starts missing paraphrased situations, a local sentence-transformer
embedding + cosine search is the natural next step; still no vector DB
needed until the corpus is far larger than "a few dozen entries per area."

### Phase 9 hosted deployment: SQLite on a Railway Volume, not a Postgres migration
Restated in full here per that phase's own instruction, since this is where the earlier design
conversation's reasoning gets formally recorded (`docs/phases/phase-9-hosted-deployment.md`'s
Decision 1). `backend/db/repositories/__init__.py` documents the `db_backend: Literal["sqlite",
"postgres"]` seam but never implements the Postgres branch — a Railway Volume mounted at the same
path `JUPUS_DB_PATH` already points to (`/data/calendar.db` in the live deployment) needed zero
repository-layer code changes; `connect(settings.db_path)` behaves identically whether that path
is ephemeral local disk or a persistent Volume. Postgres would only pay off once the in-memory
dispatcher state (`CALL_STATES`/`LOCKS`/`SPEAKING`/`DEFERRED`/`CONNECTIONS`) is also externalized
for horizontal scaling — deliberately out of scope, since this deployment is explicitly
single-instance (see the next entry). Verified live: booked slots and call history survived a
`railway redeploy`, confirming the Volume — not the container's ephemeral disk — is what's
actually being read and written.

### Phase 9 hosted deployment: shared-secret access gate — deters casual discovery, not a real auth boundary
A deployed `/session` endpoint mints real, paid OpenAI Realtime ephemeral tokens on request — the
moment its URL is known, it's a direct spend/abuse vector. `JUPUS_ACCESS_TOKEN` (a plain string
env var) is checked as a `?access_token=` query param on `/session`, `/bridge`, and every route
under `/admin`/`/admin/annotate` (a path-prefix middleware, since `/admin` is served via
`StaticFiles` and can't take a per-route dependency the same way) — no-op when unset, so local dev
is completely unaffected. This is explicitly **not** a production authentication system: the token
is embedded in the deployed client's `config.js` and is trivially visible to anyone who inspects
page source. The actual threat model this closes is a URL getting crawled or casually shared and
racking up spend from strangers who were never given the link — not a determined attacker. The
real backstop against that residual risk is billing spend caps/alerts on both the OpenAI and
Anthropic accounts, confirmed set before the first live deploy (this phase's Decision 4) — the
access gate raises the bar, the spend cap is what actually limits the damage if it's ever cleared.
Also carries `PUBLIC_CLIENT_ORIGIN`-scoped CORS (locked to the real Firebase origin instead of
`"*"` once one exists) and Railway configured for exactly one instance, no autoscaling — the
in-memory dispatcher state (previous entry) is a hard single-instance constraint this deployment
makes load-bearing rather than theoretical.

### Realtime (OpenAI) + Supervisor (Claude) — two vendors, deliberately
OpenAI Realtime has the most mature WebRTC/tool-calling/interrupt handling of the available realtime voice APIs. Claude powers the supervisor's reasoning (extraction, classification, summarization). Two API keys is an accepted tradeoff, documented clearly in the README since the brief requires documenting exactly what's needed to run the project.

### Phase 11: latency split into four stages, `deferred_wait` kept separate from `supervisor_processing`
Chosen to match where a caller actually perceives delay, not where the code happens to have a
convenient boundary: `stt_and_dialogue_decision` (caller stops talking → `ask_supervisor` received
by the backend — Realtime's own STT/turn-decision pipeline, a black box from this project's side),
`supervisor_processing` (the graph/Claude round-trip itself), `deferred_wait` (Phase 5's "caller
was still talking" queue delay), and `tts_first_audio` (reply handed to Realtime → caller hears
the first bit of the spoken response). `deferred_wait` is deliberately never folded into
`supervisor_processing` — conflating "how long did Claude take" with "how long did we wait for a
natural gap to speak" would misattribute a scheduling artifact as model latency, and the two have
completely different fixes if either turns out to be the bottleneck (a slow `supervisor_processing`
p95 points at prompt/model tuning; a slow `deferred_wait` p95 points at how aggressively the
dispatcher waits for a pause). This replaced a metric (`processing_latency_percentiles`) that had
been silently dead since Phase 6a — see `docs/fixes/2026-08-24-012.md`.

**Revised after live testing**: the phase doc's original plan for `tts_first_audio` was a
`response.audio.delta`/`response.output_audio.delta` data-channel event. Confirmed live that this
never arrives over WebRTC — zero `tts_first_audio` events recorded across a real call despite every
other new event type (including `realtime_usage`, wired the same way) firing correctly. OpenAI's
own WebRTC guide notes the peer connection handles audio playback for you rather than surfacing
per-chunk events the way the WebSocket transport does. `client/app.js` now detects first-audio
instead via the remote-stream amplitude analyser the caller-facing visualizer already runs every
animation frame (Phase 7) — the first frame where `remoteAmp` crosses the same `agentSpeaking`
threshold already driving the "Agent speaking…" UI state, while `awaitingFirstAudioDelta` is still
true, is reported as `tts_first_audio`. Trades a little precision (one frame's worth of latency,
plus the analyser's own smoothing) for actually working under this project's real transport. Known
edge case, not yet confirmed live: if the remote track is still audibly above threshold from the
tail end of the *previous* response when a new one starts, the very next frame could report a
near-zero value for that turn — accepted as a measurement caveat of this approach rather than
solved, since eliminating it would need actual silence-detection, not just an amplitude threshold.

### Phase 11: cost captured at the source, never estimated from duration or turn count
Claude token usage is captured server-side, right where the Anthropic API response already arrives
(`call_claude_json`/`call_claude_text` in `llm_utils.py`, stashed in a `threading.local()` and read
by `call_claude_tool` — chosen over threading `call_id`/`trace_repo` through every `tools.py`
function that calls them, which would be a much bigger diff for the same result; safe because
`call_claude_tool`'s call to `fn` and any nested Claude call inside it always run synchronously in
the same OS thread, and the stash is cleared before every attempt, not just after a successful
read, so a failed-then-retried call can never leak a stale value into an unrelated later call on a
reused worker thread). Realtime token usage is only ever visible to whichever side holds the OpenAI
session (`client/app.js` for WebRTC today), since `response.done`'s `usage` object is a property of
that session — relayed to `/bridge` as a `realtime_usage` message for *every* response, including
ones that never touched `ask_supervisor` (the opening greeting still costs real tokens). Neither
source is ever inferred from `duration_ms` or turn count — a slow turn isn't necessarily an
expensive one. `eval/pricing.py`'s $/million-token constants are hardcoded and must be
re-verified against current published pricing before trusting any dollar figure this project
displays — every place a cost is shown is labeled "estimated" in the UI/text itself, not just a
footnote.

### Phase 12: concurrency stress-tested at the dispatcher/asyncio/db layer, not through the full transport stack
`eval/concurrency_stress_test.py` and `backend/tests/test_concurrency_stress.py` fire N distinct
`call_id`s at `backend.dispatcher.process_supervisor_call` directly via `asyncio.gather`, rather
than driving N real browser tabs each holding a real WebRTC session and a real OpenAI Realtime
connection. The latter would mostly measure OpenAI's/the network's own concurrency handling, not
this project's — the same reasoning `eval/replay_scenarios.py` and `backend/tests/
test_scenarios.py` already apply when they call `process_supervisor_call` as "the real,
unmocked pipeline" for their own purposes. This is the one layer whose concurrency behavior is
actually this project's own engineering: a distinct `asyncio.Lock()` per `call_id`
(`dispatcher.get_lock`), and each `GRAPH.invoke` dispatched off the event loop via
`asyncio.to_thread` (Phase 5's design, since LangGraph's node functions make blocking Claude
calls).

Two genuine bottlenecks this exposed, both already-known, deliberate tradeoffs (SQLite as a local
single-writer DB is named in the README's "Known limits"; the default `asyncio.to_thread` executor
cap was flagged as a testable hypothesis, not an assumption, in `docs/phases/
phase-12-concurrency-stress-test.md`'s Decision 3) — reported here plainly rather than only
showing the N levels that looked good:

- **SQLite single-writer contention.** `eval/concurrency_stress_test.py` deliberately runs against
  a real SQLite-backed `Repositories` (not the in-memory fakes `backend/tests/test_scenarios.py`
  uses), specifically so `repos.calls.upsert`/`repos.trace.record_event` exercise SQLite's actual
  locking under concurrent writes rather than a fake that can't show this. A real, mocked-Claude
  run on this machine (20 logical CPUs) showed per-call median latency already at ~172ms at N=5
  (well above the simulated ~50ms of node work per call), climbing to ~406ms at N=20 and ~672ms at
  N=40 — visible well before N=20 crosses this machine's thread-pool cap (below), so at least part
  of this is SQLite write serialization, not thread-pool queuing alone.
- **`asyncio.to_thread`'s default executor cap** (`min(32, os.cpu_count() + 4)` — 24 on this
  20-core machine) is a hard ceiling on how many `GRAPH.invoke` calls can genuinely run in
  parallel; `backend/tests/test_concurrency_stress.py::test_thread_pool_saturation_detected_at_high_n`
  configures a small custom executor (`max_workers=2`, patched in via `asyncio.to_thread` itself,
  not `loop.set_default_executor` — see that test's comment on why the latter isn't safely
  reversible) to make this ceiling reachable and assert it's detected, without needing 32+ real
  threads. Raising the real ceiling in production is a one-line change:
  `loop.set_default_executor(ThreadPoolExecutor(max_workers=N))` at startup.

### Phase 13: prompt caching shipped, measured, and confirmed to have zero effect on this project's real system prompts — kept anyway
`call_claude_json`/`call_claude_text` (`backend/supervisor/llm_utils.py`) send the system prompt as
a `cache_control: {"type": "ephemeral"}` content block rather than a plain string, on the reasoning
that `prompts.py`'s per-node system prompts are static and resent every turn. A live `eval/
replay_scenarios.py --label phase13-baseline` run (2026-08-25, all 8 canonical-scenario calls, real
Claude API) showed `cache_write_tokens=0, cache_read_tokens=0` on every single `llm_usage` event —
including tool names called 8-10 times in the same batch with an identical system prompt
(`classify_practice_area`, `confirm_field_answer`). Root cause, confirmed against Anthropic's
current published prompt-caching docs: a system block must be **at least 1024 tokens** (Sonnet-class
minimum) before it's eligible for caching at all — anything shorter is silently never cached, no
error, no signal. This project's node system prompts, judged by the same batch's `input_tokens`
figures (roughly 100–650 tokens total per call, system prompt included), sit well under that floor.
This was measured, not assumed, precisely because Decision-making-by-assumption is what this whole
latency-reduction effort started by rejecting (see the LiveKit/filler-masking conversation that led
to this phase).

**Kept in place rather than reverted** — it costs nothing (Anthropic ignores `cache_control` below
the minimum, no error, no latency/pricing penalty for the attempt) and it correctly reports zero
cache activity rather than silently mispricing anything, so there's no downside to leaving it wired
up. It becomes a real, zero-further-effort win the moment any node's system prompt grows past 1024
tokens (plausible if Phase 14's filler/interrupt logic or a future stretch adds a substantially
richer prompt to a node) — this is not speculative future-proofing kept "just in case" so much as an
already-paid-for mechanism sitting dormant until a real trigger. The actual Phase 13 latency win, per
this finding, has to come from the other three levers in `phase-13-latency-reduction.md` (merging
sequential Claude calls, the retry-tail root cause, per-tool model choice) — none of which depend on
prompt length.

Neither bottleneck is fixed as part of this phase — measuring and reporting them honestly is the
job here, per `docs/phases/phase-12-concurrency-stress-test.md`'s own non-goals. See
`docs/answers.md`'s Q3 for the full per-N table.

**One real, capped live-API run** (`--mode live --n-levels 5`, 2026-08-25): 5 concurrent real
Claude calls, wall-clock 2937ms vs. ~2203ms for one call's own median — consistent with the mocked
evidence's shape (parallel cost barely above a single call, not N×) against a real API round-trip
instead of a simulated one. All 5 independently resolved the correct practice area with no
cross-call mixing in the stored transcripts, though a single live turn only reaches classification
and the first capture question — narrower leakage coverage than the mocked sweep's fully-populated
profiles, since less state exists yet to check for contamination.
