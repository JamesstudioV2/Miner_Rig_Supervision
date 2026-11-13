// Fő modul: élő adatkapcsolat (Server-Sent Events, tartalék: lekérdezés), fülek kezelése, fejléc.
import { api, $, $$, toast, confirmDialog } from './util.js';
import { Dashboard } from './dashboard.js';
import { HistoryView } from './history.js';
import { EventsView } from './events.js';
import { SettingsView } from './settings.js';

const store = { system: null, miners: [], summary: null, events: [], sim: false, mode: {}, version: '', lastMsg: 0 };
const views = {};
let current = null;
let renderQueued = false;

function setConn(state) {
  const el = $('#conn');
  el.className = 'conn ' + state;
  el.title = { live: 'Élő kapcsolat', wait: 'Újracsatlakozás…', down: 'Nincs kapcsolat' }[state] || '';
  $('#conn-banner').hidden = state !== 'down';
}

function applyMessage(d) {
  if (d.system) store.system = d.system;
  if (d.miners) store.miners = d.miners;
  if (d.summary) store.summary = d.summary;
  if (d.events) store.events = d.events;
  if (d.sim !== undefined) store.sim = d.sim;
  if (d.mode) store.mode = d.mode;
  if (d.version) store.version = d.version;
  store.lastMsg = Date.now();
  setConn('live');
  if (!renderQueued) {
    renderQueued = true;
    requestAnimationFrame(() => { renderQueued = false; render(); });
  }
}

function render() {
  const s = store.system;
  if (!s) return;
  const pill = $('#state-pill');
  pill.dataset.state = s.system.state;
  pill.querySelector('span').textContent = s.system.label + (s.system.alarm ? ' · ' + s.system.alarm.code : '');
  if ($('#site-name').textContent !== s.site_name) {
    $('#site-name').textContent = s.site_name;
    document.title = s.system.label + ' – ' + s.site_name;
  }
  const banner = $('#sim-banner');
  banner.hidden = !store.sim;
  if (store.sim) {
    // Raspberry Pi-n a szimuláció azt jelenti, hogy a valódi lábak NEM változnak – ezt nem lehet eléggé hangsúlyozni
    const onPi = !!store.mode.on_pi;
    banner.classList.toggle('banner-warn', onPi);
    $('#sim-banner-text').textContent = onPi
      ? 'SZIMULÁCIÓS MÓD – a program most NEM vezérli a Raspberry Pi GPIO lábait (szivattyúk, szenzorok), minden adat szimulált!'
      : 'SZIMULÁCIÓS MÓD – ez a gép nem Raspberry Pi, minden adat szimulált.';
    $('#sim-switch').hidden = !onPi;
  }
  // olvasatlan hiba/figyelmeztetés események száma
  let seen = 0;
  try { seen = parseInt(localStorage.getItem('rig.lastSeenEvent') || '0', 10); } catch (e) { /* ignore */ }
  const unseen = (store.events || []).filter((e) => e.level !== 'info' && e.id > seen).length;
  const badge = $('#events-badge');
  badge.hidden = !unseen || current === 'events';
  badge.textContent = unseen > 9 ? '9+' : String(unseen);
  if (current === 'dashboard') views.dashboard.render();
  if (current === 'settings') views.settings.live();
}

// --------------------------------------------------------- kapcsolat
let es = null;
let errors = 0;
let pollTimer = null;

function connect() {
  if (!window.EventSource) { startPolling(); return; }
  es = new EventSource('/api/stream');
  es.onmessage = (ev) => {
    errors = 0;
    try { applyMessage(JSON.parse(ev.data)); } catch (e) { console.error(e); }
  };
  es.onerror = () => {
    errors += 1;
    if (Date.now() - store.lastMsg > 4000) setConn('wait');
    if (errors > 5) {
      es.close();
      es = null;
      startPolling();
    }
  };
}

function startPolling() {
  if (pollTimer) return;
  const tick = async () => {
    try {
      applyMessage(await api('/api/state'));
    } catch (e) {
      setConn(Date.now() - store.lastMsg > 6000 ? 'down' : 'wait');
    }
  };
  tick();
  pollTimer = setInterval(tick, 2000);
  // időnként megpróbáljuk visszaállítani az élő (SSE) kapcsolatot
  setTimeout(() => {
    if (!window.EventSource) return;
    clearInterval(pollTimer);
    pollTimer = null;
    errors = 0;
    connect();
  }, 30000);
}

setInterval(() => {
  if (store.lastMsg && Date.now() - store.lastMsg > 6000) setConn('down');
}, 1000);

// ------------------------------------------------------------ fülek
function route() {
  const name = (location.hash || '#dashboard').slice(1);
  const tab = ['dashboard', 'charts', 'events', 'settings'].includes(name) ? name : 'dashboard';
  if (tab === current) return;
  if (current && views[current] && views[current].hide) views[current].hide();
  current = tab;
  for (const v of $$('.view')) v.hidden = v.dataset.view !== tab;
  for (const a of $$('#tabs a')) a.classList.toggle('active', a.dataset.tab === tab);
  if (views[tab] && views[tab].show) views[tab].show();
  if (tab === 'dashboard') render();
  window.scrollTo({ top: 0 });
}

async function switchToRealHardware() {
  const ok = await confirmDialog('Váltás valódi hardverre',
    'A program ezután a Raspberry Pi valódi GPIO lábait vezérli (szivattyú SSR-ek), és a valódi szenzorokat olvassa. ' +
    'A váltás azonnal érvényes, újraindítás nem kell. Folytatod?', 'Váltás');
  if (!ok) return;
  const done = await views.settings.applyMode('rpi');
  if (done && current !== 'settings') toast('Ellenőrizd a lábkiosztást: Beállítások › Kalibráció & GPIO', 'ok', 7000);
}

async function init() {
  $('#sim-switch').addEventListener('click', switchToRealHardware);
  views.dashboard = new Dashboard(store);
  views.charts = new HistoryView();
  views.events = new EventsView();
  views.settings = new SettingsView(store);
  window.addEventListener('hashchange', route);
  route();
  connect();
  try {
    const d = await api('/api/config');
    views.events.setCatalog(d.meta.error_catalog);
  } catch (e) { /* a jegyzék később is betöltődhet */ }
}

init();
