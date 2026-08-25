"""Hardcoded $/1,000,000-token pricing for the two paid APIs this project
uses. VERIFY AGAINST CURRENT PUBLISHED PRICING before treating any dollar
figure this project displays as accurate — these rates change over time and
this file is not automatically kept in sync with either provider. Every
place a cost is shown must be labeled "estimated" (Phase 11, Decision 10).

Confirmed live against claude.com/pricing (Claude Sonnet 5, matching
llm_utils.MODEL_ID) and developers.openai.com/api/docs/pricing
(gpt-realtime-2.1, matching backend.app.REALTIME_MODEL) on 2026-08-24.

Cache write/read rates (Phase 13, added 2026-08-25) are Anthropic's
standard multipliers on the base input rate for a 5-minute-TTL ephemeral
cache block (1.25x for a write, 0.1x for a read) — confirmed against
Anthropic's published prompt-caching pricing on 2026-08-25. Re-verify both
the multipliers and CLAUDE_SONNET_INPUT_PER_MILLION together before
trusting this file's numbers; they're derived from each other, not
independently confirmed constants.

Haiku rates (Phase 13, added 2026-08-25, for the per-tool model-choice
experiments in docs/phases/phase-13-latency-reduction.md Decision 3)
confirmed against Anthropic's published Claude Haiku 4.5 pricing on
2026-08-25.
"""

CLAUDE_SONNET_INPUT_PER_MILLION = 2.00
CLAUDE_SONNET_OUTPUT_PER_MILLION = 10.00
CLAUDE_SONNET_CACHE_WRITE_PER_MILLION = CLAUDE_SONNET_INPUT_PER_MILLION * 1.25
CLAUDE_SONNET_CACHE_READ_PER_MILLION = CLAUDE_SONNET_INPUT_PER_MILLION * 0.1
CLAUDE_HAIKU_INPUT_PER_MILLION = 1.00
CLAUDE_HAIKU_OUTPUT_PER_MILLION = 5.00
CLAUDE_HAIKU_CACHE_WRITE_PER_MILLION = CLAUDE_HAIKU_INPUT_PER_MILLION * 1.25
CLAUDE_HAIKU_CACHE_READ_PER_MILLION = CLAUDE_HAIKU_INPUT_PER_MILLION * 0.1
REALTIME_AUDIO_INPUT_PER_MILLION = 32.00
REALTIME_AUDIO_OUTPUT_PER_MILLION = 64.00
REALTIME_TEXT_INPUT_PER_MILLION = 4.00
REALTIME_TEXT_OUTPUT_PER_MILLION = 24.00

# Phase 13, Decision 3 — keyed by the exact model id llm_utils.py sends to
# the API (MODEL_ID / HAIKU_MODEL_ID), since a per-tool override means a
# single call's tokens are no longer safely assumed to be Sonnet-priced.
# An unrecognized model id falls back to the Sonnet rate (the more
# expensive of the two) rather than silently pricing at zero — a missing
# entry should show up as an overestimate worth investigating, never an
# underestimate that hides real spend.
CLAUDE_MODEL_RATES = {
    "claude-sonnet-5": {
        "input": CLAUDE_SONNET_INPUT_PER_MILLION, "output": CLAUDE_SONNET_OUTPUT_PER_MILLION,
        "cache_write": CLAUDE_SONNET_CACHE_WRITE_PER_MILLION, "cache_read": CLAUDE_SONNET_CACHE_READ_PER_MILLION,
    },
    "claude-haiku-4-5-20251001": {
        "input": CLAUDE_HAIKU_INPUT_PER_MILLION, "output": CLAUDE_HAIKU_OUTPUT_PER_MILLION,
        "cache_write": CLAUDE_HAIKU_CACHE_WRITE_PER_MILLION, "cache_read": CLAUDE_HAIKU_CACHE_READ_PER_MILLION,
    },
}


def estimate_claude_cost_usd(
    model: str, input_tokens: int, output_tokens: int,
    cache_write_tokens: int = 0, cache_read_tokens: int = 0,
) -> float:
    rates = CLAUDE_MODEL_RATES.get(model, CLAUDE_MODEL_RATES["claude-sonnet-5"])
    return (
        input_tokens * rates["input"] + output_tokens * rates["output"]
        + cache_write_tokens * rates["cache_write"] + cache_read_tokens * rates["cache_read"]
    ) / 1_000_000


def estimate_cost_usd(
    claude_input_tokens: int, claude_output_tokens: int,
    realtime_audio_in: int, realtime_audio_out: int,
    realtime_text_in: int, realtime_text_out: int,
    claude_cache_write_tokens: int = 0, claude_cache_read_tokens: int = 0,
) -> float:
    # Kept as the flat, single-model (Sonnet) estimator — eval/insights_agent.py's
    # _cost_for_call now prices each llm_usage event's own recorded model via
    # estimate_claude_cost_usd above and uses this function only for the
    # realtime-only portion (claude tokens passed as 0), so this signature's
    # "assume Sonnet" behavior is intentional, not stale.
    return (
        claude_input_tokens * CLAUDE_SONNET_INPUT_PER_MILLION
        + claude_output_tokens * CLAUDE_SONNET_OUTPUT_PER_MILLION
        + claude_cache_write_tokens * CLAUDE_SONNET_CACHE_WRITE_PER_MILLION
        + claude_cache_read_tokens * CLAUDE_SONNET_CACHE_READ_PER_MILLION
        + realtime_audio_in * REALTIME_AUDIO_INPUT_PER_MILLION
        + realtime_audio_out * REALTIME_AUDIO_OUTPUT_PER_MILLION
        + realtime_text_in * REALTIME_TEXT_INPUT_PER_MILLION
        + realtime_text_out * REALTIME_TEXT_OUTPUT_PER_MILLION
    ) / 1_000_000
