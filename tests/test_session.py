"""Session extremes, their timing, and the swings since each one."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from trading.models import Bar, Quote
from trading.session import (
    compute_session_range,
    session_start_epoch,
)
from trading.validation import _EASTERN

HOUR = 3600


def _et(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=_EASTERN)


def _session_bars(specs, day=None):
    """``specs`` is ``[(et_hour, low, high), ...]`` for one trading day."""
    day = day or _et(2026, 8, 25, 12).date()
    out = []
    for hour, low, high in specs:
        ts = int(datetime(day.year, day.month, day.day, hour,
                          tzinfo=_EASTERN).timestamp())
        out.append(Bar("QQQ", ts, open=(low + high) / 2, high=high, low=low,
                       close=(low + high) / 2, volume=100))
    return out


def _now_et(hour):
    return _et(2026, 8, 25, hour).timestamp()


# --- session boundary -------------------------------------------------------


def test_session_starts_at_the_extended_open():
    start = session_start_epoch(_now_et(12), outside_rth=True)
    eastern = datetime.fromtimestamp(start, timezone.utc).astimezone(_EASTERN)
    assert (eastern.hour, eastern.minute) == (4, 0)


def test_before_the_open_we_still_report_the_previous_session():
    """At 02:00 ET the last real session is yesterday's, not an empty today."""
    start = session_start_epoch(_now_et(2), outside_rth=True)
    eastern = datetime.fromtimestamp(start, timezone.utc).astimezone(_EASTERN)
    assert (eastern.hour, eastern.day) == (4, 24)


def test_regular_hours_boundary_is_the_open_bell():
    start = session_start_epoch(_now_et(12), outside_rth=False)
    eastern = datetime.fromtimestamp(start, timezone.utc).astimezone(_EASTERN)
    assert (eastern.hour, eastern.minute) == (9, 30)


# --- extremes and their timing ---------------------------------------------


def test_reports_both_extremes_with_the_hour_each_happened():
    bars = _session_bars([
        (5, 700.0, 702.0),
        (6, 698.0, 701.0),      # low of day
        (7, 699.0, 705.0),      # high of day
        (8, 703.0, 704.0),
    ])
    out = compute_session_range("QQQ", bars, now=_now_et(9))

    assert out.complete
    assert out.day_high == 705.0
    assert out.day_low == 698.0
    high_et = datetime.fromtimestamp(out.day_high_at, timezone.utc).astimezone(_EASTERN)
    low_et = datetime.fromtimestamp(out.day_low_at, timezone.utc).astimezone(_EASTERN)
    assert high_et.hour == 7
    assert low_et.hour == 6
    assert out.resolution_seconds == HOUR


def test_high_since_low_measures_the_run_up_off_the_bottom():
    bars = _session_bars([
        (5, 700.0, 710.0),      # early high, before the low
        (6, 690.0, 695.0),      # low of day
        (7, 692.0, 704.0),      # run up since the low
        (8, 700.0, 702.0),
    ])
    out = compute_session_range("QQQ", bars, now=_now_et(9))

    assert out.day_high == 710.0        # still the session high
    assert out.day_low == 690.0
    # Only bars at or after the low count towards the run-up.
    assert out.high_since_low == 704.0
    assert out.high_since_low != out.day_high


def test_low_since_high_measures_the_pullback_off_the_top():
    bars = _session_bars([
        (5, 690.0, 695.0),      # low of day, first
        (6, 706.0, 712.0),      # high of day
        (7, 705.0, 710.0),
        (8, 702.0, 707.0),      # deepest pullback since the high
    ])
    out = compute_session_range("QQQ", bars, now=_now_et(9))

    assert out.day_low == 690.0
    assert out.day_high == 712.0
    assert out.low_since_high == 702.0
    # The pre-high low is excluded: the pullback is measured off the top only.
    assert out.low_since_high != out.day_low


def test_low_came_first_identifies_which_leg_is_live():
    up = compute_session_range("QQQ", _session_bars([
        (5, 690.0, 695.0), (6, 700.0, 712.0),
    ]), now=_now_et(8))
    assert up.low_came_first is True

    down = compute_session_range("QQQ", _session_bars([
        (5, 700.0, 712.0), (6, 690.0, 695.0),
    ]), now=_now_et(8))
    assert down.low_came_first is False


def test_since_windows_include_the_bar_that_set_the_extreme():
    """The low bar's own high is part of the run-up off that low."""
    bars = _session_bars([(6, 690.0, 699.0), (7, 692.0, 695.0)])
    out = compute_session_range("QQQ", bars, now=_now_et(9))
    assert out.high_since_low == 699.0


# --- interaction with the live quote ---------------------------------------


def _quote(high, low, last=700.0):
    return Quote("QQQ", bid=last - 0.02, ask=last + 0.02, last=last,
                 timestamp=time.time(), day_high=high, day_low=low)


def test_a_live_quote_can_extend_a_bar_extreme():
    """The forming hour is not in a closed bar yet."""
    bars = _session_bars([(5, 700.0, 705.0), (6, 701.0, 704.0)])
    out = compute_session_range("QQQ", bars, now=_now_et(7),
                                quote=_quote(high=709.0, low=698.0))

    assert out.day_high == 709.0
    assert out.day_low == 698.0
    assert out.source == "bars+quote"


def test_a_quote_never_narrows_what_a_bar_already_traded():
    """A bar that printed 705 is a fact; a lagging quote must not shrink it."""
    bars = _session_bars([(5, 700.0, 705.0), (6, 701.0, 704.0)])
    out = compute_session_range("QQQ", bars, now=_now_et(7),
                                quote=_quote(high=702.0, low=703.0))

    assert out.day_high == 705.0
    assert out.day_low == 700.0
    assert out.source == "bars"


def test_falls_back_to_the_quote_when_no_bars_are_cached():
    out = compute_session_range("QQQ", [], now=_now_et(7),
                                quote=_quote(high=709.0, low=698.0))

    assert out.complete
    assert out.day_high == 709.0
    assert out.day_high_at is None, "a quote carries no timing"
    assert out.source == "quote"
    assert "timing is unknown" in out.note


def test_no_bars_and_no_quote_is_reported_not_guessed():
    out = compute_session_range("QQQ", [], now=_now_et(7))
    assert not out.complete
    assert out.day_high == 0.0
    assert out.note


def test_yesterdays_bars_are_excluded():
    today = _session_bars([(6, 700.0, 705.0)])
    yesterday = _session_bars(
        [(6, 600.0, 900.0)], day=_et(2026, 8, 24, 12).date()
    )
    out = compute_session_range("QQQ", yesterday + today, now=_now_et(9))

    assert out.day_high == 705.0, "yesterday's range leaked into today"
    assert out.day_low == 700.0
    assert out.bars_used == 1


def test_serialises_for_the_dashboard():
    bars = _session_bars([(5, 690.0, 695.0), (6, 700.0, 712.0)])
    d = compute_session_range("QQQ", bars, now=_now_et(8)).to_dict()

    for key in ("day_high", "day_high_at", "day_low", "day_low_at",
                "high_since_low", "low_since_high", "low_came_first",
                "resolution_seconds", "complete"):
        assert key in d
    assert d["resolution_seconds"] == HOUR
