# Interactive Brokers API setup

Uses the **Client Portal Web API** via the local Client Portal Gateway — IBKR's
current REST interface. The TWS socket API is deliberately not used: the Web
API needs no JVM, no desktop session, and runs headlessly on a server.

**IBKR has a full paper-trading environment.** Unlike Schwab, you can and
should exercise the entire order path here for real before going live.

---

## 1. Download the gateway

<https://www.interactivebrokers.com/en/trading/ib-api.php> → **Client Portal
Web API** → download the Client Portal Gateway. Requires Java 8+.

```bash
mkdir -p ~/ibkr-gateway && cd ~/ibkr-gateway
unzip ~/Downloads/clientportal.gw.zip
```

## 2. Run it

```bash
cd ~/ibkr-gateway
bin/run.sh root/conf.yaml          # Windows: bin\run.bat root\conf.yaml
```

It listens on `https://localhost:5000` with a self-signed certificate — hence
`IBKR_VERIFY_SSL=false` for localhost.

## 3. Log in

Open <https://localhost:5000> and accept the certificate warning.

- **Paper trading:** log in with your **paper** username (account id starts
  with `DU`). This is how you select paper vs live — there is no separate URL.
- **Live:** log in with your live credentials (account id starts with `U`).

The session must stay logged in. The adapter tickles `/tickle` roughly every
45 seconds to keep it alive; it still expires after extended inactivity or
after ~24 hours, requiring a browser re-login.

Verify:

```bash
curl -k -X POST https://localhost:5000/v1/api/iserver/auth/status
# {"authenticated":true,"connected":true,"competing":false,...}
```

## 4. Configure

```bash
IBKR_BASE_URL=https://localhost:5000/v1/api
IBKR_ACCOUNT_ID=DU1234567       # required if the login exposes several
IBKR_VERIFY_SSL=false
```

Pin the contract ids so symbol lookup can never resolve the wrong listing:

```bash
IBKR_CONID_QQQ=320227571
IBKR_CONID_PSQ=43645828
```

Confirm the ids yourself (they are stable but worth verifying once):

```bash
curl -k "https://localhost:5000/v1/api/iserver/secdef/search?symbol=QQQ&secType=STK"
```

## 5. Market data

Real-time US equity data requires a market-data subscription
(**Account Management → Settings → Market Data Subscriptions**). Without one,
IBKR returns delayed data and this system **refuses to trade on it** — pricing
a marketable limit off a 15-minute-old quote is how you get a bad fill.

Paper accounts share the live account's subscriptions.

To accept the risk deliberately: `IBKR_ALLOW_DELAYED_QUOTES=true`.

---

## Endpoints used

| Purpose | Method & path |
|---|---|
| Keepalive | `POST /tickle` |
| Auth status | `POST /iserver/auth/status` |
| Re-authenticate | `POST /iserver/reauthenticate` |
| Accounts (also primes `/iserver`) | `GET /iserver/accounts` |
| Portfolio priming | `GET /portfolio/accounts` |
| Positions (paginated, 30/page) | `GET /portfolio/{acct}/positions/{page}` |
| Cash ledger | `GET /portfolio/{acct}/ledger` |
| Symbol → conid | `GET /iserver/secdef/search` |
| Quote snapshot | `GET /iserver/marketdata/snapshot` |
| Live orders | `GET /iserver/account/orders` |
| Place order | `POST /iserver/account/{acct}/orders` |
| Confirm a prompt | `POST /iserver/reply/{replyId}` |
| Order status | `GET /iserver/account/order/status/{orderId}` |
| Modify order | `POST /iserver/account/{acct}/order/{orderId}` |
| Cancel order | `DELETE /iserver/account/{acct}/order/{orderId}` |

### Quirks the adapter handles

- **`/iserver/accounts` must be called before any snapshot request**, and
  `/portfolio/accounts` before any portfolio request. Both are primed
  automatically.
- **The first snapshot call returns an empty shell.** The adapter polls up to
  four times until bid and ask actually appear.
- **Prices are strings and may carry a prefix**: `C` = previous close,
  `H` = halted. Prefixes are stripped and `H` sets the halted flag, which
  aborts the command.
- **Field ids**: `31` last, `84` bid, `85` ask size, `86` ask, `88` bid size,
  `6509` data availability (`R…` realtime, `D…`/`Y…` delayed).
- **Freshness** uses the snapshot's `_updated` field. If it is absent the quote
  is rejected rather than assumed current.
- **Order confirmation prompts.** Submissions can return a question that must
  be answered at `/iserver/reply/{id}` before the order exists. Only an
  allowlist of known-benign prompts is auto-confirmed; anything unrecognised
  **stops the order** and reports the exact text. The reply loop is depth-capped
  at 5.
- **Competing sessions.** If `/iserver/auth/status` reports `competing: true`,
  the adapter refuses to connect. A TWS session logged into the same account
  can silently take over order routing.
- **Cash-only sizing** uses `min(availablefunds, settledcash)` from the ledger,
  so IBKR's margin buying power is ignored unless `ALLOW_MARGIN=true`.
- **Paper detection** is by account-id prefix, which is what enforces
  "`TRADING_MODE=paper` must not touch a live account".

## Order payload

```json
{"orders": [{
  "conid": 320227571,
  "orderType": "LMT",
  "side": "BUY",
  "quantity": 19,
  "price": 500.55,
  "tif": "DAY",
  "outsideRTH": false,
  "cOID": "qs1234567890"
}]}
```

`cOID` is a client order id, giving IBKR its own duplicate-submission guard on
top of this system's.

---

## Keeping the gateway running

The gateway must be up whenever you trade. For an always-on machine, run it
under a supervisor. Example systemd unit:

```ini
[Unit]
Description=IBKR Client Portal Gateway
After=network.target

[Service]
Type=simple
User=youruser
WorkingDirectory=/home/youruser/ibkr-gateway
ExecStart=/home/youruser/ibkr-gateway/bin/run.sh root/conf.yaml
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

The gateway still needs a **browser login** after a restart; automating that is
outside this project's scope. Check `./qqq status` before
relying on it for the day.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `Is the Client Portal Gateway running` | Gateway down, or wrong `IBKR_BASE_URL` (note the `/v1/api` suffix). |
| `gateway is not authenticated` | Log in at <https://localhost:5000> again. |
| `Another IBKR session is competing` | Close TWS or other API sessions for that account. |
| `no bid/ask for QQQ` | Missing market-data subscription, or the conid is wrong. |
| `delayed data … refused` | Subscribe to US equity data, or set `IBKR_ALLOW_DELAYED_QUOTES=true`. |
| `unrecognised confirmation question` | Read the quoted text. Place one order manually in TWS to see the prompt, then decide. |
| `Multiple IBKR accounts available` | Set `IBKR_ACCOUNT_ID`. |
| SSL errors | Keep `IBKR_VERIFY_SSL=false` for the localhost gateway. |
