"""Account NAV history and period-return calculation.

Neither broker hands us a ready-made "return over the last N months" that we
can trust uniformly, so this module owns the problem in three layers:

1. **The ledger** — a local, append-only daily NAV series per broker. Exact,
   identical for every broker, and the only layer that needs no API support.
   It starts empty, so it can only answer for periods since you installed this.
2. **Backfill** (``backfill.py``) — history reconstructed from broker
   transaction and price-history endpoints, or taken from a broker's own
   performance analytics. Fills the ledger's blind spot behind today.
3. **The maths here** — turns whatever series we ended up with into period
   returns, and is explicit about how much of each window it could actually
   cover.

Returns are **time-weighted**. A deposit is not performance: if you wire in
$10,000, a naive (end-start)/start would report a spectacular month. TWR
removes external cash flows so the number reflects how the *investments* did.
"""

from __future__ import annotations

import calendar
import json
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

#: The windows the dashboard reports, in calendar months.
DEFAULT_WINDOWS: tuple[tuple[str, int], ...] = (
    ("1M", 1),
    ("2M", 2),
    ("3M", 3),
    ("6M", 6),
)

#: How a NAV point was obtained, worst-to-best confidence. Surfaced in the UI
#: so a reconstructed number is never mistaken for a measured one.
SOURCE_SNAPSHOT = "snapshot"          # we observed the account ourselves
SOURCE_BROKER = "broker"              # the broker's own analytics
SOURCE_RECONSTRUCTED = "reconstructed"  # rebuilt from transactions + prices

_SOURCE_RANK = {
    SOURCE_SNAPSHOT: 3,
    SOURCE_BROKER: 2,
    SOURCE_RECONSTRUCTED: 1,
}


def today_utc() -> date:
    return datetime.now(timezone.utc).date()


def months_ago(anchor: date, months: int) -> date:
    """Calendar-month subtraction that survives month-end.

    31 March minus one month is 28/29 February, not an error and not 3 March.
    """
    year = anchor.year
    month = anchor.month - months
    while month <= 0:
        month += 12
        year -= 1
    day = min(anchor.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


@dataclass(frozen=True)
class NavPoint:
    """One account-value observation, on one date, for one broker."""

    date: str                       # YYYY-MM-DD (UTC)
    nav: float                      # cash + marked-to-market positions
    cash: float = 0.0
    positions_value: float = 0.0
    #: Net external money moved in (+) or out (-) on this date. Deposits,
    #: withdrawals and transfers — never trades, which move value between
    #: cash and positions without changing NAV.
    external_flow: float = 0.0
    source: str = SOURCE_SNAPSHOT

    @property
    def as_date(self) -> date:
        return datetime.strptime(self.date, "%Y-%m-%d").date()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PerformanceWindow:
    """The answer for one period, plus how much to trust it."""

    label: str                      # "3M"
    months: int
    start_date: str | None = None
    end_date: str | None = None
    start_nav: float | None = None
    end_nav: float | None = None
    net_flows: float = 0.0
    twr_pct: float | None = None    # time-weighted return, percent
    change_dollars: float | None = None  # end - start - flows
    #: True when the series actually reaches back to the start of the window.
    complete: bool = False
    covered_days: int = 0
    requested_days: int = 0
    sources: list[str] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for key in ("twr_pct", "change_dollars", "start_nav", "end_nav"):
            if d[key] is not None:
                d[key] = round(d[key], 4)
        d["net_flows"] = round(d["net_flows"], 2)
        return d


@dataclass
class BrokerPerformance:
    broker: str
    windows: list[PerformanceWindow] = field(default_factory=list)
    series: list[NavPoint] = field(default_factory=list)
    current_nav: float | None = None
    error: str | None = None

    def to_dict(self, include_series: bool = True) -> dict[str, Any]:
        return {
            "broker": self.broker,
            "current_nav": (
                round(self.current_nav, 2) if self.current_nav is not None else None
            ),
            "windows": [w.to_dict() for w in self.windows],
            "series": [p.to_dict() for p in self.series] if include_series else [],
            "error": self.error,
        }


# --- the ledger --------------------------------------------------------------


class NavLedger:
    """Append-only JSONL store of daily NAV points, one line per observation.

    Append-only means a crash cannot corrupt earlier history. Reads collapse
    duplicates for the same (broker, date), preferring the more trustworthy
    source and, within a source, the most recent write.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def record(self, broker: str, point: NavPoint) -> None:
        with self._lock:
            self._append(broker, point)

    def record_many(self, broker: str, points: Iterable[NavPoint]) -> int:
        written = 0
        with self._lock:
            for point in points:
                self._append(broker, point)
                written += 1
        return written

    def _append(self, broker: str, point: NavPoint) -> None:
        row = {"broker": broker, **point.to_dict()}
        prefix = "\n" if self._needs_newline() else ""
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(prefix + json.dumps(row, separators=(",", ":")) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def _needs_newline(self) -> bool:
        """True when a previous write was torn off mid-line.

        Appending straight onto a truncated record would fuse the two into
        one unparsable line, losing the new point as well as the old.
        """
        try:
            size = self.path.stat().st_size
        except OSError:
            return False
        if size == 0:
            return False
        with open(self.path, "rb") as fh:
            fh.seek(-1, os.SEEK_END)
            return fh.read(1) != b"\n"

    def _read_all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        rows: list[dict[str, Any]] = []
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue          # a torn final line must not lose the rest
        return rows

    def series(self, broker: str) -> list[NavPoint]:
        """Deduplicated, date-ascending NAV series for one broker."""
        best: dict[str, tuple[int, int, NavPoint]] = {}
        for index, row in enumerate(self._read_all()):
            if row.get("broker") != broker:
                continue
            try:
                point = NavPoint(
                    date=str(row["date"]),
                    nav=float(row["nav"]),
                    cash=float(row.get("cash", 0) or 0),
                    positions_value=float(row.get("positions_value", 0) or 0),
                    external_flow=float(row.get("external_flow", 0) or 0),
                    source=str(row.get("source", SOURCE_SNAPSHOT)),
                )
            except (KeyError, TypeError, ValueError):
                continue
            rank = _SOURCE_RANK.get(point.source, 0)
            existing = best.get(point.date)
            # Higher-confidence source wins; ties go to the later write.
            if existing is None or (rank, index) > (existing[0], existing[1]):
                best[point.date] = (rank, index, point)
        return [best[d][2] for d in sorted(best)]

    def brokers(self) -> list[str]:
        return sorted({str(r.get("broker")) for r in self._read_all() if r.get("broker")})

    def has_date(self, broker: str, day: str) -> bool:
        return any(p.date == day for p in self.series(broker))

    def earliest(self, broker: str) -> date | None:
        series = self.series(broker)
        return series[0].as_date if series else None

    def fill_gaps(self, broker: str, points: Iterable[NavPoint]) -> int:
        """Write backfilled points, but never overwrite a real observation."""
        existing = {p.date for p in self.series(broker)}
        fresh = [p for p in points if p.date not in existing]
        return self.record_many(broker, fresh) if fresh else 0


# --- return maths ------------------------------------------------------------


def time_weighted_return(points: list[NavPoint]) -> float | None:
    """Link daily sub-period returns, neutralising external cash flows.

    For each step, the flow is treated as arriving at the *start* of the
    period, so it is removed from the ending value before measuring growth:

        r_i = (NAV_i - flow_i) / NAV_(i-1) - 1
        TWR = Π (1 + r_i) - 1

    Returns ``None`` when there is no usable pair of points. A step whose
    prior NAV is zero or negative (a fully withdrawn account) cannot produce
    a meaningful ratio, so the chain restarts rather than dividing by it.
    """
    if len(points) < 2:
        return None

    factor = 1.0
    used = 0
    for prev, cur in zip(points, points[1:]):
        if prev.nav <= 0:
            continue
        adjusted_end = cur.nav - cur.external_flow
        if adjusted_end <= 0:
            continue
        factor *= adjusted_end / prev.nav
        used += 1

    if used == 0:
        return None
    return (factor - 1.0) * 100.0


def compute_window(
    series: list[NavPoint],
    label: str,
    months: int,
    anchor: date | None = None,
) -> PerformanceWindow:
    """Return performance over the trailing ``months``, honestly labelled."""
    anchor = anchor or today_utc()
    cutoff = months_ago(anchor, months)
    requested_days = (anchor - cutoff).days

    window = PerformanceWindow(
        label=label, months=months, requested_days=requested_days
    )
    if not series:
        window.note = "no history recorded yet"
        return window

    inside = [p for p in series if p.as_date >= cutoff]
    before = [p for p in series if p.as_date < cutoff]

    # Anchor the start on the value as of the cutoff date. If an observation
    # lands exactly on it, that is the anchor. Otherwise the last observation
    # before it is the best available estimate of the value on that day —
    # measuring from the first point *inside* the window would silently
    # shorten the period being reported.
    if inside and inside[0].as_date == cutoff:
        window.complete = True
    elif before:
        inside = [before[-1]] + inside
        window.complete = True
    else:
        window.complete = False

    if len(inside) < 2:
        window.note = (
            "not enough history to measure this period; NAV is recorded daily "
            "from now on"
        )
        if inside:
            window.start_date = window.end_date = inside[0].date
            window.start_nav = window.end_nav = inside[0].nav
        return window

    first, last = inside[0], inside[-1]
    window.start_date, window.end_date = first.date, last.date
    window.start_nav, window.end_nav = first.nav, last.nav
    window.covered_days = (last.as_date - first.as_date).days
    window.net_flows = sum(p.external_flow for p in inside[1:])
    window.change_dollars = last.nav - first.nav - window.net_flows
    window.twr_pct = time_weighted_return(inside)
    window.sources = sorted({p.source for p in inside})

    notes: list[str] = []
    if not window.complete:
        notes.append(
            f"history starts {first.date}, covering {window.covered_days} of "
            f"{requested_days} days — this is a partial period"
        )
    if SOURCE_RECONSTRUCTED in window.sources:
        notes.append(
            "includes history reconstructed from transactions and closing "
            "prices; verify against your broker statement"
        )
    window.note = "; ".join(notes)
    return window


def compute_windows(
    series: list[NavPoint],
    windows: tuple[tuple[str, int], ...] = DEFAULT_WINDOWS,
    anchor: date | None = None,
) -> list[PerformanceWindow]:
    return [compute_window(series, label, months, anchor) for label, months in windows]


def combine_series(series_by_broker: dict[str, list[NavPoint]]) -> list[NavPoint]:
    """Sum brokers into one portfolio series on their common dates.

    Only dates where *every* contributing broker has a point are used. Summing
    a date where one broker is missing would show a fake overnight collapse in
    the combined line.
    """
    non_empty = {b: s for b, s in series_by_broker.items() if s}
    if not non_empty:
        return []
    if len(non_empty) == 1:
        only = next(iter(non_empty.values()))
        return [
            NavPoint(p.date, p.nav, p.cash, p.positions_value, p.external_flow,
                     p.source)
            for p in only
        ]

    by_date: dict[str, dict[str, NavPoint]] = {}
    for broker, points in non_empty.items():
        for point in points:
            by_date.setdefault(point.date, {})[broker] = point

    combined: list[NavPoint] = []
    for day in sorted(by_date):
        entries = by_date[day]
        if len(entries) < len(non_empty):
            continue
        points = list(entries.values())
        combined.append(
            NavPoint(
                date=day,
                nav=sum(p.nav for p in points),
                cash=sum(p.cash for p in points),
                positions_value=sum(p.positions_value for p in points),
                external_flow=sum(p.external_flow for p in points),
                source=min((p.source for p in points),
                           key=lambda s: _SOURCE_RANK.get(s, 0)),
            )
        )
    return combined


def nav_from_snapshot(
    cash: float, positions_value: float, day: date | None = None
) -> NavPoint:
    return NavPoint(
        date=(day or today_utc()).isoformat(),
        nav=cash + positions_value,
        cash=cash,
        positions_value=positions_value,
        external_flow=0.0,
        source=SOURCE_SNAPSHOT,
    )


def trim_series(points: list[NavPoint], months: int = 6,
                anchor: date | None = None) -> list[NavPoint]:
    """Points inside the longest reported window, for charting."""
    cutoff = months_ago(anchor or today_utc(), months)
    return [p for p in points if p.as_date >= cutoff]


def daterange(start: date, end: date) -> Iterable[date]:
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)
