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


def test_a_slow_quote_never_blocks_a_dashboard_read(service):
    """Once a quote is known, a stale one is shown while a fresh one loads."""
    import threading
    import time

    broker = service.brokers["schwab"]
    first, _ = service.display_quote(broker, "QQQ")
    assert first is not None

    release = threading.Event()
    real = broker.get_quote

    def slow(symbol):
        release.wait(5)
        return real(symbol)

    broker.get_quote = slow
    service.settings = service.settings.__class__(
        **{**service.settings.__dict__, "quote_display_ttl_seconds": 0.0})
    started = time.monotonic()
    quote, error = service.display_quote(broker, "QQQ")
    assert time.monotonic() - started < 0.5
    assert quote is first and error is None
    release.set()
