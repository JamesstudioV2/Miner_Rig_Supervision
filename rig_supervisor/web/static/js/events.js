// Eseménynapló fül: szűrés szint szerint, lapozás, hibakód jegyzék.
import { api, $, $$, esc, toast } from './util.js';
import { renderEventRows } from './dashboard.js';

export class EventsView {
  constructor() {
    this.level = '';
    this.items = [];
    this.loading = false;
    $('#event-filter').addEventListener('click', (e) => {
      const b = e.target.closest('button');
      if (!b) return;
      this.level = b.dataset.level;
      for (const x of $$('#event-filter button')) x.classList.toggle('active', x === b);
      this.load(true);
    });
    $('#events-more').addEventListener('click', () => this.load(false));
  }

  async show() {
    await this.load(true);
    try {
      const last = this.items.length ? this.items[0].id : 0;
      localStorage.setItem('rig.lastSeenEvent', String(last));
    } catch (e) { /* ignore */ }
  }

  setCatalog(catalog) {
    $('#code-table').innerHTML = Object.entries(catalog).map(([code, text]) =>
      `<div class="code-row"><b>${esc(code)}</b><span>${esc(text)}</span></div>`).join('') +
      '<p class="hint">E = vészleállást okozó hiba · W = figyelmeztetés (nincs leállás) · I = tájékoztatás. A minerek hibái (W4xx) soha nem állítják le a rendszert.</p>';
  }

  async load(reset) {
    if (this.loading) return;
    this.loading = true;
    try {
      const before = !reset && this.items.length ? this.items[this.items.length - 1].id : '';
      const q = `/api/events?limit=100${before ? '&before=' + before : ''}${this.level ? '&level=' + this.level : ''}`;
      const d = await api(q);
      this.items = reset ? d.events : this.items.concat(d.events);
      $('#event-list').innerHTML = renderEventRows(this.items);
      $('#events-more').hidden = d.events.length < 100;
    } catch (e) {
      toast('Események betöltése sikertelen: ' + e.message, 'err');
    } finally {
      this.loading = false;
    }
  }
}
