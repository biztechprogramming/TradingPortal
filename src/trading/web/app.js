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

const $ = (id) => document.getElementById(id);
const fmtMoney = (n) =>
  n === null || n === undefined
    ? '—'
    : n.toLocaleString('en-US', { style: 'currency', currency: 'USD' });
const fmtNum = (n, d = 0) =>
  n === null || n === undefined ? '—' : Number(n).toLocaleString('en-US', {
    minimumFractionDigits: d, maximumFractionDigits: d,
  });
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
});

// --- alerts -----------------------------------------------------------------

function setAlerts(items) {
  $('alerts').innerHTML = items
    .map((a) => `<div class="alert alert-${a.level}">${a.html}</div>`)
    .join('');
  const ackBtn = document.getElementById('ack-btn');
  if (ackBtn) ackBtn.addEventListener('click', acknowledge);
}

async function acknowledge() {
  if (!confirm('Acknowledge the broker mismatch? Do this only after you have '
    + 'checked BOTH accounts and confirmed their real positions.')) return;
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
    `${m.open ? 'Market open' : 'Market closed'} — ${m.reason} · ${m.now_eastern || ''}`;
}

function renderCombined(c) {
  longSymbol = c.long_symbol || 'QQQ';
  shortSymbol = c.short_symbol || 'PSQ';
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
  const lastResults = {};
  const last = status.state?.last_result;
  if (last?.results) for (const r of last.results) lastResults[r.broker] = r;

  $('brokers').innerHTML = Object.entries(status.brokers)
    .map(([name, b]) => {
      const result = lastResults[name];
      const orders = (b.open_orders || [])
        .map((o) => `${o.side} ${fmtNum(o.quantity)} ${o.symbol} @ ${fmtNum(o.limit_price, 2)} (${o.state})`)
        .join('<br>') || '<span class="muted">none</span>';
      const errorHtml = b.error
        ? `<div class="alert alert-error" style="margin-top:10px">${esc(b.error)}</div>`
        : (result?.errors?.length
          ? `<div class="alert alert-error" style="margin-top:10px">${result.errors.map(esc).join('<br>')}</div>`
          : '');
      const acct = b.account
        ? `${esc(b.account.display_id)}${b.account.is_paper ? ' <span class="pill pill-ok">PAPER</span>' : ''}`
        : '<span class="muted">none</span>';
      const roBadge = b.read_only ? '<span class="badge-ro">READ-ONLY</span>' : '';

      return `<div class="panel">
        <div class="broker-head">
          <span class="broker-name">
            <span class="dot ${b.connected && !b.error ? 'dot-on' : 'dot-off'}"></span>${esc(name)}${roBadge}
          </span>
          <span class="pill pill-${b.exposure}">${b.exposure}</span>
        </div>
        <div class="kv"><span>Connection</span><span>${b.connected && !b.error ? 'connected' : 'disconnected'}</span></div>
        <div class="kv"><span>Account</span><span>${acct}</span></div>
        <div class="kv"><span>${longSymbol} shares</span><span>${fmtNum(qty(b, longSymbol))}</span></div>
        <div class="kv"><span>${shortSymbol} shares</span><span>${fmtNum(qty(b, shortSymbol))}</span></div>
        <div class="kv"><span>Cash available</span><span>${fmtMoney(b.balances?.cash_available)}</span></div>
        <div class="kv"><span>Buying power</span><span>${fmtMoney(b.balances?.buying_power)}</span></div>
        <div class="kv"><span>Outstanding orders</span><span>${orders}</span></div>
        <div class="kv"><span>Last command</span><span>${esc(result?.command || status.state?.last_command || '—')}</span></div>
        <div class="kv"><span>Last execution price</span><span>${
          result?.last_execution_price ? fmtMoney(result.last_execution_price) : '—'}</span></div>
        <div class="kv"><span>Last shares</span><span>${fmtNum(result?.shares || 0)}</span></div>
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

function renderEvents(events) {
  $('events').innerHTML = events
    .slice()
    .reverse()
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

// --- refresh ----------------------------------------------------------------

async function refresh() {
  if (!TOKEN) { showTokenPrompt(); return; }
  try {
    const [status, holdings, audit, perf] = await Promise.all([
      api('/api/status'),
      api('/api/holdings'),
      api('/api/audit?limit=80'),
      api('/api/performance'),
    ]);
    const s = status.body;
    renderBanner(s);
    renderCombined(s.combined || {});
    renderBrokers(s);
    renderHoldings(holdings.body);
    renderEvents(audit.body.events || []);
    renderChart(perf.body);
    renderReturns(perf.body);

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

refresh();
setInterval(() => { if (!busy) refresh(); }, 10000);
