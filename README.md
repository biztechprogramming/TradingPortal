# QQQ / PSQ Switch

Three commands, executed independently against **Charles Schwab** and
**Interactive Brokers**, using each account's own available funds.

| Command | Action |
|---|---|
| **GO LONG** | move the account into **QQQ** |
| **GO SHORT** | move the account into **PSQ** (the inverse ETF — never a short sale, never margin) |
| **GO FLAT** | sell QQQ and/or PSQ, remain in cash |

Every authorised account runs the command **independently, sized from its own
available cash** — including several Schwab accounts across several Schwab
logins. A dashboard shows all holdings and time-weighted returns over 1, 2, 3
and 6 months.

Dry-run is the default. Live trading requires two separate configuration
switches and, by default, a deliberate confirmation immediately before each
directional command.

---

## Read this first: what the broker APIs will not do

Researched against the current official APIs before implementation. These are
the places where reality diverges from an ideal design, and what this project
does instead.

### 0. Schwab has no read-only API scope

An account the app can see is an account it can trade. Schwab's only
account-level control is the checkbox list shown during the OAuth consent flow.

> **What this system does:** adds a `TRADING_ACCOUNTS` allowlist. Authorised
> accounts outside it are wrapped in a `ReadOnlyBroker` whose order methods
> raise instead of delegating, so they appear on the dashboard with full
> holdings and performance while no order path can reach them. Separate logins
> each get their own token file, because a shared one makes each refresh
> invalidate the other. See [docs/SCHWAB_SETUP.md](docs/SCHWAB_SETUP.md).

### 1. Schwab has no paper-trading environment — the significant one

The individual Trader API connects to **live accounts only**. There is no
sandbox that accepts orders.

> **What this system does:** Schwab runs in *shadow mode* for both `dry_run`
> and `paper` — real accounts, real balances, real quotes, **simulated fills**.
> `TRADING_MODE=paper` explicitly refuses to send Schwab orders, because a
> "paper" order that is silently real is the worst possible failure. Schwab
> orders happen only in `live`.
>
> **What this means for you:** the Schwab order path cannot be fully rehearsed
> before it is real. Mitigate by (a) running the full mocked test suite,
> (b) validating everything else against IBKR paper, (c) setting
> `MAX_SHARES_PER_ORDER=1` for your first live command, and (d) using dry-run
> against your real balances to check sizing arithmetic.

### 2. Schwab refresh tokens expire every 7 days, unconditionally

There is no long-lived credential and no way to extend it.

> **What this system does:** access tokens refresh automatically; the 7-day
> renewal is a manual `schwab-login`, and the failure message says exactly
> that. Put a weekly calendar reminder on a non-trading day.

### 3. IBKR requires a locally running, browser-authenticated gateway

The Client Portal Gateway needs an interactive login and expires after extended
inactivity or ~24 hours. There is no headless credential in the standard
individual setup.

> **What this system does:** tickles the session to keep it alive, detects an
> unauthenticated gateway with an actionable message, and attempts
> `/iserver/reauthenticate` when the brokerage leg drops but the session
> survives. Check `cli status` before relying on it for the day.
> (IBKR's OAuth 1.0a self-service flow can remove the browser step for some
> account types; the gateway path was chosen because it works for everyone.)

### 4. IBKR's first market-data snapshot returns nothing

Documented behaviour: the endpoint must be called at least twice.

> **What this system does:** polls up to four times until bid and ask appear,
> and strips IBKR's price prefixes (`C` = previous close, `H` = halted).

### 5. IBKR can ask questions instead of accepting an order

Order submission may return a confirmation prompt that must be answered at
`/iserver/reply/{id}` before the order exists. The same mechanism carries both
trivial notices and serious risk warnings.

> **What this system does:** auto-answers only an allowlist of known-benign
> prompts. **Anything unrecognised stops the order** and reports the exact
> text. `IBKR_AUTO_CONFIRM_ALL_QUESTIONS=true` opts out.

### 6. Schwab returns the new order id in a header, not the body

`POST`/`PUT` return empty bodies; the id is in `Location`.

> **What this system does:** parses `Location`, and treats a missing header as
> an error rather than losing track of an order that may be live.

### 7. Replacing an order changes its identity at both brokers

Schwab's `PUT` and IBKR's modify both effectively produce a new order id.

> **What this system does:** tracks cumulative fills across every order id a
> leg touches, so a partially-filled-then-replaced order is neither
> double-counted nor re-bought.

### 8. "Available cash" is not one number

Schwab reports `cashAvailableForTrading`, `availableFunds`, `buyingPower` and
`availableFundsNonMarginableTrade`; IBKR reports `cashbalance`, `settledcash`,
`availablefunds` and `buyingpower`. Several of these include borrowed money.

> **What this system does:** with `ALLOW_MARGIN=false` (default) it uses
> Schwab's `buyingPowerNonMarginableTrade` (capped at `cashBalance`) on margin accounts and IBKR's
> `min(availablefunds, settledcash)` — the money you can spend without
> borrowing.

### 9. Sale proceeds are not instantly spendable in a cash account

US equities settle T+1. Selling PSQ and immediately buying QQQ with the
proceeds can cause a good-faith violation in a **cash** account.

> **What this system does:** never assumes. After a sale it polls the broker
> until the proceeds are reported as available (up to
> `SETTLEMENT_WAIT_SECONDS`), sizes against whatever the broker actually
> reports, and warns when the credit has not appeared. Rounds cleanly in a
> margin account used without borrowing.

### 10. Fractional shares are not supported for these orders

Both APIs take whole-share limit orders for ETFs.

> **What this system does:** floors to whole shares. A small cash remainder
> always stays uninvested — `CASH_BUFFER_DOLLARS` makes that explicit rather
> than accidental.

### 11. Neither broker gives comparable performance history

Schwab has no performance endpoint. IBKR has Portfolio Analyst but rate limits
it to roughly one request per 15 minutes, and its response schema is not fully
documented publicly.

> **What this system does:** records its own daily NAV per account (exact, but
> only from installation onward), and backfills what it can — IBKR's own series
> where available, and for Schwab a reconstruction from ~180 days of
> transactions plus daily closes. Every figure is labelled with its provenance,
> and windows that history cannot cover are marked partial rather than
> silently shortened. See [docs/PERFORMANCE.md](docs/PERFORMANCE.md).

### 12. Rate limits

Schwab caps order-mutating requests per account (up to 120/min); IBKR limits
concurrent snapshot requests and returns 429s.

> **What this system does:** a client-side sliding window holds Schwab to
> 100/min, honours `Retry-After`, and **never automatically retries a mutating
> request** — a retried order submission could double the position.

---

## Architecture

```
src/trading/
  config.py          Environment-driven settings + validation (no secrets in code)
  models.py          Broker-neutral types: Quote, Position, Order, Fill, Exposure…
  errors.py          Typed errors carrying `retryable` / `position_at_risk`
  audit.py           Append-only JSONL audit log + redacting app logging
  pricing.py         Quote validation + the marketable-limit algorithm
  sizing.py          Position sizing (deploy everything, overspend nothing)
  validation.py      Pre-submit order validation + market-hours guard
  execution.py       Submit → monitor → reprice → partial fills → cancel
  performance.py     NAV ledger + time-weighted returns over 1/2/3/6 months
  backfill.py        Historical NAV reconstruction from transactions + prices
  strategy.py        GO LONG / GO SHORT / GO FLAT, broker-independent
  orchestrator.py    Locking, confirmation, dispatch, mismatch handling
  state.py           Durable state: last command, dedupe, mismatch block
  locks.py           In-process + cross-process execution lock
  api.py             FastAPI: dashboard + token-authenticated JSON API
  cli.py             Terminal interface
  web/               Dashboard (no build step, no external assets)
Dockerfile           Multi-stage, non-root, read-only rootfs, tzdata
docker-compose.yml   App + optional IBKR gateway profile + state volume
  brokers/
    base.py          The Broker interface
    schwab.py        SchwabBroker   (Trader API)
    ibkr.py          IBKRBroker     (Client Portal Web API)
    simulated.py     SimulatedBroker + ShadowBroker (dry-run)
    readonly.py      ReadOnlyBroker — authorised to read, never to trade
    factory.py       One instance per account; applies mode + permission gates
tests/               266 tests, all against mocked brokers
docs/                Limit algorithm, safety, per-broker setup, shortcuts
```

The strategy layer never sees a Schwab account hash or an IBKR conid. Adding a
third broker means writing one `Broker` subclass:

```python
class Broker(ABC):
    def get_accounts()                                  ...
    def get_positions()                                 ...
    def get_available_cash()                            ...
    def get_quote(symbol)                               ...
    def get_open_orders()                               ...
    def cancel_order(order_id)                          ...
    def submit_limit_order(symbol, side, qty, price)    ...
    def replace_order(order_id, limit_price)            ...
    def get_order_status(order_id)                      ...
```

---

## Installation

### Docker (recommended for always-on use)

```bash
cp .env.example .env && chmod 600 .env
docker compose build
docker compose run --rm app doctor          # tells you what still needs setting
docker compose run --rm -it -p 127.0.0.1:8182:8182 app schwab-login --login default
docker compose up -d
docker compose run --rm app accounts        # verify trading permissions
```

Dashboard on `http://127.0.0.1:8787/`, published to loopback only. If that
port is taken, set `API_PORT` in `.env` — it moves both the container's
listener and the published port. State
(tokens, NAV history, audit log) lives in the `qqq-switch-state` volume and
survives rebuilds — losing it means re-authorising Schwab by hand.

The container runs non-root with a read-only root filesystem, all capabilities
dropped, and `TRADING_MODE=dry_run` baked in as the image default. The IBKR
gateway needs its own decision — host gateway or the optional IBeam profile.
See **[docs/DOCKER.md](docs/DOCKER.md)**.

### Local Python

Requires Python 3.10+.

```bash
cd /srv/environments/dev/trade
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt      # or requirements.txt to skip tests

cp .env.example .env
chmod 600 .env
```

Run the tests before anything else:

```bash
python -m pytest -q          # expect: 266 passed  (needs the venv active)
```

Smoke-test with no credentials at all:

```bash
export PYTHONPATH=src
BROKERS_ENABLED=simulated ./qqq status
BROKERS_ENABLED=simulated ./qqq long
```

> The pure `simulated` broker is in-memory and per-process, so its state resets
> between CLI invocations. It exists to exercise the plumbing. For a realistic
> rehearsal, use `dry_run` with real credentials, which reads your actual
> balances and quotes.

---

## Usage

### Web dashboard

```bash
./start      # dashboard in the background (IBKR waits for Log in); ./stop to stop
./qqq serve  # or just the dashboard, in the foreground
```

Prints a URL containing the token. Three large buttons, plus:

- **logins**: each Schwab login with the days left on its token and a Log in
  button (the consent page opens in a new tab and the redirect is caught on
  `SCHWAB_CALLBACK_URL`, so port 8182 must be forwarded too when working
  remotely), and IBKR with Log in / Log out buttons. The gateway is not
  started with the server: while it runs it holds the only IBKR session and
  locks out every browser. Charts and quotes come from Schwab first, so they
  work with IBKR logged out
- per account: connection, account id, QQQ shares, PSQ shares, cash, buying
  power, exposure, outstanding orders, last command, last execution price,
  errors, and a READ-ONLY badge where trading is not permitted
- a combined summary across every account
- **all holdings in every account** — not just QQQ and PSQ
- **performance**: time-weighted returns over 1M / 2M / 3M / 6M per account,
  per broker and combined, plus an indexed account-value chart
- a live activity feed from the audit log
- a mode banner: blue (dry run), green (paper), pulsing red (live)

### Command line

`./qqq` is a wrapper that uses the project venv and puts `src/` on the path,
so it works from any directory without activating anything. (`python -m
trading.cli …` also works, but only with the venv active *and*
`PYTHONPATH=src` set.)

```bash
./qqq doctor          # what is configured, what is missing
./qqq status          # dashboard in the terminal
./qqq holdings        # every position in every account
./qqq accounts        # accounts and their trading permission
./qqq performance     # returns over 1M / 2M / 3M / 6M
./qqq long            # GO LONG
./qqq short           # GO SHORT
./qqq flat            # GO FLAT
./qqq acknowledge     # clear a broker-mismatch block
./qqq schwab-login --login <label>   # weekly, once per login
```

`--force` bypasses the duplicate window; `--yes` auto-accepts the live
confirmation; `--request-id` supplies an idempotency key.

### HTTP API

```bash
curl -X POST http://127.0.0.1:8787/api/long \
  -H "Authorization: Bearer $API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"command":"GO_LONG","request_id":"tap-1"}'
```

Read-only: `/api/status`, `/api/holdings`, `/api/performance`,
`/api/accounts`, `/api/audit`, `/api/history`, `/api/config`, `/api/health`. See **[docs/SHORTCUTS.md](docs/SHORTCUTS.md)**
for iPhone Shortcut, Stream Deck, keyboard and voice triggers, and for the full
response-code table.

---

## What a command actually does

**GO LONG** (GO SHORT is the mirror image):

1. Reconcile — read live positions, open orders, cash and buying power. Never
   trust the previous command.
2. Cancel any conflicting open QQQ/PSQ orders, and **confirm each cancel**.
   An unconfirmed cancel aborts the command.
3. If PSQ is held, sell all of it and confirm the fill.
4. Re-read cash, polling until sale proceeds are reported as available.
5. Compute the marketable limit for QQQ, then the maximum whole shares that fit
   inside `cash − buffer` at that limit.
6. Validate ticker, side, quantity, account and estimated value.
7. Submit a **marketable limit order** (never a market order).
8. Monitor; cancel/replace at a progressively more aggressive price if needed,
   inside a hard slippage guard fixed before the leg started.
9. Reconcile again and report fills, average price, remaining cash, warnings
   and errors.

If QQQ is already held it is **not** sold and re-bought. Idle cash that buys at
least one whole share is added to the position; otherwise nothing is submitted.

**GO FLAT** cancels conflicting orders, sells all QQQ, sells all PSQ, confirms
the fills, and **buys nothing**.

Both brokers run **concurrently and independently** — neither can block or
abort the other.

---

## Trading modes

| Mode | Orders reach a broker? | Notes |
|---|---|---|
| `dry_run` **(default)** | **No.** Ever. | Real reads when credentials exist; every mutation is simulated locally. |
| `paper` | IBKR paper only | Schwab stays shadowed — it has no paper API. A live IBKR account in this mode is a hard error. |
| `live` | **Yes. Real money.** | Requires `LIVE_TRADING_ENABLED=true` *and* an explicit `API_TOKEN`. |

Dry-run safety is structural: the broker object in dry-run is a `ShadowBroker`
with no code path to the live order endpoints — not a flag inside the adapter
that a future edit could bypass.

---

## Configuration

Every setting lives in `.env`; **[.env.example](.env.example)** documents each
one. The ones that matter most:

| Variable | Default | Purpose |
|---|---|---|
| `TRADING_MODE` | `dry_run` | `dry_run` / `paper` / `live` |
| `TRADING_ACCOUNTS` | *(empty = all)* | Accounts permitted to trade; others are read-only |
| `SCHWAB_LOGINS` | `default` | One label per Schwab user ID |
| `LIVE_TRADING_ENABLED` | `false` | Second switch required for live |
| `REQUIRE_CONFIRMATION` | `true` | Confirm live LONG/SHORT |
| `CASH_BUFFER_DOLLARS` | `25` | Held back from every buy |
| `ALLOW_MARGIN` | `false` | Ignore margin buying power |
| `SHARE_LOT_SIZE` | `10` | Order quantities are whole lots of this many shares when lots still reach 95% of cash; single shares otherwise |
| `TARGET_CASH_MIN_PCT` / `TARGET_CASH_MAX_PCT` | `95` / `98` | Share of available cash a buy deploys |
| `BUY_OFFSET_TICKS` / `SELL_OFFSET_TICKS` | `1` | Working offset through the touch, in ticks |
| `SPREAD_MULTIPLE` | `1.0` | Widen the offset with the spread on a thin book |
| `MAX_SLIPPAGE_TICKS_<SYM>` | QQQ `25`, PSQ `10` | **Hard guard** — abort rather than chase |
| `MAX_SPREAD_TICKS_<SYM>` | QQQ `5`, PSQ `10` | Refuse to trade a wide book |
| `MAX_QUOTE_AGE_SECONDS` | `2` | Refuse stale quotes |
| `ORDER_FILL_TIMEOUT_SECONDS` | `90` | Cancel and report after this |
| `MAX_REPRICE_ATTEMPTS` | `5` | Cap on cancel/replace cycles |
| `DUPLICATE_COMMAND_WINDOW_SECONDS` | `30` | Double-tap guard |
| `MAX_DAILY_COMMANDS` | `50` | Runaway-automation guard |
| `MARKET_HOURS_GUARD` | `true` | Regular session only |

Limit prices are measured in **ticks**, not basis points — 10 bps is 3 ticks on
PSQ and 60 on QQQ, so one bps setting cannot serve both. The bps variables
(`MAX_SLIPPAGE_BPS`, `MAX_SPREAD_BPS`) still exist as price-proportional
backstops, and whichever limit is tighter binds. See
[docs/LIMIT_ALGORITHM.md](docs/LIMIT_ALGORITHM.md).

Buys target 95–98% of available cash and always leave at least one share's
worth of cash (plus the buffer) in the account. They are sized in whole lots
of `SHARE_LOT_SIZE` shares when that still reaches 95% — on QQQ, roughly $200k
and up. Smaller accounts are sized in single shares instead, so they are not
left under-invested; set `SHARE_LOT_SIZE=1` to always size at the share level.

---

## Testing

```bash
python -m pytest -q                             # 266 tests
python -m pytest --cov=trading --cov-report=term-missing
```

No test touches a network. Coverage includes every scenario requested:

| Scenario | Where |
|---|---|
| Flat → Long / Short | `test_transitions.py` |
| Long → Flat, Short → Flat, Mixed → Flat | `test_transitions.py` |
| Long → Short, Short → Long (with ordering assertions) | `test_transitions.py` |
| Already Long → Long, Already Short → Short (no churn) | `test_transitions.py` |
| Partial fills | `test_execution.py`, `test_failures.py` |
| Insufficient cash | `test_failures.py`, `test_sizing.py` |
| Price moving during execution | `test_execution.py`, `test_failures.py` |
| Wide bid/ask spread | `test_failures.py`, `test_pricing.py` |
| Stale quote | `test_failures.py`, `test_pricing.py` |
| Rejected order | `test_failures.py`, `test_execution.py` |
| Broker API timeout | `test_failures.py`, `test_broker_schwab.py` |
| One broker succeeds, one fails | `test_safety.py`, `test_api.py` |
| Duplicate commands | `test_safety.py`, `test_api.py` |
| Several accounts across several logins | `test_accounts.py` |
| Read-only accounts refusing every order | `test_accounts.py` |
| Time-weighted return, deposits and withdrawals | `test_performance.py` |
| NAV reconstruction from transactions | `test_performance.py` |
| Simultaneous/conflicting commands | `test_safety.py` |
| Halted security, crossed quote | `test_failures.py`, `test_pricing.py` |
| Slippage guard, budget-constrained repricing | `test_execution.py` |
| Secret redaction | `test_safety.py` |
| Schwab / IBKR wire formats | `test_broker_schwab.py`, `test_broker_ibkr.py` |

**Integration testing:** IBKR offers a full paper environment — use it (see
[docs/IBKR_SETUP.md](docs/IBKR_SETUP.md)). Schwab does not, which is why the
Schwab adapter is covered by mock-transport tests asserting exact request
shapes and response parsing.

---

## Going live

Do these in order. Do not skip step 4.

1. `python -m pytest -q` — all green.
2. Run `dry_run` against **real credentials** for several days. Check the
   dashboard shows correct positions, cash and quotes, and that the sizing
   arithmetic in the audit log matches what you would have done by hand.
3. Set `TRADING_MODE=paper` and run every command against **IBKR paper**:
   flat → long → short → flat. Verify fills, prices and the audit trail.
4. Read **[docs/SAFETY.md](docs/SAFETY.md)** end to end — including the
   "what this system does *not* protect against" section.
5. Set an explicit `API_TOKEN`.
6. Set `MAX_SHARES_PER_ORDER=1` and `MAX_ORDER_NOTIONAL_DOLLARS` to something
   small.
7. Set `TRADING_MODE=live` **and** `LIVE_TRADING_ENABLED=true`.
8. Run `./qqq accounts` and confirm every account shows the
   permission you intended.
9. Run one `GO LONG` during regular hours. Confirm the fill in Schwab's and
   IBKR's own interfaces — not just in this dashboard.
10. Run `GO FLAT`. Confirm again.
11. Raise the caps gradually. Keep `REQUIRE_CONFIRMATION=true`.

---

## Documentation

- **[docs/LIMIT_ALGORITHM.md](docs/LIMIT_ALGORITHM.md)** — the marketable-limit
  algorithm, the anchor, the hard guard, repricing, partial fills, tuning
- **[docs/SAFETY.md](docs/SAFETY.md)** — every safety mechanism, and the gaps
- **[docs/SCHWAB_SETUP.md](docs/SCHWAB_SETUP.md)** — app registration, OAuth,
  weekly renewal, endpoints, troubleshooting
- **[docs/IBKR_SETUP.md](docs/IBKR_SETUP.md)** — gateway, paper vs live,
  market data, endpoints, troubleshooting
- **[docs/CONFIGURATION.md](docs/CONFIGURATION.md)** — where every environment
  variable comes from, and the order to obtain them in
- **[docs/DOCKER.md](docs/DOCKER.md)** — containerised deployment, the
  interactive Schwab login, IBKR gateway options, backups, security posture
- **[docs/PERFORMANCE.md](docs/PERFORMANCE.md)** — where the return figures
  come from, why time-weighted, how to read the windows and the chart
- **[docs/SHORTCUTS.md](docs/SHORTCUTS.md)** — iPhone Shortcut, Stream Deck,
  keyboard, voice, response codes

---

## Audit log

Append-only JSON Lines, one file per UTC day in `var/log/audit-YYYY-MM-DD.jsonl`,
`fsync`'d on every write. Every command, quote, sizing decision, validation,
submission, replacement, cancellation, fill, error and resulting position, with
a sequence number and `command_id`. Secrets are redacted by key name.

```bash
jq -c 'select(.event | startswith("order."))' var/log/audit-*.jsonl
jq -c 'select(.command_id == "eefeab6dc5e440e1")' var/log/audit-*.jsonl
```

---

## Disclaimer

This software places real orders with real money. It has been tested against
mocked brokers and IBKR's paper environment; it cannot be fully tested against
Schwab before it is live, because Schwab provides no paper API. You are
responsible for every order it places. Start in dry-run, move to paper, and go
live only with small caps.
