// Főoldal: állapot, vezérlő gombok (indítás/leállítás + tartásos légtelenítő gombok), mutatók,
// figyelmeztetések, rendszerábra, miner kártyák és részletek.
import { api, $, esc, toast, confirmDialog, openModal, num, temp, hashrate, power, duration, timeOnly, dateTime, ago,
  eventTime, STATUS_LABELS } from './util.js';
import { Diagram } from './diagram.js';

export function renderEventRows(events, compact = false) {
  if (!events || !events.length) return '<div class="empty-note">Nincs esemény.</div>';
  return events.map((e) => `<div class="ev ${e.level}">
      <span class="ev-time" title="${esc(dateTime(e.ts))}">${compact ? esc(eventTime(e.ts)) : esc(dateTime(e.ts))}</span>
      <span class="ev-code">${esc(e.code || '')}</span>
      <span class="ev-msg">${esc(e.message)}${e.source && !compact ? `<span class="ev-src">· ${esc(e.source)}</span>` : ''}</span>
    </div>`).join('');
}

const MODE_LABELS = { normal: 'Normál', sleep: 'Alvó (leállítva)', unknown: 'Ismeretlen' };
const ACTION_LABELS = { start: 'indítás', stop: 'leállítás', reboot: 'újraindítás' };

export class Dashboard {
  constructor(store) {
    this.store = store;
    this.diagram = new Diagram($('#diagram'), { onMinerClick: (id) => this.openMiner(id) });
    this.minerModal = null;
    this._bindControls();
    $('#temp-legend').innerHTML = '<span>hideg</span><span class="bar"></span><span>meleg</span>';
    $('#miner-grid').addEventListener('click', (e) => {
      const card = e.target.closest('[data-id]');
      if (card) this.openMiner(card.dataset.id);
    });
  }

  // ----------------------------------------------------------- vezérlés
  _bindControls() {
    $('#btn-start').addEventListener('click', async () => {
      const sys = this.store.system;
      if (!sys) return;
      const emergency = sys.system.state === 'emergency';
      const ok = await confirmDialog('Rendszer indítása',
        (emergency ? '<b>Vészleállás volt érvényben</b> – az indítás egyben nyugtázza is.<br><br>' : '') +
        'Először a szivattyúk indulnak, majd ' + esc(num(sys.system.start_delay, 0)) + ' mp múlva a minerek. Indítod a rendszert?',
        'Indítás');
      if (!ok) return;
      this._command('/api/control/start', $('#btn-start'));
    });
    $('#btn-stop').addEventListener('click', async () => {
      const ok = await confirmDialog('Rendszer leállítása',
        'A minerek leállítási (alvó mód) parancsot kapnak, majd ' + esc(num((this.store.system || {system: {}}).system.post_stop || 0, 0)) +
        ' mp utókeringetés után a szivattyúk is leállnak. Leállítod a rendszert?',
        'Leállítás', true);
      if (!ok) return;
      this._command('/api/control/stop', $('#btn-stop'));
    });
    $('#btn-ack').addEventListener('click', () => this._command('/api/control/ack', $('#btn-ack')));
    for (const btn of document.querySelectorAll('.hold-btn')) this._bindHold(btn);
  }

  async _command(path, btn) {
    btn.classList.add('busy');
    try {
      const r = await api(path, { method: 'POST', body: {} });
      toast(r.message || 'OK', 'ok');
    } catch (e) {
      toast(e.message, 'err', 6000);
    } finally {
      btn.classList.remove('busy');
    }
  }

  _bindHold(btn) {
    const pump = btn.dataset.pump;
    let timer = null;
    let active = false;
    const send = (action) => api('/api/control/manual', { method: 'POST', body: { pump, action } }).catch((e) => {
      if (action === 'press') toast(e.message, 'warn');
      end(false);
    });
    const start = (e) => {
      if (e) e.preventDefault();
      if (btn.disabled || active) return;
      active = true;
      btn.classList.add('holding');
      if (e && e.pointerId !== undefined) { try { btn.setPointerCapture(e.pointerId); } catch (err) { /* ignore */ } }
      send('press');
      timer = setInterval(() => send('hold'), 300);
      if (navigator.vibrate) navigator.vibrate(25);
    };
    const end = (notify = true) => {
      if (!active) return;
      active = false;
      btn.classList.remove('holding');
      clearInterval(timer);
      timer = null;
      if (notify) api('/api/control/manual', { method: 'POST', body: { pump, action: 'release' } }).catch(() => {});
    };
    btn.addEventListener('pointerdown', start);
    btn.addEventListener('pointerup', () => end());
    btn.addEventListener('pointercancel', () => end());
    btn.addEventListener('lostpointercapture', () => end());
    btn.addEventListener('contextmenu', (e) => e.preventDefault());
    btn.addEventListener('keydown', (e) => { if ((e.key === ' ' || e.key === 'Enter') && !e.repeat) start(e); });
    btn.addEventListener('keyup', (e) => { if (e.key === ' ' || e.key === 'Enter') end(); });
    window.addEventListener('blur', () => end());
    document.addEventListener('visibilitychange', () => { if (document.hidden) end(); });
    btn._endHold = end;
  }

  // -------------------------------------------------------------- render
  render() {
    const st = this.store;
    const s = st.system;
    if (!s) return;
    const sys = s.system;
    const now = Date.now() / 1000;

    const card = $('#status-card');
    card.dataset.state = sys.state;
    $('#status-label').textContent = sys.label;
    $('#status-since').textContent = duration(now - sys.since) + ' óta (' + timeOnly(sys.since) + ')';
    const phase = $('#status-phase');
    phase.hidden = !sys.phase;
    phase.textContent = sys.phase || '';
    const alarm = $('#alarm-box');
    if (sys.alarm) {
      alarm.hidden = false;
      $('#alarm-code').textContent = sys.alarm.code;
      $('#alarm-time').textContent = dateTime(sys.alarm.ts);
      $('#alarm-title').textContent = sys.alarm.title || '';
      $('#alarm-msg').textContent = sys.alarm.message || '';
      $('#btn-ack').hidden = sys.state !== 'emergency';
    } else {
      alarm.hidden = true;
    }
    const inhibit = $('#inhibit-box');
    const showInhibit = sys.start_inhibit && !['running', 'starting'].includes(sys.state);
    inhibit.hidden = !showInhibit;
    if (showInhibit) inhibit.textContent = 'Indítás tiltva: ' + sys.start_inhibit;

    const bStart = $('#btn-start');
    bStart.disabled = !sys.start_allowed;
    bStart.title = sys.start_allowed ? '' : (sys.start_inhibit || 'A rendszer már üzemel');
    for (const btn of document.querySelectorAll('.hold-btn')) {
      const p = s.pumps[btn.dataset.pump];
      btn.disabled = !sys.manual_allowed;
      if (!sys.manual_allowed && btn._endHold) btn._endHold(false);
      btn.classList.toggle('running-ind', p.on && p.manual);
      btn.querySelector('em').textContent = !sys.manual_allowed ? 'üzem közben tiltva' : p.on && p.manual ? 'JÁR – engedd el a leállításhoz' : 'tartsd nyomva';
    }

    this._renderKpis();
    this._renderWarnings();
    this.diagram.update(s, st.miners);
    this._renderMiners();
    $('#recent-events').innerHTML = renderEventRows((st.events || []).slice(0, 8), true);
    if (this.minerModal) this._renderMinerModal();
  }

  _renderKpis() {
    const st = this.store;
    const sum = st.summary || {};
    const d = st.system.derived;
    const ideal = sum.hashrate_ideal;
    const pct = ideal ? (100 * sum.hashrate) / ideal : null;
    const tiles = [
      ['Összes hashrate', hashrate(sum.hashrate || 0), pct !== null ? num(pct, 0) + '% a névlegesből' : 'online minerek összesen'],
      ['Minerek', `${sum.online || 0}<small> / ${sum.total || 0} online</small>`, `${sum.mining || 0} bányászik${sum.with_errors ? ' · ' + sum.with_errors + ' hibát jelez' : ''}`],
      ['Teljesítmény', power(sum.power), sum.power ? 'a minerek jelentése alapján' : 'a firmware nem jelenti'],
      ['Legmelegebb miner', temp(sum.max_temp, 0), sum.max_temp_name || '—', sum.max_temp >= 85],
      ['ΔT minerek', d.dt_miners === null ? '—' : num(d.dt_miners, 1) + '<small> °C</small>', 'T2 − T1 (fűtés a vízbe)'],
      ['ΔT radiátor', d.dt_radiator === null ? '—' : num(d.dt_radiator, 1) + '<small> °C</small>', 'T3 − T4 (hőleadás)'],
    ];
    $('#kpis').innerHTML = tiles.map(([l, v, sub, hot]) =>
      `<div class="kpi${hot ? ' hot' : ''}"><div class="kpi-label">${l}</div><div class="kpi-value">${v}</div><div class="kpi-sub">${esc(sub)}</div></div>`).join('');
  }

  _renderWarnings() {
    const st = this.store;
    const items = (st.system.warnings || []).map((w) => ({ level: w.level, code: w.code, message: w.message, since: w.since }));
    for (const m of st.miners || []) {
      if (!m.enabled) continue;
      if (m.status === 'offline') items.push({ level: 'warning', code: 'W401', message: `${m.name} nem elérhető${m.last_error ? ': ' + m.last_error : ''}`, since: m.last_seen });
      if (m.data && m.data.errors && m.data.errors.length && m.online) {
        items.push({ level: 'warning', code: 'W402', message: `${m.name}: ${m.data.errors.join('; ')}` });
      }
      if (m.prod_alarm) items.push({ level: 'warning', code: m.prod_alarm.code, message: m.prod_alarm.message, since: m.prod_alarm.since });
    }
    const fake = (st.miners || []).filter((m) => m.enabled && m.type === 'simulated');
    if (!st.sim && fake.length) {
      items.push({ level: 'warning', code: 'W405', message: `Szimulált (nem valódi) miner van a listában: ${fake.map((m) => m.name).join(', ')} – ` +
        'valódi hardver módban töröld a Beállítások › Minerek oldalon, és vedd fel a valódi minereket.' });
    }
    const order = { error: 0, warning: 1, info: 2 };
    items.sort((a, b) => order[a.level] - order[b.level]);
    $('#warn-list').innerHTML = items.length ? items.map((w) => `<div class="warn ${w.level}"><span class="code">${esc(w.code)}</span><span>${esc(w.message)}</span></div>`).join('')
      : '<div class="warn-empty">✓ Nincs aktív figyelmeztetés</div>';
  }

  _minerCard(m) {
    const d = m.data || {};
    const online = m.online && m.data;
    const ideal = d.hashrate_ideal;
    const pct = online && ideal ? Math.max(0, Math.min(100, (100 * (d.hashrate || 0)) / ideal)) : 0;
    const third = d.power !== null && d.power !== undefined ? ['Fogyasztás', power(d.power)]
      : d.fans && d.fans.length ? ['Ventilátor', num(Math.max(...d.fans), 0) + ' rpm'] : ['Üzemmód', MODE_LABELS[d.mode] || '—'];
    const errs = online ? (d.errors || []) : [];
    const foot = online ? `Üzemidő: ${duration(d.uptime)}${d.pool && d.pool.url ? ' · ' + esc(d.pool.url.replace(/^stratum\+tcp:\/\//, '')) : ''}`
      : (m.last_error ? esc(m.last_error) : 'Nincs adat');
    return `<div class="mcard" data-id="${esc(m.id)}" style="${m.enabled ? '' : 'opacity:.55'}">
      <div class="mcard-head"><span class="mcard-name">${esc(m.name)}</span><span class="badge ${m.status}">${STATUS_LABELS[m.status] || m.status}</span></div>
      <div class="mcard-host">${esc(m.host)}${d.model ? ' · ' + esc(d.model) : ''}</div>
      <div class="mcard-hash"><b>${online ? hashrate(d.hashrate || 0) : '—'}</b><span>${ideal ? '/ ' + hashrate(ideal, 0) + ' névleges' : ''}</span></div>
      <div class="hbar"><i style="width:${pct.toFixed(1)}%"></i></div>
      <div class="mcard-stats">
        <div class="mstat${d.temp_chip >= 85 ? ' hot' : ''}"><label>Chip max</label><span>${online ? temp(d.temp_chip, 0) : '—'}</span></div>
        <div class="mstat"><label>Panel max</label><span>${online ? temp(d.temp_board, 0) : '—'}</span></div>
        <div class="mstat"><label>${third[0]}</label><span>${online ? third[1] : '—'}</span></div>
      </div>
      ${errs.length ? `<div class="mcard-errors">${errs.map((e) => `<div>⚠ ${esc(e)}</div>`).join('')}</div>` : ''}
      ${m.prod_alarm ? `<div class="mcard-errors"><div class="prod">⚠ ${esc(m.prod_alarm.code)} – ${m.prod_alarm.code === 'W407' ? 'nem kezdett el termelni' : 'nem termel'} (${ago(m.prod_alarm.since)})</div></div>` : ''}
      ${m.last_cmd && !m.last_cmd.ok ? `<div class="mcard-errors"><div class="err">Parancs sikertelen (${esc(ACTION_LABELS[m.last_cmd.action] || m.last_cmd.action)}): ${esc(m.last_cmd.message)}</div></div>` : ''}
      <div class="mcard-foot">${foot}</div></div>`;
  }

  _renderMiners() {
    const miners = this.store.miners || [];
    $('#miners-count').textContent = miners.length ? `(${miners.filter((m) => m.enabled).length} db)` : '';
    $('#miner-grid').innerHTML = miners.length ? miners.map((m) => this._minerCard(m)).join('')
      : '<div class="empty-note">Még nincs miner felvéve. A <a href="#settings">Beállítások › Minerek</a> oldalon adhatsz hozzá (hálózati kereséssel is).</div>';
  }

  // ---------------------------------------------------------- részletek
  openMiner(id) {
    const m = (this.store.miners || []).find((x) => x.id === id);
    if (!m) return;
    const actions = [];
    if (m.can_control) {
      actions.push({ label: 'Indítás', cls: 'btn-ok', onClick: () => this._minerAction(id, 'start') });
      actions.push({ label: 'Leállítás (alvó mód)', cls: 'btn-danger', onClick: () => this._minerAction(id, 'stop') });
    }
    if (m.can_reboot) actions.push({ label: 'Újraindítás', cls: '', onClick: () => this._minerAction(id, 'reboot') });
    actions.push({ label: 'Bezárás', cls: 'btn-ghost' });
    this.minerModal = openModal({ title: m.name, body: '<div></div>', actions, wide: true, onClose: () => { this.minerModal = null; } });
    this.minerModal.minerId = id;
    this._renderMinerModal();
  }

  async _minerAction(id, action) {
    const m = (this.store.miners || []).find((x) => x.id === id);
    const sysState = this.store.system.system.state;
    if ((action === 'start' || action === 'reboot') && sysState !== 'running') {
      toast('Csak üzemelő rendszernél (járó szivattyúk mellett) indítható / indítható újra miner!', 'warn', 6000);
      return;
    }
    const labels = { start: 'elindítod', stop: 'leállítod (alvó mód)', reboot: 'újraindítod' };
    if (!(await confirmDialog(m.name, `Biztosan ${labels[action]} ezt a minert: <b>${esc(m.name)}</b>?`, 'Igen', action !== 'start'))) return;
    try {
      const r = await api(`/api/miners/${encodeURIComponent(id)}/${action}`, { method: 'POST', body: {} });
      toast(r.message, 'ok');
    } catch (e) {
      toast(e.message, 'err', 6000);
    }
  }

  _renderMinerModal() {
    const mm = this.minerModal;
    const m = (this.store.miners || []).find((x) => x.id === mm.minerId);
    if (!m) return;
    const d = m.data || {};
    const online = m.online && m.data;
    const kv = (k, v) => `<div class="mstat"><label>${k}</label><span>${v}</span></div>`;
    const boards = (d.boards || []).map((b) => `<tr><td>${b.idx}.</td><td class="live">${hashrate(b.hashrate)}</td><td class="live">${temp(b.chip, 0)}</td>
      <td class="live">${temp(b.board, 0)}</td><td class="live">${b.asics ?? '—'}</td><td class="live">${b.bad_chips ? '<span class="pill bad">' + b.bad_chips + '</span>' : '0'}</td></tr>`).join('');
    const cmd = m.last_cmd ? `${ACTION_LABELS[m.last_cmd.action] || m.last_cmd.action} – ${m.last_cmd.ok ? 'sikeres' : 'SIKERTELEN'}: ${esc(m.last_cmd.message)} (${ago(m.last_cmd.ts)})` : '—';
    mm.body.innerHTML = `
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:12px">
        <span class="badge ${m.status}">${STATUS_LABELS[m.status] || m.status}</span>
        <span class="mcard-host">${esc(m.host)} · ${esc(m.type_label)}</span></div>
      ${!online && m.last_error ? `<div class="note warn">${esc(m.last_error)}</div>` : ''}
      ${m.prod_alarm ? `<div class="note warn"><b>${esc(m.prod_alarm.code)}</b> ${esc(m.prod_alarm.message)} (${ago(m.prod_alarm.since)})</div>` : ''}
      ${(d.errors || []).length && online ? `<div class="errors-box"><b>A miner hibát jelez:</b><ul>${d.errors.map((e) => `<li>${esc(e)}</li>`).join('')}</ul></div><br>` : ''}
      <div class="detail-grid">
        ${kv('Modell', esc(d.model || '—'))}${kv('Firmware', esc(d.firmware || '—'))}${kv('Üzemmód', MODE_LABELS[d.mode] || '—')}
        ${kv('Hashrate (5 mp)', online ? hashrate(d.hashrate || 0) : '—')}${kv('Hashrate (átlag)', online ? hashrate(d.hashrate_avg) : '—')}${kv('Névleges', hashrate(d.hashrate_ideal))}
        ${kv('Chip max', temp(d.temp_chip, 0))}${kv('Panel max', temp(d.temp_board, 0))}${kv('Fogyasztás', power(d.power))}
        ${kv('Ventilátorok', d.fans && d.fans.length ? d.fans.map((f) => num(f, 0)).join(' / ') + ' rpm' : '—')}${kv('Üzemidő', duration(d.uptime))}
        ${kv('Elfogadott / elutasított', d.accepted !== null && d.accepted !== undefined ? num(d.accepted, 0) + ' / ' + num(d.rejected, 0) : '—')}
        ${kv('Utolsó adat', m.last_seen ? ago(m.last_seen) : '—')}${kv('Kívánt állapot', m.desired === true ? 'bányászik' : m.desired === false ? 'leállítva' : '—')}
      </div>
      ${d.pool ? `<div class="hint" style="margin-bottom:10px">Pool: <span class="mono">${esc(d.pool.url)}</span> · ${esc(d.pool.user || '')} · ${esc(d.pool.status || '')}</div>` : ''}
      <div class="hint" style="margin-bottom:12px">Utolsó parancs: ${cmd}</div>
      ${boards ? `<div class="tbl-wrap"><table class="tbl"><thead><tr><th>Hashboard</th><th>Hashrate</th><th>Chip</th><th>Panel</th><th>ASIC</th><th>Hibás chip</th></tr></thead><tbody>${boards}</tbody></table></div>` : ''}`;
  }
}
