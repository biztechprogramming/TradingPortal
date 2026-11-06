"""Adverse conditions: bad quotes, bad markets, bad brokers."""

from __future__ import annotations

from trading.audit import AuditLog
from trading.brokers.simulated import SimulationConfig
from trading.models import Command, Exposure, Side
from trading.strategy import Strategy
from tests.conftest import make_broker, make_rising_broker, make_settings


def run(settings, tmp_path, broker, command: Command):
    strategy = Strategy(
        settings, AuditLog(tmp_path / "log"), "cmd-fail", sleep=lambda _: None
    )
    return strategy.run(broker, command)


def test_insufficient_cash_reports_and_buys_nothing(settings, tmp_path):
    broker = make_broker(settings, cash=100.0)   # far below one QQQ share
    result = run(settings, tmp_path, broker, Command.GO_LONG)

    assert not result.ok
    assert any("insufficient funds" in e for e in result.errors)
    assert broker.submitted_orders == []
    assert result.exposure_after is Exposure.FLAT


def test_cash_below_the_buffer_buys_nothing(settings, tmp_path):
    s = make_settings(tmp_path, cash_buffer_dollars=1_000.0)
    broker = make_broker(s, cash=900.0)
    result = run(s, tmp_path, broker, Command.GO_LONG)

    assert not result.ok
    assert broker.submitted_orders == []


def test_wide_spread_aborts_before_ordering(settings, tmp_path):
    s = make_settings(tmp_path, max_spread_bps=30)
    broker = make_broker(s, cash=50_000)
    broker.set_quote("QQQ", 495.00, 505.00)      # ~200 bps wide

    result = run(s, tmp_path, broker, Command.GO_LONG)
    assert not result.ok
    assert any("WideSpreadError" in e for e in result.errors)
    assert broker.submitted_orders == []


def test_stale_quote_aborts_before_ordering(settings, tmp_path):
    s = make_settings(tmp_path, max_quote_age_seconds=5)
    broker = make_broker(s, cash=50_000)
    broker.set_quote("QQQ", 500.00, 500.04, age=120)

    result = run(s, tmp_path, broker, Command.GO_LONG)
    assert not result.ok
    assert any("StaleQuoteError" in e for e in result.errors)
    assert broker.submitted_orders == []


def test_halted_security_aborts(settings, tmp_path):
    broker = make_broker(settings, cash=50_000)
    broker.set_quote("QQQ", 500.00, 500.04, halted=True)

    result = run(settings, tmp_path, broker, Command.GO_LONG)
    assert not result.ok
    assert any("HaltedSecurityError" in e for e in result.errors)
    assert broker.submitted_orders == []


def test_rejected_order_is_reported_not_swallowed(settings, tmp_path):
    broker = make_broker(
        settings, cash=50_000,
        sim=SimulationConfig(reject_next="order rejected by exchange"),
    )
    result = run(settings, tmp_path, broker, Command.GO_LONG)

    assert not result.ok
    assert any("order rejected by exchange" in e for e in result.errors)
    assert result.exposure_after is Exposure.FLAT


def test_api_timeout_marks_the_result_as_needing_attention(settings, tmp_path):
    """A timed-out mutation may have been applied; never assume otherwise."""
    broker = make_broker(
        settings, cash=50_000, sim=SimulationConfig(timeout_next=True)
    )
    result = run(settings, tmp_path, broker, Command.GO_LONG)

    assert not result.ok
    assert result.needs_attention
    assert any("BrokerTimeout" in e for e in result.errors)


def test_partial_fill_on_a_switch_flags_an_intermediate_state(settings, tmp_path):
    """Selling PSQ only partly must not silently proceed to buy QQQ."""
    s = make_settings(
        tmp_path, order_fill_timeout_seconds=0.05, max_reprice_attempts=0,
        order_poll_interval_seconds=0.001,
    )
    broker = make_broker(
        s, cash=0, positions={"PSQ": 300},
        sim=SimulationConfig(fill_chunk=100, polls_before_fill=1),
    )

    # 100 shares fill, then liquidity vanishes and the rest never trades.
    original_status = broker.get_order_status

    def stalling_status(order_id: str):
        order = original_status(order_id)
        if order.filled_quantity >= 100:
            broker.sim.never_fill = True
        return order

    broker.get_order_status = stalling_status  # type: ignore[assignment]
    result = run(s, tmp_path, broker, Command.GO_LONG)

    assert not result.ok
    assert result.needs_attention
    assert any("PARTIAL EXECUTION" in e for e in result.errors)
    # And no QQQ was bought while PSQ was still held.
    assert broker.get_position("QQQ") is None


def test_price_moving_during_execution_still_completes(settings, tmp_path):
    s = make_settings(
        tmp_path, buy_offset_ticks=1, sell_offset_ticks=1,
        reprice_step_ticks=1, reprice_interval_seconds=0.0,
        max_reprice_attempts=2, order_poll_interval_seconds=0.001,
        order_fill_timeout_seconds=2.0,
    )
    broker = make_rising_broker(s, ("QQQ", 500.10, 500.14), cash=20_000)
    result = run(s, tmp_path, broker, Command.GO_LONG)

    assert result.ok, result.errors
    assert result.exposure_after is Exposure.LONG
    assert result.entry_fill.reprice_count >= 1


def test_quote_failure_during_reprice_does_not_abandon_the_order(
    settings, tmp_path
):
    """A transient bad quote mid-flight must not orphan a working order."""
    s = make_settings(
        tmp_path, reprice_interval_seconds=0.0, order_poll_interval_seconds=0.001,
        order_fill_timeout_seconds=1.0, max_quote_age_seconds=5,
    )
    broker = make_rising_broker(s, ("QQQ", 500.10, 500.14), cash=20_000,
                                polls_before_fill=3)

    calls = {"n": 0}
    original = broker.get_quote

    def flaky(symbol: str):
        # Call 1 sizes the order, call 2 opens the leg, call 3 is the first
        # reprice — that is the one we make stale.
        calls["n"] += 1
        if calls["n"] == 3:
            broker.set_quote(symbol, 500.10, 500.14, age=999)
            quote = original(symbol)
            broker.set_quote(symbol, 500.10, 500.14)
            return quote
        return original(symbol)

    broker.get_quote = flaky  # type: ignore[assignment]
    result = run(s, tmp_path, broker, Command.GO_LONG)

    assert result.ok, result.errors
    assert result.exposure_after is Exposure.LONG


def test_go_flat_sells_unsettled_shares_too(settings, tmp_path):
    """Shares bought today are sellable; going flat sells all of them."""
    from trading.models import Position

    broker = make_broker(settings, cash=0, positions={"QQQ": 100})
    real_positions = broker.get_positions

    def with_unsettled():
        return [
            Position(p.symbol, p.quantity, p.average_price, p.market_value,
                     settled_quantity=min(p.quantity, 60))
            for p in real_positions()
        ]

    broker.get_positions = with_unsettled  # type: ignore[assignment]
    result = run(settings, tmp_path, broker, Command.GO_FLAT)

    sells = [o for o in broker.submitted_orders if o.side is Side.SELL]
    assert sells and sells[0].quantity == 100
    assert any("settle" in w for w in result.warnings)
    assert result.ok, result.errors


def test_go_flat_that_leaves_shares_is_a_failure(settings, tmp_path):
    """Selling less than is held must not be reported as success."""
    from trading.models import Position

    broker = make_broker(settings, cash=0, positions={"QQQ": 100})
    broker.get_positions = lambda: [  # type: ignore[assignment]
        Position("QQQ", 100, 480.0, 50_000.0)
    ]
    result = run(settings, tmp_path, broker, Command.GO_FLAT)

    assert not result.ok
    assert any("not flat" in e for e in result.errors)


def test_go_flat_prices_from_another_account_when_quote_missing(
    settings, tmp_path
):
    """An account with no market data still sells, priced from a peer."""
    from trading.errors import InvalidQuoteError

    broker = make_broker(settings, name="ibkr", cash=0, positions={"QQQ": 10})
    peer = make_broker(settings, name="schwab")
    own_quote = broker.get_quote

    def no_market_data(symbol):
        own_quote(symbol)  # keep the simulator's book current
        raise InvalidQuoteError("no bid/ask")

    broker.get_quote = no_market_data  # type: ignore[assignment]
    strategy = Strategy(
        settings, AuditLog(tmp_path / "log"), "cmd-fail", sleep=lambda _: None,
        quote_fallback=lambda name, sym: ("schwab", peer.get_quote(sym)),
    )
    result = strategy.run(broker, Command.GO_FLAT)

    assert result.ok, result.errors
    assert result.exposure_after is Exposure.FLAT


def test_missing_quote_without_a_peer_still_fails(settings, tmp_path):
    from trading.errors import InvalidQuoteError

    broker = make_broker(settings, name="ibkr", cash=0, positions={"QQQ": 10})

    def no_market_data(symbol):
        raise InvalidQuoteError("no bid/ask")

    broker.get_quote = no_market_data  # type: ignore[assignment]
    strategy = Strategy(
        settings, AuditLog(tmp_path / "log"), "cmd-fail", sleep=lambda _: None,
        quote_fallback=lambda name, sym: None,
    )
    result = strategy.run(broker, Command.GO_FLAT)

    assert not result.ok
    assert any("no bid/ask" in e for e in result.errors)
