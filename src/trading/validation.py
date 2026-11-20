"""Pre-submit order validation and market-hours guard.

:func:`validate_order` runs immediately before *every* order reaches a broker
adapter, including in dry-run. If it raises, nothing is sent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as dtime, timezone
from typing import Any

from .config import Settings
from .errors import MarketClosed, OrderValidationError, SafetyLimitExceeded
from .models import Balances, Side

try:  # Python 3.9+ standard library
    from zoneinfo import ZoneInfo

    _EASTERN: Any = ZoneInfo("America/New_York")
    TZ_AVAILABLE = True
except Exception:  # pragma: no cover - tzdata missing on minimal images
    # Falling back to UTC would silently shift the session by 4-5 hours,
    # permitting pre-market trading and blocking the afternoon. So we record
    # the failure and fail closed instead of guessing. See DOCKER.md.
    _EASTERN = timezone.utc
    TZ_AVAILABLE = False

REGULAR_OPEN = dtime(9, 30)
REGULAR_CLOSE = dtime(16, 0)
#: Pre-market opens and after-hours closes, Eastern. Orders placed in these
#: windows are flagged for extended hours; see :func:`wants_outside_rth`.
EXTENDED_OPEN = dtime(4, 0)
EXTENDED_CLOSE = dtime(20, 0)

#: Full-day US equity market closures. Half-days (1pm close) are not encoded;
#: see docs/SAFETY.md for why the guard is deliberately approximate.
MARKET_HOLIDAYS_2026 = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
}


@dataclass
class MarketStatus:
    is_open: bool
    reason: str
    now_eastern: str
    #: "regular", "extended" (pre-market or after hours) or "closed".
    session: str = "closed"


def market_status(now: datetime | None = None) -> MarketStatus:
    now = now or datetime.now(timezone.utc)
    eastern = now.astimezone(_EASTERN)
    stamp = eastern.strftime("%Y-%m-%d %H:%M:%S %Z")

    if not TZ_AVAILABLE:
        # We cannot convert to Eastern, so we cannot know whether the market
        # is open. Report closed rather than compute the session in UTC.
        return MarketStatus(
            False,
            "timezone database unavailable — cannot determine US market hours "
            "(install tzdata)",
            stamp,
        )

    if eastern.weekday() >= 5:
        return MarketStatus(False, "weekend", stamp)
    if eastern.strftime("%Y-%m-%d") in MARKET_HOLIDAYS_2026:
        return MarketStatus(False, "market holiday", stamp)
    now_t = eastern.time()
    if REGULAR_OPEN <= now_t < REGULAR_CLOSE:
        return MarketStatus(True, "regular session", stamp, "regular")
    if EXTENDED_OPEN <= now_t < REGULAR_OPEN:
        return MarketStatus(False, "pre-market extended hours", stamp, "extended")
    if REGULAR_CLOSE <= now_t < EXTENDED_CLOSE:
        return MarketStatus(False, "after-hours extended session", stamp, "extended")
    return MarketStatus(False, "outside 04:00-20:00 ET trading hours", stamp)


def wants_outside_rth(settings: Settings, now: datetime | None = None) -> bool:
    """Whether an order placed now must be flagged for extended hours.

    Brokers hold a regular-session-only order until the open, so one placed
    pre-market or after hours has to say it may fill outside regular hours.
    """
    return settings.allow_outside_rth or market_status(now).session == "extended"


def check_market_hours(settings: Settings, now: datetime | None = None) -> None:
    """Refuse to trade when no session is running unless explicitly permitted.

    Extended hours are allowed; those orders go out flagged outside-RTH.
    Liquidity there is thin and spreads wide, so the orders are limits.
    """
    if not settings.market_hours_guard or settings.allow_outside_rth:
        return
    status = market_status(now)
    if status.session == "closed":
        raise MarketClosed(
            f"Market is closed ({status.reason}, now {status.now_eastern}). "
            "Set ALLOW_OUTSIDE_RTH=true to override, or MARKET_HOURS_GUARD=false "
            "to disable this check entirely."
        )


def validate_order(
    *,
    symbol: str,
    side: Side,
    quantity: float,
    limit_price: float,
    account_id: str | None,
    settings: Settings,
    balances: Balances | None = None,
    position_quantity: float | None = None,
) -> dict[str, Any]:
    """Validate ticker, side, quantity, account and estimated value.

    Returns the validation record that gets written to the audit log.
    """
    allowed = {settings.long_symbol.upper(), settings.short_symbol.upper()}
    sym = (symbol or "").upper().strip()

    if sym not in allowed:
        raise OrderValidationError(
            f"symbol {symbol!r} is not tradable by this system; only "
            f"{sorted(allowed)} are permitted"
        )
    if not isinstance(side, Side):
        raise OrderValidationError(f"side must be a Side enum, got {side!r}")
    if quantity is None or quantity <= 0:
        raise OrderValidationError(f"quantity must be positive, got {quantity!r}")
    if quantity != int(quantity):
        raise OrderValidationError(
            f"quantity must be a whole number of shares, got {quantity!r}"
        )
    if limit_price is None or limit_price <= 0:
        raise OrderValidationError(
            f"limit price must be positive, got {limit_price!r}"
        )
    if not account_id:
        raise OrderValidationError("no account selected; refusing to submit")

    quantity = int(quantity)
    if quantity < settings.min_order_shares:
        raise OrderValidationError(
            f"quantity {quantity} is below MIN_ORDER_SHARES="
            f"{settings.min_order_shares}"
        )
    if settings.max_shares_per_order > 0 and quantity > settings.max_shares_per_order:
        raise SafetyLimitExceeded(
            f"quantity {quantity} exceeds MAX_SHARES_PER_ORDER="
            f"{settings.max_shares_per_order}"
        )

    estimated_value = quantity * limit_price
    if (
        settings.max_order_notional_dollars > 0
        and estimated_value > settings.max_order_notional_dollars
    ):
        raise SafetyLimitExceeded(
            f"estimated order value ${estimated_value:,.2f} exceeds "
            f"MAX_ORDER_NOTIONAL_DOLLARS=${settings.max_order_notional_dollars:,.2f}"
        )

    if side is Side.BUY and balances is not None:
        from .sizing import spendable_cash

        available = spendable_cash(balances, settings)
        if estimated_value > available - settings.cash_buffer_dollars + 1e-6:
            raise OrderValidationError(
                f"estimated cost ${estimated_value:,.2f} exceeds available "
                f"${available:,.2f} minus buffer "
                f"${settings.cash_buffer_dollars:,.2f}"
            )

    if side is Side.SELL and position_quantity is not None:
        if quantity > position_quantity + 1e-9:
            raise OrderValidationError(
                f"attempting to sell {quantity} shares of {sym} but only "
                f"{position_quantity:g} are held. This system never sells "
                "short."
            )

    return {
        "symbol": sym,
        "side": side.value,
        "quantity": quantity,
        "limit_price": limit_price,
        "estimated_value": round(estimated_value, 2),
        "account_id_present": True,
        "checks": [
            "symbol_allowlist",
            "side_enum",
            "whole_positive_quantity",
            "positive_limit_price",
            "account_selected",
            "min_shares",
            "max_shares",
            "max_notional",
            "funds_sufficient" if side is Side.BUY else "position_sufficient",
        ],
    }
