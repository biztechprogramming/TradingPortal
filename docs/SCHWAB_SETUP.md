# Charles Schwab Trader API setup

Uses the **Schwab Trader API — Individual**, the current official API. The
retired TD Ameritrade API is not used anywhere in this project.

---

## Before you start: two things that will shape your workflow

1. **There is no paper-trading environment.** Schwab's individual Trader API
   connects to live accounts only. This project compensates by running Schwab
   in *shadow mode* (real balances and quotes, simulated fills) in both
   `dry_run` and `paper` modes. Real Schwab orders require `TRADING_MODE=live`.
2. **Refresh tokens expire after 7 days and cannot be extended.** You must
   re-run the login command weekly. Nothing you can configure changes this.

---

## 1. Create a developer account and app

1. Register at <https://developer.schwab.com> using the same credentials as
   your brokerage login.
2. **Dashboard → Apps → Create App.**
3. Product: **Trader API — Individual**.
4. **Callback URL:** `https://127.0.0.1:8182/`
   - Must be `https`, and must match `SCHWAB_CALLBACK_URL` exactly, trailing
     slash included.
   - `schwab-login` listens on that port for the few seconds the login
     takes, catches the redirect and reads the code itself. If it cannot
     bind the port it says so and falls back to asking you to copy the URL
     out of the address bar.
5. Submit and wait for approval. This is **not** instant; approval has
   historically taken from a few days to a couple of weeks. The app must show
   **Ready For Use** before the API will authenticate.
6. Once approved, copy the **App Key** and **App Secret**.

---

## What you are actually granting

Worth being precise about, because it is less granular than most people expect.

Schwab's individual Trader API gives you **two** permission levers, and neither
is per-operation:

**1. Product level** — chosen when you create the app. This system needs
**both**:

| Product | Why it is needed |
|---|---|
| Accounts and Trading Production | positions, balances, place / cancel / status orders |
| Market Data Production | quotes and price history |

Market Data is the one people forget. Without it there are no quotes, and this
system refuses to trade without a fresh quote — so it would abort every command.

**2. Account level** — chosen during the login/consent flow. Schwab lists your
linked accounts with checkboxes and you tick which ones the app may reach.
**This is the real containment lever.** An account you leave unticked is
invisible to the program.

### There is no read-only scope

Schwab does not offer "can read positions but cannot trade". If the app can see
an account, it can place orders in it. The permission you asked for — read
positions, place trades, cancel trades, check status — is exactly the
granularity available. You cannot subdivide it, and you cannot grant less.

### What the API cannot do at all

Regardless of what you tick, the Trader API has no ability to:

- move money (ACH, wire, or transfers between accounts)
- change account settings, beneficiaries, or linked banks
- reach accounts you did not authorise

So the worst case is unwanted **trading** in the accounts you tick — not
withdrawal.

### How this program limits itself anyway

Since Schwab will not enforce anything finer, the software does:

| Guard | Effect |
|---|---|
| `TRADING_ACCOUNTS` allowlist | authorised accounts outside it are wrapped so every order path raises |
| Symbol allowlist | only QQQ and PSQ can ever be ordered |
| Never short, never margin | `SELL_SHORT` is never emitted; margin buying power is ignored |
| `MAX_ORDER_NOTIONAL_DOLLARS`, `MAX_SHARES_PER_ORDER` | per-order ceilings |
| `MAX_DAILY_COMMANDS` | runaway-automation cap |
| `TRADING_MODE=dry_run` | the default; no order can reach Schwab at all |

---

## 2. Configure

```bash
cp .env.example .env
chmod 600 .env
```

### One login, one account

```bash
SCHWAB_APP_KEY=your-app-key
SCHWAB_APP_SECRET=your-app-secret
SCHWAB_CALLBACK_URL=https://127.0.0.1:8182/
```

### Several accounts, several logins

Your Schwab **user IDs** and your **accounts** are separate things. One
developer app serves every login — the app key and secret belong to the app,
not to a login. What differs per login is the OAuth authorisation and therefore
the refresh token.

For three accounts spread across two logins:

```bash
SCHWAB_APP_KEY=your-app-key
SCHWAB_APP_SECRET=your-app-secret

# One label per Schwab user ID. Names are yours to choose.
SCHWAB_LOGINS=personal,joint

# Which accounts may TRADE. Last 4 digits or full numbers.
# Leave empty and every authorised account can trade.
TRADING_ACCOUNTS=1111,2222,3333

# Authorised accounts outside the allowlist still appear, read-only.
SHOW_READONLY_ACCOUNTS=true
```

Each login gets its own token file automatically
(`var/state/schwab_token_personal.json`,
`var/state/schwab_token_joint.json`). **Do not point two logins at one file** —
each refresh would invalidate the other's, and the two would lock each other
out in a loop.

Each account becomes its own instance, named `schwab:<login>:<last4>`, so
commands, results and audit records are always attributable to exactly one
account, and each is sized from its own available cash.

## 3. Authorise — once per login, then weekly

Run this **once per Schwab user ID**:

```bash
./qqq schwab-login --login personal
./qqq schwab-login --login joint
```

For each:

1. Open the printed URL and log in with **that login's brokerage credentials**
   (not your developer-portal credentials).
2. **Tick only the accounts this program should reach.** This is your one
   chance to scope it — there is no read-only option, so any account you tick
   can be traded. Unticked accounts stay invisible to the app.
3. Approve. The browser lands on `https://127.0.0.1:8182/?code=…`, which the
   command is listening on, and shows "Authorisation received". Nothing to
   copy.

The certificate is self-signed, so the browser warns the first time. Choose
*Advanced* → *Proceed*; it is your own machine, and the certificate is reused
from `var/state/` so the warning does not return.

The command then prints the accounts that login authorised and marks each
`tradable` or `read-only` per your `TRADING_ACCOUNTS` setting.

### If the redirect cannot be caught

Pass `--no-listener`, or let it fall back on its own (port in use, no
`openssl`, browser on a different machine). You then copy the entire redirected
URL out of the address bar and paste it at the prompt.

Be quick about it. **Schwab's authorisation codes expire in roughly thirty
seconds**, timed from the moment you approve, and they work exactly once. A
slow paste produces:

```
invalid_grant: "Authorization code is invalid, expired or revoked"
```

which means only that the clock beat you — nothing is misconfigured. Log in to
schwab.com *first* so the timed window holds nothing but the checkboxes and the
Approve button, then paste without stopping to read. Every retry needs a fresh
trip through the authorise URL; a redirect URL cannot be reused.

Tokens are written with mode `0600`.

### Verify

```bash
./qqq accounts     # every account and its trading permission
./qqq status       # positions, cash, exposure
```

`accounts` is the one to check after any change. It prints, per account,
whether it is TRADABLE or READ-ONLY — confirm that matches your intent before
you enable live trading.

### Adding an account later

Schwab only shows accounts that existed at authorisation time. After opening a
new account, re-run `schwab-login` for that login and tick it. You may also
need to renew the app's keys in the developer portal for it to appear.

### Weekly renewal

The 7-day refresh-token expiry is a hard Schwab limit. When it lapses you will
see:

```
BrokerAuthError: Schwab token refresh rejected (HTTP 400).
Refresh tokens expire after 7 days; re-run `schwab-login`.
```

Put a calendar reminder on a **non-trading day** — discovering an expired token
at 09:31 on a Monday is avoidable.

Access tokens (30 minutes) refresh automatically with 60 seconds of headroom;
you never handle those.

---

## Endpoints used

| Purpose | Method & path |
|---|---|
| Account list (number → hash) | `GET /trader/v1/accounts/accountNumbers` |
| Transactions (performance history) | `GET /trader/v1/accounts/{hash}/transactions` |
| Daily closes (performance history) | `GET /marketdata/v1/pricehistory` |
| Positions & balances | `GET /trader/v1/accounts/{hash}?fields=positions` |
| Open orders | `GET /trader/v1/accounts/{hash}/orders` |
| Place order | `POST /trader/v1/accounts/{hash}/orders` |
| Order status | `GET /trader/v1/accounts/{hash}/orders/{orderId}` |
| Replace order | `PUT /trader/v1/accounts/{hash}/orders/{orderId}` |
| Cancel order | `DELETE /trader/v1/accounts/{hash}/orders/{orderId}` |
| Quotes | `GET /marketdata/v1/quotes?symbols=QQQ` |
| Token refresh | `POST /v1/oauth/token` |

### Behaviours the adapter handles

- **Account hashes, not account numbers.** Order URLs take the opaque
  `hashValue`. The real number is only ever displayed masked (`...6789`).
- **The new order id is in the `Location` header**, not the response body —
  `POST` and `PUT` return empty bodies. If `Location` is missing, the adapter
  raises rather than losing track of a live order.
- **Replace is atomic and issues a new order id.** The adapter reads back the
  unfilled quantity first and replaces for that amount only, so a partial fill
  is never re-bought.
- **Rate limits.** Order-mutating requests are capped per account (up to
  120/min). A client-side sliding window holds us to 100/min; `429` responses
  honour `Retry-After`.
- **Balances.** For `MARGIN` accounts the adapter reads
  `buyingPowerNonMarginableTrade`, capped at `cashBalance` — the amount
  spendable *without borrowing*. Not `availableFundsNonMarginableTrade`,
  which Schwab zeroes once the account holds marginable stock.
  For `CASH` accounts it reads `cashAvailableForTrading`.
- **Quote freshness** comes from `quoteTime`. If Schwab omits it, the quote is
  rejected rather than treated as current.

## Order payload

```json
{
  "orderType": "LIMIT",
  "session": "NORMAL",
  "duration": "DAY",
  "orderStrategyType": "SINGLE",
  "price": "500.55",
  "orderLegCollection": [{
    "instruction": "BUY",
    "quantity": 19,
    "instrument": {"symbol": "QQQ", "assetType": "EQUITY"}
  }]
}
```

`instruction` is only ever `BUY` or `SELL`. `SELL_SHORT` is never emitted.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `invalid_client` on login | App is not yet **Ready For Use**, or key/secret mismatch. |
| `invalid_grant` on login | The code expired (~30s) or was already used. Re-run and open the **new** authorise URL; an old redirect URL will never work. |
| Browser warns about the certificate | Expected — it is self-signed, from `var/state/oauth_cert_*.pem`. Proceed once per browser. |
| Token refresh 400 after a week | Expected — re-run `schwab-login --login <label>`. |
| One login works, the other 401s | Two logins sharing a token file. Give each its own; the defaults already do. |
| An account is missing | It was not ticked during consent. Re-run `schwab-login` for its login. |
| An account shows READ-ONLY unexpectedly | It is not in `TRADING_ACCOUNTS`. Check `cli accounts`. |
| Everything is tradable and you did not intend that | `TRADING_ACCOUNTS` is empty, which means "all". Set it. |
| Callback URL mismatch | Must match the portal exactly, including `https` and the trailing slash. |
| Empty positions on a funded account | Confirm the app was approved for **Trader API**, and that the right account was linked during authorisation. |
| `no quoteTime` errors | Market data entitlement issue on the app; check the app's approved products. |
