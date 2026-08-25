# Written Answers

One section per required question, referencing actual file paths and specific behavior — not
abstract description. Structure per `docs/phases/phase-13-polish-submission.md`.

---

## Q1 — Latency / pipeline design

Every real conversational turn is broken into four measured stages, recorded as `trace_events`
and aggregated by `eval.insights_agent.latency_breakdown_percentiles` /
`_stage_durations_for_call` (`eval/insights_agent.py`), surfaced in the admin panel's "Latency &
cost" panel and per-call at `GET /api/calls/{call_id}/latency`:

1. **`stt_and_dialogue_decision`** — caller stops talking (`speech_stopped`) → the backend
   receives the `ask_supervisor` tool call (`ask_supervisor_received`, recorded synchronously in
   `backend/dispatcher.py::on_bridge_message` before the supervisor task is spawned). This is
   OpenAI Realtime's own VAD/transcription/dialogue-decision pipeline — a black box from this
   project's side; this stage measures the *outside* of that box, not its internals, since owning
   Realtime's STT/turn-detection directly is out of scope.
2. **`supervisor_processing`** — the LangGraph/Claude round-trip itself, from
   `ask_supervisor_received` to `reply_delivered` (or `reply_deferred`, if the caller was still
   talking when the graph finished — see stage 3).
3. **`deferred_wait`** — Phase 5's "caller was still talking" queue delay. Deliberately **not**
   folded into `supervisor_processing`: conflating "how long did Claude take" with "how long did
   we wait for a natural pause to speak" would misattribute a scheduling artifact as model latency
   (`docs/DECISIONS.md`).
4. **`tts_first_audio`** — reply handed to Realtime → caller actually hears audio. Originally
   planned as a `response.audio.delta` data-channel event (per the phase doc); live testing showed
   WebRTC transport never delivers that event — audio flows over the peer connection's media track
   instead, not as data-channel deltas. `client/app.js` now detects first-audio via the same
   remote-stream amplitude analyser the caller-facing visualizer already runs every frame (see
   `docs/DECISIONS.md`'s revision note on this).

**Real observed numbers**, pooled across a small batch of logged calls (`GET /api/eval/summary`,
2026-08-25):

| Stage | avg | p50 | p95 |
|---|---|---|---|
| `stt_and_dialogue_decision` | 814ms | 767ms | 1199ms |
| `supervisor_processing` | 2830ms | 2476ms | 5638ms |
| `deferred_wait` | 0ms | 0ms | 0ms |
| `tts_first_audio` | 790ms | 752ms | 1005ms |
| `total_perceived` | 4885ms | 4638ms | 9196ms |

`supervisor_processing` is the dominant cost by a wide margin — the LangGraph/Claude round-trip,
not Realtime's own STT or the client's TTS-start detection, is where a caller actually waits. This
is exactly why the supervisor detour is kept off the hot path (fire-and-forget dispatch,
`asyncio.to_thread`, deferred-delivery — `backend/dispatcher.py`) rather than blocking the
Realtime session: Realtime itself handles STT+dialogue+TTS in one hop with no added latency; only
the supervisor detour adds a second hop, and that hop is where the real cost lives.

**Estimated cost**: `eval.pricing.estimate_cost_usd` (Claude Sonnet 5 + Realtime `gpt-realtime-2.1`
token usage, rates verified live against `claude.com/pricing` and
`developers.openai.com/api/docs/pricing` on 2026-08-24) — average **$0.024/call** observed across
the same batch (`GET /api/eval/summary`'s `cost.average_usd`), labeled "estimated" everywhere it's
shown per `docs/DECISIONS.md`'s pricing-verification caveat. Cost was raised directly as a concern
in conversation — this is a real, measured answer to it, not a guess.

## Q2 — Turn-taking / interruptions

*TBD — Phase 13 (polish/submission).*

## Q3 — Iteration / scaling / operational health

*TBD — Phase 12 (concurrency stress test) + Phase 13.*

## Q4 — Telephony / warm transfer / failure handling

*TBD — Phase 10 (telephony), if built. Design sketch otherwise per
`docs/phases/phase-13-polish-submission.md`.*
