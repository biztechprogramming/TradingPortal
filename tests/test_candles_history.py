"""Deep hourly history and 4-hour aggregation."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from trading.candles import CandleService, CandleStore, aggregate, day_start
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


def test_first_sight_of_a_future_starts_clean(tmp_path):
    store = CandleStore(tmp_path / "c.db")
    svc = CandleService(make_settings(tmp_path), store)
    store.upsert([Bar(symbol="NQ", ts=_ts(2026, 11, 1, 10), open=1, high=2,
                      low=0.5, close=1.5)])
    assert svc.check_roll([_Future(0, "NQZ6")], "NQ") == "NQZ6"
    assert store.coverage("NQ")[2] == 0
    assert store.version("NQ") == "NQZ6"


DAY = 86400


def _nq(ts, close):
    return Bar(symbol="NQ", ts=int(ts), open=close - 1, high=close + 2,
               low=close - 2, close=close, volume=7)


class _Rolled:
    """IBKR after a roll: four years of bars, all `gap` higher than before."""

    name = "ibkr:U1"
    connected = True

    def __init__(self, now, gap, contract="NQH7"):
        self.now, self.gap, self.contract = now, gap, contract
        self.calls = []

    def series_version(self, symbol):
        return self.contract

    def _series(self, step):
        start = self.now - 4 * 365 * DAY
        return [_nq(t, 1000 + (t - start) / DAY + self.gap)
                for t in range(int(start), int(self.now), step)]

    def history_chunk(self, symbol, end, *, bar_size="1h", outside_rth=True):
        self.calls.append("history_chunk")
        return self._series(DAY)          # one "hourly" bar a day keeps it small

    def get_price_bars(self, symbol, start, end, *, bar_size="1h", outside_rth=True):
        self.calls.append(f"bars {bar_size}")
        return self._series(DAY) if bar_size == "1d" else []


def _old_nq(store, now):
    """Six years of NQ on the old contract, hourly and daily."""
    start = now - 4 * 365 * DAY
    old = [_nq(t, 1000 + (t - start) / DAY)
           for t in range(int(now - 6 * 365 * DAY), int(now), DAY)]
    store.upsert(old, bar_size="1h")
    store.upsert([Bar(**{**b.__dict__, "ts": day_start(b.ts)}) for b in old],
                 bar_size="1d")
    store.mark_history_complete("NQ", "1h")
    store.set_version("NQ", "NQZ6")
    return old


def test_a_roll_keeps_all_history_on_the_new_contracts_scale(tmp_path):
    import time
    store = CandleStore(tmp_path / "c.db")
    svc = CandleService(make_settings(tmp_path), store)
    now = time.time()
    old = _old_nq(store, now)
    src = _Rolled(now, gap=50)

    # The dashboard's refresh sees the roll but leaves the cache alone.
    meta = svc.refresh([src], "NQ", bar_size="1h")
    assert meta["reason"] == "roll pending" and "NQH7" in meta["errors"][0]
    assert src.calls == [] and store.version("NQ") == "NQZ6"

    # The backfill thread realigns: nothing is lost, everything moves +50.
    svc.backfill_history([src], "NQ")
    assert store.version("NQ") == "NQH7"
    for size in ("1h", "1d"):
        bars = store.bars("NQ", 0, now + DAY, size)
        assert len(bars) == len(old)
        assert bars[0].close == old[0].close + 50
        assert bars[-1].close == old[-1].close + 50

    # Settled: later refreshes fetch normally again.
    assert svc.refresh([src], "NQ", bar_size="1h")["reason"] != "roll pending"


def test_a_roll_that_cannot_be_lined_up_changes_nothing(tmp_path):
    import time
    import pytest
    from trading.rolls import RollMismatch
    store = CandleStore(tmp_path / "c.db")
    svc = CandleService(make_settings(tmp_path), store)
    now = time.time()
    old = _old_nq(store, now)
    src = _Rolled(now + 10 * 365 * DAY, gap=50)   # no overlap at all
    with pytest.raises(RollMismatch):
        svc.backfill_history([src], "NQ")
    assert store.version("NQ") == "NQZ6"
    assert [b.close for b in store.bars("NQ", 0, now + DAY, "1h")] == [b.close for b in old]


def test_roll_adjustment_tells_a_gap_from_a_ratio():
    from trading.rolls import roll_adjustment
    old = [_nq(i * DAY, 100 + 10 * i) for i in range(10)]
    added = [_nq(b.ts, b.close + 25) for b in old]
    scaled = [_nq(b.ts, b.close * 1.02) for b in old]
    assert roll_adjustment(old, added) == ("add", 25)
    kind, k = roll_adjustment(old, scaled)
    assert kind == "mul" and abs(k - 1.02) < 1e-9


def test_non_futures_have_no_version(tmp_path):
    svc = CandleService(make_settings(tmp_path), CandleStore(tmp_path / "c.db"))
    assert svc.check_roll([_Future(0, "NQZ6")], "QQQ") is None
