# Holdings and performance

The dashboard reports, for every account and for the portfolio as a whole:

- current holdings — **every** position, not just QQQ and PSQ
- cash, buying power, and total account value
- **time-weighted returns over 1, 2, 3 and 6 months**
- account value over time, charted

`./qqq performance`, `GET /api/performance`, and the
Performance panel on the dashboard all render the same data.

---

## Where the numbers come from

Neither broker hands over a trustworthy "return over the last N months" in a
uniform way, so this is built in three layers.

### 1. The NAV ledger — exact, but only from installation onward

Every time the dashboard or CLI reads an account, the system records one point
per account per day:

```
NAV = cash available + Σ (position quantity × market price)
```

Stored append-only in `var/state/nav_history.jsonl`, one JSON object per line,
`fsync`'d on write. This layer is exact, needs no broker support, and works
identically for Schwab and IBKR.

Its limitation is obvious: **on day one it is empty**, so it cannot answer a
6-month question yet.

### 2. Broker-supplied history — used when it exists

**IBKR** publishes its own Portfolio Analyst NAV series
(`POST /pa/performance`), which is IBKR's own arithmetic and therefore the best
available answer. Two caveats shaped the implementation:

- IBKR rate limits it to roughly **one request per 15 minutes** because the
  calculation is expensive, so the result is cached and backfill runs at most
  once per account per process.
- The response schema is not fully documented publicly. The parser is
  defensive: if the shape does not match what it expects, it logs and returns
  "unavailable" so the next layer takes over, rather than raising or — far
  worse — inventing numbers.

**Schwab** publishes no performance endpoint at all.

### 3. Reconstruction — Schwab's fallback

Schwab does expose ~180 days of transactions and daily price history, which is
enough to rebuild the series. The algorithm rolls the portfolio **backwards**:

1. Start from what the account holds *now* — the one state we know exactly.
2. Pull transactions for the window and group them by date.
3. Walk backwards day by day. For each day, value the current holdings at that
   day's official close, then undo the day's transactions to get the previous
   day's holdings and cash.
4. Record deposits, withdrawals and transfers as external flows.

Backwards is the only workable direction: forwards would require knowing the
starting state, which is exactly what we lack.

Reconstruction is **approximate**. It values at official daily closes and
assumes the transaction feed is complete. Every point it produces is tagged
`reconstructed`, the dashboard says so, and you should check it against your
statements before relying on it.

### Precedence

A measured snapshot always beats a reconstructed one for the same date —
observed history is never overwritten by an estimate. Backfill only fills dates
the ledger does not already have.

---

## Why time-weighted return

Because a deposit is not performance.

Wire $10,000 into a $10,000 account and a naive `(end − start) / start` reports
a triumphant +100% month. Time-weighted return removes external cash flows so
the number reflects how the *investments* did:

```
r_i = (NAV_i − external_flow_i) / NAV_(i−1) − 1
TWR = Π (1 + r_i) − 1
```

A trade contributes **zero** external flow — buying QQQ moves value from cash
into shares without changing account value. Only money crossing the account
boundary counts: ACH, wires, journals, and securities transferred in or out.

Alongside the TWR each window also reports **net flows** and the **dollar
change excluding flows**, so you can see both what happened and why.

This is the same convention IBKR uses for its Cumulative Performance Series,
which keeps the two sources comparable.

---

## Reading the windows

Windows are **calendar** months back from today, with month-end handled
properly (31 March minus one month is 28 February, not 3 March).

Each window is anchored on the account value **as of the cutoff date**:

- if an observation lands exactly on the cutoff, that is the anchor;
- otherwise the last observation *before* it is used, as the best available
  estimate of the value on that day.

Anchoring on the first point *inside* the window would silently shorten the
period being reported — a "1 month" figure that actually covered 20 days.

### `*` means partial

A window marked `*` (dashboard and CLI) means history does not reach back far
enough to cover it. The figure shown is real but covers a shorter span than the
label implies — `covered_days` versus `requested_days` in the JSON tells you
how much shorter. Expect asterisks on the longer windows until either backfill
succeeds or enough days accrue.

---

## The chart

One line per broker family plus the combined portfolio, capped at three series.

Accounts differ enormously in size, so every series is **indexed to 100 at the
start of the window**. That puts them on a single shared axis — a second y-axis
would let two unrelated scales imply crossings and gaps that do not exist.
Hover for a crosshair with the indexed value and the actual dollar NAV on that
date.

Series colours are the validated categorical slots for this dark surface. They
were checked with a palette validator for lightness band, chroma floor,
colour-vision-deficiency separation, normal-vision separation and contrast — on
the all-pairs list, not just adjacent pairs. Every line is also directly
labelled, so identity never depends on colour alone. Do not substitute colours
by eye.

---

## Interpreting it honestly

- **These are account returns, not strategy returns.** A 6-month window mostly
  reflects whatever you were doing before this system existed.
- **Reconstructed history is an estimate.** Cross-check against statements.
- **Schwab's transaction window is ~180 days**, so reconstruction cannot reach
  further back than about 6 months.
- **Returns are gross of taxes** and of any fees the broker does not report as
  transactions.
- **Combined figures use only dates where every account has a point.** Summing
  a date where one account is missing would show a fake overnight collapse.

---

## Files and endpoints

| Thing | Where |
|---|---|
| NAV ledger | `var/state/nav_history.jsonl` |
| Maths and windows | `src/trading/performance.py` |
| Reconstruction | `src/trading/backfill.py` |
| API | `GET /api/performance?months=6&backfill=true` |
| CLI | `./qqq performance [--json] [--no-backfill]` |
| Tests | `tests/test_performance.py` (45 cases) |

To rebuild history from scratch, delete `nav_history.jsonl` and run
`cli performance` — backfill will re-derive what the brokers can supply. Your
measured snapshots are lost by doing that, so keep a copy first.
