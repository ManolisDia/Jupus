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

### `last_caller_utterance` is authored by the Realtime model, not a raw ASR transcript
Discovered during Phase 3 live testing: `ask_supervisor`'s `last_caller_utterance` argument is
not a passthrough of what OpenAI's speech recognition literally heard — it's a string the
Realtime *model itself* generates when constructing the tool call, same as any other function
argument. Observed live: a caller said "manos44" for their email and the value that reached
`extract_field` was `manos44@example.com` — the model had invented a plausible domain and
inserted an `@` neither said nor implied. This meant a supposedly `capture_failed`-triggering
malformed email got silently "fixed" upstream of anything `backend/supervisor/tools.py` or its
prompts could see or control, defeating the confidence-threshold/validation pipeline entirely.

Fixed at the one layer we do control: `client/app.js`'s tool schema now carries an explicit
`description` on `last_caller_utterance` demanding a verbatim, uncompleted transcription, and
`SUPERVISOR_INSTRUCTIONS` gained a matching rule (7). Not a certainty — this depends on the
Realtime model actually following the instruction rather than a guaranteed platform behavior, so
watch for it recurring in later live testing rather than assuming this fully closes the gap.

### Supervisor's Claude calls use `claude-haiku-4-5`, not flagship
`backend/supervisor/llm_utils.py`'s `call_claude_json`/`call_claude_text` (Phase 3 onward) use
Haiku rather than a larger model. These calls are still synchronous/blocking per conversational
turn until Phase 5's async dispatcher exists, and the tasks themselves are simple — 4-way
practice-area classification, single-field extraction from one utterance, a short confirm-back
question — well within a fast/cheap model's capability. Revisit if extraction/classification
accuracy proves unreliable in live testing; the model id is a one-line change.

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

### Realtime (OpenAI) + Supervisor (Claude) — two vendors, deliberately
OpenAI Realtime has the most mature WebRTC/tool-calling/interrupt handling of the available realtime voice APIs. Claude powers the supervisor's reasoning (extraction, classification, summarization). Two API keys is an accepted tradeoff, documented clearly in the README since the brief requires documenting exactly what's needed to run the project.
