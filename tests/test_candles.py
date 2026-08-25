"""Candle cache: schema, upsert semantics, and the minimal-fetch policy."""

from __future__ import annotations

import time

import pytest

from trading.candles import HOUR, CandleService, CandleStore
from trading.config import Settings
from trading.models import Bar, TradingMode


def _settings(tmp_path, **overrides):
    s = Settings(
        mode=TradingMode.PAPER,
        log_dir=tmp_path / "log",
        state_dir=tmp_path / "state",
    )
    for key, value in overrides.items():
        setattr(s, key, value)
    return s


def _bars(symbol, start_ts, count, step=HOUR, price=100.0):
    return [
        Bar(symbol=symbol, ts=int(start_ts + i * step), open=price, high=price + 1,
            low=price - 1, close=price + 0.5, volume=1000)
        for i in range(count)
    ]


class FakeBroker:
    """Records the ranges it was asked for, so the policy can be asserted."""

    name = "fake"

    def __init__(self, bars_by_range=None, supports=True):
        self.connected = True
        self.requests: list[tuple[float, float]] = []
        self._supports = supports
        self._bars = bars_by_range or []

    def connect(self):
        self.connected = True

    def get_price_bars(self, symbol, start, end, *, bar_size="1h", outside_rth=True):
        self.requests.append((start, end))
        if not self._supports:
            return None
        return [b for b in self._bars if start <= b.ts <= end]


# --- store ------------------------------------------------------------------


def test_migrations_apply_once_and_are_idempotent(tmp_path):
    path = tmp_path / "candles.db"
    CandleStore(path).close()
    store = CandleStore(path)          # reopening must not re-run migrations
    versions = sorted(
        row[0] for row in store._db.execute("SELECT version FROM schema_version")
    )
    from trading.candles import MIGRATIONS

    assert versions == sorted(v for v, _ in MIGRATIONS)
    store.close()


def test_a_gap_the_broker_cannot_fill_is_not_re_requested(tmp_path):
    """A market holiday is a permanent hole; asking once is enough."""
    s = _settings(tmp_path, candle_days=90, candle_max_gap_hours=80.0)
    store = CandleStore(tmp_path / "c.db")
    now = time.time()
    store.upsert(_bars("QQQ", now - 90 * 86400, 40 * 24, step=HOUR))
    store.upsert(_bars("QQQ", now - 30 * 86400, 30 * 24, step=HOUR))

    service = CandleService(s, store)
    first = FakeBroker()
    meta = service.refresh([first], "QQQ")
    assert "gap fill" in meta["reason"]
    gap_requests = len(first.requests)

    # The broker had nothing for that span, so the hole is still there --
    # but it must not be requested a second time.
    second = FakeBroker()
    meta = service.refresh([second], "QQQ")
    assert meta["reason"] == "incremental refresh"
    assert len(second.requests) < gap_requests
    store.close()


def test_upsert_corrects_a_still_forming_bar(tmp_path):
    """Re-fetching the current hour must overwrite, not be ignored."""
    store = CandleStore(tmp_path / "c.db")
    ts = 1_700_000_000
    store.upsert([Bar("QQQ", ts, 100, 101, 99, 100.5, 10)])
    store.upsert([Bar("QQQ", ts, 100, 105, 99, 104.0, 90)])

    bars = store.bars("QQQ", ts - 10, ts + 10)
    assert len(bars) == 1
    assert bars[0].close == 104.0
    assert bars[0].high == 105
    store.close()


def test_symbols_do_not_collide(tmp_path):
    store = CandleStore(tmp_path / "c.db")
    ts = 1_700_000_000
    store.upsert([Bar("QQQ", ts, 500, 501, 499, 500, 1)])
    store.upsert([Bar("PSQ", ts, 32, 33, 31, 32, 1)])

    assert len(store.bars("QQQ", ts - 1, ts + 1)) == 1
    assert store.bars("PSQ", ts - 1, ts + 1)[0].close == 32
    store.close()


def test_weekend_is_not_reported_as_a_gap(tmp_path):
    """A ~65h weekend hole is normal and must not trigger a refetch."""
    store = CandleStore(tmp_path / "c.db")
    base = 1_700_000_000
    store.upsert(_bars("QQQ", base, 5))
    store.upsert(_bars("QQQ", base + 65 * HOUR, 5))

    gaps = store.interior_gaps("QQQ", base - HOUR, base + 200 * HOUR, 80.0)
    assert gaps == []
    store.close()


def test_a_missing_week_is_reported_as_a_gap(tmp_path):
    store = CandleStore(tmp_path / "c.db")
    base = 1_700_000_000
    store.upsert(_bars("QQQ", base, 5))
    store.upsert(_bars("QQQ", base + 200 * HOUR, 5))

    gaps = store.interior_gaps("QQQ", base - HOUR, base + 300 * HOUR, 80.0)
    assert len(gaps) == 1
    store.close()


# --- refresh policy ---------------------------------------------------------


def test_empty_cache_fetches_the_whole_window(tmp_path):
    s = _settings(tmp_path, candle_days=90)
    store = CandleStore(tmp_path / "c.db")
    service = CandleService(s, store)
    broker = FakeBroker()

    meta = service.refresh([broker], "QQQ")

    assert "full backfill" in meta["reason"]
    assert len(broker.requests) == 1
    start, end = broker.requests[0]
    assert (end - start) / 86400 == pytest.approx(90, abs=1)
    store.close()


def test_warm_cache_only_refetches_the_recent_window(tmp_path):
    """The whole point of the cache: a routine refresh is small."""
    s = _settings(tmp_path, candle_days=90, candle_refresh_hours=2.0)
    store = CandleStore(tmp_path / "c.db")
    now = time.time()
    # Continuous coverage across the full window, right up to now.
    store.upsert(_bars("QQQ", now - 90 * 86400, 90 * 24, step=HOUR))

    service = CandleService(s, store)
    broker = FakeBroker()
    meta = service.refresh([broker], "QQQ")

    assert meta["reason"] == "incremental refresh"
    assert len(broker.requests) == 1
    start, end = broker.requests[0]
    # Only the trailing couple of hours, not 90 days.
    assert (end - start) <= 3 * HOUR
    store.close()


def test_short_history_backfills_the_head(tmp_path):
    s = _settings(tmp_path, candle_days=90)
    store = CandleStore(tmp_path / "c.db")
    now = time.time()
    store.upsert(_bars("QQQ", now - 5 * 86400, 5 * 24, step=HOUR))

    service = CandleService(s, store)
    broker = FakeBroker()
    meta = service.refresh([broker], "QQQ")

    assert len(broker.requests) == 2, "expected a head backfill plus the tail"
    head = broker.requests[0]
    assert (head[1] - head[0]) / 86400 == pytest.approx(85, abs=1)
    store.close()


def test_interior_gap_is_filled(tmp_path):
    s = _settings(tmp_path, candle_days=90, candle_max_gap_hours=80.0)
    store = CandleStore(tmp_path / "c.db")
    now = time.time()
    store.upsert(_bars("QQQ", now - 90 * 86400, 40 * 24, step=HOUR))
    store.upsert(_bars("QQQ", now - 30 * 86400, 30 * 24, step=HOUR))

    service = CandleService(s, store)
    broker = FakeBroker()
    meta = service.refresh([broker], "QQQ")

    assert "gap fill" in meta["reason"]
    store.close()


def test_unsupported_broker_is_skipped_not_an_error(tmp_path):
    s = _settings(tmp_path)
    store = CandleStore(tmp_path / "c.db")
    service = CandleService(s, store)
    now = time.time()

    unsupported = FakeBroker(supports=False)
    supported = FakeBroker(bars_by_range=_bars("QQQ", now - 3 * HOUR, 3))

    meta = service.refresh([unsupported, supported], "QQQ")

    assert meta["source"] == "fake"
    assert meta["bars_written"] > 0
    assert meta["errors"] == []
    store.close()


def test_series_returns_compact_rows(tmp_path):
    s = _settings(tmp_path)
    store = CandleStore(tmp_path / "c.db")
    now = time.time()
    store.upsert(_bars("QQQ", now - 3 * HOUR, 3))

    service = CandleService(s, store)
    out = service.series([], "QQQ", refresh=False)

    assert out["symbol"] == "QQQ"
    assert out["bar_size"] == "1h"
    assert out["outside_rth"] is True
    assert all(len(row) == 6 for row in out["bars"])
    store.close()
