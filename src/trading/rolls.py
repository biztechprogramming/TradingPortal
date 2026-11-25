"""Keeping a continuous future's history across a contract roll.

IBKR back-adjusts a continuous future to its current front month, and only
serves about four years of it. At each roll every bar it serves moves onto
the new contract's price scale. Bars older than that window cannot be
fetched again, so they are re-based here instead of being thrown away: the
gap between the old and new scale is measured where the two overlap and
applied to every old bar the fresh download does not replace.
"""

from __future__ import annotations

from statistics import median
from typing import Sequence

from .models import Bar


class RollMismatch(RuntimeError):
    """The old and new series do not overlap enough to line them up."""


#: Fewest overlapping bars that make the measured roll gap trustworthy.
MIN_OVERLAP = 5


def roll_adjustment(old: Sequence[Bar], fresh: Sequence[Bar]) -> tuple[str, float]:
    """How the old scale maps onto the new: ``("add", gap)`` or ``("mul", ratio)``.

    Back-adjustment is a constant gap or a constant ratio depending on the
    vendor's method; whichever is the more constant across the overlap is
    the one in use.
    """
    old_close = {b.ts: b.close for b in old}
    pairs = [(old_close[b.ts], b.close) for b in fresh if b.ts in old_close]
    pairs = [(o, n) for o, n in pairs if o > 0 and n > 0]
    if len(pairs) < MIN_OVERLAP:
        raise RollMismatch(f"only {len(pairs)} bars overlap the new series")
    gaps = sorted(n - o for o, n in pairs)
    ratios = sorted(n / o for o, n in pairs)
    gap, ratio = median(gaps), median(ratios)
    level = median(n for _, n in pairs)
    # Spread of the middle half, in price terms, so the two compare.
    q = len(pairs) // 4
    gap_spread = (gaps[-1 - q] - gaps[q]) / level
    ratio_spread = ratios[-1 - q] - ratios[q]
    return ("add", gap) if gap_spread <= ratio_spread else ("mul", ratio)


def rebase(bars: Sequence[Bar], how: tuple[str, float]) -> list[Bar]:
    """``bars`` moved onto the new contract's scale."""
    kind, k = how
    move = (lambda v: v + k) if kind == "add" else (lambda v: v * k)
    return [Bar(symbol=b.symbol, ts=b.ts, open=move(b.open), high=move(b.high),
                low=move(b.low), close=move(b.close), volume=b.volume)
            for b in bars]
