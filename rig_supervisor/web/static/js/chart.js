// Könnyű, függőség nélküli idősoros grafikon (canvas): vonal- és idősáv (timeline) mód,
// összehangolt célkereszt több grafikon között, egérrel kijelölhető nagyítás, érintés támogatás.
import { num } from './util.js';

const TIME_STEPS = [60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800, 345600, 604800, 1209600, 2592000, 7776000];
const tzOffset = () => -new Date().getTimezoneOffset() * 60;
const pad2 = (n) => (n < 10 ? '0' : '') + n;

export class HoverBus {
  constructor() { this.subs = new Set(); this.t = null; this.src = null; }
  sub(fn) { this.subs.add(fn); return () => this.subs.delete(fn); }
  set(t, src) { this.t = t; this.src = src; for (const fn of this.subs) fn(t, src); }
}

function niceStep(range, count) {
  const raw = range / Math.max(1, count);
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / mag;
  return (norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10) * mag;
}

function lowerBound(arr, x) {
  let lo = 0, hi = arr.length;
  while (lo < hi) { const mid = (lo + hi) >> 1; if (arr[mid] < x) lo = mid + 1; else hi = mid; }
  return lo;
}

function timeLabel(t, step, first) {
  const d = new Date(t * 1000);
  const md = pad2(d.getMonth() + 1) + '.' + pad2(d.getDate()) + '.';
  if (step >= 86400) return md;
  const hm = pad2(d.getHours()) + ':' + pad2(d.getMinutes());
  if (first || (d.getHours() === 0 && d.getMinutes() === 0)) return [hm, md];
  return hm;
}

export class TimeChart {
  constructor(container, opts = {}) {
    this.o = Object.assign({ height: 230, mode: 'line', digits: 1, unit: '', fmt: null, yMin: null, yMax: null, zeroBased: false, emptyText: 'Nincs adat a kiválasztott időszakban' }, opts);
    this.root = container;
    this.root.classList.add('chart-box');
    this.canvas = document.createElement('canvas');
    this.tip = document.createElement('div');
    this.tip.className = 'chart-tip';
    this.tip.hidden = true;
    this.empty = document.createElement('div');
    this.empty.className = 'chart-empty';
    this.empty.hidden = true;
    this.root.append(this.canvas, this.tip, this.empty);
    this.ctx = this.canvas.getContext('2d');
    this.series = [];
    this.start = 0;
    this.end = 1;
    this.bucket = 60;
    this.hoverT = null;
    this.drag = null;
    this.bus = this.o.bus || null;
    if (this.bus) this.unsub = this.bus.sub((t, src) => { if (src !== this) { this.hoverT = t; this.localHover = false; this.draw(); } });
    this._bind();
    if (window.ResizeObserver) {
      this.ro = new ResizeObserver(() => this.draw());
      this.ro.observe(this.root);
    }
  }

  destroy() {
    if (this.ro) this.ro.disconnect();
    if (this.unsub) this.unsub();
  }

  setData({ start, end, bucket, series }) {
    this.start = start;
    this.end = end;
    this.bucket = bucket || 60;
    this.series = series;
    this.draw();
  }

  setHidden(key, hidden) {
    const s = this.series.find((x) => x.key === key);
    if (s) { s.hidden = hidden; this.draw(); }
  }

  _bind() {
    const c = this.canvas;
    const pos = (e) => {
      const r = c.getBoundingClientRect();
      return { x: e.clientX - r.left, y: e.clientY - r.top };
    };
    c.addEventListener('pointermove', (e) => {
      const p = pos(e);
      if (this.drag) { this.drag.x1 = p.x; }
      this._hoverAt(p.x, p.y);
    });
    c.addEventListener('pointerleave', () => { if (!this.drag) this._clearHover(); });
    c.addEventListener('pointercancel', () => { this.drag = null; this._clearHover(); });
    c.addEventListener('pointerdown', (e) => {
      const p = pos(e);
      if (e.pointerType === 'mouse' && e.button === 0 && this.o.onZoom && this.plot && p.x >= this.plot.x0 && p.x <= this.plot.x1) {
        this.drag = { x0: p.x, x1: p.x };
        try { c.setPointerCapture(e.pointerId); } catch (err) { /* ignore */ }
      }
      this._hoverAt(p.x, p.y);
    });
    c.addEventListener('pointerup', () => {
      const d = this.drag;
      this.drag = null;
      if (d && Math.abs(d.x1 - d.x0) > 8 && this.plot) {
        const a = this._xToT(Math.min(d.x0, d.x1));
        const b = this._xToT(Math.max(d.x0, d.x1));
        this.o.onZoom(Math.floor(a), Math.ceil(b));
      }
      this.draw();
    });
    c.addEventListener('dblclick', () => { if (this.o.onZoomReset) this.o.onZoomReset(); });
  }

  _xToT(x) {
    const p = this.plot;
    return this.start + ((x - p.x0) / (p.x1 - p.x0)) * (this.end - this.start);
  }

  _tToX(t) {
    const p = this.plot;
    return p.x0 + ((t - this.start) / (this.end - this.start)) * (p.x1 - p.x0);
  }

  _hoverAt(x, y) {
    if (!this.plot) return;
    const p = this.plot;
    if (x < p.x0 || x > p.x1) { this._clearHover(); return; }
    this.hoverT = this._xToT(x);
    this.localHover = true;
    this.hoverY = y;
    if (this.bus) this.bus.set(this.hoverT, this);
    this.draw();
  }

  _clearHover() {
    this.hoverT = null;
    this.localHover = false;
    this.tip.hidden = true;
    if (this.bus) this.bus.set(null, this);
    this.draw();
  }

  _fmt(s, v) {
    if (v === null || v === undefined) return '—';
    if (s.fmt) return s.fmt(v);
    if (this.o.fmt) return this.o.fmt(v);
    return num(v, this.o.digits) + (this.o.unit ? ' ' + this.o.unit : '');
  }

  _valueAt(s, t) {
    if (!s.t.length) return null;
    const i = lowerBound(s.t, t);
    let best = -1, bd = Infinity;
    for (const j of [i - 1, i]) {
      if (j >= 0 && j < s.t.length) {
        const d = Math.abs(s.t[j] - t);
        if (d < bd) { bd = d; best = j; }
      }
    }
    if (best < 0 || bd > this.bucket * 1.5) return null;
    return { t: s.t[best], v: s.v[best] };
  }

  draw() {
    if (this._raf) return;
    this._raf = requestAnimationFrame(() => { this._raf = null; this._draw(); });
  }

  _draw() {
    const W = this.root.clientWidth;
    if (!W) return;
    const timeline = this.o.mode === 'timeline';
    const H = timeline ? Math.max(60, this.series.length * 26 + 34) : this.o.height;
    const dpr = window.devicePixelRatio || 1;
    if (this.canvas.width !== Math.round(W * dpr) || this.canvas.height !== Math.round(H * dpr)) {
      this.canvas.width = Math.round(W * dpr);
      this.canvas.height = Math.round(H * dpr);
      this.canvas.style.height = H + 'px';
    }
    const ctx = this.ctx;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    ctx.font = '11px system-ui, -apple-system, Segoe UI, Roboto, sans-serif';
    const css = getComputedStyle(document.documentElement);
    const cGrid = 'rgba(148,163,184,0.10)';
    const cAxis = css.getPropertyValue('--dim').trim() || '#66778a';
    const visible = this.series.filter((s) => !s.hidden);
    const hasData = this.series.some((s) => s.v.some((v) => v !== null && v !== undefined));
    this.empty.hidden = hasData;
    this.empty.textContent = this.o.emptyText;

    // ---- Y tartomány
    let yTicks = [], ymin = 0, ymax = 1, labelW = 0;
    if (!timeline) {
      let lo = Infinity, hi = -Infinity;
      for (const s of visible) for (const v of s.v) if (v !== null && v !== undefined) { if (v < lo) lo = v; if (v > hi) hi = v; }
      if (lo === Infinity) { lo = 0; hi = 1; }
      if (this.o.zeroBased) lo = Math.min(0, lo);
      if (this.o.yMin !== null) lo = Math.min(lo, this.o.yMin);
      if (this.o.yMax !== null) hi = Math.max(hi, this.o.yMax);
      if (hi - lo < 1e-9) { hi += 1; lo -= 1; }
      const padY = (hi - lo) * 0.08;
      lo -= this.o.zeroBased && lo >= 0 ? 0 : padY;
      hi += padY;
      const step = niceStep(hi - lo, Math.max(2, Math.floor(H / 48)));
      ymin = Math.floor(lo / step) * step;
      ymax = Math.ceil(hi / step) * step;
      const dig = step < 1 ? (step < 0.1 ? 2 : 1) : 0;
      for (let v = ymin; v <= ymax + step / 2; v += step) yTicks.push([v, num(v, dig)]);
      for (const [, l] of yTicks) labelW = Math.max(labelW, ctx.measureText(l).width);
    } else {
      for (const s of this.series) labelW = Math.max(labelW, ctx.measureText(s.label).width);
    }
    const x0 = Math.ceil(labelW) + 14;
    const x1 = W - 10;
    const y0 = 8;
    const y1 = H - 24;
    this.plot = { x0, x1, y0, y1 };
    const yToPx = (v) => y1 - ((v - ymin) / (ymax - ymin)) * (y1 - y0);

    // ---- idő tengely
    const span = this.end - this.start;
    const maxTicks = Math.max(2, Math.floor((x1 - x0) / 92));
    let step = TIME_STEPS[TIME_STEPS.length - 1];
    for (const st of TIME_STEPS) if (span / st <= maxTicks) { step = st; break; }
    const tz = tzOffset();
    let tt = Math.ceil((this.start + tz) / step) * step - tz;
    if (step >= 86400 && step < 2592000) {
      const d = new Date(this.start * 1000);
      d.setHours(0, 0, 0, 0);
      tt = d.getTime() / 1000;
      while (tt < this.start) tt += 86400;
    }
    ctx.strokeStyle = cGrid;
    ctx.lineWidth = 1;
    ctx.fillStyle = cAxis;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    let first = true;
    for (; tt <= this.end; tt += step) {
      const x = Math.round(this._tToX(tt)) + 0.5;
      ctx.beginPath(); ctx.moveTo(x, y0); ctx.lineTo(x, y1); ctx.stroke();
      const lbl = timeLabel(tt, step, first);
      first = false;
      if (Array.isArray(lbl)) ctx.fillText(lbl[0] + ' ' + lbl[1], x, y1 + 6);
      else ctx.fillText(lbl, x, y1 + 6);
    }

    if (!timeline) {
      // ---- Y rács
      ctx.textAlign = 'right';
      ctx.textBaseline = 'middle';
      for (const [v, l] of yTicks) {
        const y = Math.round(yToPx(v)) + 0.5;
        ctx.strokeStyle = cGrid;
        ctx.beginPath(); ctx.moveTo(x0, y); ctx.lineTo(x1, y); ctx.stroke();
        ctx.fillStyle = cAxis;
        ctx.fillText(l, x0 - 6, y);
      }
      // ---- sorozatok
      ctx.save();
      ctx.beginPath(); ctx.rect(x0, y0 - 2, x1 - x0, y1 - y0 + 4); ctx.clip();
      const gapLimit = this.bucket * 2.6;
      for (const s of visible) {
        ctx.strokeStyle = s.color;
        ctx.lineWidth = s.width || 1.8;
        ctx.lineJoin = 'round';
        ctx.setLineDash(s.dash || []);
        ctx.beginPath();
        let pen = false, prevT = null, segPts = 0, segStart = null;
        const single = [];
        for (let i = 0; i < s.t.length; i++) {
          const v = s.v[i];
          if (v === null || v === undefined) { pen = false; continue; }
          const x = this._tToX(s.t[i]);
          const y = yToPx(v);
          if (pen && prevT !== null && s.t[i] - prevT > gapLimit) pen = false;
          if (!pen) {
            if (segPts === 1) single.push(segStart); // az előző szakasz egyetlen pontból állt
            ctx.moveTo(x, y);
            segStart = [x, y];
            segPts = 1;
          } else {
            ctx.lineTo(x, y);
            segPts++;
          }
          pen = true;
          prevT = s.t[i];
        }
        if (segPts === 1) single.push(segStart);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = s.color;
        for (const [x, y] of single) { ctx.beginPath(); ctx.arc(x, y, 1.8, 0, Math.PI * 2); ctx.fill(); }
      }
      ctx.restore();
    } else {
      // ---- idősávok
      const rowH = (y1 - y0) / Math.max(1, this.series.length);
      ctx.textAlign = 'right';
      ctx.textBaseline = 'middle';
      this.series.forEach((s, r) => {
        const ry = y0 + r * rowH;
        ctx.fillStyle = 'rgba(148,163,184,0.06)';
        ctx.fillRect(x0, ry + 3, x1 - x0, rowH - 6);
        ctx.fillStyle = cAxis;
        ctx.fillText(s.label, x0 - 6, ry + rowH / 2);
        if (s.hidden) return;
        // azonos értékű, egymást követő pontok összevonása egyetlen sávvá (nincs csíkozódás)
        let i = 0;
        while (i < s.t.length) {
          const v = s.v[i];
          if (!v) { i++; continue; }
          let j = i;
          while (j + 1 < s.t.length && s.v[j + 1] && Math.abs(s.v[j + 1] - v) < 0.02 && s.t[j + 1] - s.t[j] <= this.bucket * 2.6) j++;
          const tEnd = j + 1 < s.t.length && s.t[j + 1] - s.t[j] <= this.bucket * 2.6 ? s.t[j + 1] : s.t[j] + this.bucket;
          const xa = Math.max(x0, this._tToX(s.t[i]));
          const xb = Math.min(x1, this._tToX(tEnd));
          if (xb > xa) {
            ctx.globalAlpha = 0.25 + 0.75 * Math.min(1, Math.max(0, v));
            ctx.fillStyle = s.color;
            ctx.fillRect(Math.floor(xa), ry + 4, Math.max(1, Math.ceil(xb) - Math.floor(xa)), rowH - 8);
          }
          i = j + 1;
        }
        ctx.globalAlpha = 1;
      });
    }

    // ---- kijelölés (nagyítás)
    if (this.drag) {
      const a = Math.min(this.drag.x0, this.drag.x1), b = Math.max(this.drag.x0, this.drag.x1);
      ctx.fillStyle = 'rgba(56,189,248,0.15)';
      ctx.fillRect(a, y0, b - a, y1 - y0);
      ctx.strokeStyle = 'rgba(56,189,248,0.6)';
      ctx.strokeRect(a + 0.5, y0 + 0.5, b - a, y1 - y0);
    }

    // ---- célkereszt + tooltip
    if (this.hoverT !== null && this.hoverT >= this.start && this.hoverT <= this.end && hasData) {
      const x = Math.round(this._tToX(this.hoverT)) + 0.5;
      ctx.strokeStyle = 'rgba(226,232,240,0.45)';
      ctx.setLineDash([3, 3]);
      ctx.beginPath(); ctx.moveTo(x, y0); ctx.lineTo(x, y1); ctx.stroke();
      ctx.setLineDash([]);
      const rows = [];
      let tShow = null;
      for (const s of (timeline ? this.series : visible)) {
        const hit = this._valueAt(s, this.hoverT);
        if (!hit) { rows.push([s, null]); continue; }
        tShow = tShow === null ? hit.t : tShow;
        rows.push([s, hit.v]);
        if (!timeline && hit.v !== null && hit.v !== undefined) {
          ctx.fillStyle = s.color;
          ctx.beginPath(); ctx.arc(this._tToX(hit.t), yToPx(hit.v), 3.4, 0, Math.PI * 2); ctx.fill();
        }
      }
      if (this.localHover) {
        const d = new Date((tShow || this.hoverT) * 1000);
        const head = `${d.getFullYear()}.${pad2(d.getMonth() + 1)}.${pad2(d.getDate())}. ${pad2(d.getHours())}:${pad2(d.getMinutes())}${this.bucket < 60 ? ':' + pad2(d.getSeconds()) : ''}`;
        this.tip.innerHTML = `<div class="tt">${head}</div>` + rows.map(([s, v]) =>
          `<div class="tr"><i style="background:${s.color}"></i>${s.label}<b>${timeline ? (v === null ? '—' : v >= 0.995 ? 'BE' : v <= 0.005 ? 'KI' : num(v * 100, 0) + '% BE') : this._fmt(s, v)}</b></div>`).join('');
        this.tip.hidden = false;
        const tw = this.tip.offsetWidth, th = this.tip.offsetHeight;
        let left = x + 14;
        if (left + tw > W - 4) left = x - tw - 14;
        let top = Math.min(Math.max(4, (this.hoverY || y0) - th / 2), H - th - 4);
        this.tip.style.left = Math.max(4, left) + 'px';
        this.tip.style.top = top + 'px';
      } else {
        this.tip.hidden = true;
      }
    } else {
      this.tip.hidden = true;
    }
  }
}
