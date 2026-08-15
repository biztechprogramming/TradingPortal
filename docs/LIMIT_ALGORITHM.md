# The limit-price algorithm

The system never sends a market order. Every order is a **marketable limit**:
priced through the touch so it executes essentially immediately, but with a
hard ceiling (buys) or floor (sells) that is fixed *before* the order is sent
and is never relaxed afterwards.

Implementation: `src/trading/pricing.py`. Tests: `tests/test_pricing.py`.

---

## 1. Getting a usable quote

Before any price is computed, the quote must pass `validate_quote()`. Any
failure aborts the whole command — no order is sent.

| Check | Rejects when | Config |
|---|---|---|
| Halted | the security is halted | — |
| Complete | bid or ask is missing, zero, negative, or not finite | — |
| Not crossed | `bid > ask` | — |
| Fresh | `now - quote_time > MAX_QUOTE_AGE_SECONDS` | default 10 s |
| Clock sane | quote timestamp is >30 s in the future | — |
| Tight enough | `spread_bps > MAX_SPREAD_BPS` | default 75 bps |

Two design decisions worth calling out:

- **Freshness is measured against the broker's own quote timestamp**, not
  against when we received the response. Both adapters *refuse to synthesise a
  timestamp* if the broker omits one (Schwab `quoteTime`, IBKR `_updated`) —
  falling back to "now" would make every stale quote look fresh and silently
  defeat this check.
- **The spread guard exists because a marketable limit is at its worst when
  the book is thin.** Crossing a 5-cent spread costs 1 bp on QQQ; crossing a
  wide, illiquid spread can cost far more than the trade is worth.

## 2. The anchor

When a leg begins, the system records an **anchor price**:

- buys: the ask at that moment
- sells: the bid at that moment

Every subsequent guard calculation is relative to the anchor, **not** to the
latest quote. This is the single most important detail in the algorithm.

> If the guard were recomputed from the current quote on each reprice, each
> chase would re-baseline against the price it just chased to, and "maximum
> 50 bps of slippage" would become unbounded drift over the life of the
> order. Anchoring makes the worst case a fixed number decided up front.

## 3. The limit price

For attempt *n* (0 = the initial submission):

```
offset_bps = min(BASE_SLIPPAGE_BPS + n * REPRICE_STEP_BPS, MAX_SLIPPAGE_BPS)

BUY   raw   = ask * (1 + offset_bps / 10_000)      rounded UP   to TICK_SIZE
      guard = anchor * (1 + MAX_SLIPPAGE_BPS / 10_000)

SELL  raw   = bid * (1 - offset_bps / 10_000)      rounded DOWN to TICK_SIZE
      guard = anchor * (1 - MAX_SLIPPAGE_BPS / 10_000)
```

Then:

1. **Abort check.** If the current ask is already above the buy guard (or the
   current bid below the sell guard), raise `MaxSlippageExceeded`. The market
   has moved past the worst price declared acceptable, so we stop rather than
   chase. Any shares already filled are reported.
2. **Clamp.** If the computed limit crosses the guard, it is clamped to the
   guard.
3. **Stay marketable.** Clamping rounds toward the guard, which can land the
   limit inside the spread and leave a resting order. So after clamping, a buy
   limit is floored at the ask and a sell limit is capped at the bid.

Rounding direction is deliberate: rounding a buy limit *down* to the tick could
place it one cent below the offer, converting an intended immediate fill into
a resting order.

### Worked example

QQQ bid 500.00 / ask 500.04, `BUY_SLIPPAGE_BPS=10`, `MAX_SLIPPAGE_BPS=50`,
`REPRICE_STEP_BPS=5`.

| Attempt | Offset | Limit | Guard (anchor 500.04) |
|---|---|---|---|
| 0 | 10 bps | 500.55 | 502.54 |
| 1 | 15 bps | ask × 1.0015, ≤ 502.54 | 502.54 |
| 2 | 20 bps | ask × 1.0020, ≤ 502.54 | 502.54 |
| … | … | … | 502.54 |

The limit gets more aggressive each time, but 502.54 is the worst price
possible for this leg no matter how long it runs or how many reprices occur.

## 4. Monitoring, repricing, and partial fills

The order loop (`execution.py`) polls every `ORDER_POLL_INTERVAL_SECONDS`:

- **Filled** → done.
- **Rejected** → abort with the broker's message.
- **Cancelled/expired by someone else** → if quantity remains and time remains,
  resubmit the remainder; otherwise stop and report.
- **Every `REPRICE_INTERVAL_SECONDS`**, up to `MAX_REPRICE_ATTEMPTS`: re-quote,
  recompute at the next attempt level, and cancel/replace **only if the new
  price is at least one full tick more aggressive** than the working one.
  Replacing costs a round trip and surrenders queue priority, so a sub-tick
  improvement is not worth taking.
- **A bad quote during a reprice** (stale, wide, crossed) does *not* abandon the
  working order. The reprice is skipped, noted, and retried next interval —
  the order is already live and at a price we approved.
- **`ORDER_FILL_TIMEOUT_SECONDS` reached** → cancel, confirm the cancel, and
  report what filled.

**Partial fills** are tracked cumulatively across every order id the leg uses,
because a cancel/replace usually produces a new id at both brokers. Filled
quantity and volume-weighted average price are aggregated per id and summed, so
a partially-filled-then-replaced order is never double-counted, and the
replacement is submitted only for the quantity still outstanding.

### Repricing a buy cannot overspend

A leg carries a dollar **budget** (the quantity sized × the initial limit). A
more aggressive reprice means the remaining shares cost more, which could
exceed the budget. When that happens the system does **not** submit the order
and hope:

```
affordable = (budget - already_spent) // new_limit_price
```

- `affordable >= remaining` → plain cancel/replace at the new price.
- `0 < affordable < remaining` → cancel, confirm, and resubmit the *smaller*
  quantity. The leg's target shrinks and a note records it, so the outcome is
  reported as a completed-but-smaller entry rather than a spurious failure.
- `affordable == 0` → leave the working order untouched at its current price.

## 5. Tuning

| Situation | Change |
|---|---|
| Fills are slow | raise `BUY_SLIPPAGE_BPS` / `SELL_SLIPPAGE_BPS` to 15–25 |
| Fills are worse than expected | lower `MAX_SLIPPAGE_BPS` to 20–30 |
| Aborts near the open/close | raise `MAX_SPREAD_BPS`, or avoid the first/last minutes |
| Too many cancel/replaces | raise `REPRICE_INTERVAL_SECONDS` or lower `MAX_REPRICE_ATTEMPTS` |
| Illiquid symbol swapped in | lower `MAX_SPREAD_BPS`, raise `ORDER_FILL_TIMEOUT_SECONDS` |

For QQQ and PSQ during regular hours the defaults (10 bps working, 50 bps hard
guard) cross a typical 1–2 cent spread with room to spare.
