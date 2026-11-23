"""A login swapping the brokers mid-read must not show closed connections."""
from __future__ import annotations

from tests.conftest import make_broker


def _swap_during_first_read(service, settings):
    fresh = {
        "schwab": make_broker(settings, "schwab", cash=50_000.0),
        "ibkr": make_broker(settings, "ibkr", cash=25_000.0),
    }
    old = service.brokers["schwab"]
    real = old.get_positions
    calls = {"n": 0}

    def get_positions():
        calls["n"] += 1
        service.brokers = fresh          # what rebuild_brokers does
        raise RuntimeError("connection closed")

    old.get_positions = get_positions
    return calls, real


def test_status_is_retaken_after_a_mid_read_rebuild(service, settings):
    calls, _ = _swap_during_first_read(service, settings)
    status = service.status()
    assert calls["n"] == 1
    assert all(b["error"] is None for b in status["brokers"].values())


def test_holdings_is_retaken_after_a_mid_read_rebuild(service, settings):
    calls, _ = _swap_during_first_read(service, settings)
    holdings = service.holdings()
    assert calls["n"] == 1
    assert all(b["error"] is None for b in holdings["brokers"].values())
