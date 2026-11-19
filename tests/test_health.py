"""Trading-availability checks: per-account probes, IBKR link state, skipping."""

from __future__ import annotations

import threading

from trading.errors import BrokerAuthError, BrokerLinkDown
from trading.health import (
    DOWN, LINK_DOWN, LOGIN_REQUIRED, OK, UNRESPONSIVE, HealthMonitor,
)
from trading.models import Command
from trading.orchestrator import TradingService
from tests.conftest import make_broker, make_settings


def _monitor(brokers, timeout=0.3):
    return HealthMonitor(lambda: brokers, interval=0, timeout=timeout)


def test_healthy_account_is_green_with_a_time(settings):
    b = make_broker(settings, "schwab")
    h = _monitor({"schwab": b}).check_all()["schwab"]
    assert h.state == OK and h.tradable
    assert h.checked_at > 0 and h.latency_ms is not None


def test_failures_are_classified(settings):
    auth = make_broker(settings, "schwab")
    link = make_broker(settings, "ibkr")
    gone = make_broker(settings, "other")

    def expired(max_age=0.0):
        raise BrokerAuthError("refresh token expired")

    def cut_off(max_age=0.0):
        raise BrokerLinkDown("IB Gateway lost its connection to IBKR")

    def broken(max_age=0.0):
        raise ConnectionError("refused")

    auth.health_check = expired
    auth.broker_type = "schwab"
    link.health_check = cut_off
    gone.health_check = broken
    out = _monitor({"a": auth, "l": link, "g": gone}).check_all()
    assert out["a"].state == LOGIN_REQUIRED
    assert "Schwab session expired, log in" in out["a"].message
    assert out["l"].state == LINK_DOWN
    assert out["g"].state == DOWN


def test_a_hung_probe_reads_as_unresponsive_and_is_not_stacked(settings):
    b = make_broker(settings, "ibkr")
    b.broker_type = "ibkr"
    release = threading.Event()
    calls = []

    def hang(max_age=0.0):
        calls.append(1)
        release.wait(5)

    b.health_check = hang
    mon = _monitor({"ibkr": b}, timeout=0.1)
    try:
        assert mon.check_all()["ibkr"].state == UNRESPONSIVE
        assert "Gateway unresponsive" in mon.get("ibkr").message
        mon.check_all()
        assert len(calls) == 1          # still waiting on the first one
    finally:
        release.set()


def test_unhealthy_rechecks_a_stale_red_before_deciding(settings):
    b = make_broker(settings, "schwab")
    state = {"bad": True}

    def probe(max_age=0.0):
        if state["bad"]:
            raise BrokerAuthError("expired")

    b.health_check = probe
    mon = _monitor({"schwab": b})
    mon.check_all()
    assert not mon.get("schwab").tradable
    state["bad"] = False                # user logged in again
    assert mon.unhealthy(["schwab"]) == {}


def _service(tmp_path, bad: str | None):
    s = make_settings(tmp_path, health_check_interval_seconds=0,
                      health_check_timeout_seconds=0.5)
    brokers = {
        "schwab": make_broker(s, "schwab", cash=50_000.0),
        "ibkr": make_broker(s, "ibkr", cash=25_000.0),
    }
    if bad:
        def cut_off(max_age=0.0):
            raise BrokerLinkDown("IB Gateway lost its connection to IBKR")
        brokers[bad].health_check = cut_off
    return TradingService(s, brokers=brokers), brokers


def test_buy_command_skips_a_red_account(tmp_path):
    svc, brokers = _service(tmp_path, bad="ibkr")
    result = svc.execute(Command.GO_LONG)
    by = {r.broker: r for r in result.results}
    assert by["schwab"].ok
    assert not by["ibkr"].ok
    assert "skipped, not tradable" in by["ibkr"].errors[0]
    assert brokers["ibkr"].get_open_orders() == []
    assert (brokers["ibkr"].get_position("QQQ") or None) is None


def test_go_flat_is_never_skipped(tmp_path):
    svc, brokers = _service(tmp_path, bad="ibkr")
    result = svc.execute(Command.GO_FLAT)
    assert all("skipped" not in " ".join(r.errors) for r in result.results)


def test_status_reports_health_per_account(tmp_path):
    svc, _ = _service(tmp_path, bad="ibkr")
    svc.health.check_all()
    status = svc.status()
    assert status["brokers"]["schwab"]["health"]["tradable"] is True
    assert status["brokers"]["ibkr"]["health"]["state"] == LINK_DOWN


def test_skipping_can_be_turned_off(tmp_path):
    s = make_settings(tmp_path, health_check_interval_seconds=0,
                      health_skip_unhealthy=False)
    b = make_broker(s, "ibkr")
    b.health_check = lambda max_age=0.0: (_ for _ in ()).throw(BrokerLinkDown("x"))
    svc = TradingService(s, brokers={"ibkr": b})
    result = svc.execute(Command.GO_LONG)
    assert result.results[0].ok
