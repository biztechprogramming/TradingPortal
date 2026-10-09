"""Every state transition the three commands can produce."""

from __future__ import annotations

from trading.audit import AuditLog
from trading.models import Command, Exposure, Side
from trading.strategy import Strategy
from tests.conftest import make_broker


def strategy(settings, tmp_path) -> Strategy:
    return Strategy(
        settings, AuditLog(tmp_path / "log"), "cmd-x", sleep=lambda _: None
    )


def run(settings, tmp_path, broker, command: Command):
    return strategy(settings, tmp_path).run(broker, command)


# --- flat -> directional ----------------------------------------------------


def test_flat_to_long(settings, tmp_path):
    broker = make_broker(settings, cash=10_000)
    result = run(settings, tmp_path, broker, Command.GO_LONG)

    assert result.ok, result.errors
    assert result.exposure_before is Exposure.FLAT
    assert result.exposure_after is Exposure.LONG
    # 98% of 10000 / 500.08 limit = 19 shares, rounded down to one lot of 10.
    assert broker.get_position("QQQ").quantity == 10
    assert broker.get_position("PSQ") is None
    assert result.entry_fill.side is Side.BUY
    assert result.cash_after < result.cash_before


def test_flat_to_short(settings, tmp_path):
    broker = make_broker(settings, cash=10_000)
    result = run(settings, tmp_path, broker, Command.GO_SHORT)

    assert result.ok, result.errors
    assert result.exposure_after is Exposure.SHORT
    # 98% of 10000 / 32.14 = 304 shares -> 300, which lands inside the
    # 95-98% band because a PSQ lot is small relative to the account.
    assert broker.get_position("PSQ").quantity == 300
    assert broker.get_position("QQQ") is None


# --- directional -> flat ----------------------------------------------------


def test_long_to_flat(settings, tmp_path):
    broker = make_broker(settings, cash=100, positions={"QQQ": 20})
    result = run(settings, tmp_path, broker, Command.GO_FLAT)

    assert result.ok, result.errors
    assert result.exposure_before is Exposure.LONG
    assert result.exposure_after is Exposure.FLAT
    assert broker.get_position("QQQ") is None
    assert result.cash_after > result.cash_before
    # GO FLAT must never buy anything.
    assert all(f.side is Side.SELL for f in result.fills)


def test_short_to_flat(settings, tmp_path):
    broker = make_broker(settings, cash=100, positions={"PSQ": 300})
    result = run(settings, tmp_path, broker, Command.GO_FLAT)

    assert result.ok, result.errors
    assert result.exposure_after is Exposure.FLAT
    assert broker.get_position("PSQ") is None
    assert all(f.side is Side.SELL for f in result.fills)


def test_mixed_to_flat_sells_both(settings, tmp_path):
    broker = make_broker(settings, cash=0, positions={"QQQ": 5, "PSQ": 50})
    result = run(settings, tmp_path, broker, Command.GO_FLAT)

    assert result.ok, result.errors
    assert result.exposure_before is Exposure.MIXED
    assert result.exposure_after is Exposure.FLAT
    assert {f.symbol for f in result.fills} == {"QQQ", "PSQ"}


def test_already_flat_flat_is_a_noop(settings, tmp_path):
    broker = make_broker(settings, cash=1_000)
    result = run(settings, tmp_path, broker, Command.GO_FLAT)

    assert result.ok
    assert result.fills == []
    assert any("already flat" in w for w in result.warnings)
    assert broker.submitted_orders == []


# --- side switches ----------------------------------------------------------


def test_long_to_short_sells_qqq_before_buying_psq(settings, tmp_path):
    broker = make_broker(settings, cash=50, positions={"QQQ": 20})
    result = run(settings, tmp_path, broker, Command.GO_SHORT)

    assert result.ok, result.errors
    assert result.exposure_before is Exposure.LONG
    assert result.exposure_after is Exposure.SHORT
    assert broker.get_position("QQQ") is None
    assert broker.get_position("PSQ").quantity > 0

    # Order matters: the QQQ sale must precede the PSQ purchase.
    sides = [(o.symbol, o.side) for o in broker.submitted_orders]
    assert sides[0] == ("QQQ", Side.SELL)
    assert sides[-1] == ("PSQ", Side.BUY)


def test_short_to_long_sells_psq_before_buying_qqq(settings, tmp_path):
    broker = make_broker(settings, cash=50, positions={"PSQ": 300})
    result = run(settings, tmp_path, broker, Command.GO_LONG)

    assert result.ok, result.errors
    assert result.exposure_after is Exposure.LONG
    assert broker.get_position("PSQ") is None
    assert broker.get_position("QQQ").quantity > 0

    sides = [(o.symbol, o.side) for o in broker.submitted_orders]
    assert sides[0] == ("PSQ", Side.SELL)
    assert sides[-1] == ("QQQ", Side.BUY)


# --- idempotent re-entry ----------------------------------------------------


def test_already_long_does_not_sell_and_rebuy(settings, tmp_path):
    """Re-issuing GO LONG must never churn an existing QQQ position."""
    broker = make_broker(settings, cash=10, positions={"QQQ": 20})
    result = run(settings, tmp_path, broker, Command.GO_LONG)

    assert result.ok, result.errors
    assert result.exposure_after is Exposure.LONG
    assert broker.get_position("QQQ").quantity == 20
    # No sells at all, and no buy either: $10 buys no whole share.
    assert not any(o.side is Side.SELL for o in broker.submitted_orders)
    assert broker.submitted_orders == []
    assert any("already holding" in w for w in result.warnings)


def test_already_short_does_not_sell_and_rebuy(settings, tmp_path):
    broker = make_broker(settings, cash=10, positions={"PSQ": 300})
    result = run(settings, tmp_path, broker, Command.GO_SHORT)

    assert result.ok, result.errors
    assert broker.get_position("PSQ").quantity == 300
    assert broker.submitted_orders == []


def test_already_long_tops_up_with_idle_cash(settings, tmp_path):
    """Spare cash is deployed, but the existing shares are left alone."""
    broker = make_broker(settings, cash=8_000, positions={"QQQ": 20})
    result = run(settings, tmp_path, broker, Command.GO_LONG)

    assert result.ok, result.errors
    assert broker.get_position("QQQ").quantity == 30   # 20 + one more lot
    assert not any(o.side is Side.SELL for o in broker.submitted_orders)


# --- reconciliation ---------------------------------------------------------


def test_positions_are_reread_not_assumed(settings, tmp_path):
    """The strategy must trust the broker, not the previous command."""
    broker = make_broker(settings, cash=5_000)
    run(settings, tmp_path, broker, Command.GO_LONG)

    # Simulate the position vanishing behind our back (manual sale elsewhere).
    broker.set_position("QQQ", 0)
    result = run(settings, tmp_path, broker, Command.GO_FLAT)

    assert result.ok
    assert result.exposure_before is Exposure.FLAT
    assert any("already flat" in w for w in result.warnings)
