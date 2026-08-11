/* Benefits Tracker UI.
 *
 * The server owns every number (roi.build_state); this file only lays them out
 * and posts actions back. Each action returns the full fresh state, so the view
 * re-renders from one source of truth — a figure can never disagree with itself
 * between the card bar and the overview.
 */
'use strict';

const money = n => '$' + Math.round(n).toLocaleString();
const $ = id => document.getElementById(id);

let STATE = null;
let active = 'overview';
// Which calendar year the grids render. A membership year straddles two of
// them, so the earlier months have to be reachable to be loggable at all.
let viewYear = null;
// Only one benefit's detail is open at a time within a card — opening a second
// one collapses the first back to its summary row, so nothing has to be closed
// by hand before looking at the next thing.
let openRow = null;
let flashRow = null;
// The cell currently being edited: {kind:'period'|'cash', id, index, cap, cur, label} | null.
// Cells are too narrow (down to 42px) to hold an inline input, so selecting one
// opens a single shared edit strip under whichever grid it belongs to.
let editing = null;

// --- data ------------------------------------------------------------------

async function load() {
  const q = viewYear ? `?year=${viewYear}` : '';
  STATE = await (await fetch('/api/state' + q)).json();
  viewYear = STATE.year;
  render();
}

async function post(url, data) {
  const body = new FormData();
  for (const [k, v] of Object.entries(data)) if (v !== undefined && v !== null) body.append(k, v);
  const res = await fetch(url, { method: 'POST', body });
  if (!res.ok) { alert('Could not save: ' + res.status); return; }
  STATE = await res.json();
  viewYear = STATE.year;
  render();
}

// Same POST helper, but returns its JSON instead of replacing STATE — for
// endpoints like link_token that hand back a token, not the app's state.
async function postRaw(url, data) {
  const body = new FormData();
  for (const [k, v] of Object.entries(data)) if (v !== undefined && v !== null) body.append(k, v);
  const res = await fetch(url, { method: 'POST', body });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || ('HTTP ' + res.status));
  }
  return res.json();
}

const card = id => STATE.cards.find(c => c.id === id);

// --- actions ---------------------------------------------------------------

const logPeriod = (bid, i, amount) => post('/api/period', { benefit_id: bid, index: i, amount, year: viewYear });
const logCash = (ch, i, amount) => post('/api/cash', { channel: ch, index: i, amount, year: viewYear });
const setAppl = (bid, v) => post('/api/state/' + bid, { applicable: v === null ? '' : (v ? '1' : '0') });
const setReal = (bid, v) => post('/api/state/' + bid, { realistic_value: v });

function redeemRest(bid, amt) { post('/api/redeem', { benefit_id: bid, amount: amt }); }

// --- Plaid (Phase 2) ---------------------------------------------------------

async function plaidConnect(cardId) {
  let link_token;
  try {
    ({ link_token } = await postRaw('/api/plaid/link_token', { card_id: cardId }));
  } catch (e) {
    alert('Could not start Plaid Link: ' + e.message);
    return;
  }
  // OAuth institutions (Amex, Chase, most major banks) fully navigate the
  // browser away to the bank's own login page and back — in-memory JS state
  // doesn't survive that round trip, so the token and card have to live in
  // sessionStorage for resumePlaidOAuthIfNeeded() to pick back up on reload.
  sessionStorage.setItem('plaid_link_token', link_token);
  sessionStorage.setItem('plaid_link_card', cardId);
  openPlaidLink(link_token, cardId);
}

function openPlaidLink(link_token, cardId, receivedRedirectUri) {
  const config = {
    token: link_token,
    onSuccess: (public_token, metadata) => {
      sessionStorage.removeItem('plaid_link_token');
      sessionStorage.removeItem('plaid_link_card');
      const institution_name = (metadata && metadata.institution && metadata.institution.name) || '';
      post('/api/plaid/exchange', { card_id: cardId, public_token, institution_name });
    },
    // `err` is populated whenever Link closes because it hit a Plaid API
    // error, not just a plain user-initiated exit — surfacing it is the
    // only way a silent-looking close (institution selected, modal just
    // disappears) turns into an actual diagnosable error.
    onExit: (err, metadata) => {
      sessionStorage.removeItem('plaid_link_token');
      sessionStorage.removeItem('plaid_link_card');
      if (err) {
        console.error('Plaid Link exited with error:', err, metadata);
        alert('Plaid Link error: ' + (err.error_message || err.error_code || JSON.stringify(err)));
      } else {
        console.log('Plaid Link exited (no error). Metadata:', metadata);
      }
    },
    onEvent: (eventName, metadata) => {
      console.log('Plaid Link event:', eventName, metadata);
    },
  };
  if (receivedRedirectUri) config.receivedRedirectUri = receivedRedirectUri;
  Plaid.create(config).open();
}

// After an OAuth institution's login redirects back to us, the URL carries
// ?oauth_state_id=... — resume the SAME Link session (not a fresh one) using
// the token stashed before the redirect, then scrub the param so a page
// refresh doesn't try to resume a session that's already finished.
function resumePlaidOAuthIfNeeded() {
  if (!window.location.search.includes('oauth_state_id')) return;
  const link_token = sessionStorage.getItem('plaid_link_token');
  const cardId = sessionStorage.getItem('plaid_link_card');
  if (!link_token || !cardId) return;
  openPlaidLink(link_token, cardId, window.location.href);
  window.history.replaceState({}, '', window.location.pathname);
}

function plaidSync(cardId) { post(`/api/plaid/sync/${cardId}`, {}); }

// --- cell editing ------------------------------------------------------------
// Click a cell to select it (no network call yet) -> the edit strip appears
// under its grid pre-filled with the EXACT current amount, editable, so a
// month you already logged can be corrected rather than only cleared.

function selectCell(kind, id, index, cap, cur, label) {
  cap = Number(cap); cur = Number(cur);
  // Nothing logged yet (missed/open) defaults to the FULL cap, not $0 — most
  // months really are the full amount, so Enter alone logs it: no typing
  // needed for the common case. A cell that already has a partial amount
  // pre-fills with THAT figure instead, so it can be corrected, not overwritten.
  // Rent points have no "full cap" to assume — an empty month starts blank.
  const start = kind === 'rentpoints' ? cur : (cur > 0 ? cur : cap);
  editing = { kind, id, index: Number(index), cap, cur: start, label };
  render();
  requestAnimationFrame(() => {
    const input = $('editamt');
    if (input) { input.focus(); input.select(); }
  });
}

function cancelEdit() { editing = null; render(); }

function commitEdit(forceAmount) {
  if (!editing) return;
  const input = $('editamt');
  // Rent points have no natural "full cap" to fall back to like a credit's
  // monthly allowance — an emptied field means 0, not editing.cap.
  const emptyFallback = editing.kind === 'rentpoints' ? 0 : editing.cap;
  const raw = forceAmount !== undefined ? forceAmount
    : (input && input.value !== '' ? parseFloat(input.value) : emptyFallback);
  const e = editing;
  editing = null;
  if (e.kind === 'rentpoints') {
    const year = parseInt(STATE.today.slice(0, 4), 10);
    return post('/api/rent-points', { card_id: e.id, year, month: e.index, points: Math.max(0, raw || 0) });
  }
  // Clamp client-side too (not just server-side) so the field never visibly
  // accepts more than the period allows.
  const amount = Math.min(e.cap, Math.max(0, raw || 0));
  if (e.kind === 'cash') logCash(e.id, e.index, amount);
  else logPeriod(e.id, e.index, amount);
}

function editStrip() {
  const e = editing;
  if (e.kind === 'rentpoints') {
    return `<div class="editstrip">
      <div class="es-lbl">${esc(e.label)}</div>
      <div class="es-input"><input type="number" step="1" min="0" inputmode="numeric"
        id="editamt" class="tnum" value="${e.cur || ''}" placeholder="pts"></div>
      <span class="es-cap">points earned</span>
      <button class="mini" data-act="saveedit">Save</button>
      <button class="mini ghost" data-act="clearedit">Clear</button>
      <button class="mini ghost" data-act="canceledit">Cancel</button>
    </div>`;
  }
  return `<div class="editstrip">
    <div class="es-lbl">${esc(e.label)}</div>
    <div class="es-input">$<input type="number" step="0.01" min="0" max="${e.cap}" inputmode="decimal"
      id="editamt" class="tnum" value="${e.cur}"></div>
    <span class="es-cap">of ${money(e.cap)} max</span>
    <button class="mini" data-act="saveedit">Save</button>
    <button class="mini ghost" data-act="clearedit">Clear to $0</button>
    <button class="mini ghost" data-act="canceledit">Cancel</button>
  </div>`;
}

function go(id) {
  active = id;
  render();
  window.scrollTo({ top: 0 });
}

function goBenefit(cardId, target) {
  active = cardId;
  openRow = target;
  flashRow = target;
  render();
  requestAnimationFrame(() => {
    const el = $('row-' + target);
    if (!el) return;
    el.scrollIntoView({ behavior: 'smooth', block: 'center' });
  });
}

function toggleRow(id) {
  openRow = (openRow === id) ? null : id;
  editing = null; // don't leave an edit strip open on a row that just collapsed
  render();
}

// --- shared bits -----------------------------------------------------------

// The violet bar segment must always equal whatever ACTUALLY counted toward
// verdict_value — rent points earned for Bilt, redeemed value for a future
// hybrid card, never a card's raw points.realized (that's bonus-only for
// Platinum/CSR and would misleadingly inflate their bars).
function countedPointsValue(c) {
  if (c.rent_points) return c.rent_points.value;
  if (c.points && c.points.counts_in_verdict) return c.points.realized;
  return 0;
}

/** avail/real/act are the credits(+cash) tiers. `points` is realized points
 *  value, drawn as a distinct-coloured segment stacked to the RIGHT of captured
 *  credits so the two value sources read apart at a glance. Points have no
 *  "available" ceiling, so they only extend captured (and the scale), never the
 *  Available figure. `showRealistic=false` drops the Realistic segment/legend
 *  entirely — used on Bilt's Card Credits sub-bar, where the derivation of that
 *  figure isn't legible at a glance the way it is for a single credit. */
function tierBar(avail, real, act, cost, points = 0, showRealistic = true) {
  const scale = Math.max(avail, cost, (showRealistic ? real : 0) + points, act + points) || 1;
  const pct = v => (100 * v / scale).toFixed(1);
  return `<div class="bar">
      ${showRealistic ? `<div class="real" style="width:${pct(real)}%"></div>` : ''}
      <div class="act" style="width:${pct(act)}%"></div>
      ${points > 0 ? `<div class="act-points" style="left:${pct(act)}%;width:${pct(points)}%"></div>` : ''}
      <div class="fee" style="left:${pct(cost)}%"></div></div>
    <div class="barlab">
      <span><i class="swatch" style="background:var(--line)"></i>Available <b>${money(avail)}</b></span>
      ${showRealistic ? `<span><i class="swatch" style="background:var(--accent-tint)"></i>Realistic <b>${money(real)}</b></span>` : ''}
      <span><i class="swatch" style="background:var(--accent)"></i>Captured <b class="cap">${money(act)}</b></span>
      ${points > 0 ? `<span><i class="swatch" style="background:var(--points)"></i>Points <b>${money(points)}</b></span>` : ''}
      <span><i class="swatch" style="background:var(--crit)"></i>Fee <b>${money(cost)}</b></span>
    </div>`;
}

/** Bilt's top composite bar: unlike every other card, its "captured" figure is
 *  two genuinely different things (a statement credit vs. a Bilt Cash balance
 *  burn) that shouldn't be silently summed into one number the way the generic
 *  tierBar does. No Realistic segment here either — see tierBar's doc comment.
 */
function biltCompositeBar(c) {
  const creditsAct = c.credits.actual;
  const cashAct = c.cash ? c.cash.redeemed : 0;
  const points = countedPointsValue(c);
  const avail = c.available, cost = c.cost;
  const scale = Math.max(avail, cost, creditsAct + cashAct + points) || 1;
  const pct = v => (100 * v / scale).toFixed(1);
  let left = 0;
  const seg = (val, cls) => {
    if (val <= 0) return '';
    const html = `<div class="${cls}" style="left:${pct(left)}%;width:${pct(val)}%"></div>`;
    left += val;
    return html;
  };
  return `<div class="bar">
      ${seg(creditsAct, 'act')}
      ${seg(cashAct, 'act-cash')}
      ${seg(points, 'act-points')}
      <div class="fee" style="left:${pct(cost)}%"></div></div>
    <div class="barlab">
      <span><i class="swatch" style="background:var(--line)"></i>Available <b>${money(avail)}</b></span>
      <span><i class="swatch" style="background:var(--accent)"></i>Credits captured <b>${money(creditsAct)}</b></span>
      <span><i class="swatch" style="background:var(--cash)"></i>Bilt Cash captured <b>${money(cashAct)}</b></span>
      ${points > 0 ? `<span><i class="swatch" style="background:var(--points)"></i>Point value <b>${money(points)}</b></span>` : ''}
      <span><i class="swatch" style="background:var(--crit)"></i>Fee <b>${money(cost)}</b></span>
    </div>`;
}

const COLS = { halves: 'cols2', quarters: 'cols4', months: '' };

/** Period grid: one cell per real window. An unused H1 has to read as forfeited,
 *  not vanish into a yearly total. Clicking a cell selects it for editing —
 *  amounts are rarely the full cap, so every cell has to accept an exact figure,
 *  not just an on/off toggle. */
function periodGrid(b) {
  const g = b.grid, cells = g.cells;
  let used = 0, hit = 0, tot = 0;
  const html = cells.map(c => {
    let val, tick = '';
    if (c.state === 'done') { val = money(c.redeemed); tick = '✓'; used += c.redeemed; hit++; }
    else if (c.state === 'missed') val = '$0 missed';
    else if (c.state === 'open') val = money(c.allowance) + ' open';
    else if (c.state === 'dead') val = 'n/a';
    else val = '—';
    if (c.state !== 'future' && c.state !== 'dead') tot++;
    const dis = (c.state === 'future' || c.state === 'dead');
    const sel = editing && editing.kind === 'period' && editing.id === b.id && editing.index === c.index;
    const inMem = inMembershipWindow(c.start, c.end) ? 'inmem' : '';
    return `<button class="mo ${c.state} ${inMem} ${sel ? 'editing' : ''}" ${dis ? 'disabled' : ''}
        data-act="selectperiod" data-kind="period" data-b="${b.id}" data-i="${c.index}"
        data-cap="${c.allowance}" data-cur="${c.redeemed}"
        data-lbl="${esc(b.name)} · ${esc(c.long_label)}">
        <span class="tick">${tick}</span>
        <div class="mlab">${c.long_label}</div><div class="mval tnum">${val}</div></button>`;
  }).join('');
  const strip = (editing && editing.kind === 'period' && editing.id === b.id) ? editStrip() : '';
  return `<div class="ptitle"><span>Each ${g.unit.replace(/s$/, '')} tracks separately · click to enter the exact amount</span>
      <b class="tnum">${money(used)} of ${money(b.available)} · ${hit} of ${tot} ${g.unit} used</b></div>
    ${yearNav()}
    <div class="months ${COLS[g.unit] || ''}">${html}</div>
    ${strip}
    <div class="mgrid-foot">Green = captured · red = missed &amp; past · outlined = open now · faint = upcoming. Click any cell to enter what you actually redeemed, including a partial amount.</div>`;
}

/** Matrix: buckets down, periods across. Reading down a column shows what one
 *  month captured; across a row shows a habit. Same click-to-select-then-edit
 *  behavior as periodGrid — used for grouped credits (DoorDash) and Bilt Cash
 *  channels, both of which are just as often partial. */
function matrix(rows, kind) {
  const n = rows[0].cells.length;
  const cols = `132px repeat(${n},minmax(42px,1fr)) 58px`;
  let h = yearNav() + `<div class="mxwrap"><div class="mx" style="grid-template-columns:${cols}">`;
  h += `<div></div>${rows[0].cells.map(c => `<div class="mx-hd">${c.label}</div>`).join('')}<div class="mx-hd">ytd</div>`;
  for (const r of rows) {
    let tot = 0;
    h += `<div class="mx-lbl">${r.label}<small>${money(r.cap)}/mo</small></div>`;
    for (const c of r.cells) {
      let txt;
      if (c.state === 'done') { txt = money(c.redeemed); tot += c.redeemed; }
      else if (c.state === 'missed') txt = '—';
      else if (c.state === 'open') txt = money(c.allowance);
      else txt = '';
      const dis = (c.state === 'future' || c.state === 'dead');
      const sel = editing && editing.kind === kind && editing.id === r.key && editing.index === c.index;
      const inMem = (c.start && inMembershipWindow(c.start, c.end)) ? 'inmem' : '';
      h += `<button class="mx-cell ${c.state} ${inMem} ${sel ? 'editing' : ''}" ${dis ? 'disabled' : ''}
              data-act="${kind === 'cash' ? 'selectcash' : 'selectperiod'}" data-b="${r.key}" data-i="${c.index}"
              data-cap="${c.allowance}" data-cur="${c.redeemed}" data-lbl="${esc(r.label)} · ${esc(c.label)}"
              title="${r.label} · ${c.label}">${txt}</button>`;
    }
    h += `<div class="mx-tot tnum">${money(tot)}</div>`;
  }
  h += '</div></div>';
  if (editing && editing.kind === kind && rows.some(r => r.key === editing.id)) h += editStrip();
  return h;
}

function decideBtns(b) {
  if (b.applicable === null)
    return `<button class="mini" data-act="appl" data-b="${b.id}" data-v="1">Mark useful</button>
            <button class="mini ghost" data-act="appl" data-b="${b.id}" data-v="0">Not applicable</button>`;
  if (b.applicable === false)
    return `<span style="font-size:11.5px;color:var(--faint)">not applicable</span>
            <button class="mini ghost" data-act="appl" data-b="${b.id}" data-v="">undo</button>`;
  return `<input class="tnum" data-real="${b.id}" value="${b.realistic_value ?? ''}"
            placeholder="${Math.round(b.realistic)}" title="Realistic annual value">
          <button class="mini ghost" data-act="setreal" data-b="${b.id}">set</button>
          <button class="mini ghost" data-act="appl" data-b="${b.id}" data-v="0">n/a</button>`;
}

// --- rows ------------------------------------------------------------------

function benefitRow(b) {
  const pills = [];
  if (b.tracking_mode === 'app_only_manual') pills.push('<span class="pill manual">manual</span>');
  if (b.tracking_mode === 'planned') pills.push('<span class="pill plan">plan ahead</span>');
  if (b.disputed) pills.push('<span class="pill plan">disputed</span>');
  if (b.spend_gated) pills.push('<span class="pill">$75k tier</span>');
  const pct = b.available ? 100 * b.year_redeemed / b.available : 0;
  const per = b.expired ? 'expired'
    : b.applicable === false ? 'n/a'
      : `${money(b.period_redeemed)} / ${money(b.allowance)}`;
  const stripe = sectionStripe(b._section, b.applicable, b.urgency);
  const showRedeem = !b.grid && !b.expired && b.applicable !== false && b.remaining > 0;
  const open = openRow === b.id;
  return `<div class="row ${stripe} ${open ? 'open' : ''} ${flashRow === b.id ? 'justopened' : ''}" id="row-${b.id}">
    <button class="rowhd" data-act="toggle" data-b="${b.id}">
      <div><div class="rname">${b.name}</div>
        <div class="rmeta">${esc(b.cadence_label)}${pills.length ? ' · ' + pills.join(' ') : ''}</div></div>
      <div class="rwin">${b.window_label
        ? `<span class="d ${pillClass(stripe)}">${b.days_left !== null ? b.days_left + 'd left' : ''}</span>
           <small>${b.window_label}${b.window_end ? ' · by ' + fmtDay(b.window_end) : ''}</small>`
        : '<small>no deadline</small>'}</div>
      <div class="rprog">
        <div class="nums"><span class="per">${per}</span>
          <span class="yr tnum">${money(b.year_redeemed)} / ${money(b.available)} yr</span></div>
        <div class="track"><i style="width:${pct}%"></i></div></div>
      <div class="rcaret">▸</div>
    </button>
    <div class="panel">
      ${b.grid ? periodGrid(b) : ''}
      <div class="decide">${decideBtns(b)}
        ${showRedeem ? `<button class="mini" data-act="redeem" data-b="${b.id}" data-v="${b.remaining}">Mark redeemed (${money(b.remaining)})</button>` : ''}
      </div>
      ${b.note ? `<div class="noteblock">${esc(b.note)}</div>` : ''}
    </div></div>`;
}

function groupRow(g) {
  const rows = g.members.map(m => ({ key: m.id, label: m.short_name, cap: m.allowance, cells: m.grid.cells }));
  const pct = g.available ? 100 * g.year_redeemed / g.available : 0;
  const buckets = g.members.map(m => `<div class="bucket"><div class="bn">${m.short_name}
      <small>${money(m.year_redeemed)} of ${money(m.available)} this year · ${esc(m.note)}</small></div>
      ${decideBtns(m)}</div>`).join('');
  const open = openRow === g.id;
  const stripe = sectionStripe(g._section, g.applicable, g.urgency);
  return `<div class="row ${stripe} ${open ? 'open' : ''} ${flashRow === g.id ? 'justopened' : ''}" id="row-${g.id}">
    <button class="rowhd" data-act="toggle" data-b="${g.id}">
      <div><div class="rname">${g.title}</div>
        <div class="rmeta">${esc(g.cadence_label)} · ${g.members.length} buckets
          <span class="pill manual">manual</span>
          ${g.undecided ? `<span class="pill plan">${g.undecided} undecided</span>` : ''}</div></div>
      <div class="rwin"><span class="d ${pillClass(stripe)}">${g.days_left !== null ? g.days_left + 'd left' : ''}</span>
        <small>${g.window_label || ''}</small></div>
      <div class="rprog">
        <div class="nums"><span class="per">${money(g.period_redeemed)} / ${money(g.allowance)}</span>
          <span class="yr tnum">${money(g.year_redeemed)} / ${money(g.available)} yr</span></div>
        <div class="track"><i style="width:${pct}%"></i></div></div>
      <div class="rcaret">▸</div>
    </button>
    <div class="panel">
      <div class="ptitle"><span>Each bucket logs separately · click any cell</span>
        <b class="tnum">${money(g.year_redeemed)} of ${money(g.available)} this year</b></div>
      ${matrix(rows, 'period')}
      <div class="mgrid-foot">${esc(g.blurb)}</div>
      ${buckets}</div></div>`;
}

// --- views -----------------------------------------------------------------

// One clean word per verdict state for the compact tab chip (the full label
// with the dollar figure shows on the card view and overview tiles).
const VERDICT_WORD = { keep: 'KEEP', cancel: 'DROP', pending: 'TBD', incomplete: 'DECIDE' };

// Switching year re-renders the whole view, and the sections above the grid
// rarely keep the same height between years — a credit that was "needs
// attention" in one year sits under "done" in another. Left alone the page
// reflows and the grid you were reading scrolls out of sight. So: pin the
// element you clicked in, and after the re-render put it back at the same
// spot on screen.
async function switchYear(btn, year) {
  const anchor = btn.closest('.row') || btn.closest('#view');
  const id = anchor && anchor.id;
  const before = anchor ? anchor.getBoundingClientRect().top : null;
  const scrollBefore = window.scrollY;
  viewYear = year;
  await load();
  const el = id ? document.getElementById(id) : null;
  if (el && before !== null) {
    // restore the anchor to the exact viewport offset it had
    window.scrollTo({ top: window.scrollY + el.getBoundingClientRect().top - before });
  } else {
    window.scrollTo({ top: scrollBefore });
  }
}

// Year switcher, per grid. It belongs next to the cells it changes: the year
// only matters once you're actually logging a month, and a page-level control
// left you switching context before you knew you needed to.
function yearNav() {
  const y = STATE.year, cur = STATE.current_year;
  const m = renderingCard && renderingCard.membership_year;
  const inPrev = m && m.start.slice(0, 4) < String(cur);
  return `<div class="yearnav">
    <button class="mini ghost" data-act="year" data-y="${y - 1}">◀ ${y - 1}</button>
    <b>${y}</b>
    ${y < cur ? `<button class="mini ghost" data-act="year" data-y="${y + 1}">${y + 1} ▶</button>`
              : `<button class="mini ghost" disabled>${y + 1} ▶</button>`}
    ${y !== cur ? `<span class="pill plan">viewing ${y}</span>` : ''}
    ${m ? `<span class="ct">membership year ${fmtDay(m.start)} – ${fmtDay(m.end)}${
      inPrev && y === cur ? ` · <b>${y - 1} months still count</b>` : ''}</span>` : ''}
  </div>`;
}

// Does this window fall inside the membership year the current fee bought?
// Those are the cells where logging still moves the renewal decision.
function inMembershipWindow(startIso, endIso) {
  const m = renderingCard && renderingCard.membership_year;
  if (!m) return false;
  return endIso >= m.start && startIso < m.end;
}

function renderTabs() {
  const tabs = [`<button class="tab ${active === 'overview' ? 'active' : ''}" data-go="overview">
      <span class="tname">Overview</span><span style="font-size:11px;color:var(--faint)">all cards</span></button>`];
  for (const c of STATE.cards) {
    tabs.push(`<button class="tab ${active === c.id ? 'active' : ''}" data-go="${c.id}">
      <span class="tname">${c.label}${c.attention ? `<span class="badge">${c.attention}</span>` : ''}</span>
      <span class="chip ${c.verdict[0]}">${VERDICT_WORD[c.verdict[0]] || c.verdict[0]}</span></button>`);
  }
  $('tabs').innerHTML = tabs.join('');
}

function renderOverview() {
  const exp = STATE.expiring;
  const total = exp.reduce((s, x) => s + x.amount, 0);
  const rows = exp.length ? exp.map(x => `
    <button class="act" data-goto="${x.card}" data-target="${x.target}">
      <span class="amt tnum">${money(x.amount)}</span>
      <span class="who">${x.name}<small>${x.by_month ? 'log by period · ' : ''}${x.card_label}</small></span>
      <span class="days ${x.urgency}">${x.days}d</span>
      <span class="mini">open →</span></button>`).join('')
    : '<div class="act" style="cursor:default"><span class="who">Nothing expiring in the next 30 days.</span></div>';

  const cards = STATE.cards.map(c => `
    <button class="csum" data-go="${c.id}">
      <div class="csum-hd"><div><div class="nm">${c.label}</div>
        <div class="fee">${money(c.cost)}/yr${c.cash ? ' · incl. Bilt Cash' : ''}${c.au_fee ? ' · incl. AU fee' : ''}</div></div>
        <span class="chip ${c.verdict[0]}">${c.verdict[1]}</span></div>
      ${tierBar(c.available, c.realistic, c.actual, c.cost, countedPointsValue(c))}
      <div class="attn ${c.gap > 0 ? '' : 'ok'}">${c.gap > 0
        ? `▲ ${money(c.gap)} left to capture this year`
        : '✓ fully captured'}</div>
      ${c.cash && c.cash.at_risk > 0
        ? `<div class="attn risk">⚠ ${money(c.cash.at_risk)} Bilt Cash expires Dec 31 — redeem it</div>` : ''}
    </button>`).join('');

  $('view').innerHTML = `
    <h2>Your cards</h2>
    <div class="sub">Each verdict compares the value you realistically get against the fee: <b>KEEP</b> means
      it's over the fee (worth more than it costs), <b>DROP</b> means under.</div>
    ${reviewSection()}
    <div class="actnow">
      <div class="actnow-hd"><span>Expiring within 30 days · tap to open and log</span>
        <span class="tnum">${money(total)} unused</span></div>${rows}</div>
    <div class="cardgrid">${cards}</div>`;
}

// Plaid matches that weren't confident enough to auto-apply.
//
// Collapsed by default. It used to sit expanded above the card verdicts and
// occupied half the Overview — an inbox outranking the dashboard. It also only
// ever offered the matcher's own guesses, so when every guess was wrong the
// only move was to reject and lose the credit entirely; that is what happened
// to two real airline reimbursements.
let reviewOpen = false;

function reviewSection() {
  const q = STATE.review_queue || [];
  if (!q.length) return '';
  const total = q.reduce((s, t) => s + Math.abs(t.amount), 0);
  if (!reviewOpen) {
    return `<button class="reviewbar" data-act="openreview">
      <span>⚠ <b>${q.length}</b> transaction${q.length === 1 ? '' : 's'} to review
        <span class="ct">${money(total)}</span></span><span>→</span></button>`;
  }
  // Group identical suggestions: six Dunkin' matches are one decision, not six.
  const groups = {};
  q.forEach(t => {
    const best = (t.candidates || []).slice().sort((a, b) => b.confidence - a.confidence)[0];
    const key = `${t.card_id}|${t.name}|${best ? best.benefit_id : 'none'}`;
    (groups[key] = groups[key] || { items: [], best, name: t.name, card: t.card_id }).items.push(t);
  });
  const rows = Object.values(groups).map(g => reviewGroup(g)).join('');
  return `<div class="actnow" style="border-left-color:var(--warn);margin-bottom:14px">
    <div class="actnow-hd" style="background:var(--warn-bg);color:var(--warn)">
      <span>Needs review · Plaid found these but wasn't sure</span>
      <button class="mini ghost" data-act="closereview">collapse</button></div>${rows}</div>`;
}

function reviewGroup(g) {
  const card = STATE.cards.find(c => c.id === g.card) || {};
  const n = g.items.length;
  const ids = g.items.map(t => t.id).join(',');
  const sum = g.items.reduce((s, t) => s + Math.abs(t.amount), 0);
  const when = n === 1 ? fmtDay(g.items[0].date)
    : `${fmtDay(g.items[n - 1].date)}–${fmtDay(g.items[0].date)}`;
  const suggested = g.best
    ? `<button class="mini" data-act="reviewconfirm" data-txn="${g.items[0].id}"
         data-benefit="${g.best.benefit_id}" title="${esc(g.best.reason || '')}"
         >${esc(g.best.benefit_name)} · ${Math.round(g.best.confidence * 100)}%</button>`
    : '<span class="es-cap">no suggestion</span>';
  const bulk = (n > 1 && g.best)
    ? `<button class="mini" data-act="bulkconfirm" data-txns="${ids}"
         data-benefit="${g.best.benefit_id}">Accept all ${n}</button>` : '';
  return `<div class="act reviewrow">
    <span class="amt tnum">${money(sum)}${n > 1 ? `<small>×${n}</small>` : ''}</span>
    <span class="who">${esc(g.name)}<small>${when} · ${esc(card.label || g.card)}</small></span>
    <span class="revacts">
      ${suggested}${bulk}
      ${benefitPicker(card, ids)}
      <button class="mini ghost" data-act="reviewreject" data-txns="${ids}"
        data-reason="not_a_credit" title="A refund or return — not a statement credit">Not a credit</button>
    </span></div>`;
}

// Every credit on the card, with how much of the current window is already
// used — so a wrong suggestion can be corrected rather than only discarded.
function benefitPicker(card, ids) {
  const opts = (card.entries || []).flatMap(e => e.kind === 'group' ? e.members : [e])
    .filter(m => m.applicable !== false && !m.expired)
    .map(m => `<option value="${m.id}">${esc(m.name)} — ${m.remaining > 0
        ? money(m.remaining) + ' left this period' : 'fully used this period'}</option>`)
    .join('');
  return `<select class="pickben" data-txns="${ids}">
    <option value="">choose a different credit…</option>${opts}</select>`;
}

function cashSection(c) {
  const k = c.cash;
  const rows = k.channels.map(ch => ({ key: ch.id, label: ch.name, cap: ch.cap, cells: ch.cells }));
  const scale = k.earned || 1;
  return `<div class="group-lbl">Bilt Cash <span class="ct">a currency, not a credit</span></div>
    <div class="bar"><div class="real" style="width:${(100 * k.realistic / scale).toFixed(1)}%"></div>
      <div class="act-cash" style="left:0;width:${(100 * k.redeemed / scale).toFixed(1)}%"></div></div>
    <div class="barlab">
      <span><i class="swatch" style="background:var(--line)"></i>Earned YTD <b>${money(k.earned)}</b></span>
      <span><i class="swatch" style="background:var(--accent-tint)"></i>Realistic <b>${money(k.realistic)}</b></span>
      <span><i class="swatch" style="background:var(--cash)"></i>Redeemed <b>${money(k.redeemed)}</b></span>
      <span>Balance <b>${money(k.balance)}</b></span></div>
    ${addEarnForm()}
    ${k.at_risk > 0 ? `<div class="bc-warn">⚠ ${money(k.at_risk)} of this balance expires Dec 31 — anything over $100 is destroyed at year end. Channels are capped monthly, so it can’t be banked and spent later.</div>` : ''}
    <div class="ptitle" style="margin-top:14px"><span>Monthly channels · click any cell to log a burn</span>
      <b class="tnum">${money(k.redeemed)} redeemed</b></div>
    ${matrix(rows, 'cash')}
    <div style="margin-top:10px">${k.channels.map(ch => `<div class="bucket">
        <div class="bn">${ch.name}${ch.custom ? ' <span class="pill">custom</span>' : ''}<small>${money(ch.ytd)} spent this year</small></div>
        </div>`).join('')}</div>
    ${addChannelForm()}
    ${k.housing_note ? `<div class="noteblock">${esc(k.housing_note)}</div>` : ''}`;
}

// Bilt lets you redeem Bilt Cash into categories the catalog doesn't
// enumerate — this adds one at runtime, persisted so it survives a reload.
let addingChannel = false;

function addChannelForm() {
  if (!addingChannel) {
    return `<button class="mini ghost" style="margin-top:10px" data-act="showaddchannel">+ Add a channel</button>`;
  }
  return `<div class="editstrip" style="margin-top:10px">
    <input id="newchname" class="tnum" placeholder="channel name" style="width:150px">
    <div class="es-input">$<input id="newchcap" type="number" step="0.01" min="0.01" inputmode="decimal"
      class="tnum" placeholder="10" style="width:70px"></div><span class="es-cap">/month</span>
    <button class="mini" data-act="addchannel">Add</button>
    <button class="mini ghost" data-act="canceladdchannel">Cancel</button>
  </div>`;
}

function submitAddChannel() {
  const name = ($('newchname') || {}).value || '';
  const cap = parseFloat(($('newchcap') || {}).value);
  if (!name.trim() || !(cap > 0)) return;
  addingChannel = false;
  post('/api/cash/channel', { name, monthly_cap: cap });
}

// Earned isn't only the annual award — Bilt pays 4% back on everyday spend
// too, and this app has no way to see that automatically (Plaid can't read a
// loyalty-currency balance). This is how a real balance gets reconciled —
// including a one-off catch-up entry when the tracked total falls behind
// what the Bilt app actually shows.
let addingEarn = false;

function addEarnForm() {
  if (!addingEarn) {
    return `<button class="mini ghost" style="margin-top:8px" data-act="showaddearn">+ Add earned</button>`;
  }
  return `<div class="editstrip" style="margin-top:8px">
    <div class="es-input">$<input id="newearnamt" type="number" step="0.01" min="0.01" inputmode="decimal"
      class="tnum" placeholder="amount" style="width:80px"></div>
    <input id="newearnnote" class="tnum" placeholder="note (optional)" style="width:200px">
    <button class="mini" data-act="addearn">Add</button>
    <button class="mini ghost" data-act="canceladdearn">Cancel</button>
  </div>`;
}

function submitAddEarn() {
  const amount = parseFloat(($('newearnamt') || {}).value);
  if (!(amount > 0)) return;
  const note = ($('newearnnote') || {}).value || '';
  addingEarn = false;
  post('/api/cash/earn', { amount, note });
}

/** Whether there is nothing left to act on THIS YEAR for a benefit (or every
 *  member of a group) — distinct from `remaining`, which is only about the
 *  CURRENT period. Two different shapes of "done":
 *    - grid credits: every window is done/missed/dead — none open or future.
 *      A missed H1 next to a captured H2 still counts (StubHub: H1 is gone,
 *      H2 is redeemed, nothing more is possible this year either way).
 *    - single-shot annual/opportunity credits (no grid): the whole year's
 *      value has been redeemed. This is what moves "mark redeemed" credits
 *      like the CSR travel credit or Apple TV+/Music into Done once you've
 *      actually clicked redeem, instead of leaving them flagged forever.
 */
function isYearDone(e) {
  const members = e.kind === 'group' ? e.members : [e];
  return members.every(m => {
    if (m.grid) return m.grid.cells.every(c => c.state !== 'open' && c.state !== 'future');
    return m.available > 0 ? m.year_redeemed >= m.available - 0.01 : true;
  });
}

// The left stripe must reflect the SECTION a row actually landed in, not a raw
// date-urgency recomputed independently — that's exactly how a fully-logged
// month ($25/$25, moved to "On track") kept showing red: urgency doesn't reset
// just because the period was redeemed, so a second, disconnected computation
// of it will drift from where the row visually sits.
function sectionStripe(section, applicable, urgency) {
  if (applicable === false) return 'flat';
  if (section === 'rest' || section === 'track') return 'neutral';   // done/on-track -> calm (green)
  return urgency === 'red' ? 'red' : 'orange';                        // attn -> always flagged, never green
}

// Every colored element on a row (left stripe, days-left pill, anything else
// added later) must derive from this SAME section-based value — never from a
// second, independently-computed urgency — or they can disagree with each
// other exactly like the stripe and the pill just did.
function pillClass(stripe) { return stripe === 'flat' ? 'neutral' : stripe; }

// The keep/cancel verdict only means something against a deadline: you have
// to decide BEFORE the fee posts. Unverified dates say so rather than implying
// a precision the data doesn't have.
function feeLine(c) {
  const f = c.fee_schedule;
  if (!f || !f.next_due) return '';
  const d = f.days_until;
  const cls = d <= 21 ? 'crit' : d <= 45 ? 'warn' : '';
  const when = new Date(f.next_due + 'T12:00:00')
    .toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
  const src = f.verified
    ? `from your last fee charge on ${fmtDay(f.last_charged)}`
    : 'estimated \u2014 not yet seen in your transactions';
  const m = c.membership_year;
  // The fee buys a membership year, so that — not the calendar — is the span
  // the keep/cancel call should be judged over. Shown only here, next to the
  // decision; every grid stays on the real reset windows.
  const my = m ? `<div class="feeline ${m.covers_fee ? 'ok' : 'crit'}">
      Membership year ${fmtDay(m.start)} \u2013 ${fmtDay(m.end)}:
      <b>${money(m.captured)}</b> captured against the ${money(m.fee)} fee
      ${m.covers_fee ? '\u2014 covered' : `\u2014 <b>${money(m.fee - m.captured)} short</b>`}
      ${m.excludes_points ? '<span class="ct" title="Points are reported separately on this card">credits + cash only</span>' : ''}
    </div>` : '';
  return `<div class="feeline ${cls}">${money(c.cost)} fee posts <b>${when}</b> \u00b7 ${d} days
    ${d <= 45 ? `\u00b7 <b>decide by ${fmtDay(f.decide_by)}</b>` : ''}
    <span class="ct" title="${esc(src)}">${f.verified ? 'confirmed' : 'estimated'}</span></div>${my}`;
}

let renderingCard = null;

function renderCard(c) {
  renderingCard = c;
  const g = { attn: [], track: [], rest: [] };
  for (const e of c.entries) {
    const ms = e.kind === 'group' ? e.members : [e];
    const allNA = ms.every(m => m.applicable === false);
    const undecided = ms.some(m => m.applicable === null);
    // Undecided always wins — it blocks the verdict regardless of anything else.
    if (undecided) { e._section = 'attn'; g.attn.push(e); continue; }
    if (allNA || e.expired || isYearDone(e)) { e._section = 'rest'; g.rest.push(e); continue; }
    // Due now: something is still actionable in the CURRENT period and either
    // it's genuinely urgent (red) or within the 30-day window. remaining>0 is
    // what keeps an already-fully-logged month ($25/$25) out of this bucket —
    // urgency alone doesn't reset just because you already redeemed it.
    const dueNow = e.remaining > 0 && (e.urgency === 'red' || (e.days_left !== null && e.days_left <= 30));
    e._section = dueNow ? 'attn' : 'track';
    (dueNow ? g.attn : g.track).push(e);
  }
  const rowOf = e => e.kind === 'group' ? groupRow(e) : benefitRow(e);
  const sec = (lbl, arr) => arr.length
    ? `<div class="group-lbl">${lbl} <span class="ct">${arr.length}</span></div>${arr.map(rowOf).join('')}` : '';

  const cr = c.credits;
  const pending = c.verdict[0] === 'pending';
  const decided = c.verdict[0] === 'keep' || c.verdict[0] === 'cancel';
  const earnBased = !!c.rent_points;   // Bilt: verdict driven by points EARNED, not redeemed
  // Spell out the arithmetic behind the chip: value the verdict runs on, vs fee.
  const mathLine = decided
    ? ` · getting ${money(c.verdict_value)} of realistic value for the ${money(c.cost)} fee`
    : pending ? (earnBased ? ' · log rent points earned to unlock the verdict' : ' · log a points redemption to unlock the verdict') : '';
  $('view').innerHTML = `
    <div class="focus-hd"><div><h2>${c.name}</h2>${feeLine(c)}
      <div class="sub" style="margin:4px 0 0">${money(c.cost)}/yr${c.au_fee ? ` (${money(c.fee)} + ${money(c.au_fee)} AU)` : ''}${mathLine}</div></div>
      <span class="chip ${c.verdict[0]}">${c.verdict[1]}</span></div>
    <div style="padding:16px 0 4px">${c.cash ? biltCompositeBar(c) : tierBar(c.available, c.realistic, c.actual, c.cost, countedPointsValue(c))}</div>
    ${c.cash ? `<div class="sub" style="margin:6px 0 0">Card credits <b>+</b> Bilt Cash combined — one card, one fee.</div>
        <div class="group-lbl">Card credits <span class="ct">${c.entries.length}</span></div>
        ${tierBar(cr.available, cr.realistic, cr.actual, c.cost, 0, false)}` : ''}
    ${pending ? `<div class="noteblock">This card earns its keep on points, not credits — its verdict stays PENDING until you log ${earnBased ? 'at least one month of rent points earned' : 'at least one points redemption'} below. Once you do, the keep/cancel math runs on credits realized <b>+</b> ${earnBased ? 'rent points earned' : 'points value realized'}.</div>` : ''}
    ${sec('Needs attention', g.attn)}${sec('On track', g.track)}${sec('Done &amp; not applicable', g.rest)}
    ${c.cash ? cashSection(c) : ''}
    ${c.rent_points ? rentPointsSection(c) : ''}
    ${c.points ? pointsSection(c) : ''}
    ${plaidSection(c)}`;
}

// Bank sync — one Item per card. Not connected: a single Connect button.
// Connected: institution + last-synced timestamp + a manual Sync now (no
// webhook in Phase 2 checkpoint, so syncing is user-triggered).
function plaidSection(c) {
  const item = (STATE.plaid_items || []).find(p => p.card_id === c.id);
  if (!item) {
    return `<div class="group-lbl">Bank sync</div>
      <div class="sub">Not connected — statement credits have to be logged by hand until this card is linked.</div>
      <button class="mini" data-act="plaidconnect" data-b="${c.id}">Connect via Plaid</button>`;
  }
  return `<div class="group-lbl">Bank sync <span class="ct">connected</span></div>
    <div class="sub">${esc(item.institution_name || 'Linked bank')}${item.last_synced_at ? ' · last synced ' + fmtDay(item.last_synced_at) : ' · not yet synced'}</div>
    <button class="mini ghost" data-act="plaidsync" data-b="${c.id}">Sync now</button>
    <button class="mini ghost" data-act="plaiddisconnect" data-b="${c.id}">Disconnect</button>`;
}

// Rent points earned (Bilt, Option A) — the card's actual job: points that
// wouldn't exist without it. Valued at the rate shown, which sharpens toward
// the card's own real redemption ¢/pt as those get logged (see pointsSection).
//
// Laid out as the same month-toggle grid as any other monthly credit
// (periodGrid) rather than 12 stacked rows — a month with nothing logged
// just reads "add", not a wasted full-width input. Click a month to open the
// shared edit strip (same pattern, same Enter/Escape handling) instead of
// giving every one of the 12 months its own always-visible input.
const MONTH_NAMES = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];

function rentPointsSection(c) {
  const rp = c.rent_points;
  const year = parseInt(STATE.today.slice(0, 4), 10);
  const curMonth = STATE.today.slice(0, 4) === String(year) ? parseInt(STATE.today.slice(5, 7), 10) : 12;
  const byMonth = {};
  rp.months.forEach(m => { byMonth[m.month] = m.points; });
  // Statement model (derived from Plaid spend) — suggestions only. A month
  // you've filled in yourself is never overwritten or auto-corrected.
  const stmt = {};
  (c.statement_months || []).forEach(m => { stmt[m.month] = m; });

  const cells = MONTH_NAMES.map((name, i) => {
    const month = i + 1;
    const s = stmt[month];
    const stored = Object.prototype.hasOwnProperty.call(byMonth, month);
    // Same rule the statement model uses: a 0 on a month that DID pay rent
    // can't be a real figure (the floor is the flat 250-pt tier), so it reads
    // as an empty slot rather than a green "0 pts earned".
    const placeholder = stored && byMonth[month] === 0 && s && s.rent > 0;
    const has = stored && !placeholder;
    const pts = has ? byMonth[month] : 0;
    const future = month > curMonth;
    // Two flavours of derived number, both shown IN the cell so the figure is
    // never hidden in prose: a firm suggestion once the statement has closed,
    // and a provisional projection while it's still running.
    const suggesting = !has && s && s.suggest;
    const projecting = !has && !suggesting && s && !s.closed && s.rent > 0 && s.projected_points;
    // Clicking pre-fills the edit strip with the derived figure, so accepting
    // is one click + Enter — but it's still your save.
    const prefill = (suggesting || projecting) ? s.projected_points : pts;
    const state = has ? 'done'
      : suggesting ? 'suggest'
        : projecting ? 'projecting'
          : (future ? 'future' : 'open');
    const sel = editing && editing.kind === 'rentpoints' && editing.id === c.id && editing.index === month;
    const val = has ? fmtInt(pts) + ' pts'
      : (suggesting || projecting) ? fmtInt(s.projected_points)
        : (future ? '—' : 'add');
    const hint = has ? ''
      : suggesting ? `<div class="mhint">${s.multiplier}x · tap to log</div>`
        : projecting ? `<div class="mhint">${s.multiplier}x so far · open</div>`
          : '';
    const flag = (s && s.disagrees) ? '<span class="tick" style="color:var(--warn)">!</span>' : '';
    const title = (suggesting || projecting)
      ? `${money(s.spend)} non-rent spend = ${s.pct_of_rent}% of ${money(s.rent)} rent → ${s.multiplier}x`
      : `${name} ${year} rent points`;
    return `<button class="mo ${state} ${sel ? 'editing' : ''}" ${future ? 'disabled' : ''}
        data-act="selectrentpoints" data-b="${c.id}" data-i="${month}"
        data-cap="999999999" data-cur="${prefill}" data-lbl="${esc(name)} ${year} rent points"
        title="${esc(title)}">
        <span class="tick">${has ? '✓' : ''}</span>${flag}
        <div class="mlab">${name}</div><div class="mval tnum">${val}</div>${hint}</button>`;
  }).join('');
  const strip = (editing && editing.kind === 'rentpoints' && editing.id === c.id) ? editStrip() : '';
  // Placeholder zeros aren't logged months — don't inflate the count with them.
  const loggedCount = rp.months.filter(m =>
    !(m.points === 0 && stmt[m.month] && stmt[m.month].rent > 0)).length;
  return `<div class="group-lbl">Rent points earned <span class="ct">counts toward the verdict</span></div>
    <div class="barlab" style="margin:8px 0 4px">
      <span>Value this year <b>${money(rp.value)}</b></span>
      <span>Rate <b>${rp.rate_cpp.toFixed(2)}¢/pt${rp.rate_is_default ? ' (default)' : ''}</b></span>
      <span>${fmtInt(rp.total_points)} pts <b>·</b> ${loggedCount} of 12 months logged</span>
    </div>
    <div class="months">${cells}</div>
    ${strip}
    ${statementNotes(c, stmt, curMonth, year)}
    <div class="mgrid-foot">Click a month to log rent points earned. Green = logged by you · dashed blue = derived from your statement spend (solid = statement closed, ready to accept; faded = window still open, figure can still move) · faint = upcoming.
      ${rp.rate_is_default ? ' Valued at a conservative flat 1¢/pt until you log a real Bilt redemption below — Bilt points commonly transfer above 1¢, so this is a floor, not an optimistic guess.' : ''}</div>`;
}

// The evidence behind each suggestion, plus the tier-cliff nudge for the month
// still in progress. Showing the spend and the percentage is the point — a
// bare projected number would be a black box you'd have to trust blindly.
function statementNotes(c, stmt, curMonth, year) {
  const out = [];
  const open = stmt[curMonth];
  if (open && open.rent && !open.closed) {
    const nt = open.next_tier;
    const nudge = nt && nt.points_gained > 0
      ? ` <b>${money(nt.spend_needed)} more</b> on Bilt before then reaches ${nt.pct}% → ${nt.multiplier}x, worth <b>${fmtInt(nt.points_gained)}</b> extra points.`
      : ' Already at the top tier — further spend earns its plain rate, not more rent points.';
    out.push(`<div class="noteblock"><b>${MONTH_NAMES[curMonth - 1]}</b> is still open (closes ${fmtDay(open.window_end)}):
      ${money(open.spend)} non-rent spend = <b>${open.pct_of_rent}%</b> of ${money(open.rent)} rent.${nudge}</div>`);
  }
  const ready = Object.values(stmt).filter(m => m.suggest);
  if (ready.length) {
    out.push(`<div class="noteblock">${ready.length === 1 ? 'One month is' : ready.length + ' months are'} ready to log
      from your statement spend — the dashed blue ${ready.length === 1 ? 'cell' : 'cells'} above
      (${ready.map(m => `<b>${MONTH_NAMES[m.month - 1]} ${fmtInt(m.projected_points)}</b>`).join(', ')}).
      Tap one to accept or edit it; nothing is saved until you do.</div>`);
  }
  Object.values(stmt).filter(m => m.disagrees).forEach(m => {
    out.push(`<div class="noteblock warn">${MONTH_NAMES[m.month - 1]} ${year}: you logged
      <b>${fmtInt((c.rent_points.months.find(x => x.month === m.month) || {}).points || 0)}</b> points, but your statement spend
      (${money(m.spend)} = ${m.pct_of_rent}% of rent → ${m.multiplier}x) implies <b>${fmtInt(m.projected_points)}</b>.
      Your figure is kept as-is — this is flagged because a mismatch means the model is wrong somewhere.</div>`);
  });
  return out.join('');
}

// Points value — logged per card, per the user's mapping (all Amex -> Plat,
// all Chase -> CSR, Bilt -> Bilt). For hybrid/points cards this feeds the
// verdict; for credits cards (Platinum) it's shown as bonus value only.
let addingPoints = false;

function pointsSection(c) {
  const p = c.points;
  const badge = p.counts_in_verdict
    ? `<span class="ct">counts toward the verdict</span>`
    : `<span class="ct">bonus value — verdict runs on ${c.rent_points ? 'rent points earned' : 'credits'}</span>`;
  const summary = p.count
    ? `<div class="barlab" style="margin:8px 0 4px">
         <span>Realized this year <b>${money(p.realized)}</b></span>
         <span>Avg <b>${p.avg_cpp.toFixed(2)}¢/pt</b></span>
         <span>${fmtInt(p.points_used)} ${esc(p.short)} used <b>·</b> ${p.count} redemption${p.count === 1 ? '' : 's'}</span>
       </div>`
    : `<div class="sub" style="margin:8px 0 4px">No redemptions logged yet.</div>`;
  const list = p.items.map(it => `
    <div class="bucket">
      <div class="bn">${esc(it.description || 'Redemption')}
        <small>${fmtInt(it.card_points)} ${esc(p.short)}${it.partner ? ` → ${esc(it.partner)}${it.transfer_bonus_pct ? ` (+${fmtInt(it.transfer_bonus_pct)}%)` : ''}` : ''} · ${fmtDay(it.date)}</small></div>
      <div style="text-align:right;white-space:nowrap">
        <b class="tnum">${money(it.realized)}</b>
        <span class="es-cap"> · ${it.cpp.toFixed(2)}¢/pt</span>
        <button class="mini ghost" data-act="delpoints" data-b="${it.id}" title="Delete">✕</button></div>
    </div>`).join('');
  return `<div class="group-lbl">Points value <span class="ct">${esc(p.program)}</span> ${badge}</div>
    ${summary}
    ${list}
    ${addPointsForm(c.id)}
    ${c.rent_points ? `<div class="noteblock" style="margin-top:10px">Not counted here — this card's verdict runs on
      rent points earned instead (below). Redemptions you log still matter: they refine the ¢/pt rate rent
      points get valued at, in place of the ${c.rent_points.rate_is_default ? 'current default 1¢' : 'current'}.</div>` : ''}`;
}

function addPointsForm(cardId) {
  if (!addingPoints) {
    return `<button class="mini ghost" style="margin-top:10px" data-act="showaddpoints">+ Log a redemption</button>`;
  }
  return `<div class="editstrip pointsform" style="margin-top:10px">
    <input id="pt_desc" class="tnum" placeholder="what you booked" style="width:190px">
    <div class="es-input"><input id="pt_points" type="number" min="1" step="1" inputmode="numeric"
      class="tnum" placeholder="card points" style="width:110px"></div>
    <div class="es-input">$<input id="pt_cash" type="number" min="0.01" step="0.01" inputmode="decimal"
      class="tnum" placeholder="cash value" style="width:100px"></div>
    <span class="es-cap">— transfer (optional):</span>
    <input id="pt_partner" class="tnum" placeholder="partner" style="width:90px">
    <div class="es-input"><input id="pt_bonus" type="number" min="0" step="1" inputmode="numeric"
      class="tnum" placeholder="bonus" style="width:64px"></div><span class="es-cap">% bonus</span>
    <div class="es-input"><input id="pt_ppt" type="number" min="0" step="1" inputmode="numeric"
      class="tnum" placeholder="total partner pts" style="width:130px"></div>
    <button class="mini" data-act="addpoints" data-b="${cardId}">Log</button>
    <button class="mini ghost" data-act="canceladdpoints">Cancel</button>
  </div>`;
}

function submitAddPoints(cardId) {
  const num = id => { const v = parseFloat(($(id) || {}).value); return isNaN(v) ? null : v; };
  const card_points = num('pt_points');
  const cash_value = num('pt_cash');
  if (!(card_points > 0) || !(cash_value > 0)) return;
  addingPoints = false;
  post('/api/points', {
    card_id: cardId,
    description: ($('pt_desc') || {}).value || '',
    card_points, cash_value,
    partner: ($('pt_partner') || {}).value || '',
    transfer_bonus_pct: num('pt_bonus') || 0,
    partner_points_total: num('pt_ppt') || '',
  });
}

function renderRefs() {
  if (active !== 'overview' || !STATE.reference.length) return '';
  return `<details class="refs"><summary>Reference only — ${STATE.reference.length} benefits, never counted in ROI</summary>
    <div class="noteblock">Contingent coverage and elite statuses. Real value, but you can't redeem
      insurance on a schedule — counting these would add thousands in phantom value and make every
      card look unconditionally worth keeping.</div>
    <table class="reftbl">${STATE.reference.map(r => `<tr><td class="refcard">${esc(r.card_label)}</td><td>${esc(r.name)}</td>
      <td>${esc(r.coverage || '')}${r.note ? ' — ' + esc(r.note) : ''}</td></tr>`).join('')}</table></details>`;
}

function render() {
  if (!STATE) return;
  const d = new Date(STATE.today + 'T12:00:00')
    .toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric', year: 'numeric' });
  const ss = STATE.sync_status || {};
  // Sync runs unattended, so its failure mode is silence — surface the age.
  $('today').innerHTML = `${d}` + (ss.label
    ? ` <span class="syncpill ${ss.state}" title="Oldest card last synced: ${ss.oldest || 'never'}">${esc(ss.label)}</span>` : '');
  renderTabs();
  if (active === 'overview') renderOverview(); else renderCard(card(active));
  $('view').insertAdjacentHTML('beforeend', renderRefs());
  if (flashRow) { const t = flashRow; flashRow = null; setTimeout(() => { const el = $('row-' + t); if (el) el.classList.remove('justopened'); }, 1400); }
}

// --- events (delegated, so re-rendering never loses handlers) ---------------

document.addEventListener('click', e => {
  const t = e.target.closest('[data-go],[data-goto],[data-act]');
  if (!t) return;
  if (t.dataset.goto) return goBenefit(t.dataset.goto, t.dataset.target);
  if (t.dataset.go) return go(t.dataset.go);
  const { act, b, i, v, cap, cur, lbl } = t.dataset;
  if (act === 'toggle') return toggleRow(b);
  if (act === 'selectperiod') return selectCell('period', b, i, cap, cur, lbl);
  if (act === 'selectcash') return selectCell('cash', b, i, cap, cur, lbl);
  if (act === 'saveedit') return commitEdit();
  if (act === 'clearedit') return commitEdit(0);
  if (act === 'canceledit') return cancelEdit();
  if (act === 'appl') return setAppl(b, v === '' ? null : v === '1');
  if (act === 'redeem') return redeemRest(b, v);
  if (act === 'setreal') {
    const input = document.querySelector(`[data-real="${b}"]`);
    return setReal(b, input ? input.value : '');
  }
  if (act === 'showaddchannel') { addingChannel = true; render();
    requestAnimationFrame(() => { const el = $('newchname'); if (el) el.focus(); }); return; }
  if (act === 'canceladdchannel') { addingChannel = false; return render(); }
  if (act === 'addchannel') return submitAddChannel();
  if (act === 'showaddearn') { addingEarn = true; render();
    requestAnimationFrame(() => { const el = $('newearnamt'); if (el) el.focus(); }); return; }
  if (act === 'canceladdearn') { addingEarn = false; return render(); }
  if (act === 'addearn') return submitAddEarn();
  if (act === 'showaddpoints') { addingPoints = true; render();
    requestAnimationFrame(() => { const el = $('pt_desc'); if (el) el.focus(); }); return; }
  if (act === 'canceladdpoints') { addingPoints = false; return render(); }
  if (act === 'addpoints') return submitAddPoints(b);
  if (act === 'delpoints') return post('/api/points/' + b + '/delete', {});
  if (act === 'selectrentpoints') return selectCell('rentpoints', b, i, cap, cur, lbl);
  if (act === 'plaidconnect') return plaidConnect(b);
  if (act === 'plaidsync') return plaidSync(b);
  if (act === 'plaiddisconnect') return post('/api/plaid/disconnect/' + b, {});
  if (act === 'year') return switchYear(t, parseInt(t.dataset.y, 10));
  if (act === 'openreview') { reviewOpen = true; return render(); }
  if (act === 'closereview') { reviewOpen = false; return render(); }
  if (act === 'reviewconfirm') return post(`/api/review/${t.dataset.txn}/confirm`, { benefit_id: t.dataset.benefit });
  if (act === 'bulkconfirm') return post('/api/review/bulk-confirm', { txn_ids: t.dataset.txns, benefit_id: t.dataset.benefit });
  if (act === 'reviewreject') {
    const ids = (t.dataset.txns || '').split(',').filter(Boolean);
    return Promise.all(ids.map(id => postRaw(`/api/review/${id}/reject`, { reason: t.dataset.reason || '' })))
      .then(load);
  }
});

// Assigning a reviewed transaction to a credit the matcher didn't suggest.
document.addEventListener('change', e => {
  const sel = e.target.closest('select.pickben');
  if (!sel || !sel.value) return;
  const ids = (sel.dataset.txns || '').split(',').filter(Boolean);
  post('/api/review/bulk-confirm', { txn_ids: ids.join(','), benefit_id: sel.value });
});

// Enter saves, Escape cancels, while focus is in the amount input.
document.addEventListener('keydown', e => {
  if (editing && e.target.id === 'editamt') {
    if (e.key === 'Enter') { e.preventDefault(); commitEdit(); }
    else if (e.key === 'Escape') { e.preventDefault(); cancelEdit(); }
    return;
  }
  if (addingChannel && (e.target.id === 'newchname' || e.target.id === 'newchcap')) {
    if (e.key === 'Enter') { e.preventDefault(); submitAddChannel(); }
    else if (e.key === 'Escape') { e.preventDefault(); addingChannel = false; render(); }
    return;
  }
  if (addingEarn && (e.target.id === 'newearnamt' || e.target.id === 'newearnnote')) {
    if (e.key === 'Enter') { e.preventDefault(); submitAddEarn(); }
    else if (e.key === 'Escape') { e.preventDefault(); addingEarn = false; render(); }
    return;
  }
  if (addingPoints && e.target.id && e.target.id.startsWith('pt_')) {
    if (e.key === 'Enter') { e.preventDefault(); submitAddPoints(active); }
    else if (e.key === 'Escape') { e.preventDefault(); addingPoints = false; render(); }
    return;
  }
});

$('themebtn').addEventListener('click', () => {
  const r = document.documentElement;
  const cur = r.getAttribute('data-theme') ||
    (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  const next = cur === 'dark' ? 'light' : 'dark';
  r.setAttribute('data-theme', next);
  try { localStorage.setItem('theme', next); } catch (_) {}
});
try { const t = localStorage.getItem('theme'); if (t) document.documentElement.setAttribute('data-theme', t); } catch (_) {}

function fmtDay(iso) {
  const d = new Date(iso + 'T12:00:00');
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}
function fmtInt(n) { return Math.round(n).toLocaleString(); }
function esc(s) {
  return String(s).replace(/[&<>"]/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[ch]));
}

resumePlaidOAuthIfNeeded();
load();
