#!/usr/bin/env node
/* The sweep the page runs *while a tab is visible* — node test_dashboard_sweep.mjs

   What it protects is as much #9's rule as the sweep itself: a hidden tab
   collects nothing and records nothing, so the second trigger may only be a
   second trigger for the same look(). */

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(join(here, 'dashboard.html'), 'utf8');
const page = (() => {
  return html.slice(html.lastIndexOf('<script>') + '<script>'.length, html.lastIndexOf('</script>'));
})();

const NOW = Date.parse('2026-10-07T12:00:00Z');
const snapshot = at => ({
  schema: 1, producer: { code: 'k', version: 1 }, generatedAt: at,
  tiers: [], drafts: [], closed: [], errors: [], hidden_bots: 0,
  changes: { previousAt: null, summary: {}, items: {}, gone: [] },
});

/* The page is a document, so every element it asks for is a stub that answers
   with another stub; the two things under test are the ones it is not. */
function mount(opts = {}) {
  const collected = [], stamped = [], elements = {}, created = [];
  const stub = () => new Proxy(
    { classList: { add(){}, remove(){}, toggle(){}, contains: () => false }, style: {}, dataset: {},
      appendChild: stub, addEventListener(){}, remove(){}, focus(){} },
    { get: (t, k) => (k in t ? t[k] : (t[k] = () => stub())), set: (t, k, v) => (t[k] = v, true) });
  /* Every node the page makes goes through el(), which sets className and then
     textContent on it — so a node that records those two writes is how a test
     reads what the page wrote, without a DOM. */
  const node = () => new Proxy(
    { classList: { add(){}, remove(){}, toggle(){}, contains: () => false }, style: {}, dataset: {},
      appendChild: stub, addEventListener(){}, remove(){}, focus(){} },
    { get: (t, k) => (k in t ? t[k] : (t[k] = () => stub())),
      set: (t, k, v) => { t[k] = v; if (k === 'textContent') created.push({cls: t.className, text: v}); return true; } });
  let now = NOW, interval = null, state = 'visible';
  const ctx = {
    console, setTimeout, clearTimeout, Proxy,
    document: { createElement: () => node(), createTextNode: stub, createDocumentFragment: stub,
                querySelector: stub, querySelectorAll: () => [], getElementsByTagName: () => [],
                getElementById: id => (elements[id] ||= stub()), addEventListener(){},
                get visibilityState() { return state; } },
    // a look is a collection and a record: which one it moved is the whole test
    fetch: async url => {
      collected.push(now);
      // a rail that never answers is a rail that must not hold the board
      if (opts.railsSilent && (String(url).startsWith('/specs') || String(url).startsWith('/sessions'))) {
        return new Promise(() => {});
      }
      return { json: async () => snapshot(new Date(now).toISOString()) };
    },
    localStorage: { getItem: () => stamped.at(-1) ?? null, setItem: (k, v) => stamped.push(v) },
    setInterval: (fn, ms) => { interval = { fn, ms }; },
    Date: class extends Date {
      constructor(...a) { super(...(a.length ? a : [now])); }
      static now() { return now; }
    },
  };
  ctx.globalThis = ctx;      // attention-view.js reaches for `window`, the page for `global`
  ctx.global = ctx;
  ctx.window = ctx;
  ctx.addEventListener = () => {};

  const context = vm.createContext(ctx);
  vm.runInContext(readFileSync(join(here, 'attention-view.js'), 'utf8'), context);
  vm.runInContext(page, context);
  return {
    collected, stamped, created, intervalMs: () => interval && interval.ms,
    element: id => elements[id],
    textsOf: cls => created.filter(c => c.cls === cls).map(c => c.text),
    hide: () => { state = 'hidden'; },
    show: () => { state = 'visible'; },
    age: ms => { now += ms; },
    sweep: () => interval.fn(),
  };
}

const tick = () => new Promise(r => setTimeout(r, 20));

const p = mount();
await tick();                                   // the opening load() is async
const boot = p.collected.length;                // whatever a first look asks for
assert.equal(p.intervalMs(), 5 * 60 * 1000, 'the sweep runs on SWEEP_MS');
assert.equal(p.stamped.length, 1, 'an opening look records the look');
assert.ok(boot > 0, 'an opening look collects');

p.hide();
p.age(6 * 60 * 1000);
await p.sweep();
await tick();
assert.equal(p.collected.length, boot, 'a hidden tab collects nothing');
assert.equal(p.stamped.length, 1, 'a hidden tab records nothing');

p.show();
p.age(61 * 60 * 1000);                          // the snapshot is now over STALE_MS old
await p.sweep();
await tick();
assert.ok(p.collected.length > boot, 'a visible tab refreshes a stale snapshot');

p.age(60 * 1000);                               // fresh again: a look stamps, it does not collect
const afterRefresh = p.collected.length, stamps = p.stamped.length;
await p.sweep();
await tick();
assert.equal(p.collected.length, afterRefresh, 'a fresh snapshot is not re-collected');
assert.equal(p.stamped.length, stamps + 1, 'a fresh snapshot records the look');

/* The rails: specs keeps a column of its own wherever three fit — specs left,
   board middle, live work right — and reads first in the stack, never after the
   board, where a short screen buries it. */
/* The board is the queue, and the rails are read beside it: a first look paints
   from the queue alone, and a rail with nothing to show says it is reading until
   its own read lands. Here /specs and /sessions never answer at all. */
const r = mount({ railsSilent: true });
await tick();
assert.match(r.element('stamp').textContent, /^snapshot/, 'the board paints with the rails still out');
assert.ok(r.textsOf('railtip').some(t => /Reading the backlog/.test(t || '')),
  'the specs rail says it is reading, not that there is nothing');
assert.ok(r.textsOf('railtip').some(t => /Reading what is open right now/.test(t || '')),
  'and so does the agent rail');
assert.ok(!r.textsOf('railtip').some(t => /No open spec issues|No session open right now/.test(t || '')),
  'neither rail claims to be empty before its read has landed');

const queries = html.slice(html.indexOf('@media'), html.indexOf('#app{min-width:0}'));
assert.ok(/@media \(max-width:1000px\)\{[\s\S]*?#specs\{order:-1/.test(queries),
  'the stacked layout reads specs first');
assert.ok(!/#specs\{order:[1-9]/.test(queries), 'no width drops specs below the board');

console.log('dashboard sweep: all checks passed');
