"""Cached OHLCV candles, backed by SQLite.

Ninety days of hourly extended-hours bars is roughly 1,400 rows per symbol.
Re-fetching that on every dashboard refresh would be slow and would burn
broker rate limit for data that, by definition, cannot change: a bar that
closed last Tuesday is final.

So the cache is authoritative for everything except the recent edge, and a
refresh does the smallest fetch that can be correct:

* **Nothing cached** — fetch the whole window.
* **History does not reach back far enough** — fetch the missing head.
* **An interior gap** (a day the server was down) — fetch just that span.
* **Otherwise** — fetch only the last ``CANDLE_REFRESH_HOURS`` (default 2),
  which is what refreshes the still-forming current bar and the one behind
  it.

The last case is the common one and costs a single small request per symbol.

Schema changes go in :data:`MIGRATIONS` as new numbered entries, applied in
order and recorded in ``schema_version``. Existing entries are never edited —
an already-applied migration will not run again, so changing one would leave
old and new databases with different shapes.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

from .models import Bar

log = logging.getLogger("trading.candles")

#: Ordered schema migrations. Append only; never edit an existing entry.
MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS bars (
            symbol  TEXT    NOT NULL,
            ts      INTEGER NOT NULL,   -- bar OPEN, epoch seconds UTC
            open    REAL    NOT NULL,
            high    REAL    NOT NULL,
            low     REAL    NOT NULL,
            close   REAL    NOT NULL,
            volume  REAL    NOT NULL DEFAULT 0,
            source  TEXT    NOT NULL DEFAULT '',
            fetched_at REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (symbol, ts)
        );
        CREATE INDEX IF NOT EXISTS bars_symbol_ts ON bars (symbol, ts);
        """,
    ),
    (
        2,
        """
        -- Ranges we have already asked a broker for. A market holiday leaves a
        -- permanent hole that no fetch can fill, so without this the gap
        -- detector would re-request the same dead span on every refresh.
        CREATE TABLE IF NOT EXISTS fetched_ranges (
            symbol     TEXT    NOT NULL,
            start_ts   INTEGER NOT NULL,
            end_ts     INTEGER NOT NULL,
            fetched_at REAL    NOT NULL,
            PRIMARY KEY (symbol, start_ts, end_ts)
        );
        CREATE INDEX IF NOT EXISTS fetched_symbol ON fetched_ranges (symbol);
        """,
    ),
    (
        3,
        """
        -- Bar size becomes part of a bar's identity. Session extremes are
        -- derived from a minute series while the chart uses hourly bars, and
        -- without this the two would collide on (symbol, ts).
        -- SQLite cannot alter a primary key, so the table is rebuilt.
        CREATE TABLE IF NOT EXISTS bars_v3 (
            symbol   TEXT    NOT NULL,
            bar_size TEXT    NOT NULL,
            ts       INTEGER NOT NULL,
            open     REAL    NOT NULL,
            high     REAL    NOT NULL,
            low      REAL    NOT NULL,
            close    REAL    NOT NULL,
            volume   REAL    NOT NULL DEFAULT 0,
            source   TEXT    NOT NULL DEFAULT '',
            fetched_at REAL  NOT NULL DEFAULT 0,
            PRIMARY KEY (symbol, bar_size, ts)
        );
        INSERT OR IGNORE INTO bars_v3
            (symbol, bar_size, ts, open, high, low, close, volume, source, fetched_at)
        SELECT symbol, '1h', ts, open, high, low, close, volume, source, fetched_at
        FROM bars;
        DROP TABLE bars;
        ALTER TABLE bars_v3 RENAME TO bars;
        CREATE INDEX IF NOT EXISTS bars_lookup ON bars (symbol, bar_size, ts);

        -- Fetch bookkeeping is likewise per bar size.
        ALTER TABLE fetched_ranges ADD COLUMN bar_size TEXT NOT NULL DEFAULT '1h';
        """,
    ),
]

HOUR = 3600


class CandleStore:
    """SQLite-backed bar cache. Safe to use from several threads."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # The API serves requests on a thread pool, so the connection is
        # shared with check_same_thread=False and guarded by _lock.
        self._db = sqlite3.connect(
            str(self.path), check_same_thread=False, timeout=10.0
        )
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._migrate()

    def _migrate(self) -> None:
        with self._lock:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS schema_version ("
                " version INTEGER PRIMARY KEY,"
                " applied_at REAL NOT NULL)"
            )
            applied = {
                row["version"]
                for row in self._db.execute("SELECT version FROM schema_version")
            }
            for version, sql in sorted(MIGRATIONS):
                if version in applied:
                    continue
                self._db.executescript(sql)
                self._db.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (version, time.time()),
                )
                log.info("candle store: applied migration %d", version)
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # --- writes -----------------------------------------------------------

    def upsert(self, bars: Iterable[Bar], source: str = "",
               bar_size: str = "1h") -> int:
        """Insert or replace bars. Returns how many rows were written.

        Replace rather than ignore: the most recent bar of a session is still
        forming, so a later fetch of the same timestamp is a *correction* and
        must win over what is already stored.
        """
        rows = [
            (b.symbol.upper(), bar_size, int(b.ts), b.open, b.high, b.low,
             b.close, b.volume, source, time.time())
            for b in bars
        ]
        if not rows:
            return 0
        with self._lock:
            self._db.executemany(
                "INSERT INTO bars"
                " (symbol, bar_size, ts, open, high, low, close, volume,"
                "  source, fetched_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(symbol, bar_size, ts) DO UPDATE SET"
                "  open=excluded.open, high=excluded.high, low=excluded.low,"
                "  close=excluded.close, volume=excluded.volume,"
                "  source=excluded.source, fetched_at=excluded.fetched_at",
                rows,
            )
            self._db.commit()
        return len(rows)

    # --- reads ------------------------------------------------------------

    def bars(self, symbol: str, start: float, end: float,
             bar_size: str = "1h") -> list[Bar]:
        with self._lock:
            cursor = self._db.execute(
                "SELECT symbol, ts, open, high, low, close, volume FROM bars"
                " WHERE symbol = ? AND bar_size = ? AND ts >= ? AND ts <= ?"
                " ORDER BY ts",
                (symbol.upper(), bar_size, int(start), int(end)),
            )
            return [
                Bar(
                    symbol=row["symbol"], ts=row["ts"], open=row["open"],
                    high=row["high"], low=row["low"], close=row["close"],
                    volume=row["volume"],
                )
                for row in cursor
            ]

    def record_fetch(self, symbol: str, start: float, end: float,
                     bar_size: str = "1h") -> None:
        """Remember that this span was requested, filled or not."""
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO fetched_ranges"
                " (symbol, start_ts, end_ts, fetched_at, bar_size)"
                " VALUES (?, ?, ?, ?, ?)",
                (symbol.upper(), int(start), int(end), time.time(), bar_size),
            )
            self._db.commit()

    def was_fetched(self, symbol: str, start: float, end: float,
                    bar_size: str = "1h") -> bool:
        """True when some earlier request already covered this whole span."""
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM fetched_ranges"
                " WHERE symbol = ? AND bar_size = ? AND start_ts <= ?"
                " AND end_ts >= ? LIMIT 1",
                (symbol.upper(), bar_size, int(start), int(end)),
            ).fetchone()
        return row is not None

    def prune(self, symbol: str, bar_size: str, before: float) -> int:
        """Drop bars older than ``before`` for one series.

        Minute bars exist only to time the current session's extremes, so
        keeping 90 days of them would be ~86k rows per symbol for no benefit.
        """
        with self._lock:
            cursor = self._db.execute(
                "DELETE FROM bars WHERE symbol = ? AND bar_size = ? AND ts < ?",
                (symbol.upper(), bar_size, int(before)),
            )
            self._db.commit()
            return cursor.rowcount or 0

    def coverage(self, symbol: str,
                 bar_size: str = "1h") -> tuple[int | None, int | None, int]:
        """``(oldest_ts, newest_ts, count)`` for one series."""
        with self._lock:
            row = self._db.execute(
                "SELECT MIN(ts) AS lo, MAX(ts) AS hi, COUNT(*) AS n"
                " FROM bars WHERE symbol = ? AND bar_size = ?",
                (symbol.upper(), bar_size),
            ).fetchone()
        return (row["lo"], row["hi"], row["n"] or 0)

    def interior_gaps(
        self, symbol: str, start: float, end: float, max_gap_hours: float,
        bar_size: str = "1h",
    ) -> list[tuple[int, int]]:
        """Spans inside the cached range with no bars for too long.

        A weekend is a legitimate ~65-hour hole in an extended-hours hourly
        series, so ``max_gap_hours`` has to be larger than that or every
        Monday would look like missing data.
        """
        bars = self.bars(symbol, start, end, bar_size)
        if len(bars) < 2:
            return []
        limit = max_gap_hours * HOUR
        gaps: list[tuple[int, int]] = []
        for previous, current in zip(bars, bars[1:]):
            if current.ts - previous.ts > limit:
                gaps.append((previous.ts, current.ts))
        return gaps


class CandleService:
    """Decides how little can be fetched and still be correct."""

    def __init__(self, settings: Any, store: CandleStore) -> None:
        self.settings = settings
        self.store = store

    def _fetch(
        self,
        brokers: Sequence[Any],
        symbol: str,
        start: float,
        end: float,
        bar_size: str,
    ) -> tuple[list[Bar], str, list[str]]:
        """Ask each broker in turn until one supplies bars."""
        errors: list[str] = []
        for broker in brokers:
            try:
                if not broker.connected:
                    broker.connect()
                bars = broker.get_price_bars(
                    symbol,
                    start,
                    end,
                    bar_size=bar_size,
                    outside_rth=self.settings.candle_outside_rth,
                )
            except Exception as exc:
                errors.append(f"{broker.name}: {exc}")
                continue
            if bars is None:      # adapter cannot do bars at all
                continue
            return bars, broker.name, errors
        return [], "", errors

    def refresh(
        self,
        brokers: Sequence[Any],
        symbol: str,
        days: int | None = None,
        bar_size: str | None = None,
    ) -> dict[str, Any]:
        """Bring ``symbol`` up to date with the smallest correct fetch."""
        bar_size = bar_size or self.settings.candle_bar_size
        days = days or self.settings.candle_days
        now = time.time()
        window_start = now - days * 86400
        oldest, newest, count = self.store.coverage(symbol, bar_size)

        ranges: list[tuple[float, float]] = []
        reason: str

        if count == 0 or oldest is None or newest is None:
            ranges = [(window_start, now)]
            reason = "empty cache: full backfill"
        else:
            # Head: history does not reach back far enough yet.
            if oldest > window_start + 86400:
                ranges.append((window_start, float(oldest)))
            # Tail: everything since the newest cached bar. Always at least
            # the refresh window, so the still-forming bar gets corrected.
            tail_start = min(
                float(newest), now - self.settings.candle_refresh_hours * HOUR
            )
            ranges.append((tail_start, now))
            # Interior: a span we never filled, e.g. the server was off.
            gaps = [
                (a, b)
                for a, b in self.store.interior_gaps(
                    symbol, window_start, now,
                    self.settings.candle_max_gap_hours, bar_size,
                )
                # A holiday or a half-session is a hole the broker simply has
                # no bars for. Asking once is diligence; asking every refresh
                # forever is a bug.
                if not self.store.was_fetched(symbol, a, b, bar_size)
            ]
            ranges.extend((float(a), float(b)) for a, b in gaps)
            reason = (
                "incremental refresh"
                if len(ranges) == 1
                else f"refresh plus {len(ranges) - 1} gap fill(s)"
            )

        written = 0
        source = ""
        errors: list[str] = []
        for start, end in ranges:
            bars, used, errs = self._fetch(brokers, symbol, start, end, bar_size)
            errors.extend(errs)
            if used:
                source = used
                # Only a request that actually reached a broker counts as
                # attempted; a connection failure must stay retryable.
                self.store.record_fetch(symbol, start, end, bar_size)
            if bars:
                written += self.store.upsert(bars, source=used, bar_size=bar_size)

        # Fine-grained series exist only to time the current session, so they
        # are pruned rather than accumulated.
        if bar_size != self.settings.candle_bar_size:
            self.store.prune(symbol, bar_size, window_start)

        oldest, newest, count = self.store.coverage(symbol, bar_size)
        return {
            "symbol": symbol.upper(),
            "bar_size": bar_size,
            "reason": reason,
            "ranges_fetched": [
                {"start": int(a), "end": int(b)} for a, b in ranges
            ],
            "bars_written": written,
            "source": source,
            "cached_bars": count,
            "oldest": oldest,
            "newest": newest,
            "errors": errors,
        }

    def series(
        self,
        brokers: Sequence[Any],
        symbol: str,
        days: int | None = None,
        refresh: bool = True,
        bar_size: str | None = None,
    ) -> dict[str, Any]:
        bar_size = bar_size or self.settings.candle_bar_size
        days = days or self.settings.candle_days
        meta: dict[str, Any] = {}
        if refresh:
            meta = self.refresh(brokers, symbol, days, bar_size)
        now = time.time()
        bars = self.store.bars(symbol, now - days * 86400, now, bar_size)
        return {
            "symbol": symbol.upper(),
            "bar_size": bar_size,
            "outside_rth": self.settings.candle_outside_rth,
            "days": days,
            # [ts, o, h, l, c, v] per bar: ~1400 bars stay compact on the wire.
            "bars": [b.to_row() for b in bars],
            "refresh": meta,
        }
