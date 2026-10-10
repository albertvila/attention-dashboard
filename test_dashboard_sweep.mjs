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
const snapshot = (at, tiers = []) => ({
  schema: 1, producer: { code: 'k', version: 1 }, generatedAt: at,
  tiers, drafts: [], closed: [], errors: [], hidden_bots: 0,
  changes: { previousAt: null, summary: {}, items: {}, gone: [] },
});

/* The page is a document, so every element it asks for is a stub that answers
   with another stub; the two things under test are the ones it is not. */
function mount(opts = {}) {
  const collected = [], stamped = [], elements = {}, created = [], titles = [];
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
      set: (t, k, v) => { t[k] = v; if (k === 'textContent') created.push({cls: t.className, text: v});
                          if (k === 'title') titles.push({cls: t.className, title: v}); return true; } });
  let now = NOW, interval = null, state = 'visible';
  const ctx = {
    console, setTimeout, clearTimeout, Proxy,
    // the browser's own escape, which the page uses to build a selector from a key
    CSS: { escape: s => String(s) },
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
      if (opts.team && String(url).startsWith('/team')) {
        return { json: async () => opts.team };
      }
      if (opts.comments && String(url).startsWith('/comments')) {
        return { json: async () => opts.comments };
      }
      return { json: async () => snapshot(new Date(now).toISOString(), opts.tiers) };
    },
    localStorage: { getItem: () => opts.seen ?? stamped.at(-1) ?? null, setItem: (k, v) => stamped.push(v) },
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
    collected, stamped, created, titles, intervalMs: () => interval && interval.ms,
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

/* What a card is waiting on, named on the card itself: `blocked by` what Jira
   says holds it up, `linked` the ticket it merely links to — the one you raised
   with another team. Every part of both lines comes off the row. */
const linkCard = {
  chip: 'JIRA', key: 'FIRE-77999', ref: 'FIRE-77999', title: 'high findings disclosed', url: 'u',
  container: '', states: [{ key: 'with-support', label: 'with support', tier: 'waiting', tone: 'quiet' }],
  facts: [], labels: [], children: [], section: 'waiting', tier: 'waiting', times: {},
  blocked_by: [{ key: 'FIDI-275', url: 'u2', status: 'In Progress', status_category: 'In Progress',
                 type: 'Bug', summary: 'soda checks fail' }],
  linked: [{ key: 'FISRE-27667', url: 'u3', status: 'In Progress', status_category: 'In Progress',
             type: 'Service Request', summary: 'rotate the leaked secrets' }],
};
const L = mount({ railsSilent: true, tiers: [
  { key: 'needs', title: 'Needs you now', items: [] },
  { key: 'ready', title: 'Ready when you are', items: [] },
  { key: 'waiting', title: 'Waiting on others', items: [linkCard] }] });
await tick();
assert.deepEqual(L.textsOf('blocked-label'), ['blocked by ']);
assert.deepEqual(L.textsOf('linked-label'), ['linked ']);
assert.ok(L.textsOf('jira').includes('FIDI-275'), 'the blocker is named by key');
assert.ok(L.textsOf('jira').includes('FISRE-27667'), 'so is the ticket you are waiting on');
assert.ok(L.textsOf('jira-summary').includes('rotate the leaked secrets'));
assert.equal(L.created.filter(c => String(c.cls) === 'badge wip' && c.text === 'In Progress').length, 2,
  'each line carries its own status badge');

/* The comment read lands after the board has painted, and only then does the
   line change: a thread is not in the snapshot, so the card is rebuilt in place
   when it arrives. It needs a last look to be newer than — the first look flags
   nothing, the same rule the change marks follow. */
const saidCard = {
  chip: 'JIRA', key: 'RBT-9', ref: 'RBT-9', title: 'a ticket with a thread', url: 'u', container: '',
  states: [{ key: 'in-progress', label: 'in progress', tier: 'waiting', tone: 'info' }],
  facts: [], labels: [], children: [], section: 'needs', tier: 'waiting', times: {},
};
const saidThread = { 'RBT-9': [
  { at: '2026-10-07T12:30:00.000Z', who: 'Ada', text: 'can you re-run this?', mine: false }] };
const saidTiers = [{ key: 'needs', title: 'Needs you now', items: [saidCard] },
                   { key: 'ready', title: 'Ready when you are', items: [] },
                   { key: 'waiting', title: 'Waiting on others', items: [] }];

// a first look flags nothing: no stored look, so a thread is not news yet
const fresh = mount({ railsSilent: true, comments: saidThread, tiers: saidTiers });
await tick();
assert.ok(!fresh.textsOf('do').includes('answer Ada'), 'a first look flags nothing, comment or not');

// a reader who has looked before: the comment that landed since is the line
const C = mount({ railsSilent: true, seen: '2026-10-07T12:00:00.000Z', comments: saidThread, tiers: saidTiers });
await tick(); await tick();
assert.ok(C.textsOf('do').includes('answer Ada'), 'the thread lands on the card as the line');

/* The Team box is a roster, not one flat list: one group per team, named by its
   slug, and somebody on two teams stands under each — while the reader is the row
   above them and never inside a group. A face the read gave no team at all keeps
   a row rather than falling off the panel. */
const T = mount({ railsSilent: true, team: {
  you: { login: 'albertvila' },
  people: [{ login: 'ana', name: 'Ana Plaza', teams: ['team-den'] },
           { login: 'bo', name: 'Bo Diaz', teams: ['squad-data', 'team-den'] },
           { login: 'cy', name: 'Cy Ruiz', teams: ['squad-data'] },
           { login: 'di', name: 'Di Solo' }] } });
await tick(); await tick();
const roster = T.titles.filter(t => String(t.cls).startsWith('teammate')).map(t => t.title);
assert.deepEqual(T.textsOf('teamname'), ['squad-data', 'team-den'], 'one group per team, named by its slug');
assert.deepEqual(roster, ['albertvila', 'Di Solo', 'Bo Diaz', 'Cy Ruiz', 'Ana Plaza', 'Bo Diaz'],
  'you first, then each team\u2019s members, the two-team face under both, a teamless face still on it');

console.log('dashboard sweep: all checks passed');
