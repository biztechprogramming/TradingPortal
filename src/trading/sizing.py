"""Position sizing.

Goal: deploy a deliberate slice of available cash -- ``TARGET_CASH_MAX_PCT``
of it, in whole lots of ``SHARE_LOT_SIZE`` shares where that works. Hard
rules: the *estimated worst case cost* of an order must never exceed the funds
we are allowed to spend, and at least one share's worth of cash (at the limit
price) is always left in the account.

The two knobs pull against each other on a high-priced symbol. One lot of 10
QQQ is ~$6,000, so lot rounding can only land inside a 95-98% band when the
account holds enough cash that a lot is smaller than the band is wide (roughly
$200k for QQQ, $10k for PSQ). Below that, rounding to a lot would leave a
smaller account badly under-invested, so sizing drops to single shares
instead and ``caps_applied`` records why.

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
    lot_size: int = 1
    #: Estimated cost as a percentage of spendable cash, for the audit log.
    deployed_pct: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def lot_floor(quantity: float, settings: Settings, sized_quantity: int | None = None) -> int:
    """Round a share count down to a whole lot.

    Every path that decides an order size has to go through this, not just
    :func:`calculate_quantity`: a reprice that shrinks an order to fit a
    higher limit is deciding a size too, and a 19-share replacement would
    quietly break the divisibility the account is supposed to keep.

    ``sized_quantity`` is the order's original size. When that was not a whole
    lot (a smaller account sized in single shares), single shares are kept.
    """
    lot = max(1, settings.share_lot_size)
    if sized_quantity is not None and sized_quantity % lot:
        lot = 1
    return max(0, int(quantity) // lot * lot)


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
    lot = max(1, settings.share_lot_size)
    available = spendable_cash(balances, settings)

    # Two ceilings: the hard one that keeps the buffer intact, and the target
    # one that deliberately leaves headroom. The tighter binds.
    # The hard ceiling also keeps one share's worth of cash unspent.
    one_share = limit_price + settings.estimated_fee_per_share
    hard_ceiling = (
        available - settings.cash_buffer_dollars - settings.estimated_fee_fixed - one_share
    )
    target_ceiling = (
        available * settings.target_cash_max_pct / 100.0 - settings.estimated_fee_fixed
    )
    usable = min(hard_ceiling, target_ceiling)
    if target_ceiling < hard_ceiling:
        caps.append(f"TARGET_CASH_MAX_PCT={settings.target_cash_max_pct:g}")

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
            lot_size=lot,
        )

    per_share_cost = limit_price + settings.estimated_fee_per_share
    raw_quantity = int(math.floor(usable / per_share_cost))

    if settings.max_shares_per_order > 0 and raw_quantity > settings.max_shares_per_order:
        raw_quantity = settings.max_shares_per_order
        caps.append(f"MAX_SHARES_PER_ORDER={settings.max_shares_per_order}")

    raw_quantity = max(0, raw_quantity)
    quantity = (raw_quantity // lot) * lot
    if lot > 1 and quantity != raw_quantity:
        lot_pct = quantity * per_share_cost / available * 100.0 if available > 0 else 0.0
        if lot_pct >= settings.target_cash_min_pct:
            caps.append(f"SHARE_LOT_SIZE={lot} (from {raw_quantity} shares)")
        else:
            # Whole lots would fall short of the band; single shares get
            # the account invested instead.
            caps.append(
                f"SHARE_LOT_SIZE={lot} skipped: {quantity} shares is only "
                f"{lot_pct:.1f}% deployed, sizing in single shares"
            )
            quantity = raw_quantity
            lot = 1

    fees = settings.estimated_fee_fixed + quantity * settings.estimated_fee_per_share
    estimated_cost = quantity * limit_price + fees

    # Defensive: floating-point division can round the last share up. Step by
    # the lot in use so a lotted order stays divisible.
    while quantity > 0 and estimated_cost > available - settings.cash_buffer_dollars - one_share:
        quantity -= lot
        quantity = max(0, quantity)
        fees = settings.estimated_fee_fixed + quantity * settings.estimated_fee_per_share
        estimated_cost = quantity * limit_price + fees
        if "float-safety decrement" not in caps:
            caps.append("float-safety decrement")

    deployed_pct = (estimated_cost / available * 100.0) if available > 0 else 0.0
    if quantity > 0 and deployed_pct < settings.target_cash_min_pct:
        # Not an error: in a small account the one-share reserve and buffer
        # are a large slice of cash, so this is the closest reachable size.
        caps.append(
            f"below TARGET_CASH_MIN_PCT={settings.target_cash_min_pct:g} "
            f"({deployed_pct:.1f}% deployed) against ${available:,.2f} available"
        )

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
        lot_size=lot,
        deployed_pct=round(deployed_pct, 2),
    )
