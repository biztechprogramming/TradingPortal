"""The dashboard's quote memo: fast, and never used to price an order."""

from __future__ import annotations

import time

import pytest

from trading.orchestrator import TradingService
from tests.conftest import make_broker, make_settings


@pytest.fixture
def service(tmp_path):
    settings = make_settings(tmp_path)
    settings.quote_display_ttl_seconds = 5.0
    brokers = {"sim": make_broker(settings, "sim", cash=10_000.0)}
    brokers["sim"].connect()
    return TradingService(settings, brokers=brokers)


class _CountingBroker:
    def __init__(self, fail=False, name="counting"):
        self.name = name
        self.calls = 0
        self.fail = fail
        self.connected = True

    def get_quote(self, symbol):
        self.calls += 1
        if self.fail:
            raise RuntimeError("no market data subscription")
        from trading.models import Quote

        # A realistic penny spread: a wide one would fail quote validation in
        # the execution test below for reasons unrelated to memoisation.
        return Quote(symbol, 100.00, 100.01, 100.005, time.time())


def test_repeat_reads_hit_the_broker_once(service):
    broker = _CountingBroker()
    for _ in range(5):
        quote, error = service.display_quote(broker, "QQQ")
        assert quote is not None and error is None
    assert broker.calls == 1


def test_failures_are_memoised_too(service):
    """A missing subscription must not cost a retry storm on every poll."""
    broker = _CountingBroker(fail=True)
    for _ in range(5):
        quote, error = service.display_quote(broker, "QQQ")
        assert quote is None
        assert "no market data subscription" in error
    assert broker.calls == 1, "the failing quote was re-attempted"


def test_the_memo_expires(service):
    service.settings.quote_display_ttl_seconds = 0.0
    broker = _CountingBroker()
    service.display_quote(broker, "QQQ")
    service.display_quote(broker, "QQQ")
    assert broker.calls == 2


def test_symbols_and_brokers_do_not_share_an_entry(service):
    a = _CountingBroker(name="alpha")
    b = _CountingBroker(name="beta")
    service.display_quote(a, "QQQ")
    service.display_quote(a, "PSQ")
    service.display_quote(b, "QQQ")
    assert a.calls == 2 and b.calls == 1


def test_execution_never_uses_the_memo(service, tmp_path):
    """Order pricing must always re-read, so staleness checks stay meaningful."""
    from trading.audit import AuditLog
    from trading.execution import ExecutionEngine

    broker = _CountingBroker()
    engine = ExecutionEngine(service.settings, AuditLog(tmp_path / "log"), "cmd")

    service.display_quote(broker, "QQQ")
    engine.fresh_quote(broker, "QQQ")
    engine.fresh_quote(broker, "QQQ")

    # One memoised read plus two genuine ones.
    assert broker.calls == 3
