// Dependency-free regressions: execute the shipped browser sources in isolated VMs.
// Run: node --test tests/test_frontend_review.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFileSync } from 'node:fs';

class Element {
  constructor(tag = 'div') {
    this.tagName = tag; this.children = []; this.dataset = {}; this.style = {};
    this.value = ''; this.textContent = ''; this.disabled = false; this.isConnected = true;
    this.listeners = {}; this._classes = new Set(); this._html = '';
    this.classList = {
      add: (...names) => names.forEach(n => { if (!n) throw Error('empty class'); this._classes.add(n); }),
      remove: (...names) => names.forEach(n => this._classes.delete(n)),
      contains: n => this._classes.has(n),
      toggle: (n, force = !this._classes.has(n)) => { if (force) this._classes.add(n); else this._classes.delete(n); return force; },
    };
  }
  set className(s) { this._classes = new Set(s.split(/\s+/).filter(Boolean)); }
  get className() { return [...this._classes].join(' '); }
  set innerHTML(s) { this._html = s; this.children.forEach(c => { c.isConnected = false; }); this.children = []; }
  get innerHTML() { return this._html; }
  appendChild(c) { c.parentElement = this; this.children.push(c); return c; }
  addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
  removeEventListener() {}
  emit(name, event = {}) { for (const fn of this.listeners[name] || []) fn(event); }
  querySelectorAll(sel) {
    const cls = sel.split(' ').at(-1).split(':')[0].replace(/^\./, '');
    const out = [];
    const visit = e => { for (const c of e.children) { if (c.classList.contains(cls)) out.push(c); visit(c); } };
    visit(this); return out;
  }
  querySelector(sel) {
    const found = this.querySelectorAll(sel)[0];
    if (found) return found;
    // The tests do not emulate an HTML parser; these are static template controls.
    if (['.del', '.caret', '.label'].includes(sel)) {
      const c = new Element(); c.className = sel.slice(1); return this.appendChild(c);
    }
    return null;
  }
  closest(sel) { if (this.classList.contains(sel.slice(1))) return this; return this.parentElement?.closest(sel); }
  getBoundingClientRect() { return this.rect || { left: 0, top: 0, right: 100, bottom: 100, width: 100, height: 100 }; }
  remove() { if (this.parentElement) this.parentElement.children = this.parentElement.children.filter(c => c !== this); this.isConnected = false; }
}

const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };
const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
function fixture() {
  const elements = new Map(), timers = new Map(), storage = new Map(), images = [], toasts = [];
  let nextTimer = 0;
  const el = id => { if (!elements.has(id)) { const e = new Element(); e.id = id; elements.set(id, e); } return elements.get(id); };
  const document = {
    body: new Element('body'), listeners: {},
    getElementById: el, createElement: tag => new Element(tag),
    querySelectorAll(sel) {
      if (sel.startsWith('[data-close=')) return [el(sel)];
      if (sel.startsWith('#')) { const [id, ...rest] = sel.split(' '); return el(id.slice(1)).querySelectorAll(rest.join(' ')); }
      return [...elements.values()].flatMap(e => e.querySelectorAll(sel));
    },
    querySelector(sel) { if (sel === '#tagger-tools .tool-option.selected') return el('chosen-tool'); return this.querySelectorAll(sel)[0] || null; },
    addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); },
    removeEventListener() {},
  };
  const sandbox = {
    console, document, URLSearchParams, AbortController,
    window: { innerHeight: 800, addEventListener() {} },
    Image: class extends Element { constructor() { super('img'); images.push(this); } },
    localStorage: { getItem: k => storage.get(k) || null, setItem: (k, v) => storage.set(k, v) },
    setTimeout: fn => { const id = ++nextTimer; timers.set(id, fn); return id; }, clearTimeout: id => timers.delete(id),
    setInterval: fn => { const id = ++nextTimer; timers.set(id, fn); return id; }, clearInterval: id => timers.delete(id),
    API: { get: async () => ({}), post: async () => ({}) },
    toast: (...args) => toasts.push(args),
    escapeHtml: s => String(s ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;'),
    formatDuration: String,
    SidePanel: { init() {}, open() {}, refresh() {} }, Viewer: { initKeyboard() {}, open() {} },
    Settings: { init() {} }, FolderTags: { init() {} }, Recycle: { init() {} }, promptDialog: async () => 'yes',
  };
  const ctx = vm.createContext(sandbox);
  for (const name of ['app', 'gallery', 'tree', 'metadata_panel', 'tags', 'jobprogress', 'tagger', 'imagetag']) {
    const source = readFileSync(new URL('../web/js/' + name + '.js', import.meta.url), 'utf8');
    vm.runInContext(source, ctx, { filename: name + '.js' });
  }
  const mods = vm.runInContext('({App, Gallery, TreeView, MetadataPanel, TagsEditor, JobProgress, Tagger, Imagetag})', ctx);
  el('sort-select').value = 'name'; el('page-size-input').value = '60'; el('chosen-tool').dataset.tool = 'test';
  mods.App.updateMissingBadge = () => {};
  const timer = async () => { const entry = timers.entries().next().value; assert.ok(entry, 'a timer is scheduled'); timers.delete(entry[0]); await entry[1](); };
  return { ...mods, ctx, el, document, timers, storage, images, toasts, timer };
}
function quietGallery(f) {
  for (const key of ['render', 'updatePager', 'updateResultInfo', 'updateSelInfo', 'scrollTop', 'saveBrowseState', 'loadVisibleThumbs']) f.Gallery[key] = () => {};
}
const item = (id, names = []) => ({ id, filename: id + '.jpg', path: '/test/' + id, type: 'image', tags: names.map(name => ({ name, source: 'manual' })) });

test('gallery resets page before dispatch and only latest response commits', async () => {
  const f = fixture(); quietGallery(f);
  const calls = [];
  f.ctx.API.get = url => { const d = deferred(); calls.push({ url, ...d }); return d.promise; };
  f.App.state.page = 3; f.App.state.filters.q = 'old';
  const a = f.Gallery.load(true);
  assert.equal(new URLSearchParams(calls[0].url.split('?')[1]).get('page'), '1');
  f.App.state.filters.q = 'new'; const b = f.Gallery.load(true);
  calls[1].resolve({ total: 1, items: [item(2)] }); await b;
  f.App.state.selected.add(2);
  calls[0].resolve({ total: 99, items: [item(1)] }); await a;
  assert.equal(f.App.state.items[0].id, 2); assert.equal(f.App.state.total, 1);
  assert.ok(f.App.state.selected.has(2));
});

test('obsolete gallery errors do not replace current UI feedback', async () => {
  const f = fixture(); quietGallery(f); const old = deferred(); let count = 0;
  f.ctx.API.get = () => ++count === 1 ? old.promise : Promise.resolve({ items: [], total: 0 });
  const a = f.Gallery.load(true); await f.Gallery.load(true);
  old.reject(Error('stale')); await a; assert.equal(f.toasts.length, 0);
});

test('page size clamps saved and changed values to API max before first load', async () => {
  const f = fixture(); f.storage.set('imagedb_page_size', '500');
  f.App.bindEvents = () => {}; f.TreeView.refresh = async () => {};
  f.Tagger.init = f.TagsEditor.init = f.Imagetag.init = () => {};
  f.Gallery.initBoxSelect = f.Gallery.initThumbSlider = () => {};
  const seen = []; f.Gallery.load = async () => seen.push(f.App.state.pageSize);
  await f.App.init(); assert.deepEqual(seen, [200]); assert.equal(f.el('page-size-input').max, '200');
  assert.equal(f.storage.get('imagedb_page_size'), '200');
  f.el('page-size-input').value = '201'; f.el('page-size-input').emit('change');
  assert.equal(f.App.state.pageSize, 200);
  f.el('page-size-input').value = '1'; f.el('page-size-input').emit('change');
  assert.equal(f.App.state.pageSize, 10);
});

test('single, ctrl, shift, clear and box selections retain actual card/image nodes', () => {
  const f = fixture(); f.App.state.items = [item(1), item(2), item(3)]; f.Gallery.render();
  const cards = [...f.el('gallery').children], image = cards[0].querySelector('.thumb-img');
  f.Gallery.render = () => assert.fail('selection must not rerender');
  const click = extra => ({ stopPropagation() {}, ...extra });
  cards[0].onclick(click()); assert.ok(cards[0].classList.contains('selected'));
  cards[1].onclick(click({ ctrlKey: true })); assert.equal(f.App.state.selected.size, 2);
  cards[2].onclick(click({ shiftKey: true })); assert.equal(f.App.state.selected.size, 3);
  f.Gallery.clearSelection(); assert.equal(f.App.state.selected.size, 0);
  f.Gallery.initBoxSelect();
  f.el('gallery').emit('mousedown', { target: f.el('gallery'), button: 0, clientX: 0, clientY: 0 });
  f.document.listeners.mouseup[0](); assert.equal(f.App.state.selected.size, 3);
  assert.equal(cards[0].querySelector('.thumb-img'), image);
  assert.equal(f.el('gallery').children[0], cards[0]);
});

test('thumbnail old load/error callbacks cannot mutate DOM/cache or clear new same-ID work', () => {
  const f = fixture(); const a = new AbortController(), b = new AbortController();
  const oldImg = new Element('img'), newImg = new Element('img');
  const oldBox = new Element(); oldBox.appendChild(oldImg);
  f.Gallery.loadOneThumb(oldImg, '1', a.signal);
  const oldWorker = f.images[0], lateLoad = oldWorker.onload, lateError = oldWorker.onerror;
  a.abort(); assert.equal(oldWorker.onload, null);
  f.Gallery.loadOneThumb(newImg, '1', b.signal); const newWorker = f.images[1];
  lateLoad(); lateError();
  assert.equal(f.Gallery._loading.get('1'), newWorker); assert.equal(oldImg.dataset.loaded, undefined);
  assert.equal(f.Gallery._thumbCache.size, 0); assert.equal(oldBox.innerHTML, '');
  newWorker.onload(); assert.equal(newImg.dataset.loaded, '1'); assert.equal(f.Gallery._loading.size, 0);
  assert.equal(f.Gallery._thumbCache.get('1'), newWorker);
});

test('thumbnail detached targets and already aborted requests are ignored', () => {
  const f = fixture(); const ctrl = new AbortController(), im = new Element('img');
  f.Gallery.loadOneThumb(im, '1', ctrl.signal); im.isConnected = false;
  f.images[0].onload(); assert.equal(im.dataset.loaded, undefined);
  ctrl.abort(); f.Gallery.loadOneThumb(new Element('img'), '2', ctrl.signal);
  assert.equal(f.images.length, 1);
});

test('tree last click wins including all-media click and rejected old checks', async () => {
  const f = fixture(), pending = []; let loads = 0;
  f.Gallery.load = () => { loads++; }; f.TreeView.updateActiveHighlight = () => {};
  f.ctx.API.post = () => { const d = deferred(); pending.push(d); return d.promise; };
  const parent = new Element();
  f.TreeView.renderNode(parent, { id: 1, name: 'A', path: '/A' }, 0);
  f.TreeView.renderNode(parent, { id: 2, name: 'B', path: '/B' }, 0);
  const e = { stopPropagation() {} };
  const a = parent.children[0].onclick(e), b = parent.children[1].onclick(e);
  pending[1].resolve({ exists: true }); await b; pending[0].resolve({ root_offline: true }); await a;
  assert.equal(f.App.state.currentFolderId, 2); assert.equal(loads, 1); assert.equal(f.toasts.length, 0);
  const c = parent.children[0].onclick(e); f.TreeView.render(); f.el('tree').children[0].onclick();
  pending[2].reject(Error('offline')); await c; assert.equal(f.App.state.currentFolderId, null);
  const unknown = parent.children[0].onclick(e); pending[3].resolve({ unknown: true }); await unknown;
  assert.equal(f.App.state.currentFolderId, 1);
});

test('tree refresh discards older snapshot', async () => {
  const f = fixture(), pending = []; f.TreeView.render = () => {};
  f.ctx.API.get = () => { const d = deferred(); pending.push(d); return d.promise; };
  const a = f.TreeView.refresh(), b = f.TreeView.refresh();
  pending[1].resolve({ tree: [{ id: 2 }], missing_total: 2 }); await b;
  pending[0].resolve({ tree: [{ id: 1 }], missing_total: 1 }); await a;
  assert.equal(f.App.state.tree[0].id, 2); assert.equal(f.App.state.missingTotal, 2);
});

test('metadata ignores stale successes/errors after selection and cache changes', async () => {
  const f = fixture(), pending = [], rendered = [];
  f.MetadataPanel.render = data => rendered.push(data.id);
  f.ctx.API.get = () => { const d = deferred(); pending.push(d); return d.promise; };
  const a = f.MetadataPanel.refresh([1]), b = f.MetadataPanel.refresh([2]);
  pending[1].resolve({ id: 2 }); await b; pending[0].resolve({ id: 1 }); await a;
  assert.deepEqual(rendered, [2]);
  const c = f.MetadataPanel.refresh([3]); await f.MetadataPanel.refresh([]);
  pending[2].reject(Error('stale')); await c;
  assert.equal(f.el('meta-groups').innerHTML, ''); assert.ok(!f.el('meta-status').textContent.includes('失败'));
  const d = f.MetadataPanel.refresh([4]); f.MetadataPanel.clearCache(); pending[3].resolve({ id: 4 }); await d;
  assert.equal(f.MetadataPanel._cache.size, 0);
});

test('manual tags use fresh records and fixed session IDs through add/remove and gallery refresh', async () => {
  const f = fixture(); quietGallery(f);
  f.App.state.selected.add(1); f.App.state.items = [item(1, ['outdated'])];
  const names = new Set(['old']), writes = [];
  f.ctx.API.get = async url => {
    if (url.startsWith('/api/media?')) return { items: [item(1, [...names])], total: 1 };
    if (url.startsWith('/api/tags?')) return { tags: [] };
    return item(1, [...names]);
  };
  f.ctx.API.post = async (url, body) => { writes.push(url); if (url.endsWith('/remove')) names.delete(body.tags[0]); else names.add(body.tags[0]); };
  await f.TagsEditor.open(); assert.ok(f.TagsEditor.currentTags.has('old')); assert.ok(!f.TagsEditor.currentTags.has('outdated'));
  f.el('tags-new').value = 'new'; await f.TagsEditor.addTag();
  assert.ok(f.TagsEditor.currentTags.has('new')); assert.ok(f.App.state.selected.has(1));
  // Even if navigation/selection changes, further editor writes target the original IDs.
  f.App.state.selected.clear(); f.App.state.selected.add(2);
  await f.TagsEditor.removeTag('old'); assert.ok(!f.TagsEditor.currentTags.has('old'));
  assert.deepEqual(writes, ['/api/media/1/tags', '/api/media/1/tags/remove']);
  assert.deepEqual([...f.TagsEditor.ids], [1]);
});

test('manual editor ignores closed/replaced session data and serializes mutations', async () => {
  const f = fixture(); f.TagsEditor.init(); const d = deferred();
  f.ctx.API.get = url => url.includes('/api/tags?') ? Promise.resolve({ tags: [] }) : d.promise;
  f.App.state.selected.add(1); const open = f.TagsEditor.open();
  f.el("[data-close='tags-modal']").onclick(); d.resolve(item(1, ['late'])); await open;
  assert.equal(f.TagsEditor.currentTags.size, 0); assert.ok(f.el('tags-modal').classList.contains('hidden'));
  f.ctx.API.get = async url => url.includes('/api/tags?') ? { tags: [] } : item(2);
  f.App.state.selected.clear(); f.App.state.selected.add(2); await f.TagsEditor.open();
  const save = deferred(); let writes = 0; f.ctx.API.post = () => { writes++; return save.promise; };
  f.el('tags-new').value = 'tag'; const a = f.TagsEditor.addTag(); await f.TagsEditor.addTag();
  f.el("[data-close='tags-modal']").onclick(); save.resolve({}); await a;
  assert.equal(writes, 1); assert.equal(f.el('tags-add').disabled, false);
});

test('job progress invalidates in-flight starts, polls, errors and timers on close', async () => {
  const f = fixture(), oldStart = deferred(); let completed = 0, failed = 0;
  const a = f.JobProgress.run('old', () => oldStart.promise, () => completed++, null, () => failed++);
  f.JobProgress.stopPolling(); oldStart.reject(Error('late')); await a;
  assert.equal(failed, 0); assert.equal(completed, 0);
  const poll = deferred(); f.ctx.API.get = () => poll.promise;
  await f.JobProgress.run('first', async () => ({ job_id: '1' }), () => completed++);
  f.JobProgress.stopPolling();
  const currentStart = deferred(); const next = f.JobProgress.run('current', () => currentStart.promise, () => completed++);
  poll.resolve({ status: 'done', result: {} }); await flush();
  assert.equal(f.JobProgress.isBusy(), true); assert.equal(completed, 0);
  assert.equal(f.el('imagetag-log-title').textContent, 'current'); assert.equal(f.timers.size, 0);
  f.JobProgress.stopPolling(); currentStart.resolve({ job_id: '2' }); await next;
});

test('job progress still completes current tasks and running polls schedule once', async () => {
  const f = fixture(); let calls = 0, result;
  f.ctx.API.get = async () => ++calls === 1 ? { status: 'running' } : { status: 'done', result: { ok: 1 } };
  await f.JobProgress.run('current', async () => ({ job_id: '1' }), r => { result = r; }); await flush();
  assert.equal(f.timers.size, 1); await f.timer(); assert.equal(result.ok, 1); assert.equal(f.JobProgress.isBusy(), false);
});

test('tagger close/reopen retains job and scope, invalidates old poll and restores terminal controls', async () => {
  const f = fixture(), pending = []; f.Tagger.renderTools = f.Tagger.renderScope = () => {};
  f.Tagger.loadTools = async () => {}; f.Gallery.load = async () => {};
  f.ctx.API.get = url => { const d = deferred(); pending.push({ url, ...d }); return d.promise; };
  f.Tagger.jobId = 'job-1'; f.Tagger.scopeIds = [7]; f.Tagger.scopeType = 'folder';
  await f.Tagger.open(); assert.equal(pending.length, 1); assert.equal(f.el('tagger-start').disabled, true);
  f.Tagger.close(); await f.Tagger.openWithFolder(9);
  assert.deepEqual([...f.Tagger.scopeIds], [7]); assert.equal(pending.length, 2);
  pending[0].resolve({ status: 'done', total: 10, done: 10 }); await flush();
  assert.equal(f.Tagger.jobId, 'job-1'); assert.equal(f.el('tagger-start').disabled, true);
  pending[1].resolve({ status: 'running', total: 10, done: 5 }); await flush(); assert.equal(f.timers.size, 1);
  const tick = f.timer(); pending[2].resolve({ status: 'done', total: 10, done: 10 }); await tick;
  assert.equal(f.Tagger.jobId, null); assert.equal(f.el('tagger-start').disabled, false);
  assert.equal(f.el('tagger-cancel-job').style.display, 'none'); assert.equal(f.timers.size, 0);
});

test('tagger duplicate starts blocked; close during start still permits reconnect', async () => {
  const f = fixture(), start = deferred(); let starts = 0;
  f.Tagger._visible = true; f.Tagger.scopeIds = [1]; f.Tagger.loadTools = async () => {};
  f.Tagger.renderTools = f.Tagger.renderScope = () => {};
  f.ctx.API.post = () => { starts++; return start.promise; };
  f.ctx.API.get = async () => ({ status: 'running', total: 2, done: 0 });
  const a = f.Tagger.start(); await f.Tagger.start(); assert.equal(starts, 1);
  f.Tagger.close(); start.resolve({ job_id: 'new-job' }); await a;
  assert.equal(f.Tagger.jobId, 'new-job'); assert.equal(f.timers.size, 0);
  await f.Tagger.open(); await flush(); assert.equal(f.el('tagger-start').disabled, true); assert.equal(f.timers.size, 1);
});

test('tagger read failure retains reconnect/cancel, start failure releases controls', async () => {
  const f = fixture(); f.Tagger._visible = true; f.Tagger.jobId = 'job';
  f.ctx.API.get = async () => { throw Error('network'); };
  await f.Tagger.pollProgress(); assert.equal(f.Tagger.jobId, 'job'); assert.equal(f.timers.size, 0);
  assert.match(f.el('tagger-msg').textContent, /重连/);
  let cancelUrl; f.ctx.API.post = async url => { cancelUrl = url; }; await f.Tagger.cancel(); assert.equal(cancelUrl, '/api/tagging/jobs/job/cancel');
  f.Tagger.jobId = null; f.ctx.API.post = async () => { throw Error('start failed'); }; await f.Tagger.start();
  assert.equal(f.el('tagger-start').disabled, false); assert.equal(f.Tagger._starting, false);
});

test('import result log escapes and displays partial failures', () => {
  const f = fixture(); f.Imagetag.showImportResult({ media: 1, tags: 2, files: 1, errors: [{ dir: '<dir>', filename: '<file>', error: '<bad>' }] });
  const html = f.el('imagetag-log').innerHTML;
  assert.match(html, /&lt;dir&gt;/); assert.match(html, /&lt;file&gt;/); assert.match(html, /&lt;bad&gt;/);
  assert.match(f.el('imagetag-log-title').textContent, /有错误/);
});

test('manual tag detail refresh bounds concurrent requests to eight', async () => {
  const f = fixture(), pending = [];
  f.TagsEditor.ids = Array.from({ length: 20 }, (_, i) => i + 1);
  let active = 0, maxActive = 0;
  f.ctx.API.get = url => {
    const d = deferred(); active++; maxActive = Math.max(maxActive, active);
    pending.push({ id: Number(url.split('/').at(-1)), ...d });
    return d.promise.finally(() => active--);
  };
  const refresh = f.TagsEditor.refreshTags(0);
  assert.equal(pending.length, 8);
  for (const d of pending.slice(0, 8)) d.resolve(item(d.id)); await flush();
  assert.equal(pending.length, 16);
  for (const d of pending.slice(8, 16)) d.resolve(item(d.id)); await flush();
  assert.equal(pending.length, 20);
  for (const d of pending.slice(16)) d.resolve(item(d.id)); await refresh;
  assert.equal(maxActive, 8); assert.equal(active, 0);
});

test('manual tag source strings are escaped in chips', () => {
  const f = fixture();
  f.TagsEditor.currentTags = new Map([['tag', { sources: new Set(['<script>']), count: 1 }]]);
  f.TagsEditor.renderList(); assert.match(f.el('tags-list').children[0].innerHTML, /&lt;script&gt;/);
});

test('purge retained records remain explained in the rescan dialog', async () => {
  const f = fixture(); f.Gallery.load = f.TreeView.refresh = async () => {};
  f.ctx.API.post = async () => ({ removed: 1, retained: 2 });
  f.App.showRescanResult({ missing: 3 }, 1); await f.el('rescan-purge').onclick();
  assert.match(f.el('rescan-summary').textContent, /保留 2/);
  assert.match(f.el('rescan-summary').textContent, /无法安全确认/);
  assert.equal(f.el('rescan-modal').classList.contains('hidden'), false);
  assert.equal(f.el('rescan-purge').style.display, 'none');
});

test('closed job progress cannot surface late polling errors', async () => {
  const f = fixture(), poll = deferred();
  f.ctx.API.get = () => poll.promise;
  await f.JobProgress.run('old', async () => ({ job_id: 'old' }), () => assert.fail('closed callback'));
  f.JobProgress.stopPolling(); f.JobProgress.show('new', 'new task');
  poll.reject(Error('old network error')); await flush();
  assert.equal(f.el('imagetag-log-title').textContent, 'new');
  assert.equal(f.el('imagetag-log').innerHTML, ''); assert.equal(f.timers.size, 0);
});

test('move and trash report partial backend errors rather than success only', async () => {
  const f = fixture(); f.App.state.selected.add(1); f.Gallery.load = f.TreeView.refresh = async () => {};
  f.ctx.API.post = async () => ({ moved: 0, skipped: 1, errors: ['unreadable source'] });
  await f.App.moveSelected(); assert.equal(f.toasts.at(-1)[1], 'err'); assert.match(f.toasts.at(-1)[0], /unreadable source/);
  f.App.state.selected.add(1); f.ctx.API.post = async () => ({ removed: 1, records_only: 1, errors: ['permission denied'] });
  await f.App.trashSelected(); assert.equal(f.toasts.at(-1)[1], 'err'); assert.match(f.toasts.at(-1)[0], /permission denied/);
  assert.match(f.toasts.at(-1)[0], /仅删除记录 1/);
});
