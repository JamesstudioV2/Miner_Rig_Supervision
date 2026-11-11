// A hűtőrendszer sematikus SVG ábrája: minerek (dinamikusan), belső kör, hőcserélő, külső kör, radiátor.
// Két elrendezés: széles (asztali gép) és álló (telefon). A szerkezet csak akkor épül újra,
// ha a minerek listája vagy az elrendezés változik – egyébként csak az értékek frissülnek.
import { tempColor, num, hashrate, esc, STATUS_LABELS } from './util.js';

const WIDE_MIN_PX = 640;
const FLOW_TEXT = { ok: 'ÁRAMLIK', waiting: 'INDUL…', fault: 'NINCS ÁRAMLÁS!', noflow: 'NINCS ÁRAMLÁS', idle: 'ÁLL', disabled: 'NEM FIGYELT' };

const P = (pts) => pts.map((p) => p[0].toFixed(1) + ',' + p[1].toFixed(1)).join(' ');

function pipe(id, pts, temp, loop, grad) {
  const stroke = grad ? `url(#${grad})` : '#5b6b7e';
  return `<g class="pipe" data-pipe="${id}" data-temp="${temp}" data-loop="${loop}">
    <polyline class="pipe-casing" points="${P(pts)}" stroke-width="15"/>
    <polyline class="pipe-water" points="${P(pts)}" stroke-width="9.5" stroke="${stroke}"/>
    <polyline class="pipe-flow" points="${P(pts)}" stroke-width="3.4"/></g>`;
}

function pumpSvg(id, name, x, y, label = 'below', rot = 0) {
  const blades = [0, 90, 180, 270].map((a) => `<path transform="rotate(${a})" d="M0,-3 C6,-6 9,-12 4,-17.5 C0,-13 -2.5,-8 0,-3 Z"/>`).join('');
  const lx = label === 'right' ? 32 : label === 'left' ? -32 : 0;
  const ly = label === 'below' ? 39 : label === 'above' ? -42 : -2;
  const anchor = label === 'right' ? 'start' : label === 'left' ? 'end' : 'middle';
  return `<g class="pump" data-pump="${id}" transform="translate(${x},${y})">
    <circle class="glow" r="24"/>
    <circle class="pump-body" r="22"/>
    <path d="M-22,0 L-30,-6 M-22,0 L-30,6" transform="rotate(${rot})" stroke="#0a0f15" stroke-width="0"/>
    <g class="impeller">${blades}</g><circle class="hub" r="3.5"/>
    <text class="pump-name" x="${lx}" y="${ly}" text-anchor="${anchor}">${name}</text>
    <text class="pump-state" x="${lx}" y="${ly + 13}" text-anchor="${anchor}">KI</text></g>`;
}

function flowSvg(id, name, x, y, label = 'below', rot = 0) {
  const lx = label === 'right' ? 18 : label === 'left' ? -18 : 0;
  const ly = label === 'below' ? 29 : label === 'above' ? -30 : -1;
  const anchor = label === 'right' ? 'start' : label === 'left' ? 'end' : 'middle';
  return `<g class="fsw" data-flow="${id}" transform="translate(${x},${y})">
    <circle r="11"/><path transform="rotate(${rot})" d="M-6,0 L5,0 M1,-4.5 L5.5,0 L1,4.5"/>
    <text class="fsw-name" x="${lx}" y="${ly}" text-anchor="${anchor}">${name}</text>
    <text class="fsw-state" x="${lx}" y="${ly + 12}" text-anchor="${anchor}">—</text></g>`;
}

function sensorSvg(id, label, px, py, bx, by, bw = 112) {
  const bh = 46;
  // vezető vonal a szonda és a doboz legközelebbi pontja között
  const cx = Math.max(bx, Math.min(px, bx + bw));
  const cy = Math.max(by, Math.min(py, by + bh));
  return `<g class="tsens" data-sensor="${id}">
    <line class="lead" x1="${px}" y1="${py}" x2="${cx}" y2="${cy}"/>
    <rect class="box" x="${bx}" y="${by}" width="${bw}" height="${bh}" rx="9"/>
    <text class="t-name" x="${bx + 10}" y="${by + 16}">${esc(label)}</text>
    <text class="t-val" x="${bx + 10}" y="${by + 37}">—</text>
    <circle class="probe" cx="${px}" cy="${py}" r="5.5"/></g>`;
}

function dtChip(id, x, y, w = 132, prefix = null) {
  const pre = prefix !== null ? prefix : (id === 'dt_miners' ? 'ΔT minerek: ' : 'ΔT radiátor: ');
  return `<g class="dt-chip" data-dt="${id}" data-prefix="${pre}" transform="translate(${x},${y})"><rect width="${w}" height="22" rx="11"/>
    <text x="${w / 2}" y="15" text-anchor="middle">—</text></g>`;
}

function hxSvg(x, y, w, h, orient) {
  // a belső kör oldala (meleg -> hideg) és a külső kör oldala (hideg -> meleg)
  let inner, outer, gi, go, plates = '';
  if (orient === 'v') {
    inner = `<rect x="${x}" y="${y}" width="${w / 2}" height="${h}" fill="url(#g-hx-in)"/>`;
    outer = `<rect x="${x + w / 2}" y="${y}" width="${w / 2}" height="${h}" fill="url(#g-hx-out)"/>`;
    gi = `<linearGradient id="g-hx-in" gradientUnits="userSpaceOnUse" x1="0" y1="${y}" x2="0" y2="${y + h}"><stop offset="0"/><stop offset="1"/></linearGradient>`;
    go = `<linearGradient id="g-hx-out" gradientUnits="userSpaceOnUse" x1="0" y1="${y + h}" x2="0" y2="${y}"><stop offset="0"/><stop offset="1"/></linearGradient>`;
    for (let i = 1; i < 7; i++) {
      const yy = y + (h * i) / 7;
      plates += `<path class="hx-plate" d="M${x + 5},${yy} L${x + w / 4},${yy - 5} L${x + w / 2},${yy} L${x + (3 * w) / 4},${yy - 5} L${x + w - 5},${yy}"/>`;
    }
  } else {
    inner = `<rect x="${x}" y="${y}" width="${w}" height="${h / 2}" fill="url(#g-hx-in)"/>`;
    outer = `<rect x="${x}" y="${y + h / 2}" width="${w}" height="${h / 2}" fill="url(#g-hx-out)"/>`;
    gi = `<linearGradient id="g-hx-in" gradientUnits="userSpaceOnUse" x1="${x + w}" y1="0" x2="${x}" y2="0"><stop offset="0"/><stop offset="1"/></linearGradient>`;
    go = `<linearGradient id="g-hx-out" gradientUnits="userSpaceOnUse" x1="${x}" y1="0" x2="${x + w}" y2="0"><stop offset="0"/><stop offset="1"/></linearGradient>`;
    for (let i = 1; i < 8; i++) {
      const xx = x + (w * i) / 8;
      plates += `<path class="hx-plate" d="M${xx},${y + 5} L${xx - 5},${y + h / 4} L${xx},${y + h / 2} L${xx - 5},${y + (3 * h) / 4} L${xx},${y + h - 5}"/>`;
    }
  }
  return {
    defs: gi + go,
    body: `<g class="hx">${inner}${outer}${plates}<rect class="hx-frame" x="${x}" y="${y}" width="${w}" height="${h}" rx="7" fill="none"/></g>`,
  };
}

function radSvg(x, y, w, h, orient) {
  let fins = '', waves = '', grad;
  if (orient === 'v') {
    grad = `<linearGradient id="g-rad" gradientUnits="userSpaceOnUse" x1="0" y1="${y}" x2="0" y2="${y + h}"><stop offset="0"/><stop offset="1"/></linearGradient>`;
    for (let yy = y + 9; yy < y + h - 4; yy += 8) fins += `<line class="rad-fin" x1="${x + 3}" y1="${yy}" x2="${x + w - 3}" y2="${yy}"/>`;
    const wx = x + w + 12;
    for (let i = 0; i < 3; i++) {
      const wy = y + h * (0.25 + i * 0.25);
      waves += `<path class="heat-wave" d="M${wx},${wy - 12} q6,6 0,12 q-6,6 0,12"/>`;
    }
  } else {
    grad = `<linearGradient id="g-rad" gradientUnits="userSpaceOnUse" x1="${x + w}" y1="0" x2="${x}" y2="0"><stop offset="0"/><stop offset="1"/></linearGradient>`;
    for (let xx = x + 9; xx < x + w - 4; xx += 8) fins += `<line class="rad-fin" x1="${xx}" y1="${y + 3}" x2="${xx}" y2="${y + h - 3}"/>`;
    const wy = y + h + 10;
    for (let i = 0; i < 3; i++) {
      const wx = x + w * (0.25 + i * 0.25);
      waves += `<path class="heat-wave" style="transform-box:fill-box" d="M${wx - 12},${wy} q6,6 12,0 q6,-6 12,0"/>`;
    }
  }
  return {
    defs: grad,
    body: `<g class="rad" data-orient="${orient}"><rect x="${x}" y="${y}" width="${w}" height="${h}" rx="6" fill="url(#g-rad)"/>${fins}
      <rect class="rad-frame" x="${x}" y="${y}" width="${w}" height="${h}" rx="6" fill="none"/><g class="waves">${waves}</g></g>`,
  };
}

function minerSvg(m, x, y, w, h) {
  const maxChars = Math.max(6, Math.floor((w - 34) / 7.3));
  let name = m.name || m.host;
  if (name.length > maxChars) name = name.slice(0, maxChars - 1) + '…';
  return `<g class="mbox" data-miner="${esc(m.id)}" transform="translate(${x},${y})" tabindex="0" role="button" aria-label="${esc(m.name)} részletei">
    <title>${esc(m.name)} – ${esc(m.host)}</title>
    <rect class="m-frame" width="${w}" height="${h}" rx="10"/>
    <rect class="m-strip" x="1" y="1" width="${w - 2}" height="4.5" rx="2.2"/>
    <text class="m-name" x="10" y="23">${esc(name)}</text>
    <g transform="translate(${w - 15},18)"><circle class="m-fan" r="6" stroke-dasharray="9 4"/></g>
    <text class="m-hash" x="10" y="${h * 0.5 + 5}">—</text>
    <text class="m-temp" x="10" y="${h * 0.5 + 23}">—</text>
    <text class="m-state" x="10" y="${h - 8}">—</text></g>`;
}

// ------------------------------------------------------------------ elrendezések
function minerRows(miners, x0, x1, top, opts) {
  const { minW, maxW, H, gap, pitch, maxCols } = opts;
  const n = miners.length;
  const avail = x1 - x0;
  let cols = Math.max(1, Math.min(n || 1, maxCols || 99, Math.floor((avail + gap) / (minW + gap))));
  const rows = Math.max(1, Math.ceil(n / cols));
  if (n && rows > 1) cols = Math.ceil(n / rows); // egyenletes elosztás
  const bw = Math.min(maxW, (avail - (cols - 1) * gap) / cols);
  const out = [];
  for (let r = 0; r < rows; r++) {
    const items = miners.slice(r * cols, (r + 1) * cols);
    const rowW = items.length * bw + (items.length - 1) * gap;
    const sx = x0 + (avail - rowW) / 2;
    const yh = top + r * pitch;
    const by = yh + 17;
    const yc = by + H + 17;
    out.push({ yh, yc, by, boxes: items.map((m, i) => ({ m, x: sx + i * (bw + gap), y: by, w: bw, h: H })) });
  }
  return out;
}

function innerManifolds(rows, L, R, add) {
  rows.forEach((row, r) => {
    if (!row.boxes.length) return;
    const first = row.boxes[0];
    const last = row.boxes[row.boxes.length - 1];
    add.pipe(pipe('cold' + r, [[L, row.yc], [last.x + last.w * 0.3, row.yc]], 't1', 1));
    add.pipe(pipe('hot' + r, [[first.x + first.w * 0.7, row.yh], [R, row.yh]], 't2', 1));
    row.boxes.forEach((b, i) => {
      add.pipe(pipe(`in${r}_${i}`, [[b.x + b.w * 0.3, row.yc], [b.x + b.w * 0.3, b.y + b.h]], 't1', 1));
      add.pipe(pipe(`out${r}_${i}`, [[b.x + b.w * 0.7, b.y], [b.x + b.w * 0.7, row.yh]], 't2', 1));
      add.miner(minerSvg(b.m, b.x, b.y, b.w, b.h));
    });
  });
}

function emptyMinersNote(x, y, w, h) {
  return `<g class="mbox-empty"><rect x="${x}" y="${y}" width="${w}" height="${h}" rx="10"/>
    <text x="${x + w / 2}" y="${y + h / 2 + 5}" text-anchor="middle">Nincs miner beállítva – Beállítások › Minerek</text></g>`;
}

function layoutWide(miners, S) {
  const W = 1000, L = 60, R = 680, top = 34;
  const opts = { minW: 116, maxW: 156, H: 88, gap: 14, pitch: 88 + 64 };
  const rows = minerRows(miners, L + 30, R - 30, top, opts);
  const parts = { defs: [], pipes: [], miners: [], items: [] };
  const add = { pipe: (s) => parts.pipes.push(s), miner: (s) => parts.miners.push(s) };
  if (miners.length) innerManifolds(rows, L, R, add);
  else parts.miners.push(emptyMinersNote(L + 30, rows[0].by, R - L - 60, opts.H));
  const yLast = rows[rows.length - 1].yc;
  const yB = yLast + 178;
  const hx = { x: R - 16, y: yLast + 54, w: 64, h: 84 };
  const hxB = hx.y + hx.h;
  // belső kör: hőcserélő kimenet -> alsó cső -> bal felszálló -> hideg osztók
  parts.pipes.push(pipe('ret', [[R, hxB], [R, yB], [L, yB], [L, rows[0].yc]], 't1', 1));
  // jobb leszálló: meleg gyűjtők -> hőcserélő
  parts.pipes.push(pipe('riser', [[R, rows[0].yh], [R, hx.y]], 't2', 1));
  // külső kör
  const xo = R + 32;
  const radCX = 900, radW = 58;
  const yA = top - 6;
  const radTop = yA + 76;
  const radBot = yB - 84;
  parts.pipes.push(pipe('ohot', [[xo, hx.y], [xo, yA], [radCX, yA], [radCX, radTop]], 't3', 2));
  parts.pipes.push(pipe('ocold', [[radCX, radBot], [radCX, yB], [xo, yB], [xo, hxB]], 't4', 2));
  const hxs = hxSvg(hx.x, hx.y, hx.w, hx.h, 'v');
  const rad = radSvg(radCX - radW / 2, radTop, radW, radBot - radTop, 'v');
  parts.defs.push(hxs.defs, rad.defs);
  const it = parts.items;
  it.push(hxs.body, rad.body);
  it.push(`<text class="dg-label" x="${hx.x - 10}" y="${hx.y + hx.h / 2 + 4}" text-anchor="end">Hőcserélő</text>`);
  it.push(`<text class="dg-label" x="${W - 12}" y="${radTop - 12}" text-anchor="end">Radiátor</text>`);
  it.push(`<text class="dg-label" x="${L - 2}" y="${yB + 50}">Belső kör</text>`);
  it.push(`<text class="dg-label" x="${W - 14}" y="${yB + 50}" text-anchor="end">Külső kör</text>`);
  it.push(pumpSvg('pump1', 'P1', L + 200, yB, 'below', 180));
  it.push(flowSvg('flow1', 'F1', L + 345, yB, 'below', 180));
  it.push(pumpSvg('pump2', 'P2', xo + 52, yB, 'below', 180));
  it.push(flowSvg('flow2', 'F2', xo + 128, yB, 'below', 180));
  it.push(sensorSvg('t1', 'T1 · ' + S.t1, L, yLast + 64, L + 16, yLast + 41));
  it.push(sensorSvg('t2', 'T2 · ' + S.t2, R, yLast + 30, R - 128, yLast + 9));
  it.push(sensorSvg('t3', 'T3 · ' + S.t3, radCX - 64, yA, radCX - 176, yA + 16));
  it.push(sensorSvg('t4', 'T4 · ' + S.t4, radCX, radBot + 40, radCX - 128, radBot + 17));
  it.push(dtChip('dt_miners', L + 16, yLast + 98, 150));
  it.push(dtChip('dt_radiator', radCX - 172, (radTop + radBot) / 2 - 11, 132));
  return { W, H: yB + 62, parts };
}

function layoutTall(miners, S) {
  const W = 420, L = 24, R = 396, top = 30;
  const opts = { minW: 140, maxW: 176, H: 84, gap: 12, pitch: 84 + 62, maxCols: 2 };
  const rows = minerRows(miners, L + 22, R - 22, top, opts);
  const parts = { defs: [], pipes: [], miners: [], items: [] };
  const add = { pipe: (s) => parts.pipes.push(s), miner: (s) => parts.miners.push(s) };
  if (miners.length) innerManifolds(rows, L, R, add);
  else parts.miners.push(emptyMinersNote(L + 22, rows[0].by, R - L - 44, opts.H));
  const yLast = rows[rows.length - 1].yc;
  const yB1 = yLast + 132;
  const hx = { x: 232, y: yB1 - 18, w: 132, h: 70 };
  const hxB = hx.y + hx.h;
  const yIn = yB1;                 // belső oldal középvonala (felső fél)
  // belső kör
  parts.pipes.push(pipe('riser', [[R, rows[0].yh], [R, yIn], [hx.x + hx.w, yIn]], 't2', 1));
  parts.pipes.push(pipe('ret', [[hx.x, yIn], [L, yIn], [L, rows[0].yc]], 't1', 1));
  // külső kör (a hőcserélő alól indul)
  const xCold = hx.x + 26, xHot = hx.x + hx.w - 26;
  const yA = hxB + 30;
  const L2 = 40, R2 = 380;
  const radTop = yA + 214, radH = 56, radMid = radTop + radH / 2;
  parts.pipes.push(pipe('ohot', [[xHot, hxB], [xHot, yA], [R2, yA], [R2, radMid], [R2 - 40, radMid]], 't3', 2));
  parts.pipes.push(pipe('ocold', [[L2 + 40, radMid], [L2, radMid], [L2, yA], [xCold, yA], [xCold, hxB]], 't4', 2));
  const hxs = hxSvg(hx.x, hx.y, hx.w, hx.h, 'h');
  const rad = radSvg(L2 + 40, radTop, R2 - L2 - 80, radH, 'h');
  parts.defs.push(hxs.defs, rad.defs);
  const it = parts.items;
  it.push(hxs.body, rad.body);
  it.push(`<text class="dg-label" x="${hx.x + hx.w / 2}" y="${hx.y - 8}" text-anchor="middle">Hőcserélő</text>`);
  it.push(`<text class="dg-label" x="${W / 2}" y="${radTop + radH + 46}" text-anchor="middle">Radiátor · Külső kör</text>`);
  it.push(`<text class="dg-label" x="${L + 18}" y="${yLast + 26}">Belső kör</text>`);
  it.push(pumpSvg('pump1', 'P1', 162, yIn, 'below', 180));
  it.push(flowSvg('flow1', 'F1', 80, yIn, 'below', 180));
  it.push(pumpSvg('pump2', 'P2', L2, yA + 56, 'right', -90));
  it.push(flowSvg('flow2', 'F2', L2, yA + 120, 'right', -90));
  it.push(sensorSvg('t1', 'T1 · ' + S.t1, L, yLast + 70, L + 14, yLast + 47, 118));
  it.push(sensorSvg('t2', 'T2 · ' + S.t2, R, yLast + 70, R - 132, yLast + 47, 118));
  it.push(sensorSvg('t3', 'T3 · ' + S.t3, R2, yA + 150, R2 - 132, yA + 127, 118));
  it.push(sensorSvg('t4', 'T4 · ' + S.t4, L2, yA + 182, L2 + 14, yA + 159, 118));
  it.push(dtChip('dt_miners', W / 2 - 50, yLast + 59, 100, 'ΔT '));
  it.push(dtChip('dt_radiator', R2 - 152, yA + 22, 140));
  return { W, H: radTop + radH + 60, parts };
}

// --------------------------------------------------------------------- osztály
export class Diagram {
  constructor(container, { onMinerClick } = {}) {
    this.el = container;
    this.onMinerClick = onMinerClick;
    this.key = null;
    this.system = null;
    this.miners = [];
    this.refs = null;
    container.addEventListener('click', (e) => {
      const g = e.target.closest('[data-miner]');
      if (g && this.onMinerClick) this.onMinerClick(g.getAttribute('data-miner'));
    });
    container.addEventListener('keydown', (e) => {
      const g = e.target.closest && e.target.closest('[data-miner]');
      if (g && (e.key === 'Enter' || e.key === ' ') && this.onMinerClick) { e.preventDefault(); this.onMinerClick(g.getAttribute('data-miner')); }
    });
    if (window.ResizeObserver) {
      this.ro = new ResizeObserver(() => { if (this.system) { this._rebuildIfNeeded(); this._apply(); } });
      this.ro.observe(container);
    }
  }

  update(system, miners) {
    this.system = system;
    this.miners = miners || [];
    this._rebuildIfNeeded();
    this._apply();
  }

  _rebuildIfNeeded() {
    const width = this.el.clientWidth || 800;
    const mode = width < WIDE_MIN_PX ? 'tall' : 'wide';
    const visible = this.miners.filter((m) => m.enabled);
    const t = this.system.temps;
    const names = ['t1', 't2', 't3', 't4'].map((k) => t[k].short).join('|');
    const key = mode + '#' + names + '#' + visible.map((m) => m.id + ':' + m.name).join(',');
    if (key === this.key) return;
    this.key = key;
    const S = { t1: t.t1.short, t2: t.t2.short, t3: t.t3.short, t4: t.t4.short };
    const lay = mode === 'tall' ? layoutTall(visible, S) : layoutWide(visible, S);
    const p = lay.parts;
    const gradDefs = [];
    // a minerek ágainak hideg->meleg színátmenete nincs: a csövek színe a T1/T2 alapján változik
    this.el.innerHTML = `<svg viewBox="0 0 ${lay.W} ${lay.H}" xmlns="http://www.w3.org/2000/svg">
      <defs>${p.defs.join('')}${gradDefs.join('')}</defs>
      <g class="pipes">${p.pipes.join('')}</g>
      <g class="items">${p.items.join('')}</g>
      <g class="miners">${p.miners.join('')}</g></svg>`;
    const svg = this.el.firstElementChild;
    this.refs = {
      svg,
      pipes: Array.from(svg.querySelectorAll('.pipe')).map((g) => ({ g, water: g.querySelector('.pipe-water'), temp: g.dataset.temp, loop: +g.dataset.loop })),
      pumps: Object.fromEntries(Array.from(svg.querySelectorAll('.pump')).map((g) => [g.dataset.pump, { g, state: g.querySelector('.pump-state') }])),
      flows: Object.fromEntries(Array.from(svg.querySelectorAll('.fsw')).map((g) => [g.dataset.flow, { g, state: g.querySelector('.fsw-state') }])),
      sensors: Object.fromEntries(Array.from(svg.querySelectorAll('.tsens')).map((g) => [g.dataset.sensor, { g, val: g.querySelector('.t-val') }])),
      dts: Object.fromEntries(Array.from(svg.querySelectorAll('.dt-chip')).map((g) => [g.dataset.dt, { text: g.querySelector('text'), prefix: g.dataset.prefix }])),
      miners: Object.fromEntries(Array.from(svg.querySelectorAll('.mbox')).map((g) => [g.dataset.miner, {
        g, hash: g.querySelector('.m-hash'), temp: g.querySelector('.m-temp'), state: g.querySelector('.m-state'),
      }])),
      hxIn: svg.querySelectorAll('#g-hx-in stop'),
      hxOut: svg.querySelectorAll('#g-hx-out stop'),
      rad: svg.querySelectorAll('#g-rad stop'),
      radG: svg.querySelector('.rad'),
    };
    this._last = {};
  }

  _set(el, prop, value) {
    // csak változás esetén írunk a DOM-ba
    const k = prop;
    if (el['__' + k] === value) return;
    el['__' + k] = value;
    if (prop === 'text') el.textContent = value;
    else if (prop === 'class') el.setAttribute('class', value);
    else el.setAttribute(prop, value);
  }

  _apply() {
    const r = this.refs;
    const s = this.system;
    if (!r || !s) return;
    const T = s.temps;
    const col = { t1: tempColor(T.t1.value), t2: tempColor(T.t2.value), t3: tempColor(T.t3.value), t4: tempColor(T.t4.value) };
    const flowing = {
      1: s.flows.flow1.flow || (!s.flows.flow1.enabled && s.pumps.pump1.on),
      2: s.flows.flow2.flow || (!s.flows.flow2.enabled && s.pumps.pump2.on),
    };
    for (const p of r.pipes) {
      this._set(p.water, 'stroke', col[p.temp]);
      this._set(p.g, 'class', 'pipe' + (flowing[p.loop] ? ' flowing' : ''));
    }
    for (const id of ['pump1', 'pump2']) {
      const ref = r.pumps[id];
      if (!ref) continue;
      const st = s.pumps[id];
      let cls = 'pump' + (st.on ? ' on' : '');
      let txt = st.on ? 'BE' : 'KI';
      if (st.on && st.manual) { cls += ' manual'; txt = 'KÉZI'; }
      else if (st.on && st.assist && !['running', 'starting', 'stopping'].includes(s.system.state)) { cls += ' assist'; txt = 'UTÓHŰTÉS'; }
      this._set(ref.g, 'class', cls);
      this._set(ref.state, 'text', txt);
    }
    for (const id of ['flow1', 'flow2']) {
      const ref = r.flows[id];
      if (!ref) continue;
      const st = s.flows[id];
      this._set(ref.g, 'class', 'fsw ' + st.status);
      this._set(ref.state, 'text', FLOW_TEXT[st.status] || '');
    }
    for (const id of ['t1', 't2', 't3', 't4']) {
      const ref = r.sensors[id];
      if (!ref) continue;
      const st = T[id];
      this._set(ref.g, 'class', 'tsens ' + st.status);
      this._set(ref.val, 'text', st.fault ? 'SZENZORHIBA' : st.value === null ? '—' : num(st.value, 1) + ' °C');
    }
    const dt = s.derived;
    for (const key of ['dt_miners', 'dt_radiator']) {
      const ref = r.dts[key];
      if (ref) this._set(ref.text, 'text', ref.prefix + (dt[key] === null ? '—' : num(dt[key], 1) + ' °C'));
    }
    if (r.hxIn.length === 2) { this._set(r.hxIn[0], 'stop-color', col.t2); this._set(r.hxIn[1], 'stop-color', col.t1); }
    if (r.hxOut.length === 2) { this._set(r.hxOut[0], 'stop-color', col.t4); this._set(r.hxOut[1], 'stop-color', col.t3); }
    if (r.rad.length === 2) { this._set(r.rad[0], 'stop-color', col.t3); this._set(r.rad[1], 'stop-color', col.t4); }
    if (r.radG) this._set(r.radG, 'class', 'rad' + (flowing[2] && (dt.dt_radiator || 0) > 0.3 ? ' radiating' : ''));

    for (const m of this.miners) {
      const ref = r.miners[m.id];
      if (!ref) continue;
      const d = m.data || {};
      const maxT = Math.max(d.temp_chip || -1, d.temp_board || -1);
      let cls = 'mbox ' + m.status;
      if (m.has_errors) cls += ' has-errors';
      if (maxT >= 85) cls += ' hot';
      this._set(ref.g, 'class', cls);
      const online = m.online && m.data;
      this._set(ref.hash, 'text', online ? hashrate(d.hashrate || 0) : '—');
      let tTxt;
      if (!online) tTxt = m.host;
      else if (d.temp_chip !== null && d.temp_chip !== undefined) tTxt = 'Chip: ' + num(d.temp_chip, 0) + ' °C';
      else if (d.temp_board !== null && d.temp_board !== undefined) tTxt = 'Panel: ' + num(d.temp_board, 0) + ' °C';
      else tTxt = 'Hőfok: —';
      this._set(ref.temp, 'text', tTxt);
      this._set(ref.state, 'text', (m.has_errors ? '⚠ ' : '') + (STATUS_LABELS[m.status] || m.status).toUpperCase());
    }
  }
}
