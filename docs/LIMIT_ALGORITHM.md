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
| Fresh | `now - quote_time > MAX_QUOTE_AGE_SECONDS` | default 2 s |
| Clock sane | quote timestamp is >30 s in the future | — |
| Tight enough | spread exceeds `MAX_SPREAD_TICKS` **or** `MAX_SPREAD_BPS` | QQQ 5 ticks, PSQ 10; 75 bps |

Two design decisions worth calling out:

- **Freshness is measured against the broker's own quote timestamp**, not
  against when we received the response. Both adapters *refuse to synthesise a
  timestamp* if the broker omits one (Schwab `quoteTime`, IBKR `_updated`) —
  falling back to "now" would make every stale quote look fresh and silently
  defeat this check.
- **The spread guard exists because a marketable limit is at its worst when
  the book is thin.** It is stated in ticks, with the bps figure kept as a
  backstop, and the tighter of the two binds. A bps-only guard is useless on a
  $600 symbol: 75 bps is $4.50, a spread QQQ will never show, so the check
  would never fire on the one name it matters most for.

## 2. The anchor

When a leg begins, the system records an **anchor price**:

- buys: the ask at that moment
- sells: the bid at that moment

Every subsequent guard calculation is relative to the anchor, **not** to the
latest quote. This is the single most important detail in the algorithm.

> If the guard were recomputed from the current quote on each reprice, each
> chase would re-baseline against the price it just chased to, and "maximum
> 25 ticks of slippage" would become unbounded drift over the life of the
> order. Anchoring makes the worst case a fixed number decided up front.

## 3. The limit price

The cushion is measured in **ticks and spreads**, not in basis points of the
price. This is the difference that makes the algorithm work on both halves of
the pair. A single bps figure cannot: 10 bps is about three ticks on a $28 ETF
and about sixty on a $600 one, so the same setting is timid on PSQ and
reckless on QQQ. Ticks are the unit the book is actually quoted in.

For attempt *n* (0 = the initial submission):

```
offset_ticks = OFFSET_TICKS + n * REPRICE_STEP_TICKS
offset       = max(offset_ticks * TICK_SIZE, SPREAD_MULTIPLE * spread)
offset       = min(offset, guard_distance)

guard_distance = min( MAX_SLIPPAGE_TICKS * TICK_SIZE,
                      anchor * MAX_SLIPPAGE_BPS / 10_000 )

BUY   raw   = ask + offset                rounded UP   to TICK_SIZE
      guard = anchor + guard_distance

SELL  raw   = bid - offset                rounded DOWN to TICK_SIZE
      guard = anchor - guard_distance
```

Two units for one boundary is deliberate. Ticks are what mean something on a
given book; the bps figure is a price-proportional backstop, so a fat-fingered
tick count on any symbol at any price still cannot authorise an unbounded
chase. **Whichever is tighter wins.**

The spread term is what makes a single setting serve a penny-wide QQQ book and
a nickel-wide one at the open: on the former the tick floor binds and the order
crosses by exactly one tick; on the latter the spread term binds and the order
gets proportionally more room.

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

QQQ bid 500.03 / ask 500.04 (the normal penny-wide book), `BUY_OFFSET_TICKS=1`,
`REPRICE_STEP_TICKS=1`, `SPREAD_MULTIPLE=1.0`, `MAX_SLIPPAGE_TICKS_QQQ=25`.

| Attempt | Offset | Limit | Guard (anchor 500.04) |
|---|---|---|---|
| 0 | 1 tick | 500.05 | 500.29 |
| 1 | 2 ticks | 500.06 | 500.29 |
| 2 | 3 ticks | 500.07 | 500.29 |
| … | … | ≤ 500.29 | 500.29 |

The limit gets more aggressive each time, but 500.29 is the worst price
possible for this leg no matter how long it runs or how many reprices occur.
Under the old bps settings that boundary was 502.54 — a $2.50 chase on a
symbol whose spread is a penny.

A marketable limit fills against the resting offers, not at its own limit, so
crossing by a tick normally still pays 500.04. The offset buys tolerance for
the 100-200 ms between the quote and the order's arrival; the guard is what
decides how far the market may run before the leg gives up.

The same settings on PSQ at 32.11 / 32.12 produce a limit of 32.13 — one tick,
identically — which is the entire reason for pricing this way.

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
affordable = lot_floor((budget - already_spent) // new_limit_price)
```

The `lot_floor` is not incidental. A reprice that shrinks an order is deciding
a size, exactly as the original sizing did, so it goes through the same
`SHARE_LOT_SIZE` rule — otherwise a 20-share order would quietly come back as
19. The floor is applied to what the budget affords and only *then* compared
with what is left outstanding, so a partially-filled leg can still finish its
ragged tail; it is our replacements that stay whole, not the broker's fills.

- `affordable >= remaining` → plain cancel/replace at the new price.
- `0 < affordable < remaining` → cancel, confirm, and resubmit the *smaller*
  quantity. The leg's target shrinks and a note records it, so the outcome is
  reported as a completed-but-smaller entry rather than a spurious failure.
- `affordable == 0` → leave the working order untouched at its current price.

## 5. Tuning

| Situation | Change |
|---|---|
| Fills are slow | raise `BUY_OFFSET_TICKS` / `SELL_OFFSET_TICKS` to 2–3 |
| Legs abort mid-move on QQQ | raise `MAX_SLIPPAGE_TICKS_QQQ` (25 = $0.25) |
| Fills are worse than expected | lower `MAX_SLIPPAGE_TICKS_<SYM>` |
| Aborts near the open/close | raise `MAX_SPREAD_TICKS_<SYM>`, or avoid the first/last minutes |
| `StaleQuoteError` in normal conditions | raise `MAX_QUOTE_AGE_SECONDS`; the broker's quote timestamps lag |
| Too many cancel/replaces | raise `REPRICE_INTERVAL_SECONDS` or lower `MAX_REPRICE_ATTEMPTS` |
| Illiquid symbol swapped in | lower `MAX_SPREAD_TICKS`, raise `ORDER_FILL_TIMEOUT_SECONDS` |

For QQQ and PSQ during regular hours the defaults cross the touch by exactly
one tick and abort rather than chase past $0.25 (QQQ) or $0.10 (PSQ).

`MAX_SLIPPAGE_TICKS_QQQ` is the dial worth thinking about hardest. Too tight
and a leg aborts during an ordinary fast move, leaving the position half
switched; too loose and it is not a guard. $0.25 is roughly four basis points
on QQQ, against the $3.00 the bps-only defaults used to allow.

---

## 6. How many shares

Sizing lives in `src/trading/sizing.py`. Two rules shape every buy:

- **`SHARE_LOT_SIZE`** (default 10) — the order quantity is always a whole
  multiple. Every path that decides a size honours it: the initial sizing, a
  ladder rung, and a reprice that has to shrink.
- **`TARGET_CASH_MIN_PCT` / `TARGET_CASH_MAX_PCT`** (default 95 / 98) — the
  band of available cash a buy aims to deploy. The max is a hard ceiling. The
  min is a *reporting* threshold, not a floor.

The min is only a reporting threshold because the two rules genuinely conflict
on a high-priced symbol. A buy can land inside the band only when one lot is
worth no more than the band is wide:

```
lot_size * price  <=  (MAX_PCT - MIN_PCT)% * cash
```

At 10 shares and a 3-point band that is roughly **$200,000 of cash for QQQ**
(one lot ≈ $6,000) and **$10,000 for PSQ** (one lot ≈ $320). Below that
threshold the largest whole lot at or under the ceiling is taken and
`caps_applied` records `below TARGET_CASH_MIN_PCT` with the arithmetic. The lot
rule wins the conflict, because a ragged share count is the thing the band
exists to avoid.

The sharpest edge of this: with a 10-share lot, an account holding less than
about **$5,200** cannot place a QQQ order at all — nine shares is not a lot.
The resulting error names `SHARE_LOT_SIZE` so it is not mistaken for a genuine
funding problem. Set `SHARE_LOT_SIZE=1` to size at the share level instead.

| Cash | QQQ @ ~$500 | Deployed |
|---|---|---|
| $5,000 | 0 shares — order refused | — |
| $10,000 | 10 | 50% |
| $50,000 | 90 | 90% |
| $250,000 | 480 | 96% ✅ in band |
| $1,000,000 | 1,950 | 97.5% ✅ in band |
