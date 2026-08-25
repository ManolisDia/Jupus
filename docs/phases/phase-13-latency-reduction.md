# Phase 13 — Latency Reduction (Supervisor Round-Trip)

## Goal

Phase 11 measured latency; this phase reduces it. Live trace data (`backend/db/calendar.db`, pulled 2026-08-25) shows the per-turn Claude round-trip — not the transport layer, not SQLite, not Realtime's own STT/turn-detection — as the dominant cost on the slowest turns:

| tool | n | median | max |
|---|---|---|---|
| `confirm_field_answer` | 67 | 2035ms | 10229ms |
| `confirm_booking_answer` | 23 | 1951ms | 6885ms |
| `generate_confirmation_summary` | 23 | 2075ms | 3282ms |
| `generate_confirm_back` | 48 | 1652ms | 3387ms |

versus the deterministic tools it sits next to in the same turns — `book_consultation` (27ms median), `validate_email`/`validate_phone` (6ms), `check_availability` (8ms), `suggest_alternative_slots` (12ms). This phase attacks the Claude-call numbers directly: fewer round trips per turn, cheaper round trips where accuracy allows it, and a real fix for whatever is driving the multi-second tails. It does **not** touch how the caller experiences the wait (that's Phase 14) — it changes the actual number.

## Prerequisite

Phase 11 (latency/cost instrumentation) — this phase is unmeasurable without it, and its DoD's live-call verification step is exactly how this phase's own before/after comparison gets done. No dependency on Phase 9/10 (hosting/telephony); can run any time after 11.

## Non-goals

- **Not touching Realtime, the bridge, `client/app.js`, or any interrupt/filler behavior.** That's Phase 14. This phase's changes are entirely inside `backend/supervisor/llm_utils.py`, `backend/supervisor/graph.py`, and `backend/supervisor/tools.py`'s Claude-backed functions.
- **Not adding new tools or nodes.** Every change here either merges two existing Claude calls into one, swaps a model, or fixes a retry-driven tail — no new business logic, no new `CallState` fields.
- **Not chasing `propose_taxonomy_updates`/`classify_call_errors`'s multi-second-to-35-second durations** — those are `eval/insights_agent.py`'s offline judge/critique passes, run against logged calls after the fact, never in the live-call hot path. They don't affect caller-perceived latency and are out of scope here.
- **Not a model-wide downgrade.** `MODEL_ID = "claude-sonnet-5"` stays the default; any model swap in this phase is scoped to specific, individually-justified tool calls, tested against real transcripts before landing (Decision 3 below explains why, referencing the exact prior Haiku failure in `docs/DECISIONS.md`).

## Decisions made, not left open for the implementer

**1. Prompt caching first, because it's the only change here with zero behavioral risk.** `call_claude_json`/`call_claude_text` in `llm_utils.py` currently send `system` as a plain string on every call (`messages.create(..., system=system, ...)`). Each node's system prompt (`prompts.py`) is static per-node and reused turn after turn within a call and across calls. Switching to Anthropic's `cache_control` on the system block costs nothing to correctness — the prompt content is unchanged, only how it's transmitted — so this ships first and its effect gets measured in isolation before any of the riskier changes below stack on top of it.

**2. Merge `extract_field` + `generate_confirm_back` into one schema call.** `node_capture` ([graph.py:369-399](../../backend/supervisor/graph.py)) makes these as two sequential Claude calls in the same turn whenever a field needs confirming: extract, then — in a separate round trip — generate the confirm-back phrasing for what was just extracted. One JSON-schema call returning `{value, confidence, confirm_back_phrasing}` removes a full round trip from every turn that hits this path. Same merge applies to `select_offered_slot` + `generate_confirmation_summary` in `node_booking`'s offered-slot-selection branch.

**3. Model choice is decided per-tool call, not per-project, and only after a live-transcript test — not by assumption.** `docs/DECISIONS.md` already documents *why* Haiku was rejected project-wide: it unreliably followed precise formatting instructions (spoken "at"/"dot" → `@`/`.`) on `extract_field`. That failure is specific to free-text extraction-with-formatting. `select_offered_slot`, `confirm_field_answer`, and `confirm_booking_answer` are closed-set classification (pick an index / yes-no-correction) — a materially different task shape. Each is a candidate for testing against Haiku independently; a candidate only ships if it holds up against a batch of real logged transcripts for that specific tool (via `eval/replay_scenarios.py` + `eval/compare_runs.py`, comparing error-class rates between a Sonnet-baseline label and a Haiku-candidate label), not on the assumption that "classification is safer than extraction" alone is enough.

**4. The retry tail gets root-caused, not just tolerated.** `confirm_field_answer`'s 10229ms max and `confirm_booking_answer`'s 6885ms max are roughly 5x their own medians — consistent with the existing retry path in `call_claude_tool` ([llm_utils.py:93-105](../../backend/supervisor/llm_utils.py)): a failed first attempt, a 0.5s sleep, then a full second attempt. Before treating this as "just API variance," pull every `llm_retry`/`llm_call_failed` trace event for these two tool names and check whether they cluster with the slow-turn `tool_call_end` timestamps. Two possible outcomes, both actionable: (a) it's genuinely retries, in which case the fix is understanding *why* these two calls fail more often than others (a schema issue? a specific input shape?), not just leaving the retry as an unexplained tax; (b) it's real single-attempt latency variance from the API itself, in which case the retry theory is wrong and this line item closes as "measured, not fixable from this side."

**5. Every change in this phase is validated against `eval/replay_scenarios.py`, before and after, labeled.** Same tool this project already has for exactly this purpose (its docstring/`docs/PLAN.md` entry: "drives the 6 canonical scenarios through the real, unmocked pipeline to generate a fresh comparable batch after a prompt-engineering change"). Run `python eval/replay_scenarios.py --label phase13-baseline` before touching anything, then a fresh `--label` after each of items 1-4 above, and `eval/compare_runs.py` between consecutive labels — both for latency (the new numbers from Phase 11's instrumentation) and for correctness (error-class rates must not regress just because a call got faster).

## Changes to existing files

### `backend/supervisor/llm_utils.py`
Add `cache_control: {"type": "ephemeral"}` to the system prompt block in both `call_claude_json` and `call_claude_text`. Confirm at implementation time against current Anthropic SDK/API docs exactly how a cached system block is expressed for the `output_config`/plain-text call shapes this file already uses — the API surface for prompt caching has evolved and should be verified live, same caution this project already applies elsewhere to fast-moving API surfaces (`docs/DECISIONS.md`'s Realtime event-name note).

### `backend/supervisor/tools.py` / `backend/supervisor/graph.py`
- New merged tool function, e.g. `extract_and_confirm_field(utterance, field) -> {value, confidence, confirm_back_phrasing}` (or the field-specific equivalent for email/phone spell-out framing), replacing the `extract_field` → `generate_confirm_back` pair in `node_capture`'s pending-confirm path.
- New merged tool function for `node_booking`'s offered-slot branch, replacing `select_offered_slot` → `generate_confirmation_summary`.
- Existing `extract_field`/`generate_confirm_back`/`select_offered_slot`/`generate_confirmation_summary` are only removed once every call site is migrated — no dead code left calling both old and new paths.

### `backend/supervisor/llm_utils.py` (model override)
`call_claude_tool` gains an optional `model` parameter (default `MODEL_ID`), threaded through to `call_claude_json`/`call_claude_text`'s `model=` argument — needed so item 3's per-tool Haiku experiments don't require a project-wide `MODEL_ID` change to test.

## Tests

1. `test_llm_utils_cache_control.py` — assert the system block sent to `_client.messages.create` includes the expected cache-control marker, for both `call_claude_json` and `call_claude_text`.
2. `test_extract_and_confirm_field_returns_merged_result` — mocked Claude response with all three fields; assert `node_capture`'s pending-confirm path produces the same `CallState` transition as today's two-call version, from one call.
3. `test_select_and_confirm_offered_slot_returns_merged_result` — same shape for the booking merge.
4. `test_call_claude_tool_model_override` — assert passing `model=` to `call_claude_tool` reaches the underlying `messages.create` call instead of the default `MODEL_ID`.
5. Full existing `backend/tests/test_scenarios.py` suite re-run unchanged — the merges must not alter any scenario's observable behavior, only its call count.

## Definition of Done

- [ ] `pytest` — full suite, zero regressions.
- [ ] `eval/replay_scenarios.py --label phase13-baseline` captured before any change in this phase.
- [ ] Prompt caching shipped and measured in isolation (`--label phase13-caching`) against baseline via `eval/compare_runs.py` — real before/after `supervisor_processing` p50/p95 numbers, not an assumption that it helped.
- [ ] `extract_field`+`generate_confirm_back` merge shipped, measured (`--label phase13-merge-capture`), scenario error-class rates unchanged from baseline.
- [ ] `select_offered_slot`+`generate_confirmation_summary` merge shipped, measured (`--label phase13-merge-booking`), scenario error-class rates unchanged from baseline.
- [ ] Retry-tail investigation completed and documented in `docs/fixes/` or `docs/known-issues/` (per CLAUDE.md's knowledge-base convention) — root cause identified, or explicitly recorded as "not reproducible from available data" if it can't be pinned down.
- [ ] Any per-tool model swap (item 3) shipped only with a passing `eval/compare_runs.py` diff showing no error-class regression against the Sonnet baseline for that specific tool.
- [ ] Final `eval/replay_scenarios.py --label phase13-final` run, `eval/compare_runs.py --baseline phase13-baseline --candidate phase13-final` shows a real, positive `supervisor_processing` latency delta with no correctness regression — this is the number that goes in `docs/answers.md`'s Q1 update.
- [ ] `docs/DECISIONS.md` gets an entry recording which of items 1-4 shipped, which were tried and reverted (with why), and the final measured latency delta.
