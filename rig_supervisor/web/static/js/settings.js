// Beállítások: Minerek, Védelmek, Értesítések, Kalibráció & GPIO, Rendszer.
import { api, $, $$, esc, toast, openModal, confirmDialog, num, temp, hashrate, bytes, dateTime, STATUS_LABELS } from './util.js';

const HEADER = {
  1: '3V3', 2: '5V', 3: 'GPIO2', 4: '5V', 5: 'GPIO3', 6: 'GND', 7: 'GPIO4', 8: 'GPIO14', 9: 'GND', 10: 'GPIO15',
  11: 'GPIO17', 12: 'GPIO18', 13: 'GPIO27', 14: 'GND', 15: 'GPIO22', 16: 'GPIO23', 17: '3V3', 18: 'GPIO24', 19: 'GPIO10', 20: 'GND',
  21: 'GPIO9', 22: 'GPIO25', 23: 'GPIO11', 24: 'GPIO8', 25: 'GND', 26: 'GPIO7', 27: 'ID_SD', 28: 'ID_SC', 29: 'GPIO5', 30: 'GND',
  31: 'GPIO6', 32: 'GPIO12', 33: 'GPIO13', 34: 'GND', 35: 'GPIO19', 36: 'GPIO16', 37: 'GPIO26', 38: 'GPIO20', 39: 'GND', 40: 'GPIO21',
};
const IO_SHORT = { pump1: 'P1 SSR', pump2: 'P2 SSR', flow1: 'F1', flow2: 'F2', btn_start: 'START', btn_stop: 'STOP', killswitch: 'KILL' };
const SENSORS = ['t1', 't2', 't3', 't4'];
const FAULT_LABELS = { open: 'Szakadás', short: 'Rövidzár', range: 'Tartományon kívül', adc: 'ADC hiba', noadc: 'Nincs ADC' };
// értesítési kategóriák: [azonosító, megnevezés, leírás, kritikus (azonnal, korlát nélkül)]
const NOTIFY_CATS = [
  ['killswitch', 'KILLSWITCH aktiválódott', 'E001, E002 – abszolút vészleállás: minerek és szivattyúk azonnal leállítva', true],
  ['emergency', 'Vészleállás', 'Áramlás kimaradás, hőfok határérték túllépés, szenzorhiba (E101–E214)', true],
  ['manual', 'Kézi indítás / leállítás', 'Webes felületről vagy fizikai gombbal, vészleállás nyugtázása (I601–I603)'],
  ['miner_production', 'Miner nem termel', 'Abbahagyta a termelést / indítás után nem kezdett el termelni (W406, W407), ismét termel (I606)'],
  ['errors', 'Egyéb hibák', 'Pl. a GPIO nem vezérelhető, a szivattyúk nem kapcsolhatók (E301)'],
  ['warnings', 'Figyelmeztetések', 'Miner hibát jelez / nem elérhető, hőfok- és szenzor figyelmeztetés, ADC hiba (W2xx–W4xx)'],
  ['restart', 'Program újraindulás', 'Áramszünet vagy újraindítás után (I604), automatikus visszaállás üzembe (I605)'],
];

function get(obj, path) { return path.split('.').reduce((o, k) => (o == null ? undefined : o[k]), obj); }
function setPath(obj, path, value) {
  const keys = path.split('.');
  let o = obj;
  keys.slice(0, -1).forEach((k) => { if (typeof o[k] !== 'object' || o[k] === null) o[k] = {}; o = o[k]; });
  o[keys[keys.length - 1]] = value;
}

// ------------------------------------------------------------ mező építők
function numField(cfg, path, label, unit, { step = 'any', min, max, hint } = {}) {
  const v = get(cfg, path);
  return `<div class="field"><label>${esc(label)}</label>
    <div class="inp-unit"><input class="inp num" type="number" step="${step}" ${min !== undefined ? `min="${min}"` : ''} ${max !== undefined ? `max="${max}"` : ''}
      data-path="${path}" data-type="num" value="${v === null || v === undefined ? '' : v}">${unit ? `<span>${esc(unit)}</span>` : ''}</div>
    ${hint ? `<small>${hint}</small>` : ''}</div>`;
}
function textField(cfg, path, label, hint, { placeholder = '', mono = false, list = '', wide = false } = {}) {
  return `<div class="field${wide ? ' wide' : ''}"><label>${esc(label)}</label><input class="inp${mono ? ' mono' : ''}" type="text" data-path="${path}" data-type="text"
    value="${esc(get(cfg, path) || '')}" placeholder="${esc(placeholder)}" ${list ? `list="${list}"` : ''} autocomplete="off" spellcheck="false">
    ${hint ? `<small>${hint}</small>` : ''}</div>`;
}
function areaField(cfg, path, label, hint, { placeholder = '', rows = 2, mono = false } = {}) {
  return `<div class="field wide"><label>${esc(label)}</label><textarea class="inp${mono ? ' mono' : ''}" rows="${rows}" data-path="${path}" data-type="text"
    placeholder="${esc(placeholder)}" spellcheck="false">${esc(get(cfg, path) || '')}</textarea>${hint ? `<small>${hint}</small>` : ''}</div>`;
}
// Titkos mező: a szerver sosem küldi vissza az értékét. Üresen hagyva a tárolt érték marad (null),
// a "törlés" gombbal üres szöveget küldünk, ami törli.
function secretField(cfg, path, label, hint, { textarea = false, placeholder = '', wide = false } = {}) {
  const keys = path.split('.');
  const has = get(cfg, keys.slice(0, -1).concat('has_' + keys[keys.length - 1]).join('.'));
  const ph = has ? '(változatlan – a mentett érték nem jelenik meg)' : placeholder;
  const input = textarea
    ? `<textarea class="inp mono" rows="2" data-path="${path}" data-type="secret" placeholder="${esc(ph)}" data-ph="${esc(ph)}" spellcheck="false"></textarea>`
    : `<input class="inp" type="password" data-path="${path}" data-type="secret" placeholder="${esc(ph)}" data-ph="${esc(ph)}" autocomplete="new-password">`;
  return `<div class="field${wide || textarea ? ' wide' : ''}"><label>${esc(label)}${has ? ` <button type="button" class="linkbtn" data-clear="${path}">törlés</button>` : ''}</label>
    ${input}${hint ? `<small>${hint}</small>` : ''}</div>`;
}
function switchField(cfg, path, label, hint) {
  return `<div class="field"><label class="switch"><input type="checkbox" data-path="${path}" data-type="bool" ${get(cfg, path) ? 'checked' : ''}><i></i>${esc(label)}</label>
    ${hint ? `<small>${hint}</small>` : ''}</div>`;
}
function selectField(cfg, path, label, options, type = 'text', hint) {
  const cur = get(cfg, path);
  return `<div class="field"><label>${esc(label)}</label><select class="inp" data-path="${path}" data-type="${type}">
    ${options.map(([v, l]) => `<option value="${esc(v)}" ${String(v) === String(cur) ? 'selected' : ''}>${esc(l)}</option>`).join('')}</select>
    ${hint ? `<small>${hint}</small>` : ''}</div>`;
}
function collect(root) {
  const patch = {};
  for (const el of $$('[data-path]', root)) {
    const t = el.dataset.type;
    let v;
    if (t === 'bool') v = el.checked;
    else if (t === 'secret') v = el.value !== '' ? el.value : el.dataset.cleared ? '' : null;
    else if (t === 'num' || t === 'int') {
      if (el.value.trim() === '') v = null;
      else v = t === 'int' ? parseInt(el.value, 10) : parseFloat(el.value.replace(',', '.'));
    } else v = el.value;
    setPath(patch, el.dataset.path, v);
  }
  return patch;
}
function showErrors(box, err) {
  const list = err && err.data && err.data.errors;
  box.innerHTML = `<b>${esc(err.message || 'Hiba')}</b>${list ? '<ul>' + list.map((e) => `<li>${esc(e)}</li>`).join('') + '</ul>' : ''}`;
  box.hidden = false;
}
function actions(id) {
  return `<div class="errors-box" id="${id}-err" hidden></div><div class="form-actions"><span class="status-msg" id="${id}-msg"></span>
    <button class="btn btn-ghost" id="${id}-reset">Visszaállítás</button><button class="btn btn-primary" id="${id}-save">Mentés</button></div>`;
}

export class SettingsView {
  constructor(store) {
    this.store = store;
    this.cfg = null;
    this.meta = null;
    this.sub = 'miners';
    this.hwTimer = null;
    this.hw = null;
    $('#settings-nav').addEventListener('click', (e) => {
      const b = e.target.closest('button');
      if (b) this.open(b.dataset.sub);
    });
  }

  async show() {
    try {
      await this.reload();
    } catch (e) {
      $('#settings-body').innerHTML = `<div class="card"><div class="errors-box">A beállítások nem tölthetők be: ${esc(e.message)}</div></div>`;
      return;
    }
    this.open(this.sub);
  }

  hide() {
    clearInterval(this.hwTimer);
    this.hwTimer = null;
  }

  async reload() {
    const d = await api('/api/config');
    this.cfg = d.config;
    this.meta = d.meta;
    this.sim = d.sim;
    this.mode = d.mode || {};
    this.version = d.version;
    this.busPins = d.bus_pins || {};
  }

  open(sub) {
    this.sub = sub;
    for (const b of $$('#settings-nav button')) b.classList.toggle('active', b.dataset.sub === sub);
    clearInterval(this.hwTimer);
    this.hwTimer = null;
    const body = $('#settings-body');
    if (!this.cfg) return;
    if (sub === 'miners') this._miners(body);
    else if (sub === 'protect') this._protect(body);
    else if (sub === 'notify') this._notify(body);
    else if (sub === 'calib') this._calib(body);
    else this._system(body);
    this.live();
  }

  async _save(prefix, patch, after) {
    const err = $('#' + prefix + '-err');
    const msg = $('#' + prefix + '-msg');
    if (err) err.hidden = true;
    try {
      const r = await api('/api/config', { method: 'POST', body: { patch } });
      this.cfg = r.config;
      if (r.bus_pins) this.busPins = r.bus_pins;
      if (r.mode) { this.mode = r.mode; this.sim = r.sim; }
      toast('Beállítások elmentve', 'ok');
      if (msg) msg.textContent = 'Mentve: ' + new Date().toLocaleTimeString('hu-HU');
      if (after) after();
      return true;
    } catch (e) {
      if (err) showErrors(err, e);
      else toast(e.message, 'err', 7000);
      return false;
    }
  }

  // ------------------------------------------------------- hardver mód
  _modeStatusHtml() {
    const m = this.mode || {};
    const hw = (this.hw && this.hw.hardware) || (this.store.system && this.store.system.hardware) || {};
    const why = m.reason ? ` <span class="hint">(${esc(m.reason)})</span>` : '';
    if (m.simulated) {
      return `<span class="pill bad">SZIMULÁCIÓ</span> <b>A program most NEM vezérli a Raspberry Pi lábait</b> – az itt látható
        kimenetek és bemenetek csak szimuláltak, a valódi GPIO lábak nem változnak.${why}`;
    }
    if (hw.gpio_ok === false) {
      const errs = [...(hw.gpio_errors || []), ...Object.entries(hw.pin_errors || {}).map(([k, v]) => k + ': ' + v)];
      return `<span class="pill bad">VALÓDI HARDVER – GPIO HIBA</span> A GPIO lábak nem vezérelhetők.${why}
        <div class="errors-box" style="margin-top:8px">${[...new Set(errs)].map(esc).join('<br>') || 'ismeretlen hiba'}</div>`;
    }
    return `<span class="pill on">VALÓDI HARDVER</span> A program a Raspberry Pi GPIO lábait vezérli –
      GPIO könyvtár: <b>${esc(hw.gpio_backend || '—')}</b>${m.pi_model ? ' · ' + esc(m.pi_model) : ''}.${why}`;
  }

  _modeCard(prefix) {
    const cur = (this.cfg.hardware || {}).backend;
    const opt = (v, l) => `<option value="${v}" ${v === cur ? 'selected' : ''}>${l}</option>`;
    return `<div class="card mode-card${this.mode && this.mode.simulated ? ' is-sim' : ''}">
      <div class="card-title">Hardver mód</div>
      <div class="mode-status" data-hw="mode-status">${this._modeStatusHtml()}</div>
      <div class="mode-row">
        <select class="inp" id="${prefix}-mode-sel">
          ${opt('rpi', 'Valódi hardver (GPIO + ADC)')}
          ${opt('auto', 'Automatikus (Pi-n valódi, máshol szimuláció)')}
          ${opt('sim', 'Szimuláció (a lábak nem változnak)')}
        </select>
        <button class="btn btn-primary" id="${prefix}-mode-apply">Alkalmaz</button>
      </div>
      <p class="hint" style="margin:8px 0 0">A váltás <b>azonnal</b> érvényes, a programot nem kell újraindítani. Csak leállított rendszernél váltható.
        A felületen választott mód elsőbbséget élvez a program indítási kapcsolóival (<span class="mono">--sim</span>) szemben.</p></div>`;
  }

  _bindModeCard(prefix) {
    $(`#${prefix}-mode-apply`).addEventListener('click', () => this.applyMode($(`#${prefix}-mode-sel`).value));
  }

  async applyMode(value) {
    try {
      if (!this.cfg) await this.reload();
      const r = await api('/api/config', { method: 'POST', body: { patch: { hardware: { backend: value } } } });
      this.cfg = r.config;
      this.mode = r.mode || {};
      this.sim = r.sim;
      this.hw = null;
      toast(r.sim ? 'Szimulációs mód – a valódi GPIO lábak nem változnak' : 'Valódi hardver mód bekapcsolva – a GPIO lábakat mostantól a program vezérli',
        r.sim ? 'warn' : 'ok', 7000);
      if (!document.querySelector('#view-settings').hidden) this.open(this.sub);
      return true;
    } catch (e) {
      toast(e.message, 'err', 9000);
      return false;
    }
  }

  // ------------------------------------------------------------- live
  live() {
    if (!this.cfg) return;
    const st = this.store;
    if (this.sub === 'miners') {
      for (const m of st.miners || []) {
        const el = document.querySelector(`[data-mstatus="${CSS.escape(m.id)}"]`);
        if (el) { el.className = 'badge ' + m.status; el.textContent = STATUS_LABELS[m.status] || m.status; }
      }
    } else if (this.sub === 'protect' && st.system) {
      for (const sid of SENSORS) {
        const el = document.querySelector(`[data-live-temp="${sid}"]`);
        if (!el) continue;
        const t = st.system.temps[sid];
        el.innerHTML = t.fault ? `<span class="pill warn">${esc(FAULT_LABELS[t.fault] || t.fault)}</span>`
          : `<span class="pill ${t.status === 'high' || t.status === 'low' ? 'bad' : 'on'}">${temp(t.value)}</span>`;
      }
    }
  }

  // ----------------------------------------------------------- minerek
  _miners(body) {
    const cfg = this.cfg;
    const types = Object.fromEntries(this.meta.driver_types.map((t) => [t.id, t]));
    const rows = cfg.miners.list.map((m, i) => `<tr data-idx="${i}">
        <td data-label="Név"><b>${esc(m.name)}</b></td><td class="mono" data-label="IP cím">${esc(m.host)}</td>
        <td data-label="Típus">${esc((types[m.type] || {}).label || m.type)}</td>
        <td data-label="Engedélyezve"><label class="switch"><input type="checkbox" data-toggle="${i}" ${m.enabled ? 'checked' : ''}><i></i></label></td>
        <td data-label="Állapot"><span class="badge" data-mstatus="${esc(m.id)}">—</span></td>
        <td class="actions"><div class="row-actions">
          <button class="btn btn-sm btn-icon" data-up="${i}" title="Feljebb" ${i === 0 ? 'disabled' : ''}>↑</button>
          <button class="btn btn-sm btn-icon" data-down="${i}" title="Lejjebb" ${i === cfg.miners.list.length - 1 ? 'disabled' : ''}>↓</button>
          <button class="btn btn-sm" data-edit="${i}">Szerkesztés</button>
          <button class="btn btn-sm btn-danger" data-del="${i}">Törlés</button></div></td></tr>`).join('');
    body.innerHTML = `
      <div class="card">
        <div class="card-title">Minerek <button class="btn btn-sm btn-primary" id="m-add" style="margin-left:auto">+ Új miner</button></div>
        ${cfg.miners.list.length ? `<div class="tbl-wrap"><table class="tbl cards"><thead><tr><th>Név</th><th>IP cím</th><th>Típus</th><th>Engedélyezve</th><th>Állapot</th><th></th></tr></thead>
          <tbody>${rows}</tbody></table></div>` : '<div class="empty-note">Még nincs miner felvéve. Add hozzá kézzel, vagy keresd meg őket a hálózaton (lent).</div>'}
        <p class="hint">A sorrend az ábrán és a listákban is érvényes. A kikapcsolt minereket a program nem kérdezi le és nem vezérli.</p>
      </div>
      <div class="card">
        <div class="card-title">Keresés a helyi hálózaton</div>
        <div class="form-grid">
          <div class="field"><label>Hálózat (CIDR)</label><input class="inp mono" id="scan-subnet" placeholder="192.168.1.0/24"><small>Legfeljebb /22 (1024 cím)</small></div>
          <div class="field"><label>API port</label><input class="inp num" id="scan-port" type="number" value="4028"></div>
          <div class="field"><label>&nbsp;</label><button class="btn btn-primary" id="scan-go">Keresés indítása</button></div>
        </div>
        <div class="scan-results" id="scan-results"></div>
      </div>
      <div class="card" id="mpoll">
        <div class="card-title">Lekérdezés</div>
        <div class="form-grid">
          ${numField(cfg, 'miners.poll_interval_s', 'Lekérdezési időköz', 'mp', { min: 2, max: 300 })}
          ${numField(cfg, 'miners.timeout_s', 'Kommunikációs időkorlát', 'mp', { min: 1, max: 30 })}
        </div>${actions('mp')}
      </div>`;
    $('#m-add').addEventListener('click', () => this._editMiner(null));
    body.querySelector('tbody') && body.querySelector('tbody').addEventListener('click', (e) => this._minerRowClick(e));
    body.querySelector('tbody') && body.querySelector('tbody').addEventListener('change', (e) => {
      const i = e.target.dataset.toggle;
      if (i === undefined) return;
      const list = this._listCopy();
      list[+i].enabled = e.target.checked;
      this._saveList(list);
    });
    api('/api/miners/scan').then((d) => { if (!$('#scan-subnet').value) $('#scan-subnet').value = d.subnet; }).catch(() => {});
    $('#scan-go').addEventListener('click', () => this._scan());
    $('#mp-save').addEventListener('click', () => this._save('mp', collect($('#mpoll'))));
    $('#mp-reset').addEventListener('click', () => this.open('miners'));
  }

  _listCopy() {
    return this.cfg.miners.list.map((m) => Object.assign({}, m, { password: null }));
  }

  async _saveList(list) {
    try {
      const r = await api('/api/config', { method: 'POST', body: { patch: { miners: { list } } } });
      this.cfg = r.config;
      toast('Miner lista elmentve', 'ok');
      this.open('miners');
      return true;
    } catch (e) {
      const errs = e.data && e.data.errors ? ': ' + e.data.errors.join('; ') : '';
      toast(e.message + errs, 'err', 8000);
      return false;
    }
  }

  async _minerRowClick(e) {
    const b = e.target.closest('button');
    if (!b) return;
    const list = this._listCopy();
    if (b.dataset.edit !== undefined) this._editMiner(+b.dataset.edit);
    else if (b.dataset.del !== undefined) {
      const m = list[+b.dataset.del];
      if (await confirmDialog('Miner törlése', `Biztosan törlöd: <b>${esc(m.name)}</b> (${esc(m.host)})? A miner nem kap több parancsot a programtól.`, 'Törlés', true)) {
        list.splice(+b.dataset.del, 1);
        this._saveList(list);
      }
    } else if (b.dataset.up !== undefined) {
      const i = +b.dataset.up;
      [list[i - 1], list[i]] = [list[i], list[i - 1]];
      this._saveList(list);
    } else if (b.dataset.down !== undefined) {
      const i = +b.dataset.down;
      [list[i + 1], list[i]] = [list[i], list[i + 1]];
      this._saveList(list);
    }
  }

  _editMiner(idx, preset) {
    const isNew = idx === null;
    const types = this.meta.driver_types.filter((t) => t.id !== 'simulated' || this.sim);
    const m = isNew ? Object.assign({ id: '', name: '', host: '', type: 'antminer', api_port: 4028, web_port: 80, username: 'root', password: '', enabled: true }, preset || {})
      : Object.assign({}, this.cfg.miners.list[idx]);
    const form = document.createElement('div');
    form.innerHTML = `<div class="form-grid">
        <div class="field"><label>Név</label><input class="inp" name="name" value="${esc(m.name)}" placeholder="pl. Hydro-01"></div>
        <div class="field"><label>IP cím / hosztnév</label><input class="inp mono" name="host" value="${esc(m.host)}" placeholder="192.168.1.101"></div>
        <div class="field"><label>Típus / firmware</label><select class="inp" name="type">${types.map((t) => `<option value="${t.id}" ${t.id === m.type ? 'selected' : ''}>${esc(t.label)}</option>`).join('')}</select></div>
        <div class="field"><label>API port (CGMiner)</label><input class="inp num" type="number" name="api_port" value="${m.api_port}"></div>
        <div class="field"><label>Web port</label><input class="inp num" type="number" name="web_port" value="${m.web_port}"></div>
        <div class="field"><label>Felhasználónév</label><input class="inp" name="username" value="${esc(m.username)}" autocomplete="off"></div>
        <div class="field"><label>Jelszó</label><input class="inp" type="password" name="password" value="" autocomplete="new-password" placeholder="${!isNew && m.has_password ? '(változatlan)' : ''}"></div>
        <div class="field"><label>&nbsp;</label><label class="switch"><input type="checkbox" name="enabled" ${m.enabled ? 'checked' : ''}><i></i>Engedélyezve</label></div>
      </div>
      <div class="note" id="type-note" style="margin-top:12px"></div>
      <div id="test-result"></div>`;
    const typeNote = () => {
      const t = this.meta.driver_types.find((x) => x.id === form.querySelector('[name=type]').value);
      form.querySelector('#type-note').innerHTML = t ? esc(t.note) + (t.can_control ? '' : ' <b>Vészleállásnál ezt a minert kézzel kell leállítani!</b>') : '';
    };
    form.querySelector('[name=type]').addEventListener('change', (e) => {
      const t = this.meta.driver_types.find((x) => x.id === e.target.value);
      if (t) {
        const u = form.querySelector('[name=username]');
        if (!u.value || ['root', 'admin'].includes(u.value)) u.value = t.default_username;
      }
      typeNote();
    });
    typeNote();
    const read = () => {
      const f = (n) => form.querySelector(`[name=${n}]`);
      const out = {
        id: m.id || undefined, name: f('name').value.trim(), host: f('host').value.trim(), type: f('type').value,
        api_port: parseInt(f('api_port').value, 10) || 4028, web_port: parseInt(f('web_port').value, 10) || 80,
        username: f('username').value.trim(), password: f('password').value, enabled: f('enabled').checked,
      };
      if (!out.password && !isNew) out.password = null; // változatlan
      if (!out.password && isNew) {
        const t = this.meta.driver_types.find((x) => x.id === out.type);
        out.password = t ? t.default_password : '';
      }
      return out;
    };
    openModal({
      title: isNew ? 'Új miner hozzáadása' : 'Miner szerkesztése',
      body: form,
      wide: true,
      actions: [
        { label: 'Kapcsolat tesztelése', cls: '', onClick: async (mm, btn) => {
          const box = form.querySelector('#test-result');
          btn.disabled = true;
          box.innerHTML = '<div class="note">Kapcsolódás…</div>';
          try {
            const r = await api('/api/miners/test', { method: 'POST', body: { miner: read() } });
            if (r.ok) {
              const d = r.data;
              box.innerHTML = `<div class="note"><b>Sikeres kapcsolat</b> (${r.elapsed_ms} ms) – ${esc(d.model || 'ismeretlen modell')}${d.firmware ? ' · ' + esc(d.firmware) : ''}<br>
                Hashrate: ${hashrate(d.hashrate)} · chip max: ${temp(d.temp_chip, 0)} · üzemmód: ${esc(d.mode)}
                ${d.errors && d.errors.length ? '<br>Hibák: ' + d.errors.map(esc).join('; ') : ''}</div>`;
              const nm = form.querySelector('[name=name]');
              if (!nm.value && d.model) nm.value = d.model.replace(/^Antminer\s*/i, '') + ' ' + read().host.split('.').pop();
            }
          } catch (e) {
            box.innerHTML = `<div class="errors-box">Sikertelen: ${esc(e.message)}</div>`;
          } finally {
            btn.disabled = false;
          }
        } },
        { label: 'Mégse', cls: 'btn-ghost' },
        { label: 'Mentés', cls: 'btn-primary', onClick: async (mm) => {
          const v = read();
          if (!v.host) { toast('Add meg az IP címet', 'warn'); return; }
          const list = this._listCopy();
          if (isNew) list.push(v); else list[idx] = Object.assign(list[idx], v);
          if (await this._saveList(list)) mm.close();
        } },
      ],
    });
  }

  async _scan() {
    const box = $('#scan-results');
    const btn = $('#scan-go');
    btn.disabled = true;
    box.innerHTML = '<div class="note">Keresés folyamatban… (/24 hálózatnál kb. 5–15 mp)</div>';
    try {
      const r = await api('/api/miners/scan', { method: 'POST', body: { subnet: $('#scan-subnet').value.trim(), port: parseInt($('#scan-port').value, 10) || 4028 } });
      const types = Object.fromEntries(this.meta.driver_types.map((t) => [t.id, t.label]));
      if (!r.results.length) { box.innerHTML = `<div class="empty-note">Nem található miner a(z) ${esc(r.subnet)} hálózaton (4028-as port).</div>`; return; }
      box.innerHTML = `<div class="tbl-wrap"><table class="tbl"><thead><tr><th>IP cím</th><th>Modell</th><th>Becsült típus</th><th></th></tr></thead><tbody>
        ${r.results.map((x, i) => `<tr><td class="mono">${esc(x.host)}</td><td>${esc(x.model || '—')}</td><td>${esc(types[x.type] || x.type)}</td>
          <td class="actions">${x.known ? '<span class="pill on">már felvéve</span>' : `<button class="btn btn-sm btn-primary" data-add="${i}">Hozzáadás</button>`}</td></tr>`).join('')}
        </tbody></table></div>`;
      box.querySelector('tbody').addEventListener('click', (e) => {
        const b = e.target.closest('[data-add]');
        if (!b) return;
        const x = r.results[+b.dataset.add];
        const t = this.meta.driver_types.find((dt) => dt.id === x.type) || {};
        this._editMiner(null, { host: x.host, type: x.type, api_port: x.port, name: (x.model || 'Miner').replace(/^Antminer\s*/i, '') + ' ' + x.host.split('.').pop(),
          username: t.default_username || 'root' });
      });
    } catch (e) {
      box.innerHTML = `<div class="errors-box">${esc(e.message)}</div>`;
    } finally {
      btn.disabled = false;
    }
  }

  // ---------------------------------------------------------- védelmek
  _protect(body) {
    const cfg = this.cfg;
    const rows = SENSORS.map((sid) => `<tr>
      <td><b>${sid.toUpperCase()}</b></td>
      <td><input class="inp sm" data-path="sensors.${sid}.name" data-type="text" value="${esc(cfg.sensors[sid].name)}" style="min-width:180px"></td>
      <td><input class="inp sm" data-path="sensors.${sid}.short" data-type="text" value="${esc(cfg.sensors[sid].short)}" style="min-width:100px"></td>
      <td class="live" data-live-temp="${sid}">—</td>
      <td><label class="switch"><input type="checkbox" data-path="sensors.${sid}.limit_enabled" data-type="bool" ${cfg.sensors[sid].limit_enabled ? 'checked' : ''}><i></i></label></td>
      <td><div class="inp-unit"><input class="inp sm num" type="number" step="0.5" data-path="sensors.${sid}.min" data-type="num" value="${cfg.sensors[sid].min}"><span>°C</span></div></td>
      <td><div class="inp-unit"><input class="inp sm num" type="number" step="0.5" data-path="sensors.${sid}.max" data-type="num" value="${cfg.sensors[sid].max}"><span>°C</span></div></td>
      <td><label class="switch"><input type="checkbox" data-path="sensors.${sid}.stop_on_fault" data-type="bool" ${cfg.sensors[sid].stop_on_fault ? 'checked' : ''}><i></i></label></td></tr>`).join('');
    body.innerHTML = `<div id="protect">
      <div class="card">
        <div class="card-title">Hőmérséklet határértékek</div>
        <div class="note">Ha egy <b>figyelt</b> szenzor értéke a beállított tartományon kívülre esik (és ez legalább ${num(cfg.safety.limit_trip_delay_s, 0)} mp-ig fennáll),
          üzem közben <b>vészleállás</b> történik hibakóddal (E201–E204). Leállított állapotban figyelmeztetés jelenik meg és az indítás tiltott, amíg az érték vissza nem tér a tartományba.
          A szenzorhibát (szakadás, rövidzár) a program mindig jelzi; a kapcsolóval beállítható, hogy vészleállást is okozzon-e (E211–E214).</div>
        <div class="tbl-wrap"><table class="tbl"><thead><tr><th>Szenzor</th><th>Név</th><th>Rövid név (ábra)</th><th>Most</th><th>Figyelés</th><th>Minimum</th><th>Maximum</th><th>Szenzorhiba → vészleállás</th></tr></thead>
          <tbody>${rows}</tbody></table></div>
      </div>
      <div class="card">
        <div class="card-title">Áramlásfigyelés és időzítések</div>
        <div class="form-grid">
          ${switchField(cfg, 'hardware.io.flow1.enabled', 'F1 áramláskapcsoló figyelése (belső kör)', 'Kikapcsolva nincs védelem az áramlás kimaradása ellen!')}
          ${switchField(cfg, 'hardware.io.flow2.enabled', 'F2 áramláskapcsoló figyelése (külső kör)')}
          ${numField(cfg, 'safety.miner_start_delay_s', 'Minerek indítása a szivattyúk után', 'mp', { min: 0, hint: 'Indításkor előbb a szivattyúk indulnak, ennyi idő múlva a minerek.' })}
          ${numField(cfg, 'safety.flow_start_timeout_s', 'Áramlás megindulásának időkorlátja', 'mp', { min: 2, hint: 'Ha ennyi idő alatt nem jelez áramlást a kapcsoló → vészleállás (E101/E103).' })}
          ${numField(cfg, 'safety.flow_loss_delay_s', 'Áramlás kimaradás tűrése üzem közben', 'mp', { min: 0.5, hint: 'Ennyi ideig tartó kimaradás után vészleállás (E102/E104).' })}
          ${numField(cfg, 'safety.post_stop_pump_s', 'Utókeringetés leállítás után', 'mp', { min: 0, hint: 'A minerek leállítása után ennyi ideig járnak még a szivattyúk.' })}
          ${numField(cfg, 'safety.limit_trip_delay_s', 'Hőfok határérték túllépés tűrése', 'mp', { min: 0 })}
          ${numField(cfg, 'safety.sensor_fault_delay_s', 'Szenzorhiba tűrése', 'mp', { min: 0 })}
          ${numField(cfg, 'safety.manual_hold_timeout_s', 'Kézi gomb kapcsolat-időtúllépés', 'mp', { min: 0.5, max: 10, hint: 'Ha a böngésző nem jelez (pl. megszakadt a kapcsolat), ennyi idő után leáll a kézi szivattyú.' })}
        </div>
      </div>
      <div class="card">
        <div class="card-title">Leállítás utáni túlmelegedés elleni védelem (utóhűtés)</div>
        <div class="note">Ha leállított állapotban bármelyik miner (vagy a minerek utáni T2 víz) eléri a küszöböt, a program egy időre visszakapcsolja a szivattyúkat,
          és újra leállítási parancsot küld a még dolgozó minereknek.</div>
        <div class="form-grid">
          ${switchField(cfg, 'safety.overheat_enabled', 'Utóhűtés engedélyezése')}
          ${numField(cfg, 'safety.overheat_temp_c', 'Bekapcsolási küszöb', '°C', { min: 30, max: 120 })}
          ${numField(cfg, 'safety.overheat_hysteresis_c', 'Hiszterézis', '°C', { min: 0.5, hint: 'A küszöb alá ennyivel kell hűlnie a kikapcsoláshoz.' })}
          ${numField(cfg, 'safety.overheat_min_run_s', 'Minimális futásidő', 'mp', { min: 5 })}
          ${switchField(cfg, 'safety.overheat_use_water_temp', 'A T2 vízhőfokot is figyelje', 'Akkor is működik, ha a miner leállás után nem jelent hőmérsékletet.')}
          ${switchField(cfg, 'safety.overheat_after_emergency', 'Vészleállás után is működjön')}
        </div>
      </div>
      <div class="card">
        <div class="card-title">Egyéb</div>
        <div class="form-grid">
          ${switchField(cfg, 'safety.auto_resume', 'Automatikus visszaállás áramszünet / újraindulás után', 'Ha a program üzem közben indult újra, a szivattyúk azonnal elindulnak és a rendszer visszaáll üzembe.')}
          ${switchField(cfg, 'hardware.io.btn_start.enabled', 'Fizikai INDÍTÓ gomb engedélyezése')}
          ${switchField(cfg, 'hardware.io.btn_stop.enabled', 'Fizikai LEÁLLÍTÓ gomb engedélyezése')}
          ${switchField(cfg, 'hardware.io.killswitch.enabled', 'KILLSWITCH bemenet figyelése (abszolút vészleállás)',
            'Aktív szintnél (alapból magas) azonnal leállnak a minerek ÉS a szivattyúk – utókeringetés, utóhűtés és kézi szivattyú nélkül. ' +
            'A rendszer csak a bemenet feloldása és nyugtázás után indítható újra. Láb és polaritás: Kalibráció & GPIO.')}
        </div>
      </div>
      ${actions('pr')}</div>`;
    $('#pr-save').addEventListener('click', () => this._save('pr', collect($('#protect'))));
    $('#pr-reset').addEventListener('click', () => this.open('protect'));
  }

  // --------------------------------------------------------- értesítések
  _notify(body) {
    const cfg = this.cfg;
    const nt = cfg.notifications;
    const rules = NOTIFY_CATS.map(([id, label, desc, critical]) => `<tr>
        <td><b>${esc(label)}</b>${critical ? ' <span class="pill bad">azonnal</span>' : ''}<div class="hint">${esc(desc)}</div></td>
        ${['email', 'sms'].map((ch) => `<td class="c"><label class="switch" title="${ch === 'email' ? 'Email' : 'SMS'}"><input type="checkbox"
          data-path="notifications.rules.${id}.${ch}" data-type="bool" ${nt.rules[id] && nt.rules[id][ch] ? 'checked' : ''}><i></i></label></td>`).join('')}</tr>`).join('');
    const gsmPorts = ['/dev/ttyUSB0', '/dev/ttyUSB1', '/dev/ttyUSB2', '/dev/ttyUSB3', '/dev/ttyACM0', '/dev/serial0', '/dev/ttyAMA0', '/dev/ttyS0'];
    body.innerHTML = `<div id="notify">
      <div class="card">
        <div class="card-title">Automatikus értesítések</div>
        <div class="form-grid">
          ${switchField(cfg, 'notifications.enabled', 'Értesítések bekapcsolva (főkapcsoló)', 'Kikapcsolva semmilyen email vagy SMS nem megy ki.')}
          <div class="field"><label>Csatornák (mentett állapot)</label><div class="nt-chips" id="nt-chips"></div></div>
        </div>
        <div class="subcard-title" style="margin-top:16px">Legutóbbi értesítések <span class="muted" id="nt-queue"></span></div>
        <div class="nt-history" id="nt-history"><div class="empty-note">Betöltés…</div></div>
      </div>

      <div class="card">
        <div class="card-title">Mikor küldjön értesítést?</div>
        <div class="note">A <b>KILLSWITCH</b> és a <b>vészleállás</b> üzenete azonnal és korlát nélkül megy ki. A többi eseményt a program a lent megadott
          ideig gyűjti, és egy üzenetben küldi el; ugyanazt a hibát 10 percen belül nem ismétli. Sikertelen küldésnél (pl. nincs internet) a program
          később újrapróbálja – az üzenet áramszünet után sem vész el. Az email tartalmazza a rendszer pillanatnyi állapotát is (hőfokok, szivattyúk, minerek).</div>
        <div class="tbl-wrap"><table class="tbl rules-tbl"><thead><tr><th>Esemény</th><th class="c">Email</th><th class="c">SMS</th></tr></thead>
          <tbody>${rules}</tbody></table></div>
        <div class="form-grid" style="margin-top:14px">
          ${numField(cfg, 'notifications.batch_s', 'Események összevonása', 'mp', { min: 0, max: 300, hint: 'Ennyi ideig gyűjti a nem sürgős eseményeket egy üzenetbe (0 = nincs összevonás).' })}
          ${textField(cfg, 'notifications.site_url', 'A webes felület címe az üzenetekben', 'Üresen automatikus (a Pi helyi IP címe). Távoli eléréshez (VPN, port továbbítás) írd be a külső címet.',
            { placeholder: 'http://192.168.1.50:8080/', mono: true, wide: true })}
        </div>
      </div>

      <div class="card">
        <div class="card-title">Miner termelés figyelése</div>
        <div class="note">Ha egy engedélyezett miner üzem közben <b>nem termel</b> – nem érhető el, nem bányászik, vagy a hashrate a névleges érték megadott
          százaléka alá esik –, a program <b>W406</b> (abbahagyta a termelést) vagy <b>W407</b> (az indítás után nem kezdett el termelni) hibakódot ad és értesít.
          Ez a rendszert <b>nem állítja le</b>. A szándékosan leállított minerre nem jelez.</div>
        <div class="form-grid">
          ${numField(cfg, 'miners.production_loss_delay_s', 'Kiesés tűrése', 'mp', { min: 10, max: 7200, hint: 'Ennél rövidebb kiesés (pl. pool váltás, újracsatlakozás) nem okoz riasztást.' })}
          ${numField(cfg, 'miners.production_start_timeout_s', 'Felfutási idő indítás után', 'mp', { min: 60, max: 7200, hint: 'Ennyi időn belül el kell kezdenie termelni (Antminer: hidegindításnál 5–10 perc is lehet).' })}
          ${numField(cfg, 'miners.production_min_pct', 'Termelés alsó határa', '%', { min: 0, max: 95, hint: 'A névleges hashrate ennyi %-a alatt nem számít termelésnek (0 = bármilyen hashrate elég).' })}
          ${switchField(cfg, 'notifications.notify_recovery', 'Értesítés, ha a miner ismét termel (I606)')}
        </div>
      </div>

      <div class="card" id="nt-email">
        <div class="card-title">Email (SMTP)</div>
        <div class="form-grid">
          ${switchField(cfg, 'notifications.email.enabled', 'Email értesítés bekapcsolva')}
          <div class="field"><label>Gyors beállítás</label><select class="inp" id="nt-preset">
            <option value="">– szolgáltató kiválasztása –</option>
            <option value="smtp.gmail.com|587|starttls">Gmail (smtp.gmail.com, 587, STARTTLS)</option>
            <option value="smtp.mail.yahoo.com|465|ssl">Yahoo (smtp.mail.yahoo.com, 465, SSL)</option>
          </select><small>Kitölti a szerver mezőket. Más szolgáltatónál az SMTP adatokat a szolgáltató súgója tartalmazza
            (a Microsoft/Outlook fiókok jelszavas SMTP belépést általában már nem engednek).</small></div>
          ${textField(cfg, 'notifications.email.smtp_host', 'SMTP szerver', '', { placeholder: 'smtp.gmail.com', mono: true })}
          ${numField(cfg, 'notifications.email.smtp_port', 'Port', '', { step: 1, min: 1, max: 65535 })}
          ${selectField(cfg, 'notifications.email.security', 'Titkosítás', [['starttls', 'STARTTLS (587)'], ['ssl', 'SSL/TLS (465)'], ['none', 'Nincs titkosítás (25)']])}
          ${textField(cfg, 'notifications.email.username', 'Felhasználónév', 'Általában a teljes email cím.', { placeholder: 'farm.riasztas@gmail.com' })}
          ${secretField(cfg, 'notifications.email.password', 'Jelszó', 'Gmailnél <b>alkalmazásjelszó</b> kell (Google fiók › Biztonság › Kétlépcsős azonosítás › Alkalmazásjelszavak), a normál jelszó nem működik.')}
          ${textField(cfg, 'notifications.email.sender', 'Feladó címe', 'Üresen a felhasználónév.', { placeholder: '(a felhasználónév)' })}
          ${numField(cfg, 'notifications.email.max_per_hour', 'Óránként legfeljebb', 'db', { step: 1, min: 1, max: 1000, hint: 'A KILLSWITCH és vészleállás üzenetekre nem vonatkozik.' })}
          ${areaField(cfg, 'notifications.email.recipients', 'Címzettek', 'Több cím vesszővel vagy soronként.', { placeholder: 'tulajdonos@example.com, karbantarto@example.com' })}
        </div>
        <div class="form-actions"><span class="status-msg">A teszt a mezőkben lévő (még nem mentett) adatokkal küld.</span>
          <button class="btn" id="nt-email-test">Teszt email küldése</button></div>
        <div id="nt-email-out"></div>
      </div>

      <div class="card" id="nt-sms">
        <div class="card-title">SMS</div>
        <div class="form-grid">
          ${switchField(cfg, 'notifications.sms.enabled', 'SMS értesítés bekapcsolva')}
          ${selectField(cfg, 'notifications.sms.provider', 'SMS küldés módja', [['gsm', 'GSM modem (saját SIM kártya)'], ['twilio', 'Twilio (internetes szolgáltatás)'], ['http', 'HTTP SMS átjáró (egyéni API)']])}
          ${numField(cfg, 'notifications.sms.max_per_hour', 'Óránként legfeljebb', 'db', { step: 1, min: 1, max: 500, hint: 'A KILLSWITCH és vészleállás üzenetekre nem vonatkozik.' })}
          ${switchField(cfg, 'notifications.sms.ascii', 'Ékezetek nélkül (ajánlott)', 'Ékezettel egy SMS csak 70 karakteres lehet. GSM modemnél mindig ékezet nélkül megy (160 karakter).')}
          ${areaField(cfg, 'notifications.sms.recipients', 'Telefonszámok', 'Nemzetközi formátumban, több szám vesszővel vagy soronként.', { placeholder: '+36301234567, +36201234567' })}
        </div>
        <div data-prov="gsm">
          <div class="subcard-title" style="margin-top:16px">GSM modem</div>
          <div class="form-grid">
            ${textField(cfg, 'notifications.sms.gsm_port', 'Soros port', 'USB modem: /dev/ttyUSB0 (SIM7600: gyakran /dev/ttyUSB2). GPIO-ra kötött modul: /dev/serial0.', { mono: true, list: 'nt-ports' })}
            ${selectField(cfg, 'notifications.sms.gsm_baud', 'Sebesség', [9600, 19200, 38400, 57600, 115200].map((b) => [b, b + ' baud']), 'int')}
            ${secretField(cfg, 'notifications.sms.gsm_pin', 'SIM PIN kód', 'Csak ha a SIM kártya PIN kódot kér (ajánlott a PIN kérést egy telefonban kikapcsolni).', { placeholder: 'nincs' })}
          </div>
          <datalist id="nt-ports">${gsmPorts.map((p) => `<option value="${p}">`).join('')}</datalist>
          <div class="note" style="margin-top:12px">Bármely AT parancsos GSM modem jó (SIM800C/SIM800L, SIM7600, Huawei „stick” módban stb.). <b>USB-s modemet</b> elég bedugni.
            <b>GPIO-ra kötött modulnál</b> (UART): modul TX → GPIO15 / RXD (10. tű), modul RX → GPIO14 / TXD (8. tű), GND → GND; a modul kapjon külön, legalább 2 A-es tápot
            (ne a Pi 3,3 V-járól), és 3,3 V-os jelszintet. Ilyenkor a Pi-n kapcsold ki a soros konzolt: <span class="mono">sudo raspi-config</span> › Interface Options › Serial Port ›
            login shell: <b>No</b>, serial hardware: <b>Yes</b>, majd újraindítás. A GPIO14/15 lábat ekkor a program más funkcióra nem engedi kiosztani.</div>
        </div>
        <div data-prov="twilio">
          <div class="subcard-title" style="margin-top:16px">Twilio</div>
          <div class="form-grid">
            ${textField(cfg, 'notifications.sms.twilio_sid', 'Account SID', '', { placeholder: 'AC…', mono: true })}
            ${secretField(cfg, 'notifications.sms.twilio_token', 'Auth token')}
            ${textField(cfg, 'notifications.sms.twilio_from', 'Feladó szám (Twilio szám)', '', { placeholder: '+15005550006', mono: true })}
          </div>
          <div class="note" style="margin-top:12px">A twilio.com konzolon található az Account SID és az Auth token. Internetkapcsolat szükséges – áramszünetnél vagy
            internet kiesésnél a GSM modem megbízhatóbb.</div>
        </div>
        <div data-prov="http">
          <div class="subcard-title" style="margin-top:16px">HTTP SMS átjáró</div>
          <div class="form-grid">
            ${selectField(cfg, 'notifications.sms.http_method', 'Metódus', [['GET', 'GET'], ['POST', 'POST']])}
            ${selectField(cfg, 'notifications.sms.http_content_type', 'Törzs formátuma (POST)', [['application/json', 'JSON'], ['application/x-www-form-urlencoded', 'Űrlap (urlencoded)'], ['text/plain', 'Szöveg']])}
            ${textField(cfg, 'notifications.sms.http_url', 'URL', '', { placeholder: 'https://api.example.com/sms?to={to}&text={message}', mono: true, wide: true })}
            ${areaField(cfg, 'notifications.sms.http_body', 'Törzs (POST)', '', { placeholder: '{"to": "{to}", "text": "{message}"}', mono: true })}
            ${secretField(cfg, 'notifications.sms.http_headers', 'Fejlécek (soronként: Név: érték)', 'Pl. API kulcs: <span class="mono">Authorization: Bearer …</span> – titkosként kezelve.', { textarea: true, placeholder: 'X-Api-Key: …' })}
          </div>
          <div class="note" style="margin-top:12px">Helyettesítők: <span class="mono">{message}</span> az üzenet, <span class="mono">{to}</span> a telefonszám (+36…),
            <span class="mono">{to_plain}</span> a szám csak számjegyekkel, <span class="mono">{site}</span> a telephely neve. Ha az URL-ben vagy a törzsben van
            <span class="mono">{to}</span>, minden címzettnek külön kérés megy; ha nincs, egyetlen kérés megy, és telefonszám sem kell.
            Így bármely SMS szolgáltató (vagy pl. Telegram bot) bekötható:
            <span class="mono">https://api.telegram.org/bot&lt;TOKEN&gt;/sendMessage?chat_id=&lt;ID&gt;&amp;text={message}</span></div>
        </div>
        <div class="form-actions"><span class="status-msg">A teszt a mezőkben lévő (még nem mentett) adatokkal küld.</span>
          <button class="btn" id="nt-sms-test">Teszt SMS küldése</button></div>
        <div id="nt-sms-out"></div>
      </div>
      ${actions('nt')}</div>`;
    const root = $('#notify');
    const provSel = root.querySelector('[data-path="notifications.sms.provider"]');
    const methodSel = root.querySelector('[data-path="notifications.sms.http_method"]');
    const toggle = () => {
      for (const el of $$('[data-prov]', root)) el.hidden = el.dataset.prov !== provSel.value;
      const post = methodSel.value === 'POST';
      for (const p of ['notifications.sms.http_body', 'notifications.sms.http_content_type']) {
        root.querySelector(`[data-path="${p}"]`).closest('.field').hidden = !post;
      }
    };
    provSel.addEventListener('change', toggle);
    methodSel.addEventListener('change', toggle);
    toggle();
    $('#nt-preset').addEventListener('change', (e) => {
      if (!e.target.value) return;
      const [host, port, sec] = e.target.value.split('|');
      root.querySelector('[data-path="notifications.email.smtp_host"]').value = host;
      root.querySelector('[data-path="notifications.email.smtp_port"]').value = port;
      root.querySelector('[data-path="notifications.email.security"]').value = sec;
    });
    root.addEventListener('click', (e) => {
      const b = e.target.closest('[data-clear]');
      if (!b) return;
      const inp = root.querySelector(`[data-path="${b.dataset.clear}"]`);
      const on = !inp.dataset.cleared;
      if (on) inp.dataset.cleared = '1'; else delete inp.dataset.cleared;
      inp.value = '';
      inp.placeholder = on ? '(mentéskor törlődik)' : inp.dataset.ph;
      b.textContent = on ? 'visszavonás' : 'törlés';
    });
    for (const ch of ['email', 'sms']) $(`#nt-${ch}-test`).addEventListener('click', () => this._notifyTest(ch, root));
    $('#nt-save').addEventListener('click', () => this._save('nt', collect(root), () => this.open('notify')));
    $('#nt-reset').addEventListener('click', () => this.open('notify'));
    this._notifyChips();
    const poll = async () => {
      try {
        this._renderNotifyHistory(await api('/api/notify', { quiet: true }));
      } catch (e) { /* átmeneti hiba */ }
    };
    poll();
    this.hwTimer = setInterval(poll, 4000);
  }

  _notifyChips() {
    const nt = this.cfg.notifications;
    const box = $('#nt-chips');
    if (!box) return;
    const prov = { gsm: 'GSM modem', twilio: 'Twilio', http: 'HTTP átjáró' }[nt.sms.provider] || nt.sms.provider;
    const count = (text, re) => (text || '').split(re).filter((x) => x.trim()).length;
    const chip = (on, label, detail) => `<span class="pill ${!nt.enabled ? 'off' : on ? 'on' : 'off'}">${label}: ${on ? 'BE' : 'KI'}</span>${on && detail ? ` <span class="hint">${esc(detail)}</span>` : ''}`;
    box.innerHTML = `<div>${chip(nt.email.enabled, 'Email', count(nt.email.recipients, /[,;\s]+/) + ' címzett')}</div>
      <div>${chip(nt.sms.enabled, 'SMS', prov + ', ' + count(nt.sms.recipients, /[,;\n]+/) + ' telefonszám')}</div>
      ${nt.enabled ? '' : '<div><span class="pill bad">A főkapcsoló ki van kapcsolva</span></div>'}`;
  }

  _renderNotifyHistory(st) {
    const box = $('#nt-history');
    if (!box || this.sub !== 'notify') return;
    const q = st.queue || [];
    const nextIn = q.length ? Math.min(...q.map((m) => m.next_in)) : 0;
    $('#nt-queue').textContent = q.length ? `· ${q.length} üzenet vár küldésre${nextIn ? ` (következő próbálkozás ${nextIn} mp múlva)` : ''}` : '';
    const hist = (st.history || []).slice(0, 12);
    if (!hist.length) {
      box.innerHTML = '<div class="empty-note">Még nem ment ki értesítés. A beállítások után küldj egy tesztet!</div>';
      return;
    }
    box.innerHTML = hist.map((h) => {
      const state = h.ok ? '<span class="pill on">elküldve</span>' : h.final ? '<span class="pill bad">sikertelen</span>' : `<span class="pill warn">újrapróbálja (${h.attempt}.)</span>`;
      return `<div class="nh"><span class="nh-time">${dateTime(h.ts)}</span>
        <span class="nh-tags"><span class="pill">${h.channel === 'email' ? 'Email' : 'SMS'}</span>${state}${h.test ? '<span class="pill">teszt</span>' : ''}</span>
        <div class="nh-main">${esc(h.summary || '')}<div class="nh-detail">${esc(h.detail || '')}</div></div></div>`;
    }).join('');
  }

  async _notifyTest(channel, root) {
    const btn = $(`#nt-${channel}-test`);
    const out = $(`#nt-${channel}-out`);
    const gsm = channel === 'sms' && root.querySelector('[data-path="notifications.sms.provider"]').value === 'gsm';
    btn.disabled = true;
    out.innerHTML = `<div class="note" style="margin-top:12px">Küldés folyamatban…${gsm ? ' (GSM modemmel akár 30–60 mp)' : ''}</div>`;
    try {
      const r = await api('/api/notify/test', { method: 'POST', body: { channel, settings: collect(root).notifications } });
      out.innerHTML = `<div class="note ok" style="margin-top:12px">✓ ${esc(r.message)}${channel === 'email' ? ' – nézd meg a levélszemét mappát is.' : ''}</div>`;
    } catch (e) {
      out.innerHTML = `<div class="errors-box">✗ ${esc(e.message)}</div>`;
    } finally {
      btn.disabled = false;
      api('/api/notify', { quiet: true }).then((st) => this._renderNotifyHistory(st)).catch(() => {});
    }
  }

  // ------------------------------------------------- kalibráció & GPIO
  _calib(body) {
    const cfg = this.cfg;
    const adcTypes = this.meta.adc_types;
    const channels = (adcTypes.find((a) => a.id === cfg.hardware.adc.type) || { channels: 8 }).channels;
    const g2p = this.meta.gpio_to_pin;
    const gpioOptions = (cur) => Object.keys(g2p).map(Number).sort((a, b) => a - b).map((g) => {
      const bus = this.busPins[String(g)];
      return `<option value="${g}" ${g === cur ? 'selected' : ''}>GPIO${g} (tű ${g2p[g]})${bus ? ' – ' + esc(bus) : ''}</option>`;
    }).join('');
    const sensorRows = SENSORS.map((sid) => {
      const s = cfg.sensors[sid];
      return `<tr>
        <td><b>${sid.toUpperCase()}</b><div class="hint">${esc(s.short)}</div></td>
        <td><select class="inp sm" data-path="sensors.${sid}.channel" data-type="int">${Array.from({ length: channels }, (_, c) => `<option value="${c}" ${c === s.channel ? 'selected' : ''}>${{ ads1115: 'A', ads1220: 'AIN' }[cfg.hardware.adc.type] || 'CH'}${c}</option>`).join('')}</select></td>
        <td><input class="inp sm num" type="number" step="1" data-path="sensors.${sid}.r25" data-type="num" value="${s.r25}"></td>
        <td><input class="inp sm num" type="number" step="1" data-path="sensors.${sid}.beta" data-type="num" value="${s.beta}"></td>
        <td><input class="inp sm num" type="number" step="0.05" data-path="sensors.${sid}.offset" data-type="num" value="${s.offset}"></td>
        <td><input class="inp sm num" type="number" step="0.001" data-path="sensors.${sid}.gain" data-type="num" value="${s.gain}"></td>
        <td class="live" data-hw="${sid}.voltage">—</td><td class="live" data-hw="${sid}.ohm">—</td>
        <td class="live" data-hw="${sid}.raw">—</td><td class="live" data-hw="${sid}.value">—</td>
        <td><div style="display:flex;gap:6px"><input class="inp sm num" type="number" step="0.1" placeholder="°C" data-ref="${sid}" style="width:80px">
          <button class="btn btn-sm" data-calib="${sid}">Beállít</button></div></td></tr>`;
    }).join('');
    const ioRows = this.meta.io_outputs.concat(this.meta.io_inputs).map((name) => {
      const it = cfg.hardware.io[name];
      const out = this.meta.io_outputs.includes(name);
      return `<tr>
        <td><b>${esc(this.meta.io_labels[name])}</b></td>
        <td>${out ? '<span class="pill">kimenet</span>' : '<span class="pill">bemenet</span>'}</td>
        <td><select class="inp sm" data-path="hardware.io.${name}.gpio" data-type="int" data-gpio="${name}">${gpioOptions(it.gpio)}</select></td>
        <td><select class="inp sm" data-path="hardware.io.${name}.active" data-type="text">
          <option value="high" ${it.active === 'high' ? 'selected' : ''}>Magas = aktív (normál)</option>
          <option value="low" ${it.active === 'low' ? 'selected' : ''}>Alacsony = aktív (negált)</option></select></td>
        <td>${out ? '—' : `<select class="inp sm" data-path="hardware.io.${name}.pull" data-type="text">
          <option value="up" ${it.pull === 'up' ? 'selected' : ''}>Felhúzó (pull-up)</option>
          <option value="down" ${it.pull === 'down' ? 'selected' : ''}>Lehúzó (pull-down)</option>
          <option value="off" ${it.pull === 'off' ? 'selected' : ''}>Nincs</option></select>`}</td>
        <td class="live" data-pin="${name}">—</td>
        <td>${out ? `<button class="btn btn-sm" data-test="${name}" title="A kimenet 2 másodpercre bekapcsol (csak leállított rendszernél)">Teszt 2 mp</button>` : ''}</td></tr>`;
    }).join('');
    body.innerHTML = `${this._modeCard('cb')}<div id="calib">
      <div class="note"><b>Fontos:</b> a Raspberry Pi-nek <b>nincs analóg bemenete</b>, a 10 kΩ-os NTC osztókat az <b>ADS1220</b> (SPI, 24 bit) méri:
        SCLK→GPIO11, DIN/MOSI→GPIO10, DOUT/MISO→GPIO9, CS→GPIO8 (CE0), <b>DVDD→3,3 V</b>, DGND/AGND→GND, CLK→GND (belső órajel), DRDY szabadon hagyható.
        Az <b>AVDD</b> lábat ugyanarról a tápról kell táplálni, mint a termisztoros osztókat (3,3 V vagy 5 V) – „AVDD” referenciával a mérés
        arányos, így a táp pontos értéke nem számít. A DVDD (logikai szint) mindig 3,3 V legyen, mert a Pi lábai nem 5 V-tűrők!</div>
      <div class="card">
        <div class="card-title">ADC és feszültségosztó <span class="muted" data-hw="adc-status"></span></div>
        <div class="form-grid">
          ${selectField(cfg, 'hardware.adc.type', 'ADC típus', adcTypes.map((a) => [a.id, a.label]))}
          ${selectField(cfg, 'hardware.adc.i2c_bus', 'I²C busz (ADS1115)', [[1, '1 (GPIO2/GPIO3)'], [0, '0']], 'int')}
          ${selectField(cfg, 'hardware.adc.i2c_address', 'I²C cím (ADS1115)', [[72, '0x48 (ADDR→GND)'], [73, '0x49 (ADDR→VDD)'], [74, '0x4A (ADDR→SDA)'], [75, '0x4B (ADDR→SCL)']], 'int')}
          ${selectField(cfg, 'hardware.adc.spi_bus', 'SPI busz', [[0, 'SPI0'], [1, 'SPI1']], 'int')}
          ${selectField(cfg, 'hardware.adc.spi_device', 'SPI chip select (CS)', [[0, 'CE0'], [1, 'CE1'], [2, 'CE2 (csak SPI1)']], 'int')}
          ${selectField(cfg, 'hardware.adc.ref', 'Referencia (ADS1220)', [['avdd', 'AVDD – arányos mérés (ajánlott)'], ['internal', 'Belső 2,048 V'], ['external', 'Külső (REFP0/REFN0)']], 'text',
            'AVDD esetén az osztót ugyanarról a tápról kell táplálni, mint az ADS1220 AVDD lábát.')}
          ${numField(cfg, 'hardware.adc.vref', 'Referencia feszültség', 'V', { step: 0.01, hint: 'MCP3x08: a VREF láb feszültsége; ADS1220 külső referenciánál: REFP0−REFN0.' })}
          ${numField(cfg, 'hardware.adc.samples', 'Minták száma mérésenként', 'db', { step: 1, min: 1, max: 32, hint: 'Medián szűrés a zaj ellen.' })}
          ${numField(cfg, 'hardware.divider.supply_v', 'Osztó tápfeszültsége', 'V', { step: 0.01, hint: 'A fix ellenállás felső végén mért feszültség (pl. 3,30 vagy 5,00 V).' })}
          ${numField(cfg, 'hardware.divider.series_ohm', 'Fix ellenállás', 'Ω', { step: 1, hint: 'A táp és a mérőpont közötti ellenállás (10 000 Ω).' })}
        </div>
      </div>
      <div class="card">
        <div class="card-title">Hőmérő szenzorok – kalibráció</div>
        <div class="note">Kalibrálás: tedd a szenzort ismert hőmérsékletű vízbe (vagy mérd meg mellette egy pontos hőmérővel), írd be a valós értéket a
          „Referencia” mezőbe és nyomd meg a „Beállít” gombot – a program kiszámolja az eltolást. A szorzó a meredekséget korrigálja (két pontos kalibrációhoz).
          A béta érték a termisztor adatlapjáról (gyakori: 3950 vagy 3435).</div>
        <div class="tbl-wrap"><table class="tbl"><thead><tr><th>Szenzor</th><th>ADC csatorna</th><th>R25 (Ω)</th><th>Béta</th><th>Eltolás (°C)</th><th>Szorzó</th>
          <th>Feszültség</th><th>Ellenállás</th><th>Nyers</th><th>Kalibrált</th><th>Referencia</th></tr></thead><tbody>${sensorRows}</tbody></table></div>
      </div>
      <div class="card">
        <div class="card-title">Digitális ki- és bemenetek (GPIO, BCM számozás)</div>
        <div class="note warn"><b>Figyelem:</b> mentés után a program azonnal az új lábakat használja. Csak akkor módosítsd, ha a vezetékezés is ennek megfelelő –
          egy nyomógombra vagy kapcsolóra kötött lábat kimenetként meghajtani károsíthatja a Raspberry Pi-t!
          Az SSR-ek 5 V-os vezérlésénél a Pi 3,3 V-os kimenete nem mindig elég: „alacsony szintre aktív” relémodulnál tranzisztoros illesztő javasolt.</div>
        <div class="note"><b>Aktív szint = szoftveres negálás.</b> Kimenetnél azt adja meg, milyen szintet kap a láb BE (szivattyú jár) állapotban:
          „Magas” → 3,3 V, „Alacsony (negált)” → 0 V. Bemenetnél azt, hogy milyen szint számít aktívnak (gomb lenyomva / víz áramlik):
          „Alacsony (negált)” → a 0 V (GND) jelent aktív állapotot. Az „Élő állapot” oszlop mutatja a logikai állapotot és a láb tényleges
          feszültségszintjét is, így bekötés után azonnal ellenőrizhető. A polaritás mentéskor azonnal, a láb elengedése nélkül vált
          (járó szivattyúnál sincs kiesés); egy nyomógomb átállítása nem számít gombnyomásnak.</div>
        <div class="tbl-wrap"><table class="tbl"><thead><tr><th>Funkció</th><th>Irány</th><th>GPIO port</th><th>Aktív szint (negálás)</th><th>Belső ellenállás</th><th>Élő állapot (a lábról visszaolvasva)</th><th></th></tr></thead>
          <tbody>${ioRows}</tbody></table></div>
        <div class="subcard-title" style="margin-top:16px">40 tűs csatlakozó (felülnézet, 1-es tű bal felső)</div>
        <div class="header-wrap"><div class="pinhdr" id="pinhdr"></div></div>
        <details class="diag"><summary>Hardver diagnosztika</summary><dl class="kv" data-hw="diag"></dl>
          <p class="hint">Ellenőrzés a Pi parancssorából (a láb tényleges állapota): <span class="mono">pinctrl get 17</span> vagy régebbi rendszeren
            <span class="mono">raspi-gpio get 17</span>. Részletes diagnosztika: <span class="mono">python3 -m rig_supervisor --diagnose</span></p></details>
      </div>
      ${actions('cb')}</div>`;
    const root = $('#calib');
    const adcFields = { 'hardware.adc.i2c_bus': 'i2c', 'hardware.adc.i2c_address': 'i2c', 'hardware.adc.spi_bus': 'spi',
      'hardware.adc.spi_device': 'spi', 'hardware.adc.vref': 'vref', 'hardware.adc.ref': 'ads1220' };
    const toggleAdc = () => {
      const type = root.querySelector('[data-path="hardware.adc.type"]').value;
      const bus = type === 'ads1115' ? 'i2c' : ['mcp3008', 'mcp3208', 'ads1220'].includes(type) ? 'spi' : 'none';
      const refEl = root.querySelector('[data-path="hardware.adc.ref"]');
      const show = {
        i2c: bus === 'i2c', spi: bus === 'spi', ads1220: type === 'ads1220',
        vref: type === 'mcp3008' || type === 'mcp3208' || (type === 'ads1220' && refEl && refEl.value === 'external'),
      };
      for (const [path, kind] of Object.entries(adcFields)) {
        const el = root.querySelector(`[data-path="${path}"]`);
        if (el) el.closest('.field').hidden = !show[kind];
      }
    };
    const redrawHeader = () => this._drawHeader(collect(root));
    root.addEventListener('change', (e) => {
      if (!e.target.dataset.path) return;
      if (e.target.dataset.path === 'hardware.adc.type' || e.target.dataset.path === 'hardware.adc.ref') toggleAdc();
      redrawHeader();
    });
    toggleAdc();
    redrawHeader();
    this._bindModeCard('cb');
    root.addEventListener('click', (e) => {
      const b = e.target.closest('[data-test]');
      if (b) this._testOutput(b.dataset.test, b);
    });
    $('#cb-save').addEventListener('click', () => this._save('cb', collect(root), () => this.open('calib')));
    $('#cb-reset').addEventListener('click', () => this.open('calib'));
    root.addEventListener('click', async (e) => {
      const b = e.target.closest('[data-calib]');
      if (!b) return;
      const sid = b.dataset.calib;
      const ref = parseFloat((root.querySelector(`[data-ref="${sid}"]`).value || '').replace(',', '.'));
      if (!isFinite(ref)) { toast('Írd be a referencia hőmérsékletet', 'warn'); return; }
      try {
        const r = await api('/api/calibrate', { method: 'POST', body: { sensor: sid, reference: ref } });
        toast(`${sid.toUpperCase()} kalibrálva – új eltolás: ${num(r.offset, 2)} °C`, 'ok');
        await this.reload();
        this.open('calib');
      } catch (err) {
        toast(err.message, 'err', 6000);
      }
    });
    const poll = async () => {
      try {
        this.hw = await api('/api/hw');
        this._renderHw();
      } catch (e) { /* átmeneti hiba */ }
    };
    poll();
    this.hwTimer = setInterval(poll, 1000);
  }

  _renderHw() {
    const hw = this.hw;
    if (!hw || this.sub !== 'calib') return;
    const set = (key, html) => { const el = document.querySelector(`[data-hw="${key}"]`); if (el) el.innerHTML = html; };
    for (const sid of SENSORS) {
      const r = hw.sensors[sid] || {};
      set(sid + '.voltage', r.voltage === null || r.voltage === undefined ? '—' : num(r.voltage, 3) + ' V');
      set(sid + '.ohm', r.ohm === null || r.ohm === undefined ? '—' : !isFinite(r.ohm) ? '∞' : num(r.ohm / 1000, 2) + ' kΩ');
      set(sid + '.raw', r.raw_value === null || r.raw_value === undefined ? '—' : temp(r.raw_value, 2));
      set(sid + '.value', r.fault ? `<span class="pill warn" title="${esc(r.fault_text || '')}">${esc(FAULT_LABELS[r.fault] || r.fault)}</span>` : `<b>${temp(r.value, 2)}</b>`);
    }
    set('adc-status', hw.hardware.adc_ok ? `– ${esc(hw.hardware.adc)}: működik` : `– <span style="color:#fca5a5">${esc(hw.hardware.adc_error || 'hiba')}</span>`);
    const simulated = hw.mode && hw.mode.simulated;
    const lvl = (v) => (v === null || v === undefined ? '?' : v ? '1 (3,3 V)' : '0 (GND)');
    for (const [name, p] of Object.entries(hw.pins)) {
      const el = document.querySelector(`[data-pin="${name}"]`);
      if (!el) continue;
      let html;
      if (p.error) {
        html = `<span class="pill bad" title="${esc(p.error)}">HIBA</span> <span class="hint">${esc(p.error)}</span>`;
      } else if (p.direction === 'out') {
        // a kimenet lábáról visszaolvasott szint – nem csak az, amit a program kiadott
        const shown = p.actual === null || p.actual === undefined ? p.level : p.actual;
        html = `<span class="pill ${p.active ? 'on' : 'off'}">${p.active ? 'AKTÍV' : 'inaktív'}</span> <span class="hint">láb: ${lvl(shown)}</span>`;
        if (p.mismatch) html += ` <span class="pill bad" title="A program ${lvl(p.level)} szintet ad ki, de a lábon ${lvl(p.actual)} mérhető">ELTÉRÉS!</span>`;
      } else {
        html = `<span class="pill ${p.active ? 'on' : 'off'}">${p.active ? 'AKTÍV' : 'inaktív'}</span> <span class="hint">láb: ${lvl(p.level)}</span>`;
      }
      if (simulated) html += ' <span class="pill warn" title="Szimulációs mód: a valódi láb nem változik">szimulált</span>';
      el.innerHTML = html;
    }
    const manualOk = this.store.system && this.store.system.system.manual_allowed;
    for (const b of document.querySelectorAll('[data-test]')) if (!b.dataset.busy) b.disabled = !manualOk;
    if (hw.mode) this.mode = hw.mode;
    set('mode-status', this._modeStatusHtml());
    const d = hw.diagnostics;
    if (d) {
      const h = hw.hardware;
      const errs = [...new Set([...(h.gpio_errors || []), ...Object.entries(h.pin_errors || {}).map(([k, v]) => k + ': ' + v)])];
      set('diag', `<dt>Gép</dt><dd>${esc(d.model || 'nem Raspberry Pi (vagy nem felismerhető)')}</dd>
        <dt>Hardver mód</dt><dd>${simulated ? 'szimuláció' : 'valódi hardver'} – ${esc((hw.mode || {}).reason || '')}</dd>
        <dt>GPIO könyvtár</dt><dd>${esc(h.gpio_backend || '—')} ${h.gpio_ok ? '<span class="pill on">OK</span>' : '<span class="pill bad">HIBA</span>'}</dd>
        ${errs.length ? `<dt>Hibák</dt><dd>${errs.map(esc).join('<br>')}</dd>` : ''}
        <dt>Futtató felhasználó</dt><dd>${esc(d.user)} <span class="hint">csoportok: ${esc((d.groups || []).join(', ') || '—')}</span></dd>
        <dt>Eszközfájlok</dt><dd>${Object.entries(d.devices || {}).map(([k, v]) => `<span class="mono">${esc(k)}</span>: ${esc(v)}`).join(' · ')}</dd>
        <dt>Python</dt><dd>${esc(d.python)}</dd>`);
    }
  }

  async _testOutput(name, btn) {
    if (btn.dataset.busy) return;
    const send = (action) => api('/api/control/manual', { method: 'POST', body: { pump: name, action } });
    btn.dataset.busy = '1';
    btn.disabled = true;
    const label = btn.textContent;
    btn.textContent = 'BE…';
    let timer = null;
    try {
      await send('press');
      timer = setInterval(() => send('hold').catch(() => {}), 300);
      await new Promise((r) => setTimeout(r, 2000));
    } catch (e) {
      toast(e.message, 'warn', 6000);
    } finally {
      clearInterval(timer);
      send('release').catch(() => {});
      btn.textContent = label;
      delete btn.dataset.busy;
      btn.disabled = false;
    }
  }

  _drawHeader(values) {
    const io = (values.hardware && values.hardware.io) || {};
    const used = {};
    for (const [name, it] of Object.entries(io)) if (it && it.gpio !== undefined) used['GPIO' + it.gpio] = name;
    const adc = (values.hardware && values.hardware.adc) || {};
    const busByGpio = {};
    if (adc.type === 'ads1115' && +adc.i2c_bus === 1) { busByGpio.GPIO2 = 'SDA'; busByGpio.GPIO3 = 'SCL'; }
    if (['mcp3008', 'mcp3208', 'ads1220'].includes(adc.type)) {
      if (+adc.spi_bus === 0) {
        Object.assign(busByGpio, { GPIO9: 'MISO', GPIO10: 'MOSI', GPIO11: 'SCLK' });
        busByGpio[+adc.spi_device === 1 ? 'GPIO7' : 'GPIO8'] = 'CE' + (+adc.spi_device === 1 ? 1 : 0);
      } else {
        Object.assign(busByGpio, { GPIO19: 'MISO', GPIO20: 'MOSI', GPIO21: 'SCLK' });
        busByGpio[{ 0: 'GPIO18', 1: 'GPIO17', 2: 'GPIO16' }[+adc.spi_device] || 'GPIO18'] = 'CE' + (+adc.spi_device || 0);
      }
    }
    const cell = (pin) => {
      const name = HEADER[pin];
      let cls = 'pin';
      let fn = '';
      if (name === '5V') cls += ' pw5';
      else if (name === '3V3') cls += ' pw3';
      else if (name === 'GND') cls += ' gnd';
      if (used[name]) { cls += ' used' + (['pump1', 'pump2'].includes(used[name]) ? ' out' : ''); fn = IO_SHORT[used[name]]; }
      else if (busByGpio[name]) { cls += ' bus'; fn = 'ADC ' + busByGpio[name]; }
      return `<div class="${cls}" style="grid-column:${Math.ceil(pin / 2)};grid-row:${pin % 2 ? 1 : 2}" title="${pin}. tű – ${name}${fn ? ' – ' + fn : ''}"><b>${pin}</b>${name}${fn ? `<span class="fn">${esc(fn)}</span>` : ''}</div>`;
    };
    $('#pinhdr').innerHTML = Array.from({ length: 40 }, (_, i) => cell(i + 1)).join('');
  }

  // ----------------------------------------------------------- rendszer
  _system(body) {
    const cfg = this.cfg;
    body.innerHTML = `
      <div class="card" id="sys-gen">
        <div class="card-title">Általános</div>
        <div class="form-grid">${textField(cfg, 'site_name', 'Telephely neve (fejlécben)')}</div>
        ${actions('sg')}
      </div>
      <div class="card" id="sys-log">
        <div class="card-title">Adatnaplózás</div>
        <div class="form-grid">
          ${numField(cfg, 'logging.interval_s', 'Mintavételi időköz', 'mp', { min: 1, max: 3600, hint: 'Ilyen gyakran kerül minden mért érték a naplóba.' })}
          ${numField(cfg, 'logging.max_size_gb', 'Napló maximális mérete', 'GB', { step: 0.1, hint: 'Ha a naplófájlok együttes mérete meghaladja, a legrégebbi napi fájl törlődik.' })}
          ${numField(cfg, 'logging.min_free_mb', 'Minimális szabad hely az SD kártyán', 'MB', { step: 10 })}
        </div>
        <div id="log-info" style="margin-top:14px"></div>
        ${actions('sl')}
      </div>
      <div class="card" id="sys-auth">
        <div class="card-title">Webes hozzáférés</div>
        <div class="note">Bekapcsolva a felület csak felhasználónévvel és jelszóval érhető el (a böngésző kéri be). Elfelejtett jelszó esetén a Pi-n:
          <span class="mono">sudo rig-supervisor-reset-password</span></div>
        <div class="form-grid">
          <div class="field"><label class="switch"><input type="checkbox" id="auth-on" ${cfg.web.auth_enabled ? 'checked' : ''}><i></i>Jelszavas védelem</label></div>
          <div class="field"><label>Felhasználónév</label><input class="inp" id="auth-user" value="${esc(cfg.web.username)}" autocomplete="off"></div>
          <div class="field"><label>Új jelszó</label><input class="inp" id="auth-pw" type="password" autocomplete="new-password" placeholder="${cfg.web.has_password ? '(változatlan)' : ''}"></div>
          <div class="field"><label>Új jelszó még egyszer</label><input class="inp" id="auth-pw2" type="password" autocomplete="new-password"></div>
        </div>
        <div class="form-actions"><button class="btn btn-primary" id="auth-save">Mentés</button></div>
      </div>
      <div class="card">
        <div class="card-title">Hardver</div>
        <dl class="kv" id="hw-kv"></dl>
      </div>
      ${this._modeCard('sy')}
      <div class="card" id="sim-card" ${this.sim ? '' : 'hidden'}>
        <div class="card-title">Szimuláció – hibák kipróbálása</div>
        <div class="note">A szimulációs módban a védelmi funkciók veszélytelenül kipróbálhatók: áramlás kimaradás, szenzorhiba, le nem álló miner (utóhűtés), fizikai gombok.</div>
        <div id="sim-body"></div>
      </div>
      <div class="card"><div class="card-title">Névjegy</div>
        <p style="margin:0">Rig Supervisor v${esc(this.version || '')} – vízhűtéses Antminer telep felügyelete Raspberry Pi-n.</p></div>`;
    $('#sg-save').addEventListener('click', () => this._save('sg', collect($('#sys-gen')), () => { document.getElementById('site-name').textContent = this.cfg.site_name; }));
    $('#sg-reset').addEventListener('click', () => this.open('system'));
    $('#sl-save').addEventListener('click', () => this._save('sl', collect($('#sys-log')), () => this._logInfo()));
    $('#sl-reset').addEventListener('click', () => this.open('system'));
    this._bindModeCard('sy');
    $('#auth-save').addEventListener('click', async () => {
      const on = $('#auth-on').checked;
      const pw = $('#auth-pw').value;
      if (pw !== $('#auth-pw2').value) { toast('A két jelszó nem egyezik', 'warn'); return; }
      try {
        await api('/api/password', { method: 'POST', body: { enabled: on, username: $('#auth-user').value, password: pw } });
        toast(on ? 'Jelszavas védelem bekapcsolva – a böngésző bejelentkezést kérhet' : 'Jelszavas védelem kikapcsolva', 'ok', 6000);
        await this.reload();
        this.open('system');
      } catch (e) { toast(e.message, 'err', 6000); }
    });
    this._logInfo();
    this._hwInfo();
    if (this.sim) this._simPanel();
  }

  async _logInfo() {
    try {
      const { info } = await api('/api/loginfo');
      const pct = info.max_bytes ? Math.min(100, (100 * info.size_bytes) / info.max_bytes) : 0;
      $('#log-info').innerHTML = `<div class="hint">Napló mérete: <b>${bytes(info.size_bytes)}</b> / ${bytes(info.max_bytes)} (${num(pct, 1)}%) ·
        ${info.files} napi fájl${info.oldest_day ? ` (${info.oldest_day} – ${info.newest_day})` : ''} · szabad hely: ${bytes(info.disk_free_bytes)}</div>
        <div class="usage-bar"><i style="width:${pct.toFixed(1)}%"></i></div><div class="hint mono">${esc(info.directory)}</div>`;
    } catch (e) { /* ignore */ }
  }

  _hwInfo() {
    const hw = this.store.system && this.store.system.hardware;
    if (!hw) return;
    const errs = [...(hw.gpio_errors || []), ...Object.entries(hw.pin_errors || {}).map(([k, v]) => k + ': ' + v)];
    $('#hw-kv').innerHTML = `<dt>Mód</dt><dd>${hw.sim ? 'Szimuláció' : 'Raspberry Pi (valódi hardver)'}</dd>
      <dt>GPIO könyvtár</dt><dd>${esc(hw.gpio_backend || '—')} ${hw.gpio_ok ? '<span class="pill on">OK</span>' : '<span class="pill bad">HIBA</span>'}</dd>
      <dt>ADC</dt><dd>${esc(hw.adc || '—')} ${hw.adc_ok ? '<span class="pill on">OK</span>' : `<span class="pill bad">${esc(hw.adc_error || 'HIBA')}</span>`}</dd>
      ${errs.length ? `<dt>Hibák</dt><dd>${errs.map(esc).join('<br>')}</dd>` : ''}`;
  }

  async _simPanel() {
    let st;
    try { st = (await api('/api/sim', { method: 'POST', body: { action: 'get' } })).sim; } catch (e) { return; }
    const f = st.faults;
    const sensorSel = (sid) => `<select class="inp sm" data-simsensor="${sid}">
      <option value="" ${!f[sid] ? 'selected' : ''}>OK</option><option value="open" ${f[sid] === 'open' ? 'selected' : ''}>Szakadás</option>
      <option value="short" ${f[sid] === 'short' ? 'selected' : ''}>Rövidzár</option></select>`;
    $('#sim-body').innerHTML = `<div class="sim-grid">
        <div class="sim-item"><b>Áramlás hiba</b>
          <label class="switch"><input type="checkbox" data-simflow="flow1" ${f.flow1 ? 'checked' : ''}><i></i>F1 – belső kör nem áramlik</label>
          <label class="switch"><input type="checkbox" data-simflow="flow2" ${f.flow2 ? 'checked' : ''}><i></i>F2 – külső kör nem áramlik</label></div>
        <div class="sim-item"><b>Hőmérő szenzor hiba</b>
          ${SENSORS.map((sid) => `<div style="display:flex;align-items:center;gap:8px"><span style="width:28px">${sid.toUpperCase()}</span>${sensorSel(sid)}</div>`).join('')}</div>
        <div class="sim-item"><b>Fizikai nyomógombok</b>
          <button class="btn btn-ok" data-simbtn="btn_start">START gomb megnyomása</button>
          <button class="btn btn-danger" data-simbtn="btn_stop">STOP gomb megnyomása</button>
          <label class="switch"><input type="checkbox" data-simkill ${st.killswitch ? 'checked' : ''}><i></i>KILLSWITCH bemenet aktív</label></div>
        <div class="sim-item"><b>Külső hőmérséklet (radiátor)</b>
          <div class="inp-unit"><input class="inp sm num" type="number" id="sim-amb" value="${st.ambient}"><span>°C</span></div>
          <button class="btn btn-sm" id="sim-amb-set">Beállít</button></div>
      </div>
      <div class="tbl-wrap" style="margin-top:12px"><table class="tbl"><thead><tr><th>Miner</th><th>Nem elérhető</th><th>Nem áll le (utóhűtés teszt)</th><th>Hibaüzenet</th></tr></thead><tbody>
        ${Object.entries(st.miners).map(([id, m]) => `<tr><td>${esc(m.name)}</td>
          <td><label class="switch"><input type="checkbox" data-simminer="${esc(id)}" data-field="offline" ${m.offline ? 'checked' : ''}><i></i></label></td>
          <td><label class="switch"><input type="checkbox" data-simminer="${esc(id)}" data-field="ignore_stop" ${m.ignore_stop ? 'checked' : ''}><i></i></label></td>
          <td><div style="display:flex;gap:6px"><input class="inp sm" data-simerr="${esc(id)}" value="${esc(m.error || '')}" placeholder="pl. Ventilátor hiba (kód 2)">
            <button class="btn btn-sm" data-simerrset="${esc(id)}">Beállít</button></div></td></tr>`).join('')}
      </tbody></table></div>`;
    const sim = (body) => api('/api/sim', { method: 'POST', body }).then(() => toast('Szimuláció módosítva', 'ok', 1800)).catch((e) => toast(e.message, 'err'));
    const root = $('#sim-body');
    root.addEventListener('change', (e) => {
      const t = e.target;
      if (t.dataset.simflow) sim({ action: 'fault', key: t.dataset.simflow, value: t.checked });
      else if (t.dataset.simsensor) sim({ action: 'fault', key: t.dataset.simsensor, value: t.value || null });
      else if (t.dataset.simkill !== undefined) sim({ action: 'killswitch', value: t.checked });
      else if (t.dataset.simminer) sim({ action: 'miner', id: t.dataset.simminer, field: t.dataset.field, value: t.checked });
    });
    root.addEventListener('click', (e) => {
      const b = e.target.closest('button');
      if (!b) return;
      if (b.dataset.simbtn) sim({ action: 'button', key: b.dataset.simbtn });
      else if (b.id === 'sim-amb-set') sim({ action: 'ambient', value: parseFloat($('#sim-amb').value) });
      else if (b.dataset.simerrset) sim({ action: 'miner', id: b.dataset.simerrset, field: 'error', value: root.querySelector(`[data-simerr="${CSS.escape(b.dataset.simerrset)}"]`).value });
    });
  }
}
