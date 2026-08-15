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
    s = make_settings(tmp_path, buy_slippage_bps=10, max_slippage_bps=50)
    d = compute_limit_price(q(), Side.BUY, s)
    # 500.04 * 1.0010 = 500.54, rounded up to the tick.
    assert d.limit_price == 500.55
    assert d.limit_price > d.reference_price
    assert d.reference_price == 500.04
    assert not d.capped


def test_sell_limit_sits_below_the_bid(tmp_path):
    s = make_settings(tmp_path, sell_slippage_bps=10, max_slippage_bps=50)
    d = compute_limit_price(q(), Side.SELL, s)
    # 500.00 * 0.9990 = 499.50
    assert d.limit_price == 499.50
    assert d.limit_price < d.reference_price


def test_percentage_and_bps_are_equivalent(tmp_path, monkeypatch):
    from trading.config import _resolve_bps

    monkeypatch.setenv("BUY_SLIPPAGE_PCT", "0.10")
    assert _resolve_bps("BUY_SLIPPAGE_PCT", "BUY_SLIPPAGE_BPS", 5) == 10.0
    monkeypatch.delenv("BUY_SLIPPAGE_PCT")
    monkeypatch.setenv("BUY_SLIPPAGE_BPS", "10")
    assert _resolve_bps("BUY_SLIPPAGE_PCT", "BUY_SLIPPAGE_BPS", 5) == 10.0


def test_reprice_attempts_escalate_then_clamp(tmp_path):
    s = make_settings(
        tmp_path, buy_slippage_bps=10, reprice_step_bps=10, max_slippage_bps=30
    )
    anchor = 500.04
    prices = [
        compute_limit_price(q(), Side.BUY, s, anchor_price=anchor, attempt=i).limit_price
        for i in range(5)
    ]
    # Monotonically more aggressive...
    assert prices == sorted(prices)
    # ...but never past anchor * (1 + 30bps).
    assert max(prices) <= anchor * 1.0030 + 1e-9


def test_buy_aborts_when_market_runs_past_the_guard(tmp_path):
    s = make_settings(tmp_path, max_slippage_bps=20)
    anchor = 500.04
    runaway = q(bid=505.00, ask=505.04)   # +100 bps
    with pytest.raises(MaxSlippageExceeded, match="Aborting"):
        compute_limit_price(runaway, Side.BUY, s, anchor_price=anchor)


def test_sell_aborts_when_market_falls_past_the_guard(tmp_path):
    s = make_settings(tmp_path, max_slippage_bps=20)
    anchor = 500.00
    collapse = q(bid=490.00, ask=490.10)
    with pytest.raises(MaxSlippageExceeded, match="Aborting"):
        compute_limit_price(collapse, Side.SELL, s, anchor_price=anchor)


def test_clamped_buy_limit_stays_marketable(tmp_path):
    """Clamping to the guard must never drop the limit below the offer."""
    s = make_settings(
        tmp_path, buy_slippage_bps=1, sell_slippage_bps=1, max_slippage_bps=1.5
    )
    anchor = 500.00
    # Ask has moved up to within the guard but above anchor*(1+1bps).
    d = compute_limit_price(q(bid=500.05, ask=500.07), Side.BUY, s,
                            anchor_price=anchor)
    assert d.limit_price >= 500.07


def test_should_reprice_requires_a_full_tick(tmp_path):
    s = make_settings(tmp_path)
    d = compute_limit_price(q(), Side.BUY, s)
    assert not should_reprice(d.limit_price, d, s.tick_size)
    assert should_reprice(d.limit_price - 0.05, d, s.tick_size)
    assert not should_reprice(d.limit_price + 0.05, d, s.tick_size)


def test_spread_bps_maths():
    quote = Quote("QQQ", 100.00, 100.10, 100.05, time.time())
    assert quote.mid == pytest.approx(100.05)
    assert quote.spread_bps == pytest.approx(9.995, rel=1e-3)
