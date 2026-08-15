"""Position sizing.

Goal: deploy essentially all available cash. Hard rule: the *estimated worst
case cost* of an order must never exceed the funds we are allowed to spend.

Because we always send limit orders, the worst-case cost of a buy is exactly
``quantity * limit_price`` plus estimated fees — never the ask, never the
last trade. Sizing therefore divides by the limit price, which makes an
insufficient-funds rejection structurally impossible barring a broker-side
balance change between the quote and the submit.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from typing import Any

from .config import Settings
from .errors import InsufficientFunds
from .models import Balances


@dataclass
class SizingDecision:
    quantity: int
    limit_price: float
    usable_cash: float
    estimated_cost: float
    estimated_fees: float
    cash_available: float
    buying_power: float
    buffer_applied: float
    margin_allowed: bool
    caps_applied: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def spendable_cash(balances: Balances, settings: Settings) -> float:
    """How much money this command is permitted to deploy.

    Cash-only is the default: we take the smaller of reported cash and
    reported buying power so that a margin-enabled account cannot silently
    lever up just because the broker advertises extra buying power.
    """
    base = balances.cash_available
    if settings.allow_margin:
        base = max(balances.cash_available, balances.buying_power)
    else:
        # buying_power can legitimately be reported as 0 by some endpoints;
        # only let it constrain us when it looks like a real number.
        if balances.buying_power > 0:
            base = min(base, balances.buying_power)
    return max(0.0, base)


def calculate_quantity(
    balances: Balances,
    limit_price: float,
    settings: Settings,
) -> SizingDecision:
    """Maximum whole shares purchasable without exceeding available funds."""
    if limit_price <= 0:
        raise InsufficientFunds("cannot size an order against a non-positive price")

    caps: list[str] = []
    available = spendable_cash(balances, settings)
    usable = available - settings.cash_buffer_dollars - settings.estimated_fee_fixed

    if settings.max_order_notional_dollars > 0 and usable > settings.max_order_notional_dollars:
        usable = settings.max_order_notional_dollars
        caps.append(
            f"MAX_ORDER_NOTIONAL_DOLLARS={settings.max_order_notional_dollars:g}"
        )

    if usable <= 0:
        return SizingDecision(
            quantity=0,
            limit_price=limit_price,
            usable_cash=usable,
            estimated_cost=0.0,
            estimated_fees=0.0,
            cash_available=balances.cash_available,
            buying_power=balances.buying_power,
            buffer_applied=settings.cash_buffer_dollars,
            margin_allowed=settings.allow_margin,
            caps_applied=caps + ["no usable cash after buffer"],
        )

    per_share_cost = limit_price + settings.estimated_fee_per_share
    quantity = int(math.floor(usable / per_share_cost))

    if settings.max_shares_per_order > 0 and quantity > settings.max_shares_per_order:
        quantity = settings.max_shares_per_order
        caps.append(f"MAX_SHARES_PER_ORDER={settings.max_shares_per_order}")

    quantity = max(0, quantity)
    fees = settings.estimated_fee_fixed + quantity * settings.estimated_fee_per_share
    estimated_cost = quantity * limit_price + fees

    # Defensive: floating-point division can round the last share up.
    while quantity > 0 and estimated_cost > available - settings.cash_buffer_dollars:
        quantity -= 1
        fees = settings.estimated_fee_fixed + quantity * settings.estimated_fee_per_share
        estimated_cost = quantity * limit_price + fees
        if "float-safety decrement" not in caps:
            caps.append("float-safety decrement")

    return SizingDecision(
        quantity=quantity,
        limit_price=limit_price,
        usable_cash=usable,
        estimated_cost=round(estimated_cost, 2),
        estimated_fees=round(fees, 2),
        cash_available=balances.cash_available,
        buying_power=balances.buying_power,
        buffer_applied=settings.cash_buffer_dollars,
        margin_allowed=settings.allow_margin,
        caps_applied=caps,
    )
