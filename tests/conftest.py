"""Shared fixtures. Every test runs against mocked brokers — never a network."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from trading.audit import AuditLog
from trading.config import Settings, validate_settings
from trading.brokers.simulated import SimulatedBroker, SimulationConfig
from trading.models import TradingMode
from trading.orchestrator import TradingService


def make_settings(tmp_path: Path, **overrides) -> Settings:
    """A fast, permissive-but-valid configuration for tests."""
    defaults = dict(
        mode=TradingMode.LIVE,
        live_trading_enabled=True,
        require_confirmation=False,
        api_token="test-token",
        brokers_enabled=("schwab", "ibkr"),
        long_symbol="QQQ",
        short_symbol="PSQ",
        cash_buffer_dollars=25.0,
        # Keep the clock out of the way: these tests assert behaviour, not timing.
        order_poll_interval_seconds=0.001,
        reprice_interval_seconds=0.002,
        order_fill_timeout_seconds=2.0,
        cancel_confirm_timeout_seconds=0.5,
        settlement_wait_seconds=0.0,
        duplicate_command_window_seconds=0.0,
        market_hours_guard=False,
        max_daily_commands=0,
        log_dir=tmp_path / "log",
        state_dir=tmp_path / "state",
    )
    defaults.update(overrides)
    settings = Settings(**defaults)
    validate_settings(settings)
    return settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def audit(tmp_path: Path) -> AuditLog:
    return AuditLog(tmp_path / "log")


def make_broker(
    settings: Settings,
    name: str = "sim",
    cash: float = 100_000.0,
    positions: dict[str, float] | None = None,
    sim: SimulationConfig | None = None,
    **quote_overrides,
) -> SimulatedBroker:
    broker = SimulatedBroker(
        settings, name=name, cash=cash, positions=positions, sim=sim,
        account_id=f"{name.upper()}1234",
    )
    broker.connect()
    # Deterministic, tight quotes unless a test says otherwise.
    broker.set_quote("QQQ", quote_overrides.get("qqq_bid", 500.00),
                     quote_overrides.get("qqq_ask", 500.04))
    broker.set_quote("PSQ", quote_overrides.get("psq_bid", 32.10),
                     quote_overrides.get("psq_ask", 32.12))
    return broker


class RisingMarketBroker(SimulatedBroker):
    """A simulator whose market jumps once, after the order is already live.

    Repricing only ever matters when the market moves *after* submission —
    a marketable limit computed from the current quote is otherwise filled
    on the spot. This broker reproduces that timing exactly.
    """

    def __init__(self, *args, jump_after_polls: int = 1,
                 jump_to: tuple[str, float, float] | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._status_polls = 0
        self._jump_after = jump_after_polls
        self._jump_to = jump_to

    def get_order_status(self, order_id: str):
        order = super().get_order_status(order_id)
        self._status_polls += 1
        if self._jump_to and self._status_polls == self._jump_after:
            symbol, bid, ask = self._jump_to
            self.set_quote(symbol, bid, ask)
        return order


def make_rising_broker(
    settings: Settings,
    jump_to: tuple[str, float, float],
    *,
    cash: float = 100_000.0,
    jump_after_polls: int = 1,
    polls_before_fill: int = 2,
    name: str = "sim",
) -> RisingMarketBroker:
    broker = RisingMarketBroker(
        settings,
        name=name,
        cash=cash,
        sim=SimulationConfig(polls_before_fill=polls_before_fill),
        account_id=f"{name.upper()}1234",
        jump_after_polls=jump_after_polls,
        jump_to=jump_to,
    )
    broker.connect()
    broker.set_quote("QQQ", 500.00, 500.04)
    broker.set_quote("PSQ", 32.10, 32.12)
    return broker


@pytest.fixture
def broker(settings: Settings) -> SimulatedBroker:
    return make_broker(settings)


@pytest.fixture
def service(settings: Settings) -> TradingService:
    """A two-broker service backed entirely by simulators."""
    brokers = {
        "schwab": make_broker(settings, "schwab", cash=50_000.0),
        "ibkr": make_broker(settings, "ibkr", cash=25_000.0),
    }
    svc = TradingService(settings, brokers=brokers)
    return svc


@pytest.fixture
def quote_now() -> float:
    return time.time()
