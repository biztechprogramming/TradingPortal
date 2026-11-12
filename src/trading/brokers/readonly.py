"""Read-only broker wrapper.

Schwab's API has no read-only scope: an account the app can see is an account
the app can trade. This wrapper restores that separation in software. An
account that is authorised but not on ``TRADING_ACCOUNTS`` is wrapped here, so
it appears on the dashboard with full holdings while every order path raises.

Like ``ShadowBroker``, the safety is structural — the mutating methods do not
call through to the wrapped adapter at all.
"""

from __future__ import annotations

from ..config import Settings
from ..errors import TradingError
from ..models import Account, Balances, Order, Position, Quote, Side
from .base import Broker


class AccountNotTradable(TradingError):
    """An order was attempted against a display-only account."""


class ReadOnlyBroker(Broker):
    """Delegates every read; refuses every write."""

    def __init__(
        self, inner: Broker, settings: Settings, reason: str = "",
        warning: str | None = None,
    ) -> None:
        super().__init__(settings)
        self.inner = inner
        self.name = inner.name
        self.reason = reason or (
            "this account is not on the TRADING_ACCOUNTS allowlist"
        )
        #: Shown on the dashboard when set: read-only because something is
        #: wrong, not because the operator chose it.
        self.warning = warning

    # --- identity ---------------------------------------------------------

    @property
    def read_only(self) -> bool:
        return True

    @property
    def broker_type(self) -> str:
        return getattr(self.inner, "broker_type", self.inner.name)

    @property
    def connected(self) -> bool:
        return self.inner.connected

    @property
    def last_error(self) -> str | None:
        return self.inner.last_error

    @property
    def account(self) -> Account | None:
        return self.inner.account

    # --- reads pass straight through --------------------------------------

    def connect(self) -> None:
        self.inner.connect()

    def close(self) -> None:
        self.inner.close()

    def get_accounts(self) -> list[Account]:
        return self.inner.get_accounts()

    def get_positions(self) -> list[Position]:
        return self.inner.get_positions()

    def get_available_cash(self) -> Balances:
        return self.inner.get_available_cash()

    def get_quote(self, symbol: str) -> Quote:
        return self.inner.get_quote(symbol)

    def get_open_orders(self) -> list[Order]:
        return self.inner.get_open_orders()

    def get_order_status(self, order_id: str) -> Order:
        return self.inner.get_order_status(order_id)

    def get_transactions(self, start, end):
        return self.inner.get_transactions(start, end)

    def get_recent_fills(self, days: int = 30):
        return self.inner.get_recent_fills(days)

    def get_price_history(self, symbol, start, end):
        return self.inner.get_price_history(symbol, start, end)

    def get_broker_performance(self, months: int = 6):
        return self.inner.get_broker_performance(months)

    def get_price_bars(self, symbol, start, end, *, bar_size="1h",
                       outside_rth=True):
        return self.inner.get_price_bars(
            symbol, start, end, bar_size=bar_size, outside_rth=outside_rth
        )

    # --- writes are refused -----------------------------------------------

    def _refuse(self, what: str) -> None:
        account = self.account.display_id if self.account else "unknown"
        hint = "" if self.warning else " Add it to TRADING_ACCOUNTS to allow trading."
        raise AccountNotTradable(
            f"{what} refused for {self.name} (account {account}): "
            f"{self.reason}.{hint}"
        )

    def submit_limit_order(
        self, symbol: str, side: Side, quantity: int, limit_price: float
    ) -> Order:
        self._refuse(f"{side.value} {quantity} {symbol}")
        raise AssertionError("unreachable")

    def submit_market_order(
        self, symbol: str, side: Side, quantity: int
    ) -> Order:
        self._refuse(f"{side.value} {quantity} {symbol} at market")
        raise AssertionError("unreachable")

    def replace_order(self, order_id: str, limit_price: float) -> Order:
        self._refuse(f"replace of order {order_id}")
        raise AssertionError("unreachable")

    def cancel_order(self, order_id: str) -> None:
        self._refuse(f"cancel of order {order_id}")
