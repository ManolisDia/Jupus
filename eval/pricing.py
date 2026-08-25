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
"""

CLAUDE_SONNET_INPUT_PER_MILLION = 2.00
CLAUDE_SONNET_OUTPUT_PER_MILLION = 10.00
CLAUDE_SONNET_CACHE_WRITE_PER_MILLION = CLAUDE_SONNET_INPUT_PER_MILLION * 1.25
CLAUDE_SONNET_CACHE_READ_PER_MILLION = CLAUDE_SONNET_INPUT_PER_MILLION * 0.1
REALTIME_AUDIO_INPUT_PER_MILLION = 32.00
REALTIME_AUDIO_OUTPUT_PER_MILLION = 64.00
REALTIME_TEXT_INPUT_PER_MILLION = 4.00
REALTIME_TEXT_OUTPUT_PER_MILLION = 24.00


def estimate_cost_usd(
    claude_input_tokens: int, claude_output_tokens: int,
    realtime_audio_in: int, realtime_audio_out: int,
    realtime_text_in: int, realtime_text_out: int,
    claude_cache_write_tokens: int = 0, claude_cache_read_tokens: int = 0,
) -> float:
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
