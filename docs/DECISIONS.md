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

### Phase 13: `extract_field` + `generate_confirm_back` merged into one call — the originally-planned second merge doesn't exist
`node_capture`'s fresh-extraction branch (`backend/supervisor/graph.py`) made two sequential Claude
calls whenever a field needed confirming: extract, then a separate round trip for the confirm-back
phrasing. `tools.extract_and_confirm_field` now does both in one JSON-schema call. Real live-call
measurement (`replay-s4-62114f1c` baseline vs. `phase13-merge-capture`) showed the identical turn
shape going from two round trips (`extract_field` 1522ms + `generate_confirm_back` 2552ms = 4074ms)
to one (`extract_and_confirm_field` 3221ms) with zero error-class regression — ~21% faster, and
structurally more significant than the percentage: one fewer full request/response cycle, and one
fewer place for the retry tail (below) to strike.

This doc's phase-13 planning originally also claimed a second merge —
`select_offered_slot`+`generate_confirmation_summary` in `node_booking` — as a mirror-image
opportunity. **Found to be wrong while implementing**: re-reading `node_booking` in full shows
`select_offered_slot`'s branch never calls `generate_confirmation_summary` at all (it books directly
and replies with a fixed string); `generate_confirmation_summary`'s actual neighbor is
`extract_datetime`, separated by a deterministic `check_availability` call whose *result* (which
slot, if any, is available) `generate_confirmation_summary` needs as input — genuinely blocked by a
data dependency, not just unexplored. A second real capture-side candidate,
`_finish_fast_pass`'s own extract+confirm-back pair, was also considered and deliberately left
unmerged: unlike `node_capture`'s branch, its confirm-back can be for an *earlier* field than the one
just extracted (Phase 7's background verification can resolve an earlier field first), so merging
would need either drafting confirm-back phrasing for a value the model never saw, or conditional
fallback logic — real complexity risk in code with a documented history of confirm-back
misattribution bugs (`docs/fixes/`), for the single-call saving on the least-frequent capture turn.

### Phase 13: `confirm_field_answer`'s retry-driven latency tail — root-caused via real trace data, fixed at the prompt, not the token ceiling
A live call's `trace_events` (`call_id=56e57e0f-...`) showed `confirm_field_answer`'s `output_tokens`
climbing unpredictably (201, 351, a truncated failure, 274, 178, 30) for what should be a compact
3-field JSON object, while a caller repeatedly re-spelled a garbled email. The failing call's
`corrected_value` generation grew long enough to exceed `call_claude_json`'s `max_tokens=512`
mid-generation, producing invalid truncated JSON, triggering the existing retry-once path — two
~7.5s failed attempts is exactly where the previously-observed 10s+ max came from.
`confirm_booking_answer` (boolean-only schema, no free-text field) showed zero retries in the same
data, confirming the fix belongs on `CONFIRM_FIELD_ANSWER_PROMPT`'s verbosity, not the retry
machinery or a larger token ceiling — raising `max_tokens` would let the wasteful generation succeed
without making it any faster. See `docs/fixes/2026-08-25-002.md`.

### Phase 13: per-tool model choice via a thread-local override, not a project-wide `MODEL_ID` change — one candidate shipped, three left for follow-up
`call_claude_tool` gained an optional `model=` kwarg (a thread-local stash cleared in `finally`, same
shape as Decision 8's usage-capture stash) so a specific tool call can use `HAIKU_MODEL_ID` without
touching the project-wide default. `docs/DECISIONS.md`'s existing Haiku-rejection entry is scoped
to free-text extraction-with-formatting (`extract_field`'s spoken "at"/"dot" → `@`/`.` conversion) —
closed-set classification is a different task shape and was never actually re-tested until now.
`select_offered_slot` (pick an index / decline / needs-clarification from a short offered list)
shipped on `HAIKU_MODEL_ID` after `eval/replay_scenarios.py --label phase13-haiku-select-slot` +
`eval/compare_runs.py` showed zero error-class regression against the Sonnet baseline. Notably,
Haiku returned `needs_clarification: True` for the same ambiguous "Yes, the alternative works."
utterance (against 3 offered slots) that Sonnet's own baseline run *also* returned
`needs_clarification: True` for — identical behavior, not a regression, and this incidentally
surfaces that scenario S3's scripted utterance is genuinely ambiguous against the "offer up to
three alternatives" feature, independent of model choice (not chased further, out of scope here).
On the unambiguous cases, Haiku resolved the same call shape in 1092-1782ms vs. Sonnet's 3500ms in
baseline — roughly 2-3x faster. `confirm_field_answer`, `confirm_booking_answer`, and
`classify_practice_area` remain untested Haiku candidates — one demonstrated, measured swap was
judged sufficient to prove the pattern within this phase, not evidence the others would also be
safe; each needs its own `eval/compare_runs.py` check before switching.

Shipping a real per-call model override also required fixing a latent cost-accounting gap:
`llm_usage`'s recorded `model` field previously hardcoded `MODEL_ID` rather than reading
`response.model`, and `eval/pricing.py`'s cost estimator assumed every Claude call in a batch was
priced at one model's rate. `eval/pricing.py` now has a `CLAUDE_MODEL_RATES` table (Haiku confirmed
at $1/$5 per million input/output vs. Sonnet's $2/$10, 2026-08-25) and `estimate_claude_cost_usd`
prices per-event by whichever model that specific call actually used; an unrecognized model id falls
back to the (more expensive) Sonnet rate rather than silently pricing at zero, so a missing pricing
entry shows up as an overestimate worth investigating, never a hidden underestimate.

### Phase 13 (follow-up): `ground_statute_citation` moved to Haiku — same pattern, second confirmed candidate
Requested directly after identifying `ground_statute_citation` (Phase 8's legal-citation grounding
call) as the single longest individual tool call post-Phase-13 (5047ms in one sample). Same task
shape as `select_offered_slot`: closed-set selection from ≤3 candidates plus one short spoken-framing
sentence, not free-text extraction — the same reasoning that made `select_offered_slot` a safe
Haiku candidate applies here. A "trim the candidate text sent as input" idea was considered and
dropped after actually measuring: the statute corpus entries are already short (200-400 characters,
~50-80 tokens each) and the system prompt is compact (~220 tokens) — there was no real bloat to cut,
and forcing a trim without evidence behind it would have been exactly the kind of
assumption-over-measurement this whole latency effort set out to avoid.

Correctness/timing confirmed via a direct, isolated comparison (not `eval/replay_scenarios.py` — see
the known-issues entry below): the same real utterance against the same candidate set, Sonnet took
4090ms and selected `tenancy-poe1977-s5` (Protection from Eviction Act); Haiku took 1367ms and
selected the identical statute id — same correctness, ~3x faster, consistent with `select_offered_slot`'s
result.

Worth noting: this call is already latency-hidden behind Phase 8's research filler question
(`node_research_deliver` treats "still running" and "nothing found" identically — Decision 4 of that
phase). Speeding it up doesn't reduce caller-perceived wait time; it raises the odds the grounding
finishes before the caller answers the filler, so a real citation is more likely to survive the race
instead of being silently dropped.

**Found in passing, documented, not fully resolved**: `eval/replay_scenarios.py` intermittently
loses this background task entirely when driven through the full scripted S7a/S7b conversation — no
error, no trace event past `search_statute_candidates`'s own `tool_call_start`, no exception. Not
reproducible when the identical utterances are driven directly against `dispatcher.process_supervisor_call`
in isolation (which is how the 4090ms/1367ms comparison above was actually obtained). A partial fix
(explicitly awaiting the background task after the scripted turns, mirroring `test_scenarios.py`'s
own `_await_statute_search` helper) was applied to `replay_scenarios.py` regardless — correct and
necessary, but not sufficient to make the full-script case reliable. See
`docs/known-issues/2026-08-25-003.md`.

### Phase 14: LiveKit Agents for transport, OpenAI Realtime kept as the speech model

**Why not Pipecat (or any chained STT→LLM→TTS pipeline).** Out of scope by construction: the
original choice of OpenAI Realtime was made because it "has the most mature WebRTC/tool-calling/
interrupt handling of the available realtime voice APIs," and a chained pipeline reverses that.
LiveKit was adopted specifically *because* it can host OpenAI Realtime through its own
`openai.realtime` plugin, so the speech model is byte-for-byte the same one Phases 1–13 were tuned
against — same `gpt-realtime-2.1`, same `marin` voice, same `semantic_vad` with `eagerness: "low"`,
same `near_field` noise reduction, all of which have live-testing reasons recorded above. A
transport migration that also swapped the speech model would have made any live regression
impossible to attribute to one or the other.

**Why not stay hand-rolled.** The hand-rolled path worked, but a large fraction of it was
re-implementing turn-taking badly. Deleted outright by this phase: the SDP offer/answer dance, ICE
handling, the `oai-events` data channel, Realtime server-event parsing, `session.update`, the tool
schema, the `responseActive`/`pendingResponseCreate` one-deep collision queue, and the
`transcriptionPending`/`awaitingToolCall` ASR-race fix — plus `dispatcher.py`'s `SPEAKING`,
`DEFERRED` and `CONNECTIONS` bookkeeping. Two of those (the response-collision queue and the ASR
race) each cost a live-debugging session of their own and have their own `docs/fixes/` entries.
They are the kind of thing a transport library should own, and LiveKit does.

**LiveKit Cloud free tier, not self-hosted.** Self-hosting was considered and rejected on two
grounds, in order. First, latency: a self-hosted SFU would run in one region, while LiveKit Cloud
routes through a geographically distributed edge — for a WebRTC media path, one fixed region is
likely *worse* for any caller not near it, not better. Second, effort: the OSS server needs UDP
port ranges for media (or a TURN relay fallback, which adds its own latency), and the existing
Railway deployment is built around HTTP/TCP ingress through its proxy. That is a real infra project
for no gain. The honest cost of the Cloud choice is named plainly in the README's "Known
limitations": this adds a third external account to the project's surface, alongside OpenAI and
Anthropic.

**The agent worker runs in-process with FastAPI.** Not the usual `lk agent` deployment. The
supervisor's per-call state — `CALL_STATES`, the per-call `asyncio.Lock`s, the Phase 7/8 background
task registries — lives in module-level globals that the admin trace stream also reads. A separate
worker process would have meant reintroducing an IPC bridge, which is precisely what this phase
deletes, and would have broken the admin panel's live view. Two consequences worth recording
because both are silent failure modes:

- `job_executor_type=JobExecutorType.THREAD` is passed **explicitly**. LiveKit defaults to a
  subprocess on Linux/macOS and a thread on Windows, so relying on the default would work on a
  Windows dev machine and silently break on Railway, with the agent mutating a `CALL_STATES` in the
  wrong process.
- A thread job still gets its **own event loop**, and an `asyncio.Lock` binds to the first loop that
  awaits it. Every supervisor call is therefore marshalled back onto the FastAPI loop
  (`_on_main_loop`), which keeps the concurrency model identical to the pre-Phase-14 one rather
  than introducing a second, subtly different one.

### Phase 14: filler is transport-scheduled and pre-rendered — reconciling with the Phase 2 decision that removed it

This phase reintroduces something an earlier decision above ("No filler acknowledgment ... reversed
after live testing") explicitly removed. That earlier finding was not wrong, and it is not being
overturned wholesale — the mechanism is different in the two ways that finding actually complained
about.

The Phase 2 complaint was: *"a spoken promise ('one moment') followed by dead air until the real
reply eventually lands reads as more broken than a brief, unannounced pause."* Two distinct
failures are bundled in that sentence — narrating a wait that turns out to be short, and making a
promise that is then followed by silence. Phase 14 addresses both:

- **Short waits are still not narrated.** The old filler was model-generated at the *start* of the
  turn, before anyone knew how long the turn would take. LiveKit's `RunContext.with_filler`
  schedules on a continuous-idle dwell instead (`FILLER_IDLE_DELAY_SECONDS = 0.4`), so a turn that
  resolves quickly produces no filler at all, and the filler can never talk over the caller.
- **Long waits are re-acknowledged, not promised once and abandoned.** Each of the three target
  sites has a second line that fires only if the supervisor is still working
  `FILLER_REPEAT_SECONDS = 4.0` later. The second line is deliberately reassurance with no new
  promise in it ("Still with you." not "Almost done!"), because a second promise would compound the
  original complaint rather than answer it.

**Pre-rendered audio, not live TTS.** Discovered at implementation time: `session.say()` refuses
text-only input when the LLM is a `RealtimeModel` — the OpenAI plugin reports
`supports_say = False` and the call raises unless a TTS plugin is attached or `audio=` is supplied.
Attaching a TTS purely to unlock `say()` would have pulled a second voice into the call. Instead the
fixed phrase set is rendered once by `scripts/generate_filler_audio.py` at the same voice and speed
as the live session and committed as WAVs. This is strictly better than the live-TTS alternative on
the phase's own terms: Decision 1 rejected a model-generated filler because even a fast model costs
a 200–400ms round trip, and pre-rendering makes that a local file read.

**Deviation from Decision 2's stated mechanism.** The phase doc scopes filler to
`confirm_field_answer`, `confirm_booking_answer` and `generate_confirmation_summary`, reasoning that
for those "the reply *is* the answer." Checked against the code, that is literally true only for
`generate_confirmation_summary` — the other two are classifiers whose turns produce a reply from a
template, a repeated question, or a second Claude call. The doc's *intent* holds exactly (these are
the turns where the caller has just answered and is left waiting with nothing else being asked), but
the filler cannot hang off the tool function: by the time a tool runs, the turn is already underway.
Selection instead reads the **pre-turn `CallState`**, which deterministically predicts which call
site the turn will reach — which also keeps it inside CLAUDE.md rule #2. One honest gap:
`node_capture_fast`'s `_fallback_to_real_capture` can also reach `confirm_field_answer`, but nothing
in the pre-turn state predicts it (that is the point of the fast path — it decides mid-turn), so
those turns keep their pre-Phase-14 behaviour.

### Phase 14: what actually changed — perceived wait, not round-trip latency, with both numbers

The phase doc's central warning is that LiveKit must not be credited with anything Phase 13
achieved: the Anthropic round trip is fixed by the SDK call inside `llm_utils.py` and is identical
regardless of transport. Rather than assert that, here are both numbers, measured together from the
same traces (`python eval/filler_latency_report.py`, over calls driven by
`eval/livekit_live_call.py`):

| turns | n | round trip | time to first audio |
|---|---:|---:|---:|
| `confirm_field` | 5 | 2543ms | 422ms |
| `propose_slot` | 1 | 2332ms | 406ms |
| **with filler, p50** | 6 | **2484ms** | **422ms** |
| **with filler, p95** | | 5022ms | 577ms |
| **without filler, p50** | 12 | 766ms | **1796ms** |
| **without filler, p95** | | 5419ms | 6342ms |

Read it this way:

- **Round trip is Phase 13's number and it did not move.** Same tools, same models, same durations.
  **Phase 14 reduced it by nothing and claims nothing.**
- **The two blocks are the comparison, not a before/after of the same turns.** On a filler turn the
  caller hears something at ~420ms while the supervisor is still working — time-to-audio sits
  *below* the round trip. On a turn without one they hear nothing until the reply itself is
  generated and played: 1796ms at p50, 6.3s at p95. That second row is what all three filler sites
  looked like before this phase.
- **The round trips differ between the blocks (2484ms vs 766ms) because filler turns ARE the slow
  ones.** That is Decision 2 working as intended rather than a sampling artefact — filler is scoped
  to exactly the sites where the caller has just answered and has nothing else to do but wait.
- **The p95 row is the real argument.** 6.3 seconds of silence is where a pause stops reading as a
  pause and starts reading as a dropped call.

Two honest limits. `first_audio` is a real playout signal (LiveKit's agent state entering
"speaking"), not the moment `say()` was called — an earlier version of this measurement made that
mistake and reported ~400ms for a clip that took 1.3s to make a sound, because 890ms of silence was
baked into the front of the WAV. And this is 18 turns on one machine against one LiveKit region,
from a gitignored local database: enough to show the shape of the change and to prove the round
trip is untouched, reproducible by re-running the two commands above, but not a production latency
budget.
