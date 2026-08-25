from eval.pricing import (
    CLAUDE_SONNET_INPUT_PER_MILLION,
    CLAUDE_SONNET_OUTPUT_PER_MILLION,
    REALTIME_AUDIO_INPUT_PER_MILLION,
    REALTIME_AUDIO_OUTPUT_PER_MILLION,
    REALTIME_TEXT_INPUT_PER_MILLION,
    REALTIME_TEXT_OUTPUT_PER_MILLION,
    estimate_cost_usd,
)


def test_estimate_cost_usd_zero_tokens_is_zero_dollars():
    assert estimate_cost_usd(0, 0, 0, 0, 0, 0) == 0.0


def test_estimate_cost_usd_matches_hand_computed_value():
    result = estimate_cost_usd(
        claude_input_tokens=1000, claude_output_tokens=500,
        realtime_audio_in=2000, realtime_audio_out=1500,
        realtime_text_in=300, realtime_text_out=100,
    )
    expected = (
        1000 * CLAUDE_SONNET_INPUT_PER_MILLION
        + 500 * CLAUDE_SONNET_OUTPUT_PER_MILLION
        + 2000 * REALTIME_AUDIO_INPUT_PER_MILLION
        + 1500 * REALTIME_AUDIO_OUTPUT_PER_MILLION
        + 300 * REALTIME_TEXT_INPUT_PER_MILLION
        + 100 * REALTIME_TEXT_OUTPUT_PER_MILLION
    ) / 1_000_000
    assert result == expected
