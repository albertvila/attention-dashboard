#!/usr/bin/env node
/* The shared consumer rules are the one piece of JS every surface runs, so they
   get a runnable check of their own:  node test_attention_view.mjs

   Covers what used to be copied per surface: change flags, the closed/new rule,
   ghosts (hidden in Needs, never for mail), the Mail fold, snooze parking, and
   an ack that only holds while the card has not moved. */

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
new Function(readFileSync(join(here, 'attention-view.js'), 'utf8'))();   // defines window.AttentionView
const { overlay } = globalThis.AttentionView;

const NOW = '2026-10-02T12:00:00.000Z';
const row = (over) => ({
  chip: 'JIRA', key: 'FIRE-1', title: 't', ref: 'FIRE-1', url: 'u', container: '',
  states: [{ key: 'in-progress', label: 'in progress', tier: 'waiting', tone: 'info' }],
  facts: [], labels: [], children: [], section: 'waiting', tier: 'waiting',
  times: {}, firstSeenAt: '2026-10-01T12:00:00.000Z', lastChangedAt: '2026-10-01T12:00:00.000Z',
  ...over,
});
const data = (over = {}) => ({
  generatedAt: NOW, changes: { previousAt: '2026-10-02T11:00:00.000Z', summary: {}, items: {}, gone: [] },
  tiers: [{ key: 'needs', title: 'Needs you now', items: [] },
          { key: 'ready', title: 'Ready when you are', items: [] },
          { key: 'waiting', title: 'Waiting on others', items: [row()] }],
  drafts: [], closed: [], hidden_bots: 0, errors: [],
  ...over,
});

// --- flags ------------------------------------------------------------------
let view = overlay(data({ tiers: [{ key: 'needs', title: 'Needs you now', items: [] },
                                  { key: 'ready', title: 'Ready when you are', items: [] },
                                  { key: 'waiting', title: 'Waiting on others',
                                    items: [row({ key: 'N1', firstSeenAt: '2026-10-02T11:30:00.000Z' }),
                                            row({ key: 'M1', lastChangedAt: '2026-10-02T11:30:00.000Z',
                                                  lastChange: { kind: 'moved', label: 'waiting → CI failing', to_tier: 'needs' } }),
                                            row({ key: 'S1' })]}] }), { now: NOW });
let byKey = Object.fromEntries(view.tiers[2].rows.map(r => [r.key, r]));
assert.deepEqual(view.flag(byKey.N1), { kind: 'new' });
assert.deepEqual(view.flag(byKey.M1), { kind: 'moved', label: 'waiting → CI failing', to_tier: 'needs' });
assert.equal(view.flag(byKey.S1), null);
assert.equal(view.summaryText(), '1 new · 1 changed · 0 dropped since the previous snapshot');
// the timestamp renders in the reader's timezone, so only the shape is asserted
assert.equal(overlay(data(), { seenAt: '2026-10-02T11:45:00.000Z', now: NOW }).summaryText()
  .startsWith('0 new · 0 changed · 0 dropped since your last look ('), true);

// with no last look, the previous snapshot is the baseline: the row() fixture
// predates it, so nothing is new until a row appears after previousAt
assert.equal(overlay(data(), { now: NOW }).counts().new, 0);
assert.equal(overlay(data({ tiers: [{ key: 'needs', title: 'n', items: [] }, { key: 'ready', title: 'r', items: [] },
                                     { key: 'waiting', title: 'w',
                                       items: [row({ key: 'N1', firstSeenAt: '2026-10-02T11:30:00.000Z' })] }] }),
                     { now: NOW }).counts().new, 1);

// closed cards are never new
view = overlay(data({ tiers: [{ key: 'needs', title: 'n', items: [] }, { key: 'ready', title: 'r', items: [] },
                               { key: 'waiting', title: 'w', items: [] }],
                      closed: [row({ key: 'C1', section: 'closed', states: [{ key: 'merged', label: 'merged', tier: 'ready', tone: 'good' }] })] }),
               { now: NOW });
assert.equal(view.flag(view.folds.closed[0]), null);
assert.equal(view.counts().new, 0);

// --- ghosts -----------------------------------------------------------------
const ghost = row({ key: 'G1', section: 'needs', goneAt: '2026-10-02T11:30:00.000Z', change: { kind: 'gone' } });
const ghostWaiting = row({ key: 'G2', section: 'waiting', goneAt: '2026-10-02T11:30:00.000Z', change: { kind: 'gone' } });
const ghostMail = row({ chip: 'MAIL', key: 'G3', section: 'waiting', goneAt: '2026-10-02T11:30:00.000Z', change: { kind: 'gone' } });
view = overlay(data({ changes: { previousAt: '2026-10-02T11:00:00.000Z', summary: {}, items: {}, gone: [ghost, ghostWaiting, ghostMail] } }),
               { now: NOW });
assert.deepEqual(view.tiers[0].rows.map(r => r.key), []);          // needs: hidden
assert.deepEqual(view.tiers[2].rows.map(r => r.key), ['FIRE-1', 'G2']);  // waiting keeps its ghost
assert.equal(view.tiers[2].rows[1].change.kind, 'gone');
assert.equal(view.counts().gone, 3);                                // but the header still counts all three

// --- mail and parking -------------------------------------------------------
const mail = row({ chip: 'MAIL', key: 'mail/1', section: 'needs' });
view = overlay(data({ tiers: [{ key: 'needs', title: 'n', items: [mail] }, { key: 'ready', title: 'r', items: [] },
                              { key: 'waiting', title: 'w', items: [row()] }] }), { now: NOW });
assert.deepEqual(view.tiers[0].rows, []);                        // mail never sits in a tier
assert.deepEqual(view.folds.mail.map(r => r.key), ['mail/1']);
assert.deepEqual(view.folds.snoozed, []);

view = overlay(data({ tiers: [{ key: 'needs', title: 'n', items: [mail] }, { key: 'ready', title: 'r', items: [] },
                              { key: 'waiting', title: 'w', items: [row()] }] }),
               { snoozes: { 'mail/1': '2026-10-03T12:00:00.000Z', 'FIRE-1': '2026-10-03T12:00:00.000Z' }, now: NOW });
assert.deepEqual(view.folds.snoozed.map(r => r.key).sort(), ['FIRE-1', 'mail/1']);
assert.deepEqual(view.folds.mail, []);                            // parking beats the mail fold
assert.equal(view.tiers[2].count, 0);
assert.equal(view.formatWhen('2026-10-03T12:00:00.000Z').startsWith('until '), true);
// an expired snooze is not a snooze
assert.equal(overlay(data(), { snoozes: { 'FIRE-1': '2026-10-01T00:00:00.000Z' }, now: NOW }).folds.snoozed.length, 0);

// --- ack: holds until the card moves ---------------------------------------
const acks = { 'FIRE-1': '2026-10-02T11:30:00.000Z' };
view = overlay(data(), { acks, now: NOW });
assert.deepEqual(view.folds.acked.map(r => r.key), ['FIRE-1']);    // unchanged since the ack
assert.equal(view.tiers[2].count, 0);
view = overlay(data({ tiers: [{ key: 'needs', title: 'n', items: [] }, { key: 'ready', title: 'r', items: [] },
                               { key: 'waiting', title: 'w', items: [row({ lastChangedAt: '2026-10-02T11:45:00.000Z' })] }] }),
               { acks, now: NOW });
assert.deepEqual(view.folds.acked, []);                            // it moved: back on the board
assert.equal(view.tiers[2].count, 1);
assert.equal(view.flag(view.tiers[2].rows[0]).kind, 'moved');

// --- stale-code witness -----------------------------------------------------
assert.deepEqual(globalThis.AttentionView.stale({ producer: { code: 'aaa' } }, { producer: { code: 'aaa' } }),
  { stale: false, running: 'aaa', snapshot: 'aaa' });
assert.equal(globalThis.AttentionView.stale({ producer: { code: 'old' } }, { producer: { code: 'new' } }).stale, true);
assert.equal(globalThis.AttentionView.stale({}, { producer: { code: 'new' } }).stale, true);   // file predates the stamp
assert.equal(globalThis.AttentionView.stale({}, null).stale, false);                          // static hosting: unknown

// --- choices and labels -----------------------------------------------------
assert.deepEqual(view.choices.map(c => c.value), ['4', '24', '72', '168', 'ack']);
assert.equal(view.changeLabel({ kind: 'new' }), 'new');
assert.equal(view.changeLabel({ kind: 'gone' }), 'dropped');
assert.equal(view.changeLabel({ kind: 'moved', to_tier: 'needs', label: 'a → b' }), 'a → b');
assert.equal(view.changeTone({ kind: 'moved', to_tier: 'needs' }), 'moved');
assert.equal(view.changeTone({ kind: 'moved', to_tier: 'ready' }), 'moved out');
assert.equal(view.changeClass({ kind: 'moved', to_tier: 'needs' }), ' chg-moved');
assert.equal(view.changeClass({ kind: 'moved', to_tier: 'ready' }), ' chg-moved out');
assert.equal(view.changeClass({ kind: 'gone' }), ' chg-gone');
assert.equal(view.changeClass(null), '');

console.log('attention-view: all checks passed');
