"""Market-order commands and the per-command spread override."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from trading.audit import AuditLog
from trading.errors import ConfirmationRequired, MarketClosed, OrderValidationError
from trading.models import Command, Exposure
from trading.orchestrator import TradingService
from trading.strategy import Strategy
from trading.validation import check_regular_session
from tests.conftest import make_broker, make_settings


def run(settings, tmp_path, broker, command, market=False):
    strategy = Strategy(
        settings, AuditLog(tmp_path / "log"), "cmd-m",
        sleep=lambda _: None, market=market,
    )
    return strategy.run(broker, command)


def confirmed(service, command, **kwargs):
    """Run a command through its confirmation step."""
    with pytest.raises(ConfirmationRequired) as exc:
        service.execute(command, **kwargs)
    return service.execute(command, confirm_token=exc.value.token, **kwargs)


# --- strategy ---------------------------------------------------------------


def test_market_long_from_flat_uses_market_orders(settings, tmp_path):
    broker = make_broker(settings, cash=10_000)
    result = run(settings, tmp_path, broker, Command.GO_LONG, market=True)

    assert result.ok, result.errors
    assert result.exposure_after is Exposure.LONG
    assert broker.submitted_orders
    assert all(o.order_type == "MARKET" for o in broker.submitted_orders)
    assert broker.replaced_orders == []
    # Filled at the ask, not at the price it was sized against.
    assert result.entry_fill.average_price == pytest.approx(500.04)


def test_market_short_from_long_sells_then_buys(settings, tmp_path):
    broker = make_broker(settings, cash=0, positions={"QQQ": 20})
    result = run(settings, tmp_path, broker, Command.GO_SHORT, market=True)

    assert result.ok, result.errors
    assert broker.get_position("QQQ") is None
    assert broker.get_position("PSQ").quantity > 0
    assert [o.order_type for o in broker.submitted_orders] == ["MARKET", "MARKET"]


def test_market_flat_sells_everything(settings, tmp_path):
    broker = make_broker(settings, cash=0, positions={"QQQ": 20, "PSQ": 5})
    result = run(settings, tmp_path, broker, Command.GO_FLAT, market=True)

    assert result.ok, result.errors
    assert result.exposure_after is Exposure.FLAT


def test_market_buy_is_sized_with_headroom_for_a_moving_price(settings, tmp_path):
    # $10,000 at the ask would buy 19 shares; sized at the slippage guard
    # (25 ticks above the ask) the buy leaves room for the price to move.
    broker = make_broker(settings, cash=10_000)
    result = run(settings, tmp_path, broker, Command.GO_LONG, market=True)
    assert result.ok, result.errors
    assert broker.get_position("QQQ").quantity * 500.29 <= 10_000 - 25


# --- regular session only ---------------------------------------------------


def _et(hour, minute):
    # 2026-11-12 is a Thursday; Eastern is UTC-5 in November.
    return datetime(2026, 11, 12, hour + 5, minute, tzinfo=timezone.utc)


@pytest.mark.parametrize("hour,minute,allowed", [
    (9, 29, False), (9, 30, True), (15, 59, True), (16, 0, False), (7, 0, False),
])
def test_market_orders_only_in_regular_session(tmp_path, hour, minute, allowed):
    settings = make_settings(tmp_path, market_hours_guard=True)
    if allowed:
        check_regular_session(settings, _et(hour, minute))
    else:
        with pytest.raises(MarketClosed):
            check_regular_session(settings, _et(hour, minute))


def test_extended_hours_override_does_not_allow_market_orders(tmp_path):
    settings = make_settings(
        tmp_path, market_hours_guard=True, allow_outside_rth=True)
    with pytest.raises(MarketClosed):
        check_regular_session(settings, _et(8, 0))


# --- confirmation -----------------------------------------------------------


def test_market_command_always_needs_confirmation(service):
    # The fixture has REQUIRE_CONFIRMATION off; market orders confirm anyway.
    with pytest.raises(ConfirmationRequired):
        service.execute(Command.GO_FLAT, market=True)
    result = confirmed(service, Command.GO_LONG, market=True)
    assert result.status == "OK"
    assert result.to_dict()["order_type"] == "MARKET"


def test_limit_confirmation_cannot_authorise_a_market_order(tmp_path):
    settings = make_settings(tmp_path, require_confirmation=True)
    service = TradingService(settings, brokers={
        "sim": make_broker(settings, "sim", cash=10_000)})
    with pytest.raises(ConfirmationRequired) as exc:
        service.execute(Command.GO_LONG)
    with pytest.raises(ConfirmationRequired):
        service.execute(Command.GO_LONG, confirm_token=exc.value.token,
                        market=True)


# --- spread ---------------------------------------------------------------


def wide_service(tmp_path, spread):
    settings = make_settings(tmp_path)
    broker = make_broker(settings, "sim", cash=10_000,
                         qqq_bid=500.00, qqq_ask=500.00 + spread)
    return TradingService(settings, brokers={"sim": broker})


def test_wide_spread_blocks_a_limit_command(tmp_path):
    result = wide_service(tmp_path, 0.15).execute(Command.GO_LONG)
    assert result.status == "FAILED"
    assert "WideSpreadError" in result.results[0].errors[0]


def test_spread_scale_widens_the_limit_for_one_command(tmp_path):
    service = wide_service(tmp_path, 0.15)
    assert service.execute(Command.GO_LONG, spread_scale=2.0).status == "OK"
    # The configured limit itself is untouched.
    assert service.settings.max_spread_ticks_for("QQQ") == 10


def test_spread_scale_can_narrow_the_limit(tmp_path):
    result = wide_service(tmp_path, 0.04).execute(
        Command.GO_LONG, spread_scale=0.3)
    assert result.status == "FAILED"
    assert "WideSpreadError" in result.results[0].errors[0]


def test_spread_scale_out_of_range_is_rejected(service):
    with pytest.raises(OrderValidationError):
        service.execute(Command.GO_LONG, spread_scale=50)


def test_market_order_ignores_the_tick_spread_limit(tmp_path):
    service = wide_service(tmp_path, 0.15)
    assert confirmed(service, Command.GO_LONG, market=True).status == "OK"


def test_market_order_still_refuses_a_broken_book(tmp_path):
    # $5 wide on $500 is 100 bps, past the 75 bps backstop.
    service = wide_service(tmp_path, 5.00)
    result = confirmed(service, Command.GO_LONG, market=True)
    assert result.status == "FAILED"
    assert "WideSpreadError" in result.results[0].errors[0]


# --- API --------------------------------------------------------------------


@pytest.fixture
def client(tmp_path):
    from trading.api import create_app

    settings = make_settings(tmp_path, api_token="test-token")
    service = TradingService(settings, brokers={
        "sim": make_broker(settings, "sim", cash=10_000,
                           qqq_bid=500.00, qqq_ask=500.15)})
    with TestClient(create_app(settings, service)) as c:
        yield c


AUTH = {"Authorization": "Bearer test-token"}


def test_api_market_order_round_trip(client):
    body = {"command": "GO_LONG", "order_type": "MARKET", "request_id": "m1"}
    first = client.post("/api/command", json=body, headers=AUTH)
    assert first.status_code == 409
    assert first.json()["status"] == "CONFIRMATION_REQUIRED"
    assert first.json()["order_type"] == "MARKET"

    second = client.post("/api/command", headers=AUTH, json={
        **body, "confirm_token": first.json()["confirm_token"]})
    assert second.status_code == 200, second.json()
    assert second.json()["order_type"] == "MARKET"


def test_api_spread_scale(client):
    resp = client.post("/api/command", headers=AUTH, json={
        "command": "GO_LONG", "spread_scale": 2})
    assert resp.status_code == 200, resp.json()


def test_api_rejects_unknown_order_type(client):
    resp = client.post("/api/command", headers=AUTH, json={
        "command": "GO_LONG", "order_type": "STOP"})
    assert resp.status_code == 422
