"""NAV history, time-weighted returns, and historical reconstruction."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from trading.backfill import Backfiller, PriceLookup, reconstruct_nav_series
from trading.models import Command, Transaction
from trading.orchestrator import TradingService
from trading.performance import (
    SOURCE_BROKER,
    SOURCE_RECONSTRUCTED,
    SOURCE_SNAPSHOT,
    NavLedger,
    NavPoint,
    combine_series,
    compute_window,
    months_ago,
    time_weighted_return,
)
from tests.conftest import make_broker, make_settings


def d(day: str) -> date:
    return date.fromisoformat(day)


def pt(day: str, nav: float, flow: float = 0.0, source: str = SOURCE_SNAPSHOT):
    return NavPoint(date=day, nav=nav, cash=0.0, positions_value=nav,
                    external_flow=flow, source=source)


# --- calendar arithmetic -----------------------------------------------------


@pytest.mark.parametrize("anchor,months,expected", [
    ("2026-08-15", 1, "2026-07-15"),
    ("2026-08-15", 6, "2026-02-15"),
    ("2026-01-15", 1, "2025-12-15"),
    ("2026-01-15", 3, "2025-10-15"),
    # Month-end must clamp rather than overflow into the next month.
    ("2026-03-31", 1, "2026-02-28"),
    ("2026-05-31", 3, "2026-02-28"),
    ("2024-03-31", 1, "2024-02-29"),          # leap year
])
def test_months_ago(anchor, months, expected):
    assert months_ago(d(anchor), months) == d(expected)


# --- the ledger --------------------------------------------------------------


def test_ledger_round_trip(tmp_path):
    ledger = NavLedger(tmp_path / "nav.jsonl")
    ledger.record("schwab:1111", pt("2026-08-01", 10_000))
    ledger.record("schwab:1111", pt("2026-08-02", 10_100))
    ledger.record("ibkr:DU1", pt("2026-08-01", 5_000))

    series = ledger.series("schwab:1111")
    assert [p.date for p in series] == ["2026-08-01", "2026-08-02"]
    assert ledger.series("ibkr:DU1")[0].nav == 5_000
    assert ledger.brokers() == ["ibkr:DU1", "schwab:1111"]


def test_ledger_sorts_regardless_of_write_order(tmp_path):
    ledger = NavLedger(tmp_path / "nav.jsonl")
    for day in ("2026-08-03", "2026-08-01", "2026-08-02"):
        ledger.record("a", pt(day, 100))
    assert [p.date for p in ledger.series("a")] == [
        "2026-08-01", "2026-08-02", "2026-08-03",
    ]


def test_a_measured_snapshot_beats_a_reconstructed_one(tmp_path):
    """Observed history must never be overwritten by an estimate."""
    ledger = NavLedger(tmp_path / "nav.jsonl")
    ledger.record("a", pt("2026-08-01", 10_000, source=SOURCE_SNAPSHOT))
    ledger.record("a", pt("2026-08-01", 9_000, source=SOURCE_RECONSTRUCTED))

    series = ledger.series("a")
    assert len(series) == 1
    assert series[0].nav == 10_000
    assert series[0].source == SOURCE_SNAPSHOT


def test_later_write_wins_within_the_same_source(tmp_path):
    ledger = NavLedger(tmp_path / "nav.jsonl")
    ledger.record("a", pt("2026-08-01", 10_000))
    ledger.record("a", pt("2026-08-01", 10_500))
    assert ledger.series("a")[0].nav == 10_500


def test_fill_gaps_never_overwrites(tmp_path):
    ledger = NavLedger(tmp_path / "nav.jsonl")
    ledger.record("a", pt("2026-08-02", 10_000))
    written = ledger.fill_gaps("a", [
        pt("2026-08-01", 9_900, source=SOURCE_RECONSTRUCTED),
        pt("2026-08-02", 1, source=SOURCE_RECONSTRUCTED),   # already present
    ])
    assert written == 1
    assert [p.nav for p in ledger.series("a")] == [9_900, 10_000]


def test_torn_line_does_not_lose_the_rest(tmp_path):
    path = tmp_path / "nav.jsonl"
    ledger = NavLedger(path)
    ledger.record("a", pt("2026-08-01", 10_000))
    with open(path, "a") as fh:
        fh.write('{"broker": "a", "date": "2026-08-0')     # crash mid-write
    ledger.record("a", pt("2026-08-03", 10_200))

    assert [p.date for p in ledger.series("a")] == ["2026-08-01", "2026-08-03"]


# --- time-weighted return ----------------------------------------------------


def test_simple_growth():
    twr = time_weighted_return([pt("2026-08-01", 100), pt("2026-08-02", 110)])
    assert twr == pytest.approx(10.0)


def test_compounding_links_across_days():
    twr = time_weighted_return([
        pt("2026-08-01", 100), pt("2026-08-02", 110), pt("2026-08-03", 121),
    ])
    assert twr == pytest.approx(21.0)


def test_a_deposit_is_not_performance():
    """The whole reason for TWR: $10k wired in is not a 100% month."""
    series = [pt("2026-08-01", 10_000), pt("2026-08-02", 20_000, flow=10_000)]
    assert time_weighted_return(series) == pytest.approx(0.0)


def test_a_withdrawal_is_not_a_loss():
    series = [pt("2026-08-01", 20_000), pt("2026-08-02", 10_000, flow=-10_000)]
    assert time_weighted_return(series) == pytest.approx(0.0)


def test_growth_alongside_a_deposit_is_isolated():
    # Start 10k, deposit 5k, end 16.5k -> the investments made 10%.
    series = [pt("2026-08-01", 10_000), pt("2026-08-02", 16_500, flow=5_000)]
    assert time_weighted_return(series) == pytest.approx(15.0)


def test_losses_are_negative():
    assert time_weighted_return(
        [pt("2026-08-01", 100), pt("2026-08-02", 90)]
    ) == pytest.approx(-10.0)


def test_insufficient_data_returns_none():
    assert time_weighted_return([]) is None
    assert time_weighted_return([pt("2026-08-01", 100)]) is None


def test_zero_nav_does_not_divide_by_zero():
    series = [pt("2026-08-01", 0), pt("2026-08-02", 100, flow=100),
              pt("2026-08-03", 110)]
    twr = time_weighted_return(series)
    assert twr == pytest.approx(10.0)


# --- windows -----------------------------------------------------------------


def test_complete_window_is_anchored_before_the_cutoff():
    anchor = d("2026-08-15")
    series = [pt("2026-06-01", 100), pt("2026-07-15", 110), pt("2026-08-15", 121)]
    window = compute_window(series, "1M", 1, anchor)

    assert window.complete
    assert window.start_date == "2026-07-15"
    assert window.twr_pct == pytest.approx(10.0)


def test_incomplete_window_says_so():
    anchor = d("2026-08-15")
    series = [pt("2026-08-01", 100), pt("2026-08-15", 110)]
    window = compute_window(series, "6M", 6, anchor)

    assert not window.complete
    assert window.twr_pct == pytest.approx(10.0)     # still reports what it has
    assert "partial period" in window.note
    assert window.covered_days == 14
    assert window.requested_days > 150


def test_empty_history_window():
    window = compute_window([], "3M", 3, d("2026-08-15"))
    assert window.twr_pct is None
    assert not window.complete
    assert "no history" in window.note


def test_single_point_window():
    window = compute_window([pt("2026-08-15", 100)], "1M", 1, d("2026-08-15"))
    assert window.twr_pct is None
    assert "not enough history" in window.note


def test_reconstructed_history_is_flagged():
    anchor = d("2026-08-15")
    series = [pt("2026-06-01", 100, source=SOURCE_RECONSTRUCTED),
              pt("2026-07-01", 105, source=SOURCE_RECONSTRUCTED),
              pt("2026-08-15", 110)]
    window = compute_window(series, "1M", 1, anchor)
    assert window.complete
    window6 = compute_window(series, "6M", 6, anchor)
    assert SOURCE_RECONSTRUCTED in window6.sources
    assert "verify against your broker statement" in window6.note


def test_net_flows_are_reported():
    anchor = d("2026-08-15")
    series = [pt("2026-07-01", 10_000), pt("2026-08-15", 16_500, flow=5_000)]
    window = compute_window(series, "3M", 3, anchor)
    assert window.net_flows == 5_000
    assert window.change_dollars == pytest.approx(1_500)


# --- combining accounts ------------------------------------------------------


def test_combine_sums_shared_dates():
    combined = combine_series({
        "a": [pt("2026-08-01", 100), pt("2026-08-02", 110)],
        "b": [pt("2026-08-01", 200), pt("2026-08-02", 190)],
    })
    assert [(p.date, p.nav) for p in combined] == [
        ("2026-08-01", 300), ("2026-08-02", 300),
    ]


def test_combine_skips_dates_missing_from_an_account():
    """Summing a partial date would show a fake overnight collapse."""
    combined = combine_series({
        "a": [pt("2026-08-01", 100), pt("2026-08-02", 110)],
        "b": [pt("2026-08-02", 200)],
    })
    assert [p.date for p in combined] == ["2026-08-02"]


def test_combine_handles_one_and_zero_accounts():
    assert combine_series({}) == []
    assert combine_series({"a": []}) == []
    single = combine_series({"a": [pt("2026-08-01", 100)], "b": []})
    assert [p.nav for p in single] == [100]


def test_combined_flows_are_summed():
    combined = combine_series({
        "a": [pt("2026-08-01", 100), pt("2026-08-02", 150, flow=50)],
        "b": [pt("2026-08-01", 100), pt("2026-08-02", 100)],
    })
    assert combined[-1].external_flow == 50


# --- price lookup ------------------------------------------------------------


def test_price_lookup_carries_forward_over_weekends():
    prices = PriceLookup()
    prices.add("QQQ", {"2026-08-07": 500.0, "2026-08-10": 505.0})

    assert prices.close_on("QQQ", d("2026-08-07")) == 500.0
    assert prices.close_on("QQQ", d("2026-08-08")) == 500.0   # Saturday
    assert prices.close_on("QQQ", d("2026-08-10")) == 505.0
    assert prices.close_on("QQQ", d("2026-08-06")) is None    # before the data
    assert prices.close_on("SPY", d("2026-08-07")) is None


# --- reconstruction ----------------------------------------------------------


def test_reconstruct_with_no_activity_is_pure_marking():
    prices = PriceLookup()
    prices.add("QQQ", {"2026-08-01": 100.0, "2026-08-02": 110.0,
                       "2026-08-03": 121.0})
    series = reconstruct_nav_series(
        current_positions={"QQQ": 10},
        current_cash=1_000,
        transactions=[],
        prices=prices,
        start=d("2026-08-01"),
        end=d("2026-08-03"),
    )
    assert [(p.date, p.nav) for p in series] == [
        ("2026-08-01", 2_000.0), ("2026-08-02", 2_100.0), ("2026-08-03", 2_210.0),
    ]
    assert all(p.source == SOURCE_RECONSTRUCTED for p in series)


def test_reconstruct_unwinds_a_purchase():
    """Yesterday we held cash; today we hold shares. Both must value correctly."""
    prices = PriceLookup()
    prices.add("QQQ", {"2026-08-01": 100.0, "2026-08-02": 100.0})
    buy = Transaction(date="2026-08-02", type="TRADE", symbol="QQQ",
                      quantity=10, price=100.0, net_amount=-1_000.0)

    series = reconstruct_nav_series(
        current_positions={"QQQ": 10},
        current_cash=0.0,
        transactions=[buy],
        prices=prices,
        start=d("2026-08-01"),
        end=d("2026-08-02"),
    )
    by_date = {p.date: p for p in series}
    assert by_date["2026-08-02"].nav == 1_000.0        # 10 shares at 100
    assert by_date["2026-08-01"].nav == 1_000.0        # the cash before it
    assert by_date["2026-08-01"].positions_value == 0.0
    # A trade moves value between cash and shares; it is not a flow.
    assert all(p.external_flow == 0.0 for p in series)


def test_reconstruct_records_a_deposit_as_a_flow():
    prices = PriceLookup()
    prices.add("QQQ", {"2026-08-01": 100.0, "2026-08-02": 100.0})
    deposit = Transaction(date="2026-08-02", type="TRANSFER", net_amount=5_000.0,
                          external_flow=5_000.0)

    series = reconstruct_nav_series(
        current_positions={},
        current_cash=15_000.0,
        transactions=[deposit],
        prices=prices,
        start=d("2026-08-01"),
        end=d("2026-08-02"),
    )
    by_date = {p.date: p for p in series}
    assert by_date["2026-08-02"].nav == 15_000.0
    assert by_date["2026-08-01"].nav == 10_000.0
    assert by_date["2026-08-02"].external_flow == 5_000.0
    # And the deposit must not read as performance.
    assert time_weighted_return(series) == pytest.approx(0.0)


def test_reconstruct_handles_a_position_opened_mid_window():
    prices = PriceLookup()
    prices.add("PSQ", {"2026-08-01": 30.0, "2026-08-02": 32.0,
                       "2026-08-03": 33.0})
    buy = Transaction(date="2026-08-02", type="TRADE", symbol="PSQ",
                      quantity=100, price=32.0, net_amount=-3_200.0)

    series = reconstruct_nav_series(
        current_positions={"PSQ": 100},
        current_cash=800.0,
        transactions=[buy],
        prices=prices,
        start=d("2026-08-01"),
        end=d("2026-08-03"),
    )
    by_date = {p.date: p for p in series}
    assert by_date["2026-08-03"].nav == pytest.approx(4_100.0)   # 3300 + 800
    assert by_date["2026-08-01"].nav == pytest.approx(4_000.0)   # all cash


# --- backfiller --------------------------------------------------------------


def test_backfiller_prefers_broker_supplied_history(tmp_path):
    s = make_settings(tmp_path)
    broker = make_broker(s, "ibkr")
    supplied = [pt("2026-08-01", 100, source=SOURCE_BROKER),
                pt("2026-08-02", 110, source=SOURCE_BROKER)]
    broker.get_broker_performance = lambda months=6: supplied
    broker.get_transactions = lambda start, end: (_ for _ in ()).throw(
        AssertionError("must not reconstruct when the broker answers")
    )

    ledger = NavLedger(tmp_path / "nav.jsonl")
    written = Backfiller(s, ledger).backfill(broker)

    assert written == 2
    assert [p.source for p in ledger.series("ibkr")] == [SOURCE_BROKER] * 2


def test_backfiller_runs_once_per_process(tmp_path):
    s = make_settings(tmp_path)
    broker = make_broker(s, "ibkr")
    calls = {"n": 0}

    def once(months=6):
        calls["n"] += 1
        return [pt("2026-08-01", 100, source=SOURCE_BROKER)]

    broker.get_broker_performance = once
    filler = Backfiller(s, NavLedger(tmp_path / "nav.jsonl"))
    filler.backfill(broker)
    filler.backfill(broker)
    assert calls["n"] == 1        # IBKR rate limits this hard


def test_backfiller_survives_a_broker_that_cannot_help(tmp_path):
    s = make_settings(tmp_path)
    broker = make_broker(s, "schwab")     # simulator supports neither call
    written = Backfiller(s, NavLedger(tmp_path / "nav.jsonl")).backfill(broker)
    assert written == 0


def test_backfiller_swallows_broker_errors(tmp_path):
    s = make_settings(tmp_path)
    broker = make_broker(s, "schwab")

    def boom(months=6):
        raise RuntimeError("rate limited")

    broker.get_broker_performance = boom
    assert Backfiller(s, NavLedger(tmp_path / "nav.jsonl")).backfill(broker) == 0


# --- end-to-end through the service -----------------------------------------


def _service(tmp_path):
    s = make_settings(tmp_path)
    brokers = {
        "schwab:1111": make_broker(s, "schwab:1111", cash=10_000),
        "ibkr:DU1": make_broker(s, "ibkr:DU1", cash=5_000),
    }
    for name, broker in brokers.items():
        broker.broker_type = name.split(":")[0]
    return TradingService(s, brokers=brokers)


def test_status_records_a_daily_nav_point(tmp_path):
    svc = _service(tmp_path)
    svc.status()

    for name in svc.brokers:
        series = svc.nav.series(name)
        assert len(series) == 1
        assert series[0].source == SOURCE_SNAPSHOT
    assert svc.nav.series("schwab:1111")[0].nav == pytest.approx(10_000)


def test_repeated_status_does_not_duplicate_the_day(tmp_path):
    svc = _service(tmp_path)
    for _ in range(4):
        svc.status()
    assert len(svc.nav.series("ibkr:DU1")) == 1


def test_nav_tracks_value_not_just_cash(tmp_path):
    """After buying, NAV must stay put: cash became shares."""
    svc = _service(tmp_path)
    svc.status()
    before = svc.nav.series("schwab:1111")[-1].nav

    svc.execute(Command.GO_LONG)
    svc._nav_recorded.clear()             # allow a second point the same day
    svc.status()
    after = svc.nav.series("schwab:1111")[-1].nav

    assert after == pytest.approx(before, rel=0.01)


def test_performance_report_shape(tmp_path):
    svc = _service(tmp_path)
    svc.status()
    report = svc.performance(backfill=False)

    assert report["windows"] == ["1M", "2M", "3M", "6M"]
    assert {a["broker"] for a in report["accounts"]} == set(svc.brokers)
    assert {g["broker_type"] for g in report["by_broker"]} == {"schwab", "ibkr"}
    assert report["combined"]["current_nav"] == pytest.approx(15_000)

    # One day of history cannot answer a one-month question, and says so.
    for window in report["combined"]["windows"]:
        assert window["twr_pct"] is None
        assert not window["complete"]


def test_performance_reports_returns_once_history_exists(tmp_path):
    svc = _service(tmp_path)
    anchor = date.today()
    # One point well before the 1M cutoff, one exactly on it, one today.
    days = [
        ((anchor - timedelta(days=40)).isoformat(), 10_000),
        (months_ago(anchor, 1).isoformat(), 10_500),
        (anchor.isoformat(), 11_000),
    ]
    for day, nav in days:
        svc.nav.record("schwab:1111", pt(day, nav))
        svc.nav.record("ibkr:DU1", pt(day, nav / 2))

    report = svc.performance(backfill=False)
    account = next(a for a in report["accounts"] if a["broker"] == "schwab:1111")
    one_month = next(w for w in account["windows"] if w["label"] == "1M")

    # Anchored on the observation that sits exactly on the cutoff.
    assert one_month["complete"]
    assert one_month["start_nav"] == 10_500
    assert one_month["twr_pct"] == pytest.approx(4.7619, abs=0.01)
    assert account["current_nav"] == pytest.approx(11_000)

    # Three months of history do not exist, so the window is partial but
    # still reports what it can measure: 10,000 -> 11,000.
    three_month = next(w for w in account["windows"] if w["label"] == "3M")
    assert not three_month["complete"]
    assert three_month["twr_pct"] == pytest.approx(10.0)
