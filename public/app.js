/* CRISPprint Ghana — front end. Vanilla JS, no build step, local server with offline sync.
   Selected job-status updates are delivered by the configured shop server; other messages
   can still be handed off to WhatsApp or Mail for staff to send. */
'use strict';

const S = { boot: null, clients: [], route: null, timer: null, flash: null, settle: false, notify: { byId: {} }, momo: null, offlineQueuedToast: false, connected: navigator.onLine };
const OFFLINE_DB = 'crispprint-offline-v1';
let offlineDbPromise = null;
let syncingOfflineQueue = false;
let nextOfflineId = -Date.now();
let nextOfflineSequence = 0;
let deferredInstallPrompt = null;

/* --------------------------------------------------------------- tiny helpers */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const esc = (v) => String(v === null || v === undefined ? '' : v)
  .replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const sym = () => (S.boot ? S.boot.shop.currency_symbol : '₵');
const n2 = (v) => Number(v === null || v === undefined || v === '' ? 0 : v).toFixed(2);
const money = (v) => {
  const n = Number(v || 0);
  const body = Math.abs(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return (n < 0 ? '-' : '') + sym() + ' ' + body;
};
const compact = (v) => {
  const n = Number(v || 0);
  if (Math.abs(n) >= 1000) return sym() + (n / 1000).toFixed(1).replace(/\.0$/, '') + 'k';
  return sym() + n.toFixed(0);
};
const day10 = (s) => (s ? String(s).slice(0, 10) : '');
const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const fdate = (s) => {
  if (!s) return '—';
  const d = new Date(day10(s) + 'T00:00:00');
  if (isNaN(d)) return esc(s);
  return d.getDate() + ' ' + MON[d.getMonth()];
};
const fdatetime = (s) => {
  if (!s) return '—';
  const d = new Date(String(s).replace(' ', 'T'));
  if (isNaN(d)) return esc(s);
  return d.getDate() + ' ' + MON[d.getMonth()] + ' ' + d.getFullYear() + ', ' +
    String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
};
const todayISO = () => {
  const d = new Date();
  return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
};
const daysBetween = (a, b) => Math.round((new Date(b + 'T00:00:00') - new Date(a + 'T00:00:00')) / 86400000);
const dueLabel = (job) => {
  if (!job.due_date) return '<span class="due">no due date</span>';
  const due = day10(job.due_date);
  const open = job.status !== 'Delivered' && job.status !== 'Cancelled';
  const diff = daysBetween(todayISO(), due);
  if (diff < 0 && open) return '<span class="due late">' + esc(fdate(due)) + ' · ' + (-diff) + 'd late</span>';
  if (diff === 0) return '<span class="due late">due today</span>';
  if (diff <= 2 && open) return '<span class="due">' + esc(fdate(due)) + ' · in ' + diff + 'd</span>';
  return '<span class="due">' + esc(fdate(due)) + '</span>';
};
const pill = (status) => '<span class="pill s-' + esc(status) + '">' + esc(status) + '</span>';
const prio = (p) => (p === 'Urgent' ? ' <span class="pill p-Urgent">Urgent</span>' : '');
const kindTag = (j) => (j.kind === 'Quote' ? ' <span class="pill k-Quote">Quote</span>' : '');
/* Billed work whose balance has come down to nothing. Overpayment leaves a negative balance,
   which is credit rather than a settlement, so it does not count as paid in full here. */
const isSettled = (j) => (j.kind !== 'Quote' && j.status !== 'Cancelled' &&
  Number(j.total) > 0 && Number(j.balance) >= -0.005 && Number(j.balance) <= 0.005);
// A job with item lines is priced by its lines, so the header quantity no longer means much.
const linesLabel = (j) => (j.item_count
  ? j.item_count + ' item line' + (j.item_count === 1 ? '' : 's')
  : (j.quantity ? j.quantity + ' ' + (j.unit || 'pcs') : ''));
const validityLabel = (j) => {
  if (!j.valid_until) return '<span class="due">no expiry</span>';
  const diff = daysBetween(todayISO(), day10(j.valid_until));
  if (diff < 0) return '<span class="due late">expired ' + esc(fdate(j.valid_until)) + '</span>';
  if (diff === 0) return '<span class="due late">last day today</span>';
  return '<span class="due">valid ' + esc(fdate(j.valid_until)) + ' · ' + diff + 'd left</span>';
};
const dateLabel = (j) => (j.kind === 'Quote' ? validityLabel(j) : dueLabel(j));
const qs = (obj) => Object.keys(obj).filter((k) => obj[k] !== '' && obj[k] !== null && obj[k] !== undefined && obj[k] !== 'all')
  .map((k) => encodeURIComponent(k) + '=' + encodeURIComponent(obj[k])).join('&');

function offlineDB() {
  if (!offlineDbPromise) offlineDbPromise = new Promise((resolve, reject) => {
    if (!('indexedDB' in window)) return reject(new Error('This browser cannot store offline changes safely.'));
    const request = indexedDB.open(OFFLINE_DB, 1);
    request.onupgradeneeded = () => {
      const db = request.result;
      db.createObjectStore('responses', { keyPath: 'key' });
      db.createObjectStore('outbox', { keyPath: 'operationId' });
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error('Could not open offline storage.'));
  });
  return offlineDbPromise;
}
async function offlineRead(store, key) {
  const db = await offlineDB();
  return new Promise((resolve, reject) => {
    const request = db.transaction(store, 'readonly').objectStore(store).get(key);
    request.onsuccess = () => resolve(request.result || null);
    request.onerror = () => reject(request.error || new Error('Could not read offline storage.'));
  });
}
async function offlineWrite(store, value) {
  const db = await offlineDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(store, 'readwrite');
    tx.objectStore(store).put(value);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error || new Error('Could not save offline data.'));
    tx.onabort = () => reject(tx.error || new Error('Offline save was cancelled.'));
  });
}
async function offlineAll(store) {
  const db = await offlineDB();
  return new Promise((resolve, reject) => {
    const request = db.transaction(store, 'readonly').objectStore(store).getAll();
    request.onsuccess = () => resolve(request.result || []);
    request.onerror = () => reject(request.error || new Error('Could not read offline storage.'));
  });
}
function apiUrl(path) {
  const url = new URL(path, location.href);
  return url.pathname + url.search;
}
function mutationResource(path) {
  const parts = new URL(path, location.href).pathname.split('/').filter(Boolean);
  return parts.length >= 3 && parts[0] === 'api' &&
    ['jobs', 'clients', 'leads', 'expenses', 'spoiled'].includes(parts[1])
    ? '/' + parts.slice(0, 3).join('/') : '';
}
function makeOfflineResponse(path, payload, id) {
  if (path === '/api/leads' || path === '/api/clients' || path === '/api/jobs' ||
      path === '/api/expenses' || path === '/api/payments' || path === '/api/spoiled') {
    return Object.assign({ id, offlineQueued: true, ref: 'Pending sync' }, payload || {});
  }
  if (/^\/api\/leads\/\d+\/convert$/.test(path)) {
    return { offlineQueued: true, job: { id, ref: 'Pending sync' }, lead: { id: Number(path.split('/')[3]), name: 'Enquiry' } };
  }
  return { id, offlineQueued: true };
}
async function api(path, opts) {
  const options = opts || {};
  const method = String(options.method || 'GET').toUpperCase();
  const key = apiUrl(path);
  if (method === 'GET') {
    try {
      const response = await fetch(path, options);
      const ct = response.headers.get('Content-Type') || '';
      const body = ct.indexOf('json') >= 0 ? await response.json() : await response.text();
      if (!response.ok) {
        const error = new Error((body && body.error) || 'Something went wrong (' + response.status + ')');
        error.status = response.status;
        error.body = body;
        throw error;
      }
      if (ct.indexOf('json') >= 0 && !key.startsWith('/api/backup')) {
        await offlineWrite('responses', { key, value: body, etag: response.headers.get('ETag') || '', savedAt: Date.now() });
      }
      S.connected = true;
      paintNetworkStatus();
      return body;
    } catch (error) {
      if (error instanceof TypeError || !navigator.onLine) {
        S.connected = false;
        paintNetworkStatus();
        const cached = await offlineRead('responses', key);
        if (cached) return cached.value;
        throw new Error('No saved copy of this screen is available offline. Connect to the shop Wi-Fi and try again.');
      }
      throw error;
    }
  }

  let payload = {};
  try { payload = typeof options.body === 'string' ? JSON.parse(options.body) : (options.body || {}); } catch (_) {}
  const headers = Object.assign({ 'Content-Type': 'application/json' }, options.headers || {});
  if (!headers['X-Operation-Id']) headers['X-Operation-Id'] =
    (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : String(Date.now()) + '-' + Math.random().toString(36).slice(2);
  const resource = mutationResource(path);
  if (resource && !headers['If-Match']) {
    const cached = await offlineRead('responses', resource);
    if (cached && cached.etag) headers['If-Match'] = cached.etag;
  }
  try {
    const response = await fetch(path, Object.assign({}, options, { headers }));
    const ct = response.headers.get('Content-Type') || '';
    const body = ct.indexOf('json') >= 0 ? await response.json() : await response.text();
    if (!response.ok) {
      const error = new Error((body && body.error) || 'Something went wrong (' + response.status + ')');
      error.status = response.status;
      error.body = body;
      throw error;
    }
    S.connected = true;
    paintNetworkStatus();
    return body;
  } catch (error) {
    if (!(error instanceof TypeError) && navigator.onLine) throw error;
    S.connected = false;
    const tempId = nextOfflineId--;
    const entry = {
      operationId: headers['X-Operation-Id'], method, path: key,
      body: payload, baseEtag: headers['If-Match'] || '',
      tempId: method === 'POST' && /^\/api\/(clients|jobs|leads|expenses|payments|spoiled)$/.test(new URL(path, location.href).pathname)
        ? tempId : null,
      createdAt: new Date().toISOString(), sequence: nextOfflineSequence++, state: 'pending', error: '',
    };
    await offlineWrite('outbox', entry);
    S.offlineQueuedToast = true;
    paintNetworkStatus();
    return makeOfflineResponse(new URL(path, location.href).pathname, payload, entry.tempId);
  }
}
async function paintNetworkStatus() {
  const el = $('#networkStatus');
  if (!el) return;
  let pending = 0;
  try { pending = (await offlineAll('outbox')).filter((row) => row.state === 'pending' || row.state === 'conflict').length; }
  catch (_) {}
  el.textContent = S.connected
    ? (pending ? 'Connected · ' + pending + ' changes waiting' : 'Connected to the book')
    : 'Offline · saved changes: ' + pending;
  el.classList.toggle('offline', !S.connected || pending > 0);
  const sync = $('.navlink[data-view="sync"]');
  if (sync) {
    const old = sync.querySelector('span');
    if (old) old.remove();
    if (pending) sync.insertAdjacentHTML('beforeend', '<span>' + pending + ' waiting</span>');
  }
}
function replaceTemporaryIds(value, map) {
  if (typeof value === 'number' && value < 0 && map[String(value)]) return map[String(value)];
  if (typeof value === 'string' && /^-\d+$/.test(value) && map[value]) return map[value];
  if (Array.isArray(value)) return value.map((item) => replaceTemporaryIds(item, map));
  if (value && typeof value === 'object') {
    const copy = {};
    Object.keys(value).forEach((key) => { copy[key] = replaceTemporaryIds(value[key], map); });
    return copy;
  }
  return value;
}
async function syncOfflineQueue() {
  if (syncingOfflineQueue || !navigator.onLine) return;
  syncingOfflineQueue = true;
  let reachedServer = true;
  try {
    const entries = (await offlineAll('outbox')).sort((a, b) =>
      a.createdAt.localeCompare(b.createdAt) || a.sequence - b.sequence);
    if (!entries.some((entry) => entry.state === 'pending')) return;
    let syncedAny = false;
    const idMap = {};
    entries.filter((entry) => entry.state === 'synced' && entry.tempId && entry.serverId)
      .forEach((entry) => { idMap[String(entry.tempId)] = entry.serverId; });
    for (const entry of entries) {
      if (entry.state === 'synced' || entry.state === 'discarded') continue;
      if (entry.state === 'conflict') break;
      let path = entry.path;
      Object.keys(idMap).forEach((temp) => { path = path.replace('/' + temp, '/' + idMap[temp]); });
      const headers = {
        'Content-Type': 'application/json',
        'X-Operation-Id': entry.operationId,
      };
      if (entry.baseEtag) headers['If-Match'] = entry.baseEtag;
      let response;
      try {
        response = await fetch(path, {
          method: entry.method, headers,
          body: entry.method === 'DELETE' ? undefined : JSON.stringify(replaceTemporaryIds(entry.body, idMap)),
        });
      } catch (_) { reachedServer = false; break; }
      const ct = response.headers.get('Content-Type') || '';
      const body = ct.includes('json') ? await response.json() : await response.text();
      if (response.status === 401) {
        S.connected = true;
        loginScreen('Sign in again to sync saved changes.');
        break;
      }
      if (response.status >= 500) { reachedServer = false; break; }
      if (response.status === 409 || !response.ok) {
        entry.state = 'conflict';
        entry.error = (body && body.error) || 'This change needs review before it can sync.';
        entry.current = body && body.current;
        entry.currentEtag = body && body.etag;
        await offlineWrite('outbox', entry);
        break;
      }
      entry.state = 'synced';
      entry.serverId = body && (body.id || (body.job && body.job.id)) || null;
      await offlineWrite('outbox', entry);
      syncedAny = true;
      if (entry.tempId && entry.serverId) idMap[String(entry.tempId)] = entry.serverId;
    }
    if (syncedAny) {
      const db = await offlineDB();
      await new Promise((resolve, reject) => {
        const tx = db.transaction('responses', 'readwrite');
        tx.objectStore('responses').clear();
        tx.oncomplete = resolve;
        tx.onerror = () => reject(tx.error || new Error('Could not refresh offline copies.'));
      });
    }
    S.connected = reachedServer;
    await paintNetworkStatus();
    if (S.route && S.route.view === 'sync') await render();
  } finally {
    syncingOfflineQueue = false;
  }
}
function toast(msg, kind) {
  if (S.offlineQueuedToast) {
    S.offlineQueuedToast = false;
    msg = 'Saved on this device; waiting to sync when connected to the shop.';
    kind = 'good';
  }
  const t = document.createElement('div');
  t.className = 'toast ' + (kind || '');
  t.textContent = msg;
  $('#toasts').appendChild(t);
  setTimeout(() => t.remove(), 3200);
}
function fail(err) { toast(err.message || String(err), 'bad'); }

/* ------------------------------------------------------------------- chrome */
async function boot() {
  S.boot = await api('/api/bootstrap');
  if (S.boot.db) $('#footDb').textContent = S.boot.db;
  paintNav();
}
/* Whether *this* device is holding the book open with a session of its own. The shop's own
   computer never does — it is the machine the records sit on — so it gets no sign-out button. */
function paintSession() {
  const btn = $('#signOut');
  if (!btn) return;
  const s = S.session || {};
  btn.hidden = !(s.required && s.authenticated && !s.this_is_the_shop_computer);
}
function paintNav() {
  const b = S.boot;
  const badges = {
    jobs: b.open_jobs || '',
    leads: b.open_leads || '',
    quotes: '',
    clients: '',
    accounts: '',
    expenses: '',
    dashboard: '',
    reports: '',
    sync: '',
    shop: '',
  };
  $$('.navlink[data-view]').forEach((a) => {
    const view = a.dataset.view;
    a.classList.toggle('on', S.route && S.route.view === view);
    const old = a.querySelector('span');
    if (old) old.remove();
    let text = badges[view];
    if (view === 'jobs' && b.open_quotes) text += ' · ' + pluralise(b.open_quotes, 'quote');
    if (view === 'jobs' && b.to_send) text += ' · ' + pluralise(b.to_send, 'message') + ' to send';
    if (view === 'dashboard' && b.to_check) text += ' · ' + pluralise(b.to_check, 'MoMo payment') + ' to check';
    if (text) a.insertAdjacentHTML('beforeend', '<span>' + text + '</span>');
  });
}
const pluralise = (n, word) => n + ' ' + (n === 1 ? word
  : word.endsWith('y') ? word.slice(0, -1) + 'ies' : word + 's');
function paintFoot() {
  const plural = pluralise;
  const b = S.boot;
  const bits = [plural(b.open_jobs, 'open job'), plural(b.clients, 'client')];
  if (b.open_quotes) bits.push(plural(b.open_quotes, 'quote'));
  if (b.open_leads) bits.push(plural(b.open_leads, 'enquiry'));
  $('#footCounts').textContent = bits.join(' · ');
  // One book, in one file, on one Mac: how recently it was copied belongs where it can be seen
  // from any screen, because the screens that need it are the ones that come after a loss.
  const foot = $('#footBackup');
  const copy = b.backup || {};
  if (foot) {
    foot.textContent = copy.at ? fdate(copy.at.slice(0, 10)) : 'never';
    foot.classList.toggle('late', !!copy.stale);
  }
}
async function refreshChrome() {
  try { S.boot = await api('/api/bootstrap'); } catch (e) { /* keep going */ }
  paintNav();
  paintFoot();
}

/* ---------------------------------------------------------------- phone shell */
/* A phone has no room for a rail, so the same destinations become a tab bar a thumb
   reaches and everything that does not fit moves into a sheet behind its last slot.
   Links and buttons are moved rather than copied, so the router, the active state and
   the "3 waiting" badges keep working wherever they end up living. */
const PHONE = window.matchMedia('(max-width: 720px)');
const TABS = { dashboard: 'Desk', jobs: 'Jobs', accounts: 'Money', clients: 'Clients' };
let phoneRestore = null;

function stash(node, parent) {
  if (!node || node.parentNode === parent) return;
  phoneRestore.push([node, node.parentNode, node.nextElementSibling]);
  parent.appendChild(node);
}

function shellChrome() {
  const wide = !PHONE.matches;
  document.documentElement.dataset.shell = wide ? 'wide' : 'phone';
  if (wide) {
    (phoneRestore || []).slice().reverse().forEach((entry) => {
      const node = entry[0], parent = entry[1], next = entry[2];
      if (parent) parent.insertBefore(node, next && next.parentNode === parent ? next : null);
    });
    phoneRestore = null;
    ['#phonetop', '#tabbar', '#moresheet'].forEach((sel) => {
      const el = $(sel);
      if (el) el.remove();
    });
    return;
  }
  if (document.getElementById('tabbar')) return;
  phoneRestore = [];
  const side = $('#side');
  const top = document.createElement('div');
  top.className = 'phonetop';
  top.id = 'phonetop';
  $('.main').insertBefore(top, $('#topbar'));
  stash($('#networkStatus'), top);

  const bar = document.createElement('nav');
  bar.className = 'tabbar';
  bar.id = 'tabbar';
  bar.setAttribute('aria-label', 'Main screens');
  Object.keys(TABS).forEach((view) => {
    const link = $('#nav .navlink[data-view="' + view + '"]');
    if (!link) return;
    link.dataset.short = TABS[view];
    stash(link, bar);
  });
  const more = document.createElement('button');
  more.type = 'button';
  more.className = 'navlink more';
  more.id = 'moreTab';
  more.dataset.short = 'More';
  more.dataset.action = 'toggle-more';
  more.setAttribute('aria-expanded', 'false');
  bar.appendChild(more);

  const sheet = document.createElement('div');
  sheet.className = 'moresheet';
  sheet.id = 'moresheet';
  sheet.hidden = true;
  side.appendChild(sheet);
  side.appendChild(bar);
  $$('#nav .navlink').forEach((link) => stash(link, sheet));
  stash($('.side-cta'), sheet);
  stash($('.side-bottom'), sheet);
}

function closeMore() {
  const sheet = $('#moresheet');
  if (!sheet || sheet.hidden) return;
  sheet.hidden = true;
  const tab = $('#moreTab');
  if (tab) tab.setAttribute('aria-expanded', 'false');
}
PHONE.addEventListener('change', shellChrome);

/* A launcher shortcut opens the app already part-way into a job: ?action= names the
   button the tap stood for, and the address is tidied away once it has been honoured. */
function launchShortcut() {
  const asked = new URLSearchParams(location.search).get('action');
  if (!asked) return;
  history.replaceState(null, '', location.pathname + location.hash);
  if (!/^[a-z][a-z-]{2,30}$/.test(asked)) return;
  const handler = ACTIONS[asked];
  if (typeof handler === 'function') {
    handler(document.querySelector('[data-action="' + asked + '"]') || document.body);
  }
}
function topbar(title, sub, controls, buttons) {
  const list = buttons || [{ label: '+ New job', action: 'new-job', primary: true }];
  $('#topbar').innerHTML =
    '<div class="title"><h1>' + esc(title) + '</h1>' +
    (sub ? '<span class="sub">' + sub + '</span>' : '') +
    '<span class="spacer"></span>' +
    list.map((b) => '<button class="btn' + (b.primary ? ' primary' : '') + '" data-action="' + b.action + '">' +
      esc(b.label) + '</button>').join('') + '</div>' +
    (controls || '');
}
function emptyState(title, note, action) {
  return '<div class="empty"><b>' + esc(title) + '</b><span>' + esc(note) + '</span>' +
    (action || '') + '</div>';
}
function loginScreen(message) {
  S.session = { required: true, authenticated: false };
  paintSession();
  $('#topbar').innerHTML = '';
  $('#view').innerHTML = '<div class="login-card card"><h1>Sign in to the shop book</h1>' +
    '<p class="hint">Enter the shop password to access shared records.</p>' +
    (message ? '<p class="err">' + esc(message) + '</p>' : '') +
    '<form id="loginForm"><label class="field"><span>Shop password</span>' +
    '<input name="password" type="password" autocomplete="current-password" required autofocus></label>' +
    '<button class="btn primary">Sign in</button><div class="err" id="loginErr"></div></form></div>';
  $('#loginForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    $('#loginErr').textContent = '';
    try {
      const response = await fetch('/api/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password: form.password.value }),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.error || 'Sign-in failed');
      location.reload();
    } catch (error) {
      $('#loginErr').textContent = error.message || 'Could not sign in. Reconnect to the shop server and try again.';
    }
  });
}

/* ------------------------------------------------------------------- router */
function parseHash() {
  const raw = (location.hash || '#/dashboard').replace(/^#\/?/, '');
  const cut = raw.indexOf('?');
  const path = cut < 0 ? raw : raw.slice(0, cut);
  const parts = path.split('/').filter(Boolean);
  return {
    view: parts[0] || 'dashboard',
    id: parts[1] || null,
    params: new URLSearchParams(cut < 0 ? '' : raw.slice(cut + 1)),
  };
}
function go(view, params, id) {
  const p = params || {};
  location.hash = '#/' + view + (id ? '/' + id : '') + (Object.keys(p).length && qs(p) ? '?' + qs(p) : '');
}
async function render() {
  S.route = parseHash();
  paintNav();
  paintNetworkStatus();
  const v = $('#view');
  const r = S.route;
  try {
    if (r.view === 'jobs') await viewJobs(r);
    else if (r.view === 'spoiled') await viewSpoiled(r);
    else if (r.view === 'sync') await viewSyncQueue();
    else if (r.view === 'clients') await viewClients(r);
    else if (r.view === 'accounts') await viewAccounts(r);
    else if (r.view === 'reports') await viewReports(r);
    else if (r.view === 'leads') await viewLeads(r);
    else if (r.view === 'expenses') await viewExpenses(r);
    else if (r.view === 'shop') await viewShop(r);
    else await viewDashboard(r);
    if (r.id && r.view === 'jobs') openJobDrawer(Number(r.id));
    if (r.id && r.view === 'clients') openClientDrawer(Number(r.id));
    if (r.id && r.view === 'leads') openLeadDrawer(Number(r.id));
  } catch (err) {
    if (err.status === 401) { loginScreen('This device has been out of the book for a while. Enter the shop password again.'); return; }
    v.innerHTML = emptyState('Could not load this page', err.message,
      '<button class="btn" onclick="location.reload()">Try again</button>');
  }
  if (S.settle) {
    S.settle = false;
    v.classList.remove('settle');
    void v.offsetWidth;  /* without a reflow the browser sees no change and skips the animation */
    v.classList.add('settle');
    setTimeout(() => { S.flash = null; }, 1400);
  }
}

function filtersBar(view, defs) {
  if (!defs.length) return '';
  const p = S.route.params;
  const ctrl = defs.map((d) => {
    if (d.type === 'tabs') {
      return '<div class="tabs" role="tablist">' + d.options.map((o) =>
        '<button class="tab' + ((p.get(d.key) || d.def) === o.v ? ' on' : '') + '" data-filter="' + d.key + '" data-value="' +
        esc(o.v) + '">' + esc(o.label) + (o.count !== undefined ? ' <b>' + o.count + '</b>' : '') + '</button>').join('') + '</div>';
    }
    if (d.type === 'select') {
      const cur = p.get(d.key) || '';
      return '<select data-filter="' + d.key + '">' + d.options.map((o) =>
        '<option value="' + esc(o[0]) + '"' + (cur === o[0] ? ' selected' : '') + '>' + esc(o[1]) + '</option>').join('') + '</select>';
    }
    if (d.type === 'date') {
      return '<label class="datef"><span>' + esc(d.label) + '</span><input type="date" data-filter="' + d.key +
        '" value="' + esc(p.get(d.key) || d.def || '') + '"></label>';
    }
    return '<div class="search"><input data-filter="' + d.key + '" placeholder="' + esc(d.placeholder || 'Search') +
      '" value="' + esc(p.get(d.key) || '') + '"></div>';
  }).join('');
  return '<div class="toolbar">' + ctrl +
    '<span class="spacer" style="flex:1"></span>' +
    '<button class="btn sm ghost" data-action="clear-filters">Clear</button>' +
    '</div>';
}
function paramOrDefault(key, def) {
  const v = S.route.params.get(key);
  return v === null || v === '' ? def : v;
}

/* ------------------------------------------------------ click-to-change cells */
/* A counter book gets nudged all day: a date slips, a stage moves, a name was typed
   wrong. Rather than open the whole form for that, these cells edit themselves in place
   and post one field, so nothing else on the row can be overwritten by the change. */
const QUICK = {
  'jobs/title': { type: 'text', max: 120 },
  'jobs/status': { type: 'choice', options: () => S.boot.statuses },
  'jobs/priority': { type: 'choice', options: () => ['Normal', 'Urgent'] },
  'jobs/due_date': { type: 'date' },
  'jobs/valid_until': { type: 'date' },
  'leads/interest': { type: 'text', max: 160 },
  'leads/value': { type: 'number' },
  'leads/stage': { type: 'choice', options: () => S.boot.lead_stages },
  'leads/follow_up': { type: 'date' },
};
const QUICK_HINT = {
  title: 'Click to rename', status: 'Click to change the status', priority: 'Click to set priority',
  due_date: 'Click to move the due date', valid_until: 'Click to change how long the price holds',
  interest: 'Click to write what they want', value: 'Click to change the estimate',
  stage: 'Click to move the enquiry along', follow_up: 'Click to set the next chase date',
};
function qe(path, current, label) {
  const field = path.slice(path.lastIndexOf('/') + 1);
  return '<button type="button" class="qe' + (S.flash === path ? ' saved' : '') + '" data-qe="' + esc(path) +
    '" data-cur="' + esc(current === null || current === undefined ? '' : current) + '" title="' +
    esc(QUICK_HINT[field] || 'Click to change') + '">' + label + '</button>';
}
let editor = null;
function openEditor(cell) {
  if (editor && editor.cell === cell) return;
  if (editor) saveEditor();
  const cut = cell.dataset.qe.split('/');
  const spec = QUICK[cut[0] + '/' + cut[2]];
  if (!spec) return;
  const cur = cell.dataset.cur;
  let input;
  if (spec.type === 'choice') {
    input = document.createElement('select');
    input.innerHTML = spec.options().map((o) => '<option' + (o === cur ? ' selected' : '') + '>' + esc(o) + '</option>').join('');
  } else {
    input = document.createElement('input');
    if (spec.type === 'date') { input.type = 'date'; input.value = cur; }
    else if (spec.type === 'number') { input.type = 'number'; input.step = '0.01'; input.min = '0'; input.value = Number(cur || 0); }
    else { input.type = 'text'; if (spec.max) input.maxLength = spec.max; input.value = cur; }
  }
  input.className = 'qe-in';
  // The field sits beside the cell, not inside it: a control nested in a button is not
  // valid markup, and the date picker stops working in some engines.
  cell.parentNode.insertBefore(input, cell);
  cell.hidden = true;
  const ed = { cell: cell, input: input, focused: false };
  /* A web view that is not the key window can hand back a field that never took focus, so
     blur alone would close the editor the instant it opened. Clicking away settles it too. */
  ed.away = (ev) => {
    if (editor !== ed || ev.target === input || input.contains(ev.target)) return;
    saveEditor();
  };
  editor = ed;
  document.addEventListener('click', ed.away, true);
  input.addEventListener('focus', () => { ed.focused = true; });
  input.addEventListener('blur', () => { if (ed.focused && editor === ed) saveEditor(); });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { e.preventDefault(); cancelEditor(); }
    else if (e.key === 'Enter' || e.key === 'Tab') { e.preventDefault(); saveEditor(); }
  });
  if (spec.type !== 'text' && spec.type !== 'number') input.addEventListener('change', () => saveEditor());
  input.focus();
  if (input.setSelectionRange && spec.type === 'text') input.setSelectionRange(input.value.length, input.value.length);
}
function closeEditor() {
  if (!editor) return;
  document.removeEventListener('click', editor.away, true);
  editor.cell.hidden = false;
  editor.input.remove();
  editor = null;
}
function cancelEditor() {
  if (!editor) return;
  editor.cell.classList.remove('saving');
  closeEditor();
}
function saveEditor() {
  if (!editor) return;
  const cell = editor.cell;
  const value = editor.input.value;
  closeEditor();
  const cut = cell.dataset.qe.split('/');
  if (value === cell.dataset.cur) return;
  cell.classList.add('saving');
  api('/api/' + cut[0] + '/' + cut[1] + '/quick', { method: 'POST', body: JSON.stringify({ field: cut[2], value: value }) })
    .then(() => { S.flash = cell.dataset.qe; refreshQuiet(); })
    .catch((err) => {
      cell.classList.remove('saving');
      cell.classList.add('bad');
      setTimeout(() => cell.classList.remove('bad'), 1200);
      fail(err);
    });
}
/* Re-read the book after a change without the page jumping back to the top. */
function refreshQuiet() {
  const y = window.scrollY;
  S.settle = true;
  return Promise.all([boot(), render()])
    .then(() => window.scrollTo(0, y))
    .catch(() => window.scrollTo(0, y));
}

/* --------------------------------------------------------------- dashboard */
async function viewDashboard() {
  const d = await api('/api/dashboard');
  let momo = { signals: [], waiting: 0, booked: 0, booked_total: 0, waiting_total: 0, sent: 0,
    sent_total: 0, clients: [], number: '' };
  try { momo = await loadMomo(); } catch (e) { /* the dashboard still draws without the notices */ }
  watchMomoPanel();
  const k = d.kpi;
  const in15 = d.trend.reduce((a, t) => a + t.collected, 0);
  const out15 = d.trend.reduce((a, t) => a + t.spent, 0);
  const maxIn = Math.max.apply(null, d.trend.map((x) => x.collected)) || 1;
  const bars = d.trend.map((t) => {
    const pct = Math.round((t.collected / maxIn) * 100);
    return '<div class="bar' + (t.collected > 0 ? '' : ' zero') + '" title="' + esc(t.day) + ': in ' + esc(money(t.collected)) +
      ', out ' + esc(money(t.spent)) + ', ' + t.new_jobs + (t.new_jobs === 1 ? ' new job' : ' new jobs') + '"><u>' +
      (t.collected > 0 ? compact(t.collected) : '') + '</u>' +
      '<i style="height:' + Math.max(pct, 2) + '%"></i><s>' + fdate(t.day).replace(/ \w+$/, '') + '</s></div>';
  }).join('');
  const maxMix = Math.max.apply(null, d.mix.map((m) => m.billed).concat([1]));
  const maxSpend = Math.max.apply(null, d.spend.map((m) => m.amount).concat([1]));

  topbar('Dashboard', esc(new Date().toDateString()), filtersBar('dashboard', []));
  $('#view').innerHTML = '<p class="hint editable-hint">Anything underlined you can click: move a due date, change a status, ' +
    'reprice an enquiry — it saves as soon as you press Enter.</p>' +
    '<div class="grid kpis">' +
    stat('Collected today', money(k.collected_today), k.collected_today > 0 ? 'payments entered' : 'nothing entered yet', 'amt good',
      '#/accounts', 'Click to see money in and out') +
    stat('This week', money(k.collected_week), 'last 7 days', 'amt', '#/accounts', 'Click to see the accounts book') +
    stat('Month to date', money(k.collected_month), monthName(day10(d.today)), 'amt', '#/reports', 'Click for this month in reports') +
    stat('Owed to the shop', money(k.outstanding), 'unpaid job balances', k.outstanding > 0 ? 'amt warn' : 'amt',
      '#/accounts', 'Click to work the balances') +
    '</div>' +
    '<div class="grid kpis">' +
    stat('Open jobs', k.open_jobs, statusLine(d.by_status) + ' in the pipeline', '', '#/jobs', 'Click to see the queue') +
    stat('Ready for pickup', k.ready_jobs, 'waiting to be collected', '', '#/jobs?status=Ready', 'Click to see what is ready') +
    stat('Overdue', k.overdue_jobs, 'past their due date', k.overdue_jobs ? 'warn' : '', '#/jobs?overdue=1', 'Click to chase late work') +
    stat('Clients on file', k.clients, 'registered', '', '#/clients', 'Click for the client book') +
    '</div>' +
    '<div class="grid kpis">' +
    stat('Quotes waiting', k.open_quotes, money(k.quoted_value) + ' priced, not booked', '', '#/jobs?kind=Quote', 'Click to see open quotes') +
    stat('Enquiries to chase', k.open_leads, k.leads_due ? k.leads_due + ' due today' : money(k.pipeline_value) + ' of work out there',
      k.leads_due ? 'warn' : '', '#/leads', 'Click to work the pipeline') +
    stat('Spent this month', money(k.spent_month), 'paper, ink, rent and the rest', 'amt', '#/expenses', 'Click to see money out') +
    stat('Profit this month', money(k.profit_month), 'of ' + money(k.billed_month) + ' billed',
      'amt ' + (k.profit_month < 0 ? 'warn' : 'good'), '#/reports', 'Click for the full picture') +
    '</div>' +
    momoCard(momo) +
    '<div class="grid split">' +
    '<div class="card"><h3>Money in — last 15 days</h3>' +
    '<div class="card-b"><div class="bars">' + bars + '</div>' +
    '<div class="legend"><span><i style="background:var(--brand)"></i>cash, MoMo and transfers received</span></div>' +
    '<div class="duo"><span>Received</span><b class="pos">' + money(in15) + '</b>' +
    '<span>Paid out</span><b class="neg">' + money(out15) + '</b>' +
    '<span>Net</span><b class="' + (in15 - out15 < 0 ? 'neg' : '') + '">' + money(in15 - out15) + '</b></div>' +
    '</div></div>' +
    '<div class="card"><h3>What we print most — last 3 months</h3><div class="card-b mix">' +
    (d.mix.length ? d.mix.map((m) => '<div class="row"><span>' + esc(m.category) + '</span><b>' + compact(m.billed) +
      ' · ' + m.jobs + '</b><div class="track"><i style="width:' + Math.round((m.billed / maxMix) * 100) + '%"></i></div></div>').join('')
      : '<p class="hint">No jobs booked in this window.</p>') +
    '</div></div>' +
    '</div>' +
    '<div class="grid split">' +
    cardTable('Quotes waiting on an answer', '<a href="#/jobs?kind=Quote" class="btn sm ghost">All quotes</a>',
      d.quotes.length ? d.quotes.map((q) => '<tr data-open="jobs/' + q.id + '"><td class="strong">' +
        qe('jobs/' + q.id + '/title', q.title, esc(q.title)) +
        '<div class="ref">' + esc(q.ref) + ' · ' + esc(q.client) + '</div></td>' +
        '<td class="num">' + money(q.total) + '</td>' +
        '<td class="when">' + qe('jobs/' + q.id + '/valid_until', day10(q.valid_until), validityLabel(q)) + '</td>' +
        '<td class=num><a class="btn sm" href="#/jobs/' + q.id + '">Open</a></td></tr>').join('')
        : '<tr><td colspan="4" class="hint">No open quotes. Anything asked for has been booked or nothing is waiting.</td></tr>',
      ['Quote', 'Worth', 'Price holds', '']) +
    cardTable('Enquiries to chase', '<a href="#/leads" class="btn sm ghost">All enquiries</a>',
      d.follow_ups.length ? d.follow_ups.map((l) => '<tr data-open-lead="' + l.id + '"><td class="strong">' + esc(l.name) +
        '<div class="ref">' + esc(l.phone || 'no phone') + ' · ' + esc(l.source) + '</div>' +
        qe('leads/' + l.id + '/stage', l.stage, '<span class="pill stage">' + esc(l.stage) + '</span>') + '</td>' +
        '<td>' + qe('leads/' + l.id + '/interest', l.interest,
          l.interest ? esc(l.interest) : '<span class="due">nothing written</span>') + '</td>' +
        '<td class="num">' + qe('leads/' + l.id + '/value', l.value,
          l.value ? money(l.value) : '<span class="due">not priced</span>') + '</td>' +
        '<td class="when">' + qe('leads/' + l.id + '/follow_up', day10(l.follow_up), followLabel(l)) + '</td></tr>').join('')
        : '<tr><td colspan="4" class="hint">Nothing is waiting to be chased up.</td></tr>',
      ['Who asked', 'What they want', 'Worth', 'Chase']) +
    '</div>' +
    '<div class="grid split">' +
    cardTable('Late work', '<a href="#/jobs?overdue=1" class="btn sm ghost">All overdue</a>',
      d.overdue.length ? jobRows(d.overdue) : '<tr><td colspan="5" class="hint">Nothing is past its due date.</td></tr>',
      ['Job', 'Total', 'Balance', 'Status', 'Due']) +
    cardTable('Top debtors', '<a href="#/accounts" class="btn sm ghost">Accounts</a>',
      d.debtors.length ? d.debtors.map((c) => '<tr data-open="clients/' + c.id + '"><td class="strong">' + esc(c.name) +
        '<div class="ref">' + esc(c.phone || 'no phone') + '</div></td><td class="num">' + c.jobs + '</td>' +
        '<td class="num neg balance">' + money(c.owed) + '</td><td><button class="btn sm" data-pay-client="' + c.id +
        '" data-pay-name="' + esc(c.name) + '">Receive</button></td></tr>').join('')
        : '<tr><td colspan="4" class="hint">Every client has paid up. Well done.</td></tr>',
      ['Client', 'Jobs', 'Owes', '']) +
    '</div>' +
    '<div class="grid split">' +
    '<div class="card"><h3>Where the money went — this month<span class="spacer"></span>' +
    '<a href="#/expenses" class="btn sm ghost">Expenses</a></h3><div class="card-b mix">' +
    (d.spend.length ? d.spend.map((c) => '<div class="row"><span>' + esc(c.category) + '</span><b>' + compact(c.amount) +
      ' · ' + c.n + '</b><div class="track"><i class="out" style="width:' + Math.round((c.amount / maxSpend) * 100) + '%"></i></div></div>').join('')
      : '<p class="hint">No costs recorded this month — that makes the profit figure look too good.</p>') +
    '</div></div>' +
    cardTable('Recently booked', '<a href="#/jobs" class="btn sm ghost">All jobs</a>',
      jobRows(d.recent), ['Job', 'Total', 'Balance', 'Status', 'Due']) +
    '</div>';
}
function monthName(iso) {
  return MON[Number((iso || '').slice(5, 7)) - 1] + ' ' + (iso || '').slice(0, 4);
}
function statusLine(by) {
  return ['Pending', 'Printing', 'Ready'].map((s) => (by[s] || 0) + ' ' + s.toLowerCase()).join(' · ');
}
function stat(k, v, n, cls, to, hint) {
  const body = '<span class="k">' + esc(k) + '</span><b class="v">' + v + '</b>' +
    (n ? '<span class="n">' + n + '</span>' : '');
  if (!to) return '<div class="stat ' + (cls || '') + '">' + body + '</div>';
  return '<a class="stat go ' + (cls || '') + '" href="' + esc(to) + '" title="' + esc(hint || 'Click to open') +
    '">' + body + '<i class="arrow" aria-hidden="true"></i></a>';
}
function cardTable(title, action, body, heads) {
  return '<div class="card"><h3>' + esc(title) + (action ? '<span class="spacer"></span>' + action : '') + '</h3>' +
    '<div class="tablewrap"><table><thead><tr>' +
    heads.map((h) => '<th' + (['Jobs', 'Qty', 'Total', 'Paid', 'Balance', 'Outstanding', 'Owes'].indexOf(h) >= 0 ? ' class=num' : '') +
      '>' + esc(h) + '</th>').join('') + '</tr></thead><tbody>' + body + '</tbody></table></div></div>';
}
/* The two dashboard job cards share one row shape: five columns, with the client folded in
   under the job title, so the row stays inside a half-width card. */
function jobRows(rows) {
  if (!rows.length) return '<tr><td colspan="5" class="hint">Nothing here yet.</td></tr>';
  return rows.map((j) => '<tr data-open="jobs/' + j.id + '">' +
    '<td>' + qe('jobs/' + j.id + '/title', j.title, '<span class="strong">' + esc(j.title) + '</span>') + kindTag(j) +
    ' ' + qe('jobs/' + j.id + '/priority', j.priority || 'Normal',
      j.priority === 'Urgent' ? prio('Urgent') : '<span class="pill quiet">normal</span>') +
    '<div class="ref">' + esc(j.category) + ' · ' + esc(j.client) +
    (linesLabel(j) ? ' · ' + esc(linesLabel(j)) : '') +
    (j.to_send ? ' · <span class="tosend">' + pluralise(j.to_send, 'message') + ' to send</span>' : '') + '</div></td>' +
    '<td class="num">' + money(j.total) + '</td>' +
    '<td class="num' + (j.balance > 0 ? ' balance neg' : ' pos') + '">' + money(j.balance) + '</td>' +
    '<td class="chips">' + qe('jobs/' + j.id + '/status', j.status, pill(j.status)) + '</td>' +
    '<td class="when">' + qe('jobs/' + j.id + '/' + (j.kind === 'Quote' ? 'valid_until' : 'due_date'),
      day10(j.kind === 'Quote' ? j.valid_until : j.due_date), dateLabel(j)) + '</td>' +
    '</tr>').join('');
}

/* ---------------------------------------------------------- money in by MoMo
   Clients pay into the shop's MoMo number and the network texts that fact to the shop's own
   phone. The app reads that text rather than asking someone to type the money in a second
   time, but a figure pulled out of an SMS is a proposal, not a posting: the panel shows what
   was read and who it matched, and the shop presses Book. Auto-booking removes the press, not
   the check — the notice and its raw text stay in the book either way. */
let momoDebtors = [];

const momoPhone = (p) => {
  const d = String(p || '').replace(/\D/g, '');
  if (d.length === 12 && d.slice(0, 3) === '233') return '+233 ' + d.slice(3, 5) + ' ' + d.slice(5, 8) + ' ' + d.slice(8);
  if (d.length === 10 && d.charAt(0) === '0') return '0' + d.slice(1, 3) + ' ' + d.slice(3, 6) + ' ' + d.slice(6);
  return String(p || '');
};
const momoWay = (s) => (s.direction === 'Credit' ? '<span class="pill in">Money in</span>'
  : s.direction === 'Out' ? '<span class="pill out">Money out</span>'
    : '<span class="pill maybe">Which way unclear</span>');
/* Mirror the server's guess on the client, so what the panel shows is what the press books. */
function momoGuess(clientId, amount) {
  const jobs = momoDebtors.filter((j) => String(j.client_id) === String(clientId) && j.balance > 0.005);
  if (!jobs.length || !(amount > 0)) return null;
  return jobs.map((j) => ({ id: j.id, gap: Math.abs(j.balance - amount) }))
    .sort((a, b) => a.gap - b.gap)[0].id;
}
function momoJobOptions(clientId, picked, blank) {
  const jobs = momoDebtors.filter((j) => String(j.client_id) === String(clientId) && j.balance > 0.005);
  return '<option value="">' + esc(blank || 'Kept on their account (credit)') + '</option>' + jobs.map((j) =>
    '<option value="' + j.id + '"' + (String(j.id) === String(picked || '') ? ' selected' : '') + '>' +
    esc(j.ref) + ' · owes ' + esc(money(j.balance)) + '</option>').join('');
}
function momoSig(s) {
  const clients = (S.momo && S.momo.clients) || [];
  const cats = (S.boot && S.boot.expense_categories) || [];
  const amt = (s.amount === null || s.amount === undefined) ? '' : n2(s.amount);
  const payer = [s.payer, s.payer_phone ? momoPhone(s.payer_phone) : ''].filter(Boolean).join(' · ');
  const meta = [s.source === 'Messages' ? 'read from Messages' : 'pasted in', fdatetime(s.seen_at)];
  const out = s.direction === 'Out';
  const unclear = s.direction !== 'Credit' && s.direction !== 'Out';
  return '<div class="sig" data-sig="' + s.id + '"' + (out || unclear ? ' data-out="1">' : '>') +
    '<div class="sig-h"><b class="money big">' +
    (amt ? esc(money(Number(amt))) : '<span class="muted">no figure read</span>') + '</b>' + momoWay(s) +
    '<span class="spacer"></span><span class="ref">' + esc(meta.join(' · ')) + '</span></div>' +
    (payer ? '<p class="sig-by">' + (out ? 'Sent to <b>' : unclear ? 'A name in it: <b>' : 'Sent by <b>') +
      esc(payer) + '</b></p>' : '') +
    (s.reason ? '<p class="sig-note">' + esc(s.reason) + '</p>' : '') +
    '<div class="sig-f">' +
    '<label><span>Amount</span><input type="number" class="momo-amt" min="0.01" step="0.01" placeholder="0.00" value="' + esc(amt) + '"></label>' +
    '<label><span>' + (out || unclear ? 'Who this went to' : 'Whose money') + '</span><select class="momo-client"><option value="">' +
    (out || unclear ? 'Nobody in the book — money out' : 'Choose a client…') + '</option>' +
    clients.map((c) => '<option value="' + c.id + '"' + (String(c.id) === String(s.client_id || '') ? ' selected' : '') +
      '>' + esc(c.name) + '</option>').join('') + '</select></label>' +
    (out || unclear ? '<label><span>Money out for</span><select class="momo-cat">' +
      cats.map((c) => '<option value="' + esc(c) + '"' + (c === 'Other' ? ' selected' : '') + '>' + esc(c) + '</option>').join('') +
      '</select></label>' : '') +
    '<label><span>' + (out ? 'Refund it against' : 'Put it against') + '</span><select class="momo-job">' +
      momoJobOptions(s.client_id || '', s.job_id,
        out ? 'Money back on their account' : (unclear ? 'No job of theirs' : '')) + '</select></label>' +
    '<div class="row-actions">' +
    (out ? '<button class="btn sm primary" data-momo-send>Record send</button>'
      : '<button class="btn sm primary" data-momo-book>Book it</button>') +
    (unclear ? '<button class="btn sm" data-momo-send>Record as a send</button>' : '') +
    '<button class="btn sm" data-momo-ignore>Put aside</button></div>' +
    '</div><details class="sig-raw"><summary>The alert as it arrived</summary><p>' + esc(s.raw) + '</p></details>' +
    '</div>';
}
function momoDone(s) {
  const figure = s.amount ? money(s.amount) : 'no figure';
  const who = s.client_id ? '<a href="#/clients/' + s.client_id + '">' + esc(s.client || 'a client') + '</a>' : '';
  let what;
  if (s.state === 'Ignored') what = 'put aside by the shop';
  else if (s.expense_id) what = 'money out · ' + esc(s.expense_category || 'Other') +
    (s.expense_payee ? ' · to ' + esc(s.expense_payee) : '');
  else if (s.direction === 'Out') what = 'refunded to ' + who +
    ' · ' + esc(s.job_ref ? s.job_ref : 'their account');
  else what = 'booked to ' + who + ' · reference ' + esc(s.client || '') + ' · ' +
    esc(s.job_ref ? s.job_ref : 'account credit');
  return '<div class="sig done' + (s.state === 'Ignored' ? ' aside' : '') + '">' +
    '<b class="money">' + esc(figure) + '</b>' +
    '<span>' + what + '</span>' +
    '<span class="spacer"></span><time>' + esc(fdatetime(s.booked_at || s.seen_at)) + '</time></div>';
}
function momoInner(m) {
  const waiting = m.signals.filter((s) => s.state === 'Unreviewed');
  const sends = waiting.filter((s) => s.direction === 'Out');
  const done = m.signals.filter((s) => s.state !== 'Unreviewed').slice(0, 6);
  const toggle = (key, on, label, note) => '<button class="btn sm' + (on ? ' primary' : '') + '" data-momo-' + key +
    ' aria-pressed="' + (on ? 'true' : 'false') + '" title="' + esc(note) + '">' + (on ? '✓ ' : '') + esc(label) + '</button>';
  return '<h3>MoMo money in and out<span class="spacer"></span><span class="momo-ctl">' +
    toggle('watch', m.watching, 'Watch Messages', 'Read Messages every twenty seconds for MoMo alerts, both ways.') +
    toggle('auto', m.auto, 'Book without asking',
      'Post a matched payment in on its own, the moment the alert arrives. A send out always waits for you, ' +
      'because only you can say whether it was money back to a client or the shop paying for something.') +
    '<button class="btn sm" data-momo-check>Check Messages now</button></span></h3><div class="card-b">' +
    '<p class="momo-sum">' + (waiting.length
      ? (m.waiting_total
        ? '<b>' + esc(money(m.waiting_total)) + '</b> in ' + pluralise(waiting.length, 'notice') + ' to check' +
          (sends.length ? ' · ' + pluralise(sends.length, 'send') + ' going out' : '')
        : waiting.length + (waiting.length === 1 ? ' notice with no figure' : ' notices with no figure') + ' to check')
      : '<b>Nothing is waiting.</b>') +
    (m.booked ? '<span>· ' + m.booked + ' booked, ' + esc(money(m.booked_total)) + ' in</span>' : '') +
    (m.sent ? '<span>· ' + m.sent + ' out, ' + esc(money(m.sent_total)) + '</span>' : '') +
    '<span>· clients pay into <b>' + esc(m.number) + '</b></span>' +
    (m.checked ? '<span>· last looked ' + esc(fdatetime(m.checked)) + '</span>' : '') + '</p>' +
    (m.status ? '<p class="sig-note">' + esc(m.status) + '</p>' : '') +
    waiting.map(momoSig).join('') +
    '<div class="momo-paste"><label><span>Paste an alert instead — this needs no permission</span>' +
    '<textarea id="momoPaste" rows="2" placeholder="MTN MoMo: GHS 750.00 received from KOFI MENSA …"></textarea></label>' +
    '<button class="btn sm primary" data-momo-paste>Read it</button></div>' +
    (done.length ? '<div class="momo-done">' + done.map(momoDone).join('') + '</div>' : '') +
    '</div>';
}
const momoCard = (m) => '<div class="card momo" id="momoCard">' + momoInner(m) + '</div>';
async function loadMomo() {
  const res = await api('/api/momo');
  S.momo = res;
  momoDebtors = res.waiting
    ? await api('/api/jobs?' + qs({ status: 'open', kind: 'Job', debtors: '1' })).catch(() => [])
    : [];
  return res;
}
function paintMomo(payload) {
  if (payload) S.momo = payload;
  const card = $('#momoCard');
  if (!card) return;
  card.innerHTML = momoInner(S.momo || { signals: [] });
}
function syncMomoRow(row) {
  const clientId = row.querySelector('.momo-client').value;
  const amount = Number(row.querySelector('.momo-amt').value || 0);
  const out = row.dataset.out === '1';
  row.querySelector('.momo-job').innerHTML = momoJobOptions(clientId, momoGuess(clientId, amount),
    out ? 'Money back on their account' : '');
}
/* The Messages watcher runs on the server, so a payment can arrive while this screen is open.
   A repaint while the shop is typing into the panel would wipe the words, so focus stays safe. */
let momoTick = null;
let notifyTick = null;
let notifyPollBusy = false;
function watchMomoPanel() {
  if (momoTick) return;
  momoTick = setInterval(async () => {
    if (!S.route || S.route.view !== 'dashboard' || !$('#momoCard')) return;
    if ($('#momoCard').contains(document.activeElement)) return;
    if (!$('#modal').hidden || !$('#drawer').hidden) return;
    const before = S.momo || { signals: [] };
    let res;
    try { res = await loadMomo(); } catch (e) { return; }
    const moved = res.waiting !== before.waiting || res.booked_total !== before.booked_total ||
      res.sent_total !== before.sent_total;
    if (!moved) return;
    render();   /* a fresh notice or a fresh booking moves the KPI figures too */
  }, 20000);
}

/* --------------------------------------------------------------------- jobs */
async function viewJobs(r) {
  const p = r.params;
  const kind = p.get('kind') || 'Job';
  const rows = await api('/api/jobs?' + qs({
    status: p.get('status') || 'open', kind: kind, category: p.get('category'), q: p.get('q'),
    sort: p.get('sort'), from: p.get('from'), to: p.get('to'), overdue: p.get('overdue'),
    debtors: p.get('debtors'), client: p.get('client'), paid: p.get('paid'),
  }));
  const quoted = kind === 'Quote';
  const total = rows.reduce((a, j) => a + j.total, 0);
  const due = rows.reduce((a, j) => a + Math.max(j.balance, 0), 0);
  const profit = rows.reduce((a, j) => a + (j.profit || 0), 0);
  topbar(quoted ? 'Quotes' : 'Print jobs',
    rows.length + ' shown · ' + (quoted ? 'quoted ' : 'billed ') + money(total) +
    (quoted ? '' : ' · outstanding ' + money(due) + ' · profit on these ' + money(profit)),
    '', [
      { label: quoted ? '+ New quote' : '+ New job', action: quoted ? 'new-quote' : 'new-job', primary: true },
      { label: '+ Enquiry', action: 'new-lead' },
    ]);
  $('#topbar').insertAdjacentHTML('beforeend', filtersBar('jobs', [
    { type: 'tabs', key: 'kind', def: 'Job', options: [
      { v: 'Job', label: 'Jobs' }, { v: 'Quote', label: 'Quotes', count: S.boot.open_quotes || 0 }]},
    { type: 'tabs', key: 'status', def: 'open', options: [
      { v: 'open', label: 'Open', count: (S.boot.counts.Pending || 0) + (S.boot.counts.Printing || 0) + (S.boot.counts.Ready || 0) },
      { v: 'all', label: 'All', count: Object.values(S.boot.counts).reduce((a, b) => a + b, 0) },
      { v: 'Pending', label: 'Pending', count: S.boot.counts.Pending || 0 },
      { v: 'Printing', label: 'Printing', count: S.boot.counts.Printing || 0 },
      { v: 'Ready', label: 'Ready', count: S.boot.counts.Ready || 0 },
      { v: 'Delivered', label: 'Delivered', count: S.boot.counts.Delivered || 0 },
      { v: 'Cancelled', label: 'Cancelled', count: S.boot.counts.Cancelled || 0 }]},
    /* Quotes are an ask, not a bill, so "paid in full" means nothing on that tab. */
    ...(quoted ? [] : [{ type: 'select', key: 'paid', options: [
      ['', 'Money: any'], ['due', 'Still owing'], ['full', 'Paid in full']] }]),
    { type: 'search', key: 'q', placeholder: 'Search job, item, client or phone' },
    { type: 'select', key: 'category', options: [['', 'Any service']].concat(S.boot.categories.map((c) => [c, c])) },
    { type: 'select', key: 'sort', options: [['created', 'Newest first'], ['due', quoted ? 'Expiring soonest' : 'Due soonest'], ['balance', 'Biggest balance'], ['value', 'Highest value'], ['profit', 'Best profit'], ['client', 'Client A–Z']] },
    { type: 'date', key: 'from', label: 'booked' }, { type: 'date', key: 'to', label: 'to' },
  ]));
  $('#view').innerHTML = '<div class="card">' +
    '<div class="tablewrap"><table><thead><tr><th>' + (quoted ? 'What was quoted' : 'Job') + '</th><th>Client</th>' +
    '<th class=num>' + (quoted ? 'Quoted' : 'Total') + '</th>' +
    (quoted ? '' : '<th class=num>Paid</th><th class=num>Balance</th><th class=num>Profit</th>') +
    '<th>Status</th><th>' + (quoted ? 'Validity' : 'Due') + '</th></tr></thead>' +
    '<tbody>' + (rows.length ? rows.map((j) => '<tr data-open="jobs/' + j.id + '">' +
      '<td><span class="strong">' + esc(j.title) + '</span>' + prio(j.priority) +
      (j.item_count > 1 ? ' <span class="pill i-lines">' + j.item_count + ' lines</span>' : '') +
      '<div class="ref">' + esc(j.ref) + ' · ' + esc(j.category) + ' · booked ' + fdate(j.created_at) + '</div>' +
      (j.size ? '<div class="ref">' + esc(j.size) + '</div>' : '') +
      (linesLabel(j) ? '<div class="ref">' + esc(linesLabel(j)) + '</div>' : '') + '</td>' +
      '<td>' + esc(j.client) + (j.client_phone ? '<div class="ref">' + esc(j.client_phone) + '</div>' : '') + '</td>' +
      '<td class=num>' + money(j.total) + '</td>' +
      (quoted ? '' :
        '<td class=num>' + money(j.paid) + '</td>' +
        '<td class=num' + (j.balance > 0.005 ? ' balance neg' : ' pos') + '">' + money(j.balance) + '</td>' +
        '<td class="num' + (j.profit < 0 ? ' neg' : j.cost > 0 ? '' : ' muted') + '" title="' +
          (j.cost > 0 ? '' : 'Nothing has been costed on this job yet') + '">' + money(j.profit) + '</td>') +
      '<td>' + pill(j.status) + (isSettled(j) ? ' <span class="pill k-paid">Paid in full</span>' : '') + '</td>' +
      '<td>' + dateLabel(j) + '</td></tr>').join('')
      : '<tr><td colspan="8">' + emptyState(quoted ? 'No quotes out there' : 'No jobs match this filter',
        quoted ? 'Send a price before the client commits — a quote can be booked as a job in one click.'
               : 'Try another status tab, or book the first job for today.',
        '<button class="btn primary" data-action="' + (quoted ? 'new-quote' : 'new-job') + '">+ ' +
        (quoted ? 'New quote' : 'New job') + '</button>') + '</td></tr>') +
    '</tbody></table></div></div>' +
    '<p class="hint">' + (quoted
      ? 'Click a quote to see it, print it as an estimate, or book it as a job.'
      : 'Click any row to open the job: item lines, costs, payments, notes and a printable job sheet.') + '</p>';
  restoreFocus();
}

/* ------------------------------------------------------------------ clients */
async function loadClients() {
  S.clients = await api('/api/clients');
  return S.clients;
}
async function viewClients(r) {
  const rows = await api('/api/clients?' + qs({ q: r.params.get('q'), kind: r.params.get('kind'), archived: r.params.get('archived') }));
  const owed = rows.reduce((a, c) => a + Math.max(c.balance_due, 0), 0);
  topbar('Clients', rows.length + ' on file · ' + money(owed) + ' owed across them');
  $('#topbar').insertAdjacentHTML('beforeend', filtersBar('clients', [
    { type: 'search', key: 'q', placeholder: 'Search name, phone, email or area' },
    { type: 'select', key: 'kind', options: [['', 'All types']].concat(S.boot.kinds.map((k) => [k, k])) },
    { type: 'tabs', key: 'archived', def: '', options: [{ v: '', label: 'Active' }, { v: '1', label: 'Archived' }] },
  ]));
  $('#view').innerHTML = '<div class="card"><div class="tablewrap"><table><thead><tr><th>Client</th><th>Contact</th>' +
    '<th class=num>Jobs</th><th class=num>Billed</th><th class=num>Received</th><th class=num>Balance due</th><th>Last job</th></tr></thead><tbody>' +
    (rows.length ? rows.map((c) => '<tr data-open="clients/' + c.id + '">' +
      '<td><span class="strong">' + esc(c.name) + '</span><div class="ref">' + esc(c.kind) +
      (c.archived ? ' · archived' : '') + '</div></td>' +
      '<td>' + (c.phone ? esc(c.phone) : '<span class="hint">no phone</span>') +
      (c.email ? '<div class="ref">' + esc(c.email) + '</div>' : '') + '</td>' +
      '<td class=num>' + c.jobs + (c.active_jobs ? '<div class="ref">' + c.active_jobs + ' open</div>' : '') + '</td>' +
      '<td class=num>' + money(c.billed) + '</td>' +
      '<td class=num>' + money(c.received) + '</td>' +
      '<td class=num' + (c.balance_due > 0.005 ? ' balance neg' : c.balance_due < -0.005 ? ' pos' : '') + '">' + money(c.balance_due) +
      (c.balance_due < -0.005 ? '<div class="ref">credit</div>' : '') + '</td>' +
      '<td>' + (c.last_job ? fdate(c.last_job) : '<span class="hint">never</span>') + '</td></tr>').join('')
      : '<tr><td colspan="7">' + emptyState('No clients yet', 'Add the person or business you printed for; everything else hangs off their record.',
        '<button class="btn primary" data-action="new-client">+ Add first client</button>') + '</td></tr>') +
    '</tbody></table></div></div>';
  restoreFocus();
}

/* ------------------------------------------------------------------ enquiries */
const stagePill = (s) => '<span class="pill g-' + esc(s) + '">' + esc(s) + '</span>';
async function viewLeads(r) {
  const p = r.params;
  const d = await api('/api/leads?' + qs({
    stage: p.get('stage') || 'open', source: p.get('source'), q: p.get('q'), followup: p.get('followup'),
  }));
  const t = d.totals;
  topbar('Enquiries', t.open + ' being chased · ' + money(t.open_value) + ' of work out there · ' +
    t.won + ' won, ' + t.lost + ' lost',
    '', [{ label: '+ New enquiry', action: 'new-lead', primary: true }]);
  const stageTabs = [{ v: 'open', label: 'Being chased', count: t.open }]
    .concat(S.boot.lead_stages.map((s) => {
      const hit = d.pipeline.filter((x) => x.stage === s)[0];
      return { v: s, label: s, count: s === 'Won' || s === 'Lost' ? t[s.toLowerCase()] : (hit ? hit.n : 0) };
    }), [{ v: 'all', label: 'Everything', count: t.total }]);
  $('#topbar').insertAdjacentHTML('beforeend', filtersBar('leads', [
    { type: 'tabs', key: 'stage', def: 'open', options: stageTabs },
    { type: 'search', key: 'q', placeholder: 'Search name, phone or what they want' },
    { type: 'select', key: 'source', options: [['', 'Any source']].concat(S.boot.lead_sources.map((s) => [s, s])) },
    { type: 'tabs', key: 'followup', def: '', options: [{ v: '', label: 'Any time' }, { v: '1', label: 'Chase now', count: t.due_today }] },
  ]));
  const strip = d.pipeline.length ? '<div class="grid kpis">' + S.boot.lead_stages
    .filter((s) => s !== 'Won' && s !== 'Lost')
    .map((s) => {
      const hit = d.pipeline.filter((x) => x.stage === s)[0];
      return stat(s, hit ? hit.n : 0, hit ? money(hit.value) + ' estimated' : 'nothing here', '');
    }).join('') + '</div>' : '';
  $('#view').innerHTML = strip +
    '<div class="card"><div class="tablewrap"><table><thead><tr><th>Who asked</th><th>Contact</th>' +
    '<th>What they want</th><th>Source</th><th class=num>Est. value</th><th>Stage</th><th>Follow up</th><th></th></tr></thead><tbody>' +
    (d.rows.length ? d.rows.map((l) => '<tr data-open-lead="' + l.id + '">' +
      '<td><span class="strong">' + esc(l.name) + '</span>' +
      (l.client ? '<div class="ref"><a href="#/clients/' + l.client_id + '">' + esc(l.client) + '</a></div>' : '<div class="ref">not on file yet</div>') + '</td>' +
      '<td>' + (l.phone ? '<a href="tel:' + esc(l.phone) + '">' + esc(l.phone) + '</a>' : '<span class="hint">no phone</span>') +
      (l.whatsapp && l.whatsapp !== l.phone ? '<div class="ref">WA ' + esc(l.whatsapp) + '</div>' : '') + '</td>' +
      '<td>' + esc(l.interest || '—') + (l.job_title ? '<div class="ref"><a href="#/jobs/' + l.job_id + '">' + esc(l.ref) + ' · ' + esc(l.job_title) + '</a></div>' : '') + '</td>' +
      '<td>' + esc(l.source) + '</td>' +
      '<td class=num>' + (l.value ? money(l.value) : '—') + '</td>' +
      '<td>' + stagePill(l.stage) + '</td>' +
      '<td>' + followLabel(l) + '</td>' +
      '<td class=num><button class="btn sm ghost" data-edit-lead="' + l.id + '">Edit</button></td></tr>').join('')
      : '<tr><td colspan="8">' + emptyState('Nothing in the pipeline',
        'Someone walked in and asked about printing? Write it down before they leave.',
        '<button class="btn primary" data-action="new-lead">+ New enquiry</button>') + '</td></tr>') +
    '</tbody></table></div></div>' +
    '<p class="hint">Click a row to open the enquiry: move it through the stages, or turn a yes into a client and a booked job in one click.</p>';
  restoreFocus();
}
function followLabel(l) {
  const inDays = l.days_to_follow === null || l.days_to_follow === undefined ? l.days : l.days_to_follow;
  if (l.stage === 'Won' || l.stage === 'Lost') return '<span class="due">closed ' + fdate(l.closed_at || l.updated_at) + '</span>';
  if (!l.follow_up) return '<span class="due">no date set</span>';
  if (inDays < 0) return '<span class="due late">' + esc(fdate(l.follow_up)) + ' · ' + (-inDays) + 'd overdue</span>';
  if (inDays === 0) return '<span class="due late">chase today</span>';
  return '<span class="due">' + esc(fdate(l.follow_up)) + ' · in ' + inDays + 'd</span>';
}

/* ------------------------------------------------------------------ expenses */
async function viewExpenses(r) {
  const p = r.params;
  const d = await api('/api/expenses?' + qs({
    category: p.get('category'), q: p.get('q'), from: p.get('from'), to: p.get('to'),
    overhead: p.get('overhead'), job: p.get('job'),
  }));
  const t = d.totals;
  topbar('Expenses', money(t.this_month) + ' out this month · ' + money(t.all_time) + ' all time',
    '', [{ label: '+ Record expense', action: 'new-expense', primary: true },
         { label: '+ New job', action: 'new-job' }]);
  $('#topbar').insertAdjacentHTML('beforeend', filtersBar('expenses', [
    { type: 'tabs', key: 'overhead', def: '', options: [{ v: '', label: 'Everything' },
      { v: '0', label: 'On jobs', count: d.rows.filter((e) => e.job_id).length },
      { v: '1', label: 'Shop overhead', count: d.rows.filter((e) => !e.job_id).length }]},
    { type: 'search', key: 'q', placeholder: 'Search payee, note or reference' },
    { type: 'select', key: 'category', options: [['', 'Any category']].concat(S.boot.expense_categories.map((c) => [c, c])) },
    { type: 'date', key: 'from', label: 'from' }, { type: 'date', key: 'to', label: 'to' },
  ]));
  const maxCat = Math.max.apply(null, d.by_category.map((c) => c.amount).concat([1]));
  $('#view').innerHTML = '<div class="grid kpis">' +
    stat('This month', money(t.this_month), 'money that left the shop', 'amt warn') +
    stat('Last 7 days', money(t.this_week), pluralise(d.rows.filter((e) => e.spent_on >= weekStart()).length, 'entry'), 'amt') +
    stat('Charged to jobs', money(t.on_jobs), 'counted in each job’s cost', 'amt') +
    stat('Shop overhead', money(t.overhead), 'rent, power and the like', 'amt') +
    '</div>' +
    '<div class="grid split">' +
    '<div class="card"><h3>Where the money went — this month<span class="spacer"></span>' +
    '<button class="btn sm ghost" data-export="expenses">Expenses CSV</button></h3><div class="card-b mix">' +
    (d.by_category.length ? d.by_category.map((c) => '<div class="row"><span>' + esc(c.category) + '</span><b>' +
      compact(c.amount) + ' · ' + c.n + '</b><div class="track"><i style="width:' +
      Math.round((c.amount / maxCat) * 100) + '%"></i></div></div>').join('')
      : '<p class="hint">Nothing recorded this month.</p>') + '</div></div>' +
    cardTable('Month by month', '',
      d.months.length ? d.months.map((m) => '<tr><td class="ref">' + monthName(m.month + '-01') + '</td>' +
        '<td class="num pos">' + money(m.income) + '</td><td class="num neg">' + money(m.spent) + '</td>' +
        '<td class="num' + (m.net < 0 ? ' neg' : ' pos') + '">' + money(m.net) + '</td></tr>').join('')
        : '<tr><td colspan="4" class="hint">No money has moved yet.</td></tr>',
      ['Month', 'In', 'Out', 'Net']) +
    '</div>' +
    '<div class="card"><div class="tablewrap"><table><thead><tr><th>When</th><th>What it was</th><th>Paid to</th>' +
    '<th>Against</th><th>Method</th><th class=num>Amount</th><th></th></tr></thead><tbody>' +
    (d.rows.length ? d.rows.map((e) => '<tr>' +
      '<td class="ref">' + fdate(e.spent_on) + '</td>' +
      '<td><span class="strong">' + esc(e.category) + '</span>' + (e.note ? '<div class="ref">' + esc(e.note) + '</div>' : '') + '</td>' +
      '<td>' + esc(e.payee || '—') + (e.reference ? '<div class="ref">' + esc(e.reference) + '</div>' : '') + '</td>' +
      '<td>' + (e.ref ? '<a href="#/jobs/' + e.job_id + '">' + esc(e.ref) + '</a><div class="ref">' + esc(e.job_title || '') + '</div>'
                       : '<span class="hint">shop overhead</span>') + '</td>' +
      '<td>' + esc(e.method) + '</td>' +
      '<td class="num neg">' + money(e.amount) + '</td>' +
      '<td class=num>' + (e.is_spoilage
        ? '<a class="btn sm ghost" href="#/spoiled">Spoiled work</a>'
        : '<button class="btn sm ghost" data-edit-expense="' + e.id + '">Edit</button> ' +
          '<button class="btn sm ghost" data-del-expense="' + e.id + '" data-del-amount="' + e.amount + '">Remove</button>') +
      '</td></tr>').join('')
      : '<tr><td colspan="7">' + emptyState('No expenses recorded',
        'Paper, ink, transport, rent — writing these down is what turns takings into profit.',
        '<button class="btn primary" data-action="new-expense">+ Record the first one</button>') + '</td></tr>') +
    '</tbody></table></div></div>' +
    '<p class="hint">Money charged to a job comes off that job’s profit. Everything else is shop overhead and only shows in the month-by-month table.</p>';
  restoreFocus();
}
function weekStart() {
  const d = new Date(Date.now() - 6 * 86400000);
  return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
}

/* ------------------------------------------------------------- shop & devices
   The counter's own screen: where the book lives on the Wi-Fi, how a phone gets it,
   who is signed in, and whether the book has been copied lately. Everything here is
   about the shop, not about a job. */
async function viewShop() {
  const shop = await api('/api/shop');
  let devices = [];
  try { devices = (await api('/api/devices')).devices || []; } catch (e) { /* hosted or signed out */ }
  let guard = null;
  try { guard = await api('/api/security'); } catch (e) { /* a device that is not signed in */ }
  const login = shop.login, copy = shop.backup;
  const address = shop.address;
  const shopMac = !!(S.session || {}).this_is_the_shop_computer;
  topbar('Shop & devices',
    (shop.secure ? 'HTTPS' : 'plain HTTP') + ' · ' + devices.length + ' signed in · ' +
    (copy.at ? 'book copied ' + fdate(copy.at.slice(0, 10)) : 'the book has never been copied'),
    '', [{ label: login.chosen_on_this_mac ? 'Change the password' : 'Choose a password',
          action: 'shop-password', primary: true },
         { label: 'Install page', action: 'setup-page' },
         { label: '+ New job', action: 'new-job' }]);
  $('#view').innerHTML =
    '<div class="grid kpis">' +
      stat('Shop address', '<span class="amt">' + esc(address.replace(/^https?:\/\//, '').replace(/\/$/, '')) + '</span>',
           shop.reachable_from_wifi ? 'devices on this Wi-Fi reach the book here'
                                    : 'only this Mac can reach it right now') +
      stat('Sign-in', login.required ? (login.from_environment ? 'Set by the service' : 'One shop password') : 'Not required',
           login.required ? 'every other device has to know it'
                          : 'anyone on this network could read the book',
           login.required ? '' : 'warn') +
      stat('Signed-in devices', devices.length, devices.length
           ? 'phones and laptops holding the book open' : 'nothing but this Mac') +
      stat('Copies of the book', copy.copies || 0,
           copy.auto ? 'by itself every ' + cadenceWords(copy.every_hours)
                     : 'only when the shop asks for one',
           (copy.stale || !copy.auto) ? 'warn' : '') +
    '</div>' +
    (login.required ? '' : '<div class="card warn-card"><div class="card-b">' +
      '<b>The book is open to anything on this Wi-Fi.</b>' +
      '<p class="hint">Choose one shop password and a phone left on the counter stops being the ' +
      'whole ledger. It is kept as a scrambled hash inside the book file, never as the word itself, ' +
      'and every device is signed out whenever it changes.</p>' +
      '<button class="btn primary" data-action="shop-password">Choose the shop password</button>' +
      '</div></div>') +
    '<div class="grid split">' +
    '<div class="card"><h3>Put the book on a device</h3><div class="card-b">' +
      '<p class="hint">A phone or laptop opens one page, trusts the shop certificate once, then ' +
      'keeps the app on its home screen. The records stay on this Mac — the device only ever ' +
      'holds a window onto them.</p>' +
      '<p><a class="btn primary" data-action="setup-page" href="/setup">Open the install page</a></p>' +
      (shop.secure
        ? '<p class="hint">This address is HTTPS, so a browser will allow it to be installed.' +
          (shop.certificate ? ' The shop certificate is on that page, with its fingerprint.' : ' No certificate file has been made yet; run <code>tools/shop-server.sh certs</code> on this Mac.')
        + '</p>'
        : '<p class="err">Plain HTTP will open the book but will not let a device install it or keep ' +
          'it offline. Run <code>tools/shop-server.sh install</code> on this Mac for the HTTPS address.</p>') +
      '<p class="hint">' + (shop.reachable_from_wifi
        ? 'Devices on this Wi-Fi can reach the book now.'
        : 'Only this Mac can reach the book. Start the shop server with <code>tools/shop-server.sh install</code> to let the counter’s phone in.') + '</p>' +
    '</div></div>' +
    '<div class="card"><h3>Kept awake for the shop<span class="spacer"></span></h3><div class="card-b">' +
      '<p class="hint">' + (shop.awake
        ? 'This Mac is being held awake while the book is served, so a phone on the counter can always reach it.'
        : 'If this Mac sleeps, every installed device goes quiet with it. The always-on server asks ' +
          'for it to stay awake; this window was opened without that request.') + '</p>' +
    '</div></div>' +
    '</div>' +
    '<div class="card"><h3>Signed in on these devices<span class="spacer"></span>' +
      '<button class="btn sm ghost" data-action="shop-devices">Refresh</button>' +
      (devices.length ? '<button class="btn sm ghost" data-action="sign-out-all-devices">Throw out every other one</button>' : '') +
      '</h3>' +
      '<div class="tablewrap"><table><thead><tr><th>Device</th><th>Since</th><th>Last used</th><th></th></tr></thead><tbody>' +
      (devices.length ? devices.map((d) => '<tr><td><span class="strong">' + esc(d.device || 'a device') + '</span></td>' +
        '<td class="ref">' + fdate((d.created_at || '').slice(0, 10)) + '</td>' +
        '<td class="ref">' + fdate((d.last_seen || '').slice(0, 10)) + '</td>' +
        '<td class="num"><button class="btn sm ghost" data-device-revoke="' + d.id + '">Sign out</button></td></tr>').join('')
        : '<tr><td colspan="4" class="hint">No other device is signed in. Once a phone installs the ' +
          'app and knows the password, it appears here — and can be thrown out from here.</td></tr>') +
      '</tbody></table></div>' +
      '<p class="hint">A device stays signed in for ' + plural(guard ? guard.session_days : 30, 'day') +
      ' of ordinary use, then asks again. Signing one out here takes effect the next time it asks ' +
      'for something.</p></div>' +
    '<div class="card"><h3>The shop password<span class="spacer"></span></h3><div class="card-b">' +
      '<p class="hint">' + (login.from_environment
        ? 'This server takes its password from the environment (<code>CHRISPHICS_AUTH_PASSWORD</code>), ' +
          'so it cannot be changed from here. Remove it from the service settings to choose one in the app.'
        : login.required
          ? 'Chosen on this Mac and kept in the book as a hash. Anyone who knows it can read the ' +
            'records, so treat it like the till key: hand it to staff, not to a screen.'
          : 'Nothing is set. Until it is, the book is readable by any device that finds the address.') + '</p>' +
      '<p>' + (login.from_environment ? ''
        : '<button class="btn" data-action="shop-password">' +
          (login.required ? 'Change the shop password' : 'Choose the shop password') + '</button> ' +
          (login.required && !shop.reachable_from_wifi
            ? '<button class="btn ghost" data-action="shop-password-off">Switch sign-in off</button>' : '')) + '</p>' +
    '</div></div>' +
    copiesCard(copy, shopMac) +
    securityCard(guard, shopMac);
  wireShopForms();
  restoreFocus();
}

/* Two cards the shop reads once a day and changes rarely: whether the book is being copied by
   itself, and who is allowed to open it. Both are settled on the shop's own Mac — a phone on the
   counter can read the answer but cannot quietly change the rules. */
function plural(n, word) {
  const count = Number(n || 0);
  return count + ' ' + word + (count === 1 ? '' : 's');
}

function cadenceWords(hours) {
  const every = Number(hours || 24);
  return every % 24 === 0 ? plural(every / 24, 'day') : plural(every, 'hour');
}

function copiesCard(copy, shopMac) {
  const every = copy.every_hours || 24;
  const cadence = cadenceWords(every);
  const recent = (copy.recent || []).map((r) =>
    '<tr><td class="ref">' + esc(r.at) + '</td>' +
    '<td>' + (r.kind === 'manual' ? 'asked for' : 'by itself') + '</td>' +
    '<td class="num">' + Math.round((r.bytes || 0) / 1024) + ' kB</td>' +
    '<td class="ref">' + esc(r.name) + '</td></tr>').join('');
  return '<div class="card"><h3>Copies of the book<span class="spacer"></span>' +
    '<button class="btn sm ghost" data-action="book-copy">Copy it now</button>' +
    '<button class="btn sm ghost" data-action="backup">Take a copy out</button></h3><div class="card-b">' +
    (copy.at
      ? '<p class="hint">Last copy: ' + (copy.file ? '<b>' + esc(copy.file) + '</b> ' : '') +
        esc(copy.at) + (copy.note ? ' — ' + esc(copy.note) : '') + '.</p>'
      : '<p class="hint">The book has not been copied yet. One file holds every job, client and ' +
        'payment, so a failed disk takes the whole account book with it.</p>') +
    (copy.error
      ? '<p class="err">The last automatic copy did not finish (' + esc(copy.error_at || '') + '): ' +
        esc(copy.error) + '</p>' : '') +
    (copy.auto
      ? (copy.due
        ? '<p class="err">A copy is overdue — the server is running, so it should have made one by'
          + ' now. It will try again within five minutes.</p>'
        : '<p class="hint">Copied by itself every ' + cadence + '; the next one is due in ' +
          (copy.next_in_hours === null ? '—' : plural(Math.max(0, Math.round(copy.next_in_hours * 10) / 10), 'hour')) + '.</p>')
      : '<p class="err">Automatic copying is switched off. The book exists in one place only until'
        + ' it is switched back on or a copy is asked for by hand.</p>') +
    '<p class="hint">Folder: <code>' + esc(copy.folder) + '</code>' +
    (copy.free_gb !== null && copy.free_gb !== undefined ? ' · ' + copy.free_gb + ' GB free there' : '') + '.</p>' +
    (copy.mirror_set
      ? '<p class="hint">Second destination: <code>' + esc(copy.mirror) + '</code>' +
        (copy.mirror_dir ? ' — each new copy is written there too.'
                         : ' — that folder is not there right now, so nothing is being mirrored.') + '</p>'
      : '<p class="hint">No second destination is set, so both the book and its copies live on this'
        + ' one Mac. An external disk plugged in now and then is the cheapest insurance there is.</p>') +
    (shopMac
      ? '<form id="copiesForm" class="stack">' +
        '<label class="consent wide"><input type="checkbox" name="auto"' + (copy.auto ? ' checked' : '') + '>' +
        '<span>Copy the book by itself while the server is running</span></label>' +
        '<div class="grid split">' +
        '<label class="field"><span>Every (hours)</span><input name="every_hours" type="number" min="1" max="168"' +
        ' step="1" value="' + every + '"></label>' +
        '<label class="field"><span>Keep night copies for (days)</span><input name="keep_days" type="number"' +
        ' min="3" max="120" step="1" value="' + (copy.keep_days || 14) + '"></label>' +
        '</div>' +
        '<label class="field wide"><span>A second folder (optional)</span>' +
        '<input name="mirror" type="text" maxlength="300" placeholder="/Volumes/Backup/Chrisphics"' +
        ' value="' + esc(copy.mirror || '') + '"></label>' +
        '<p class="hint">One copy for each month is kept for a year beyond those days, and the last' +
        ' twelve copies asked for by hand. Each one is opened and counted against the book before any' +
        ' older copy is cleared.</p>' +
        '<div class="err" id="copiesErr"></div>' +
        '<button class="btn primary">Save the copying</button></form>'
      : '<p class="hint">How often the book is copied, and where the copies go, is settled on the' +
        ' shop’s own computer.</p>') +
    (recent ? '<div class="tablewrap"><table><thead><tr><th>When</th><th>Why</th><th class="num">Size</th>' +
      '<th>File</th></tr></thead><tbody>' + recent + '</tbody></table></div>' : '') +
    '</div></div>';
}

function securityCard(guard, shopMac) {
  if (!guard) {
    return '<div class="card"><h3>Security of this book</h3><div class="card-b">' +
      '<p class="hint">The security of the book is read from the shop’s own computer, or after' +
      ' signing in. Nothing here is missing — this device simply has not been asked to show it.</p>' +
      '</div></div>';
  }
  const trail = (guard.trail || []).slice(0, 12).map((row) =>
    '<tr><td class="ref">' + esc(row.at) + '</td><td><span class="strong">' + esc(row.kind) + '</span></td>' +
    '<td class="ref">' + esc(row.address || '') + '</td><td class="ref">' + esc(row.detail || '') + '</td></tr>').join('');
  return '<div class="card"><h3>Security of this book<span class="spacer"></span>' +
    '<button class="btn sm ghost" data-action="shop-security">Refresh</button></h3><div class="card-b">' +
    '<p class="hint">Sign-in: <b>' + (guard.required ? (guard.from_environment
      ? 'set by the hosting service' : 'one shop password') : 'not required') + '</b>' +
    (guard.required ? ' — the word is kept only as a salted hash, run through ' +
      (guard.rounds / 1000) + ' thousand rounds.' : ' — choose a shop password above.') + '</p>' +
    '<p class="hint">' + (guard.https
      ? 'This address is HTTPS, so a password crossing the Wi-Fi is encrypted on the way.'
      : '<span class="err">Plain HTTP: a password typed here crosses the Wi-Fi in the clear.'
        + '</span> Run <code>tools/shop-server.sh install</code> on this Mac.') + '</p>' +
    '<p class="hint">' + plural(guard.allowed_fails, 'wrong password') + ' in a row and that device' +
    ' is made to wait, doubling each time up to a quarter of an hour. More than ' +
    plural(guard.calls_per_minute, 'call') + ' in one minute from one address is answered with a' +
    ' wait, so a script cannot bury the counter’s own screen.</p>' +
    '<p class="hint">' + (guard.loopback_open
      ? 'This Mac is the shop’s own computer and is not asked for the password — the counter’s'
        + ' keyboard is treated as a key in the till.'
      : 'Even this Mac is asked for the password, because the shop switched that on below.') + '</p>' +
    (shopMac
      ? '<form id="securityForm" class="stack">' +
        '<label class="field"><span>Days a device stays signed in</span><input name="session_days"' +
        ' type="number" min="1" max="90" step="1" value="' + guard.session_days + '"></label>' +
        '<label class="consent wide"><input type="checkbox" name="shop_mac_signin"' +
        (guard.shop_mac_signin ? ' checked' : '') + '><span>Ask this Mac for the password too' +
        ' (its own Settings windows then save only after the page has signed in)</span></label>' +
        '<div class="err" id="securityErr"></div>' +
        '<button class="btn primary">Save the security</button></form>'
      : '<p class="hint">How long a device stays signed in, and whether this Mac is asked as well,' +
        ' are settled on the shop’s own computer.</p>') +
    '<h4>What the book remembers of being opened</h4>' +
    (trail ? '<div class="tablewrap"><table><thead><tr><th>When</th><th>What</th><th>From</th>' +
      '<th>Note</th></tr></thead><tbody>' + trail + '</tbody></table></div>'
      : '<p class="hint">Nothing has been written yet. Every sign-in, every wrong password, every'
        + ' copy taken out and every device thrown out goes here from now on.</p>') +
    '<p class="hint">The last few hundred events only, and never a password, a half-typed one, or a' +
    ' device’s cookie — only what happened, when, and from which address.</p>' +
    '</div></div>';
}

function wireShopForms() {
  const copies = document.getElementById('copiesForm');
  if (copies) {
    copies.addEventListener('submit', async (e) => {
      e.preventDefault();
      const f = e.target;
      document.getElementById('copiesErr').textContent = '';
      try {
        await api('/api/backups', { method: 'PUT', body: JSON.stringify({
          auto: !!f.auto.checked,
          every_hours: f.every_hours.value,
          keep_days: f.keep_days.value,
          mirror: f.mirror.value.trim(),
        }) });
        toast('The book will be copied that way from now on', 'good');
        await render();
      } catch (err) { document.getElementById('copiesErr').textContent = err.message; }
    });
  }
  const security = document.getElementById('securityForm');
  if (security) {
    security.addEventListener('submit', async (e) => {
      e.preventDefault();
      const f = e.target;
      document.getElementById('securityErr').textContent = '';
      try {
        await api('/api/security', { method: 'PUT', body: JSON.stringify({
          session_days: f.session_days.value,
          shop_mac_signin: !!f.shop_mac_signin.checked,
        }) });
        toast('Sign-in rules saved', 'good');
        await render();
      } catch (err) { document.getElementById('securityErr').textContent = err.message; }
    });
  }
}

function passwordForm(login) {
  const changing = login.required && !login.from_environment;
  openModal(modalHeader(changing ? 'Change the shop password' : 'Choose the shop password',
    changing ? 'Every device is signed out, and each one has to be told the new password.'
             : 'Long beats clever: four ordinary words are easier to remember than a trick') +
    '<form id="shopPasswordForm">' +
    (changing ? '<label class="field wide"><span>What it is now</span>' +
      '<input name="current" type="password" autocomplete="current-password" required></label>' : '') +
    '<label class="field wide"><span>New shop password</span>' +
    '<input name="password" type="password" minlength="8" maxlength="200" autocomplete="new-password" required></label>' +
    '<label class="field wide"><span>Once more</span>' +
    '<input name="again" type="password" autocomplete="new-password" required></label>' +
    '<p class="hint">Kept in the book as a salted hash, never as the word. Staff sign in on each ' +
    'device with it; a wrong password five times makes that device wait.</p>' +
    '<div class="err" id="shopPasswordErr"></div>' +
    '<button class="btn primary">Save</button></form>');
  $('#shopPasswordForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const f = e.target;
    if (f.password.value !== f.again.value) {
      $('#shopPasswordErr').textContent = 'The two do not match yet';
      return;
    }
    $('#shopPasswordErr').textContent = '';
    try {
      await api('/api/shop-password', { method: 'POST', body: JSON.stringify({
        password: f.password.value, current: f.current ? f.current.value : '',
      }) });
      closeModal();
      toast('The shop password is set. Every other device has to be told.', 'good');
      await render();
    } catch (err) { $('#shopPasswordErr').textContent = err.message; }
  });
}

function passwordOffForm(login) {
  openModal(modalHeader('Switch sign-in off', 'Only do this if every device is inside the shop'),
    '<form id="shopPasswordOffForm">' +
    '<label class="field wide"><span>The shop password</span>' +
    '<input name="current" type="password" autocomplete="current-password" required></label>' +
    '<p class="hint">Any device that can reach this address will then read the whole book without ' +
    'asking — including a stranger’s phone on the same Wi-Fi.</p>' +
    '<div class="err" id="shopOffErr"></div><button class="btn">Switch it off</button></form>');
  $('#shopPasswordOffForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    $('#shopOffErr').textContent = '';
    try {
      await api('/api/shop-password', { method: 'DELETE',
        body: JSON.stringify({ current: e.target.current.value }) });
      closeModal();
      toast('Sign-in is off. The book is open on this network.', 'warn');
      await render();
    } catch (err) { $('#shopOffErr').textContent = err.message; }
  });
}

async function viewSpoiled() {
  const rows = await api('/api/spoiled');
  const total = rows.reduce((sum, row) => sum + Number(row.amount || 0), 0);
  const quantity = rows.reduce((sum, row) => sum + Number(row.quantity || 0), 0);
  topbar('Spoiled work',
    rows.length + ' record' + (rows.length === 1 ? '' : 's') + ' · ' +
      quantity + ' item' + (quantity === 1 ? '' : 's') + ' · cost ' + money(total),
    '', [{ label: '+ Log spoiled work', action: 'new-spoilage', primary: true }]);
  $('#view').innerHTML = '<div class="card"><div class="tablewrap"><table><thead><tr>' +
    '<th>Date</th><th>Job</th><th>Client</th><th class=num>Qty</th><th>Reason</th>' +
    '<th class=num>Cost</th><th></th></tr></thead><tbody>' +
    (rows.length ? rows.map((row) => '<tr>' +
      '<td class="ref">' + fdate(row.spoiled_on) + '</td>' +
      '<td><a href="#/jobs/' + row.job_id + '">' + esc(row.ref) + '</a><div class="ref">' + esc(row.job_title) + '</div></td>' +
      '<td>' + esc(row.client) + '</td>' +
      '<td class=num>' + Number(row.quantity).toLocaleString('en-US') + '</td>' +
      '<td>' + esc(row.reason) + '</td>' +
      '<td class="num neg">' + money(row.amount) + '</td>' +
      '<td class=num><button class="btn sm ghost" data-edit-spoilage="' + row.id + '">Edit</button> ' +
        '<button class="btn sm ghost" data-del-spoilage="' + row.id + '" data-del-quantity="' + row.quantity +
        '" data-del-job="' + esc(row.ref) + '">Remove</button></td></tr>').join('')
      : '<tr><td colspan="7">' + emptyState('No spoiled work recorded',
        'Log spoiled items against a job. Any cost you enter is added to that job’s costs and reduces its profit.',
        '<button class="btn primary" data-action="new-spoilage">+ Log spoiled work</button>') + '</td></tr>') +
    '</tbody></table></div></div>' +
    '<p class="hint">Spoilage costs are recorded as job expenses and included in each job’s profit. Review the reason and quantity here.</p>';
  restoreFocus();
}
function spoilageForm(record, jobs, jobId) {
  const row = record || {};
  const chosen = record ? record.job_id : jobId;
  openModal(modalHeader(record ? 'Edit spoiled work' : 'Log spoiled work',
    'Record the affected job, quantity, reason and extra cost.') +
    '<form id="spoilageForm" data-spoilage-id="' + (record ? record.id : '') + '"><div class="f-grid">' +
    '<label class="field wide"><span>Job *</span><select name="job_id" required>' +
    '<option value="">Choose a job</option>' +
    jobs.map((j) => '<option value="' + j.id + '"' + (String(chosen) === String(j.id) ? ' selected' : '') + '>' +
      esc(j.ref) + ' · ' + esc(j.client) + ' · ' + esc(j.title) + '</option>').join('') +
    '</select></label>' +
    '<label class="field"><span>Quantity spoiled *</span><input name="quantity" type="number" min="1" step="1" required value="' +
      esc(row.quantity || '') + '" placeholder="e.g. 25"></label>' +
    '<label class="field"><span>Extra cost *</span><input name="amount" type="number" min="0" step="0.01" required value="' +
      esc(row.amount === undefined ? '' : n2(row.amount)) + '" placeholder="0.00"></label>' +
    '<label class="field"><span>Date spoiled</span><input name="spoiled_on" type="date" value="' +
      esc(day10(row.spoiled_on || todayISO())) + '"></label>' +
    '<label class="field wide"><span>Reason *</span><textarea name="reason" maxlength="500" rows="3" required placeholder="Describe what went wrong">' +
      esc(row.reason || '') + '</textarea></label>' +
    '</div><p class="hint">The cost is included in the selected job’s expenses and profit calculation. Enter 0 if no extra cost was incurred.</p>' +
    '<div class="err" id="spoilageErr"></div><div class="modal-foot"><span class="spacer"></span>' +
    '<button type="button" class="btn" data-action="close-modal">Cancel</button>' +
    '<button class="btn primary">' + (record ? 'Save changes' : 'Record spoilage') + '</button></div></form>');
}
async function viewSyncQueue() {
  const rows = (await offlineAll('outbox')).sort((a, b) => b.createdAt.localeCompare(a.createdAt));
  const pending = rows.filter((row) => row.state === 'pending');
  const conflicts = rows.filter((row) => row.state === 'conflict');
  topbar('Pending sync',
    pending.length + ' waiting · ' + conflicts.length + ' need review · ' +
      rows.filter((row) => row.state === 'synced').length + ' synced on this device',
    '', [{ label: 'Sync now', action: 'sync-now', primary: true }]);
  $('#view').innerHTML = '<p class="hint">Changes saved while disconnected stay on this device until it can reach the shop book. ' +
    'Reconnect to the shop Wi-Fi to sync. The screens show the last saved copy until queued changes have synced.</p>' +
    (rows.length ? rows.map((row) => {
      const conflict = row.state === 'conflict';
      const synced = row.state === 'synced';
      return '<div class="card sync-entry"><h3>' + esc(row.method + ' ' + row.path) +
        '<span class="spacer"></span>' + (conflict ? '<span class="pill s-Cancelled">Needs review</span>' :
          synced ? '<span class="pill s-Delivered">Synced</span>' : '<span class="pill s-Pending">Waiting</span>') +
        '</h3><div class="card-b"><p class="ref">' + esc(fdatetime(row.createdAt)) + '</p>' +
        '<details><summary>Your saved change</summary><pre>' + esc(JSON.stringify(row.body, null, 2)) + '</pre></details>' +
        (conflict ? '<p class="hint">' + esc(row.error) + '</p>' +
          (row.current ? '<details open><summary>Current shared record</summary><pre>' +
            esc(JSON.stringify(row.current, null, 2)) + '</pre></details>' : '') +
          '<div class="row-actions">' +
            (row.currentEtag ? '<button class="btn sm primary" data-sync-keep="' + esc(row.operationId) + '">Apply my saved change</button>' : '') +
            '<button class="btn sm" data-sync-discard="' + esc(row.operationId) + '">Keep shared version / discard mine</button>' +
          '</div>' : '') +
        '</div></div>';
    }).join('') : emptyState('Nothing waiting to sync',
      'This device has no offline changes. It will keep a local copy of screens you have opened for offline viewing.'));
  restoreFocus();
}
function expenseForm(expense, presetJob, jobs) {
  const e = expense || {};
  const list = jobs || [];
  openModal(modalHeader(expense ? 'Edit this expense' : 'Money out',
    'Costs that belong to a job make its profit honest.') +
    '<form id="expenseForm" data-expense-id="' + (expense ? expense.id : '') + '"><div class="f-grid">' +
    '<label class="field"><span>Amount *</span><input name="amount" type="number" min="0.01" step="0.01" required placeholder="0.00" value="' + esc(e.amount ? n2(e.amount) : '') + '"></label>' +
    '<label class="field"><span>What it was</span><select name="category">' + S.boot.expense_categories.map((c) =>
      '<option' + ((e.category || 'Other') === c ? ' selected' : '') + '>' + esc(c) + '</option>').join('') + '</select></label>' +
    '<label class="field"><span>Paid to</span><input name="payee" maxlength="160" placeholder="supplier, landlord, driver…" value="' + esc(e.payee || '') + '"></label>' +
    '<label class="field"><span>Method</span><select name="method">' + S.boot.methods.map((m) =>
      '<option' + ((e.method || 'Cash') === m ? ' selected' : '') + '>' + esc(m) + '</option>').join('') + '</select></label>' +
    '<label class="field"><span>Date</span><input name="spent_on" type="date" value="' + esc(day10(e.spent_on || todayISO())) + '"></label>' +
    '<label class="field"><span>Transaction ref</span><input name="reference" maxlength="80" placeholder="MoMo / bank id" value="' + esc(e.reference || '') + '"></label>' +
    '<label class="field wide"><span>Which job was this for?</span><select name="job_id">' +
    '<option value="">Shop overhead — not a specific job</option>' +
    list.map((j) => '<option value="' + j.id + '"' + (String(e.job_id) === String(j.id) || (presetJob && String(presetJob) === String(j.id)) ? ' selected' : '') + '>' +
      esc(j.ref) + ' · ' + esc(j.client) + ' · ' + esc(j.title) + '</option>').join('') + '</select></label>' +
    '<label class="field wide"><span>Note</span><input name="note" maxlength="500" placeholder="what it bought" value="' + esc(e.note || '') + '"></label>' +
    '</div><div class="err" id="expenseErr"></div><div class="modal-foot"><span class="spacer"></span>' +
    '<button type="button" class="btn" data-action="close-modal">Cancel</button>' +
    '<button class="btn primary">' + (expense ? 'Save changes' : 'Record it') + '</button></div></form>');
}
async function openLeadDrawer(id) {
  let l;
  try { l = await api('/api/leads/' + id); } catch (e) { return fail(e); }
  $('#drawerBody').innerHTML =
    '<div class="drawer-h"><div><div class="ref">enquiry · asked ' + fdatetime(l.created_at) + '</div>' +
    '<h2>' + esc(l.name) + '</h2>' +
    '<div style="margin-top:6px">' + stagePill(l.stage) + ' <span class="tag">' + esc(l.source) + '</span></div></div>' +
    '<span class="spacer"></span><button class="iconbtn" data-action="close-drawer" aria-label="Close">✕</button></div>' +
    '<div class="row-actions">' +
    '<button class="btn primary" data-edit-lead="' + l.id + '">Edit enquiry</button>' +
    (l.stage === 'Won' ? '' : '<button class="btn" data-convert-lead="' + l.id + '">Book it as a job</button>') +
    (l.phone ? '<a class="btn" href="tel:' + esc(l.phone) + '">Call</a>' : '') +
    (l.whatsapp ? '<a class="btn" href="https://wa.me/' + esc(String(l.whatsapp).replace(/[^0-9]/g, '')) + '" target="_blank" rel="noopener">WhatsApp</a>' : '') +
    '<button class="btn ghost danger" data-del-lead="' + l.id + '" data-del-name="' + esc(l.name) + '">Delete</button></div>' +
    '<div class="amounts">' +
    '<div><span>Estimated value</span><b>' + money(l.value) + '</b></div>' +
    '<div><span>Stage</span><b>' + esc(l.stage) + '</b></div>' +
    '<div><span>Follow up</span><b>' + (l.follow_up ? fdate(l.follow_up) : 'not set') + '</b></div>' +
    '</div>' +
    '<div><div class="section-h">Move this enquiry to</div><div class="steps" style="margin-top:9px">' +
    S.boot.lead_stages.map((s) => '<button class="step' + (s === l.stage ? ' on' : '') + '" data-lead-stage="' + esc(s) +
      '" data-stage-lead="' + l.id + '">' + esc(s) + '</button>').join('') + '</div></div>' +
    '<div class="kv-grid">' +
    '<div><span>Phone</span><b>' + (l.phone ? '<a href="tel:' + esc(l.phone) + '">' + esc(l.phone) + '</a>' : '—') + '</b></div>' +
    '<div><span>WhatsApp</span><b>' + esc(l.whatsapp || '—') + '</b></div>' +
    '<div><span>Email</span><b>' + (l.email ? '<a href="mailto:' + esc(l.email) + '">' + esc(l.email) + '</a>' : '—') + '</b></div>' +
    '<div><span>Interested in</span><b>' + esc(l.interest || '—') + '</b></div>' +
    (l.client ? '<div><span>On the book as</span><b><a href="#/clients/' + l.client_id + '">' + esc(l.client) + '</a></b></div>' : '') +
    (l.ref ? '<div><span> Became</span><b><a href="#/jobs/' + l.job_id + '">' + esc(l.ref) + ' · ' + esc(l.job_title) + '</a></b></div>' : '') +
    '</div>' +
    (l.note ? '<div><div class="section-h">Note</div><p style="margin:8px 0 0">' + esc(l.note) + '</p></div>' : '');
  $('#drawer').hidden = false;
  $('#drawerBody').scrollTop = 0;
}
function leadForm(lead) {
  const l = lead || {};
  const inDays = (n) => {
    const d = new Date(Date.now() + n * 86400000);
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
  };
  openModal(modalHeader(lead ? 'Edit the enquiry' : 'Someone asked about printing',
    'Write it down now, chase it later, turn a yes into a job.') +
    '<form id="leadForm" data-lead-id="' + (lead ? lead.id : '') + '"><div class="f-grid">' +
    '<label class="field wide"><span>Who asked *</span><input name="name" required maxlength="160" placeholder="Name or business" value="' + esc(l.name || '') + '"></label>' +
    '<label class="field"><span>Phone</span><input name="phone" maxlength="40" placeholder="024…" value="' + esc(l.phone || '') + '"></label>' +
    '<label class="field"><span>WhatsApp</span><input name="whatsapp" maxlength="40" value="' + esc(l.whatsapp || '') + '"></label>' +
    '<label class="field"><span>Email</span><input name="email" type="email" maxlength="160" value="' + esc(l.email || '') + '"></label>' +
    '<label class="field"><span>How they reached us</span><select name="source">' + S.boot.lead_sources.map((s) =>
      '<option' + ((l.source || 'Walk-in') === s ? ' selected' : '') + '>' + esc(s) + '</option>').join('') + '</select></label>' +
    '<label class="field wide"><span>What they want printed</span><input name="interest" maxlength="200" placeholder="e.g. 400 conference flyers and a roll-up" value="' + esc(l.interest || '') + '"></label>' +
    '<label class="field"><span>Rough value of the work</span><input name="value" type="number" min="0" step="0.01" placeholder="0.00" value="' + esc(l.value ? n2(l.value) : '') + '"></label>' +
    '<label class="field"><span>Stage</span><select name="stage">' + S.boot.lead_stages.map((s) =>
      '<option' + ((l.stage || 'Prospect') === s ? ' selected' : '') + '>' + esc(s) + '</option>').join('') + '</select></label>' +
    '<label class="field"><span>Chase again on</span><input name="follow_up" type="date" value="' + esc(day10(l.follow_up || (lead ? '' : inDays(3)))) + '"></label>' +
    '<label class="field wide"><span>Note</span><textarea name="note" rows="2" placeholder="Budget, deadline, who referred them…">' + esc(l.note || '') + '</textarea></label>' +
    '</div><div class="err" id="leadErr"></div><div class="modal-foot"><span class="spacer"></span>' +
    '<button type="button" class="btn" data-action="close-modal">Cancel</button>' +
    '<button class="btn primary">' + (lead ? 'Save changes' : 'Save enquiry') + '</button></div></form>');
}
/* WKWebView has no prompt(), so the details of the booked work are collected in a form. */
function convertLeadForm(l) {
  openModal(modalHeader('Turn this enquiry into work',
    (l.client ? esc(l.name) + ' is already on the book as ' + esc(l.client) + '.'
              : 'Adds ' + esc(l.name) + ' as a client, then books the job.')) +
    '<form id="convertLeadForm" data-lead-id="' + l.id + '"><div class="f-grid">' +
    '<label class="field wide"><span>What is being printed *</span><input name="title" required maxlength="200" value="' +
      esc(l.interest || '') + '" placeholder="e.g. 400 conference flyers, A5 double sided"></label>' +
    '<label class="field"><span>Service</span><select name="category">' + S.boot.categories.map((c) => '<option>' + esc(c) + '</option>').join('') + '</select></label>' +
    '<label class="field"><span>Quantity</span><input name="quantity" type="number" min="1" step="1" value="1"></label>' +
    '<label class="field"><span>Unit</span><select name="unit">' + S.boot.units.map((u) => '<option>' + esc(u) + '</option>').join('') + '</select></label>' +
    '<label class="field"><span>Price each</span><input name="unit_price" type="number" min="0" step="0.01" placeholder="0.00" value="' + esc(l.value ? n2(l.value) : '') + '"></label>' +
    '<label class="field"><span>Ready by</span><input name="due_date" type="date" value=""></label>' +
    '<label class="field"><span>Priority</span><select name="priority"><option>Normal</option><option>Urgent</option></select></label>' +
    '</div>' +
    '<div class="calc"><div class="line total"><span>Job total</span><b id="cvTotal">—</b></div></div>' +
    '<div class="err" id="convertErr"></div><div class="modal-foot"><span class="spacer"></span>' +
    '<button type="button" class="btn" data-action="close-modal">Cancel</button>' +
    '<button class="btn primary">Book the job</button></div></form>');
}

/* ----------------------------------------------------------------- accounts */
async function viewAccounts(r) {
  const d = await api('/api/accounts?' + qs({ from: r.params.get('from'), to: r.params.get('to'), q: r.params.get('q'), method: r.params.get('method') }));
  const t = d.totals;
  topbar('Accounts & payments',
    money(t.receivable) + ' to collect' + (t.overdue_value > 0 ? ' · ' + money(t.overdue_value) + ' on late jobs' : ''),
    '', [{ label: '+ Receive payment', action: 'receive-payment', primary: true },
         { label: '+ Record expense', action: 'new-expense' }]);
  $('#topbar').insertAdjacentHTML('beforeend', filtersBar('accounts', [
    { type: 'search', key: 'q', placeholder: 'Search payer, job ref or transaction id' },
    { type: 'date', key: 'from', label: 'paid' }, { type: 'date', key: 'to', label: 'to' },
    { type: 'select', key: 'method', options: [['', 'Any method']].concat(S.boot.methods.map((m) => [m, m])) },
  ]));
  $('#view').innerHTML = '<div class="grid kpis">' +
    stat('Owed on jobs', money(t.receivable), 'sum of open job balances', 'amt warn') +
    stat('Received (this filter)', money(d.ledger.total), pluralise(d.ledger.count, 'entry'), 'amt good') +
    stat('Paid out this month', money(d.spent_total.this_month), money(d.spent_total.all_time) + ' all time', 'amt') +
    stat('Credit on account', money((d.credit || []).reduce((a, c) => a + c.credit, 0)), 'money held, not applied to a job', 'amt') +
    '</div>' +
    cardTable('Outstanding — who owes what', '<button class="btn sm" data-action="receive-payment">Receive payment</button>',
      d.debtors.length ? d.debtors.map((j) => '<tr data-open="jobs/' + j.id + '">' +
        '<td><span class="strong">' + esc(j.title) + '</span><div class="ref">' + esc(j.ref) + ' · ' + esc(j.category) + '</div></td>' +
        '<td>' + esc(j.client) + (j.phone ? '<div class="ref">' + esc(j.phone) + '</div>' : '') + '</td>' +
        '<td>' + pill(j.status) + '</td><td>' + dueLabel(j) + '</td>' +
        '<td class=num>' + money(j.total) + '</td><td class=num>' + money(j.paid) + '</td>' +
        '<td class="num balance neg">' + money(j.balance) + '</td>' +
        '<td class=num><button class="btn sm primary" data-pay-job="' + j.id + '" data-pay-name="' + esc(j.client) +
        '" data-pay-amount="' + j.balance + '">Get ' + compact(j.balance) + '</button></td></tr>').join('')
        : '<tr><td colspan="8" class="hint">No outstanding balances. Everything booked has been paid.</td></tr>',
      ['Job', 'Client', 'Status', 'Due', 'Total', 'Paid', 'Balance', '']) +
    cardTable('Paid in full — settled jobs', '',
      d.settled.length ? d.settled.map((j) => '<tr data-open="jobs/' + j.id + '">' +
        '<td><span class="strong">' + esc(j.title) + '</span><div class="ref">' + esc(j.ref) + ' · ' + esc(j.category) + '</div></td>' +
        '<td>' + esc(j.client) + (j.phone ? '<div class="ref">' + esc(j.phone) + '</div>' : '') + '</td>' +
        '<td>' + pill(j.status) + ' <span class="pill k-paid">Paid in full</span></td>' +
        '<td class=num>' + money(j.total) + '</td><td class="num pos">' + money(j.paid) + '</td>' +
        '<td class="ref">' + (j.settled_on ? fdate(j.settled_on) : '—') + '</td></tr>').join('')
        : '<tr><td colspan="6" class="hint">Nothing has been cleared in full yet. Take the whole balance on a job and it lands here.</td></tr>',
      ['Job', 'Client', 'Money', 'Billed', 'Collected', 'Cleared on']) +
    cardTable('Payment ledger', '<button class="btn sm ghost" data-export="payments">Export CSV</button>',
      d.ledger.rows.length ? d.ledger.rows.map((p) => '<tr>' +
        '<td class="ref">' + fdate(p.paid_at) + '<div>' + day10(p.paid_at).slice(8) + '/' + day10(p.paid_at).slice(5, 7) + '</div></td>' +
        '<td class="strong">' + esc(p.client) + (p.ref ? '<div class="ref"><a href="#/jobs/' + p.job_id + '">' + esc(p.ref) + ' · ' + esc(p.title) + '</a></div>' : '<div class="ref">on account</div>') + '</td>' +
        '<td>' + esc(p.kind) + '</td>' +
        '<td class="num' + (p.kind === 'Refund' ? ' neg' : ' pos') + '">' + money(p.kind === 'Refund' ? -p.amount : p.amount) + '</td>' +
        '<td>' + esc(p.method) + (p.reference ? '<div class="ref">' + esc(p.reference) + '</div>' : '') + '</td>' +
        '<td class="num"><button class="btn sm ghost" data-del-payment="' + p.id + '" data-del-amount="' + p.amount + '">Remove</button></td></tr>').join('')
        : '<tr><td colspan="6" class="hint">No payments in this window.</td></tr>',
      ['Date', 'Client / job', 'Type', 'Amount', 'Method', '']);
  $('#view').insertAdjacentHTML('beforeend', '<div class="grid cols-3">' +
    cardTable('In and out, month by month', '<a href="#/expenses" class="btn sm ghost">Expenses</a>',
      d.spent.length ? d.spent.map((m) => '<tr><td class="ref">' + monthName(m.month + '-01') + '</td>' +
        '<td class="num pos">' + money(m.income) + '</td><td class="num neg">' + money(m.spent) + '</td>' +
        '<td class="num' + (m.net < 0 ? ' neg' : ' pos') + '">' + money(m.net) + '</td></tr>').join('')
        : '<tr><td colspan="4" class="hint">No money has moved yet.</td></tr>',
      ['Month', 'In', 'Out', 'Net']) +
    (d.credit.length ? cardTable('Clients with credit on account', '',
      d.credit.map((c) => '<tr data-open="clients/' + c.id + '"><td class="strong">' + esc(c.name) + '</td>' +
        '<td class="num pos">' + money(c.credit) + '</td></tr>').join(''), ['Client', 'Credit']) : '') +
    '</div>');
  restoreFocus();
}

/* ------------------------------------------------------------------ reports */
async function viewReports(r) {
  const p = r.params;
  const from = p.get('from') || todayISO().slice(0, 8) + '01';
  const to = p.get('to') || todayISO();
  const period = p.get('period') || 'day';
  const d = await api('/api/report?from=' + from + '&to=' + to + '&period=' + period);
  const t = d.totals;
  topbar('Reports', esc(from) + ' to ' + esc(to));
  $('#topbar').insertAdjacentHTML('beforeend', filtersBar('reports', [
    { type: 'date', key: 'from', label: 'from', def: from }, { type: 'date', key: 'to', label: 'to', def: to },
    { type: 'tabs', key: 'period', def: 'day', options: [{ v: 'day', label: 'Daily' }, { v: 'week', label: 'Weekly' }, { v: 'month', label: 'Monthly' }] },
  ]));
  const maxB = Math.max.apply(null, d.buckets.map((b) => b.billed).concat([1]));
  const maxSpend = Math.max.apply(null, d.spend_by_category.map((c) => c.amount).concat([1]));
  const dueTotal = d.buckets.reduce((a, b) => a + b.still_due, 0);
  $('#view').innerHTML = '<div class="grid kpis">' +
    stat('Billed', money(t.billed), pluralise(t.jobs, 'job') + ' booked' + (t.quotes ? ' · ' + t.quotes + ' quoted' : ''), 'amt') +
    stat('What it cost us', money(t.cost), 'item costs plus costs booked on jobs', 'amt') +
    stat('Profit on jobs', money(t.profit), 'before rent and overhead', 'amt ' + (t.profit < 0 ? 'warn' : 'good')) +
    stat('Paid out', money(t.spent), 'everything that left the shop', 'amt') +
    '</div>' +
    '<div class="grid kpis">' +
    stat('Collected', money(t.collected), 'cash received in the range', 'amt good') +
    stat('Still owed', money(dueTotal), 'from these jobs', 'amt warn') +
    stat('Delivered', t.delivered, 'jobs closed out') +
    stat('Margin', t.billed > 0 ? Math.round((t.profit / t.billed) * 100) + '%' : '—', 'profit divided by billing') +
    '</div>' +
    '<div class="card"><h3>Billed per ' + esc(period) + '<span class="spacer"></span>' +
    '<button class="btn sm ghost" data-export="jobs">Jobs CSV</button>' +
    '<button class="btn sm ghost" data-export="items">Item lines CSV</button>' +
    '<button class="btn sm ghost" data-export="expenses">Expenses CSV</button>' +
    '<button class="btn sm ghost" data-export="accounts">Debtors CSV</button>' +
    '<button class="btn sm ghost" data-export="payments">Payments CSV</button></h3>' +
    '<div class="card-b">' + (d.buckets.length ? '<div class="bars">' + d.buckets.map((b) =>
      '<div class="bar' + (b.billed > 0 ? '' : ' zero') + '" title="' + esc(b.bucket) + ' — billed ' + esc(money(b.billed)) +
      ', profit ' + esc(money(b.profit)) + ', collected ' + esc(money(b.collected)) + '"><u>' + (b.billed > 0 ? compact(b.billed) : '') + '</u>' +
      '<i style="height:' + Math.max(Math.round((b.billed / maxB) * 100), 2) + '%"></i><s>' + esc(bucketLabel(b.bucket, period)) +
      '</s></div>').join('') + '</div>' +
      '<div class="legend"><span><i style="background:var(--brand)"></i>billed</span>' +
      '<span>' + d.buckets.length + ' ' + esc(period) + (d.buckets.length === 1 ? ' bucket' : ' buckets') + '</span></div>'
      : '<p class="hint">No jobs were booked in this range.</p>') + '</div></div>' +
    cardTable('Billed, costed and collected per ' + period, '',
      d.buckets.length ? d.buckets.map((b) => '<tr><td class="ref">' + esc(bucketLabel(b.bucket, period)) + '</td>' +
        '<td class="num">' + b.jobs + '</td><td class="num">' + money(b.billed) + '</td>' +
        '<td class="num">' + money(b.cost) + '</td>' +
        '<td class="num' + (b.profit < 0 ? ' neg' : ' pos') + '">' + money(b.profit) + '</td>' +
        '<td class="num">' + money(b.collected) + '</td>' +
        '<td class="num' + (b.still_due > 0.005 ? ' neg' : '') + '">' + money(b.still_due) + '</td></tr>').join('')
        : '<tr><td colspan="7" class="hint">Nothing booked in this range.</td></tr>',
      ['Period', 'Jobs', 'Billed', 'Cost', 'Profit', 'Collected', 'Owed']) +
    '<div class="grid cols-3">' +
    cardTable('By service', '', d.by_category.length ? d.by_category.map((c) => '<tr><td>' + esc(c.category) + '</td>' +
      '<td class="num">' + c.jobs + '</td><td class="num">' + money(c.billed) + '</td><td class="num' +
      (c.profit < 0 ? ' neg' : ' pos') + '">' + money(c.profit) + '</td><td class="num' + (c.due > 0 ? ' neg' : '') + '">' +
      money(c.due) + '</td></tr>').join('') : '<tr><td colspan="5" class="hint">Nothing booked.</td></tr>',
      ['Service', 'Jobs', 'Billed', 'Profit', 'Owed']) +
    cardTable('How people pay', '', d.methods.length ? d.methods.map((m) => '<tr><td>' + esc(m.method) + '</td>' +
      '<td class="num">' + m.n + '</td><td class="num">' + money(m.amount) + '</td></tr>').join('')
      : '<tr><td colspan="3" class="hint">No payments in this range.</td></tr>', ['Method', 'Entries', 'Amount']) +
    '<div class="card"><h3>Where the money went</h3><div class="card-b mix">' +
    (d.spend_by_category.length ? d.spend_by_category.map((c) => '<div class="row"><span>' + esc(c.category) + '</span><b>' +
      compact(c.amount) + ' · ' + c.lines + '</b><div class="track"><i class="out" style="width:' +
      Math.round((c.amount / maxSpend) * 100) + '%"></i></div></div>').join('')
      : '<p class="hint">No expenses in this range, so nothing has been subtracted from profit but the item costs.</p>') +
    '</div></div>' +
    '</div>' +
    cardTable('Best paying work', '<button class="btn sm ghost" data-export="clients">Clients CSV</button>',
      d.top_jobs.length ? d.top_jobs.map((j) => '<tr data-open="jobs/' + j.id + '"><td class="strong">' + esc(j.title) +
        '<div class="ref">' + esc(j.ref) + ' · ' + esc(j.client) + '</div></td>' +
        '<td>' + esc(j.category) + '</td><td class="num">' + money(j.total) + '</td><td class="num">' + money(j.cost) +
        '</td><td class="num' + (j.profit < 0 ? ' neg' : ' pos') + '">' + money(j.profit) + '</td>' +
        '<td>' + pill(j.status) + '</td></tr>').join('')
        : '<tr><td colspan="6" class="hint">No jobs in this range.</td></tr>',
      ['Job', 'Service', 'Billed', 'Cost', 'Profit', 'Status']) +
    cardTable('Top clients by billing', '',
      d.top_clients.length ? d.top_clients.map((c) => '<tr data-open="clients/' + c.id + '"><td class="strong">' + esc(c.name) +
        '</td><td class="num">' + c.jobs + '</td><td class="num">' + money(c.billed) + '</td><td class="num">' + money(c.paid) +
        '</td><td class="num' + (c.profit < 0 ? ' neg' : ' pos') + '">' + money(c.profit) +
        '</td><td class="num' + (c.due > 0.005 ? ' balance neg' : '') + '">' + money(c.due) + '</td></tr>').join('')
        : '<tr><td colspan="6" class="hint">No jobs in this range.</td></tr>',
        ['Client', 'Jobs', 'Billed', 'Paid', 'Profit', 'Owed']);
  restoreFocus();
}
function bucketLabel(bucket, period) {
  if (period === 'month') { const ym = bucket.split('-'); return MON[Number(ym[1]) - 1] + ' ' + ym[0].slice(2); }
  if (period === 'week') return 'wk' + bucket.split('-')[1];
  return bucket.slice(8) + '/' + bucket.slice(5, 7);
}

/* ------------------------------------------------------------------ drawers */
function closeDrawer() {
  if (notifyTick) { clearInterval(notifyTick); notifyTick = null; }
  $('#drawer').hidden = true;
  $('#drawerBody').innerHTML = '';
  if (S.route && S.route.id) {
    history.replaceState(null, '', '#/' + S.route.view + (S.route.params.toString() ? '?' + S.route.params.toString() : ''));
  }
}
/* ------------------------------------------------------------------ client messages */
function openExternal(url) {
  const handler = window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.external;
  if (handler) {
    try { handler.postMessage(url); return true; } catch (e) { /* falls through to the tab path */ }
  }
  if (/^mailto:/i.test(url)) { window.location.href = url; return true; }
  return !!window.open(url, '_blank', 'noopener');
}

function notifyCard(job, n) {
  S.notify.byId = {};
  S.notify.client = n.client;
  n.messages.forEach((m) => { S.notify.byId[m.id] = m; });
  /* An automatic message with no provider set up would sit queued and give the shop no way to
     actually tell the client, so on those channels the handoff buttons stay available. */
  const blocked = {};
  (n.blocked || []).forEach((b) => { blocked[b.channel] = b.reason; });
  const newest = n.messages.reduce((a, b) => (b.state === 'Queued' && b.id > a ? b.id : a), 0);
  const rows = n.messages.map((m) => {
    const waiting = m.state === 'Queued';
    const automatic = !!m.auto_send;
    const label = automatic
      ? (m.delivery_state === 'Sent' ? (m.provider_id ? 'Sent automatically' : 'Sent by the shop')
        : m.delivery_state === 'Sending' ? 'Sending automatically'
          : m.delivery_state === 'Failed' ? 'Delivery failed'
            : m.delivery_state === 'Cancelled' ? 'Cancelled'
              : 'Automatic · queued')
      : m.state;
    const details = automatic
      ? (m.delivery_error ? '<p class="bad delivery-error">' + esc(m.delivery_error) + '</p>'
        : m.delivery_state === 'Pending' && m.delivery_attempts
          ? '<p class="hint delivery-error">Retry scheduled after attempt ' + m.delivery_attempts + '.</p>'
          : m.delivery_state === 'Cancelled'
            ? '<p class="hint delivery-error">The client withdrew permission; this message was not sent.</p>'
            : m.delivery_state === 'Sent' && !m.provider_id
              ? '<p class="hint">Carried by the shop and closed here by hand.</p>'
              : '<p class="hint">Sent by the shop server when the provider is available.</p>')
      : '';
    const verb = m.channel === 'WhatsApp' ? 'Open in WhatsApp' : 'Open in Mail';
    /* A client who withdrew permission is not to be written to, buttons or otherwise. */
    const aside = m.delivery_state !== 'Cancelled';
    const handoff = aside && (!automatic || !!blocked[m.channel]);
    return '<details class="msg' + (waiting ? ' waiting' : '') + '"' + (m.id === newest ? ' open' : '') + '>' +
      '<summary><span class="pill n-' + esc(automatic ? m.delivery_state : m.state) + '">' + esc(label) + '</span>' +
      '<b>' + esc(m.event) + '</b><span class="m-chan">' + esc(m.channel) + ' &middot; ' + esc(m.to_address) + '</span>' +
      '<span class="spacer"></span><time>' + fdatetime(m.updated_at) + '</time></summary>' +
      '<p class="m-body">' + esc(m.body) + '</p>' +
      details +
      '<div class="m-act">' +
      (automatic && m.delivery_state === 'Failed'
        ? '<button class="btn sm primary" data-notify-retry="' + m.id + '">Retry now</button>' : '') +
      (handoff
        ? '<button class="btn sm' + (m.channel === 'WhatsApp' && handoff ? ' primary' : '') +
          '" data-notify-open="' + m.id + '">' + verb + '</button>' +
          '<button class="btn sm ghost" data-notify-copy="' + m.id + '">Copy</button>' +
          (m.state === 'Sent'
            ? '<button class="btn sm ghost" data-notify-state="' + m.id + '" data-notify-to="Queued">Back to queued</button>'
            : '<button class="btn sm ghost" data-notify-state="' + m.id + '" data-notify-to="Sent">Mark as sent</button>')
        : '') +
      '<span class="spacer"></span>' +
      '<button class="btn sm ghost danger" data-notify-del="' + m.id + '">Remove</button>' +
      '</div></details>';
  }).join('');
  const again = '<div class="chips">' + n.events.map((e) =>
    '<button class="btn sm ghost" data-notify-queue="' + job.id + '" data-notify-event="' + esc(e) + '">' +
    esc(e) + '</button>').join('') + '</div>';
  const canSend = n.whatsapp_to || n.email_to;
  return '<div class="section-h">Tell the client' +
    (n.to_send ? ' <span class="pill n-Queued">' + pluralise(n.to_send, 'message') + ' to send</span>' : '') +
    '</div><p class="hint" style="margin:8px 0 0">Pending, Printing, Ready and Delivered updates go out ' +
    'automatically on the channels the client agreed to. Other messages can still be opened as drafts ' +
    'for staff to send.</p>' +
    (rows ? '<div class="msgs">' + rows + '</div>' : '<p class="hint" style="margin-top:8px">Nothing has been written for this job yet.</p>') +
    (canSend
      ? '<div class="section-h sub">Write it again, or send a stage you skipped</div>' + again
      : '<p class="bad" style="margin:10px 0 0">There is no WhatsApp number or email on file for ' +
        esc(n.client) + ', so there is nothing to send. ' +
        '<a href="#/clients/' + job.client_id + '">Add their contact details</a> and this fills itself in.</p>') +
    (n.not_consented && n.not_consented.length
      ? '<p class="hint" style="margin-top:8px">Automatic updates were skipped on ' +
        n.not_consented.map(esc).join(' and ') +
        ' because permission is not recorded. Edit the client record to record consent.</p>'
      : '') +
    (n.blocked && n.blocked.length
      ? '<p class="sig-note" style="margin-top:8px">' + n.blocked.map((b) =>
          esc(b.channel) + ' is queued and waiting, but cannot leave this Mac on its own: ' +
          esc(b.reason)).join(' ') +
        ' Until that is set up, press ' + (n.blocked[0].channel === 'WhatsApp' ? 'WhatsApp' : 'Email') +
        ' above — the message opens ready to send, and Mark as sent closes it here.</p>'
      : '') +
    (n.missing.length && canSend
      ? '<p class="hint" style="margin-top:8px">' + n.missing.map((c) =>
          'No ' + c.toLowerCase() + ' on file for ' + esc(n.client) + '.').join(' ') + '</p>'
      : '');
}

function paintNotify(id) {
  const box = document.getElementById('notifyCard');
  if (!box) return Promise.resolve();
  return api('/api/jobs/' + id + '/notifications')
    .then((n) => {
      box.innerHTML = notifyCard(S.notify.job || { id: id, client_id: 0 }, n);
      S.notify.signature = notificationSignature(n);
      return refreshChrome();
    })
    .catch((e) => { fail(e); });
}

function notificationSignature(n) {
  return (n.messages || []).map((m) =>
    [m.id, m.state, m.delivery_state, m.delivery_attempts, m.delivery_error, m.updated_at].join(':')
  ).join('|');
}
function watchNotificationDelivery() {
  if (notifyTick) clearInterval(notifyTick);
  notifyTick = setInterval(async () => {
    if ($('#drawer').hidden || !S.notify.job) {
      clearInterval(notifyTick); notifyTick = null; return;
    }
    if (notifyPollBusy) return;
    notifyPollBusy = true;
    try {
      const job = S.notify.job;
      const n = await api('/api/jobs/' + job.id + '/notifications');
      const signature = notificationSignature(n);
      if (signature !== S.notify.signature) {
        const box = document.getElementById('notifyCard');
        if (box) box.innerHTML = notifyCard(job, n);
        S.notify.signature = signature;
        await refreshChrome();
      }
    } catch (error) {
      clearInterval(notifyTick); notifyTick = null; fail(error);
    } finally {
      notifyPollBusy = false;
    }
  }, 10000);
}

async function openJobDrawer(id) {
  let job, notes;
  try {
    const both = await Promise.all([api('/api/jobs/' + id), api('/api/jobs/' + id + '/notifications')]);
    job = both[0];
    notes = both[1];
  } catch (e) { return fail(e); }
  S.notify.job = { id: job.id, client_id: job.client_id };
  S.notify.signature = notificationSignature(notes);
  const isQuote = job.kind === 'Quote';
  const paid = job.balance <= 0.005;
  const settled = isSettled(job);
  const items = job.items || [];
  const expenses = job.expenses || [];
  const spent = expenses.reduce((a, e) => a + e.amount, 0);
  const spoiled = job.spoilage || [];
  const spoilCost = spoiled.reduce((a, s) => a + Number(s.amount || 0), 0);
  $('#drawerBody').innerHTML =
    '<div class="drawer-h"><div><div class="ref">' + esc(job.ref) + ' · ' + (isQuote ? 'quoted' : 'booked') + ' ' + fdatetime(job.created_at) + '</div>' +
    '<h2>' + esc(job.title) + '</h2>' +
    '<div style="margin-top:6px">' + pill(job.status) + prio(job.priority) +
    (settled ? ' <span class="pill k-paid">Paid in full</span>' : '') +
    (isQuote ? ' <span class="pill k-Quote">Quote</span>' : '') +
    ' <span class="tag">' + esc(job.category) + '</span>' +
    (items.length ? ' <span class="tag">' + items.length + ' item lines</span>' : '') + '</div></div>' +
    '<span class="spacer"></span><button class="iconbtn" data-action="close-drawer" aria-label="Close">✕</button></div>' +
    '<div class="row-actions">' +
    '<button class="btn primary" data-edit-job="' + job.id + '">Edit ' + (isQuote ? 'quote' : 'job') + '</button>' +
    (isQuote ? '<button class="btn" data-convert-job="' + job.id + '">Book it as a job</button>'
             : (paid ? '' : '<button class="btn" data-pay-job="' + job.id + '" data-pay-name="' + esc(job.client) + '" data-pay-amount="' + job.balance + '">Receive payment</button>')) +
    '<a class="btn" href="/print/' + job.id + '" target="_blank" rel="noopener">' + (isQuote ? 'Print estimate' : 'Job sheet') + '</a>' +
    (isQuote ? '' : '<button class="btn" data-job-expense="' + job.id + '">Record a cost</button>') +
    '<button class="btn ghost danger" data-del-job="' + job.id + '" data-del-name="' + esc(job.ref) + '">Delete</button></div>' +
    '<div class="amounts">' +
    (isQuote
      ? '<div><span>Quoted total</span><b>' + money(job.total) + '</b></div>' +
        '<div><span>What it costs us</span><b>' + money(job.cost) + '</b></div>' +
        '<div><span>Margin if accepted</span><b class="' + (job.profit < 0 ? 'neg' : 'pos') + '">' + money(job.profit) + '</b></div>'
      : '<div><span>Total</span><b>' + money(job.total) + '</b></div>' +
        '<div><span>Paid</span><b class="pos">' + money(job.paid) + '</b></div>' +
        '<div><span>' + (job.balance < -0.005 ? 'Credit' : 'Balance due') + '</span><b class="' + (job.balance > 0.005 ? 'neg' : 'pos') + '">' + money(job.balance) + '</b></div>') +
    '</div>' +
    (isQuote ? '' :
      '<div class="amounts second"><div><span>Cost of this job</span><b>' + money(job.cost) + '</b></div>' +
      '<div><span>Profit on this job</span><b class="' + (job.profit < 0 ? 'neg' : 'pos') + '">' + money(job.profit) + '</b></div>' +
      '<div><span>Margin</span><b' + (job.cost > 0 ? '' : ' class="muted" title="Nothing has been costed on this job yet"') + '>' +
        (job.cost <= 0 ? 'not costed' : job.total > 0 ? Math.round((job.profit / job.total) * 100) + '%' : '—') + '</b></div></div>') +
    (settled ? '<p class="hint">Nothing is owed on this job' +
      (job.last_paid ? ' — cleared by the payment of ' + fdate(job.last_paid) + '. Any money taken from here on sits as credit on their account.' : '.') +
      '</p>' : '') +
    '<div><div class="section-h">Move this ' + (isQuote ? 'quote' : 'job') + ' to</div><div class="steps" style="margin-top:9px">' +
    S.boot.statuses.map((s) => '<button class="step' + (s === job.status ? ' on' : '') + '" data-status="' + esc(s) +
      '" data-status-job="' + job.id + '">' + esc(s) + '</button>').join('') + '</div></div>' +
    '<div class="notify" id="notifyCard">' + notifyCard(job, notes) + '</div>' +
    '<div class="kv-grid">' +
    '<div><span>Client</span><b><a href="#/clients/' + job.client_id + '">' + esc(job.client) + '</a></b></div>' +
    '<div><span>Phone</span><b>' + (job.client_phone ? '<a href="tel:' + esc(job.client_phone) + '">' + esc(job.client_phone) + '</a>' : '—') + '</b></div>' +
    '<div><span>WhatsApp</span><b>' + esc(job.client_whatsapp || '—') + '</b></div>' +
    (isQuote ? '<div><span>Price holds until</span><b>' + (job.valid_until ? esc(fdate(job.valid_until)) : 'not set') + '</b></div>'
             : '<div><span>Due</span><b>' + (job.due_date ? esc(fdate(job.due_date)) : 'not set') + '</b></div>') +
    (isQuote && job.converted_at ? '<div><span>Booked as a job</span><b>' + fdatetime(job.converted_at) + '</b></div>' : '') +
    (items.length ? '' : '<div><span>Quantity</span><b>' + job.quantity + ' ' + esc(job.unit) + ' @ ' + money(job.unit_price) + '</b></div>') +
    '<div><span>Specification</span><b>' + esc(job.size || '—') + '</b></div>' +
    (job.extras ? '<div><span>Materials / finishing</span><b>' + money(job.extras) + '</b></div>' : '') +
    (job.discount ? '<div><span>Discount</span><b>' + money(job.discount) + '</b></div>' : '') +
    '</div>' +
    (job.description ? '<div><div class="section-h">Brief</div><p style="margin:8px 0 0">' + esc(job.description) + '</p></div>' : '') +
    '<div><div class="section-h">' + (items.length ? 'What is on this order' : 'Priced as one line') + '</div>' +
    (items.length ? '<div class="tablewrap"><table><thead><tr><th>Item</th><th class=num>Qty</th><th class=num>Unit</th>' +
      (isQuote ? '' : '<th class=num>Cost</th>') + '<th class=num>Amount</th></tr></thead><tbody>' +
      items.map((it) => '<tr><td><span class="strong">' + esc(it.title) + '</span>' +
        (it.size ? '<div class="ref">' + esc(it.size) + '</div>' : '') + '</td>' +
        '<td class=num>' + it.quantity + ' ' + esc(it.unit) + '</td>' +
        '<td class=num>' + money(it.unit_price) + '</td>' +
        (isQuote ? '' : '<td class="num' + (it.unit_cost > 0 ? '' : ' hint') + '">' + (it.unit_cost > 0 ? money(it.line_cost) : '—') + '</td>') +
        '<td class=num>' + money(it.line_total) + '</td></tr>').join('') +
      '</tbody></table></div>'
      : '<p class="hint" style="margin-top:8px">One line for the whole order — ' + job.quantity + ' ' + esc(job.unit) +
        ' at ' + money(job.unit_price) + '. Edit the job to split it into separate products.</p>') + '</div>' +
    (isQuote ? '' :
      '<div><div class="section-h">Costs booked against this job</div>' +
      (expenses.length ? '<div class="tablewrap"><table><thead><tr><th>Date</th><th>Category</th><th>Paid to</th><th class=num>Amount</th><th></th></tr></thead><tbody>' +
        expenses.map((e) => '<tr><td class="ref">' + fdate(e.spent_on) + '</td><td>' + esc(e.category) + '</td>' +
          '<td>' + esc(e.payee || '—') + (e.reference ? '<div class="ref">' + esc(e.reference) + '</div>' : '') + '</td>' +
          '<td class="num neg">' + money(e.amount) + '</td>' +
          '<td class=num><button class="btn sm ghost" data-del-expense="' + e.id + '" data-del-amount="' + e.amount + '">Remove</button></td></tr>').join('') +
        '</tbody></table></div>'
        : '<p class="hint" style="margin-top:8px">No costs recorded against this job, so profit is only reduced by what the item lines cost.</p>') +
      '<p class="hint" style="margin-top:6px">' + money(spent) + ' paid out on ' + expenses.length + ' entr' + (expenses.length === 1 ? 'y' : 'ies') + '.</p></div>') +
    (isQuote ? '' :
      '<div><div class="section-h">Spoiled on this job</div>' +
      '<div style="margin:9px 0 8px"><button class="btn sm" data-new-spoilage="' + job.id + '">+ Log spoiled work</button></div>' +
      (spoiled.length ? '<div class="tablewrap"><table><thead><tr><th>Date</th><th class=num>Qty</th><th>Reason</th><th class=num>Cost</th><th></th></tr></thead><tbody>' +
        spoiled.map((s) => '<tr><td class="ref">' + fdate(s.spoiled_on) + '</td>' +
          '<td class=num>' + Number(s.quantity).toLocaleString('en-US') + '</td>' +
          '<td>' + esc(s.reason) + '</td><td class="num neg">' + money(s.amount) + '</td>' +
          '<td class=num><button class="btn sm ghost" data-edit-spoilage="' + s.id + '">Edit</button></td></tr>').join('') +
        '</tbody></table></div>' +
        '<p class="hint" style="margin-top:6px">' + money(spoilCost) + ' of waste on ' + pluralise(spoiled.length, 'record') +
        ', already counted against this job’s profit.</p>'
        : '<p class="hint" style="margin-top:8px">Nothing spoiled on this job yet. Log it here when a batch goes wrong — the cost lands on this job, not on the client.</p>') + '</div>') +
    '<div><div class="section-h">Payments on this job</div>' +
    (job.payments.length ? '<div class="tablewrap"><table><thead><tr><th>Date</th><th>Type</th><th>Method</th><th class=num>Amount</th><th></th></tr></thead><tbody>' +
      job.payments.map((p) => '<tr><td class="ref">' + fdate(p.paid_at) + '</td><td>' + esc(p.kind) + '</td><td>' + esc(p.method) +
        (p.reference ? '<div class="ref">' + esc(p.reference) + '</div>' : '') + '</td>' +
        '<td class="num' + (p.kind === 'Refund' ? ' neg' : ' pos') + '">' + money(p.kind === 'Refund' ? -p.amount : p.amount) + '</td>' +
        '<td class=num><button class="btn sm ghost" data-del-payment="' + p.id + '" data-del-amount="' + p.amount + '">Remove</button></td></tr>').join('') +
      '</tbody></table></div>' : '<p class="hint" style="margin-top:8px">' + (isQuote ? 'Quotes do not take money — book it as a job first.' : 'Nothing received yet.') + '</p>') + '</div>' +
    '<div><div class="section-h">Record of this ' + (isQuote ? 'quote' : 'job') + '</div>' +
    '<div style="margin-top:10px"><ul class="timeline">' +
    job.events.map((e) => '<li class="' + (e.type === 'payment' ? 'pay' : e.type === 'note' ? 'note' : '') + '"><i></i><div>' +
      '<p>' + esc(e.detail) + '</p><time>' + fdatetime(e.created_at) + '</time></div></li>').join('') + '</ul></div>' +
    '<form data-note-form="' + job.id + '" style="grid-template-columns:1fr auto;align-items:end">' +
    '<label class="field"><span>Add a note to the record</span><input name="note" placeholder="e.g. client approved the proof on WhatsApp" required></label>' +
    '<button class="btn">Save note</button></form></div>';
  $('#drawer').hidden = false;
  $('#drawerBody').scrollTop = 0;
  watchNotificationDelivery();
}
async function openClientDrawer(id) {
  if (notifyTick) { clearInterval(notifyTick); notifyTick = null; }
  let c;
  try { c = await api('/api/clients/' + id); } catch (e) { return fail(e); }
  const openJobs = c.jobs.filter((j) => j.kind !== 'Quote' &&
    ['Pending', 'Printing', 'Ready'].indexOf(j.status) >= 0).length;
  $('#drawerBody').innerHTML =
    '<div class="drawer-h"><div><div class="ref">' + esc(c.kind) + (c.archived ? ' · archived' : '') + ' · client since ' + fdate(c.created_at) + '</div>' +
    '<h2>' + esc(c.name) + '</h2></div><span class="spacer"></span>' +
    '<button class="iconbtn" data-action="close-drawer" aria-label="Close">✕</button></div>' +
    '<div class="row-actions">' +
    '<button class="btn primary" data-new-job-for="' + c.id + '">New job for ' + esc(c.name.split(' ')[0]) + '</button>' +
    '<button class="btn" data-pay-client="' + c.id + '" data-pay-name="' + esc(c.name) + '">Receive payment</button>' +
    '<button class="btn" data-edit-client="' + c.id + '">Edit details</button>' +
    '<button class="btn ghost danger" data-archive-client="' + c.id + '" data-archived="' + c.archived + '">' +
    (c.archived ? 'Restore' : 'Archive') + '</button></div>' +
    '<div class="amounts">' +
    '<div><span>Total billed</span><b>' + money(c.billed) + '</b></div>' +
    '<div><span>Total received</span><b class="pos">' + money(c.received) + '</b></div>' +
    '<div><span>' + (c.balance_due < -0.005 ? 'Credit held' : 'Balance due') + '</span><b class="' + (c.balance_due > 0.005 ? 'neg' : 'pos') + '">' + money(c.balance_due) + '</b></div>' +
    '</div>' +
    '<div class="kv-grid">' +
    '<div><span>Phone</span><b>' + (c.phone ? '<a href="tel:' + esc(c.phone) + '">' + esc(c.phone) + '</a>' : '—') + '</b></div>' +
    '<div><span>WhatsApp</span><b>' + esc(c.whatsapp || '—') + '</b></div>' +
    '<div><span>Email</span><b>' + (c.email ? '<a href="mailto:' + esc(c.email) + '">' + esc(c.email) + '</a>' : '—') + '</b></div>' +
    '<div><span>WhatsApp updates</span><b>' + (c.whatsapp_updates ? 'Agreed' : 'Not opted in') + '</b></div>' +
    '<div><span>Email updates</span><b>' + (c.email_updates ? 'Agreed' : 'Not opted in') + '</b></div>' +
    '<div><span>Address</span><b>' + esc(c.address || '—') + '</b></div>' +
    '<div><span>Open jobs</span><b>' + openJobs + ' of ' + c.jobs.filter((j) => j.kind !== 'Quote').length + '</b></div>' +
    (c.open_quotes ? '<div><span>Quotes waiting</span><b>' + c.open_quotes + '</b></div>' : '') +
    '</div>' +
    (c.notes ? '<div><div class="section-h">Notes</div><p style="margin:8px 0 0">' + esc(c.notes) + '</p></div>' : '') +
    '<div><div class="section-h">Everything we have printed for them</div>' +
    (c.jobs.length ? '<div class="tablewrap"><table><thead><tr><th>Ref</th><th>Job</th><th class=num>Total</th><th class=num>Owes</th><th>Status</th><th>' + (c.open_quotes ? 'Due / validity' : 'Due') + '</th></tr></thead><tbody>' +
      c.jobs.map((j) => {
        const quote = j.kind === 'Quote';
        return '<tr data-open="jobs/' + j.id + '"><td class="ref">' + esc(j.ref) + '</td><td><span class="strong">' + esc(j.title) + '</span>' +
        prio(j.priority) + (quote ? ' <span class="pill k-Quote">Quote</span>' : '') +
        '<div class="ref">' + esc(j.category) + ' · ' + fdate(j.created_at) +
        (j.item_count ? ' · ' + j.item_count + ' lines' : '') + '</div></td>' +
        '<td class=num>' + money(j.total) + '</td>' +
        (quote ? '<td class="num hint">not billed</td>'
               : '<td class="num' + (j.balance > 0.005 ? ' balance neg' : ' pos') + '">' + money(j.balance) + '</td>') +
        '<td>' + pill(j.status) + '</td><td>' + dateLabel(j) + '</td></tr>';
      }).join('') + '</tbody></table></div>'
        : '<p class="hint" style="margin-top:8px">No jobs booked yet.</p>') + '</div>' +
    '<div><div class="section-h">Payment history</div>' +
    (c.payments.length ? '<div class="tablewrap"><table><thead><tr><th>Date</th><th>For</th><th>Type</th><th class=num>Amount</th><th>Method</th></tr></thead><tbody>' +
      c.payments.map((p) => '<tr><td class="ref">' + fdate(p.paid_at) + '</td><td>' + (p.ref ? '<a href="#/jobs/' + p.job_id + '">' + esc(p.ref) + '</a>' : 'on account') +
        '</td><td>' + esc(p.kind) + '</td><td class="num' + (p.kind === 'Refund' ? ' neg' : ' pos') + '">' + money(p.kind === 'Refund' ? -p.amount : p.amount) +
        '</td><td>' + esc(p.method) + (p.reference ? '<div class="ref">' + esc(p.reference) + '</div>' : '') + '</td></tr>').join('') +
      '</tbody></table></div>' : '<p class="hint" style="margin-top:8px">No payments recorded.</p>') + '</div>';
  $('#drawer').hidden = false;
  $('#drawerBody').scrollTop = 0;
}

/* ------------------------------------------------------------------- modals */
function openModal(html) {
  $('#modalCard').innerHTML = html;
  $('#modal').hidden = false;
  const first = $('#modalCard').querySelector('input,select,textarea');
  if (first) setTimeout(() => first.focus(), 30);
  bindModal();
}
function closeModal() { $('#modal').hidden = true; $('#modalCard').innerHTML = ''; }
function modalHeader(title, sub) {
  return '<header><div><h2>' + title + '</h2>' + (sub ? '<div class="hint">' + sub + '</div>' : '') +
    '</div><span class="spacer"></span><button class="iconbtn" data-action="close-modal" aria-label="Close">✕</button></header>';
}
function installInstructions() {
  const ua = navigator.userAgent || '';
  const ios = /iPhone|iPad|iPod/.test(ua) ||
    (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  let steps;
  if (!window.isSecureContext) {
    steps = '<p>This address uses plain HTTP. Browsers require HTTPS before they allow this app ' +
      'to be installed or its offline app shell to be cached on another device.</p>' +
      '<p>The shop’s install page carries the address, the certificate to trust and the steps for ' +
      'this kind of device: <a href="/setup" target="_blank">open /setup</a> — or just the ' +
      '<a href="/shop-root-ca.cer" download="CRISPprint-Shop-Root-CA.cer">shop certificate</a> ' +
      'if this device already has the HTTPS address.</p>' +
      '<p class="hint">Install a certificate only for a shop you trust. The page prints its ' +
      'fingerprint so you can check the file is the one the shop made.</p>';
  } else if (ios) {
    steps = '<p>In Safari, tap <b>Share</b>, then choose <b>Add to Home Screen</b> and confirm.</p>';
  } else if (/Android/.test(ua)) {
    steps = '<p>In Chrome, open the browser menu and choose <b>Install app</b> or ' +
      '<b>Add to Home screen</b>.</p>';
  } else {
    steps = '<p>In Microsoft Edge or Chrome, use the install icon in the address bar or open ' +
      'the browser menu and choose <b>Install this site as an app</b>.</p>';
  }
  openModal(modalHeader('Install CRISPprint', 'Add an app shortcut to this device') +
    '<div class="install-note">' + steps +
    '<p class="hint">Installing adds this app to the device; shop records remain on the shop computer.</p></div>');
}
async function installApp() {
  if (window.matchMedia('(display-mode: standalone)').matches || navigator.standalone === true) {
    toast('CRISPprint is already installed on this device.', 'good');
    return;
  }
  if (deferredInstallPrompt) {
    const prompt = deferredInstallPrompt;
    deferredInstallPrompt = null;
    await prompt.prompt();
    const choice = await prompt.userChoice;
    if (choice && choice.outcome === 'accepted') toast('CRISPprint was installed.', 'good');
    return;
  }
  installInstructions();
}
window.addEventListener('beforeinstallprompt', (event) => {
  event.preventDefault();
  deferredInstallPrompt = event;
});
window.addEventListener('appinstalled', () => {
  deferredInstallPrompt = null;
  toast('CRISPprint was installed.', 'good');
});
async function jobForm(job, presetClient, asQuote) {
  if (!S.clients.length) await loadClients();
  const j = job || {};
  const clientId = j.client_id || presetClient || '';
  const isQuote = job ? j.kind === 'Quote' : !!asQuote;
  const lines = jobLines(j);
  openModal(modalHeader(job ? 'Edit ' + esc(j.ref) : (isQuote ? 'Write a quote' : 'Book a print job'),
    job ? 'Changes are written into the job record.'
        : 'One line for the work, one line for the money — or a line for each product.') +
    '<form id="jobForm" data-job-id="' + (job ? job.id : '') + '">' +
    '<input type="hidden" name="kind" value="' + (isQuote ? 'Quote' : 'Job') + '">' +
    '<div class="f-grid">' +
    clientPicker(clientId) +
    '<label class="field wide"><span>' + (isQuote ? 'What is being quoted' : 'What is being printed') + '</span>' +
    '<input name="title" required maxlength="200" placeholder="e.g. 500 business cards, double sided" value="' + esc(j.title || '') + '"></label>' +
    '<label class="field"><span>Service</span><select name="category">' +
    S.boot.categories.map((c) => '<option' + (j.category === c ? ' selected' : '') + '>' + esc(c) + '</option>').join('') + '</select></label>' +
    '<label class="field"><span>Specification / stock</span><input name="size" maxlength="120" placeholder="A4, 300gsm gloss, 2m x 1m…" value="' + esc(j.size || '') + '"></label>' +
    '<label class="field"><span>Status</span><select name="status">' + S.boot.statuses.map((s) =>
      '<option' + ((j.status || 'Pending') === s ? ' selected' : '') + '>' + esc(s) + '</option>').join('') + '</select></label>' +
    '<label class="field"><span>Priority</span><select name="priority">' + ['Normal', 'Urgent'].map((s) =>
      '<option' + ((j.priority || 'Normal') === s ? ' selected' : '') + '>' + esc(s) + '</option>').join('') + '</select></label>' +
    '<label class="field"><span>' + (isQuote ? 'Ready by' : 'Ready by') + '</span><input name="due_date" type="date" value="' + esc(day10(j.due_date || '')) + '"></label>' +
    (isQuote ? '<label class="field"><span>Price holds until</span><input name="valid_until" type="date" value="' +
      esc(day10(j.valid_until || plusDays(14))) + '"></label>' : '') +
    '</div>' +
    itemsEditor(j, lines) +
    '<div class="f-grid" id="singleBlock"' + (lines.length ? ' hidden' : '') + '>' +
    '<label class="field"><span>Quantity</span><input name="quantity" type="number" min="0" step="1" value="' + esc(j.quantity === undefined ? 1 : j.quantity) + '"></label>' +
    '<label class="field"><span>Unit</span><select name="unit">' + S.boot.units.map((u) =>
      '<option' + ((j.unit || 'pcs') === u ? ' selected' : '') + '>' + esc(u) + '</option>').join('') + '</select></label>' +
    '<label class="field"><span>Price per unit</span><input name="unit_price" type="number" min="0" step="0.01" value="' + esc(j.unit_price === undefined ? '' : n2(j.unit_price)) + '" placeholder="0.00"></label>' +
    '<label class="field"><span>Cost per unit</span><input name="unit_cost" type="number" min="0" step="0.01" value="" placeholder="0.00"></label>' +
    '</div>' +
    '<div class="f-grid">' +
    '<label class="field"><span>Materials / finishing</span><input name="extras" type="number" min="0" step="0.01" value="' + esc(j.extras === undefined ? '' : n2(j.extras)) + '" placeholder="0.00"></label>' +
    '<label class="field"><span>Discount</span><input name="discount" type="number" min="0" step="0.01" value="' + esc(j.discount === undefined ? '' : n2(j.discount)) + '" placeholder="0.00"></label>' +
    '<label class="field wide"><span>Brief / instructions</span><textarea name="description" rows="3" placeholder="Colours, finishing, delivery instructions, who approved the proof…">' + esc(j.description || '') + '</textarea></label>' +
    '</div>' +
    calcBox(j) +
    (job ? '' : (isQuote ? '' : depositBox())) +
    '<div class="err" id="jobErr"></div>' +
    '<div class="modal-foot"><span class="spacer"></span>' +
    '<button type="button" class="btn" data-action="close-modal">Cancel</button>' +
    '<button class="btn primary">' + (job ? 'Save changes' : (isQuote ? 'Write the quote' : 'Book the job')) + '</button></div>' +
    '</form>');
}
function plusDays(n) {
  const d = new Date(Date.now() + n * 86400000);
  return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
}
/* One order, many products. A job with no lines is priced by the single block above. */
function jobLines(j) {
  if ((j.items || []).length) return j.items;
  // A job booked before item lines existed is shown as one line, so editing it never
  // changes the money — it just moves the same numbers into the lines editor.
  if (j.id && !j.item_count) {
    return [{ title: j.title, size: j.size, quantity: j.quantity, unit: j.unit,
              unit_price: j.unit_price, unit_cost: 0,
              line_total: (j.quantity || 0) * (j.unit_price || 0) }];
  }
  return j.items || [];
}
function itemRowHtml(it) {
  const v = it || {};
  return '<tr class="item-row">' +
    '<td><input class="it-title" maxlength="200" placeholder="e.g. 500 flyers A5" value="' + esc(v.title || '') + '"></td>' +
    '<td><input class="it-size" maxlength="120" placeholder="stock / size" value="' + esc(v.size || '') + '"></td>' +
    '<td><input class="it-qty" type="number" min="1" step="1" value="' + esc(v.quantity === undefined ? '' : v.quantity) + '" placeholder="0"></td>' +
    '<td><select class="it-unit">' + S.boot.units.map((u) => '<option' + ((v.unit || 'pcs') === u ? ' selected' : '') + '>' + esc(u) + '</option>').join('') + '</select></td>' +
    '<td><input class="it-price" type="number" min="0" step="0.01" value="' + esc(v.unit_price === undefined ? '' : n2(v.unit_price)) + '" placeholder="0.00"></td>' +
    '<td><input class="it-cost" type="number" min="0" step="0.01" value="' + esc(v.unit_cost ? n2(v.unit_cost) : '') + '" placeholder="0.00"></td>' +
    '<td class="num it-line">' + money(v.line_total || (v.quantity || 0) * (v.unit_price || 0)) + '</td>' +
    '<td class="num"><button type="button" class="iconbtn" data-remove-item aria-label="Remove line">✕</button></td>' +
    '</tr>';
}
function itemsEditor(j, items) {
  return '<div class="items">' +
    '<div class="section-h">Item lines on this order' +
    '<span class="spacer"></span>' +
    '<button type="button" class="btn sm" data-add-item>+ Add a product</button></div>' +
    '<div class="tablewrap"><table class="items-t"><thead><tr>' +
    '<th>What</th><th>Specification</th><th class=num>Qty</th><th>Unit</th>' +
    '<th class=num>Selling price</th><th class=num>Our cost each</th><th class=num>Line total</th><th></th>' +
    '</tr></thead><tbody id="itemRows">' + items.map(itemRowHtml).join('') + '</tbody></table></div>' +
    '<p class="hint">Use one line for each product on the same order — cards and a banner on one ticket. ' +
    'With no lines here, the single quantity and price below set the total.</p></div>';
}
function calcBox(j) {
  return '<div class="calc" id="calc" data-paid="' + (j.paid === undefined ? '' : n2(j.paid)) + '" data-ref="' + esc(j.ref || '') + '"><div class="line"><span>Item lines</span><b id="cGross">—</b></div>' +
    '<div class="line"><span>Materials / finishing</span><b id="cExtras">—</b></div>' +
    '<div class="line"><span>Discount</span><b id="cDisc">—</b></div>' +
    '<div class="line total"><span id="cTotalLabel">Job total</span><b id="cTotal">—</b></div>' +
    '<div class="line"><span>What it costs the shop</span><b id="cCost">—</b></div>' +
    '<div class="line"><span>We keep on this job</span><b id="cProfit">—</b></div>' +
    '<div class="line"><span id="cPaidLabel">Deposit taken now</span><b id="cBalance">—</b></div>' +
    '<div class="line"><span id="cPaidTag">Paid to date</span><b id="cDue">—</b></div></div>';
}
function depositBox() {
  return '<div class="f-grid" style="margin-top:2px">' +
    '<label class="field"><span>Deposit received now (optional)</span><input name="deposit" type="number" min="0" step="0.01" placeholder="0.00"></label>' +
    '<label class="field"><span>Paid by</span><select name="method">' + S.boot.methods.map((m) => '<option' + (m === 'Cash' ? ' selected' : '') + '>' + esc(m) + '</option>').join('') + '</select></label>' +
    '</div>';
}
function clientPicker(selectedId) {
  return '<div class="picker-field wide"><span>Client</span>' +
    '<div class="picker"><input id="clientFind" placeholder="Type to find the client (name or phone)" autocomplete="off">' +
    '<select name="client_id" id="clientSelect" size="4">' + clientOptions(selectedId) + '</select>' +
    '<div class="row-actions"><button type="button" class="btn sm" data-action="toggle-newclient">+ Register a new client</button>' +
    '<span class="hint" id="clientHint">' + S.clients.length + ' on file</span></div>' +
    '<div id="newClientBox" hidden class="f-grid">' +
    '<label class="field wide"><span>New client name *</span><input name="new_name" maxlength="160" placeholder="Person or business name"></label>' +
    '<label class="field"><span>Phone</span><input name="new_phone" maxlength="40" placeholder="024…"></label>' +
    '<label class="field"><span>WhatsApp</span><input name="new_whatsapp" maxlength="40" placeholder="024…"></label>' +
    '<label class="field"><span>Email</span><input name="new_email" type="email" maxlength="160" placeholder="name@example.com"></label>' +
    '<label class="field"><span>Type</span><select name="new_kind">' + S.boot.kinds.map((k) => '<option>' + esc(k) + '</option>').join('') + '</select></label>' +
    '<label class="field wide"><span>Area / address</span><input name="new_address" maxlength="400" placeholder="Neighbourhood, city"></label>' +
    '<label class="consent wide"><input type="checkbox" name="new_whatsapp_updates" checked><span>Client agreed to receive job updates on WhatsApp</span></label>' +
    '<label class="consent wide"><input type="checkbox" name="new_email_updates" checked><span>Client agreed to receive job updates by email</span></label>' +
    '</div></div></div>';
}
function clientOptions(selectedId, filter) {
  const f = (filter || '').toLowerCase();
  return S.clients.filter((c) => !f || c.name.toLowerCase().indexOf(f) >= 0 || (c.phone || '').toLowerCase().indexOf(f) >= 0)
    .map((c) => '<option value="' + c.id + '"' + (String(c.id) === String(selectedId) ? ' selected' : '') + '>' +
      esc(c.name) + (c.phone ? ' · ' + esc(c.phone) : '') + (c.balance_due > 0 ? '  (owes ' + money(c.balance_due) + ')' : '') + '</option>').join('') ||
    '<option value="">no client matches</option>';
}
function clientForm(client) {
  const c = client || {};
  openModal(modalHeader(client ? 'Edit client' : 'New client', 'Save contact details and record the client’s permission for job updates.') +
    '<form id="clientForm" data-client-id="' + (client ? client.id : '') + '"><div class="f-grid">' +
    '<label class="field wide"><span>Name *</span><input name="name" required maxlength="160" value="' + esc(c.name || '') + '" placeholder="e.g. Kwame Mensah / Bethel Chapel"></label>' +
    '<label class="field"><span>Phone</span><input name="phone" maxlength="40" value="' + esc(c.phone || '') + '" placeholder="0244 000 000"></label>' +
    '<label class="field"><span>WhatsApp</span><input name="whatsapp" maxlength="40" value="' + esc(c.whatsapp || '') + '"></label>' +
    '<label class="field"><span>Email</span><input name="email" type="email" maxlength="160" value="' + esc(c.email || '') + '"></label>' +
    '<label class="consent wide"><input type="checkbox" name="whatsapp_updates"' + (c.whatsapp_updates ? ' checked' : '') + '><span>Client agreed to receive job updates on WhatsApp</span></label>' +
    '<label class="consent wide"><input type="checkbox" name="email_updates"' + (c.email_updates ? ' checked' : '') + '><span>Client agreed to receive job updates by email</span></label>' +
    '<label class="field"><span>Type</span><select name="kind">' + S.boot.kinds.map((k) =>
      '<option' + ((c.kind || 'Individual') === k ? ' selected' : '') + '>' + esc(k) + '</option>').join('') + '</select></label>' +
    '<label class="field wide"><span>Address</span><input name="address" maxlength="400" value="' + esc(c.address || '') + '" placeholder="Area, street"></label>' +
    '<label class="field wide"><span>Notes</span><textarea name="notes" rows="2" placeholder="Prefers MoMo, collects on weekends, always wants a proof first…">' + esc(c.notes || '') + '</textarea></label>' +
    '</div><div class="err" id="clientErr"></div><div class="modal-foot"><span class="spacer"></span>' +
    '<button type="button" class="btn" data-action="close-modal">Cancel</button>' +
    '<button class="btn primary">' + (client ? 'Save changes' : 'Add client') + '</button></div></form>');
}
function paymentForm(opts) {
  const o = opts || {};
  const open = S.openJobs || [];
  const jobRow = o.jobId ? '<input type="hidden" name="job_id" value="' + o.jobId + '">' :
    '<label class="field wide"><span>Which job is this for?</span><select name="job_id" id="payJob">' +
    '<option value="">Money kept on the client account (credit)</option>' +
    open.map((j) => '<option value="' + j.id + '"' + (o.jobId === j.id ? ' selected' : '') + '>' + esc(j.ref) + ' · ' + esc(j.client) +
      ' · ' + esc(j.title) + ' · owes ' + money(j.balance) + '</option>').join('') + '</select></label>';
  const clientRow = o.jobId ? '' :
    '<label class="field wide"><span>Client</span><select name="client_id" id="payClient">' +
    S.clients.map((c) => '<option value="' + c.id + '"' + (o.clientId === c.id ? ' selected' : '') + '>' + esc(c.name) +
      (c.account_credit ? ' · credit ' + money(c.account_credit) : '') + '</option>').join('') + '</select></label>';
  openModal(modalHeader('Receive money', o.jobId ? 'Applied to ' + esc(o.name || 'this job') + '.' :
    'Pay it against a job, or keep it on the client’s account.') +
    '<form id="payForm">' + (o.jobId ? '' : clientRow) + jobRow +
    '<div class="f-grid">' +
    '<label class="field"><span>Amount *</span><input name="amount" type="number" min="0.01" step="0.01" required placeholder="0.00" value="' + esc(o.amount ? n2(o.amount) : '') + '"></label>' +
    '<label class="field"><span>Type</span><select name="kind">' +
    ['Deposit', 'Payment', 'Credit', 'Refund'].map((k) => '<option' + (k === 'Payment' ? ' selected' : '') + '>' + esc(k) + '</option>').join('') + '</select></label>' +
    '<label class="field"><span>Method</span><select name="method">' + S.boot.methods.map((m) =>
      '<option' + (m === 'Cash' ? ' selected' : '') + '>' + esc(m) + '</option>').join('') + '</select></label>' +
    '<label class="field"><span>Transaction ref</span><input name="reference" maxlength="80" placeholder="MoMo / bank id"></label>' +
    '<label class="field"><span>Date paid</span><input name="paid_at" type="date" value="' + todayISO() + '"></label>' +
    '<label class="field wide"><span>Note</span><input name="note" maxlength="500" placeholder="anything worth remembering"></label>' +
    '</div><div class="calc"><div class="line"><span id="payHint">Balance after this payment</span><b id="payAfter">' +
    (o.balance !== undefined ? esc(money(Math.max(o.balance - (Number(o.amount) || 0), 0))) : '—') + '</b></div>' +
    '<button type="button" class="btn sm" id="payFull" hidden>Cover the full balance</button>' +
    '<p class="settles" id="paySettles" hidden></p></div>' +
    '<div class="err" id="payErr"></div><div class="modal-foot"><span class="spacer"></span>' +
    '<button type="button" class="btn" data-action="close-modal">Cancel</button>' +
    '<button class="btn primary">Record payment</button></div></form>');
  const box = $('#payForm');
  const chip = $('#payFull');
  const settles = $('#paySettles');
  let owed = o.balance === undefined ? null : Number(o.balance);
  chip.addEventListener('click', () => {
    box.amount.value = n2(owed);
    if (box.kind.value === 'Deposit' || box.kind.value === 'Credit') box.kind.value = 'Payment';
    if (!box.note.value) box.note.value = 'Paid in full';
    box.dispatchEvent(new Event('input'));
    box.amount.focus();
  });
  box.addEventListener('input', () => {
    const sel = box.querySelector('[name=job_id]');
    const amount = Number(box.amount.value || 0);
    const sign = box.kind.value === 'Refund' ? -1 : 1;
    let balance = null;
    if (sel && sel.tagName === 'SELECT') {
      const job = (S.openJobs || []).filter((j) => String(j.id) === sel.value)[0];
      balance = job ? job.balance : null;
      $('#payHint').textContent = job ? 'Balance on ' + job.ref : 'Kept on the client account — no job balance';
    } else if (o.balance !== undefined) {
      balance = o.balance;
      $('#payHint').textContent = 'Balance on this job after the payment';
    }
    $('#payAfter').textContent = balance === null ? '—' : money(Math.max(balance - sign * amount, 0)) +
      (balance - sign * amount < -0.005 ? ' (credit)' : '');
    if (balance === null) return;
    if (box.kind.value === 'Deposit' && amount >= balance - 0.005) box.kind.value = 'Payment';
    owed = balance;
    const rest = balance - sign * amount;
    const refunding = box.kind.value === 'Refund';
    chip.hidden = !(rest > 0.005 && !refunding);
    if (!chip.hidden) {
      chip.textContent = (amount > 0.005 ? 'Cover the rest · ' : 'Cover the full balance · ') + money(rest);
    }
    const clears = rest <= 0.005 && amount > 0.005 && !refunding;
    settles.hidden = !clears;
    if (clears) settles.textContent = 'That is the whole balance — this job will read Paid in full.';
  });
  box.dispatchEvent(new Event('input'));
}

/* ------------------------------------------------------------- form binding */
function bindModal() {
  const card = $('#modalCard');
  const jobFormEl = card.querySelector('#jobForm');
  if (jobFormEl) {
    const rowsBox = jobFormEl.querySelector('#itemRows');
    const single = jobFormEl.querySelector('#singleBlock');
    const readItems = () => $$('.item-row', rowsBox).map((tr) => ({
      title: tr.querySelector('.it-title').value.trim(),
      size: tr.querySelector('.it-size').value.trim(),
      quantity: Number(tr.querySelector('.it-qty').value || 0),
      unit: tr.querySelector('.it-unit').value,
      unit_price: Number(tr.querySelector('.it-price').value || 0),
      unit_cost: Number(tr.querySelector('.it-cost').value || 0),
      category: (jobFormEl.category ? jobFormEl.category.value : 'Other'),
    })).filter((it) => it.title || it.quantity > 0 || it.unit_price > 0);
    const calc = () => {
      const f = jobFormEl;
      const lines = readItems();
      const hasLines = lines.length > 0;
      const singleQty = Number((f.quantity ? f.quantity.value : 0) || 0);
      const singlePrice = Number((f.unit_price ? f.unit_price.value : 0) || 0);
      const singleCost = Number((f.unit_cost ? f.unit_cost.value : 0) || 0);
      const gross = hasLines ? lines.reduce((a, it) => a + Math.max(it.quantity * it.unit_price, 0), 0)
                             : singleQty * singlePrice;
      const cost = hasLines ? lines.reduce((a, it) => a + Math.max(it.quantity * it.unit_cost, 0), 0)
                            : singleQty * singleCost;
      const extras = Number(f.extras.value || 0);
      const disc = Number(f.discount.value || 0);
      const total = Math.max(gross + extras - disc, 0);
      const dep = Number((f.deposit ? f.deposit.value : 0) || 0);
      $$('.item-row', rowsBox).forEach((tr) => {
        const t = Math.max(Number(tr.querySelector('.it-qty').value || 0) *
                          Number(tr.querySelector('.it-price').value || 0), 0);
        tr.querySelector('.it-line').textContent = money(t);
      });
      if (single) single.hidden = hasLines;
      $('#cGross').parentNode.querySelector('span').textContent = hasLines
        ? lines.length + ' item line' + (lines.length === 1 ? '' : 's') : 'Quantity × price';
      $('#cTotalLabel').textContent = f.kind.value === 'Quote' ? 'Quoted total' : 'Job total';
      $('#cGross').textContent = money(gross);
      $('#cExtras').textContent = money(extras);
      $('#cDisc').textContent = '-' + money(disc);
      $('#cTotal').textContent = money(total);
      $('#cCost').textContent = money(cost);
      $('#cProfit').textContent = money(total - cost);
      $('#cProfit').className = total - cost < 0 ? 'neg' : 'pos';
      if (f.kind.value === 'Quote') {
        $('#cPaidLabel').textContent = 'Cost if they accept';
        $('#cBalance').textContent = money(cost);
        $('#cPaidTag').textContent = 'Margin';
        $('#cDue').textContent = money(total - cost);
        return;
      }
      if (f.deposit) {
        $('#cPaidLabel').textContent = 'Deposit taken now';
        $('#cBalance').textContent = money(dep);
        $('#cDue').textContent = money(Math.max(total - dep, 0));
        $('#cPaidTag').textContent = 'Balance the client still owes';
      } else {
        const already = Number($('#calc').dataset.paid || 0);
        $('#cPaidLabel').textContent = 'Balance after saving';
        $('#cBalance').textContent = money(Math.max(total - already, 0));
        $('#cDue').textContent = money(already);
        $('#cPaidTag').textContent = 'Paid to date';
      }
    };
    const addItem = (it) => {
      rowsBox.insertAdjacentHTML('beforeend', itemRowHtml(it || {}));
      const tr = rowsBox.lastElementChild;
      tr.querySelector('.it-title').focus();
      calc();
    };
    jobFormEl.addEventListener('click', (e) => {
      const add = e.target.closest('[data-add-item]');
      if (add) { e.preventDefault(); addItem(); return; }
      const rm = e.target.closest('[data-remove-item]');
      if (rm) { e.preventDefault(); rm.closest('.item-row').remove(); calc(); }
    });
    jobFormEl.addEventListener('input', calc);
    jobFormEl.addEventListener('change', calc);
    calc();
    const find = card.querySelector('#clientFind');
    if (find) find.addEventListener('input', () => {
      const sel = $('#clientSelect');
      const keep = sel.value;
      sel.innerHTML = clientOptions(keep, find.value);
    });
    jobFormEl.addEventListener('submit', async (e) => {
      e.preventDefault();
      const f = e.target;
      const id = f.dataset.jobId;
      const lines = readItems();
      const payload = {
        title: f.title.value, category: f.category.value, size: f.size.value,
        description: f.description.value, extras: f.extras.value, discount: f.discount.value,
        status: f.status.value, priority: f.priority.value, due_date: f.due_date.value,
        kind: f.kind.value,
        quantity: lines.length ? lines.reduce((a, it) => a + (it.quantity || 0), 0) : f.quantity.value,
        unit: lines.length === 1 ? lines[0].unit : 'job',
        unit_price: lines.length ? (lines.reduce((a, it) => a + it.quantity * it.unit_price, 0) /
                                    Math.max(lines.reduce((a, it) => a + it.quantity, 0), 1)) : f.unit_price.value,
        items: lines,
      };
      if (f.valid_until) payload.valid_until = f.valid_until.value;
      $('#jobErr').textContent = '';
      try {
        let clientId = f.client_id.value;
        if (!clientId && f.new_name && f.new_name.value.trim()) {
          const created = await api('/api/clients', { method: 'POST', body: JSON.stringify({
            name: f.new_name.value, phone: f.new_phone.value,
            whatsapp: f.new_whatsapp.value || f.new_phone.value, email: f.new_email.value,
            whatsapp_updates: f.new_whatsapp_updates.checked, email_updates: f.new_email_updates.checked,
            kind: f.new_kind.value, address: f.new_address.value }) });
          clientId = created.id;
          S.clients.push(created);
          toast('Client ' + created.name + ' added', 'good');
        }
        if (!clientId) throw new Error('Pick a client, or register a new one.');
        if (!lines.length && !(Number(payload.quantity) > 0 || Number(payload.unit_price) > 0)) {
          throw new Error('Add at least one item line, or give a quantity and price.');
        }
        if (!lines.length && Number(f.unit_cost.value) > 0) {
          // The cost of a single-line job has nowhere to live on the jobs row, so it is
          // kept as one item line. The maths is identical, the profit is not lost.
          payload.items = [{ title: payload.title, size: payload.size,
            quantity: Number(payload.quantity) || 1, unit: payload.unit,
            unit_price: Number(payload.unit_price) || 0, unit_cost: Number(f.unit_cost.value) }];
        }
        payload.client_id = clientId;
        if (id) {
          const saved = await api('/api/jobs/' + id, { method: 'PUT', body: JSON.stringify(payload) });
          if (saved.offlineQueued) { closeModal(); toast('Job changes saved on this device; waiting to sync.', 'good'); return; }
          closeModal(); toast('Job updated', 'good'); await render(); openJobDrawer(Number(id));
          return;
        }
        const job = await api('/api/jobs', { method: 'POST', body: JSON.stringify(payload) });
        if (job.kind !== 'Quote' && Number(f.deposit.value) > 0) {
          await api('/api/jobs/' + job.id + '/payment', { method: 'POST', body: JSON.stringify({
            amount: f.deposit.value, method: f.method.value, kind: 'Deposit' }) });
        }
        closeModal();
        if (job.offlineQueued) {
          toast('Job saved on this device; it will appear in Print jobs after sync.', 'good');
          return;
        }
        toast((job.kind === 'Quote' ? 'Quoted ' : 'Booked ') + job.ref + ' — ' + money(job.total), 'good');
        await render(); go('jobs', { kind: job.kind }, job.id);
      } catch (err) { $('#jobErr').textContent = err.message; }
    });
  }
  const payFormEl = card.querySelector('#payForm');
  if (payFormEl) payFormEl.addEventListener('submit', async (e) => {
    e.preventDefault();
    const f = e.target;
    const payload = {
      amount: f.amount.value, kind: f.kind.value, method: f.method.value,
      reference: f.reference.value, paid_at: f.paid_at.value, note: f.note.value,
    };
    const jobSel = f.querySelector('[name=job_id]');
    const clientSel = f.querySelector('[name=client_id]');
    if (jobSel) payload.job_id = jobSel.value || null;
    if (clientSel) payload.client_id = clientSel.value;
    $('#payErr').textContent = '';
    try {
      const saved = await api('/api/payments', { method: 'POST', body: JSON.stringify(payload) });
      closeModal();
      if (saved.offlineQueued) { toast('Payment saved on this device; waiting to sync.', 'good'); return; }
      toast('Received ' + money(saved.amount) + ' from ' + saved.client, 'good');
      await afterMutation();
      if (saved.job_id) openJobDrawer(saved.job_id);
    } catch (err) { $('#payErr').textContent = err.message; }
  });
  const clientFormEl = card.querySelector('#clientForm');
  if (clientFormEl) clientFormEl.addEventListener('submit', async (e) => {
    e.preventDefault();
    const f = e.target;
    const id = f.dataset.clientId;
    const payload = { name: f.name.value, phone: f.phone.value, whatsapp: f.whatsapp.value,
      email: f.email.value, whatsapp_updates: f.whatsapp_updates.checked,
      email_updates: f.email_updates.checked, kind: f.kind.value,
      address: f.address.value, notes: f.notes.value };
    $('#clientErr').textContent = '';
    try {
      const saved = id ? await api('/api/clients/' + id, { method: 'PUT', body: JSON.stringify(payload) })
                      : await api('/api/clients', { method: 'POST', body: JSON.stringify(payload) });
      S.clients = [];
      closeModal();
      if (saved.offlineQueued) { toast('Client saved on this device; waiting to sync.', 'good'); return; }
      toast(id ? 'Client updated' : 'Client ' + saved.name + ' added', 'good');
      await refreshChrome();
      await render();
      if (!id) go(S.route.view, {}, saved.id);
      else openClientDrawer(saved.id);
    } catch (err) { $('#clientErr').textContent = err.message; }
  });
  const expenseFormEl = card.querySelector('#expenseForm');
  if (expenseFormEl) expenseFormEl.addEventListener('submit', async (e) => {
    e.preventDefault();
    const f = e.target;
    const id = f.dataset.expenseId;
    const payload = {
      amount: f.amount.value, category: f.category.value, payee: f.payee.value,
      method: f.method.value, spent_on: f.spent_on.value, reference: f.reference.value,
      job_id: f.job_id.value || null, note: f.note.value,
    };
    $('#expenseErr').textContent = '';
    try {
      const saved = id
        ? await api('/api/expenses/' + id, { method: 'PUT', body: JSON.stringify(payload) })
        : await api('/api/expenses', { method: 'POST', body: JSON.stringify(payload) });
      if (saved.offlineQueued) { closeModal(); toast('Expense saved on this device; waiting to sync.', 'good'); return; }
      closeModal(); toast(id ? 'Expense updated' : 'Money out recorded', 'good');
      await afterMutation(true);
      if (payload.job_id) openJobDrawer(Number(payload.job_id));
    } catch (err) { $('#expenseErr').textContent = err.message; }
  });
  const spoilageFormEl = card.querySelector('#spoilageForm');
  if (spoilageFormEl) spoilageFormEl.addEventListener('submit', async (e) => {
    e.preventDefault();
    const f = e.target;
    const id = f.dataset.spoilageId;
    const payload = {
      job_id: f.job_id.value, quantity: f.quantity.value, amount: f.amount.value,
      spoiled_on: f.spoiled_on.value, reason: f.reason.value,
    };
    $('#spoilageErr').textContent = '';
    try {
      const saved = id
        ? await api('/api/spoiled/' + id, { method: 'PUT', body: JSON.stringify(payload) })
        : await api('/api/spoiled', { method: 'POST', body: JSON.stringify(payload) });
      if (saved.offlineQueued) { closeModal(); toast('Spoilage record saved on this device; waiting to sync.', 'good'); return; }
      closeModal(); toast(id ? 'Spoilage record updated' : 'Spoilage recorded against ' + saved.ref, 'good');
      await afterMutation(true);
      if (payload.job_id) openJobDrawer(Number(payload.job_id));
    } catch (err) { $('#spoilageErr').textContent = err.message; }
  });
  const leadFormEl = card.querySelector('#leadForm');
  if (leadFormEl) leadFormEl.addEventListener('submit', async (e) => {
    e.preventDefault();
    const f = e.target;
    const id = f.dataset.leadId;
    const payload = {
      name: f.name.value, phone: f.phone.value, whatsapp: f.whatsapp.value,
      email: f.email.value, source: f.source.value, interest: f.interest.value,
      value: f.value.value, stage: f.stage.value, follow_up: f.follow_up.value, note: f.note.value,
    };
    $('#leadErr').textContent = '';
    try {
      const saved = id ? await api('/api/leads/' + id, { method: 'PUT', body: JSON.stringify(payload) })
                       : await api('/api/leads', { method: 'POST', body: JSON.stringify(payload) });
      closeModal();
      if (saved.offlineQueued) { toast('Enquiry saved on this device; waiting to sync.', 'good'); return; }
      toast(id ? 'Enquiry updated' : 'Enquiry written down', 'good');
      const keep = S.route.params.toString();
      const target = '#/leads/' + saved.id + (keep ? '?' + keep : '');
      if (location.hash === target) { await refreshChrome(); await render(); }
      else location.hash = target;
    } catch (err) { $('#leadErr').textContent = err.message; }
  });
  const convertEl = card.querySelector('#convertLeadForm');
  if (convertEl) {
    const total = () => {
      const f = convertEl;
      $('#cvTotal').textContent = money(Math.max((Number(f.quantity.value) || 0) *
        (Number(f.unit_price.value) || 0), 0));
    };
    convertEl.addEventListener('input', total);
    convertEl.addEventListener('change', total);
    total();
    convertEl.addEventListener('submit', async (e) => {
      e.preventDefault();
      const f = e.target;
      $('#convertErr').textContent = '';
      const payload = {
        title: f.title.value, category: f.category.value, quantity: f.quantity.value,
        unit: f.unit.value, unit_price: f.unit_price.value, due_date: f.due_date.value,
        priority: f.priority.value,
      };
      try {
        const res = await api('/api/leads/' + f.dataset.leadId + '/convert',
          { method: 'POST', body: JSON.stringify(payload) });
        closeModal();
        if (res.offlineQueued) { toast('Booking saved on this device; waiting to sync.', 'good'); return; }
        S.clients = [];
        toast('Booked ' + res.job.ref + ' for ' + res.lead.name, 'good');
        await afterMutation(true);
        go('jobs', { kind: 'Job' }, res.job.id);
      } catch (err) { $('#convertErr').textContent = err.message; }
    });
  }
}

/* ------------------------------------------------------------------ actions */
async function afterMutation(keepDrawer) {
  await refreshChrome();
  await render();
  if (!keepDrawer) closeDrawer();
}
const ACTIONS = {
  'install-app'() { installApp().catch(fail); },
  async 'shop-password'(el) {
    try { passwordForm((await api('/api/shop')).login); } catch (err) { fail(err); }
  },
  async 'shop-password-off'() {
    try { passwordOffForm((await api('/api/shop')).login); } catch (err) { fail(err); }
  },
  'setup-page'() { window.open('/setup', '_blank'); },
  'shop-devices'() { render().catch(fail); },
  'shop-security'() { render().catch(fail); },
  async 'book-copy'() {
    try {
      await api('/api/backups', { method: 'POST', body: '{}' });
      toast('The book was copied and counted against itself', 'good');
      await render();
    } catch (err) { fail(err); }
  },
  async 'sign-out-all-devices'() {
    try {
      const answer = await api('/api/devices', { method: 'DELETE' });
      toast((answer.devices || 0) + ' other device(s) thrown out — they will have to know the' +
            ' password again', 'good');
      await render();
    } catch (err) { fail(err); }
  },
  async 'device-revoke'(el) {
    try {
      await api('/api/devices/' + el.dataset.deviceRevoke, { method: 'DELETE' });
      toast('That device is signed out.', 'good');
      await render();
    } catch (err) { fail(err); }
  },
  async 'sign-out'() {
    try {
      await api('/api/logout', { method: 'POST', body: '{}' });
      loginScreen('Signed out of the shop book.');
    } catch (err) { fail(err); }
  },
  'toggle-more'() {
    const sheet = $('#moresheet');
    if (!sheet) return;
    sheet.hidden = !sheet.hidden;
    $('#moreTab').setAttribute('aria-expanded', String(!sheet.hidden));
    if (!sheet.hidden) sheet.scrollTop = 0;
  },
  async 'sync-now'() { await syncOfflineQueue(); },
  async 'sync-keep'(el) {
    const row = await offlineRead('outbox', el.dataset.syncKeep);
    if (!row || row.state !== 'conflict' || !row.currentEtag) return;
    row.baseEtag = row.currentEtag;
    row.operationId = (window.crypto && crypto.randomUUID) ? crypto.randomUUID()
      : String(Date.now()) + '-' + Math.random().toString(36).slice(2);
    row.state = 'pending';
    row.error = '';
    delete row.current;
    delete row.currentEtag;
    await offlineWrite('outbox', row);
    toast('Saved change queued for review-resolved sync', 'good');
    await paintNetworkStatus();
    if (navigator.onLine) await syncOfflineQueue();
    else await render();
  },
  async 'sync-discard'(el) {
    const row = await offlineRead('outbox', el.dataset.syncDiscard);
    if (!row || !confirm('Discard this saved local change? The shared version will remain.')) return;
    row.state = 'discarded';
    await offlineWrite('outbox', row);
    toast('Local change discarded');
    await paintNetworkStatus();
    await render();
  },
  async 'new-job'(el) { if (!S.clients.length) await loadClients(); jobForm(null, el.dataset.client); },
  async 'new-quote'(el) { if (!S.clients.length) await loadClients(); jobForm(null, el.dataset.client, true); },
  async 'new-client'() { clientForm(null); },
  async 'receive-payment'(el) {
    if (!S.clients.length) await loadClients();
    S.openJobs = await api('/api/jobs?debtors=1&sort=due');
    paymentForm({});
  },
  'close-modal'() { closeModal(); },
  'close-drawer'() { closeDrawer(); },
  'clear-filters'() { go(S.route.view, {}); },
  'backup'() { window.location = '/api/backup'; toast('Backup downloading — keep it somewhere safe', 'good'); },
  'theme-set'(el) { setTheme(el.dataset.themeSet); },
  'export'(el) {
    const p = {};
    S.route.params.forEach((v, k) => { p[k] = v; });
    delete p.status;
    window.location = '/api/export/' + el.dataset.export + '.csv?' + qs(p);
  },
  async 'toggle-newclient'(el) {
    const box = $('#newClientBox');
    box.hidden = !box.hidden;
    el.textContent = box.hidden ? '+ Register a new client' : '− Cancel new client';
    if (!box.hidden) box.querySelector('input').focus();
  },
  async 'del-job'(el) {
    if (!confirm('Delete ' + el.dataset.delName + ' and its payments? This cannot be undone.')) return;
    try { await api('/api/jobs/' + el.dataset.delJob, { method: 'DELETE' }); closeDrawer(); toast('Job deleted'); await afterMutation(true); }
    catch (err) { fail(err); }
  },
  async 'del-payment'(el) {
    if (!confirm('Remove this payment of ' + money(el.dataset.delAmount) + ' from the book?')) return;
    try { await api('/api/payments/' + el.dataset.delPayment, { method: 'DELETE' }); toast('Payment removed'); await afterMutation(); }
    catch (err) { fail(err); }
  },
  async 'pay-job'(el) {
    if (!S.clients.length) await loadClients();
    S.openJobs = await api('/api/jobs?debtors=1&sort=due');
    const job = await api('/api/jobs/' + el.dataset.payJob);
    paymentForm({ jobId: job.id, name: job.client + ' · ' + job.ref, amount: job.balance, balance: job.balance });
  },
  async 'pay-client'(el) {
    if (!S.clients.length) await loadClients();
    S.openJobs = await api('/api/jobs?debtors=1&sort=due');
    paymentForm({ clientId: Number(el.dataset.payClient), name: el.dataset.payName });
  },
  async 'new-job-for'(el) { await jobForm(null, Number(el.dataset.newJobFor)); },
  async 'edit-job'(el) { const job = await api('/api/jobs/' + el.dataset.editJob); await jobForm(job); },
  async 'edit-client'(el) { const c = await api('/api/clients/' + el.dataset.editClient); clientForm(c); },
  async 'archive-client'(el) {
    const archiving = el.dataset.archived === '0';
    try {
      await api('/api/clients/' + el.dataset.archiveClient + (archiving ? '' : '/restore'),
        { method: archiving ? 'DELETE' : 'POST' });
      S.clients = []; toast(archiving ? 'Client archived' : 'Client restored', 'good'); closeDrawer(); await afterMutation(true);
    } catch (err) { fail(err); }
  },
  async 'status'(el) {
    const id = Number(el.dataset.statusJob);
    try {
      const before = (S.boot && S.boot.to_send) || 0;
      await api('/api/jobs/' + id + '/status', { method: 'POST', body: JSON.stringify({ status: el.dataset.status }) });
      await refreshChrome();
      const owed = ((S.boot && S.boot.to_send) || 0) - before;
      toast(owed > 0 ? 'Marked as ' + el.dataset.status + ' — client message ready to send'
                     : 'Marked as ' + el.dataset.status, 'good');
      await afterMutation(true); openJobDrawer(id);
    } catch (err) { fail(err); }
  },
  async 'notify-queue'(el) {
    const id = Number(el.dataset.notifyQueue);
    try {
      await api('/api/jobs/' + id + '/notify', { method: 'POST', body: JSON.stringify({ event: el.dataset.notifyEvent }) });
      toast(el.dataset.notifyEvent + ' message written for ' + (S.notify.client || 'the client'), 'good');
      await paintNotify(id);
    } catch (err) { fail(err); }
  },
  async 'notify-open'(el) {
    const m = S.notify.byId[Number(el.dataset.notifyOpen)];
    if (!m) return;
    if (!openExternal(m.link)) { toast('This Mac would not open ' + m.channel + ' — copy the words instead', 'bad'); return; }
    if (m.state !== 'Sent') {
      try { await api('/api/notifications/' + m.id + '/state', { method: 'POST', body: JSON.stringify({ state: 'Opened' }) }); }
      catch (e) { /* the words are on the screen whatever the book says */ }
    }
    toast('Opened in ' + m.channel + '. Press send there, then mark it sent here.', 'good');
    paintNotify(m.job_id);
  },
  async 'notify-state'(el) {
    const m = S.notify.byId[Number(el.dataset.notifyState)];
    try {
      await api('/api/notifications/' + (m ? m.id : el.dataset.notifyState) + '/state',
        { method: 'POST', body: JSON.stringify({ state: el.dataset.notifyTo }) });
      toast(el.dataset.notifyTo === 'Sent' ? 'Marked as sent — the client is up to date on that'
                                           : 'Back in the queue', 'good');
      if (m) paintNotify(m.job_id);
    } catch (err) { fail(err); }
  },
  async 'notify-retry'(el) {
    const id = Number(el.dataset.notifyRetry);
    try {
      await api('/api/notifications/' + id + '/retry', { method: 'POST', body: '{}' });
      toast('Delivery retry queued', 'good');
      const message = S.notify.byId[id];
      if (message) await paintNotify(message.job_id);
    } catch (err) { fail(err); }
  },
  async 'notify-copy'(el) {
    const m = S.notify.byId[Number(el.dataset.notifyCopy)];
    if (!m) return;
    let ok = false;
    try { await navigator.clipboard.writeText(m.body); ok = true; } catch (e) { ok = false; }
    toast(ok ? 'Copied — paste it into WhatsApp or Mail' : 'Select the words and copy them yourself',
      ok ? 'good' : 'bad');
  },
  async 'notify-del'(el) {
    const m = S.notify.byId[Number(el.dataset.notifyDel)];
    if (!m) return;
    if (!confirm('Take the ' + m.event + ' ' + m.channel + ' message out of the queue?\n\n' +
      'The job record keeps what was already written.')) return;
    try {
      await api('/api/notifications/' + m.id, { method: 'DELETE' });
      toast('Message removed from the queue');
      paintNotify(m.job_id);
    } catch (err) { fail(err); }
  },
  async 'convert-job'(el) {
    try {
      const job = await api('/api/jobs/' + el.dataset.convertJob);
      if (!confirm('Turn ' + job.ref + ' into a booked job?\n\n' +
        'It gets a CH- reference and starts counting as money owed. ' +
        'Anything already paid on the quote is kept.')) return;
      const saved = await api('/api/jobs/' + el.dataset.convertJob + '/convert',
        { method: 'POST', body: JSON.stringify({ status: 'Pending' }) });
      closeModal();
      if (saved.offlineQueued) { toast('Quote booking saved on this device; waiting to sync.', 'good'); return; }
      toast('Booked as ' + saved.ref, 'good');
      await afterMutation(true);
      go('jobs', { kind: 'Job' }, saved.id);
    } catch (err) { fail(err); }
  },
  async 'new-expense'(el) {
    const jobs = await api('/api/jobs?status=open&sort=due');
    expenseForm(null, el.dataset.job, jobs);
  },
  async 'new-spoilage'(el) {
    const jobs = await api('/api/jobs?status=all&kind=Job&sort=created');
    spoilageForm(null, jobs, el.dataset.newSpoilage || null);
  },
  async 'edit-spoilage'(el) {
    const [row, jobs] = await Promise.all([
      api('/api/spoiled/' + el.dataset.editSpoilage),
      api('/api/jobs?status=all&kind=Job&sort=created'),
    ]);
    spoilageForm(row, jobs);
  },
  async 'del-spoilage'(el) {
    if (!confirm('Remove the spoiled-work record for ' + el.dataset.delQuantity + ' items on ' +
        el.dataset.delJob + '? Its spoilage cost will also be removed from that job.')) return;
    try {
      await api('/api/spoiled/' + el.dataset.delSpoilage, { method: 'DELETE' });
      toast('Spoilage record removed'); await refreshChrome(); await render();
    } catch (err) { fail(err); }
  },
  async 'job-expense'(el) {
    const jobs = await api('/api/jobs?status=open&sort=due');
    expenseForm(null, Number(el.dataset.jobExpense), jobs);
  },
  async 'edit-expense'(el) {
    const row = await api('/api/expenses/' + el.dataset.editExpense);
    const jobs = await api('/api/jobs?status=open&sort=due');
    expenseForm(row, null, jobs);
  },
  async 'del-expense'(el) {
    if (!confirm('Remove this expense of ' + money(el.dataset.delAmount) + ' from the book?')) return;
    try { await api('/api/expenses/' + el.dataset.delExpense, { method: 'DELETE' }); toast('Expense removed'); await afterMutation(true); }
    catch (err) { fail(err); }
  },
  async 'new-lead'() { leadForm(null); },
  async 'edit-lead'(el) { const l = await api('/api/leads/' + el.dataset.editLead); leadForm(l); },
  async 'del-lead'(el) {
    if (!confirm('Delete the enquiry from ' + el.dataset.delName + '? This cannot be undone.')) return;
    try { await api('/api/leads/' + el.dataset.delLead, { method: 'DELETE' }); closeDrawer(); toast('Enquiry removed'); await afterMutation(true); }
    catch (err) { fail(err); }
  },
  async 'lead-stage'(el) {
    try {
      await api('/api/leads/' + el.dataset.stageLead + '/stage', { method: 'POST', body: JSON.stringify({ stage: el.dataset.leadStage }) });
      toast('Moved to ' + el.dataset.leadStage, 'good'); await afterMutation(true); openLeadDrawer(Number(el.dataset.stageLead));
    } catch (err) { fail(err); }
  },
  async 'convert-lead'(el) {
    const l = await api('/api/leads/' + el.dataset.convertLead);
    convertLeadForm(l);
  },
  async 'momo-check'() {
    try {
      const res = await api('/api/momo', { method: 'POST', body: JSON.stringify({ check: 1 }) });
      S.momo = res;
      if (res.found) { await afterMutation(true); toast('Took in ' + pluralise(res.found, 'payment notice') + ' from Messages', 'good'); return; }
      paintMomo(res);
      await refreshChrome();
      if (res.status) toast(res.status, 'bad'); else toast('Messages held nothing new');
    } catch (err) { fail(err); }
  },
  async 'momo-watch'() {
    try {
      const res = await api('/api/momo', { method: 'POST', body: JSON.stringify({ watch: !(S.momo && S.momo.watching) }) });
      S.momo = res; paintMomo(res);
      toast(res.watching ? 'Watching Messages every twenty seconds' : 'Stopped watching Messages', res.watching ? 'good' : '');
    } catch (err) { fail(err); }
  },
  async 'momo-auto'() {
    try {
      const res = await api('/api/momo', { method: 'POST', body: JSON.stringify({ auto: !(S.momo && S.momo.auto) }) });
      S.momo = res;
      if (res.auto && !confirm('Book every matched alert on its own?\n\nA payment is then entered as soon as the text arrives, ' +
        'with the client\'s name as the reference. You can still undo it in Accounts, but nothing will wait for your eye. ' +
        'Turn it off again any time from this panel.')) {
        const back = await api('/api/momo', { method: 'POST', body: JSON.stringify({ auto: 0 }) });
        S.momo = back; paintMomo(back); return;
      }
      paintMomo(res);
      toast(res.auto ? 'Alerts will book themselves when the payer matches a client' : 'Each notice will wait for you to press Book',
        res.auto ? 'good' : '');
    } catch (err) { fail(err); }
  },
  async 'momo-paste'() {
    const box = $('#momoPaste');
    const raw = box ? box.value.trim() : '';
    if (!raw) { toast('Paste the alert text first', 'bad'); return; }
    try {
      const res = await api('/api/momo', { method: 'POST', body: JSON.stringify({ text: raw }) });
      const n = res.notice;
      if (!n) { toast('That notice was already in the list'); box.value = ''; paintMomo(res); return; }
      if (n.state === 'Booked') toast('Booked ' + money(n.amount) + ' for ' + (n.client || 'the client'), 'good');
      else if (n.direction === 'Out') toast('That reads as money going out — press Record send on it');
      else toast('Read ' + (n.amount ? money(n.amount) : 'a notice') + ' from it' +
        (n.client ? ' · ' + n.client : '') + ' — check it below', 'good');
      box.value = '';
      await afterMutation(true);
    } catch (err) { fail(err); }
  },
  async 'momo-book'(el) {
    const row = el.closest('.sig');
    const clientId = row.querySelector('.momo-client').value;
    const amount = row.querySelector('.momo-amt').value;
    if (!clientId) { toast('Say whose money this is first', 'bad'); return; }
    if (!(Number(amount) > 0)) { toast('Type the amount the client sent', 'bad'); return; }
    try {
      const res = await api('/api/momo/' + row.dataset.sig + '/book', {
        method: 'POST', body: JSON.stringify({ amount: amount, client_id: clientId, job_id: row.querySelector('.momo-job').value }),
      });
      toast('Booked ' + money(res.payment.amount) + ' for ' + res.payment.client +
        ' · ' + (res.payment.ref ? 'against ' + res.payment.ref : 'kept as account credit'), 'good');
      await afterMutation(true);
    } catch (err) { fail(err); }
  },
  async 'momo-send'(el) {
    const row = el.closest('.sig');
    const clientId = row.querySelector('.momo-client').value;
    const amount = row.querySelector('.momo-amt').value;
    const cat = row.querySelector('.momo-cat');
    if (!(Number(amount) > 0)) { toast('Type the amount that went out', 'bad'); return; }
    if (!clientId && !confirm('Nobody in the book is chosen, so this goes in as the shop paying out ' +
      money(Number(amount)) + (cat ? ' for ' + cat.value.toLowerCase() : '') + '.\n\n' +
      'Press Cancel instead if the money actually went back to one of your clients — then choose them ' +
      'and it books as a refund on their account.')) return;
    try {
      const res = await api('/api/momo/' + row.dataset.sig + '/send', {
        method: 'POST', body: JSON.stringify({
          amount: amount, client_id: clientId, job_id: row.querySelector('.momo-job').value,
          category: cat ? cat.value : '',
        }),
      });
      const r = res.recorded;
      toast(r.recorded_as === 'refund'
        ? 'Refunded ' + money(r.amount) + ' to ' + r.client +
          ' · ' + (r.ref ? 'against ' + r.ref : 'their account')
        : 'Recorded ' + money(r.amount) + ' as money out · ' + r.category +
          (r.payee ? ' · ' + r.payee : ''), 'good');
      await afterMutation(true);
    } catch (err) { fail(err); }
  },
  async 'momo-ignore'(el) {
    try {
      await api('/api/momo/' + el.closest('.sig').dataset.sig + '/ignore', { method: 'POST', body: JSON.stringify({}) });
      toast('Put aside — it stays in the notices, out of the money');
      await afterMutation(true);
    } catch (err) { fail(err); }
  },
};

function restoreFocus() {
  const q = S.route.params.get('q');
  if (q) {
    const el = $('#topbar [data-filter="q"]');
    if (el) { el.focus(); el.setSelectionRange(el.value.length, el.value.length); }
  }
}

/* ------------------------------------------------------------------- appearance
   Light, dark, or whatever the Mac is set to. The choice is kept in localStorage, and the
   head script of index.html reads it before the first paint so the screen never flashes the
   wrong palette. The native shell seeds the same key at launch (its web storage does not
   survive a restart) and mirrors every change into its own settings. */
const THEME_KEY = "crispprint-theme";
const THEME_MODES = ["light", "dark", "system"];
const prefersDark = window.matchMedia("(prefers-color-scheme: dark)");

function themeMode() {
  let stored = null;
  try { stored = localStorage.getItem(THEME_KEY); } catch (e) { stored = null; }
  return THEME_MODES.indexOf(stored) >= 0 ? stored : "system";
}

function paintTheme(mode) {
  const root = document.documentElement;
  root.dataset.themeMode = mode;
  root.dataset.theme = (mode === "dark" || (mode === "system" && prefersDark.matches)) ? "dark" : "light";
  $$("[data-theme-set]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.themeSet === mode)));
}

function setTheme(mode) {
  if (THEME_MODES.indexOf(mode) < 0) return;
  try { localStorage.setItem(THEME_KEY, mode); } catch (e) { /* private mode: the look still changes */ }
  paintTheme(mode);
  // Window chrome and the menu bar belong to the shell, not the page.
  try { window.webkit.messageHandlers.theme.postMessage(mode); } catch (e) { /* browser tab */ }
}

prefersDark.addEventListener("change", () => {
  if (document.documentElement.dataset.themeMode === "system") paintTheme("system");
});

/* ------------------------------------------------------------------- wiring */
const ACTION_SEL = ['[data-action]'].concat(Object.keys(ACTIONS).map((k) => '[data-' + k + ']')).join(',');
function actionFor(el) {
  if (el.dataset.action) return ACTIONS[el.dataset.action];
  const hit = Object.keys(ACTIONS).filter((k) => el.hasAttribute('data-' + k))[0];
  return hit ? ACTIONS[hit] : null;
}
document.addEventListener('click', (e) => {
  if (e.target.closest('.drawer-scrim')) { closeDrawer(); return; }
  if (e.target.closest('.modal-scrim')) { closeModal(); return; }
  const cell = e.target.closest('.qe');
  if (cell) { openEditor(cell); return; }
  const actor = e.target.closest(ACTION_SEL);
  if (actor) {
    const fn = actionFor(actor);
    if (fn) { e.preventDefault(); fn(actor, e); return; }
  }
  const tab = e.target.closest('.tab[data-filter]');
  if (tab) { setFilter(tab.dataset.filter, tab.dataset.value); return; }
  // Reaching this point means the click landed on a real field: typing in it, or opening
  // its picker, is not the same as choosing the row around it.
  if (e.target.closest('input,select,textarea')) return;
  const open = e.target.closest('[data-open]');
  if (open && !e.target.closest('a,button')) {
    const cut = open.dataset.open.split('/');
    go(cut[0], {}, cut[1]);
  }
  const leadRow = e.target.closest('[data-open-lead]');
  if (leadRow && !e.target.closest('a,button')) go('leads', {}, leadRow.dataset.openLead);
});
document.addEventListener('change', (e) => {
  const momoCtl = e.target.closest('.momo-client, .momo-amt');
  if (momoCtl) { syncMomoRow(momoCtl.closest('.sig')); return; }
  const ctl = e.target.closest('[data-filter]');
  if (ctl && (ctl.tagName === 'SELECT' || ctl.type === 'date')) setFilter(ctl.dataset.filter, ctl.value);
});
document.addEventListener('input', (e) => {
  const ctl = e.target.closest('input[data-filter="q"]');
  if (!ctl) return;
  clearTimeout(S.timer);
  S.timer = setTimeout(() => setFilter('q', ctl.value, true), 260);
});
document.addEventListener('submit', (e) => {
  const nf = e.target.closest('[data-note-form]');
  if (!nf) return;
  e.preventDefault();
  api('/api/jobs/' + nf.dataset.noteForm + '/note', { method: 'POST', body: JSON.stringify({ note: nf.note.value }) })
    .then(() => { toast('Note added to the record', 'good'); openJobDrawer(Number(nf.dataset.noteForm)); })
    .catch(fail);
});
function setFilter(key, value, quiet) {
  const p = {};
  S.route.params.forEach((v, k) => { p[k] = v; });
  p[key] = value || '';
  const hash = '#/' + S.route.view + (qs(p) ? '?' + qs(p) : '');
  if (hash === location.hash) return;
  if (quiet) {
    history.replaceState(null, '', hash);
    S.route = parseHash();
    render();
  } else {
    location.hash = hash; /* the hashchange listener renders */
  }
}
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    if (!$('#modal').hidden) closeModal();
    else if (!$('#drawer').hidden) closeDrawer();
  }
  if (e.key === '/' && !/input|textarea|select/i.test(document.activeElement.tagName)) {
    const s = $('#topbar input[data-filter="q"]');
    if (s) { e.preventDefault(); s.focus(); }
  }
});
window.addEventListener('hashchange', () => { closeMore(); render(); });
window.addEventListener('online', () => {
  S.connected = true;
  paintNetworkStatus();
  syncOfflineQueue().then(() => render()).catch(fail);
});
window.addEventListener('offline', () => {
  S.connected = false;
  paintNetworkStatus();
});

/* The native menu bar has no buttons of its own, so it asks for these by name.
   If the current screen happens to show the matching button, it is handed over
   so any data-* it carries (client, job) still reaches the form. */
window.chrisphics = {
  act(name) {
    const handler = ACTIONS[name];
    if (typeof handler !== 'function') return false;
    handler(document.querySelector('[data-action="' + name + '"]') || document.body);
    return true;
  },
  theme(mode) { setTheme(mode); },
};

(async function start() {
  paintTheme(themeMode());
  shellChrome();
  try {
    await paintNetworkStatus();
    const session = await api('/api/session');
    S.session = session;
    paintSession();
    if (session.required && !session.authenticated) { loginScreen(); return; }
    await boot();
    await refreshChrome();
    if (navigator.onLine) await syncOfflineQueue();
    if (!location.hash) location.hash = '#/dashboard';
    await render();
    launchShortcut();
  } catch (err) {
    if (err.status === 401) loginScreen(err.message);
    else $('#view').innerHTML = emptyState('The app cannot reach its database', err.message +
      ' — connect to the shop Wi-Fi to sync; saved changes remain on this device.');
  }
})();
