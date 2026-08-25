"""Session extremes and the swings between them.

Four figures, derived from the cached bars for the current trading day:

* the **high of day** and **when** it happened;
* the **low of day** and **when** it happened;
* the **high since the low** — how far price has run up off the bottom;
* the **low since the high** — how far it has pulled back off the top.

The last two are what make the first two actionable. A high of day set at
06:00 that has been fading ever since is a different market from one set five
minutes ago, and the pair of "since" figures is what separates them.

Resolution is the bar size of whatever series is passed in. The dashboard
feeds this a minute series so the times are minute-exact; an hourly bar could
only ever say *which hour* a high happened in, which is not a time. Whatever
the series, :attr:`SessionRange.resolution_seconds` states it rather than
leaving it implied, because a timestamp that looks exact but is not would be
worse than no timestamp at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Any, Sequence

from .models import Bar, Quote
from .validation import _EASTERN, TZ_AVAILABLE

#: US equities extended session, Eastern. Bars starting at or after the open
#: bound belong to the current trading day.
EXTENDED_OPEN = dtime(4, 0)
EXTENDED_CLOSE = dtime(20, 0)
REGULAR_OPEN = dtime(9, 30)
REGULAR_CLOSE = dtime(16, 0)


@dataclass
class SessionRange:
    """Where the session's extremes are, and what has happened since."""

    symbol: str
    day_high: float = 0.0
    day_high_at: int | None = None
    day_low: float = 0.0
    day_low_at: int | None = None
    #: Highest high at or after the low-of-day bar. Equals ``day_high`` when
    #: the high came after the low.
    high_since_low: float = 0.0
    high_since_low_at: int | None = None
    #: Lowest low at or after the high-of-day bar. Equals ``day_low`` when the
    #: low came after the high.
    low_since_high: float = 0.0
    low_since_high_at: int | None = None
    bars_used: int = 0
    session_start: int | None = None
    #: Width of the bars these figures came from, in seconds. A time is exact
    #: only to this granularity, and the dashboard says so.
    resolution_seconds: int = 60
    #: "bars", "quote", or "bars+quote" when the live quote extended a bar
    #: extreme within the still-forming bar.
    source: str = ""
    note: str = ""

    @property
    def complete(self) -> bool:
        return bool(self.day_high > 0 and self.day_low > 0)

    @property
    def has_swing_anchors(self) -> bool:
        """Whether both swing levels are usable as ladder anchors."""
        return bool(self.high_since_low > 0 and self.low_since_high > 0)

    @property
    def anchors(self) -> list[tuple[str, float]]:
        """The two levels the ladder hangs off, as ``(code, price)``."""
        return [("HSL", self.high_since_low), ("LSH", self.low_since_high)]

    @property
    def low_came_first(self) -> bool | None:
        """True when the session bottomed before it topped."""
        if self.day_low_at is None or self.day_high_at is None:
            return None
        return self.day_low_at < self.day_high_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "day_high": round(self.day_high, 4) if self.day_high else 0.0,
            "day_high_at": self.day_high_at,
            "day_low": round(self.day_low, 4) if self.day_low else 0.0,
            "day_low_at": self.day_low_at,
            "high_since_low": (
                round(self.high_since_low, 4) if self.high_since_low else 0.0
            ),
            "high_since_low_at": self.high_since_low_at,
            "low_since_high": (
                round(self.low_since_high, 4) if self.low_since_high else 0.0
            ),
            "low_since_high_at": self.low_since_high_at,
            "bars_used": self.bars_used,
            "session_start": self.session_start,
            "resolution_seconds": self.resolution_seconds,
            "low_came_first": self.low_came_first,
            "has_swing_anchors": self.has_swing_anchors,
            "complete": self.complete,
            "source": self.source,
            "note": self.note,
        }


#: Bar-size strings the brokers accept, and their width in seconds.
BAR_SECONDS: dict[str, int] = {
    "1min": 60, "2min": 120, "3min": 180, "5min": 300, "10min": 600,
    "15min": 900, "30min": 1800, "1h": 3600, "2h": 7200, "4h": 14400,
    "8h": 28800, "1d": 86400,
}


def bar_seconds(bar_size: str, default: int = 60) -> int:
    return BAR_SECONDS.get((bar_size or "").strip().lower(), default)


def session_start_epoch(now: float, outside_rth: bool = True) -> int | None:
    """Epoch second at which the current trading day's session began.

    Before the session opens this returns the *previous* day's open, so the
    figures on screen stay those of the last real session rather than
    emptying out overnight.
    """
    if not TZ_AVAILABLE:
        return None
    eastern = datetime.fromtimestamp(now, timezone.utc).astimezone(_EASTERN)
    open_at = EXTENDED_OPEN if outside_rth else REGULAR_OPEN
    start = eastern.replace(
        hour=open_at.hour, minute=open_at.minute, second=0, microsecond=0
    )
    if eastern < start:
        start -= timedelta(days=1)
    return int(start.timestamp())


def compute_session_range(
    symbol: str,
    bars: Sequence[Bar],
    *,
    now: float,
    quote: Quote | None = None,
    outside_rth: bool = True,
    resolution_seconds: int = 60,
) -> SessionRange:
    """Derive the session extremes and the swings between them.

    ``bars`` may span many days; only those in the current session are used.
    ``quote`` is optional and only ever *extends* what the bars already show —
    the most recent moments are not in a closed bar yet, so a live quote can
    legitimately print past the last bar's high or low.
    """
    result = SessionRange(
        symbol=symbol.upper(), resolution_seconds=resolution_seconds
    )
    start = session_start_epoch(now, outside_rth)
    result.session_start = start

    if start is None:
        result.note = (
            "timezone database unavailable, so the session boundary cannot be "
            "determined (install tzdata)"
        )
        session_bars: list[Bar] = []
    else:
        session_bars = [b for b in bars if b.ts >= start and b.symbol.upper()
                        == symbol.upper()]

    result.bars_used = len(session_bars)

    if session_bars:
        result.source = "bars"
        high_bar = max(session_bars, key=lambda b: b.high)
        low_bar = min(session_bars, key=lambda b: b.low)
        result.day_high, result.day_high_at = high_bar.high, high_bar.ts
        result.day_low, result.day_low_at = low_bar.low, low_bar.ts

        # A live quote can print beyond the last closed bar. Only widen, never
        # narrow -- a bar that already traded there is a fact.
        if quote is not None:
            latest = max(b.ts for b in session_bars)
            if quote.day_high > result.day_high:
                result.day_high, result.day_high_at = quote.day_high, latest
                result.source = "bars+quote"
            if 0 < quote.day_low < result.day_low:
                result.day_low, result.day_low_at = quote.day_low, latest
                result.source = "bars+quote"

        # "Since" windows are inclusive of the bar that set the extreme: the
        # run up off the low includes the low bar's own high.
        after_low = [b for b in session_bars if b.ts >= (result.day_low_at or 0)]
        if after_low:
            best = max(after_low, key=lambda b: b.high)
            result.high_since_low, result.high_since_low_at = best.high, best.ts
        after_high = [b for b in session_bars if b.ts >= (result.day_high_at or 0)]
        if after_high:
            worst = min(after_high, key=lambda b: b.low)
            result.low_since_high, result.low_since_high_at = worst.low, worst.ts

        # The quote may also have extended the trailing swing inside the
        # still-forming bar.
        if quote is not None:
            if quote.day_high > result.high_since_low:
                result.high_since_low = quote.day_high
                result.high_since_low_at = max(b.ts for b in after_low or session_bars)
            if 0 < quote.day_low < result.low_since_high:
                result.low_since_high = quote.day_low
                result.low_since_high_at = max(
                    b.ts for b in after_high or session_bars
                )
        return result

    # No bars for this session: fall back to whatever the quote knows, which
    # carries the levels but no timing.
    if quote is not None and quote.has_day_range:
        result.day_high = quote.day_high
        result.day_low = quote.day_low
        result.high_since_low = quote.day_high
        result.low_since_high = quote.day_low
        result.source = "quote"
        result.note = (
            "no cached bars for this session yet, so the levels come from the "
            "live quote and their timing is unknown"
        )
    elif not result.note:
        result.note = "no session data available yet"
    return result
