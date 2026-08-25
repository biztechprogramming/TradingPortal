"""Parsing of session high/low and historical bars from broker payloads.

The live gateway cannot be reached from the test suite, so these pin the
translation from each broker's wire format onto :class:`Quote` and
:class:`Bar` using representative payloads.
"""

from __future__ import annotations

import time

from trading.brokers.ibkr import F_ASK, F_BID, F_HIGH, F_LAST, F_LOW, _parse_snapshot
from trading.brokers.schwab import _parse_quote


# --- IBKR snapshot ----------------------------------------------------------


def _snapshot(**extra):
    raw = {
        F_LAST: "500.02",
        F_BID: "500.00",
        F_ASK: "500.04",
        "_updated": str(int(time.time() * 1000)),
    }
    raw.update(extra)
    return raw


def test_ibkr_parses_session_high_and_low():
    quote = _parse_snapshot("QQQ", _snapshot(**{F_HIGH: "502.35", F_LOW: "497.80"}))
    assert quote.day_high == 502.35
    assert quote.day_low == 497.80
    assert quote.has_day_range


def test_ibkr_strips_the_prefix_character_from_high_low():
    """IBKR prefixes prices with C (previous close) or H (halted)."""
    quote = _parse_snapshot("QQQ", _snapshot(**{F_HIGH: "C502.35", F_LOW: "C497.80"}))
    assert quote.day_high == 502.35
    assert quote.day_low == 497.80


def test_ibkr_quote_is_still_valid_without_high_low():
    """Before the first trade of a session there is no range -- not an error."""
    quote = _parse_snapshot("QQQ", _snapshot())
    assert quote.bid == 500.00 and quote.ask == 500.04
    assert quote.day_high == 0.0 and quote.day_low == 0.0
    assert not quote.has_day_range


def test_ibkr_bar_parsing(monkeypatch):
    from trading.brokers.ibkr import IBKRBroker
    from trading.config import Settings

    broker = IBKRBroker.__new__(IBKRBroker)
    broker.settings = Settings()
    broker._iserver_primed = True

    now = int(time.time())
    payload = {
        "symbol": "QQQ",
        "data": [
            {"t": (now - 7200) * 1000, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5, "v": 10},
            {"t": (now - 3600) * 1000, "o": 1.5, "h": 2.5, "l": 1.0, "c": 2.0, "v": 20},
            # Outside the requested window: must be trimmed, not returned.
            {"t": (now - 999999) * 1000, "o": 9, "h": 9, "l": 9, "c": 9, "v": 1},
        ],
    }
    monkeypatch.setattr(broker, "tickle", lambda: None)
    monkeypatch.setattr(broker, "resolve_conid", lambda s: 320227571)
    monkeypatch.setattr(broker, "_request", lambda *a, **k: payload)

    bars = broker.get_price_bars("QQQ", now - 10800, now)

    assert [b.ts for b in bars] == [now - 7200, now - 3600]
    assert bars[0].open == 1.0 and bars[0].close == 1.5
    assert bars[1].high == 2.5 and bars[1].volume == 20
    assert all(b.symbol == "QQQ" for b in bars)


def test_ibkr_bars_survive_a_malformed_row(monkeypatch):
    from trading.brokers.ibkr import IBKRBroker
    from trading.config import Settings

    broker = IBKRBroker.__new__(IBKRBroker)
    broker.settings = Settings()
    broker._iserver_primed = True

    now = int(time.time())
    payload = {"data": [
        {"t": "not-a-number", "o": 1, "h": 1, "l": 1, "c": 1},
        {"o": 1, "h": 1, "l": 1, "c": 1},                      # no timestamp
        {"t": (now - 3600) * 1000, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 5},
    ]}
    monkeypatch.setattr(broker, "tickle", lambda: None)
    monkeypatch.setattr(broker, "resolve_conid", lambda s: 1)
    monkeypatch.setattr(broker, "_request", lambda *a, **k: payload)

    bars = broker.get_price_bars("QQQ", now - 10800, now)
    assert len(bars) == 1


# --- Schwab -----------------------------------------------------------------


def test_schwab_parses_session_high_and_low():
    entry = {
        "quote": {
            "bidPrice": 500.00, "askPrice": 500.04, "lastPrice": 500.02,
            "highPrice": 502.35, "lowPrice": 497.80,
            "quoteTime": int(time.time() * 1000),
        },
        "realtime": True,
    }
    quote = _parse_quote("QQQ", entry)
    assert quote.day_high == 502.35
    assert quote.day_low == 497.80
    assert quote.has_day_range


def test_schwab_aggregates_thirty_minute_bars_into_hours(monkeypatch):
    """Schwab's finest bar is 30m, so hourly has to be built from pairs."""
    from trading.brokers.schwab import SchwabBroker
    from trading.config import Settings

    broker = SchwabBroker.__new__(SchwabBroker)
    broker.settings = Settings()

    hour = 1_700_000_000 - (1_700_000_000 % 3600)

    class FakeResponse:
        @staticmethod
        def json():
            return {"candles": [
                {"datetime": hour * 1000,
                 "open": 10, "high": 12, "low": 9, "close": 11, "volume": 100},
                {"datetime": (hour + 1800) * 1000,
                 "open": 11, "high": 15, "low": 8, "close": 14, "volume": 200},
                {"datetime": (hour + 3600) * 1000,
                 "open": 14, "high": 16, "low": 13, "close": 15, "volume": 50},
            ]}

    monkeypatch.setattr(broker, "_request", lambda *a, **k: FakeResponse())

    bars = broker.get_price_bars("QQQ", hour - 10, hour + 7200)

    assert len(bars) == 2
    first = bars[0]
    assert first.ts == hour
    assert first.open == 10          # first half-hour's open
    assert first.close == 14         # second half-hour's close
    assert first.high == 15          # max across both
    assert first.low == 8            # min across both
    assert first.volume == 300       # summed
    assert bars[1].ts == hour + 3600


def test_schwab_declines_non_hourly_bar_sizes(monkeypatch):
    from trading.brokers.schwab import SchwabBroker
    from trading.config import Settings

    broker = SchwabBroker.__new__(SchwabBroker)
    broker.settings = Settings()
    assert broker.get_price_bars("QQQ", 0, 1, bar_size="5m") is None
