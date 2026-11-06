'use strict';

// --- token handling ---------------------------------------------------------
// The token may arrive in the URL (convenient for a bookmark) or be typed once
// and kept in sessionStorage. It is never written to localStorage.

function readToken() {
  const url = new URL(window.location.href);
  const fromUrl = url.searchParams.get('token');
  if (fromUrl) {
    sessionStorage.setItem('api_token', fromUrl);
    url.searchParams.delete('token');
    window.history.replaceState({}, '', url.toString());
    return fromUrl;
  }
  return sessionStorage.getItem('api_token') || '';
}

let TOKEN = readToken();
let busy = false;
let longSymbol = 'QQQ';
let shortSymbol = 'PSQ';
let assetVersion = null;

const $ = (id) => document.getElementById(id);
const fmtMoney = (n) =>
  n === null || n === undefined
    ? '—'
    : n.toLocaleString('en-US', { style: 'currency', currency: 'USD' });
const fmtNum = (n, d = 0) =>
  n === null || n === undefined ? '—' : Number(n).toLocaleString('en-US', {
    minimumFractionDigits: d, maximumFractionDigits: d,
  });
// Ladder prices can be sub-penny (PSQ steps by 0.005), so prices are never
// formatted with the 2-decimal money formatter.
const fmtPrice = (n, tick) => {
  if (n === null || n === undefined) return '—';
  const decimals = tick && tick < 0.01 ? 3 : 2;
  return `$${Number(n).toFixed(decimals)}`;
};
const esc = (s) =>
  String(s ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${TOKEN}`,
      ...(options.headers || {}),
    },
  });
  if (res.status === 401) {
    showTokenPrompt();
    throw new Error('Unauthorized');
  }
  const body = await res.json().catch(() => ({}));
  return { status: res.status, body };
}

function showTokenPrompt() { $('token-prompt').classList.remove('hidden'); }

$('token-save').addEventListener('click', () => {
  const value = $('token-input').value.trim();
  if (!value) return;
  TOKEN = value;
  sessionStorage.setItem('api_token', value);
  $('token-prompt').classList.add('hidden');
  refresh();
  refreshLogins();
});

// --- live reload ------------------------------------------------------------
// The server serves the dashboard assets with no-store and fingerprints them
// at /api/version. When the fingerprint changes -- an edit to app.js, or a
// server restart -- the open page reloads itself instead of silently running
// stale code.

async function checkVersion() {
  try {
    const res = await fetch('/api/version', { cache: 'no-store' });
    if (!res.ok) return;
    const body = await res.json();
    const stamp = `${body.assets}:${body.server_started_at}`;
    if (assetVersion === null) { assetVersion = stamp; return; }
    if (stamp !== assetVersion) {
      console.info('dashboard assets changed; reloading');
      window.location.reload();
    }
  } catch (err) {
    // Server restarting mid-poll is normal; the next tick picks it up.
  }
}

// --- alerts -----------------------------------------------------------------

function setAlerts(items) {
  $('alerts').innerHTML = items
    .map((a) => `<div class="alert alert-${a.level}">${a.html}</div>`)
    .join('');
  const ackBtn = document.getElementById('ack-btn');
  if (ackBtn) ackBtn.addEventListener('click', acknowledge);
}

async function acknowledge() {
  // The dashboard's own dialog, not confirm(): a browser that suppresses
  // pop-ups answers confirm() with "Cancel" and the click silently does nothing.
  if (!await askConfirm('Acknowledge the broker mismatch?',
    '<p>This only clears the warning and unblocks trading. It places, retries '
    + 'and cancels nothing.</p><p>Do it after you have checked each account\'s '
    + 'real positions.</p>')) return;
  await api('/api/acknowledge', {
    method: 'POST',
    body: JSON.stringify({ note: 'acknowledged from dashboard' }),
  });
  refresh();
}

// --- rendering --------------------------------------------------------------

function renderBanner(status) {
  const banner = $('mode-banner');
  banner.className = `banner banner-${status.mode}`;
  const label = status.mode === 'live'
    ? '⚠ LIVE TRADING — REAL MONEY'
    : status.mode === 'paper'
      ? 'PAPER TRADING'
      : 'DRY RUN — NO ORDERS ARE SENT';
  $('mode-text').textContent = label;
  const m = status.market || {};
  $('market-text').textContent =
    `${m.open ? 'Market open' : m.session === 'extended' ? 'Extended hours'
      : 'Market closed'} — ${m.reason} · ${m.now_eastern || ''}`;
}

function renderCombined(c) {
  longSymbol = c.long_symbol || 'QQQ';
  shortSymbol = c.short_symbol || 'PSQ';
  // Chart set is server-driven, so adding a reference symbol is a config
  // change rather than a frontend one.
  if (c.chart_symbols?.length) chartSymbols = c.chart_symbols;
  $('long-sub').textContent = longSymbol;
  $('short-sub').textContent = shortSymbol;

  const stats = [
    ['Exposure', `<span class="pill pill-${c.exposure}">${c.exposure}</span>`],
    [`${longSymbol} shares`, fmtNum(c.long_shares)],
    [`${shortSymbol} shares`, fmtNum(c.short_shares)],
    ['Cash available', fmtMoney(c.cash_available)],
    ['Positions value', fmtMoney(c.positions_market_value)],
    ['Account value', fmtMoney(c.account_value)],
    ['Open orders', fmtNum(c.open_orders)],
    ['Brokers up', `${c.brokers_connected} / ${c.brokers_total}`],
  ];
  $('combined').innerHTML = stats
    .map(([label, value]) =>
      `<div class="stat"><div class="stat-label">${label}</div>
       <div class="stat-value">${value}</div></div>`)
    .join('');
}

function renderBrokers(status) {
  const lastFills = status.last_fills || {};
  const symbols = [longSymbol, shortSymbol];

  $('brokers').innerHTML = Object.entries(status.brokers)
    .map(([name, b]) => {
      const result = (status.state?.last_result?.results || [])
        .find((r) => r.broker === name);
      const errorHtml = b.error
        ? `<div class="alert alert-error" style="margin-top:10px">${esc(b.error)}</div>`
        : (result?.errors?.length
          ? `<div class="alert alert-error" style="margin-top:10px">${result.errors.map(esc).join('<br>')}</div>`
          : '');
      const acct = b.account
        ? `${esc(b.account.display_id)}${b.account.is_paper ? ' <span class="pill pill-ok">PAPER</span>' : ''}`
        : '<span class="muted">none</span>';
      const roBadge = b.read_only ? '<span class="badge-ro">READ-ONLY</span>' : '';

      // Position is whatever the broker reports and nothing else. Last shares
      // is history -- what most recently filled -- and is shown beside it so
      // the two are never confused for one another.
      const fills = lastFills[name] || {};
      const positionRows = symbols.map((sym) => {
        const held = qty(b, sym);
        const last = fills[sym];
        const tick = window.__lastLadder?.symbols?.[sym]?.tick;
        // Say when, and whether it was a sale: "950 @ $26.21" alone reads
        // like a current figure when it may be weeks old.
        const when = last?.at
          ? new Date(last.at * 1000).toLocaleDateString('en-US',
              { month: 'short', day: 'numeric', timeZone: 'America/New_York' })
          : '';
        const lastCell = last
          ? `${last.side === 'SELL' ? 'sold ' : ''}${fmtNum(last.shares)}
             <span class="muted">@ ${fmtPrice(last.price, tick)}${
               when ? ` · ${when}` : ''}</span>`
          : '<span class="muted">—</span>';
        return `<tr>
          <td>${esc(sym)}</td>
          <td class="num">${fmtNum(held)}</td>
          <td class="num">${lastCell}</td>
        </tr>`;
      }).join('');

      const orders = (b.open_orders || []).filter((o) => o.state !== 'FILLED');
      const orderRows = orders.length
        ? orders.map((o) => {
            const tick = window.__lastLadder?.symbols?.[o.symbol]?.tick;
            const filled = o.filled_quantity || 0;
            const q = filled > 0
              ? `${fmtNum(filled)} / ${fmtNum(o.quantity)}`
              : fmtNum(o.quantity);
            return `<tr>
              <td><span class="pill pill-${o.side === 'BUY' ? 'LONG' : 'SHORT'}">${esc(o.side)}</span></td>
              <td>${esc(o.symbol)}</td>
              <td class="num">${q}</td>
              <td class="num">${fmtPrice(o.limit_price, tick)}</td>
              <td>${esc(o.state)}</td>
            </tr>`;
          }).join('')
        : `<tr><td colspan="5" class="muted">no working orders</td></tr>`;

      return `<div class="panel">
        <div class="broker-head">
          <span class="broker-name">
            <span class="dot ${b.connected && !b.error ? 'dot-on' : 'dot-off'}"></span>${esc(name)}${roBadge}
          </span>
          <span class="pill pill-${b.exposure}">${b.exposure}</span>
        </div>
        <div class="kv"><span>Connection</span><span>${b.connected && !b.error ? 'connected' : 'disconnected'}</span></div>
        <div class="kv"><span>Account</span><span>${acct}</span></div>
        <div class="kv"><span>Cash available</span><span>${fmtMoney(b.balances?.cash_available)}</span></div>
        <div class="kv"><span>Buying power</span><span>${fmtMoney(b.balances?.buying_power)}</span></div>

        <h3 class="sub-head">Positions</h3>
        <table class="sub-table">
          <thead><tr><th>Symbol</th><th class="num">Held</th><th class="num">Last filled</th></tr></thead>
          <tbody>${positionRows}</tbody>
        </table>

        <h3 class="sub-head">
          Working orders
          <span class="pill ${orders.length ? 'pill-warn' : 'pill-UNKNOWN'}">${orders.length}</span>
        </h3>
        <table class="sub-table">
          <thead><tr><th>Side</th><th>Symbol</th><th class="num">Qty</th>
            <th class="num">Limit</th><th>State</th></tr></thead>
          <tbody>${orderRows}</tbody>
        </table>
        <p class="sub-note">
          A working order is not a position. Nothing is held until the broker
          reports the fill.
        </p>
        ${errorHtml}
      </div>`;
    })
    .join('');
}

function qty(broker, symbol) {
  const p = (broker.positions || []).find((x) => x.symbol === symbol);
  return p ? p.quantity : 0;
}

function renderHoldings(data) {
  const rows = [];
  for (const [name, b] of Object.entries(data.brokers || {})) {
    if (b.error) {
      rows.push(`<tr><td>${esc(name)}</td><td colspan="4" class="muted">${esc(b.error)}</td></tr>`);
      continue;
    }
    if (!b.positions?.length) {
      rows.push(`<tr><td>${esc(name)}</td><td class="muted">no positions</td><td>—</td><td>—</td><td>—</td></tr>`);
    }
    for (const p of b.positions || []) {
      rows.push(`<tr>
        <td>${esc(name)}</td>
        <td>${esc(p.symbol)}</td>
        <td>${fmtNum(p.quantity, 0)}</td>
        <td>${fmtMoney(p.average_price)}</td>
        <td>${fmtMoney(p.market_value)}</td>
      </tr>`);
    }
    rows.push(`<tr><td>${esc(name)}</td><td class="muted">CASH</td><td>—</td><td>—</td>
      <td>${fmtMoney(b.balances?.cash_available)}</td></tr>`);
  }
  $('holdings').innerHTML = `<table>
    <thead><tr><th>Broker</th><th>Symbol</th><th>Qty</th><th>Avg price</th><th>Value</th></tr></thead>
    <tbody>${rows.join('')}</tbody></table>`;
}

const MAX_EVENTS_SHOWN = 25;

function renderEvents(events) {
  $('events').innerHTML = events
    .slice()
    .reverse()
    .slice(0, MAX_EVENTS_SHOWN)
    .map((e) => {
      const time = new Date(e.ts * 1000).toLocaleTimeString();
      const detail = summarise(e);
      return `<div class="event">
        <span class="event-ts">${time}</span>
        <span class="event-name">${esc(e.broker ? e.broker + '·' : '')}${esc(e.event)}</span>
        <span class="event-detail">${esc(detail)}</span>
      </div>`;
    })
    .join('') || '<div class="muted">no events yet</div>';
}

function summarise(e) {
  const d = e.data || {};
  if (d.quote) return `${d.symbol} bid ${d.quote.bid} ask ${d.quote.ask} (${d.quote.spread_bps} bps)`;
  if (d.order) return `${d.order.side} ${d.order.quantity} ${d.order.symbol} @ ${d.order.limit_price} → ${d.order.state}`;
  if (d.fill) return `${d.fill.side} ${d.fill.filled_quantity}/${d.fill.requested_quantity} ${d.fill.symbol} @ ${d.fill.average_price}`;
  if (d.sizing) return `${d.symbol}: ${d.sizing.quantity} shares, est ${d.sizing.estimated_cost}`;
  if (d.status) return `status ${d.status}`;
  if (d.message) return d.message;
  if (d.reason) return d.reason;
  if (d.summary) return d.summary;
  if (d.exposure) return `exposure ${d.exposure} (${d.label || ''})`;
  return JSON.stringify(d).slice(0, 200);
}

// --- ladder -----------------------------------------------------------------

function renderLadder(ladder) {
  if (!ladder.enabled) {
    $('ladder').innerHTML =
      '<div class="muted">Ladder disabled (LADDER_ENABLED=false).</div>';
    $('ladder-note').textContent = '';
    return;
  }

  const accounts = ladder.accounts || {};
  // Which price each account already has working, so a rung can show that
  // clicking it re-prices rather than adds.
  const workingBySymbol = {};
  for (const [name, acct] of Object.entries(accounts)) {
    for (const o of acct.open_orders || []) {
      if (o.side !== 'BUY') continue;
      (workingBySymbol[o.symbol] ||= []).push({ account: name, price: o.limit_price });
    }
  }

  const cards = Object.values(ladder.symbols || {}).map((entry) => {
    const sym = entry.symbol;
    const q = entry.quote || {};
    const tick = entry.tick;
    const working = workingBySymbol[sym] || [];

    if (entry.error && !(entry.rungs || []).length) {
      return `<div class="panel ladder-card">
        <div class="broker-head"><span class="broker-name">${esc(sym)}</span></div>
        <div class="alert alert-warn">${esc(entry.error)}</div>
      </div>`;
    }

    // Session structure: not just where the extremes are, but when, and what
    // price has done since each one.
    const ss = entry.session || {};
    const hhmm = (ts) => ts
      ? new Date(ts * 1000).toLocaleTimeString('en-US',
          { hour: 'numeric', minute: '2-digit', timeZone: 'America/New_York' })
        + ' ET'
      : '—';
    const level = (label, value, at, extra = '') =>
      `<div class="kv session-kv">
        <span>${label}</span>
        <span class="num">${fmtPrice(value || null, tick)}
          <span class="muted session-at">${at ? `at ${esc(hhmm(at))}` : ''}</span>
          ${extra}</span>
      </div>`;

    // Which extreme is the live one: after a low, the run-up is what matters.
    const upLeg = ss.low_came_first === true;
    const sessionHtml = ss.complete
      ? `${level('High of day', ss.day_high, ss.day_high_at)}
         ${level('Low of day', ss.day_low, ss.day_low_at)}
         ${level('High since low', ss.high_since_low, ss.high_since_low_at,
             upLeg ? '<span class="pill pill-anchor">LEG</span>' : '')}
         ${level('Low since high', ss.low_since_high, ss.low_since_high_at,
             upLeg === false ? '<span class="pill pill-anchor">LEG</span>' : '')}
         <div class="sub-note">
           From ${ss.bars_used} bar(s) this session; times are exact to
           ${ss.resolution_seconds >= 3600
             ? `the ${ss.resolution_seconds / 3600}h`
             : `${ss.resolution_seconds / 60} min`}.
           ${ss.note ? esc(ss.note) : ''}
         </div>`
      : `<div class="alert alert-warn">${esc(ss.note || 'no session data yet')}</div>`;

    const workingNote = working.length
      ? `<div class="alert alert-warn ladder-working">
           Working: ${working.map((w) =>
             `${esc(w.account)} @ ${fmtPrice(w.price, tick)}`).join(', ')}
           — clicking a price re-prices ${working.length > 1 ? 'these orders' : 'this order'}.
         </div>`
      : '';

    // Typed-in price: defaults to the bid, survives the 10s redraw.
    const ask = q.ask || 0;
    const draft = priceDrafts[sym];
    const startPrice = draft ?? (q.bid || q.last
      ? snapToTick(q.bid || q.last, tick).toFixed(priceDecimals(tick)) : '');
    const entryHtml = `<div class="order-entry">
        <div class="order-row">
          <button class="small step-btn" data-symbol="${esc(sym)}" data-dir="-1"
            title="Down one tick">−</button>
          <input class="price-input" data-symbol="${esc(sym)}" data-tick="${tick}"
            inputmode="decimal" autocomplete="off" value="${esc(startPrice)}">
          <button class="small step-btn" data-symbol="${esc(sym)}" data-dir="1"
            title="Up one tick">+</button>
          <button class="small buy-btn" data-symbol="${esc(sym)}">Buy</button>
          <button class="small reset-btn" data-symbol="${esc(sym)}"
            title="Reset to the bid">Bid</button>
        </div>
        <div class="order-hint" id="hint-${esc(sym)}"></div>
        <button class="small buy-now-btn" data-symbol="${esc(sym)}"
          data-price="${ask}" data-tick="${tick}" ${ask ? '' : 'disabled'}>
          Buy now @ ask ${fmtPrice(ask || null, tick)}</button>
      </div>`;

    // Only prices that wait are listed; anything at or above the ask is the
    // same action as "Buy now", so it is not repeated as a rung.
    const allRungs = entry.rungs || [];
    const restingRungs = allRungs.filter((r) => !r.marketable);
    const hiddenCount = allRungs.length - restingRungs.length;

    const rows = restingRungs.map((r) => {
      const isWorking = working.some(
        (w) => Math.abs(w.price - r.price) < (tick || 0.01) / 2);
      const badges = [
        r.offset_steps === 0
          ? `<span class="pill pill-anchor">${esc(r.anchor)}</span>` : '',
        r.marketable ? '<span class="pill pill-warn">FILLS NOW</span>' : '',
        isWorking ? '<span class="pill pill-ok">WORKING</span>' : '',
      ].filter(Boolean).join(' ');
      const cls = ['rung', r.marketable ? 'rung-marketable' : '',
        isWorking ? 'rung-working' : ''].filter(Boolean).join(' ');
      return `<button class="${cls}"
          data-symbol="${esc(sym)}" data-price="${r.price}" data-tick="${tick}">
        <span class="rung-price">${fmtPrice(r.price, tick)}</span>
        <span class="rung-label">${esc(r.label)}</span>
        <span class="rung-badges">${badges}</span>
      </button>`;
    }).join('');

    return `<div class="panel ladder-card">
      <div class="broker-head">
        <span class="broker-name">${esc(sym)}</span>
        <span class="muted">step ${fmtPrice(entry.step, tick)}</span>
      </div>
      ${sessionHtml}
      <div class="kv"><span>Last</span>
        <span class="num">${fmtPrice(q.last, tick)}</span></div>
      <div class="kv"><span>Bid / Ask</span>
        <span class="num">${fmtPrice(q.bid, tick)} / ${fmtPrice(q.ask, tick)}</span></div>
      ${entryHtml}
      ${workingNote}
      <div class="rungs">${rows || '<div class="muted">no resting rungs</div>'}</div>
      ${hiddenCount ? `<div class="sub-note">${hiddenCount} rung(s) at or above
        the ask hidden — they would fill now; use Buy now instead.</div>` : ''}
    </div>`;
  }).join('');

  // Redrawing replaces the input; put the cursor back if it was in one.
  const focused = document.activeElement;
  const focusSym = focused?.classList?.contains('price-input')
    ? focused.dataset.symbol : null;
  const caret = focusSym ? focused.selectionStart : null;

  $('ladder').innerHTML = cards || '<div class="muted">no symbols</div>';
  $('ladder-note').textContent = `${ladder.levels} level(s) each side of high and low`;

  for (const btn of document.querySelectorAll('.rung, .buy-now-btn')) {
    btn.addEventListener('click', (e) => {
      const el = e.currentTarget;
      sendLadderOrder(el.dataset.symbol, Number(el.dataset.price),
        Number(el.dataset.tick));
    });
  }
  for (const input of document.querySelectorAll('.price-input')) {
    const sym = input.dataset.symbol;
    updateOrderHint(sym);
    input.addEventListener('input', () => {
      priceDrafts[sym] = input.value;
      updateOrderHint(sym);
    });
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') buyTypedPrice(sym);
      else if (e.key === 'ArrowUp') { e.preventDefault(); stepPrice(sym, 1); }
      else if (e.key === 'ArrowDown') { e.preventDefault(); stepPrice(sym, -1); }
    });
    if (sym === focusSym) {
      input.focus();
      if (caret !== null) input.setSelectionRange(caret, caret);
    }
  }
  for (const btn of document.querySelectorAll('.step-btn')) {
    btn.addEventListener('click', (e) => {
      const el = e.currentTarget;
      stepPrice(el.dataset.symbol, Number(el.dataset.dir));
    });
  }
  for (const btn of document.querySelectorAll('.buy-btn')) {
    btn.addEventListener('click', (e) => buyTypedPrice(e.currentTarget.dataset.symbol));
  }
  for (const btn of document.querySelectorAll('.reset-btn')) {
    btn.addEventListener('click', (e) => {
      delete priceDrafts[e.currentTarget.dataset.symbol];
      renderLadder(window.__lastLadder);
    });
  }
}

// --- typed-in price ---------------------------------------------------------

/** What the user has typed per symbol; absent means "use the bid". */
const priceDrafts = {};

const priceDecimals = (tick) => (tick && tick < 0.01 ? 3 : 2);

function snapToTick(price, tick) {
  const t = tick > 0 ? tick : 0.01;
  return Math.round(price / t) * t;
}

function priceInput(sym) {
  return document.querySelector(`.price-input[data-symbol="${CSS.escape(sym)}"]`);
}

function stepPrice(sym, dir) {
  const input = priceInput(sym);
  if (!input) return;
  const tick = Number(input.dataset.tick) || 0.01;
  const current = Number(input.value);
  const base = Number.isFinite(current) && current > 0
    ? current : (window.__lastLadder?.symbols?.[sym]?.quote?.bid || 0);
  if (!base) return;
  const next = snapToTick(base + dir * tick, tick);
  if (next <= 0) return;
  input.value = next.toFixed(priceDecimals(tick));
  priceDrafts[sym] = input.value;
  updateOrderHint(sym);
}

/** Say what the typed price will do before anything is clicked. */
function updateOrderHint(sym) {
  const hint = $(`hint-${sym}`);
  const input = priceInput(sym);
  if (!hint || !input) return;
  const tick = Number(input.dataset.tick) || 0.01;
  const price = Number(input.value);
  const q = window.__lastLadder?.symbols?.[sym]?.quote || {};
  hint.className = 'order-hint';
  if (!input.value.trim() || !Number.isFinite(price) || price <= 0) {
    hint.textContent = 'Enter a price.';
    hint.classList.add('order-hint-bad');
  } else if (q.ask && price >= q.ask) {
    hint.textContent = `Fills now at the market — ask is ${fmtPrice(q.ask, tick)}.`;
    hint.classList.add('order-hint-warn');
  } else {
    const ref = q.last || q.bid;
    const pct = ref ? ` (${(((price - ref) / ref) * 100).toFixed(2)}% from last)` : '';
    hint.textContent = `Rests until the price comes down to ${fmtPrice(price, tick)}${pct}.`;
  }
}

function buyTypedPrice(sym) {
  const input = priceInput(sym);
  if (!input) return;
  const tick = Number(input.dataset.tick) || 0.01;
  const typed = Number(input.value);
  if (!Number.isFinite(typed) || typed <= 0) {
    updateOrderHint(sym);
    input.focus();
    return;
  }
  // Brokers reject off-grid prices; snap and show the snapped value.
  const price = Number(snapToTick(typed, tick).toFixed(6));
  input.value = price.toFixed(priceDecimals(tick));
  priceDrafts[sym] = input.value;
  sendLadderOrder(sym, price, tick);
}

async function sendLadderOrder(symbol, price, tick) {
  if (busy) return;
  const requestId = `LADDER-${symbol}-${price}-${Date.now()}-${
    Math.random().toString(36).slice(2, 8)}`;
  setBusy(true);
  try {
    const body = { symbol, limit_price: price, request_id: requestId };
    let res = await api('/api/ladder/order', {
      method: 'POST', body: JSON.stringify(body),
    });

    if (res.status === 409 && res.body.status === 'CONFIRMATION_REQUIRED') {
      const ok = await askConfirm(
        `Buy ${symbol} at ${fmtPrice(price, tick)}`,
        ladderConfirmBody(symbol, price, tick, res.body),
      );
      if (!ok) { setBusy(false); return; }
      res = await api('/api/ladder/order', {
        method: 'POST',
        body: JSON.stringify({ ...body, confirm_token: res.body.confirm_token }),
      });
    }
    reportLadderResult(symbol, price, tick, res);
  } catch (err) {
    setAlerts([{ level: 'error', html: esc(String(err)) }]);
  } finally {
    setBusy(false);
    refresh();
  }
}

/** Say plainly what the click will do before it is confirmed. */
function ladderConfirmBody(symbol, price, tick, body) {
  const lines = [`<p>${esc(body.detail || '')}</p>`];
  const working = window.__lastLadder?.accounts || {};
  const existing = [];
  for (const [name, acct] of Object.entries(working)) {
    for (const o of acct.open_orders || []) {
      if (o.symbol === symbol && o.side === 'BUY') {
        existing.push(`${esc(name)}: ${fmtNum(o.remaining_quantity ?? o.quantity)}
          @ ${fmtPrice(o.limit_price, tick)}`);
      }
    }
  }
  if (existing.length) {
    lines.push(`<p class="confirm-warn"><strong>This re-prices a working order.</strong>
      No new order is added.<br>${existing.join('<br>')}
      <br>→ ${fmtPrice(price, tick)}</p>`);
  } else {
    lines.push(`<p><strong>This places a new order</strong> sized against the
      whole account at ${fmtPrice(price, tick)}.</p>`);
  }

  const entry = window.__lastLadder?.symbols?.[symbol];
  const ask = entry?.quote?.ask;
  if (ask && price >= ask) {
    lines.push(`<p class="confirm-warn"><strong>This fills immediately.</strong>
      ${esc(symbol)} is offered at ${fmtPrice(ask, tick)}, so a buy limit at
      ${fmtPrice(price, tick)} crosses the spread and executes now at the
      market — it will not wait at this price.</p>`);
  }
  lines.push('<p class="muted">A filled order becomes a position only when the '
    + 'broker reports it. The dashboard will not show a position until then.</p>');
  return lines.join('');
}

function reportLadderResult(symbol, price, tick, res) {
  const b = res.body || {};
  if (b.status === 'OK' || b.status === 'PARTIAL') {
    const lines = (b.results || []).map((r) => {
      if (!r.ok) {
        return `${esc(r.broker)}: FAILED — ${(r.errors || []).map(esc).join('; ')}`;
      }
      const verb = { PLACED: 'placed', MODIFIED: 're-priced', RESIZED: 're-priced (resized)',
        UNCHANGED: 'already at this price' }[r.action] || r.action;
      const warn = (r.warnings || []).length
        ? `<br><span class="muted">${r.warnings.map(esc).join('<br>')}</span>` : '';
      return `${esc(r.broker)}: ${verb} ${fmtNum(r.quantity)} ${esc(symbol)}
        @ ${fmtPrice(r.limit_price, tick)}${warn}`;
    }).join('<br>');
    setAlerts([{
      level: b.status === 'OK' ? 'ok' : 'error',
      html: `<strong>${esc(symbol)} @ ${fmtPrice(price, tick)}
        ${b.status === 'OK' ? 'done' : 'PARTIAL — accounts differ'}.</strong>
        <br>${lines}`,
    }]);
    return;
  }
  const detail = b.detail
    || (b.results || []).flatMap((r) => r.errors || []).map(esc).join('<br>')
    || JSON.stringify(b).slice(0, 400);
  setAlerts([{
    level: 'error',
    html: `<strong>${esc(symbol)} @ ${fmtPrice(price, tick)} — ${
      esc(b.status || res.status)}</strong><br>${detail}`,
  }]);
}

// --- candles ----------------------------------------------------------------
// Hourly OHLC including extended hours. The server keeps 90 days cached and
// only re-fetches the recent edge, so changing range here is free -- it is a
// slice of data already in the browser, not a new request.

// Which US session a bar belongs to. Done in Eastern regardless of where the
// browser is, because 09:30-16:00 is defined in Eastern, not locally.
const ET_PARTS = new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/New_York', hour: '2-digit', minute: '2-digit', hour12: false,
});

function etMinutes(ts) {
  const parts = ET_PARTS.formatToParts(new Date(ts * 1000));
  const h = Number(parts.find((p) => p.type === 'hour').value);
  const m = Number(parts.find((p) => p.type === 'minute').value);
  return h * 60 + m;
}

const REGULAR_OPEN_MIN = 9 * 60 + 30;
const REGULAR_CLOSE_MIN = 16 * 60;
const isRegularSession = (ts) => {
  const m = etMinutes(ts);
  return m >= REGULAR_OPEN_MIN && m < REGULAR_CLOSE_MIN;
};

const RANGES = [
  { label: '1D', days: 1 },
  { label: '5D', days: 5 },
  { label: '1M', days: 30 },
  { label: '3M', days: 90 },
];
let rangeDays = 5;
let candleData = {};
let expandedSymbol = null;
let chartSymbols = null;

function renderRangePicker() {
  $('range-picker').innerHTML = RANGES.map((r) =>
    `<button class="small range-btn ${r.days === rangeDays ? 'range-on' : ''}"
       data-days="${r.days}">${r.label}</button>`).join('');
  for (const btn of document.querySelectorAll('.range-btn')) {
    btn.addEventListener('click', (e) => {
      rangeDays = Number(e.currentTarget.dataset.days);
      renderRangePicker();
      renderCandles();
    });
  }
}

function candleSymbols() {
  return chartSymbols && chartSymbols.length
    ? chartSymbols
    : [longSymbol, shortSymbol];
}

async function refreshCandles(refresh = true) {
  if (!TOKEN) return;
  try {
    const results = await Promise.all(
      candleSymbols().map((sym) =>
        api(`/api/candles?symbol=${encodeURIComponent(sym)}&refresh=${refresh}`)
          .then((r) => [sym, r.body])
          .catch(() => [sym, null])),
    );
    for (const [sym, body] of results) if (body) candleData[sym] = body;
    const days = Object.values(candleData)[0]?.days;
    if (days) $('candle-days').textContent = days;
    renderCandles();
  } catch (err) {
    console.error(err);
  }
}

function renderCandles() {
  const symbols = candleSymbols();
  const host = $('candles');
  const charts = symbols.map((sym) => {
    const data = candleData[sym];
    if (!data) return `<div class="candle-block"><h3 class="sub-head">${esc(sym)}</h3>
      <div class="muted">loading…</div></div>`;
    const err = (data.refresh?.errors || []).length
      ? `<div class="alert alert-warn">${data.refresh.errors.map(esc).join('<br>')}</div>`
      : '';
    const isOpen = expandedSymbol === sym;
    const refBadge = (chartSymbols || []).includes(sym)
      && sym !== longSymbol && sym !== shortSymbol
      ? '<span class="pill pill-anchor">REFERENCE</span>' : '';
    return `<div class="candle-block${isOpen ? ' candle-expanded' : ''}"
        data-block="${esc(sym)}">
      <h3 class="sub-head">${esc(sym)}${refBadge}
        <span class="muted">${(data.bars || []).length} bars cached</span>
        <button class="small expand-btn" data-expand="${esc(sym)}">
          ${isOpen ? '✕ Close' : '⤢ Full screen'}</button>
      </h3>
      ${err}
      ${candleChart(sym, data)}
    </div>`;
  }).join('');
  host.innerHTML = charts;
  host.classList.toggle('has-expanded', expandedSymbol !== null);

  for (const btn of host.querySelectorAll('.expand-btn')) {
    btn.addEventListener('click', (e) => {
      const sym = e.currentTarget.dataset.expand;
      expandedSymbol = expandedSymbol === sym ? null : sym;
      renderCandles();
    });
  }
  for (const sym of symbols) attachCandleCrosshair(sym);
}

// Esc leaves the expanded chart, which is what a full-screen view should do.
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && expandedSymbol !== null) {
    expandedSymbol = null;
    renderCandles();
  }
});

/** Candlestick SVG for one symbol over the selected range. */
function candleChart(symbol, data) {
  const cutoff = Date.now() / 1000 - rangeDays * 86400;
  const bars = (data.bars || []).filter((b) => b[0] >= cutoff);
  if (bars.length < 2) {
    return '<div class="muted" style="padding:20px 0">Not enough cached data '
      + 'for this range yet.</div>';
  }

  // A taller viewBox when expanded: the SVG stretches to its container, so
  // more height here buys real vertical resolution rather than a scale-up.
  const isOpen = expandedSymbol === symbol;
  const W = 1000, H = isOpen ? 560 : 300;
  const M = { top: 10, right: 58, bottom: 22, left: 8 };
  const plotW = W - M.left - M.right;
  const plotH = H - M.top - M.bottom;

  let lo = Math.min(...bars.map((b) => b[3]));
  let hi = Math.max(...bars.map((b) => b[2]));
  const pad = Math.max((hi - lo) * 0.06, 0.01);
  lo -= pad; hi += pad;

  // Bars are laid out by index, not by clock time. Extended-hours sessions
  // leave real gaps overnight and at weekends; spacing by time would render
  // those as wide dead bands and squeeze the actual trading into slivers.
  const slot = plotW / bars.length;
  const bodyW = Math.max(1, Math.min(isOpen ? 16 : 9, slot * 0.7));
  const cx = (i) => M.left + slot * (i + 0.5);
  const py = (v) => M.top + plotH - ((v - lo) / (hi - lo)) * plotH;

  const ticks = 4;
  const grid = Array.from({ length: ticks + 1 }, (_, i) => {
    const value = lo + ((hi - lo) * i) / ticks;
    const y = py(value);
    return `<line class="grid-line" x1="${M.left}" y1="${y}" x2="${M.left + plotW}" y2="${y}"/>
      <text class="axis-text" x="${M.left + plotW + 6}" y="${y + 3.5}"
        text-anchor="start">${value.toFixed(2)}</text>`;
  }).join('');

  // Extended-hours bars are in the data but look identical to regular ones
  // without this, which reads as "extended hours are missing".
  const extended = bars.map((b) => !isRegularSession(b[0]));
  const bands = [];
  let runStart = null;
  for (let i = 0; i <= bars.length; i++) {
    if (i < bars.length && extended[i]) {
      if (runStart === null) runStart = i;
    } else if (runStart !== null) {
      bands.push(`<rect class="session-band" x="${(M.left + slot * runStart).toFixed(2)}"
        y="${M.top}" width="${(slot * (i - runStart)).toFixed(2)}"
        height="${plotH}"/>`);
      runStart = null;
    }
  }
  const extendedCount = extended.filter(Boolean).length;

  const candles = bars.map((b, i) => {
    const [, o, h, l, c] = b;
    const up = c >= o;
    const cls = `${up ? 'candle-up' : 'candle-down'}${extended[i] ? ' candle-ext' : ''}`;
    const x = cx(i);
    const top = py(Math.max(o, c));
    const bottom = py(Math.min(o, c));
    const height = Math.max(1, bottom - top);
    return `<line class="wick ${cls}" vector-effect="non-scaling-stroke"
        x1="${x.toFixed(2)}" y1="${py(h).toFixed(2)}"
        x2="${x.toFixed(2)}" y2="${py(l).toFixed(2)}"/>
      <rect class="body ${cls}" vector-effect="non-scaling-stroke"
        x="${(x - bodyW / 2).toFixed(2)}" y="${top.toFixed(2)}"
        width="${bodyW.toFixed(2)}" height="${height.toFixed(2)}"/>`;
  }).join('');

  const first = new Date(bars[0][0] * 1000);
  const last = new Date(bars[bars.length - 1][0] * 1000);
  const stamp = (d) => d.toLocaleString('en-US',
    { month: 'short', day: 'numeric', hour: 'numeric' });

  return `<figure class="chart-figure candle-figure" data-symbol="${esc(symbol)}">
    <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img"
        aria-label="${esc(symbol)} hourly candles">
      ${bands.join('')}${grid}${candles}
      <line class="crosshair candle-cross" y1="${M.top}" y2="${M.top + plotH}"
        style="display:none"/>
      <rect class="candle-hit" x="${M.left}" y="${M.top}" width="${plotW}"
        height="${plotH}" fill="transparent"/>
    </svg>
    <div class="chart-tip candle-tip" style="display:none"></div>
    <figcaption class="muted">${esc(stamp(first))} → ${esc(stamp(last))}
      · ${bars.length} × ${esc(data.bar_size || '1h')} bars
      · <span class="band-key"></span> ${extendedCount} extended-hours
      (${bars.length - extendedCount} regular session)</figcaption>
  </figure>`;
}

function attachCandleCrosshair(symbol) {
  const fig = document.querySelector(`.candle-figure[data-symbol="${symbol}"]`);
  if (!fig) return;
  const svg = fig.querySelector('svg');
  const hit = fig.querySelector('.candle-hit');
  const line = fig.querySelector('.candle-cross');
  const tip = fig.querySelector('.candle-tip');
  if (!svg || !hit) return;

  const cutoff = Date.now() / 1000 - rangeDays * 86400;
  const bars = (candleData[symbol]?.bars || []).filter((b) => b[0] >= cutoff);
  if (!bars.length) return;
  const W = 1000, M = { left: 8, right: 58 };   // must match candleChart
  const plotW = W - M.left - M.right;
  const slot = plotW / bars.length;

  hit.addEventListener('mouseleave', () => {
    line.style.display = 'none'; tip.style.display = 'none';
  });
  hit.addEventListener('mousemove', (event) => {
    const box = svg.getBoundingClientRect();
    const svgX = (event.clientX - box.left) * (W / box.width);
    const i = Math.max(0, Math.min(bars.length - 1,
      Math.floor((svgX - M.left) / slot)));
    const [ts, o, h, l, c, v] = bars[i];
    const x = M.left + slot * (i + 0.5);
    line.setAttribute('x1', x); line.setAttribute('x2', x);
    line.style.display = '';
    const when = new Date(ts * 1000).toLocaleString('en-US',
      { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
    tip.innerHTML = `<div class="tip-date">${esc(when)}</div>
      <div class="tip-row"><span>O</span><span class="tip-value">${o.toFixed(2)}</span></div>
      <div class="tip-row"><span>H</span><span class="tip-value">${h.toFixed(2)}</span></div>
      <div class="tip-row"><span>L</span><span class="tip-value">${l.toFixed(2)}</span></div>
      <div class="tip-row"><span>C</span><span class="tip-value">${c.toFixed(2)}</span></div>
      <div class="tip-row"><span>Vol</span><span class="tip-value">${fmtNum(v)}</span></div>`;
    tip.style.display = '';
    const left = (x / W) * box.width;
    tip.style.left = `${Math.min(Math.max(left + 12, 0), box.width - tip.offsetWidth - 4)}px`;
    tip.style.top = '8px';
  });
}

// --- performance -------------------------------------------------------------
// Series colours are the validated categorical slots 1-3 stepped for this dark
// surface. They passed the lightness/chroma/CVD/normal-vision/contrast checks
// on the all-pairs list; swapping them by eye would break that.
const SERIES_COLORS = ['#3987e5', '#d95926', '#199e70'];
const WINDOW_LABELS = ['1M', '2M', '3M', '6M'];

const fmtPct = (v) => (v === null || v === undefined ? '—'
  : `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`);

function returnClass(w) {
  if (!w || w.twr_pct === null || w.twr_pct === undefined) return 'ret-none';
  const base = w.twr_pct >= 0 ? 'ret-pos' : 'ret-neg';
  return w.complete ? base : `${base} ret-partial`;
}

function renderReturns(perf) {
  const labels = perf.account_labels || {};
  const rows = [];

  const row = (name, sub, windows, extraClass = '') => {
    const cells = WINDOW_LABELS.map((label) => {
      const w = (windows || []).find((x) => x.label === label);
      const title = w?.note ? ` title="${esc(w.note)}"` : '';
      return `<div class="returns-cell ${returnClass(w)}"${title}>${
        fmtPct(w?.twr_pct ?? null)}</div>`;
    }).join('');
    rows.push(`<div class="returns-row ${extraClass}">
      <div class="returns-name">${name}${sub ? `<small>${sub}</small>` : ''}</div>
      ${cells}</div>`);
  };

  rows.push(`<div class="returns-row returns-head">
    <div>Account</div>${WINDOW_LABELS.map((l) => `<div>${l}</div>`).join('')}
  </div>`);

  for (const account of perf.accounts || []) {
    const meta = labels[account.broker] || {};
    const badge = meta.read_only ? '<span class="badge-ro">READ-ONLY</span>' : '';
    const nav = account.current_nav !== null ? fmtMoney(account.current_nav) : '—';
    row(`${esc(account.broker)}${badge}`, `${esc(meta.display_id || '')} · ${nav}`,
      account.windows);
  }

  for (const group of perf.by_broker || []) {
    if ((group.accounts || []).length > 1) {
      row(`${esc(group.broker_type)} — all accounts`,
        `${group.accounts.length} accounts · ${fmtMoney(group.current_nav)}`,
        group.windows);
    }
  }

  row('<strong>Combined</strong>',
    `every account · ${fmtMoney(perf.combined?.current_nav)}`,
    perf.combined?.windows, 'returns-total');

  $('returns').innerHTML = `<div class="returns-grid">${rows.join('')}</div>`;

  const notes = new Set();
  let anyPartial = false;
  for (const source of [...(perf.accounts || []), perf.combined || {}]) {
    for (const w of source.windows || []) {
      if (w.note) notes.add(w.note);
      if (w.twr_pct !== null && !w.complete) anyPartial = true;
    }
  }
  const lines = [];
  if (anyPartial) {
    lines.push('<strong>*</strong> This period is only partly covered — the '
      + 'history does not reach that far back yet.');
  }
  for (const note of notes) lines.push(esc(note));
  $('perf-notes').innerHTML = lines.length
    ? `<div class="alert alert-warn" style="margin-top:12px">${lines.join('<br>')}</div>`
    : '';
}

/**
 * Series to plot. One line per broker family plus the combined portfolio —
 * at most three, which is the validated cap for this palette.
 */
function chartSeries(perf) {
  const out = [];
  for (const group of perf.by_broker || []) {
    if ((group.series || []).length > 1) {
      out.push({ name: group.broker_type, points: group.series });
    }
  }
  const combined = perf.combined?.series || [];
  if (combined.length > 1 && out.length !== 1) {
    out.push({ name: 'combined', points: combined });
  }
  return out.slice(0, 3).map((s, i) => ({ ...s, color: SERIES_COLORS[i] }));
}

function renderChart(perf) {
  const host = $('nav-chart');
  const series = chartSeries(perf);
  $('chart-legend').innerHTML = series
    .map((s) => `<span class="legend-item">
      <span class="legend-swatch" style="background:${s.color}"></span>${esc(s.name)}
    </span>`).join('');

  if (!series.length) {
    host.innerHTML = '<div class="muted" style="padding:24px 0">'
      + 'No history yet. Account value is recorded once a day from now on, and '
      + 'the dashboard backfills what your brokers can supply.</div>';
    $('chart-caption').textContent = '';
    return;
  }

  // Index every series to 100 at its first point. Accounts differ hugely in
  // size; indexing puts them on ONE axis instead of reaching for a second.
  const dates = [...new Set(series.flatMap((s) => s.points.map((p) => p.date)))].sort();
  const xOf = new Map(dates.map((d, i) => [d, i]));
  const indexed = series.map((s) => {
    const base = s.points[0].nav || 1;
    return {
      ...s,
      values: s.points.map((p) => ({
        date: p.date, x: xOf.get(p.date), y: (p.nav / base) * 100, nav: p.nav,
      })),
    };
  });

  const all = indexed.flatMap((s) => s.values.map((v) => v.y));
  let lo = Math.min(...all, 100);
  let hi = Math.max(...all, 100);
  const pad = Math.max((hi - lo) * 0.12, 0.5);
  lo -= pad; hi += pad;

  const W = 800, H = 240;
  const M = { top: 12, right: 62, bottom: 26, left: 46 };
  const plotW = W - M.left - M.right;
  const plotH = H - M.top - M.bottom;
  const px = (x) => M.left + (dates.length > 1 ? (x / (dates.length - 1)) * plotW : plotW / 2);
  const py = (y) => M.top + plotH - ((y - lo) / (hi - lo)) * plotH;

  const ticks = 4;
  const gridlines = Array.from({ length: ticks + 1 }, (_, i) => {
    const value = lo + ((hi - lo) * i) / ticks;
    const y = py(value);
    return `<line class="grid-line" x1="${M.left}" y1="${y}" x2="${M.left + plotW}" y2="${y}"/>
      <text class="axis-text" x="${M.left - 8}" y="${y + 3.5}" text-anchor="end">${value.toFixed(1)}</text>`;
  }).join('');

  const baseY = py(100);
  const baseline = baseY >= M.top && baseY <= M.top + plotH
    ? `<line class="baseline" x1="${M.left}" y1="${baseY}" x2="${M.left + plotW}" y2="${baseY}"/>` : '';

  const xLabels = [0, Math.floor((dates.length - 1) / 2), dates.length - 1]
    .filter((i, idx, arr) => i >= 0 && arr.indexOf(i) === idx)
    .map((i) => `<text class="axis-text" x="${px(i)}" y="${H - 8}" text-anchor="${
      i === 0 ? 'start' : i === dates.length - 1 ? 'end' : 'middle'}">${dates[i]}</text>`)
    .join('');

  // Direct labels sit beside each line's final point. Where two lines end
  // close together the labels would overlap, so push them apart vertically
  // and keep the stack inside the plot. The marker stays on the true value;
  // only the text moves.
  const LABEL_GAP = 12;
  const ends = indexed
    .map((s, i) => ({ i, y: py(s.values[s.values.length - 1].y) }))
    .sort((a, b) => a.y - b.y);
  for (let k = 1; k < ends.length; k++) {
    if (ends[k].y - ends[k - 1].y < LABEL_GAP) ends[k].y = ends[k - 1].y + LABEL_GAP;
  }
  const overflow = ends.length ? ends[ends.length - 1].y - (M.top + plotH) : 0;
  if (overflow > 0) for (const e of ends) e.y -= overflow;
  const labelY = new Map(ends.map((e) => [e.i, e.y]));

  const paths = indexed.map((s, i) => {
    const dAttr = s.values
      .map((v, j) => `${j === 0 ? 'M' : 'L'}${px(v.x).toFixed(1)},${py(v.y).toFixed(1)}`)
      .join(' ');
    const last = s.values[s.values.length - 1];
    const ty = labelY.get(i);
    // A leader line when the label had to move away from its point.
    const leader = Math.abs(ty - py(last.y)) > 2
      ? `<line class="grid-line" x1="${px(last.x) + 4}" y1="${py(last.y)}"
               x2="${px(last.x) + 7}" y2="${ty - 3}"/>`
      : '';
    return `<path class="series-line" d="${dAttr}" stroke="${s.color}"/>
      ${leader}
      <circle class="series-end" cx="${px(last.x)}" cy="${py(last.y)}" r="4" fill="${s.color}"/>
      <text class="series-label" x="${px(last.x) + 8}" y="${ty + 3.5}"
            fill="${s.color}">${esc(s.name)}</text>`;
  }).join('');

  host.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"
      role="img" aria-label="Account value indexed to 100">
    ${gridlines}${baseline}${xLabels}${paths}
    <line id="crosshair" class="crosshair" y1="${M.top}" y2="${M.top + plotH}" style="display:none"/>
    <rect id="hit" x="${M.left}" y="${M.top}" width="${plotW}" height="${plotH}" fill="transparent"/>
  </svg><div id="chart-tip" class="chart-tip" style="display:none"></div>`;

  $('chart-caption').textContent =
    `${dates[0]} to ${dates[dates.length - 1]} · ${dates.length} observations`;

  attachCrosshair(host, { dates, indexed, px, py, M, plotW, W });
}

function attachCrosshair(host, ctx) {
  const svg = host.querySelector('svg');
  const hit = host.querySelector('#hit');
  const line = host.querySelector('#crosshair');
  const tip = host.querySelector('#chart-tip');
  if (!svg || !hit) return;

  const hide = () => { line.style.display = 'none'; tip.style.display = 'none'; };

  hit.addEventListener('mouseleave', hide);
  hit.addEventListener('mousemove', (event) => {
    const box = svg.getBoundingClientRect();
    const scale = ctx.W / box.width;
    const svgX = (event.clientX - box.left) * scale;
    const ratio = (svgX - ctx.M.left) / ctx.plotW;
    const index = Math.max(0, Math.min(ctx.dates.length - 1,
      Math.round(ratio * (ctx.dates.length - 1))));
    const date = ctx.dates[index];

    line.setAttribute('x1', ctx.px(index));
    line.setAttribute('x2', ctx.px(index));
    line.style.display = '';

    const rows = ctx.indexed.map((s) => {
      const point = s.values.find((v) => v.date === date);
      if (!point) return '';
      return `<div class="tip-row">
        <span class="legend-swatch" style="background:${s.color}"></span>
        <span>${esc(s.name)}</span>
        <span class="tip-value">${point.y.toFixed(1)} · ${fmtMoney(point.nav)}</span>
      </div>`;
    }).join('');

    tip.innerHTML = `<div class="tip-date">${esc(date)}</div>${rows}`;
    tip.style.display = '';
    const left = (ctx.px(index) / ctx.W) * box.width;
    tip.style.left = `${Math.min(Math.max(left + 12, 0), box.width - tip.offsetWidth - 4)}px`;
    tip.style.top = '8px';
  });
}

// --- logins -----------------------------------------------------------------
// Schwab tokens die every 7 days and the IBKR gateway session can drop, so
// both logins live here rather than in a terminal. The Schwab consent page
// opens in its own tab; the server catches the redirect on the callback
// address and the panel follows along by polling /api/logins.

let loginsBusy = false;   // a login is in flight: poll faster

function loginPill(state) {
  return {
    ok: '<span class="pill pill-ok">OK</span>',
    expiring: '<span class="pill pill-warn">RENEW SOON</span>',
    expired: '<span class="pill pill-bad">EXPIRED</span>',
    missing: '<span class="pill pill-bad">NOT LOGGED IN</span>',
    unreadable: '<span class="pill pill-bad">UNREADABLE</span>',
    unknown: '<span class="pill pill-warn">AGE UNKNOWN</span>',
    rejected: '<span class="pill pill-bad">REJECTED — LOG IN AGAIN</span>',
    shared: '<span class="pill pill-bad">SAME ACCOUNT AS ANOTHER LOGIN</span>',
  }[state] || '';
}

function renderLogins(data) {
  const rows = [];
  const schwab = data.schwab || {};
  const flow = schwab.flow || {};
  const flowActive = flow.state === 'waiting' || flow.state === 'exchanging';

  if (schwab.enabled && !schwab.configured) {
    rows.push(`<div class="login-row"><span class="login-name">Schwab</span>
      <span class="login-detail muted">Set SCHWAB_APP_KEY and SCHWAB_APP_SECRET in .env first.</span></div>`);
  } else if (schwab.enabled) {
    for (const l of schwab.logins || []) {
      const left = l.detail ? esc(l.detail)
        : l.days_left === null || l.days_left === undefined
          ? '' : l.days_left > 0 ? `${l.days_left.toFixed(1)} days left` : '';
      const label = schwab.logins.length > 1 ? `Schwab · ${esc(l.login)}` : 'Schwab';
      rows.push(`<div class="login-row">
        <span class="login-name">${label}</span>
        <span class="login-detail">${loginPill(l.state)} <span class="muted">${left}</span></span>
        <button class="small" data-schwab-login="${esc(l.login)}" ${flowActive ? 'disabled' : ''}>
          ${l.state === 'ok' ? 'Log in again' : 'Log in'}</button>
      </div>`);
    }
  }

  const ibkr = data.ibkr || {};
  const restart = ibkr.restart || {};
  const restarting = restart.state === 'restarting';
  const loggingOut = restart.state === 'logging_out';
  if (ibkr.enabled) {
    // A stopped gateway is the normal logged-out state, not a fault.
    const stopped = ibkr.gateway_running === false;
    const pill = ibkr.connected
      ? '<span class="pill pill-ok">LOGGED IN</span>'
      : '<span class="pill pill-bad">NOT LOGGED IN</span>';
    const note = restart.state && restart.state !== 'idle'
      ? `<span class="muted">${esc(restart.message || '')}</span>`
      : stopped ? '<span class="muted">Gateway stopped — IBKR is free for another browser.</span>'
      : (!ibkr.connected && ibkr.error ? `<span class="muted">${esc(ibkr.error)}</span>` : '');
    // Log out whenever the gateway holds (or may hold) the session.
    const showLogout = !restarting && (ibkr.connected || loggingOut || !stopped);
    const button = restarting
      ? '<button class="small" disabled>Waiting for phone approval…</button>'
      : showLogout
        ? `<button class="small" id="ibkr-logout" ${loggingOut ? 'disabled' : ''}>
            ${loggingOut ? 'Logging out…' : 'Log out'}</button>`
        : '<button class="small" id="ibkr-restart">Log in</button>';
    rows.push(`<div class="login-row">
      <span class="login-name">Interactive Brokers</span>
      <span class="login-detail">${pill} ${note}</span>
      ${button}
    </div>`);
  }

  $('logins').innerHTML = rows.join('') || '<div class="muted">No brokers need a login.</div>';

  for (const btn of document.querySelectorAll('[data-schwab-login]')) {
    btn.addEventListener('click', (e) => startSchwabLogin(e.currentTarget.dataset.schwabLogin));
  }
  const ib = document.getElementById('ibkr-restart');
  if (ib) ib.addEventListener('click', restartIbkr);
  const ibOut = document.getElementById('ibkr-logout');
  if (ibOut) ibOut.addEventListener('click', logoutIbkr);

  // The flow box is updated in place, never re-rendered, so a half-pasted
  // URL survives the poll.
  const showFlow = flow.state && flow.state !== 'idle';
  $('login-flow').classList.toggle('hidden', !showFlow);
  if (showFlow) {
    const cls = flow.state === 'failed' ? 'alert alert-error'
      : flow.state === 'done' ? 'alert alert-ok' : 'alert alert-warn';
    $('login-flow-msg').className = cls;
    $('login-flow-msg').textContent = flow.message || '';
  }
  $('login-paste').classList.toggle('hidden', flow.state !== 'waiting');

  const wasBusy = loginsBusy;
  loginsBusy = flowActive || restarting || loggingOut;
  // A login just finished: the broker set may have changed.
  if (wasBusy && !loginsBusy) refresh();
}

async function refreshLogins() {
  if (!TOKEN) return;
  try {
    const res = await api('/api/logins');
    if (res.status === 200) renderLogins(res.body);
  } catch (err) {
    console.error(err);
  }
}

async function startSchwabLogin(login) {
  // Opened inside the click so the popup blocker allows it; pointed at
  // Schwab once the server has its listener up.
  const tab = window.open('about:blank', '_blank');
  const res = await api('/api/logins/schwab/start', {
    method: 'POST',
    body: JSON.stringify({ login }),
  });
  if (res.status !== 200) {
    if (tab) tab.close();
    alert(res.body.detail || 'Could not start the Schwab login.');
    return;
  }
  $('login-paste-input').value = '';
  if (tab) tab.location = res.body.auth_url;
  else window.open(res.body.auth_url, '_blank');
  refreshLogins();
}

async function submitPastedUrl() {
  const url = $('login-paste-input').value.trim();
  if (!url) return;
  $('login-paste-btn').disabled = true;
  try {
    const res = await api('/api/logins/schwab/paste', {
      method: 'POST',
      body: JSON.stringify({ url }),
    });
    if (res.status !== 200) alert(res.body.detail || 'That URL was not accepted.');
    else $('login-paste-input').value = '';
  } finally {
    $('login-paste-btn').disabled = false;
    refreshLogins();
  }
}

async function restartIbkr() {
  const res = await api('/api/logins/ibkr/restart', { method: 'POST' });
  toast(res.status === 200
    ? 'IBKR login started — approve it on your phone.'
    : (res.body.detail || 'IBKR login could not be started.'));
  refreshLogins();
}

function toast(message) {
  const el = document.createElement('div');
  el.className = 'toast';
  el.setAttribute('role', 'status');
  el.textContent = message;
  document.body.appendChild(el);
  setTimeout(() => el.classList.add('toast-out'), 4500);
  setTimeout(() => el.remove(), 5000);
}

async function logoutIbkr() {
  if (!await askConfirm('Log out of IBKR?',
    '<p>IBKR accounts disappear from the dashboard; charts keep using Schwab.</p>')) return;
  await api('/api/logins/ibkr/logout', { method: 'POST' });
  refreshLogins();
}

$('login-paste-btn').addEventListener('click', submitPastedUrl);

// --- refresh ----------------------------------------------------------------

async function refresh() {
  if (!TOKEN) { showTokenPrompt(); return; }
  try {
    const [status, holdings, audit, perf, ladder] = await Promise.all([
      api('/api/status'),
      api('/api/holdings'),
      api('/api/audit?limit=80'),
      api('/api/performance'),
      api('/api/ladder'),
    ]);
    const s = status.body;
    renderBanner(s);
    renderCombined(s.combined || {});
    renderBrokers(s);
    renderHoldings(holdings.body);
    renderEvents(audit.body.events || []);
    renderChart(perf.body);
    renderReturns(perf.body);
    // Kept for the confirmation dialog, which needs to state whether a click
    // will place a new order or re-price a working one.
    window.__lastLadder = ladder.body;
    renderLadder(ladder.body);

    const alerts = [];
    if (s.state?.mismatch) {
      alerts.push({
        level: 'error',
        html: `<strong>Brokers disagree.</strong> ${esc(s.state.mismatch.summary)}
               — trading is blocked until acknowledged.
               <button id="ack-btn" class="small danger">Acknowledge</button>`,
      });
    }
    if (!s.combined?.exposures_agree) {
      alerts.push({
        level: 'warn',
        html: '<strong>Exposure differs between brokers.</strong> Check each account below.',
      });
    }
    for (const w of s.environment_warnings || []) {
      alerts.push({ level: 'warn', html: esc(w) });
    }
    setAlerts(alerts);
  } catch (err) {
    console.error(err);
  }
}

// --- commands ---------------------------------------------------------------

function setBusy(state) {
  busy = state;
  for (const id of ['btn-long', 'btn-short', 'btn-flat']) $(id).disabled = state;
  for (const btn of document.querySelectorAll(
    '.rung, .buy-btn, .buy-now-btn, .step-btn, .reset-btn')) {
    btn.disabled = state || (btn.classList.contains('buy-now-btn')
      && !Number(btn.dataset.price));
  }
}

function askConfirm(title, body) {
  return new Promise((resolve) => {
    $('modal-title').textContent = title;
    $('modal-body').innerHTML = body;
    $('modal').classList.remove('hidden');
    const close = (value) => {
      $('modal').classList.add('hidden');
      $('modal-confirm').onclick = null;
      $('modal-cancel').onclick = null;
      resolve(value);
    };
    $('modal-confirm').onclick = () => close(true);
    $('modal-cancel').onclick = () => close(false);
  });
}

async function sendCommand(command) {
  if (busy) return;
  const requestId = `${command}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  setBusy(true);
  try {
    let res = await api('/api/command', {
      method: 'POST',
      body: JSON.stringify({ command, request_id: requestId }),
    });

    if (res.status === 409 && res.body.status === 'CONFIRMATION_REQUIRED') {
      const ok = await askConfirm(
        `Confirm ${command.replace('_', ' ')}`,
        `<strong>This is a LIVE order and will deploy the whole account.</strong>
         <br><br>${esc(res.body.detail)}`,
      );
      if (!ok) { setBusy(false); return; }
      res = await api('/api/command', {
        method: 'POST',
        body: JSON.stringify({
          command, request_id: requestId, confirm_token: res.body.confirm_token,
        }),
      });
    }

    reportResult(command, res);
  } catch (err) {
    setAlerts([{ level: 'error', html: esc(String(err)) }]);
  } finally {
    setBusy(false);
    refresh();
  }
}

function reportResult(command, res) {
  const b = res.body || {};
  if (b.status === 'OK') {
    const lines = (b.results || [])
      .map((r) => `${r.broker}: ${r.exposure_before} → ${r.exposure_after}` +
        (r.last_execution_price ? `, ${r.shares} @ ${fmtMoney(r.last_execution_price)}` : ''))
      .join('<br>');
    setAlerts([{ level: 'ok', html: `<strong>${command} complete.</strong><br>${lines}` }]);
    return;
  }
  if (b.status === 'PARTIAL') {
    const lines = (b.results || [])
      .map((r) => `${r.broker}: ${r.ok ? 'OK' : 'FAILED — ' + (r.errors || []).map(esc).join('; ')}`)
      .join('<br>');
    setAlerts([{
      level: 'error',
      html: `<strong>${command} PARTIALLY completed — the brokers are not in the `
        + `same state.</strong><br>${lines}`,
    }]);
    return;
  }
  const detail = b.detail
    || (b.results || []).flatMap((r) => r.errors || []).map(esc).join('<br>')
    || JSON.stringify(b).slice(0, 400);
  setAlerts([{ level: 'error', html: `<strong>${command} — ${esc(b.status || res.status)}</strong><br>${detail}` }]);
}

for (const id of ['btn-long', 'btn-short', 'btn-flat']) {
  $(id).addEventListener('click', (e) =>
    sendCommand(e.currentTarget.dataset.command));
}
$('btn-refresh').addEventListener('click', refresh);

renderRangePicker();
refresh();
refreshLogins();
// Every 2s while a login is in flight, otherwise every 10s.
let loginTicks = 0;
setInterval(() => {
  loginTicks += 1;
  if (loginsBusy || loginTicks % 5 === 0) refreshLogins();
}, 2000);
setInterval(() => { if (!busy) refresh(); }, 10000);
refreshCandles();
setInterval(() => { if (!busy) refreshCandles(); }, 60000);

// Poll for code changes more often than data: an edit should show up quickly.
checkVersion();
setInterval(checkVersion, 3000);
