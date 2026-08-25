# Phase 14 — LiveKit Transport + Perceived-Latency Masking

## Goal

Phase 13 reduces the actual Claude round-trip time. This phase does not — it cannot, and should not be sold as if it does (see Decision 1). What it does: replace the hand-rolled WebRTC bridge (`client/app.js` + `backend/app.py`'s `/session`/`/bridge` + `dispatcher.py`'s `SPEAKING`/`DEFERRED`/`CONNECTIONS` bookkeeping) with LiveKit Agents' managed transport, keeping OpenAI Realtime as the speech model via LiveKit's own `openai.realtime` plugin — and use LiveKit's turn-taking/interruption primitives to build a real "acknowledge fast, resolve slow" pattern for the calls that have no natural next question to hide behind (`confirm_field_answer`, `confirm_booking_answer`, `generate_confirmation_summary`), on top of whatever those calls' Phase 13 durations end up being.

## Prerequisite

Phase 13 complete and measured. This phase's DoD explicitly compares against Phase 13's final numbers — building this on unmeasured, unoptimized Claude calls would conflate "the transport got better" with "the underlying call got faster," exactly the confusion this whole planning conversation started by untangling.

## Why this exists, and why it's scoped separately from Phase 13

Confirmed directly in planning: LiveKit does not shrink the Anthropic API round-trip — that number is fixed by `llm_utils.py`'s `client.messages.create()` call and is identical regardless of transport. What LiveKit changes is what the caller *hears* during that unchanged wait, and how robustly the system handles the caller talking over that filler. This is real UX value (dead air reads as "did it hang up?"; filler reads as "it's thinking") but it is not a latency number, and this doc's Definition of Done must not claim it reduced anything Phase 13 didn't already reduce.

## Non-goals

- **Not a Pipecat-style full pipeline swap.** Dropping OpenAI Realtime for a separate STT→LLM→TTS pipeline reverses a decision `docs/DECISIONS.md` made deliberately ("OpenAI Realtime has the most mature WebRTC/tool-calling/interrupt handling of the available realtime voice APIs") and is explicitly out of scope.
- **Not changing `ask_supervisor`, `graph.py`'s nodes/edges, or `tools.py`.** CLAUDE.md rule #1 (Realtime sees exactly one tool) and rule #2 (deterministic graph edges) are transport-independent; this phase touches transport only.
- **Not new hosted infrastructure by default.** CLAUDE.md: *"No telephony, no Docker, no Railway hosting unless every phase through Phase 8 is done ahead of schedule."* This project is well past Phase 8. LiveKit Cloud's free "Build" tier (5,000 WebRTC min/mo, 1,000 agent min/mo, no card required, confirmed 2026-08-25) removes the *cost* objection but not the *new external dependency* one — this phase adds a third paid-capable external account (alongside OpenAI, Anthropic) to the project's surface, on top of the Railway backend already deployed. This is named plainly here and in the README's "Known limitations," not glossed over.
- **Not attempted until the interrupt-handling test cases below are written down and understood** — this codebase has a documented, recent live bug in exactly this area (`ask_supervisor` racing ASR transcription completion, commit `43757bc`), so "wire it up and see" is not an acceptable approach here.

## Decisions made, not left open for the implementer

**1. Filler content is a fixed, canned phrase per node — never a model-generated one.** Generating the filler with even a fast model still costs a network round trip (200-400ms), which defeats the purpose of a filler meant to start speaking immediately. Each of the three target calls gets one or two short, hand-written filler lines (e.g. "Okay, one sec." / "Let me get that booked.") selected deterministically by node, not composed per-turn.

**2. Only the three calls with no natural next question get this treatment.** `node_research_gather` and the field-capture background-verification path already hide their latency behind a real follow-up question (Phase 7/8's existing pattern) — they are not touched by this phase. This phase's filler treatment is scoped exactly to `confirm_field_answer`, `confirm_booking_answer`, and `generate_confirmation_summary`'s call sites, because those are the ones where the reply *is* the answer and there's nothing else to ask in the meantime.

**3. Interrupt-during-filler has one explicit, deterministic policy, decided here rather than improvised at implementation time: the caller's utterance during the filler is captured and queued, exactly like today's `DEFERRED` mechanism for "caller was speaking when a reply was ready" — but inverted (here, the agent is speaking when the caller starts). If the queued utterance is substantive (not just an acknowledgment like "okay"/"mhm"), it must be handed to the graph as this turn's real input once the filler finishes, not discarded. Distinguishing "okay" from a substantive interruption reuses `heuristics.py`'s existing pattern of small, deterministic keyword/short-utterance checks — not a new LLM call (that would reintroduce exactly the latency this phase is trying to hide behind).

**4. If the background real-answer task fails after the filler has already been spoken, the caller must not be left hanging.** This reuses the existing `_llm_failure_fallback` pattern (graceful fallback reply, `consecutive_llm_failures` bookkeeping, 3-strikes escalation per CLAUDE.md rule #7) — the filler having already played changes nothing about that contract, it just means the fallback fires one turn later than it would today.

**5. LiveKit's turn-taking replaces `SPEAKING`/`DEFERRED`/`CONNECTIONS`, not just supplements them.** Running both the old hand-rolled bookkeeping and LiveKit's own turn-detection/interruption state in parallel is exactly the kind of doubled, driftable state this project's architecture doctrine (rule #9's spirit — one source of truth per concern) argues against. `dispatcher.py`'s connection-management globals are retired as part of this migration, not left dormant alongside the new path.

## Migration scope

| Piece | Today | After this phase |
|---|---|---|
| Client transport | `client/app.js`, hand-rolled WebRTC to OpenAI Realtime | LiveKit room + agent, `openai.realtime` plugin hosting the same Realtime model |
| Token/session issuance | `backend/app.py`'s `POST /session` | LiveKit room-token issuance (same endpoint shape, different token type) |
| Tool-call bridge | `backend/app.py`'s `WS /bridge`, `dispatcher.on_bridge_message` | LiveKit agent-side function-call handling, dispatched into the same `process_supervisor_call` |
| Turn-taking / interrupt state | `dispatcher.py`'s `SPEAKING`/`DEFERRED`/`CONNECTIONS` | LiveKit's built-in turn-detection + this phase's new filler/interrupt-queue logic (Decision 3) |
| Everything below the bridge | `graph.py`, `tools.py`, `state.py`, repositories | **Unchanged** |

## Tests

New interrupt-handling scenarios, on top of the existing 7 canonical scenarios in `docs/scenarios.md` (all of which must still pass unchanged through the new transport):

1. `test_filler_plays_before_real_answer_ready` — the real answer takes longer than the filler; assert the filler is what's spoken first and the real answer follows once ready.
2. `test_caller_acknowledgment_during_filler_does_not_reroute` — caller says "okay"/"mhm" during the filler; assert this does not get treated as a substantive new utterance fed into the graph.
3. `test_caller_substantive_interruption_during_filler_is_queued_and_processed` — caller says something real (e.g. corrects a detail) during the filler; assert it's captured and becomes the next turn's real input, not dropped.
4. `test_background_answer_ready_before_filler_finishes_speaking` — real answer resolves faster than expected; assert it queues correctly rather than colliding with the still-playing filler (mirrors today's one-active-response-at-a-time constraint, now under LiveKit's model).
5. `test_background_failure_after_filler_spoken_falls_back_gracefully` — the real call fails (`LLMCallFailed`) after the filler already played; assert `_llm_failure_fallback`'s existing contract still holds.
6. Full existing scenario suite (`backend/tests/test_scenarios.py`, all 7 `docs/scenarios.md` cases) re-run against the new transport, unchanged pass/fail expectations.

## Definition of Done

- [x] LiveKit Cloud free-tier account provisioned (or self-hosted server running on Railway alongside the existing backend — pick one, document which and why in `docs/DECISIONS.md`). — Cloud; rationale in `DECISIONS.md` (single-region self-hosting would likely make media latency *worse*, plus UDP/TURN work Railway's HTTP ingress doesn't suit).
- [x] `client/app.js` replaced by a LiveKit client integration; `backend/app.py`'s `/session`/`/bridge` replaced by LiveKit's session/agent wiring. — `client/livekit-transport.js` + `backend/transport/`; `app.js` 677 → 252 lines.
- [~] All 7 canonical scenarios (`docs/scenarios.md`) re-run live over the new transport and pass. — **Partially.** Six of the eight scripts (S7 splits into S7a/S7b) reached their expected outcome live; S4 and S7b desynchronized because the scripts assume *mocked* extraction, so once a real extraction took a different branch the later scripted lines answered questions the agent hadn't asked. Harness limitation, written up in `eval/livekit_live_call.py`; closing it properly needs an adaptive caller. Not claimed as a pass.
- [x] All 5 new interrupt-handling test cases above pass, including live manual verification of at least the substantive-interruption-during-filler case (item 3). — All five in `backend/tests/test_livekit_agent.py`; barge-in with a real correction verified on a live call (2026-08-25). Case 4 is reinterpreted and documented: under LiveKit a turn faster than the idle dwell produces *no* filler, so there is nothing to collide with.
- [x] `pytest` — full suite, zero regressions. — 402 passing.
- [x] Filler treatment shipped for exactly the three calls named in Decision 2, no others. — `backend/supervisor/fillers.py`; `test_fillers.py` pins both that the three sites get one and that nothing else does.
- [x] `docs/DECISIONS.md` gets an entry: why LiveKit over Pipecat, why LiveKit over staying hand-rolled, the free-tier-vs-self-host choice, and an explicit statement that this phase changed perceived latency, not the Phase 13 round-trip numbers — with both sets of numbers shown side by side. — Three entries; numbers measured by `eval/filler_latency_report.py` from real playout.
- [x] README's "Known limitations" section names the new external LiveKit dependency plainly. — Plus the two operational sharp edges that fail silently (automatic dispatch across two backends; the worker living inside the backend process).
- [x] `docs/answers.md`'s Q1 and Q2 answers updated. — Q2 was a TBD placeholder and is now written.

### Post-merge follow-ups (not blockers, recorded so they aren't lost)

1. **The first turn of every call is the slowest and has no filler** — measured at 3.3–4.3s
   (`classify_practice_area` plus ~1s playout). Decision 2 didn't consider it because routing
   normally ends by asking a question, but on turn one there is nothing in front of the gap. It is
   the caller's first impression.
2. **`capture_fast`'s last field pays two sequential Claude calls** (`extract_field` +
   `generate_confirm_back`, measured 6.3s to first audio) and gets no filler. Phase 7's fast path is
   zero-LLM for every field *except* the last, which has no following turn to run against. It fits
   Decision 2's rationale exactly without being one of its three named sites.
3. **The browser client has no automated coverage.** The "thinking" indicator was dead from the
   migration until it was caught by hand; a page-level smoke test would have caught it.
4. **An adaptive live caller** would make live scenario runs trustworthy (see item 3 above).
