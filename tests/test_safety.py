"""Safeguards: duplicates, locking, confirmation, mismatch, dry-run, secrets."""

from __future__ import annotations

import json
import threading
import time

import pytest

from trading.brokers.simulated import (
    ShadowBroker,
    SimulatedBroker,
    SimulationConfig,
)
from trading.errors import (
    ConfirmationRequired,
    DuplicateCommand,
    ExecutionLocked,
    SafetyLimitExceeded,
    UnreconciledState,
)
from trading.models import Command, TradingMode
from trading.orchestrator import TradingService
from tests.conftest import make_broker, make_settings


def build_service(tmp_path, **overrides) -> TradingService:
    settings = make_settings(tmp_path, **overrides)
    brokers = {
        "schwab": make_broker(settings, "schwab", cash=50_000.0),
        "ibkr": make_broker(settings, "ibkr", cash=25_000.0),
    }
    return TradingService(settings, brokers=brokers)


# --- duplicate suppression --------------------------------------------------


def test_duplicate_command_inside_the_window_is_suppressed(tmp_path):
    svc = build_service(tmp_path, duplicate_command_window_seconds=60.0)
    svc.execute(Command.GO_LONG)
    with pytest.raises(DuplicateCommand, match="already issued"):
        svc.execute(Command.GO_LONG)


def test_force_overrides_the_duplicate_window(tmp_path):
    svc = build_service(tmp_path, duplicate_command_window_seconds=60.0)
    svc.execute(Command.GO_LONG)
    result = svc.execute(Command.GO_LONG, force=True)
    assert result.status in ("OK", "PARTIAL", "FAILED")


def test_a_different_command_is_not_a_duplicate(tmp_path):
    svc = build_service(tmp_path, duplicate_command_window_seconds=60.0)
    svc.execute(Command.GO_LONG)
    svc.execute(Command.GO_FLAT)     # allowed immediately


def test_request_id_makes_a_retry_idempotent(tmp_path):
    """A Shortcut that fires twice on a flaky network must not trade twice."""
    svc = build_service(tmp_path, duplicate_command_window_seconds=0.0)
    svc.execute(Command.GO_LONG, request_id="abc-123")
    with pytest.raises(DuplicateCommand, match="already executed"):
        svc.execute(Command.GO_LONG, request_id="abc-123")


def test_request_id_dedupe_survives_a_restart(tmp_path):
    svc = build_service(tmp_path, duplicate_command_window_seconds=0.0)
    svc.execute(Command.GO_LONG, request_id="persist-me")

    reborn = build_service(tmp_path, duplicate_command_window_seconds=0.0)
    with pytest.raises(DuplicateCommand):
        reborn.execute(Command.GO_LONG, request_id="persist-me")


# --- execution lock ---------------------------------------------------------


def test_commands_cannot_run_simultaneously(tmp_path):
    svc = build_service(tmp_path)
    with svc.lock.acquire("test-holder"):
        with pytest.raises(ExecutionLocked):
            svc.execute(Command.GO_SHORT)


def test_long_and_short_serialise_under_contention(tmp_path):
    """A LONG racing a SHORT must end with exactly one winner."""
    svc = build_service(tmp_path, duplicate_command_window_seconds=0.0)
    for broker in svc.brokers.values():
        broker.sim = SimulationConfig(latency=0.02)

    outcomes: list[str] = []
    lock = threading.Lock()

    def fire(command: Command) -> None:
        try:
            svc.execute(command, force=True)
            with lock:
                outcomes.append(f"{command.value}:ok")
        except ExecutionLocked:
            with lock:
                outcomes.append(f"{command.value}:locked")

    threads = [
        threading.Thread(target=fire, args=(Command.GO_LONG,)),
        threading.Thread(target=fire, args=(Command.GO_SHORT,)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert sorted(outcomes) != []
    assert sum(1 for o in outcomes if o.endswith(":locked")) >= 1, outcomes
    # Whichever won, the account is never left holding both instruments.
    for broker in svc.brokers.values():
        held = {p.symbol for p in broker.get_positions() if p.quantity > 0}
        assert held != {"QQQ", "PSQ"}


# --- live confirmation ------------------------------------------------------


def test_live_directional_commands_require_confirmation(tmp_path):
    svc = build_service(tmp_path, mode=TradingMode.LIVE,
                        live_trading_enabled=True, require_confirmation=True)
    with pytest.raises(ConfirmationRequired) as exc:
        svc.execute(Command.GO_LONG)

    token = exc.value.token
    result = svc.execute(Command.GO_LONG, confirm_token=token, force=True)
    assert result.status == "OK", result.to_dict()


def test_confirmation_token_is_single_use(tmp_path):
    svc = build_service(tmp_path, mode=TradingMode.LIVE,
                        live_trading_enabled=True, require_confirmation=True)
    with pytest.raises(ConfirmationRequired) as exc:
        svc.execute(Command.GO_LONG)
    token = exc.value.token
    svc.execute(Command.GO_LONG, confirm_token=token, force=True)

    with pytest.raises(ConfirmationRequired):
        svc.execute(Command.GO_LONG, confirm_token=token, force=True)


def test_confirmation_token_is_bound_to_its_command(tmp_path):
    svc = build_service(tmp_path, mode=TradingMode.LIVE,
                        live_trading_enabled=True, require_confirmation=True)
    with pytest.raises(ConfirmationRequired) as exc:
        svc.execute(Command.GO_LONG)

    # A LONG token must not authorise a SHORT.
    with pytest.raises(ConfirmationRequired):
        svc.execute(Command.GO_SHORT, confirm_token=exc.value.token)


def test_confirmation_expires(tmp_path):
    svc = build_service(tmp_path, mode=TradingMode.LIVE,
                        live_trading_enabled=True, require_confirmation=True,
                        confirmation_ttl_seconds=0.05)
    with pytest.raises(ConfirmationRequired) as exc:
        svc.execute(Command.GO_LONG)
    time.sleep(0.1)
    with pytest.raises(ConfirmationRequired):
        svc.execute(Command.GO_LONG, confirm_token=exc.value.token, force=True)


def test_go_flat_never_needs_confirmation(tmp_path):
    """The de-risking command must stay one tap away."""
    svc = build_service(tmp_path, mode=TradingMode.LIVE,
                        live_trading_enabled=True, require_confirmation=True)
    result = svc.execute(Command.GO_FLAT)
    assert result.status == "OK"


# --- one broker up, one broker down -----------------------------------------


def test_one_broker_failing_reports_partial_not_success(tmp_path):
    svc = build_service(tmp_path)
    svc.brokers["ibkr"].sim = SimulationConfig(reject_next="ibkr is down")

    result = svc.execute(Command.GO_LONG)

    assert result.status == "PARTIAL"
    assert result.mismatch is True
    assert not result.all_ok and result.any_ok
    by_broker = {r.broker: r for r in result.results}
    assert by_broker["schwab"].ok
    assert not by_broker["ibkr"].ok


def test_a_mismatch_blocks_the_next_command_until_acknowledged(tmp_path):
    svc = build_service(tmp_path, duplicate_command_window_seconds=0.0)
    svc.brokers["ibkr"].sim = SimulationConfig(reject_next="ibkr is down")
    svc.execute(Command.GO_LONG)

    assert svc.state.data.mismatch is not None
    with pytest.raises(UnreconciledState, match="disagreement"):
        svc.execute(Command.GO_SHORT)

    svc.acknowledge_mismatch("checked both accounts")
    assert svc.state.data.mismatch is None
    svc.execute(Command.GO_SHORT)     # unblocked


def test_go_flat_is_allowed_while_a_mismatch_is_outstanding(tmp_path):
    """You must always be able to de-risk, even mid-incident."""
    svc = build_service(tmp_path, duplicate_command_window_seconds=0.0)
    svc.brokers["ibkr"].sim = SimulationConfig(reject_next="ibkr is down")
    svc.execute(Command.GO_LONG)
    assert svc.state.data.mismatch is not None

    result = svc.execute(Command.GO_FLAT)
    assert result.status in ("OK", "PARTIAL")


# --- other limits -----------------------------------------------------------


def test_daily_command_limit(tmp_path):
    svc = build_service(tmp_path, max_daily_commands=2,
                        duplicate_command_window_seconds=0.0)
    svc.execute(Command.GO_LONG, force=True)
    svc.execute(Command.GO_FLAT, force=True)
    with pytest.raises(SafetyLimitExceeded, match="MAX_DAILY_COMMANDS"):
        svc.execute(Command.GO_LONG, force=True)


def test_live_mode_requires_the_second_switch(tmp_path):
    from trading.config import Settings, validate_settings
    from trading.errors import ConfigError

    s = Settings(mode=TradingMode.LIVE, live_trading_enabled=False,
                 api_token="x")
    with pytest.raises(ConfigError, match="LIVE_TRADING_ENABLED"):
        validate_settings(s)


def test_live_mode_requires_an_explicit_api_token(tmp_path):
    from trading.config import Settings, validate_settings
    from trading.errors import ConfigError

    s = Settings(mode=TradingMode.LIVE, live_trading_enabled=True, api_token="")
    with pytest.raises(ConfigError, match="API_TOKEN"):
        validate_settings(s)


# --- dry run ----------------------------------------------------------------


def test_shadow_broker_never_touches_the_real_order_endpoints(tmp_path):
    """Dry-run safety is structural, not a flag check inside the adapter."""
    settings = make_settings(tmp_path, mode=TradingMode.DRY_RUN,
                             live_trading_enabled=False)
    real = SimulatedBroker(settings, name="real", cash=10_000.0)
    real.connect()
    shadow = ShadowBroker(real, settings)
    shadow.connect()

    from trading.models import Side

    order = shadow.submit_limit_order("QQQ", Side.BUY, 5, 500.55)
    shadow.get_order_status(order.order_id)

    assert order.simulated is True
    assert real.submitted_orders == []      # nothing reached the real adapter
    assert real.get_available_cash().cash_available == 10_000.0


def test_dry_run_factory_never_returns_a_live_order_path(tmp_path, monkeypatch):
    from trading.brokers.factory import build_broker

    settings = make_settings(tmp_path, mode=TradingMode.DRY_RUN,
                             live_trading_enabled=False,
                             schwab_app_key=None, schwab_app_secret=None)
    broker = build_broker("schwab", settings)
    assert isinstance(broker, (SimulatedBroker, ShadowBroker))


def test_paper_mode_refuses_to_send_real_schwab_orders(tmp_path):
    from trading.brokers.factory import build_broker

    settings = make_settings(tmp_path, mode=TradingMode.PAPER,
                             live_trading_enabled=False,
                             schwab_app_key="k", schwab_app_secret="s")
    broker = build_broker("schwab", settings)
    # Schwab has no paper environment, so paper mode must shadow it.
    assert isinstance(broker, ShadowBroker)


def test_paper_mode_rejects_a_live_ibkr_account(tmp_path):
    from trading.brokers.factory import verify_environment
    from trading.errors import ConfigError
    from trading.models import Account

    settings = make_settings(tmp_path, mode=TradingMode.PAPER,
                             live_trading_enabled=False)
    broker = make_broker(settings, "ibkr")
    broker._account = Account("U1234567", "U1234567", is_paper=False)

    with pytest.raises(ConfigError, match="LIVE account"):
        verify_environment(broker, settings)


# --- audit / secrets --------------------------------------------------------


def test_audit_log_records_the_whole_command(tmp_path):
    svc = build_service(tmp_path)
    result = svc.execute(Command.GO_LONG)

    events = [e["event"] for e in svc.audit.tail(limit=500)]
    for expected in (
        "command.received", "command.started", "broker.reconcile",
        "market.quote", "order.sizing", "order.validated",
        "order.submitted", "order.filled", "command.finished",
    ):
        assert expected in events, f"{expected} missing from {sorted(set(events))}"

    # And it is on disk, one JSON object per line.
    lines = svc.audit.path.read_text().strip().splitlines()
    assert len(lines) >= len(events)
    assert json.loads(lines[0])["event"] == "command.received"
    assert result.command_id


def test_secrets_are_redacted_from_audit_payloads(tmp_path):
    svc = build_service(tmp_path)
    svc.audit.record(
        "test.event",
        access_token="super-secret-value",
        app_secret="another-secret",
        refresh_token="third-secret",
        account_number="12345678",
        harmless="visible",
    )
    text = svc.audit.path.read_text()
    assert "super-secret-value" not in text
    assert "another-secret" not in text
    assert "third-secret" not in text
    assert "visible" in text


def test_settings_public_dict_leaks_nothing(tmp_path):
    settings = make_settings(tmp_path, schwab_app_key="APPKEY123",
                             schwab_app_secret="APPSECRET456",
                             schwab_account_number="98765432",
                             api_token="tok-en", ibkr_account_id="DU1234567")
    public = json.dumps(settings.to_public_dict())
    for secret in ("APPKEY123", "APPSECRET456", "98765432", "tok-en", "DU1234567"):
        assert secret not in public


def test_log_scrubber_removes_bearer_tokens():
    from trading.audit import _scrub

    assert "abc123" not in _scrub("Authorization: Bearer abc123")
    assert "xyz789" not in _scrub('{"refresh_token": "xyz789"}')
