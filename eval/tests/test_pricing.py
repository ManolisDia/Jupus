from eval.pricing import (
    CLAUDE_HAIKU_INPUT_PER_MILLION,
    CLAUDE_HAIKU_OUTPUT_PER_MILLION,
    CLAUDE_SONNET_CACHE_READ_PER_MILLION,
    CLAUDE_SONNET_CACHE_WRITE_PER_MILLION,
    CLAUDE_SONNET_INPUT_PER_MILLION,
    CLAUDE_SONNET_OUTPUT_PER_MILLION,
    REALTIME_AUDIO_INPUT_PER_MILLION,
    REALTIME_AUDIO_OUTPUT_PER_MILLION,
    REALTIME_TEXT_INPUT_PER_MILLION,
    REALTIME_TEXT_OUTPUT_PER_MILLION,
    estimate_claude_cost_usd,
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


def test_estimate_cost_usd_defaults_cache_tokens_to_zero():
    # Positional 6-arg call (no cache args) must behave identically to
    # before Phase 13 added cache pricing — existing call sites that
    # haven't been touched yet must not silently start costing differently.
    assert estimate_cost_usd(1000, 500, 0, 0, 0, 0) == estimate_cost_usd(
        1000, 500, 0, 0, 0, 0, claude_cache_write_tokens=0, claude_cache_read_tokens=0
    )


def test_estimate_cost_usd_includes_cache_write_and_read():
    result = estimate_cost_usd(
        claude_input_tokens=0, claude_output_tokens=0,
        realtime_audio_in=0, realtime_audio_out=0, realtime_text_in=0, realtime_text_out=0,
        claude_cache_write_tokens=1000, claude_cache_read_tokens=2000,
    )
    expected = (1000 * CLAUDE_SONNET_CACHE_WRITE_PER_MILLION + 2000 * CLAUDE_SONNET_CACHE_READ_PER_MILLION) / 1_000_000
    assert result == expected


def test_estimate_claude_cost_usd_prices_sonnet():
    result = estimate_claude_cost_usd("claude-sonnet-5", 1000, 500)
    expected = (1000 * CLAUDE_SONNET_INPUT_PER_MILLION + 500 * CLAUDE_SONNET_OUTPUT_PER_MILLION) / 1_000_000
    assert result == expected


def test_estimate_claude_cost_usd_prices_haiku_cheaper_than_sonnet():
    # Phase 13, Decision 3 — the whole point of a per-tool Haiku override is
    # that it's cheaper; a regression here would silently erase that.
    haiku = estimate_claude_cost_usd("claude-haiku-4-5-20251001", 1000, 500)
    sonnet = estimate_claude_cost_usd("claude-sonnet-5", 1000, 500)
    expected_haiku = (1000 * CLAUDE_HAIKU_INPUT_PER_MILLION + 500 * CLAUDE_HAIKU_OUTPUT_PER_MILLION) / 1_000_000
    assert haiku == expected_haiku
    assert haiku < sonnet


def test_estimate_claude_cost_usd_unknown_model_falls_back_to_sonnet_rate():
    # An unrecognized model id must never silently price at zero — falling
    # back to the more expensive known rate surfaces as an overestimate
    # worth investigating, not a hidden underestimate.
    assert estimate_claude_cost_usd("some-future-model", 1000, 500) == estimate_claude_cost_usd(
        "claude-sonnet-5", 1000, 500
    )
