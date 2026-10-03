#!/usr/bin/env node
/* The shared consumer rules are the one piece of JS every surface runs, so they
   get a runnable check of their own:  node test_attention_view.mjs

   Covers what used to be copied per surface: change flags, the closed/new rule,
   ghosts (hidden in Needs, never for mail or support), the With support and Mail
   folds, snooze parking, an ack that only holds while the card has not moved,
   the fold summaries, and the next-card line. */

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

// --- With support: a Jira card in Support Investigating ---------------------
const SUPPORT = { key: 'with-support', label: 'with support', tier: 'waiting', tone: 'quiet' };
const sup = (over = {}) => row({ key: 'SUP-1', ref: 'SUP-1', age: '1d', states: [SUPPORT],
  times: { updated: '2026-10-01T12:00:00.000Z' }, ...over });
const supData = (items) => data({ tiers: [{ key: 'needs', title: 'n', items: [] },
                                           { key: 'ready', title: 'r', items: [] },
                                           { key: 'waiting', title: 'w', items }] });

view = overlay(supData([sup(), row()]), { now: NOW });
assert.deepEqual(view.tiers[2].rows.map(r => r.key), ['FIRE-1']);    // out of the queue
assert.deepEqual(view.folds.support.map(r => r.key), ['SUP-1']);     // in its own fold
assert.equal(view.tiers[2].count, 1);                                // the count is the queue, not the fold

// the header's state decides, children included — membership is by card, not member
view = overlay(supData([sup({ children: [row({ key: 'C1', states: [{ key: 'needs-reply', label: 'needs reply', tier: 'needs', tone: 'warn' }] })] })]), { now: NOW });
assert.deepEqual(view.folds.support.map(r => r.key), ['SUP-1']);
assert.deepEqual(view.folds.support[0].children.map(r => r.key), ['C1']);
assert.deepEqual(view.tiers[2].rows, []);
// a more urgent header stays in its own tier, its support child along for the ride
view = overlay(data({ tiers: [{ key: 'needs', title: 'n', items: [row({ key: 'N1', children: [sup()] })] },
                               { key: 'ready', title: 'r', items: [] }, { key: 'waiting', title: 'w', items: [] }] }), { now: NOW });
assert.deepEqual(view.tiers[0].rows.map(r => r.key), ['N1']);
assert.deepEqual(view.folds.support, []);

// parking beats the support fold, and coming back lands in the fold, not the queue
view = overlay(supData([sup()]), { snoozes: { 'SUP-1': '2026-10-03T12:00:00.000Z' }, now: NOW });
assert.deepEqual(view.folds.snoozed.map(r => r.key), ['SUP-1']);
assert.deepEqual(view.folds.support, []);
assert.equal(view.tiers[2].count, 0);
view = overlay(supData([sup()]), { snoozes: { 'SUP-1': '2026-10-01T00:00:00.000Z' }, now: NOW });
assert.deepEqual(view.folds.support.map(r => r.key), ['SUP-1']);     // expired: back in the fold
assert.deepEqual(view.tiers[2].rows, []);
view = overlay(supData([sup()]), { acks: { 'SUP-1': '2026-10-02T11:00:00.000Z' }, now: NOW });
assert.deepEqual(view.folds.acked.map(r => r.key), ['SUP-1']);
assert.deepEqual(view.folds.support, []);
view = overlay(supData([sup()]), { acks: { 'SUP-1': '2026-09-30T00:00:00.000Z' }, now: NOW });
assert.deepEqual(view.folds.support.map(r => r.key), ['SUP-1']);     // released: still not the queue

// a vanished one does not ghost back into the queue; the header still counts the drop
const supGhost = sup({ goneAt: '2026-10-02T11:30:00.000Z', change: { kind: 'gone' } });
view = overlay({ ...supData([row()]), changes: { previousAt: '2026-10-02T11:00:00.000Z', summary: {}, items: {}, gone: [supGhost] } }, { now: NOW });
assert.deepEqual(view.tiers[2].rows.map(r => r.key), ['FIRE-1']);
assert.equal(view.counts().gone, 1);
// a new one still counts as new
assert.equal(overlay(supData([sup({ firstSeenAt: '2026-10-02T11:30:00.000Z' })]), { now: NOW }).counts().new, 1);
// and a changed one still counts as changed
view = overlay(supData([sup({ lastChangedAt: '2026-10-02T11:30:00.000Z',
  lastChange: { kind: 'moved', label: 'waiting → with support' } })]), { now: NOW });
assert.equal(view.counts().changed, 1);
// an ordinary in-progress row stays exactly where it was
assert.deepEqual(overlay(supData([row()]), { now: NOW }).tiers[2].rows.map(r => r.key), ['FIRE-1']);

// --- fold summaries: what a collapsed fold is hiding ------------------------
const mailRow = (over = {}) => row({ chip: 'MAIL', key: 'mail/' + (over.id || '1'), ref: '',
  states: [{ key: 'needs-reply', label: 'needs reply', tier: 'needs', tone: 'warn' }], ...over });
view = overlay(data({ tiers: [{ key: 'needs', title: 'n', items: [
    mailRow({ id: '1', age: '5d', times: { updated: '2026-09-27T12:00:00.000Z' }, facts: [{ label: 'unread', tone: 'warn' }] }),
    mailRow({ id: '2', age: '2d', times: { updated: '2026-09-30T12:00:00.000Z' } })] },
  { key: 'ready', title: 'r', items: [] }, { key: 'waiting', title: 'w', items: [] }] }), { now: NOW });
assert.equal(view.notes.mail(), 'oldest 5d · 1 unread');            // the oldest row's own age
assert.equal(view.notes.drafts(), '');                             // nothing hidden, nothing to say
assert.equal(view.notes.support(), '');
// unread shows even at zero, and no updated time means no oldest clause
view = overlay(data({ tiers: [{ key: 'needs', title: 'n', items: [mailRow({ id: '1' })] },
                              { key: 'ready', title: 'r', items: [] }, { key: 'waiting', title: 'w', items: [] }] }), { now: NOW });
assert.equal(view.notes.mail(), '0 unread');
// a conflicted draft says so; a draft without conflicts is just old
view = overlay(data({ drafts: [row({ key: 'D1', draft: true, age: '4d',
  times: { updated: '2026-09-28T12:00:00.000Z' },
  states: [{ key: 'conflicts', label: 'merge conflicts', tier: 'needs', tone: 'bad' }] })] }), { now: NOW });
assert.equal(view.notes.drafts(), 'conflicts · oldest 4d');
assert.deepEqual(view.folds.drafts.map(r => r.key), ['D1']);        // a conflicted draft stays in Drafts
assert.deepEqual(view.tiers[0].rows, []);
view = overlay(data({ drafts: [row({ key: 'D1', draft: true, age: '4d',
  times: { updated: '2026-09-28T12:00:00.000Z' } })] }), { now: NOW });
assert.equal(view.notes.drafts(), 'oldest 4d');
assert.equal(overlay(data({ drafts: [row({ key: 'D1', draft: true })] }), { now: NOW }).notes.drafts(), '');
// support names the oldest age of the rows it is hiding
view = overlay(supData([sup(), sup({ key: 'SUP-2', age: '1h', times: { updated: '2026-10-02T11:00:00.000Z' } })]), { now: NOW });
assert.equal(view.notes.support(), 'oldest 1d');

// --- the next-card line -----------------------------------------------------
const readyRow = (over = {}) => row({ key: 'o/r#7', ref: 'o/r#7', title: 'a ready PR',
  states: [{ key: 'ready', label: 'ready', tier: 'ready', tone: 'good' }], ...over });
const lineData = (needs, ready, waiting) => data({ tiers: [
  { key: 'needs', title: 'n', items: needs }, { key: 'ready', title: 'r', items: ready },
  { key: 'waiting', title: 'w', items: waiting || [row()] }] });
assert.equal(overlay(lineData([row()], [readyRow()]), { now: NOW }).nextCard(), null);   // work in Needs: no line
assert.equal(overlay(lineData([], [readyRow()]), { now: NOW }).nextCard(),
  'Nothing needs you. Next: o/r#7 · ready');
assert.equal(overlay(lineData([], [readyRow({ ref: '', title: 'a ready PR' })]), { now: NOW }).nextCard(),
  'Nothing needs you. Next: a ready PR · ready');                                         // no ref: the title
assert.equal(overlay(lineData([], []), { now: NOW }).nextCard(), 'Nothing needs you.');
// never a waiting row, never a fold: only Ready is work you can pick up
assert.equal(overlay(lineData([], []), { now: NOW }).nextCard().includes('FIRE-1'), false);
assert.equal(overlay(supData([sup()]), { now: NOW }).nextCard(), 'Nothing needs you.');
assert.equal(overlay(lineData([mailRow({ id: '1' })], []), { now: NOW }).nextCard(), 'Nothing needs you.');

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
