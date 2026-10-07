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
const page = (() => {
  const html = readFileSync(join(here, 'dashboard.html'), 'utf8');
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
function mount() {
  const collected = [], stamped = [], elements = {};
  const stub = () => new Proxy(
    { classList: { add(){}, remove(){}, toggle(){}, contains: () => false }, style: {}, dataset: {},
      appendChild: stub, addEventListener(){}, remove(){}, focus(){} },
    { get: (t, k) => (k in t ? t[k] : (t[k] = () => stub())), set: (t, k, v) => (t[k] = v, true) });
  let now = NOW, interval = null, state = 'visible';
  const ctx = {
    console, setTimeout, clearTimeout, Proxy,
    document: { createElement: stub, createTextNode: stub, createDocumentFragment: stub,
                querySelector: stub, querySelectorAll: () => [], getElementsByTagName: () => [],
                getElementById: id => (elements[id] ||= stub()), addEventListener(){},
                get visibilityState() { return state; } },
    // a look is a collection and a record: which one it moved is the whole test
    fetch: async () => { collected.push(now); return { json: async () => snapshot(new Date(now).toISOString()) }; },
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
    collected, stamped, intervalMs: () => interval && interval.ms,
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

console.log('dashboard sweep: all checks passed');
