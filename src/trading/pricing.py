"""Marketable-limit price algorithm and quote validation.

The whole point of this module: get filled fast, but never accept a price
worse than a hard, pre-declared boundary. See docs/LIMIT_ALGORITHM.md.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, asdict
from typing import Any

from .config import Settings
from .errors import (
    HaltedSecurityError,
    InvalidQuoteError,
    MaxSlippageExceeded,
    StaleQuoteError,
    WideSpreadError,
)
from .models import Quote, Side


def round_to_tick(price: float, tick: float, mode: str = "nearest") -> float:
    """Round to a tradable increment.

    ``up``/``down`` are used to keep an order marketable after rounding: a
    buy limit rounded *down* could land inside the spread and sit unfilled.
    """
    if tick <= 0:
        return round(price, 2)
    units = price / tick
    if mode == "up":
        units = math.ceil(units - 1e-9)
    elif mode == "down":
        units = math.floor(units + 1e-9)
    else:
        units = round(units)
    # Two decimals is the SEC sub-penny rule for equities >= $1.00.
    return round(units * tick, 4)


def validate_quote(quote: Quote, settings: Settings, now: float | None = None) -> None:
    """Reject any quote we should not be pricing an order against.

    Raised errors are all subclasses of :class:`QuoteError`, so a caller can
    abort the whole command with one ``except``.
    """
    now = now if now is not None else time.time()

    if quote.halted:
        raise HaltedSecurityError(f"{quote.symbol} is halted; refusing to trade")

    for name, value in (("bid", quote.bid), ("ask", quote.ask)):
        if value is None or not math.isfinite(value) or value <= 0:
            raise InvalidQuoteError(
                f"{quote.symbol} {name} is {value!r}; refusing to trade on an "
                "incomplete quote"
            )

    if quote.ask < quote.bid:
        raise InvalidQuoteError(
            f"{quote.symbol} quote is crossed (bid {quote.bid} > ask {quote.ask})"
        )

    age = quote.age_seconds(now)
    if age > settings.max_quote_age_seconds:
        raise StaleQuoteError(
            f"{quote.symbol} quote is {age:.1f}s old, limit is "
            f"{settings.max_quote_age_seconds:.1f}s"
        )
    if age < -30:  # clock skew large enough to make ageing meaningless
        raise StaleQuoteError(
            f"{quote.symbol} quote timestamp is {abs(age):.1f}s in the future; "
            "check system clock or broker time field"
        )

    max_spread = settings.max_spread_dollars_for(quote.symbol, quote.mid)
    if quote.spread > max_spread + 1e-9:
        ticks = quote.spread / settings.tick_size
        raise WideSpreadError(
            f"{quote.symbol} spread is {quote.spread:.4f} ({ticks:.0f} ticks, "
            f"{quote.spread_bps:.1f} bps; bid {quote.bid} / ask {quote.ask}), "
            f"limit is {max_spread:.4f} "
            f"({settings.max_spread_ticks_for(quote.symbol):g} ticks / "
            f"{settings.max_spread_bps:.1f} bps, whichever is tighter)"
        )


@dataclass
class PriceDecision:
    symbol: str
    side: Side
    limit_price: float
    reference_price: float     # the ask (buy) or bid (sell) we priced off
    anchor_price: float        # reference at the *start* of this leg
    slippage_bps: float        # offset actually applied this attempt, in bps
    guard_price: float         # the hard boundary we will never cross
    attempt: int
    spread_bps: float
    capped: bool               # True when the guard clamped the limit
    offset_ticks: float = 0.0     # cushion requested this attempt, in ticks
    offset_dollars: float = 0.0   # cushion actually applied, in dollars
    guard_ticks: float = 0.0      # guard distance from the anchor, in ticks
    spread_ticks: float = 0.0
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["side"] = self.side.value
        return d


def compute_limit_price(
    quote: Quote,
    side: Side,
    settings: Settings,
    *,
    anchor_price: float | None = None,
    attempt: int = 0,
) -> PriceDecision:
    """Compute a marketable limit price.

    The cushion beyond the touch is measured in **ticks and spreads**, not in
    basis points of the price. A single bps figure cannot serve a pair like
    QQQ and PSQ: 10 bps is three ticks on a $28 ETF and sixty ticks on a $600
    one, so the same setting is timid on one and reckless on the other. Ticks
    are the unit the book is actually quoted in, and the spread term supplies
    the extra room a thin book needs without loosening a normal one::

        offset = max(offset_ticks * tick, spread_multiple * spread)

    ``anchor_price`` is the reference price captured when the leg began. All
    guard arithmetic is relative to the anchor, not to the latest quote --
    otherwise each reprice would re-baseline against the price it just
    chased, and the "maximum slippage" would be unbounded over time.

    ``attempt`` escalates the offset by ``reprice_step_ticks`` per reprice, so
    the first try is cheap and later tries get progressively more aggressive
    up to the hard guard.
    """
    tick = settings.tick_size
    reference = quote.ask if side is Side.BUY else quote.bid
    anchor = anchor_price if anchor_price is not None else reference

    offset_ticks = (
        settings.offset_ticks_for(quote.symbol, side.value)
        + attempt * settings.reprice_step_ticks
    )
    offset = max(offset_ticks * tick, settings.spread_multiple * quote.spread)

    guard_distance = settings.guard_distance_for(quote.symbol, anchor)
    # The working cushion can never exceed the boundary it is bounded by;
    # clamping here keeps the reported offset honest rather than letting the
    # guard silently undo it below.
    offset = min(offset, guard_distance)

    if side is Side.BUY:
        guard = anchor + guard_distance
        # If the market itself has run past the guard, chasing is pointless
        # and unsafe: we would be buying above the worst price we declared
        # acceptable before the leg started.
        if reference > guard:
            raise MaxSlippageExceeded(
                f"{quote.symbol} ask {reference:.4f} exceeds the maximum "
                f"slippage guard {guard:.4f} (anchor {anchor:.4f}, "
                f"{guard_distance / tick:.0f} ticks). Aborting instead of "
                "chasing the price."
            )
        raw = reference + offset
        limit = round_to_tick(raw, tick, "up")
        capped = limit > guard
        if capped:
            limit = round_to_tick(guard, tick, "down")
        # Rounding down to the guard must never fall below the touch, or the
        # order is no longer marketable and will simply rest.
        if limit < reference:
            limit = round_to_tick(reference, tick, "up")
    else:
        guard = anchor - guard_distance
        if reference < guard:
            raise MaxSlippageExceeded(
                f"{quote.symbol} bid {reference:.4f} is below the maximum "
                f"slippage guard {guard:.4f} (anchor {anchor:.4f}, "
                f"{guard_distance / tick:.0f} ticks). Aborting instead of "
                "chasing the price down."
            )
        raw = reference - offset
        limit = round_to_tick(raw, tick, "down")
        capped = limit < guard
        if capped:
            limit = round_to_tick(guard, tick, "up")
        if limit > reference:
            limit = round_to_tick(reference, tick, "down")

    if limit <= 0:
        raise InvalidQuoteError(
            f"computed a non-positive limit price for {quote.symbol}"
        )

    applied = abs(limit - reference)
    return PriceDecision(
        symbol=quote.symbol,
        side=side,
        limit_price=limit,
        reference_price=reference,
        anchor_price=anchor,
        slippage_bps=round(applied / reference * 10_000.0, 3) if reference else 0.0,
        guard_price=round(guard, 4),
        attempt=attempt,
        spread_bps=round(quote.spread_bps, 2),
        capped=capped,
        offset_ticks=round(offset_ticks, 3),
        offset_dollars=round(applied, 4),
        guard_ticks=round(guard_distance / tick, 2),
        spread_ticks=round(quote.spread / tick, 2),
        reason=(
            "clamped to hard slippage guard" if capped else "marketable limit"
        ),
    )


def should_reprice(
    current_limit: float, new_decision: PriceDecision, tick: float
) -> bool:
    """Only cancel/replace when it would actually change the outcome.

    Replacing costs a round trip and loses queue priority, so we require the
    new price to be at least one tick more aggressive than the working one.
    """
    if new_decision.side is Side.BUY:
        return new_decision.limit_price > current_limit + tick / 2
    return new_decision.limit_price < current_limit - tick / 2
