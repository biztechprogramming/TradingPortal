"""Daily bars, and the weekly and monthly charts built from them."""

from __future__ import annotations

import time
from datetime import datetime, timezone

from trading.candles import ET, CandleService, CandleStore, aggregate, day_start
from trading.config import Settings
from trading.models import Bar, TradingMode


def _settings(tmp_path):
    return Settings(mode=TradingMode.PAPER, log_dir=tmp_path / "log",
                    state_dir=tmp_path / "state")


def _day(y, m, d, price=100.0):
    """A daily bar stamped the way IBKR does: the bare date, at UTC midnight."""
    ts = int(datetime(y, m, d, tzinfo=timezone.utc).timestamp())
    return Bar(symbol="QQQ", ts=ts, open=price, high=price + 2, low=price - 2,
               close=price + 1, volume=10)


class DailyBroker:
    name = "fake"

    def __init__(self, bars):
        self.connected = True
        self.bars = bars
        self.calls: list[tuple[float, float, str, bool]] = []

    def connect(self):
        pass

    def get_price_bars(self, symbol, start, end, *, bar_size="1h", outside_rth=True):
        self.calls.append((start, end, bar_size, outside_rth))
        return [b for b in self.bars if start <= b.ts <= end]


def test_day_start_lands_ibkr_and_schwab_stamps_on_the_same_et_midnight():
    ibkr = int(datetime(2024, 3, 15, tzinfo=timezone.utc).timestamp())
    schwab = int(datetime(2024, 3, 15, 5, tzinfo=timezone.utc).timestamp())  # 00:00 CDT
    expected = int(datetime(2024, 3, 15, tzinfo=ET).timestamp())
    assert day_start(ibkr) == day_start(schwab) == expected


def test_weekly_bars_start_on_monday_and_span_the_week():
    days = [_day(2024, 3, d, price=100 + d) for d in (11, 12, 13, 14, 15, 18)]
    days = [Bar(**{**b.__dict__, "ts": day_start(b.ts)}) for b in days]
    weeks = aggregate(days, "week")
    assert len(weeks) == 2
    first = weeks[0]
    assert datetime.fromtimestamp(first.ts, ET).date().isoformat() == "2024-03-11"
    assert first.open == 111 and first.close == 116
    assert first.high == 117 and first.low == 109 and first.volume == 50


def test_monthly_bars_start_on_the_first():
    days = [_day(2024, 2, 28), _day(2024, 2, 29), _day(2024, 3, 1)]
    days = [Bar(**{**b.__dict__, "ts": day_start(b.ts)}) for b in days]
    months = aggregate(days, "month")
    assert [datetime.fromtimestamp(b.ts, ET).date().isoformat() for b in months] \
        == ["2024-02-01", "2024-03-01"]


def test_daily_series_fetches_the_whole_range_in_regular_hours_and_keeps_it(tmp_path):
    store = CandleStore(tmp_path / "c.db")
    service = CandleService(_settings(tmp_path), store)
    broker = DailyBroker([_day(2000, 1, 3), _day(2000, 1, 4), _day(2024, 3, 15)])

    out = service.series([broker], "QQQ", days=20000, bar_size="1d")
    start, _, size, outside_rth = broker.calls[0]
    assert size == "1d" and outside_rth is False
    assert start < time.time() - 19000 * 86400   # not clamped to CANDLE_DAYS
    assert len(out["bars"]) == 3 and out["history_complete"] is True
    assert out["bars"][0][0] == int(datetime(2000, 1, 3, tzinfo=ET).timestamp())

    # Nothing older exists: the head is not asked for again, and nothing is
    # pruned, so the next refresh is a single tail request.
    broker.calls.clear()
    service.series([broker], "QQQ", days=20000, bar_size="1d")
    daily = [c for c in broker.calls if c[2] == "1d"]   # not today's 30min
    assert len(daily) == 1
    assert daily[0][0] == day_start(_day(2024, 3, 15).ts)   # the tail
    assert store.coverage("QQQ", "1d")[2] == 3


def test_weekly_and_monthly_are_built_from_stored_daily_bars(tmp_path):
    store = CandleStore(tmp_path / "c.db")
    service = CandleService(_settings(tmp_path), store)
    broker = DailyBroker([_day(2024, 3, d) for d in (11, 12, 13, 14, 15, 18, 19)])

    weekly = service.series([broker], "QQQ", days=20000, bar_size="1w")
    monthly = service.series([broker], "QQQ", days=20000, bar_size="1mo")
    assert broker.calls[0][2] == "1d"
    assert weekly["bar_size"] == "1w" and len(weekly["bars"]) == 2
    assert monthly["bar_size"] == "1mo" and len(monthly["bars"]) == 1


class IntradayBroker(DailyBroker):
    """Has no candle for today yet, only 30-minute bars."""

    def get_price_bars(self, symbol, start, end, *, bar_size="1h", outside_rth=True):
        self.calls.append((start, end, bar_size, outside_rth))
        return [b for b in self.bars if start <= b.ts <= end] if bar_size == "30min" else []


def test_todays_daily_bar_is_built_from_intraday_until_the_broker_has_one(tmp_path):
    store = CandleStore(tmp_path / "c.db")
    service = CandleService(_settings(tmp_path), store)
    open_ = datetime(2026, 11, 25, 9, 30, tzinfo=ET).timestamp()
    halves = [Bar(symbol="QQQ", ts=int(open_ + i * 1800), open=100 + i, high=105 + i,
                  low=95 - i, close=101 + i, volume=5) for i in range(3)]
    broker = IntradayBroker(halves)
    now = open_ + 3 * 1800

    service._today_from_intraday([broker], "QQQ", now)
    (bar,) = store.bars("QQQ", 0, now, "1d")
    assert bar.ts == day_start(now) and broker.calls[0][2:] == ("30min", False)
    assert (bar.open, bar.high, bar.low, bar.close, bar.volume) == (100, 107, 93, 103, 15)

    # Once the broker's own candle is stored, intraday stops being asked.
    store.upsert([Bar(symbol="QQQ", ts=bar.ts, open=1, high=2, low=0, close=1,
                      volume=1)], source="schwab", bar_size="1d")
    broker.calls.clear()
    service._today_from_intraday([broker], "QQQ", now)
    assert broker.calls == []
    assert store.bars("QQQ", 0, now, "1d")[0].close == 1


def test_no_intraday_request_before_the_open_or_at_weekends(tmp_path):
    service = CandleService(_settings(tmp_path), CandleStore(tmp_path / "c.db"))
    broker = IntradayBroker([])
    service._today_from_intraday(
        [broker], "QQQ", datetime(2026, 11, 25, 9, 0, tzinfo=ET).timestamp())
    service._today_from_intraday(
        [broker], "QQQ", datetime(2026, 11, 28, 12, 0, tzinfo=ET).timestamp())
    assert broker.calls == []
