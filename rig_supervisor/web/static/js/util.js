// Közös segédfüggvények: API hívás, formázás, DOM, értesítések, párbeszédablakok.

export async function api(path, { method = 'GET', body, quiet = false } = {}) {
  const opts = { method, headers: {}, cache: 'no-store' };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, opts);
  } catch (e) {
    const err = new Error('Nincs kapcsolat a vezérlővel');
    err.network = true;
    throw err;
  }
  let data = null;
  try { data = await res.json(); } catch (e) { /* nem JSON */ }
  if (!res.ok || (data && data.ok === false)) {
    const err = new Error((data && (data.error || data.message)) || ('HTTP ' + res.status));
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

// ---------------------------------------------------------------- formázás
const nf = {};
function numFmt(digits) {
  if (!nf[digits]) nf[digits] = new Intl.NumberFormat('hu-HU', { minimumFractionDigits: digits, maximumFractionDigits: digits });
  return nf[digits];
}
export function num(v, digits = 1) {
  if (v === null || v === undefined || !isFinite(v)) return '—';
  return numFmt(digits).format(v);
}
export function temp(v, digits = 1) {
  return v === null || v === undefined ? '—' : num(v, digits) + ' °C';
}
const HS_UNITS = [[1e18, 'EH/s'], [1e15, 'PH/s'], [1e12, 'TH/s'], [1e9, 'GH/s'], [1e6, 'MH/s'], [1e3, 'kH/s'], [1, 'H/s']];
export function hashrate(hs, digits) {
  if (hs === null || hs === undefined || !isFinite(hs)) return '—';
  if (hs === 0) return '0 TH/s';
  for (const [div, unit] of HS_UNITS) {
    if (Math.abs(hs) >= div) {
      const v = hs / div;
      const d = digits !== undefined ? digits : (v >= 100 ? 1 : 2);
      return num(v, d) + ' ' + unit;
    }
  }
  return num(hs, 0) + ' H/s';
}
export function power(w) {
  if (w === null || w === undefined || !isFinite(w)) return '—';
  if (Math.abs(w) >= 10000) return num(w / 1000, 1) + ' kW';
  if (Math.abs(w) >= 1000) return num(w / 1000, 2) + ' kW';
  return num(w, 0) + ' W';
}
export function bytes(b) {
  if (b === null || b === undefined) return '—';
  const units = ['B', 'kB', 'MB', 'GB', 'TB'];
  let i = 0;
  let v = b;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return num(v, i >= 3 ? 2 : i === 0 ? 0 : 1) + ' ' + units[i];
}
export function duration(sec) {
  if (sec === null || sec === undefined || !isFinite(sec)) return '—';
  sec = Math.max(0, Math.floor(sec));
  if (sec < 60) return sec + ' mp';
  const m = Math.floor(sec / 60);
  if (m < 60) return m + ' perc';
  const h = Math.floor(m / 60);
  if (h < 48) return h + ' óra ' + (m % 60) + ' perc';
  const d = Math.floor(h / 24);
  return d + ' nap ' + (h % 24) + ' óra';
}
export function ago(ts) {
  if (!ts) return '—';
  const s = Date.now() / 1000 - ts;
  if (s < 5) return 'épp most';
  if (s < 60) return Math.floor(s) + ' mp-e';
  if (s < 3600) return Math.floor(s / 60) + ' perce';
  if (s < 86400) return Math.floor(s / 3600) + ' órája';
  return Math.floor(s / 86400) + ' napja';
}
const dtf = new Intl.DateTimeFormat('hu-HU', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' });
const tf = new Intl.DateTimeFormat('hu-HU', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
const df = new Intl.DateTimeFormat('hu-HU', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
export function dateTime(ts) { return ts ? dtf.format(new Date(ts * 1000)) : '—'; }
export function timeOnly(ts) { return ts ? tf.format(new Date(ts * 1000)) : '—'; }
export function shortDateTime(ts) { return ts ? df.format(new Date(ts * 1000)) : '—'; }
export function eventTime(ts) {
  const d = new Date(ts * 1000);
  const today = new Date();
  if (d.toDateString() === today.toDateString()) return tf.format(d);
  return df.format(d);
}

// --------------------------------------------------------------------- DOM
export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
export function esc(s) {
  return String(s === null || s === undefined ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
export function h(html) {
  const t = document.createElement('template');
  t.innerHTML = html.trim();
  return t.content.firstElementChild;
}

// ----------------------------------------------------------------- toasts
export function toast(msg, kind = 'ok', ms = 3800) {
  const root = document.getElementById('toasts');
  const el = document.createElement('div');
  el.className = 'toast ' + kind;
  el.textContent = msg;
  root.appendChild(el);
  setTimeout(() => {
    el.style.transition = 'opacity .3s';
    el.style.opacity = '0';
    setTimeout(() => el.remove(), 320);
  }, ms);
}

// ------------------------------------------------------------------ modal
let modalStack = [];
export function openModal({ title, body, actions = [], wide = false, onClose } = {}) {
  const root = document.getElementById('modal-root');
  const back = document.createElement('div');
  back.className = 'modal-back';
  back.innerHTML = `<div class="modal${wide ? ' wide' : ''}" role="dialog" aria-modal="true">
      <div class="modal-head"><h3></h3><button class="btn btn-icon btn-ghost x" title="Bezárás" aria-label="Bezárás">×</button></div>
      <div class="modal-body"></div>
      <div class="modal-foot"></div></div>`;
  back.querySelector('h3').textContent = title || '';
  const bodyEl = back.querySelector('.modal-body');
  if (typeof body === 'string') bodyEl.innerHTML = body;
  else if (body) bodyEl.appendChild(body);
  const foot = back.querySelector('.modal-foot');
  const close = () => {
    back.remove();
    modalStack = modalStack.filter((m) => m !== api_);
    document.removeEventListener('keydown', onKey);
    if (onClose) onClose();
  };
  const api_ = { el: back, body: bodyEl, foot, close };
  for (const a of actions) {
    const b = document.createElement('button');
    b.className = 'btn ' + (a.cls || '');
    b.textContent = a.label;
    b.addEventListener('click', () => a.onClick ? a.onClick(api_, b) : close());
    if (a.id) b.id = a.id;
    foot.appendChild(b);
  }
  if (!actions.length) foot.remove();
  back.querySelector('.x').addEventListener('click', close);
  back.addEventListener('mousedown', (e) => { if (e.target === back) close(); });
  const onKey = (e) => { if (e.key === 'Escape' && modalStack[modalStack.length - 1] === api_) close(); };
  document.addEventListener('keydown', onKey);
  root.appendChild(back);
  modalStack.push(api_);
  return api_;
}

export function confirmDialog(title, text, okLabel = 'Igen', danger = false) {
  return new Promise((resolve) => {
    let answered = false;
    const m = openModal({
      title,
      body: `<p style="margin:0;font-size:15px">${text}</p>`,
      actions: [
        { label: 'Mégse', cls: 'btn-ghost', onClick: (mm) => { answered = true; mm.close(); resolve(false); } },
        { label: okLabel, cls: danger ? 'btn-danger' : 'btn-primary', onClick: (mm) => { answered = true; mm.close(); resolve(true); } },
      ],
      onClose: () => { if (!answered) resolve(false); },
    });
    const btns = m.foot.querySelectorAll('button');
    btns[btns.length - 1].focus();
  });
}

// ------------------------------------------------------------ hőszín skála
const TEMP_STOPS = [[5, [37, 99, 235]], [18, [6, 182, 212]], [32, [250, 204, 21]], [45, [249, 115, 22]], [58, [239, 68, 68]]];
export function tempColor(t) {
  if (t === null || t === undefined || !isFinite(t)) return '#5b6b7e';
  if (t <= TEMP_STOPS[0][0]) return rgb(TEMP_STOPS[0][1]);
  for (let i = 1; i < TEMP_STOPS.length; i++) {
    const [t1, c1] = TEMP_STOPS[i];
    const [t0, c0] = TEMP_STOPS[i - 1];
    if (t <= t1) {
      const k = (t - t0) / (t1 - t0);
      return rgb(c0.map((v, j) => v + (c1[j] - v) * k));
    }
  }
  return rgb(TEMP_STOPS[TEMP_STOPS.length - 1][1]);
}
function rgb(c) { return `rgb(${Math.round(c[0])},${Math.round(c[1])},${Math.round(c[2])})`; }

export const STATUS_LABELS = {
  mining: 'Bányászik', starting: 'Indul…', restarting: 'Újraindul…', sleeping: 'Alvó mód', idle: 'Tétlen',
  offline: 'Nem elérhető', connecting: 'Kapcsolódás…', disabled: 'Kikapcsolva',
};
