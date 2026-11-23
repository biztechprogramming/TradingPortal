"""Deep hourly history and 4-hour aggregation."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from trading.candles import CandleService, CandleStore, aggregate
from trading.models import Bar
from tests.conftest import make_settings

ET = ZoneInfo("America/New_York")


def _ts(y, m, d, h):
    return int(datetime(y, m, d, h, tzinfo=ET).timestamp())


def _bar(ts, o, h, l, c, v=10):
    return Bar(symbol="QQQ", ts=ts, open=o, high=h, low=l, close=c, volume=v)


def test_4h_bars_align_to_the_eastern_clock():
    hours = [_bar(_ts(2026, 3, 2, h), 100 + h, 101 + h, 99 + h, 100.5 + h)
             for h in range(4, 12)]
    out = aggregate(hours, 4)
    assert [datetime.fromtimestamp(b.ts, ET).hour for b in out] == [4, 8]
    first = out[0]
    assert first.open == 104 and first.close == 107.5
    assert first.high == 108 and first.low == 103 and first.volume == 40


class _Ibkr:
    name = "ibkr:U1"
    connected = True

    def __init__(self, floor):
        self.floor = floor
        self.calls = []

    def history_chunk(self, symbol, end, *, bar_size="1h", outside_rth=True):
        self.calls.append(end)
        start = max(self.floor, end - 365 * 86400)
        return [_bar(t, 1, 2, 0.5, 1.5) for t in range(int(start), int(end), 86400)]


def test_backfill_walks_back_a_year_at_a_time_then_stops(tmp_path):
    settings = make_settings(tmp_path)
    store = CandleStore(tmp_path / "c.db")
    svc = CandleService(settings, store)
    now = _ts(2026, 11, 1, 10)
    store.upsert([_bar(now, 1, 2, 0.5, 1.5)], bar_size="1h")
    src = _Ibkr(floor=now - 2.5 * 365 * 86400)
    states = [svc.backfill_history([src], "QQQ") for _ in range(6)]
    assert states[:3] == ["more", "more", "more"]
    assert states[3:] == ["done", "done", "done"]
    assert len(src.calls) == 4          # never asks again once complete
    oldest, _, _ = store.coverage("QQQ")
    assert oldest <= src.floor + 86400


def test_backfill_needs_a_history_source(tmp_path):
    svc = CandleService(make_settings(tmp_path), CandleStore(tmp_path / "c.db"))
    assert svc.backfill_history([object()], "QQQ") == "no source"


class _Future(_Ibkr):
    def __init__(self, floor, contract):
        super().__init__(floor)
        self.contract = contract

    def series_version(self, symbol):
        return self.contract if symbol == "NQ" else None

    def history_chunk(self, symbol, end, *, bar_size="1h", outside_rth=True):
        bars = super().history_chunk(symbol, end, bar_size=bar_size)
        return [Bar(symbol="NQ", ts=b.ts, open=b.open, high=b.high, low=b.low,
                    close=b.close, volume=b.volume) for b in bars]


def test_a_futures_roll_restarts_the_series(tmp_path):
    settings = make_settings(tmp_path)
    store = CandleStore(tmp_path / "c.db")
    svc = CandleService(settings, store)
    now = _ts(2026, 11, 1, 10)
    src = _Future(floor=now - 2 * 365 * 86400, contract="NQZ6")
    store.upsert([Bar(symbol="NQ", ts=now, open=1, high=2, low=0.5, close=1.5)])
    assert svc.check_roll([src], "NQ") == "NQZ6"
    assert store.coverage("NQ")[2] == 0            # first sight: clean start
    store.upsert([Bar(symbol="NQ", ts=now, open=1, high=2, low=0.5, close=1.5)])
    while svc.backfill_history([src], "NQ") != "done":
        pass
    assert store.history_complete("NQ")
    assert svc.check_roll([src], "NQ") == "NQZ6"   # same contract: kept
    assert store.coverage("NQ")[2] > 1

    src.contract = "NQH7"                          # the roll
    assert svc.check_roll([src], "NQ") == "NQH7"
    assert store.coverage("NQ")[2] == 0
    assert not store.history_complete("NQ")
    assert store.version("NQ") == "NQH7"


def test_non_futures_have_no_version(tmp_path):
    svc = CandleService(make_settings(tmp_path), CandleStore(tmp_path / "c.db"))
    assert svc.check_roll([_Future(0, "NQZ6")], "QQQ") is None
