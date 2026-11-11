// Grafikonok fül: választható időtartomány, összehangolt grafikonok, nagyítás, CSV export.
import { api, $, $$, esc, toast, num, hashrate as fmtHash, power as fmtPower, dateTime } from './util.js';
import { TimeChart, HoverBus } from './chart.js';

const SYS_COLORS = { t1: '#60a5fa', t2: '#f97316', t3: '#f43f5e', t4: '#2dd4bf' };
const MINER_COLORS = ['#38bdf8', '#a78bfa', '#f472b6', '#facc15', '#34d399', '#fb923c', '#60a5fa', '#e879f9', '#4ade80', '#f87171', '#22d3ee', '#c084fc'];
const LS_KEY = 'rig.range';

function toLocalInput(ts) {
  const d = new Date(ts * 1000);
  const p = (n) => (n < 10 ? '0' : '') + n;
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}

export class HistoryView {
  constructor() {
    this.bus = new HoverBus();
    this.charts = {};
    this.range = 86400;
    this.end = null;        // null = "most" (élő)
    this.stack = [];
    this.loading = false;
    this.timer = null;
    try {
      const saved = JSON.parse(localStorage.getItem(LS_KEY) || 'null');
      if (saved && saved.range) this.range = saved.range;
    } catch (e) { /* localStorage tiltva */ }
    this._buildCharts();
    this._bind();
  }

  _card(id, title, opts) {
    const card = document.createElement('div');
    card.className = 'card chart-card';
    card.innerHTML = `<div class="chart-head"><div class="card-title">${esc(title)}</div><div class="chart-legend"></div></div><div class="chart-host"></div>`;
    $('#charts').appendChild(card);
    const chart = new TimeChart(card.querySelector('.chart-host'), Object.assign({
      bus: this.bus,
      onZoom: (a, b) => this.zoom(a, b),
      onZoomReset: () => this.unzoom(),
    }, opts));
    chart.legendEl = card.querySelector('.chart-legend');
    chart.card = card;
    this.charts[id] = chart;
    return chart;
  }

  _buildCharts() {
    this._card('temps', 'Vízhőmérsékletek (°C)', { unit: '°C', digits: 1, height: 250 });
    this._card('dts', 'Hőmérséklet-különbségek (°C)', { unit: '°C', digits: 1, height: 180 });
    this._card('states', 'Üzemállapot, szivattyúk és áramlás', { mode: 'timeline' });
    this._card('hash', 'Hashrate', { height: 230, zeroBased: true });
    this._card('mtemp', 'Miner hőmérsékletek – legmelegebb chip (°C)', { unit: '°C', digits: 0, height: 230 });
    this._card('power', 'Teljesítményfelvétel', { height: 200, zeroBased: true });
  }

  _legend(chart) {
    chart.legendEl.innerHTML = chart.series.map((s) => `<span class="lg${s.hidden ? ' off' : ''}" data-key="${esc(s.key)}"><i style="background:${s.color}"></i>${esc(s.label)}</span>`).join('');
    chart.legendEl.onclick = (e) => {
      const el = e.target.closest('.lg');
      if (!el) return;
      const s = chart.series.find((x) => x.key === el.dataset.key);
      if (!s) return;
      chart.setHidden(s.key, !s.hidden);
      el.classList.toggle('off', s.hidden);
    };
  }

  _bind() {
    $('#range-presets').addEventListener('click', (e) => {
      const b = e.target.closest('button');
      if (!b) return;
      this.range = +b.dataset.range;
      this.end = null;
      this.stack = [];
      try { localStorage.setItem(LS_KEY, JSON.stringify({ range: this.range })); } catch (err) { /* ignore */ }
      this.load();
    });
    $('#range-prev').addEventListener('click', () => { this.end = this._end() - this.range; this.stack = []; this.load(); });
    $('#range-next').addEventListener('click', () => {
      const e = this._end() + this.range;
      this.end = e >= Date.now() / 1000 - 5 ? null : e;
      this.stack = [];
      this.load();
    });
    $('#range-now').addEventListener('click', () => { this.end = null; this.stack = []; this.load(); });
    $('#range-apply').addEventListener('click', () => {
      const a = Date.parse($('#range-from').value) / 1000;
      const b = Date.parse($('#range-to').value) / 1000;
      if (!isFinite(a) || !isFinite(b) || b <= a) { toast('Érvénytelen időtartomány', 'warn'); return; }
      this.range = Math.round(b - a);
      this.end = b >= Date.now() / 1000 - 5 ? null : Math.round(b);
      this.stack = [];
      this.load();
    });
    $('#range-live').addEventListener('change', () => this._schedule());
  }

  _end() { return this.end === null ? Math.floor(Date.now() / 1000) : this.end; }

  zoom(a, b) {
    if (b - a < 60) b = a + 60;
    this.stack.push({ range: this.range, end: this.end });
    this.range = b - a;
    this.end = b >= Date.now() / 1000 - 5 ? null : b;
    this.load();
  }

  unzoom() {
    const prev = this.stack.pop();
    if (!prev) return;
    this.range = prev.range;
    this.end = prev.end;
    this.load();
  }

  show() {
    this.load();
    this._schedule();
  }

  hide() {
    clearTimeout(this.timer);
    this.timer = null;
  }

  _schedule() {
    clearTimeout(this.timer);
    if (!$('#range-live').checked || this.end !== null || document.querySelector('#view-charts').hidden) return;
    const every = Math.max(10, Math.min(300, this.range / 120));
    this.timer = setTimeout(() => { this.load(true); }, every * 1000);
  }

  async load(silent = false) {
    if (this.loading) return;
    this.loading = true;
    const end = this._end();
    const start = end - this.range;
    for (const b of $$('#range-presets button')) b.classList.toggle('active', +b.dataset.range === this.range && this.end === null);
    $('#range-from').value = toLocalInput(start);
    $('#range-to').value = toLocalInput(end);
    $('#csv-link').href = `/api/export.csv?start=${start}&end=${end}`;
    const width = $('#charts').clientWidth || 800;
    const points = Math.max(150, Math.min(1500, Math.round(width / 1.6)));
    try {
      const d = await api(`/api/history?start=${start}&end=${end}&points=${points}`);
      this._render(d);
      const bucketTxt = d.bucket >= 3600 ? num(d.bucket / 3600, 1) + ' óra' : d.bucket >= 60 ? num(d.bucket / 60, 0) + ' perc' : d.bucket + ' mp';
      $('#range-info').textContent = `${dateTime(start)} – ${dateTime(end)} · felbontás: ${bucketTxt} átlag (${d.source === '5m' ? '5 perces összesítés' : 'nyers adatok'})` +
        (this.stack.length ? ' · dupla kattintás: vissza az előző nézetre' : ' · egérrel kijelölve nagyítható');
    } catch (e) {
      if (!silent) toast('Grafikon adatok betöltése sikertelen: ' + e.message, 'err');
    } finally {
      this.loading = false;
      this._schedule();
    }
  }

  _render(d) {
    const base = { start: d.start, end: d.end, bucket: d.bucket };
    const sys = d.sys;
    const keep = (chart, key) => { const s = chart.series.find((x) => x.key === key); return s ? s.hidden : false; };
    const C = this.charts;

    C.temps.setData(Object.assign({}, base, {
      series: ['t1', 't2', 't3', 't4'].map((k, i) => ({
        key: k, label: ['T1 minerek előtt', 'T2 minerek után', 'T3 radiátor előtt', 'T4 radiátor után'][i],
        color: SYS_COLORS[k], t: sys.t, v: sys[k], hidden: keep(C.temps, k),
      })),
    }));
    const diff = (a, b) => sys.t.map((_, i) => (sys[a][i] === null || sys[b][i] === null ? null : sys[a][i] - sys[b][i]));
    C.dts.setData(Object.assign({}, base, {
      series: [
        { key: 'dtm', label: 'ΔT minerek (T2−T1)', color: '#fb923c', t: sys.t, v: diff('t2', 't1'), hidden: keep(C.dts, 'dtm') },
        { key: 'dtr', label: 'ΔT radiátor (T3−T4)', color: '#22d3ee', t: sys.t, v: diff('t3', 't4'), hidden: keep(C.dts, 'dtr') },
      ],
    }));
    C.states.setData(Object.assign({}, base, {
      series: [
        { key: 'run', label: 'Üzemel', color: '#22c55e', t: sys.t, v: sys.run },
        { key: 'emerg', label: 'Vészleállás', color: '#ef4444', t: sys.t, v: sys.emerg },
        { key: 'pump1', label: 'P1 szivattyú', color: '#38bdf8', t: sys.t, v: sys.pump1 },
        { key: 'flow1', label: 'F1 áramlás', color: '#0ea5e9', t: sys.t, v: sys.flow1 },
        { key: 'pump2', label: 'P2 szivattyú', color: '#a78bfa', t: sys.t, v: sys.pump2 },
        { key: 'flow2', label: 'F2 áramlás', color: '#8b5cf6', t: sys.t, v: sys.flow2 },
      ],
    }));

    // hashrate: automatikus mértékegység
    let maxHs = 0;
    for (const v of sys.hashrate) if (v > maxHs) maxHs = v;
    const units = [[1e15, 'PH/s'], [1e12, 'TH/s'], [1e9, 'GH/s'], [1e6, 'MH/s'], [1, 'H/s']];
    const [div, unit] = units.find(([dv]) => maxHs >= dv) || [1e12, 'TH/s'];
    const scale = (arr) => arr.map((v) => (v === null ? null : v / div));
    const hashSeries = [{ key: 'total', label: 'Összesen', color: '#e2e8f0', width: 2.4, t: sys.t, v: scale(sys.hashrate), hidden: keep(C.hash, 'total'),
      fmt: (v) => num(v, 2) + ' ' + unit }];
    d.miners.forEach((m, i) => hashSeries.push({ key: 'h-' + m.id, label: m.name, color: MINER_COLORS[i % MINER_COLORS.length], t: m.t,
      v: scale(m.hashrate), hidden: keep(C.hash, 'h-' + m.id), fmt: (v) => num(v, 2) + ' ' + unit }));
    C.hash.card.querySelector('.card-title').textContent = 'Hashrate (' + unit + ')';
    C.hash.setData(Object.assign({}, base, { series: hashSeries }));

    C.mtemp.setData(Object.assign({}, base, {
      series: d.miners.map((m, i) => ({ key: 'c-' + m.id, label: m.name, color: MINER_COLORS[i % MINER_COLORS.length], t: m.t,
        v: m.temp_chip, hidden: keep(C.mtemp, 'c-' + m.id) })),
    }));

    const hasPower = sys.power.some((v) => v !== null);
    C.power.card.hidden = !hasPower;
    if (hasPower) {
      const pSeries = [{ key: 'total', label: 'Összesen', color: '#e2e8f0', width: 2.4, t: sys.t, v: sys.power.map((v) => (v === null ? null : v / 1000)),
        fmt: (v) => fmtPower(v * 1000) }];
      d.miners.forEach((m, i) => pSeries.push({ key: 'p-' + m.id, label: m.name, color: MINER_COLORS[i % MINER_COLORS.length], t: m.t,
        v: m.power.map((v) => (v === null ? null : v / 1000)), fmt: (v) => fmtPower(v * 1000), hidden: keep(C.power, 'p-' + m.id) }));
      C.power.card.querySelector('.card-title').textContent = 'Teljesítményfelvétel (kW)';
      C.power.setData(Object.assign({}, base, { series: pSeries }));
    }
    for (const c of Object.values(C)) this._legend(c);
  }
}
