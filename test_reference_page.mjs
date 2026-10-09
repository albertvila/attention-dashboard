#!/usr/bin/env node
/* The reference page's loop and filters — node test_reference_page.mjs

   The loop names a shape (/orchestrate-plan), never a harness — so the filter
   that name feeds has to reach both harness skills AND the install line has to
   say where they come from. The page also has to hold no trace of the sections
   it no longer renders. */

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(join(here, 'reference.html'), 'utf8');
const page = html.slice(html.lastIndexOf('<script>') + '<script>'.length, html.lastIndexOf('</script>'));

/* The page is a document, so each element it asks for is a plain node: two
   things under test are what it puts into #app and what the #q input handler
   builds from a filter. */
function mount() {
  const elements = {}, handlers = {};
  const el = id => {
    if (elements[id]) return elements[id];
    const node = { id, value: '', innerHTML: '', dataset: {}, style: {},
      classList: { toggle(){}, add(){}, remove(){}, contains: () => false },
      appendChild(){}, remove(){}, focus(){}, scrollIntoView(){},
      addEventListener(type, fn) { (handlers[id] ||= {})[type] = fn; } };
    return (elements[id] = node);
  };
  const ctx = {
    console,
    document: { getElementById: el, querySelectorAll: () => [], querySelector: () => null,
                createElement: () => el('made'), createTextNode: () => ({}),
                addEventListener(){}, },
  };
  ctx.window = ctx;
  ctx.global = ctx;
  ctx.globalThis = ctx;
  vm.createContext(ctx);
  vm.runInContext(page, ctx);
  return { elements, handlers };
}

const slice = (text, from, to) => text.slice(text.indexOf(from), text.indexOf(to));
const { elements, handlers } = mount();
const shown = () => elements.app.innerHTML;

/* ---- the page as it opens ---- */
const first = shown();
assert.ok(!first.includes('Automations'), 'the Automations section is gone');
assert.ok(!first.includes('Custom / yours'), 'the custom skills are gone');
assert.ok(!first.includes('mcp-scripting'), 'mcp-scripting is gone (its npm package is not installed)');
assert.ok(!first.includes('twg-bench-lite'), 'twg-bench-lite is gone (no longer shipped by the twg CLI)');
for (const surfaced of ['herdr-gpui-browser'])
  assert.ok(first.includes(surfaced), `${surfaced} surfaced into the page`);
assert.ok(!first.includes('paseo'), 'the paseo skills are off the page');

/* the loop and the run shapes name the shape, not the harness */
const loop = slice(first, '<h2>The loop</h2>', '<div class="loopback">');
for (const generic of ['/orchestrate-plan', '/orchestrate-threads', '/orchestrate-retro'])
  assert.ok(loop.includes(generic), `the loop shows ${generic}`);
assert.ok(!/\/orchestrate-(bb|herdr)-/.test(loop), 'the loop names no harness');
const shapes = slice(first, 'Step 5 — pick a shape per run', '<h2 id="skills">');
assert.ok(shapes.includes('/orchestrate-threads @.scratch/'), 'the run shapes use the generic command');
assert.ok(!/\/orchestrate-(bb|herdr)-/.test(shapes), 'the run shapes name no harness');
assert.ok(first.includes('ships for <b>BB</b> and <b>Herdr</b>'), 'the page says which orchestrators it ships for');

/* the install line is for a filter, not for the whole table */
assert.ok(!first.includes('install —'), 'no install line until something filters');

/* every listed skill carries a source the legend knows: the chip counts add up */
const total = +first.match(/Skills — (\d+) shown/)[1];
const chips = [...elements.legend.innerHTML.matchAll(/<b>(\d+)<\/b>/g)].map(m => +m[1]);
assert.equal(chips.reduce((a, b) => a + b, 0), total, `the legend accounts for all ${total} skills`);

/* ---- a skill pill: the alias reaches both harness skills, and names the source ---- */
elements.q.value = 'orchestrate-plan';
handlers.q.input();
const pill = shown();
assert.ok(pill.includes('Skills — 2 shown'), 'the alias filters to the two harness skills');
assert.ok(pill.includes('/orchestrate-bb-plan') && pill.includes('/orchestrate-herdr-plan'),
  'both harness skills are shown');
assert.ok(pill.includes('install —') && pill.includes('github.com/albertvila/dotfiles'),
  'the install line names where they come from');
assert.ok(!pill.includes('/orchestrate-bb-threads'), 'the alias does not leak the other shapes');
const cardAt = step => pill.lastIndexOf('<div class="card', pill.indexOf(`data-step="${step}"`));
const planCard = pill.slice(cardAt('plan'), cardAt('run'));
assert.ok(!planCard.includes('dimmed'), 'the plan step is not dimmed by its own shape filter');
assert.ok(pill.includes('dimmed'), 'steps the shape does not touch stay dimmed');

/* ---- a source chip: the filter and its install line agree ---- */
elements.q.value = '';
handlers.q.input();
handlers.legend.click({ target: { closest: () => ({ dataset: { src: 'twg' } }) } });
const chip = shown();
assert.ok(chip.includes('twg-jira'), 'the twg chip shows the twg skills');
assert.ok(!chip.includes('/orchestrate-bb-plan'), 'and nothing else');
assert.ok(chip.includes('twg skills install'), 'the twg install line says how they install');

/* ---- typing a source name shows that source's link ---- */
handlers.legend.click({ target: { closest: () => ({ dataset: { src: 'twg' } }) } });   // chip off again
elements.q.value = 'ponytail';
handlers.q.input();
const typed = shown();
assert.ok(typed.includes('Skills — 6 shown'), 'ponytail filters to its six skills');
assert.ok(typed.includes('github.com/DietrichGebert/ponytail'), 'and names where they come from');

/* ---- a query that spans two sources names both ---- */
elements.q.value = 'herdr';
handlers.q.input();
const spread = shown();
assert.ok(spread.includes('github.com/herdrdev/herdr') && spread.includes('github.com/albertvila/dotfiles'),
  'a query over two sources names both installs');

console.log('reference page: all checks passed');
