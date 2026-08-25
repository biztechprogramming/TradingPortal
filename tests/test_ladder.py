"""Ladder rung construction, sizing, and the place-or-modify behaviour."""

from __future__ import annotations

import time

import pytest

from trading.audit import AuditLog
from trading.config import Settings
from trading.errors import OrderValidationError
from trading.ladder import (
    ANCHOR_HIGH_SINCE_LOW,
    ANCHOR_LOW_SINCE_HIGH,
    ACTION_MODIFIED,
    ACTION_PLACED,
    ACTION_RESIZED,
    ACTION_UNCHANGED,
    LadderExecutor,
    build_rung_views,
    build_rungs,
    round_to_ladder_tick,
    rung_key,
)
from trading.models import Quote, Side, TradingMode


def _settings(tmp_path, **overrides):
    s = Settings(
        mode=TradingMode.PAPER,
        log_dir=tmp_path / "log",
        state_dir=tmp_path / "state",
        market_hours_guard=False,
        cash_buffer_dollars=0.0,
    )
    for key, value in overrides.items():
        setattr(s, key, value)
    return s


def _quote(symbol="QQQ", bid=500.0, ask=500.04, last=500.02, high=502.0, low=498.0):
    return Quote(
        symbol=symbol, bid=bid, ask=ask, last=last, timestamp=time.time(),
        bid_size=100, ask_size=100, source="test",
        day_high=high, day_low=low,
    )


def _anchors(high, low):
    """The two swing levels the ladder hangs off."""
    return [(ANCHOR_HIGH_SINCE_LOW, high), (ANCHOR_LOW_SINCE_HIGH, low)]


# --- rung construction ------------------------------------------------------


def test_rungs_span_both_anchors(tmp_path):
    s = _settings(tmp_path, ladder_levels=2)
    rungs = build_rungs("QQQ", s, _anchors(502.00, 498.00))
    prices = [r.price for r in rungs]

    # 2 levels either side of each of two anchors, plus the anchors: 10 rungs.
    assert len(rungs) == 10
    assert prices == sorted(prices), "rungs must run low to high"
    assert 502.00 in prices and 498.00 in prices
    assert 502.20 in prices and 497.80 in prices


def test_psq_uses_sub_penny_step_without_penny_rounding(tmp_path):
    """PSQ's 0.005 step must survive; TICK_SIZE=0.01 must not collapse it."""
    s = _settings(tmp_path, ladder_levels=2)
    rungs = build_rungs("PSQ", s, _anchors(32.20, 32.00))
    prices = sorted({r.price for r in rungs})

    assert s.ladder_step_for("PSQ") == 0.005
    assert 32.205 in prices, "sub-penny rung was rounded away"
    assert 32.195 in prices
    # Neighbouring rungs are exactly one half-cent apart, not one cent.
    high_rungs = sorted(
        r.price for r in rungs if r.anchor == ANCHOR_HIGH_SINCE_LOW
    )
    diffs = {round(b - a, 6) for a, b in zip(high_rungs, high_rungs[1:])}
    assert diffs == {0.005}


def test_off_grid_anchor_still_gives_evenly_spaced_rungs(tmp_path):
    """A low of 497.75 is not on the 0.10 step grid.

    Rounding each rung independently used to merge neighbours (497.75 and
    497.85 both landing on 497.8) and drop levels.
    """
    s = _settings(tmp_path, ladder_levels=5)
    rungs = [r for r in build_rungs("QQQ", s, _anchors(502.50, 497.75))
             if r.anchor == ANCHOR_LOW_SINCE_HIGH]
    prices = [r.price for r in rungs]

    assert len(prices) == 11, "every level must survive rounding"
    assert len(set(prices)) == 11, "no two rungs may share a price"
    diffs = {round(b - a, 6) for a, b in zip(prices, prices[1:])}
    assert diffs == {0.10}, f"uneven spacing: {sorted(diffs)}"
    # The true session low is representable on the 0.01 tick, so it survives.
    assert 497.75 in prices


def test_qqq_uses_the_penny_tick_not_the_step(tmp_path):
    """The tick is what the venue accepts; the step is how far rungs walk."""
    s = _settings(tmp_path)
    assert s.ladder_step_for("QQQ") == 0.10
    assert s.ladder_tick_for("QQQ") == 0.01
    # PSQ's step is finer than a penny, so there the tick follows the step.
    assert s.ladder_tick_for("PSQ") == 0.005


def test_qqq_and_psq_get_different_steps(tmp_path):
    s = _settings(tmp_path)
    assert s.ladder_step_for("QQQ") == 0.10
    assert s.ladder_step_for("PSQ") == 0.005


def test_no_rungs_without_usable_anchors(tmp_path):
    """A zero level must mean 'no anchor', never 'anchor at zero'."""
    s = _settings(tmp_path)
    assert build_rungs("QQQ", s, _anchors(0.0, 0.0)) == []
    assert build_rungs("QQQ", s, []) == []


def test_one_usable_anchor_still_builds_its_side(tmp_path):
    """Losing one level must not silently blank the whole ladder."""
    s = _settings(tmp_path, ladder_levels=2)
    rungs = build_rungs("QQQ", s, _anchors(502.00, 0.0))
    assert len(rungs) == 5
    assert {r.anchor for r in rungs} == {ANCHOR_HIGH_SINCE_LOW}


def test_overlapping_anchors_do_not_duplicate_a_price(tmp_path):
    """A narrow session range makes the two ladders overlap."""
    s = _settings(tmp_path, ladder_levels=5)
    rungs = build_rungs("QQQ", s, _anchors(500.20, 500.00))
    prices = [r.price for r in rungs]
    assert len(prices) == len(set(prices))


def test_marketable_flag_marks_rungs_at_or_above_the_ask(tmp_path):
    s = _settings(tmp_path, ladder_levels=3)
    views = build_rung_views("QQQ", s, _anchors(500.10, 499.90),
                             reference_price=500.04, last_price=500.02)
    for v in views:
        assert v.marketable == (v.price >= 500.04)
    assert any(v.marketable for v in views)
    assert any(not v.marketable for v in views)


def test_round_to_ladder_tick_handles_sub_penny():
    assert round_to_ladder_tick(32.2049, 0.005) == 32.205
    assert round_to_ladder_tick(500.137, 0.10) == 500.10


def test_rung_key_binds_symbol_and_price():
    assert rung_key("qqq", 500.1) == "LADDER_BUY:QQQ@500.1000"
    assert rung_key("QQQ", 500.1) != rung_key("PSQ", 500.1)
    assert rung_key("QQQ", 500.1) != rung_key("QQQ", 500.2)


# --- execution --------------------------------------------------------------


@pytest.fixture
def executor(tmp_path):
    s = _settings(tmp_path)
    return s, LadderExecutor(s, AuditLog(s.log_dir), "cmd-1")


def _broker(settings, cash=10_000.0):
    from trading.brokers.simulated import SimulatedBroker

    broker = SimulatedBroker(settings)
    broker.connect()
    broker.set_cash(cash)
    return broker


def test_place_new_order_sizes_against_the_whole_account(executor):
    settings, ex = executor
    broker = _broker(settings, cash=10_000.0)
    broker.set_quote("QQQ", 500.00, 500.04, day_high=502.0, day_low=498.0)

    result = ex.place(broker, "QQQ", 498.00)

    assert result.ok, result.errors
    assert result.action == ACTION_PLACED
    assert result.quantity == 20            # 10000 / 498 -> 20 whole shares
    assert result.marketable is False
    assert len(result.open_orders) == 1
    # An order is not a position.
    assert result.position_quantity == 0.0


def test_second_click_modifies_instead_of_stacking(executor):
    settings, ex = executor
    broker = _broker(settings, cash=10_000.0)
    broker.set_quote("QQQ", 500.00, 500.04, day_high=502.0, day_low=498.0)

    first = ex.place(broker, "QQQ", 498.00)
    assert first.action == ACTION_PLACED

    second = ex.place(broker, "QQQ", 497.50)

    assert second.ok, second.errors
    assert second.action == ACTION_MODIFIED
    assert second.previous_limit_price == 498.00
    assert second.limit_price == 497.50
    # The whole point: still exactly one working order, not two.
    assert len(second.open_orders) == 1
    assert second.open_orders[0]["limit_price"] == 497.50


def test_clicking_the_working_price_again_changes_nothing(executor):
    settings, ex = executor
    broker = _broker(settings, cash=10_000.0)
    broker.set_quote("QQQ", 500.00, 500.04, day_high=502.0, day_low=498.0)

    ex.place(broker, "QQQ", 498.00)
    again = ex.place(broker, "QQQ", 498.00)

    assert again.ok
    assert again.action == ACTION_UNCHANGED
    assert len(again.open_orders) == 1


def test_reprice_upward_shrinks_rather_than_overspending(executor):
    """A higher limit must never quietly exceed the cash that funded it."""
    settings, ex = executor
    broker = _broker(settings, cash=10_000.0)
    broker.set_quote("QQQ", 500.00, 500.04, day_high=502.0, day_low=460.0)

    first = ex.place(broker, "QQQ", 460.00)
    assert first.quantity == 21             # 10000 / 460

    second = ex.place(broker, "QQQ", 500.00)

    assert second.ok, second.errors
    assert second.action == ACTION_RESIZED
    assert second.quantity == 20            # 10000 / 500, not still 21
    assert second.quantity * second.limit_price <= 10_000.0
    assert any("reduced" in w for w in second.warnings)


def test_price_far_from_the_market_is_refused(executor):
    settings, ex = executor
    broker = _broker(settings, cash=10_000.0)
    broker.set_quote("QQQ", 500.00, 500.04, day_high=502.0, day_low=498.0)

    result = ex.place(broker, "QQQ", 100.00)

    assert not result.ok
    assert any("fat-finger" in e for e in result.errors)
    assert result.open_orders == []


def test_insufficient_funds_places_nothing(executor):
    settings, ex = executor
    broker = _broker(settings, cash=10.0)
    broker.set_quote("QQQ", 500.00, 500.04, day_high=502.0, day_low=498.0)

    result = ex.place(broker, "QQQ", 498.00)

    assert not result.ok
    assert any("insufficient funds" in e for e in result.errors)
    assert result.open_orders == []


def test_sell_orders_are_never_repriced(executor):
    """The ladder is buy-only; a working exit must be left alone."""
    settings, ex = executor
    broker = _broker(settings, cash=10_000.0)
    broker.set_quote("QQQ", 500.00, 500.04, day_high=502.0, day_low=498.0)
    broker.set_position("QQQ", 10)
    broker.submit_limit_order("QQQ", Side.SELL, 10, 505.00)

    result = ex.place(broker, "QQQ", 498.00)

    assert result.ok, result.errors
    # A new buy was placed; the sell was untouched.
    assert result.action == ACTION_PLACED
    sells = [o for o in result.open_orders if o["side"] == "SELL"]
    assert len(sells) == 1
    assert sells[0]["limit_price"] == 505.00
