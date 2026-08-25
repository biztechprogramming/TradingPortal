"""The broker-neutral interface every adapter implements.

The strategy layer talks only to this interface. Adding a third broker means
writing one subclass and nothing else.
"""

from __future__ import annotations

import abc
import time
from datetime import date
from typing import Any

from ..config import Settings
from ..models import (
    Account,
    Balances,
    Bar,
    Order,
    OrderState,
    Position,
    Quote,
    Side,
    TradingMode,
    Transaction,
)
from ..performance import NavPoint


class Broker(abc.ABC):
    """Common interface. All prices are USD; all quantities are shares."""

    #: Unique instance identifier. With several accounts at one broker this is
    #: qualified, e.g. ``"schwab:1234"``, so results and audit records can
    #: always be attributed to a single account.
    name: str = "broker"

    #: The family this instance belongs to, e.g. ``"schwab"``. Several
    #: instances share a type; each has its own ``name``.
    broker_type: str = "broker"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._account: Account | None = None
        self._connected = False
        self._last_error: str | None = None

    @property
    def read_only(self) -> bool:
        """True when this instance must never place an order."""
        return False

    # --- lifecycle --------------------------------------------------------

    @abc.abstractmethod
    def connect(self) -> None:
        """Establish/verify a session. Must be idempotent and cheap to retry."""

    def close(self) -> None:
        """Release network resources. Safe to call when never connected."""

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def last_error(self) -> str | None:
        return self._last_error

    # --- required interface ----------------------------------------------

    @abc.abstractmethod
    def get_accounts(self) -> list[Account]:
        ...

    @abc.abstractmethod
    def get_positions(self) -> list[Position]:
        ...

    @abc.abstractmethod
    def get_available_cash(self) -> Balances:
        ...

    @abc.abstractmethod
    def get_quote(self, symbol: str) -> Quote:
        ...

    @abc.abstractmethod
    def get_open_orders(self) -> list[Order]:
        ...

    @abc.abstractmethod
    def cancel_order(self, order_id: str) -> None:
        ...

    @abc.abstractmethod
    def submit_limit_order(
        self, symbol: str, side: Side, quantity: int, limit_price: float
    ) -> Order:
        ...

    @abc.abstractmethod
    def replace_order(self, order_id: str, limit_price: float) -> Order:
        """Cancel/replace at a new price, keeping the *unfilled* quantity.

        Implementations must return an :class:`Order` whose ``order_id`` is
        the one to poll from now on; some brokers issue a new id on replace.
        """

    @abc.abstractmethod
    def get_order_status(self, order_id: str) -> Order:
        ...

    # --- optional history capabilities ------------------------------------
    #
    # Not every broker can answer these, and the performance layer degrades
    # rather than failing when one cannot. Returning ``None``/``[]`` means
    # "unsupported", which is different from "supported and empty".

    def get_transactions(self, start: date, end: date) -> list[Transaction] | None:
        """Cash and trade activity, for reconstructing NAV history."""
        return None

    def get_price_history(
        self, symbol: str, start: date, end: date
    ) -> dict[str, float] | None:
        """``{"YYYY-MM-DD": close}`` for valuing reconstructed positions."""
        return None

    def get_broker_performance(self, months: int = 6) -> list[NavPoint] | None:
        """The broker's own NAV/return series, when it publishes one."""
        return None

    def get_price_bars(
        self,
        symbol: str,
        start: float,
        end: float,
        *,
        bar_size: str = "1h",
        outside_rth: bool = True,
    ) -> list[Bar] | None:
        """OHLCV candles in ``[start, end]``, as epoch seconds.

        ``None`` means this adapter cannot supply bars at all, which the
        candle cache treats as "ask a different broker" rather than as an
        error. An empty list means "supported, but nothing in that range".
        """
        return None

    # --- shared helpers ---------------------------------------------------

    @property
    def account(self) -> Account | None:
        return self._account

    @property
    def is_paper_account(self) -> bool:
        return bool(self._account and self._account.is_paper)

    def get_position(self, symbol: str) -> Position | None:
        for p in self.get_positions():
            if p.symbol.upper() == symbol.upper():
                return p
        return None

    def get_open_orders_for(self, symbols: set[str]) -> list[Order]:
        upper = {s.upper() for s in symbols}
        return [
            o
            for o in self.get_open_orders()
            if o.symbol.upper() in upper and o.state.is_open
        ]

    def wait_until_cancelled(self, order_id: str, timeout: float) -> Order:
        """Poll until a cancel is acknowledged; brokers cancel asynchronously.

        A cancel that never confirms is a hazard: the order may still fill.
        Callers treat a timeout here as an error requiring attention.
        """
        deadline = time.monotonic() + timeout
        order = self.get_order_status(order_id)
        while time.monotonic() < deadline:
            if order.state.is_terminal:
                return order
            time.sleep(min(self.settings.order_poll_interval_seconds, 1.0))
            order = self.get_order_status(order_id)
        return order

    def describe(self) -> dict[str, Any]:
        return {
            "broker": self.name,
            "connected": self._connected,
            "account": self._account.to_dict() if self._account else None,
            "paper": self.is_paper_account,
            "last_error": self._last_error,
        }


class ReadOnlyBrokerMixin:
    """Blocks every mutating call. Mixed in for dry-run wrapping."""

    def _blocked(self, what: str) -> None:
        raise RuntimeError(f"{what} blocked: broker is in read-only mode")


def normalize_state(raw: str, mapping: dict[str, OrderState]) -> OrderState:
    """Map a broker status string onto :class:`OrderState`.

    Unknown statuses map to ``UNKNOWN`` rather than guessing, because an
    unrecognised status must never be mistaken for a terminal one.
    """
    return mapping.get((raw or "").strip().upper(), OrderState.UNKNOWN)


def mask_account(account_id: str) -> str:
    """``...1234`` — enough to identify, not enough to be worth stealing."""
    if not account_id:
        return ""
    tail = account_id[-4:]
    return f"...{tail}"


def mode_of(settings: Settings) -> TradingMode:
    return settings.mode
