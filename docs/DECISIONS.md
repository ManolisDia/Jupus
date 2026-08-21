# Decisions

Short log of *why*, not *what* — so a later session doesn't quietly re-decide something already ruled out. Add an entry whenever a non-obvious architecture call is made or revisited.

---

### `ask_supervisor` is one coarse dispatch tool, not many fine-grained tools on Realtime
Realtime natively supports tool-calling, so it could call `book_consultation`/`check_availability`/etc. directly. We chose a single dispatch tool instead because Realtime models are tuned for naturalness/speed, not for reliably sequencing multi-step business logic (confidence handling, retries, booking conflict resolution) — that's exactly what this brief grades. Routing all of that through a separate Claude-driven LangGraph supervisor makes the logic deterministic, testable, and debuggable, at the cost of one extra hop of latency per "real" turn — mitigated with async dispatch (see below) rather than blocking.

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
Drafted explicitly in Phase 2 (not left as a vague "be helpful" prompt) because this is the one piece of prompt content that gates whether the entire observability/eval stack (traces, error taxonomy, admin panel) sees anything at all — if Realtime free-styles a legal-sounding answer without calling the tool, nothing downstream ever knows it happened. The instructions explicitly forbid stating legal information, confirming bookings, or inventing firm details on its own, and require calling `ask_supervisor` even when it "thinks it already knows" the answer. A short filler acknowledgment ("let me check that") is allowed while waiting, but is explicitly flagged as needing empirical verification against the actual Realtime API version's behavior — the instruction text alone doesn't guarantee the platform supports speaking and calling a tool in the same turn, and Phase 2's DoD requires confirming this live rather than assuming it.

### No cap on session/call duration
Considered a hard server-side timeout on call length (protects against a stuck loop or an abandoned tab with a live mic). Explicitly decided against — not part of this build. If cost or runaway-session risk becomes a real concern later (e.g. if a hosted demo is ever stood up), address it there specifically rather than constraining every local test call now.

### Realtime model: `gpt-realtime-2.1-mini`, not flagship `gpt-realtime-2.1` — provisional, revisit at Phase 2/4
Switched during Phase 1 live testing after noticing perceptible reply latency on longer turns.
`gpt-realtime-2.1-mini` is OpenAI's faster/cheaper sibling to the flagship model, released
alongside it (July 2026) for exactly this latency/cost tradeoff. The case for it fitting here
specifically: rule 1 above means Realtime *never* does classification, extraction, confidence
scoring, or booking logic — all business reasoning is offloaded to Claude via the LangGraph
supervisor precisely because Realtime models aren't meant to sequence multi-step logic. So
Realtime's job stays narrow for the life of this project: decide whether to call
`ask_supervisor`, relay the reason, and speak the result back naturally. That's within mini's
stated strengths; OpenAI's own guidance is to reach for full `gpt-realtime-2.1` only when you
need "the strongest realtime reasoning, tool use, instruction following, and voice-agent
behavior."

**Where this could bite and needs re-checking, not assumed fine:**
- Phase 2's DoD already requires live-verifying that Realtime reliably defers to `ask_supervisor`
  instead of answering on its own — mini's weaker instruction-following is the specific risk that
  check exists to catch.
- Phase 3/4's confirm-back and booking-confirmation loops require Realtime to speak back emails,
  phone numbers, and appointment times accurately — mini's accuracy here hasn't been tested yet
  (nothing to test it against in Phase 1, which has zero tools).

If either of those checks shows mini stumbling, this reverts to `gpt-realtime-2.1` — it's a
one-line constant change (`backend/app.py`'s `REALTIME_MODEL`), not an architecture change.

### Realtime (OpenAI) + Supervisor (Claude) — two vendors, deliberately
OpenAI Realtime has the most mature WebRTC/tool-calling/interrupt handling of the available realtime voice APIs. Claude powers the supervisor's reasoning (extraction, classification, summarization). Two API keys is an accepted tradeoff, documented clearly in the README since the brief requires documenting exactly what's needed to run the project.
