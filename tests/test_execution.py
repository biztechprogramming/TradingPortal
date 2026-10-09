"""Order lifecycle: fills, partial fills, repricing, timeouts, rejections."""

from __future__ import annotations

import pytest

from trading.audit import AuditLog
from trading.brokers.simulated import SimulationConfig
from trading.errors import (
    MaxSlippageExceeded,
    OrderRejected,
    OrderTimeout,
)
from trading.execution import ExecutionEngine, LegRequest, leg_from_exception
from trading.models import Balances, OrderState, Side
from tests.conftest import make_broker, make_rising_broker, make_settings


def engine(settings, tmp_path) -> ExecutionEngine:
    return ExecutionEngine(
        settings, AuditLog(tmp_path / "log"), "cmd-test", sleep=lambda _: None
    )


def test_simple_buy_fills_completely(settings, tmp_path):
    broker = make_broker(settings, cash=100_000)
    fill = engine(settings, tmp_path).execute_leg(
        broker,
        LegRequest("QQQ", Side.BUY, 10, budget=10 * 500.55,
                   balances=Balances(100_000, 100_000)),
    )
    assert fill.complete
    assert fill.filled_quantity == 10
    # Fills at the offer, which is inside our limit.
    assert fill.average_price == pytest.approx(500.04)
    assert len(broker.submitted_orders) == 1


def test_simple_sell_fills_completely(settings, tmp_path):
    broker = make_broker(settings, positions={"QQQ": 10})
    fill = engine(settings, tmp_path).execute_leg(
        broker, LegRequest("QQQ", Side.SELL, 10, position_quantity=10)
    )
    assert fill.complete
    assert fill.average_price == pytest.approx(500.00)


def test_partial_fills_are_accumulated(settings, tmp_path):
    """Three chunks of 4 must be reported as one 12-share fill."""
    broker = make_broker(
        settings, cash=100_000, sim=SimulationConfig(fill_chunk=4)
    )
    fill = engine(settings, tmp_path).execute_leg(
        broker,
        LegRequest("QQQ", Side.BUY, 12, budget=12 * 500.55,
                   balances=Balances(100_000, 100_000)),
    )
    assert fill.complete
    assert fill.filled_quantity == 12
    assert broker.get_position("QQQ").quantity == 12


def test_timeout_leaves_a_partial_fill_and_cancels_the_rest(settings, tmp_path):
    """An unfillable remainder must time out, cancel, and report honestly."""
    s = make_settings(
        tmp_path, order_fill_timeout_seconds=0.05, max_reprice_attempts=0,
        order_poll_interval_seconds=0.001,
    )
    broker = make_broker(s, cash=100_000, sim=SimulationConfig(never_fill=True))
    with pytest.raises(OrderTimeout) as exc:
        engine(s, tmp_path).execute_leg(
            broker,
            LegRequest("QQQ", Side.BUY, 10, budget=10 * 500.55,
                       balances=Balances(100_000, 100_000)),
        )
    fill = leg_from_exception(exc.value)
    assert fill is not None and fill.filled_quantity == 0
    # Nothing may be left working at the broker.
    assert broker.get_open_orders() == []
    assert broker.cancelled_orders


def test_rejected_order_surfaces_as_an_error(settings, tmp_path):
    broker = make_broker(
        settings, cash=100_000,
        sim=SimulationConfig(reject_next="account restricted"),
    )
    with pytest.raises(OrderRejected, match="account restricted"):
        engine(settings, tmp_path).execute_leg(
            broker,
            LegRequest("QQQ", Side.BUY, 10, budget=10 * 500.55,
                       balances=Balances(100_000, 100_000)),
        )


def test_price_moving_up_triggers_a_reprice(settings, tmp_path):
    """Market runs away after submission, we chase it once, and it fills."""
    s = make_settings(
        tmp_path, buy_offset_ticks=1, sell_offset_ticks=1, spread_multiple=0.0,
        reprice_step_ticks=1, reprice_interval_seconds=0.0,
        max_reprice_attempts=1,
        order_fill_timeout_seconds=2.0, order_poll_interval_seconds=0.001,
    )
    # Initial limit 500.05; the offer jumps a dime to 500.14, out of reach.
    broker = make_rising_broker(s, ("QQQ", 500.10, 500.14), cash=100_000)

    fill = engine(s, tmp_path).execute_leg(
        broker,
        LegRequest("QQQ", Side.BUY, 10, budget=10 * 520.0,
                   balances=Balances(100_000, 100_000)),
    )
    assert fill.complete
    assert fill.filled_quantity == 10
    assert fill.reprice_count >= 1
    assert broker.replaced_orders or len(broker.submitted_orders) > 1


def test_reprice_stops_at_the_hard_slippage_guard(settings, tmp_path):
    """A runaway market must abort, not be chased indefinitely."""
    s = make_settings(
        tmp_path, buy_offset_ticks=1, sell_offset_ticks=1,
        max_slippage_ticks=25, reprice_interval_seconds=0.0,
        order_fill_timeout_seconds=1.0, order_poll_interval_seconds=0.001,
    )
    # Anchor 500.04, guard 500.29; the market gaps 2% away to 510.04.
    broker = make_rising_broker(s, ("QQQ", 510.00, 510.04), cash=1_000_000)

    with pytest.raises(MaxSlippageExceeded, match="Aborting"):
        engine(s, tmp_path).execute_leg(
            broker,
            LegRequest("QQQ", Side.BUY, 5, budget=5 * 600.0,
                       balances=Balances(1_000_000, 1_000_000)),
        )
    # And nothing is left working after the abort.
    assert broker.get_open_orders() == []


def test_reprice_shrinks_the_order_rather_than_overspending(settings, tmp_path):
    """A higher limit must shrink the order, never breach the cash budget."""
    s = make_settings(
        tmp_path, buy_offset_ticks=1, sell_offset_ticks=1, spread_multiple=1.0,
        reprice_step_ticks=1, reprice_interval_seconds=0.0,
        order_fill_timeout_seconds=1.0, order_poll_interval_seconds=0.001,
        cash_buffer_dollars=0,
    )
    # Barely enough for 20 shares at the first limit (500.08); once the
    # reprice lifts it to 500.18 only one whole lot still fits.
    broker = make_rising_broker(s, ("QQQ", 500.10, 500.14), cash=10_002.00)

    fill = engine(s, tmp_path).execute_leg(
        broker,
        LegRequest("QQQ", Side.BUY, 20, budget=10_002.00,
                   balances=Balances(10_002.00, 10_002.00)),
    )
    # Fewer shares than requested, bought without exceeding the budget, and
    # still a whole lot -- the shrink must not produce a ragged 19.
    assert fill.filled_quantity == 10
    assert fill.filled_quantity % 10 == 0
    assert fill.notional <= 10_002.00
    assert any("reduced remaining quantity" in n for n in fill.notes)
    assert broker.get_available_cash().cash_available >= 0


def test_conflicting_orders_are_cancelled_first(settings, tmp_path):
    broker = make_broker(settings, cash=100_000, positions={"PSQ": 100})
    stale = broker.submit_limit_order("PSQ", Side.SELL, 50, 30.00)
    assert stale.state is OrderState.WORKING

    cancelled = engine(settings, tmp_path).cancel_conflicting_orders(
        broker, {"QQQ", "PSQ"}
    )
    assert stale.order_id in cancelled
    assert broker.get_open_orders() == []


def test_unconfirmed_cancel_raises_rather_than_proceeding(settings, tmp_path):
    """If we cannot prove an order is dead we must not trade around it."""
    broker = make_broker(settings, cash=100_000, positions={"PSQ": 100})
    # Priced above the bid so it rests rather than filling.
    broker.submit_limit_order("PSQ", Side.SELL, 50, 40.00)

    # Make cancels silently do nothing, as a stuck venue would.
    broker.cancel_order = lambda order_id: None  # type: ignore[assignment]
    s = make_settings(tmp_path, cancel_confirm_timeout_seconds=0.05,
                      order_poll_interval_seconds=0.001)
    with pytest.raises(OrderTimeout, match="not confirmed"):
        engine(s, tmp_path).cancel_conflicting_orders(broker, {"QQQ", "PSQ"})


def test_broker_read_timeout_propagates(settings, tmp_path):
    from trading.errors import BrokerTimeout

    broker = make_broker(settings, cash=100_000,
                         sim=SimulationConfig(timeout_next_read=True))
    with pytest.raises(BrokerTimeout):
        engine(settings, tmp_path).execute_leg(
            broker,
            LegRequest("QQQ", Side.BUY, 5, budget=5 * 500.55,
                       balances=Balances(100_000, 100_000)),
        )
