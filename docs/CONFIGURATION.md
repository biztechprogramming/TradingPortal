# Where each environment variable comes from

Run this at any point to see exactly what is set, what is missing, and what to
do about each — it never prints a secret's value:

```bash
./qqq doctor
# or, in Docker:
docker compose run --rm app doctor
```

---

## The order matters

Some values cannot be known until after you have authorised, so there is a
sequence. Trying to fill in `.env` top to bottom in one sitting will stall.

```
1. Generate what you own          -> API_TOKEN                (30 seconds)
2. Register a Schwab app          -> APP_KEY / APP_SECRET     (days: approval)
3. Authorise each Schwab login    -> token files              (5 min each)
4. DISCOVER your account numbers  -> TRADING_ACCOUNTS         (after step 3)
5. Start the IBKR gateway         -> IBKR_ACCOUNT_ID          (30 min)
```

**Leave `TRADING_ACCOUNTS` empty until step 4.** You cannot sensibly fill it in
before you have seen which accounts your logins actually expose. Stay in
`dry_run` until it is set — an empty allowlist means every authorised account
can trade.

---

## 1. Values you generate yourself

No signup, no waiting.

| Variable | How to get it |
|---|---|
| `API_TOKEN` | `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `TRADING_MODE` | Your choice: `dry_run` (default), `paper`, `live` |
| `LIVE_TRADING_ENABLED` | `false` until you have finished the go-live checklist |
| `API_PORT` | The port to listen on (default 8787). Under Docker this moves both sides of the port mapping |
| `HOST_API_PORT` | Docker only, rarely needed: publish on a different host port than the container listens on |

`API_TOKEN` is mandatory before live trading and whenever the API is not bound
to loopback (which includes every Docker run). Generate it once and keep it —
it is what your iPhone Shortcut and Stream Deck will send.

---

## 2. Schwab app credentials

**From <https://developer.schwab.com>.** Approval is not instant — historically
days to a couple of weeks — so start this first.

1. Register with the same credentials as your brokerage login.
2. **Dashboard → Apps → Create App.**
3. Select **both** products:
   - *Accounts and Trading Production* — positions, balances, orders
   - *Market Data Production* — quotes and price history
   Missing the second is the most common setup mistake. Without it there are no
   quotes, and this system refuses to trade without a fresh quote, so every
   command aborts.
4. Callback URL: `https://127.0.0.1:8182/` — must match `SCHWAB_CALLBACK_URL`
   exactly, trailing slash included. `schwab-login` listens there itself for
   the duration of the login.
5. Wait for the app to show **Ready For Use**.
6. Copy the App Key and App Secret.

| Variable | Value |
|---|---|
| `SCHWAB_APP_KEY` | App Key from the app's page |
| `SCHWAB_APP_SECRET` | App Secret from the same page |
| `SCHWAB_CALLBACK_URL` | `https://127.0.0.1:8182/` |

**One app serves every login.** The key and secret belong to the *app*, not to
a Schwab user ID.

---

## 3. Schwab logins and their token files

`SCHWAB_LOGINS` is a label per Schwab **user ID**. The names are yours; they
only decide which token file is which.

```bash
SCHWAB_LOGINS=personal,joint
```

Then authorise each one:

```bash
./qqq schwab-login --login personal
./qqq schwab-login --login joint
```

During each consent screen, **tick only the accounts this program should
reach**. Schwab has no read-only scope, so any account you tick can be traded;
unticked accounts stay invisible to the program.

Token files are written automatically to `<STATE_DIR>/schwab_token_<label>.json`.
You do not need to configure paths. Do **not** point two logins at one file —
each refresh would invalidate the other's, locking both out.

> `SCHWAB_TOKEN_PATH` is commented out in `.env.example` on purpose. Setting it
> overrides the automatic derivation, so `STATE_DIR` would stop moving the
> token files with the rest of your state — which in Docker means writing them
> outside the volume and losing them on restart.

**These expire every 7 days.** `doctor` shows the countdown per login:

```
set      login 'personal': valid, 6.8 days left
MISSING  login 'joint': refresh token EXPIRED (8.1 days old)
```

---

## 4. Which accounts may trade

Only knowable after step 3. Run:

```bash
./qqq accounts
```

```
schwab:personal:1111   ...1111   TRADABLE    connected
schwab:personal:2222   ...2222   TRADABLE    connected
schwab:joint:3333      ...3333   TRADABLE    connected
```

Copy the last 4 digits of the accounts you want to trade:

```bash
TRADING_ACCOUNTS=1111,2222,3333
```

Re-run `accounts` and confirm each shows the permission you intended. Anything
authorised but absent from this list becomes READ-ONLY: fully visible on the
dashboard, but every order path raises.

---

## 5. Interactive Brokers

| Variable | How to get it |
|---|---|
| `IBKR_BASE_URL` | `https://localhost:5000/v1/api` on the host; `https://host.docker.internal:5000/v1/api` from Docker |
| `IBKR_ACCOUNT_ID` | Shown in Client Portal / TWS. `U…` = live, `DU…` = paper. Leave empty to use all |
| `IBKR_ACCOUNT_IDS` | Comma-separated, if you have several |
| `IBKR_VERIFY_SSL` | `false` — the local gateway uses a self-signed certificate |
| `IBKR_CONID_QQQ` / `_PSQ` | Optional. Pin them so symbol lookup cannot resolve the wrong listing |

Start the gateway and log in first ([IBKR_SETUP.md](IBKR_SETUP.md)), then
`cli accounts` will show the account id — you do not have to hunt for it.

To find the conids once:

```bash
curl -k "https://localhost:5000/v1/api/iserver/secdef/search?symbol=QQQ&secType=STK"
```

`IBEAM_ACCOUNT` / `IBEAM_PASSWORD` are needed **only** if you opt into the
containerised gateway profile — see [DOCKER.md](DOCKER.md) for the trade-off
that involves.

---

## 6. Everything else

Every remaining variable in `.env.example` has a working default and is
documented inline there. The ones most worth a deliberate decision:

| Variable | Default | Why you might change it |
|---|---|---|
| `CASH_BUFFER_DOLLARS` | 25 | Larger if you want more headroom against fees |
| `MAX_ORDER_NOTIONAL_DOLLARS` | 0 (no cap) | **Set this small for your first live trade** |
| `MAX_SHARES_PER_ORDER` | 0 (no cap) | Same — start at 1 |
| `MAX_SLIPPAGE_BPS` | 50 | The hard abort guard |
| `MARKET_HOURS_GUARD` | true | Leave on |

---

## 7. Price ladder

Rungs hang off the two **swing** levels — the high since the low of day
(`HSL`) and the low since the high of day (`LSH`) — not off the day extremes.
Whichever day extreme came second is one of the two anyway, so nothing current
is lost, and the pair brackets the leg price is actually in.

Every rung click needs a confirmation regardless of mode or
`REQUIRE_CONFIRMATION`, and a rung already at the working price re-prices that
order rather than adding a second one.

| Variable | Default | Why you might change it |
|---|---|---|
| `LADDER_ENABLED` | true | Set false to hide the ladder entirely |
| `LADDER_LEVELS` | 5 | Rungs either side of each anchor. Capped at 20 |
| `LADDER_STEP_QQQ` | 0.10 | Rung spacing for the long symbol |
| `LADDER_STEP_PSQ` | 0.005 | Rung spacing for the short symbol |
| `LADDER_TICK_<SYM>` | min(step, `TICK_SIZE`) | The venue increment rungs snap to. Only set this if your venue's tick differs from a penny |
| `LADDER_MAX_DEVIATION_PCT` | 10 | Fat-finger guard: refuses a rung this far from last |

`LADDER_STEP_<SYMBOL>` follows whatever `LONG_SYMBOL`/`SHORT_SYMBOL` are set
to, not a hard-coded QQQ/PSQ pair.

---

## 8. Charts and cached bars

Ninety days of hourly extended-hours bars per symbol live in
`var/state/candles.db`. A refresh re-fetches only the recent edge; closed bars
are final. A separate, finer series times the session's extremes — an hourly
bar can only say *which hour* a high happened in, which is not a time.

| Variable | Default | Why you might change it |
|---|---|---|
| `CANDLE_DAYS` | 90 | How much history the chart holds |
| `CANDLE_BAR_SIZE` | 1h | Bar size for the chart |
| `CANDLE_OUTSIDE_RTH` | true | Include pre/post-market bars |
| `CANDLE_REFRESH_HOURS` | 2 | How much of the recent edge is re-fetched |
| `CANDLE_MAX_GAP_HOURS` | 80 | Must exceed 72 — a weekend is a legitimate ~65h hole |
| `SESSION_BAR_SIZE` | 1min | The fine series used to time the extremes |
| `SESSION_DAYS` | 2 | Days of the fine series kept; the rest is pruned |
| `SESSION_REFRESH_SECONDS` | 45 | Floor between fine-series refreshes |
| `QUOTE_DISPLAY_TTL_SECONDS` | 5 | Dashboard quote memo. Order pricing never uses it |
| `REFERENCE_SYMBOLS` | NDX | Charted but **never** tradable — see below |

`REFERENCE_SYMBOLS` is a comma-separated list charted alongside the traded
pair, because QQQ and PSQ both track the Nasdaq-100 and the underlying is what
explains their moves. They are deliberately kept out of the tradable
allowlist: an order for one is rejected like any other unknown ticker, and
startup refuses a reference symbol that shadows `LONG_SYMBOL` or
`SHORT_SYMBOL`.

Note that `NDX` is an index, so it is only computed during the regular
session — its chart has no extended-hours bars even when `CANDLE_OUTSIDE_RTH`
is on. Point `REFERENCE_SYMBOLS` at a Nasdaq-100 futures contract instead if
you want overnight coverage; set `IBKR_CONID_<SYMBOL>` to pin the contract,
since futures symbols are ambiguous across expiries.

---

## Keeping secrets out of the wrong places

- `.env` is git-ignored and excluded from the Docker build context.
- `chmod 600 .env`.
- Nothing secret is baked into the image; credentials arrive at runtime.
- The audit log redacts by key name, so any variable containing `TOKEN`,
  `SECRET`, `KEY`, `PASSWORD`, `CREDENTIAL` or `AUTH` is written as
  `***REDACTED***`.
- `doctor` prints `********` for secret-named variables, so its output is safe
  to share.

For stronger isolation than a dotfile, export the same variable names from a
secrets manager (systemd `EnvironmentFile`, Docker secrets, 1Password CLI,
Vault). The code only ever reads the environment — it has no idea where the
values came from.
