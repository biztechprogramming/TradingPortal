"""Limit-price algorithm and quote validation."""

from __future__ import annotations

import time

import pytest

from trading.errors import (
    HaltedSecurityError,
    InvalidQuoteError,
    MaxSlippageExceeded,
    StaleQuoteError,
    WideSpreadError,
)
from trading.models import Quote, Side
from trading.pricing import (
    compute_limit_price,
    round_to_tick,
    should_reprice,
    validate_quote,
)
from tests.conftest import make_settings


def q(bid=500.00, ask=500.04, last=500.02, age=0.0, **kw) -> Quote:
    return Quote("QQQ", bid, ask, last, time.time() - age, 100, 100, **kw)


# --- rounding ---------------------------------------------------------------


def test_round_to_tick_directions():
    assert round_to_tick(500.041, 0.01, "up") == 500.05
    assert round_to_tick(500.049, 0.01, "down") == 500.04
    assert round_to_tick(500.044, 0.01, "nearest") == 500.04
    # An exact tick must not be nudged by floating-point error.
    assert round_to_tick(500.04, 0.01, "up") == 500.04
    assert round_to_tick(500.04, 0.01, "down") == 500.04


# --- validation -------------------------------------------------------------


def test_valid_quote_passes(tmp_path):
    validate_quote(q(), make_settings(tmp_path))


def test_stale_quote_rejected(tmp_path):
    s = make_settings(tmp_path, max_quote_age_seconds=5)
    with pytest.raises(StaleQuoteError, match="old"):
        validate_quote(q(age=30), s)


def test_future_timestamp_rejected(tmp_path):
    with pytest.raises(StaleQuoteError, match="future"):
        validate_quote(q(age=-120), make_settings(tmp_path))


def test_wide_spread_rejected(tmp_path):
    s = make_settings(tmp_path, max_spread_bps=20)
    with pytest.raises(WideSpreadError, match="spread"):
        validate_quote(q(bid=495.00, ask=505.00), s)


def test_zero_and_crossed_quotes_rejected(tmp_path):
    s = make_settings(tmp_path)
    with pytest.raises(InvalidQuoteError):
        validate_quote(q(bid=0.0), s)
    with pytest.raises(InvalidQuoteError):
        validate_quote(q(ask=0.0), s)
    with pytest.raises(InvalidQuoteError, match="crossed"):
        validate_quote(q(bid=501.0, ask=500.0), s)


def test_halted_security_rejected(tmp_path):
    with pytest.raises(HaltedSecurityError):
        validate_quote(q(halted=True), make_settings(tmp_path))


# --- limit computation ------------------------------------------------------


def test_buy_limit_sits_above_the_ask(tmp_path):
    s = make_settings(tmp_path)
    d = compute_limit_price(q(), Side.BUY, s)
    # offset = max(1 tick, 1 x the 4-cent spread) = 0.04.
    assert d.limit_price == 500.08
    assert d.limit_price > d.reference_price
    assert d.reference_price == 500.04
    assert d.offset_dollars == 0.04
    assert not d.capped


def test_sell_limit_sits_below_the_bid(tmp_path):
    s = make_settings(tmp_path)
    d = compute_limit_price(q(), Side.SELL, s)
    assert d.limit_price == 499.96
    assert d.limit_price < d.reference_price


def test_penny_wide_qqq_crosses_by_exactly_one_tick(tmp_path):
    """The normal QQQ book: a single tick is the whole cushion needed."""
    s = make_settings(tmp_path)
    d = compute_limit_price(q(bid=500.03, ask=500.04), Side.BUY, s)
    assert d.limit_price == 500.05
    assert d.offset_ticks == 1.0
    # The same settings on a 20x cheaper symbol give the same one-tick cross,
    # which is the entire point of pricing in ticks rather than basis points.
    psq = Quote("PSQ", 32.11, 32.12, 32.115, time.time())
    assert compute_limit_price(psq, Side.BUY, s).limit_price == 32.13


def test_offset_widens_with_the_spread(tmp_path):
    """A thin book gets more cushion without loosening a normal one."""
    s = make_settings(tmp_path, spread_multiple=1.0)
    tight = compute_limit_price(q(bid=500.03, ask=500.04), Side.BUY, s)
    wide = compute_limit_price(q(bid=499.94, ask=500.04), Side.BUY, s)
    assert tight.offset_dollars == 0.01
    assert wide.offset_dollars == 0.10


def test_tick_guard_binds_before_the_bps_guard_on_qqq(tmp_path):
    """50 bps is $3.00 on QQQ; the tick guard is what must actually bind."""
    s = make_settings(tmp_path, max_slippage_bps=50, max_slippage_ticks=25)
    assert s.guard_distance_for("QQQ", 500.04) == pytest.approx(0.25)
    d = compute_limit_price(q(), Side.BUY, s, anchor_price=500.04)
    assert d.guard_price == pytest.approx(500.29)
    assert d.guard_ticks == 25.0
    # A 30-cent run past the anchor is outside the guard and must abort.
    with pytest.raises(MaxSlippageExceeded, match="Aborting"):
        compute_limit_price(q(bid=500.30, ask=500.34), Side.BUY, s,
                            anchor_price=500.04)


def test_bps_guard_still_backstops_a_fat_fingered_tick_count(tmp_path):
    """Whichever guard is tighter wins, in both directions."""
    s = make_settings(
        tmp_path, buy_slippage_bps=1, sell_slippage_bps=1,
        max_slippage_bps=2, max_slippage_ticks=500,
    )
    # 2 bps of 500.04 is $0.10, well inside 500 ticks.
    assert s.guard_distance_for("QQQ", 500.04) == pytest.approx(0.10, abs=1e-4)


def test_spread_guard_is_measured_in_ticks(tmp_path):
    s = make_settings(tmp_path, max_spread_ticks=5, max_spread_bps=75)
    validate_quote(q(bid=500.00, ask=500.05), s)          # exactly 5 ticks
    with pytest.raises(WideSpreadError, match="ticks"):
        validate_quote(q(bid=500.00, ask=500.06), s)      # 6 ticks


def test_percentage_and_bps_are_equivalent(tmp_path, monkeypatch):
    from trading.config import _resolve_bps

    monkeypatch.setenv("BUY_SLIPPAGE_PCT", "0.10")
    assert _resolve_bps("BUY_SLIPPAGE_PCT", "BUY_SLIPPAGE_BPS", 5) == 10.0
    monkeypatch.delenv("BUY_SLIPPAGE_PCT")
    monkeypatch.setenv("BUY_SLIPPAGE_BPS", "10")
    assert _resolve_bps("BUY_SLIPPAGE_PCT", "BUY_SLIPPAGE_BPS", 5) == 10.0


def test_reprice_attempts_escalate_then_clamp(tmp_path):
    s = make_settings(
        tmp_path, buy_offset_ticks=1, reprice_step_ticks=2,
        spread_multiple=0.0, max_slippage_ticks=8, max_slippage_bps=50,
    )
    anchor = 500.04
    prices = [
        compute_limit_price(q(), Side.BUY, s, anchor_price=anchor, attempt=i).limit_price
        for i in range(6)
    ]
    # One tick, then three, five, seven...
    assert prices[:4] == [500.05, 500.07, 500.09, 500.11]
    # Monotonically more aggressive...
    assert prices == sorted(prices)
    # ...but never past the 8-tick guard.
    assert max(prices) <= anchor + 0.08 + 1e-9


def test_buy_aborts_when_market_runs_past_the_guard(tmp_path):
    s = make_settings(tmp_path, max_slippage_bps=20, max_slippage_ticks=0)
    anchor = 500.04
    runaway = q(bid=505.00, ask=505.04)   # +100 bps
    with pytest.raises(MaxSlippageExceeded, match="Aborting"):
        compute_limit_price(runaway, Side.BUY, s, anchor_price=anchor)


def test_sell_aborts_when_market_falls_past_the_guard(tmp_path):
    s = make_settings(tmp_path, max_slippage_bps=20, max_slippage_ticks=0)
    anchor = 500.00
    collapse = q(bid=490.00, ask=490.10)
    with pytest.raises(MaxSlippageExceeded, match="Aborting"):
        compute_limit_price(collapse, Side.SELL, s, anchor_price=anchor)


def test_clamped_buy_limit_stays_marketable(tmp_path):
    """Clamping to the guard must never drop the limit below the offer."""
    s = make_settings(
        tmp_path, buy_offset_ticks=1, sell_offset_ticks=1,
        spread_multiple=0.0, max_slippage_ticks=7, max_slippage_bps=50,
    )
    anchor = 500.00
    # Ask has moved up to within the guard but above anchor*(1+1bps).
    d = compute_limit_price(q(bid=500.05, ask=500.07), Side.BUY, s,
                            anchor_price=anchor)
    assert d.limit_price >= 500.07


def test_should_reprice_requires_a_full_tick(tmp_path):
    s = make_settings(tmp_path)
    d = compute_limit_price(q(), Side.BUY, s)
    assert d.limit_price == 500.08
    assert not should_reprice(d.limit_price, d, s.tick_size)
    assert should_reprice(d.limit_price - 0.05, d, s.tick_size)
    assert not should_reprice(d.limit_price + 0.05, d, s.tick_size)


def test_spread_bps_maths():
    quote = Quote("QQQ", 100.00, 100.10, 100.05, time.time())
    assert quote.mid == pytest.approx(100.05)
    assert quote.spread_bps == pytest.approx(9.995, rel=1e-3)
