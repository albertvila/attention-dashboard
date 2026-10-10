#!/usr/bin/env node
/* The shared consumer rules are the one piece of JS every surface runs, so they
   get a runnable check of their own:  node test_attention_view.mjs

   Covers what used to be copied per surface: change flags, the closed/new rule,
   ghosts (hidden in Needs), the one Parked fold both kinds of parking land in,
   an ack that only holds while the card has not moved, the Waiting tier's
   groups, the fold summaries, and the next-card line. */

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

// a closed member of a live card: closed wherever it rides, so never new — and
// the surface carries a predicate to strike it, without knowing the vocabulary
view = overlay(data({ tiers: [{ key: 'needs', title: 'n',
                                items: [row({ key: 'P1', firstSeenAt: '2026-10-02T11:30:00.000Z',
                                              states: [{ key: 'merged', label: 'merged', tier: 'ready', tone: 'good' }] })] },
                              { key: 'ready', title: 'r', items: [] }, { key: 'waiting', title: 'w', items: [] }] }),
               { now: NOW });
assert.equal(view.flag(view.tiers[0].rows[0]), null);
assert.equal(view.closed(view.tiers[0].rows[0]), true);
assert.equal(view.closed(row()), false);
assert.equal(view.counts().new, 0);

// --- ghosts -----------------------------------------------------------------
const ghost = row({ key: 'G1', section: 'needs', goneAt: '2026-10-02T11:30:00.000Z', change: { kind: 'gone' } });
const ghostWaiting = row({ key: 'G2', section: 'waiting', goneAt: '2026-10-02T11:30:00.000Z', change: { kind: 'gone' } });
const ghostSupport = row({ key: 'G3', section: 'waiting', goneAt: '2026-10-02T11:30:00.000Z', change: { kind: 'gone' },
                           states: [{ key: 'with-support', label: 'with support', tier: 'waiting', tone: 'quiet' }] });
view = overlay(data({ changes: { previousAt: '2026-10-02T11:00:00.000Z', summary: {}, items: {}, gone: [ghost, ghostWaiting, ghostSupport] } }),
               { now: NOW });
assert.deepEqual(view.tiers[0].rows.map(r => r.key), []);          // needs: hidden
assert.deepEqual(view.tiers[2].rows.map(r => r.key), ['FIRE-1', 'G2', 'G3']);  // waiting keeps its ghosts, support included
assert.equal(view.tiers[2].rows[1].change.kind, 'gone');
assert.equal(view.counts().gone, 3);                                // and the header counts all three

// a parent that went while its child stayed live: the live card already names
// the parent, so the struck copy does not render — but it still counts
const kidKilled = row({ key: 'K1' });
const parentGhost = row({ key: 'G4', section: 'waiting', goneAt: '2026-10-02T11:30:00.000Z',
                          change: { kind: 'gone' }, children: [kidKilled] });
const halfGhost = row({ key: 'G5', section: 'waiting', goneAt: '2026-10-02T11:30:00.000Z',
                        change: { kind: 'gone' }, children: [kidKilled, row({ key: 'K2' })] });
view = overlay(data({ tiers: [{ key: 'needs', title: 'n', items: [] }, { key: 'ready', title: 'r', items: [] },
                              { key: 'waiting', title: 'w', items: [kidKilled] }],
                  changes: { previousAt: '2026-10-02T11:00:00.000Z', summary: {}, items: {},
                             gone: [parentGhost, halfGhost] } }), { now: NOW });
assert.deepEqual(view.tiers[2].rows.map(r => r.key), ['K1', 'G5']);  // G5 still has a child only it holds
assert.equal(view.counts().gone, 2);

// --- a fold and parking -----------------------------------------------------
const parkedRow = row({ key: 'FIRE-9' });
const parkData = { tiers: [{ key: 'needs', title: 'n', items: [parkedRow] }, { key: 'ready', title: 'r', items: [] },
                           { key: 'waiting', title: 'w', items: [row()] }] };
view = overlay(data(parkData), { now: NOW });
assert.deepEqual(view.tiers[0].rows.map(r => r.key), ['FIRE-9']);  // unparked, it sits in its own tier
assert.deepEqual(view.folds.parked, []);
assert.equal('snoozed' in view.folds || 'acked' in view.folds, false);   // one fold, not two

view = overlay(data(parkData),
               { snoozes: { 'FIRE-9': '2026-10-03T12:00:00.000Z', 'FIRE-1': '2026-10-03T12:00:00.000Z' }, now: NOW });
assert.deepEqual(view.folds.parked.map(r => r.key).sort(), ['FIRE-1', 'FIRE-9']);
assert.equal(view.tiers[2].count, 0);
assert.equal(view.formatWhen('2026-10-03T12:00:00.000Z').startsWith('until '), true);
// an expired snooze is not a snooze
const expired = overlay(data(), { snoozes: { 'FIRE-1': '2026-10-01T00:00:00.000Z' }, now: NOW });
assert.equal(expired.folds.parked.length, 0);
assert.equal(expired.snoozeOf(expired.tiers[2].rows[0]), null);   // and it is not parked at all
assert.equal(expired.parked(expired.tiers[2].rows[0]), false);

// --- ack: holds until the card moves ---------------------------------------
const acks = { 'FIRE-1': '2026-10-02T11:30:00.000Z' };
view = overlay(data(), { acks, now: NOW });
assert.deepEqual(view.folds.parked.map(r => r.key), ['FIRE-1']);    // unchanged since the ack
assert.equal(view.tiers[2].count, 0);
view = overlay(data({ tiers: [{ key: 'needs', title: 'n', items: [] }, { key: 'ready', title: 'r', items: [] },
                               { key: 'waiting', title: 'w', items: [row({ lastChangedAt: '2026-10-02T11:45:00.000Z' })] }] }),
               { acks, now: NOW });
assert.deepEqual(view.folds.parked, []);                           // it moved: back on the board
assert.equal(view.tiers[2].count, 1);
assert.equal(view.flag(view.tiers[2].rows[0]).kind, 'moved');

// --- the jump list: what moved, in render order, folds included ----------------
assert.deepEqual(view.changedRows().map(r => r.key), ['FIRE-1']);   // moved, and only moved
view = overlay(data({ tiers: [{ key: 'needs', title: 'n', items: [row({ key: 'N1', firstSeenAt: '2026-10-02T11:30:00.000Z' })] },
                              { key: 'ready', title: 'r', items: [] },
                              { key: 'waiting', title: 'w', items: [row({ key: 'M1', lastChangedAt: '2026-10-02T11:30:00.000Z',
                                                                         lastChange: { kind: 'moved', label: 'waiting \u2192 CI failing', to_tier: 'needs' } })] }] }),
               { now: NOW });
assert.deepEqual(view.changedRows().map(r => r.key), ['M1']);       // a new card is not a jump target
const parkedMoved = overlay(data({ tiers: [{ key: 'needs', title: 'n', items: [] }, { key: 'ready', title: 'r', items: [] },
                                            { key: 'waiting', title: 'w', items: [row({ key: 'P1', lastChangedAt: '2026-10-02T11:30:00.000Z',
                                                                                       lastChange: { kind: 'moved', label: 'moved' } })] }] }),
                             { snoozes: { P1: '2026-10-03T12:00:00.000Z' }, now: NOW });
assert.deepEqual(parkedMoved.folds.parked.map(r => r.key), ['P1']);
assert.deepEqual(parkedMoved.changedRows().map(r => r.key), ['P1']);  // a parked move still jumps

// --- one Parked fold for both kinds ----------------------------------------
// A snooze parks until a time, an ack parks until the card moves: same fold,
// no tier, and the row still says which kind it is under.
const parkedData = (items) => data({ tiers: [{ key: 'needs', title: 'n', items: [] },
                                             { key: 'ready', title: 'r', items: [] },
                                             { key: 'waiting', title: 'w', items }] });
view = overlay(parkedData([row({ key: 'S1' }), row({ key: 'A1' })]),
               { snoozes: { S1: '2026-10-03T12:00:00.000Z' }, acks: { A1: '2026-10-02T11:30:00.000Z' }, now: NOW });
assert.deepEqual(view.tiers[2].rows, []);                          // parked renders in no tier
assert.equal(view.tiers[2].count, 0);
assert.deepEqual(view.folds.parked.map(r => r.key), ['S1', 'A1']); // snoozed first, then acked
const parkedBy = Object.fromEntries(view.folds.parked.map(r => [r.key, r]));
assert.equal(view.snoozeOf(parkedBy.S1), '2026-10-03T12:00:00.000Z');  // a snooze still says when it wakes
assert.equal(view.ackOf(parkedBy.S1), null);
assert.equal(view.ackOf(parkedBy.A1), '2026-10-02T11:30:00.000Z');     // an ack still says which kind
assert.equal(view.snoozeOf(parkedBy.A1), null);
assert.equal(view.parked(parkedBy.S1), true);
assert.equal(view.parked(parkedBy.A1), true);
assert.equal(view.notes.parked(), 'snoozed until a time, or until the card changes');
// both files name the same card: it shows once, and the snooze is what it reports
view = overlay(parkedData([row({ key: 'B1' })]),
               { snoozes: { B1: '2026-10-03T12:00:00.000Z' }, acks: { B1: '2026-10-02T11:30:00.000Z' }, now: NOW });
assert.deepEqual(view.folds.parked.map(r => r.key), ['B1']);
assert.equal(view.snoozeOf(view.folds.parked[0]), '2026-10-03T12:00:00.000Z');
// parking is not a hide: a new or moved parked card still counts in the header line
view = overlay(parkedData([row({ key: 'N1', firstSeenAt: '2026-10-02T11:30:00.000Z' }),
                           row({ key: 'M1', firstSeenAt: '2026-09-01T00:00:00.000Z',
                                 lastChangedAt: '2026-10-02T11:30:00.000Z',
                                 lastChange: { kind: 'moved', label: 'a → b' } })]),
               { snoozes: { N1: '2099-01-01T00:00:00.000Z' }, acks: { M1: '2026-10-02T11:30:00.000Z' }, now: NOW });
assert.deepEqual(view.folds.parked.map(r => r.key).sort(), ['M1', 'N1']);
assert.equal(view.counts().new, 1);
assert.equal(view.counts().changed, 1);

// --- With support: a Jira card in Support Investigating ---------------------
// It is a waiting card like any other: it rides Waiting on others, counted and
// marked, with nothing folding it out.
const SUPPORT = { key: 'with-support', label: 'with support', tier: 'waiting', tone: 'quiet' };
const sup = (over = {}) => row({ key: 'SUP-1', ref: 'SUP-1', age: '1d', states: [SUPPORT],
  times: { updated: '2026-10-01T12:00:00.000Z' }, ...over });
const supData = (items) => data({ tiers: [{ key: 'needs', title: 'n', items: [] },
                                           { key: 'ready', title: 'r', items: [] },
                                           { key: 'waiting', title: 'w', items }] });

view = overlay(supData([sup(), row()]), { now: NOW });
assert.deepEqual(view.tiers[2].rows.map(r => r.key), ['SUP-1', 'FIRE-1']);   // in the queue, where it was
assert.equal(view.tiers[2].count, 2);                                // and counted in it

// the header's state decides, children included — membership is by card, not member
view = overlay(supData([sup({ children: [row({ key: 'C1', states: [{ key: 'needs-reply', label: 'needs reply', tier: 'needs', tone: 'warn' }] })] })]), { now: NOW });
assert.deepEqual(view.tiers[2].rows.map(r => r.key), ['SUP-1']);
assert.deepEqual(view.tiers[2].rows[0].children.map(r => r.key), ['C1']);
// a more urgent header stays in its own tier, its support child along for the ride
view = overlay(data({ tiers: [{ key: 'needs', title: 'n', items: [row({ key: 'N1', children: [sup()] })] },
                               { key: 'ready', title: 'r', items: [] }, { key: 'waiting', title: 'w', items: [] }] }), { now: NOW });
assert.deepEqual(view.tiers[0].rows.map(r => r.key), ['N1']);

// parking still beats it, and coming back lands in the queue
view = overlay(supData([sup()]), { snoozes: { 'SUP-1': '2026-10-03T12:00:00.000Z' }, now: NOW });
assert.deepEqual(view.folds.parked.map(r => r.key), ['SUP-1']);
assert.equal(view.tiers[2].count, 0);
view = overlay(supData([sup()]), { snoozes: { 'SUP-1': '2026-10-01T00:00:00.000Z' }, now: NOW });
assert.deepEqual(view.tiers[2].rows.map(r => r.key), ['SUP-1']);     // expired: back in the queue
view = overlay(supData([sup()]), { acks: { 'SUP-1': '2026-10-02T11:00:00.000Z' }, now: NOW });
assert.deepEqual(view.folds.parked.map(r => r.key), ['SUP-1']);
view = overlay(supData([sup()]), { acks: { 'SUP-1': '2026-09-30T00:00:00.000Z' }, now: NOW });
assert.deepEqual(view.tiers[2].rows.map(r => r.key), ['SUP-1']);     // released: still in the queue

// a vanished one ghosts back into the queue like any other waiting card
const supGhost = sup({ goneAt: '2026-10-02T11:30:00.000Z', change: { kind: 'gone' } });
view = overlay({ ...supData([row()]), changes: { previousAt: '2026-10-02T11:00:00.000Z', summary: {}, items: {}, gone: [supGhost] } }, { now: NOW });
assert.deepEqual(view.tiers[2].rows.map(r => r.key), ['FIRE-1', 'SUP-1']);
assert.equal(view.counts().gone, 1);
// a new one still counts as new
assert.equal(overlay(supData([sup({ firstSeenAt: '2026-10-02T11:30:00.000Z' })]), { now: NOW }).counts().new, 1);
// and a changed one still counts as changed
view = overlay(supData([sup({ lastChangedAt: '2026-10-02T11:30:00.000Z',
  lastChange: { kind: 'moved', label: 'waiting → with support' } })]), { now: NOW });
assert.equal(view.counts().changed, 1);
// an ordinary in-progress row stays exactly where it was
assert.deepEqual(overlay(supData([row()]), { now: NOW }).tiers[2].rows.map(r => r.key), ['FIRE-1']);

// --- waiting groups: what the cards are -------------------------------------
// Six near-identical job warnings are six identical rows, so the rules hand the
// Waiting tier back grouped — by repository, else by Jira project. The tier's
// rows and their order do not change; the groups only say how they are read.
const wData = (items) => data({ tiers: [{ key: 'needs', title: 'n', items: [] },
                                       { key: 'ready', title: 'r', items: [] },
                                       { key: 'waiting', title: 'w', items }] });
const groupRows = (v) => v.tiers[2].groups.flatMap(g => g.rows);

// the key is the repository the card belongs to...
view = overlay(wData([row({ key: 'R1', ref: 'albertvila/attention-dashboard#14',
                            container: 'albertvila/attention-dashboard' })]), { now: NOW });
assert.equal(view.tiers[2].groups[0].key, 'attention-dashboard');
assert.equal(view.tiers[2].rows[0].groupKey, 'attention-dashboard');   // the row says where it landed
// ...else the Jira project its key names, which is all a Jira card has...
view = overlay(wData([row({ key: 'F1', ref: 'FIRE-83399' })]), { now: NOW });
assert.equal(view.tiers[2].groups[0].key, 'FIRE');
assert.equal(view.tiers[2].rows[0].groupKey, 'FIRE');
// ...else the card is only "other"
view = overlay(wData([row({ key: 'O1', ref: '', container: '' })]), { now: NOW });
assert.equal(view.tiers[2].groups[0].key, 'other');
assert.deepEqual(view.tiers[2].groups[0].rows.map(r => r.key), ['O1']);

// a group names the opening its rows share, counted; rows that share none say
// nothing, which is how a surface knows not to collapse them
const job = (over = {}) => row({ container: 'acme/web-frontend',
  title: '[DATABRICKS] Job Failed : one', ...over });
const twins = wData([job({ key: 'D1', ref: 'web-frontend#1', title: '[DATABRICKS] Job Failed : one' }),
                     job({ key: 'D2', ref: 'web-frontend#2', title: '[DATABRICKS] Job Failed : two' }),
                     job({ key: 'D3', ref: 'web-frontend#3', title: '[DATABRICKS] Job Failed : three' }),
                     row({ key: 'F1', ref: 'FIRE-83399', title: 'a lone Jira card' })]);
view = overlay(twins, { now: NOW });
assert.deepEqual(view.tiers[2].groups.map(g => [g.key, g.rows.length, g.note]),
  [['web-frontend', 3, '3 × [DATABRICKS] Job Failed'],    // the opening they share, counted
   ['FIRE', 1, '']]);                                       // a lone card is its own group of one
// one-offs share no opening, so they carry no note however they were bucketed:
// a Jira project's unrelated tickets, or one repo's distinct issues
view = overlay(wData([row({ key: 'F1', ref: 'FIRE-83399', title: 'AWS Health Event - LAMBDA' }),
                      row({ key: 'F2', ref: 'FIRE-96795', title: 'PLS - Lambda Error Rate High' }),
                      row({ key: 'F3', ref: 'FIRE-96736', title: '[DATABRICKS] Job Failed : bit-dataUnification' })]),
               { now: NOW });
assert.deepEqual(view.tiers[2].groups.map(g => [g.key, g.rows.length, g.note]),
  [['FIRE', 3, '']]);
assert.deepEqual(groupRows(view).map(r => r.key), ['F1', 'F2', 'F3']);   // still the partition: nothing hidden
// one repo, distinct issues: same rule, no opening shared, so no note
view = overlay(wData([job({ key: 'D1', ref: 'web-frontend#1', title: '[DATABRICKS] Job Failed : one' }),
                      job({ key: 'D2', ref: 'web-frontend#2', title: 'Update cluster policy' })]),
               { now: NOW });
assert.deepEqual(view.tiers[2].groups.map(g => [g.key, g.rows.length, g.note]),
  [['web-frontend', 2, '']]);
// nothing lost, nothing duplicated: the groups are the tier's rows, once each
assert.deepEqual(groupRows(view).map(r => r.key), view.tiers[2].rows.map(r => r.key));
assert.equal(new Set(groupRows(view).map(r => r.key)).size, groupRows(view).length);
// the snapshot itself is not written to: the key rides on the rules' own rows
assert.equal('groupKey' in twins.tiers[2].items[0], false);
// groups appear in the order their key first appears — not by size — while a
// tier that interleaves them keeps its own row order
view = overlay(wData([row({ key: 'F1', ref: 'FIRE-83399', title: 'a lone Jira card' }),
                      job({ key: 'D1', ref: 'web-frontend#1', title: '[DATABRICKS] Job Failed : one' }),
                      job({ key: 'D2', ref: 'web-frontend#2', title: '[DATABRICKS] Job Failed : two' })]),
               { now: NOW });
assert.deepEqual(view.tiers[2].rows.map(r => r.key), ['F1', 'D1', 'D2']);
assert.deepEqual(view.tiers[2].groups.map(g => g.key), ['FIRE', 'web-frontend']);
assert.deepEqual(view.tiers[2].groups.map(g => g.rows.map(r => r.key)), [['F1'], ['D1', 'D2']]);
assert.deepEqual(groupRows(view).map(r => r.key), ['F1', 'D1', 'D2']);
// a key's rows merge wherever they sit in the tier, not only where they are
// adjacent: two web-frontend rows with a FIRE row between them are one group
const scattered = wData([job({ key: 'D1', ref: 'web-frontend#1', title: '[DATABRICKS] Job Failed : one' }),
                         row({ key: 'F1', ref: 'FIRE-83399', title: 'a lone Jira card' }),
                         job({ key: 'D2', ref: 'web-frontend#2', title: '[DATABRICKS] Job Failed : two' })]);
view = overlay(scattered, { now: NOW });
assert.equal(view.tiers[2].groups.length, 2);                          // run-length reading would say three
assert.deepEqual(view.tiers[2].groups.map(g => g.key), ['web-frontend', 'FIRE']);
assert.deepEqual(view.tiers[2].groups.map(g => g.rows.map(r => r.key)), [['D1', 'D2'], ['F1']]);
assert.equal(view.tiers[2].groups[0].note, '2 × [DATABRICKS] Job Failed');   // counted across the merge
// interleaved, so the partition is asserted by key, not by position
assert.deepEqual(view.tiers[2].rows.map(r => r.key), ['D1', 'F1', 'D2']);    // the tier's own order
const flat = groupRows(view).map(r => r.key);
assert.deepEqual(flat.slice().sort(), view.tiers[2].rows.map(r => r.key).slice().sort());
assert.equal(new Set(flat).size, flat.length);                         // once each, none lost
// only Waiting groups: the other tiers are plain lists
assert.equal('groups' in view.tiers[0], false);
assert.equal('groups' in view.tiers[1], false);

// --- specs beside the board -------------------------------------------------
// A backlog, joined to the board by key. The snapshot is handed in untouched:
// every row is the very row the producer wrote.
const sessData = data({ tiers: [{ key: 'needs', title: 'n', items: [] }, { key: 'ready', title: 'r', items: [] },
  { key: 'waiting', title: 'w', items: [row({ key: 'acme/shared-lib#412', ref: 'shared-lib#412',
                                                container: 'acme/shared-lib' })] }] });
view = overlay(sessData, { now: NOW,
                           specs: [{ ref: 'acme/shared-lib#412' }, { ref: 'Launchmetrics/PLS-rubn#9' }] });
assert.deepEqual(view.specs.map(s => [s.ref, s.onBoard]),
  [['acme/shared-lib#412', true], ['Launchmetrics/PLS-rubn#9', false]]);
// with nothing read, every spec reads as a suggestion and no card grows anything
view = overlay(data(), { now: NOW });
assert.deepEqual(view.specs, []);
assert.equal(view.tiers[2].rows.some(r => 'sessions' in r), false);

// --- fold summaries: what a collapsed fold is hiding ------------------------
view = overlay(data(), { now: NOW });
assert.equal(view.notes.drafts(), '');                             // nothing hidden, nothing to say
assert.equal(view.notes.parked(), '');                             // nothing parked, nothing to say
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
// no updated time on the rows, no oldest clause to give
assert.equal(overlay(data({ drafts: [row({ key: 'D1', draft: true })] }), { now: NOW }).notes.drafts(), '');

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
assert.equal(overlay(data({ tiers: [{ key: 'needs', title: 'n', items: [] },
                                    { key: 'ready', title: 'r', items: [readyRow()] },
                                    { key: 'waiting', title: 'w', items: [sup()] }] }), { now: NOW }).nextCard(),
  'Nothing needs you. Next: o/r#7 · ready');   // a support card waits: it is never the next pick-up

// --- the next line: what a card is asking for --------------------------------
// A rule that asks you to do something is marked so a surface may show it
// wherever the card sits; a rule that only says you are waiting is marked so the
// surface can leave it out — that is what the tier already means.
const kid = (over = {}) => ({ ref: 'o/r#1', title: 'a PR', chip: 'MY PR', states: [], times: {}, ...over });
let nl = view.nextOf(row({ children: [kid({ states: [{ key: 'ready', label: 'ready', tier: 'ready', tone: 'good' }] })] }));
assert.equal(nl.next.say, 'merge o/r#1 — approved and green, nothing blocks it');
assert.equal(nl.next.act, true);
assert.equal(nl.rule.id, 'merge-a-ready-pr');

// a merged member leaves the ticket behind, in each shape that comes up
nl = view.nextOf(row({ states: [{ key: 'to-deploy', label: 'to deploy', tier: 'ready', tone: 'info' }],
                       children: [kid({ states: [{ key: 'merged', label: 'merged', tier: 'ready', tone: 'good' }] })] }));
assert.match(nl.next.say, /^ship it — the work is already merged/);
nl = view.nextOf(row({ facts: [{ label: 'Ready to Test' }],
                       children: [kid({ states: [{ key: 'merged', label: 'merged', tier: 'ready', tone: 'good' }] })] }));
assert.match(nl.next.say, /^verify in staging/);

// a Waiting card that owes something still says so — the action is not the tier's
nl = view.nextOf(row({ facts: [{ label: 'checks green' }, { label: 'merge blocked' }] }));
assert.match(nl.next.say, /^you need an approval/);
assert.equal(nl.next.act, true);

// and a card that only waits says so without pretending it is work
const SUP2 = { key: 'with-support', label: 'with support', tier: 'waiting', tone: 'quiet' };
nl = view.nextOf(row({ states: [SUP2] }));
assert.equal(nl.next.say, 'with support — nothing for you until they answer');
assert.equal(nl.next.act, false);
// the open link is the wait; the finished one is history
nl = view.nextOf(row({ states: [SUP2],
  linked: [{ key: 'FISRE-27667', status: 'In Progress', status_category: 'In Progress' },
           { key: 'RBT-433', status: 'Discard', status_category: 'Done' }] }));
assert.equal(nl.next.say, 'wait for FISRE-27667 (In Progress)');
assert.equal(nl.next.act, false);

// nothing fires: the card names what it would take to have a line at all
nl = view.nextOf(row({}));
assert.equal(nl.next, null);
assert.deepEqual(nl.wanting.map(w => w.needs), ['comment', 'transition']);
// and a card that only waits still reports the wait, not a silence
assert.equal(view.nextOf(row({ states: [SUP2] })).next.act, false);

// read one more thing and a rule answers, without any other rule changing: the
// evidence arrives with the overlay, the way a surface hands it over
const LANDED = { comment: [{ at: '2026-10-02T11:30:00.000Z', who: 'Ada', text: 'can you re-run this?' }] };
const readView = (reads, seenAt) => overlay(data(), { reads, seenAt });
assert.match(readView({ comment: { 'FIRE-1': LANDED.comment } }, '2026-10-02T11:00:00.000Z').nextOf(row()).next.say,
  /^answer Ada — they wrote after your last look/);
assert.equal(readView({ comment: { 'FIRE-1': LANDED.comment } }, '2026-10-02T12:00:00.000Z').nextOf(row()).next, null);
// read and empty is not the same as not read: one leaves the card waiting, the other does not
assert.equal(readView({ comment: { 'FIRE-1': [] } }, NOW).nextOf(row()).wanting.some(w => w.needs === 'comment'), false);
assert.equal(overlay(data(), {}).nextOf(row()).wanting.some(w => w.needs === 'comment'), true);
// and it outranks a wait: the card still links to another team's ticket, and the
// message that landed since your last look is the thing you can act on
assert.match(readView({ comment: { 'FIRE-1': LANDED.comment } }, '2026-10-02T11:00:00.000Z')
  .nextOf(row({ states: [SUP2], linked: [{ key: 'FISRE-27667', status: 'In Progress', status_category: 'In Progress' }] })).next.say,
  /^answer Ada/);
// a kind nobody has read yet is reachable the moment something provides it
assert.match(readView({ transition: { 'FIRE-1': ['Deploy to Prod', 'Close'] } }, NOW)
  .nextOf(row({ states: [{ key: 'to-deploy', label: 'to deploy', tier: 'ready', tone: 'info' }] })).next.say,
  /— Jira offers Deploy to Prod, Close$/);

// reading a line never touches the row: this is all read-side
const untouched = row({ children: [kid({ states: [{ key: 'ready', label: 'ready', tier: 'ready', tone: 'good' }] })] });
const before = JSON.stringify(untouched);
view.nextOf(untouched);
assert.equal(JSON.stringify(untouched), before);

// --- the silence in the sentence ---------------------------------------------
// Silence is a number, not a feeling: the cards you can act on that ask for
// nothing from what has been read. Same test as the surface's own "nothing from
// the board" line, so the sentence and the cards cannot disagree.
const mergedKid = { ref: 'o/r#9', title: 'a PR', chip: 'MY PR', times: {},
                    states: [{ key: 'merged', label: 'merged', tier: 'ready', tone: 'good' }] };
const silData = data({ tiers: [
  { key: 'needs', title: 'n', items: [row({ key: 'N1' })] },
  { key: 'ready', title: 'r', items: [row({ key: 'R1', children: [mergedKid] }),
    row({ key: 'G9', section: 'ready', goneAt: '2026-10-02T11:30:00.000Z', change: { kind: 'gone' } })] },
  { key: 'waiting', title: 'w', items: [row({ key: 'W1' })] }] });
const sv = overlay(silData, { now: NOW });
assert.equal(sv.silentCount(), 1);                                  // N1 only: W1 is not counted, G9 is a ghost
assert.match(sv.summaryText(), /\u00b7 1 say nothing$/);
assert.equal(overlay(data({ tiers: [{ key: 'needs', title: 'n', items: [] }, { key: 'ready', title: 'r', items: [] },
                                    { key: 'waiting', title: 'w', items: [row()] }] }), { now: NOW })
  .summaryText().includes('say nothing'), false);                   // nothing silent, nothing said
assert.match(overlay({ ...silData, changes: {} }, { now: NOW }).summaryText(),
  /^first look \u00b7 nothing to compare \u00b7 1 say nothing$/);

// --- one fact once, one linked item once ------------------------------------
// A Jira card carries its status twice — the state badge (`to deploy`) and the
// raw status name as a fact (`TO_DEPLOY`), both off the same field — and lists a
// linked item twice when one read saw it live and another saw it finished. What
// a reader is shown of each is one.
const shaped = overlay(data(), { now: NOW });
assert.deepEqual(shaped.factsOf(row({ key: 'D1',
  states: [{ key: 'to-deploy', label: 'to deploy', tier: 'ready', tone: 'info' }],
  facts: [{ label: 'TO_DEPLOY', tone: 'warn' }] })), [],
  'a fact that only restates the state is the same word twice');
assert.deepEqual(shaped.factsOf(row({ facts: [{ label: 'Ready to Test', tone: 'warn' }] }))
  .map(f => f.label), ['Ready to Test'], 'a status the state does not say is kept');
assert.deepEqual(shaped.factsOf(row({ states: [], facts: [{ label: 'checks green', tone: 'ok' }] }))
  .map(f => f.label), ['checks green'], 'no state, nothing to restate');

assert.deepEqual(shaped.kidsOf(row({ key: 'K1',
  children: [kid({ key: 'a' }), kid({ key: 'a' }), kid({ key: 'b' })] })).map(k => k.key), ['a', 'b'],
  'a linked item that arrived twice is drawn once, in the order it arrived');
assert.deepEqual(shaped.kidsOf(row({ key: 'a', children: [kid({ key: 'a' }), kid({ key: 'b' })] })).map(k => k.key),
  ['b'], 'a card never lists itself');
assert.equal(shaped.kidsOf(row({ children: [kid({ key: '' })] })).length, 1,
  'a child with no key has nothing to tell apart');
assert.deepEqual(shaped.kidsOf(row({})), [], 'a card with no links draws none');

// --- stale-code witness -----------------------------------------------------
assert.deepEqual(globalThis.AttentionView.stale({ producer: { code: 'aaa' } }, { producer: { code: 'aaa' } }),
  { stale: false, running: 'aaa', snapshot: 'aaa' });
assert.equal(globalThis.AttentionView.stale({ producer: { code: 'old' } }, { producer: { code: 'new' } }).stale, true);
assert.equal(globalThis.AttentionView.stale({}, { producer: { code: 'new' } }).stale, true);   // file predates the stamp
assert.equal(globalThis.AttentionView.stale({}, null).stale, false);                          // static hosting: unknown

// --- choices and labels -----------------------------------------------------
// The snooze choices are moments on the reader's own clock — the one clock that
// knows where 09:00 is — so what they say is what they mean.
assert.deepEqual(view.choices.map(c => c.label),
  ['4 hours', 'Tomorrow 09:00', 'next Monday 09:00', 'until it changes']);
const clockOf = label => { const d = new Date(view.choices.find(c => c.label === label).value); return [d.getDay(), d.getHours(), d.getMinutes()]; };
assert.deepEqual(clockOf('Tomorrow 09:00').slice(1), [9, 0]);            // at nine, tomorrow
assert.deepEqual(clockOf('next Monday 09:00'), [1, 9, 0]);               // the next Monday, at nine
assert.ok(new Date(view.choices.find(c => c.label === '4 hours').value) - new Date(NOW) === 4 * 60 * 60 * 1000);
// from a Monday, "next Monday" is next week's — not this morning
const mondayChoice = iso => new Date(overlay(data(), { now: iso }).choices.find(c => c.label === 'next Monday 09:00').value);
assert.equal(mondayChoice('2026-10-05T12:00:00.000Z').getDate(), 12);
assert.equal(mondayChoice('2026-10-04T12:00:00.000Z').getDate(), 5);    // from a Sunday: tomorrow
assert.equal(mondayChoice(NOW).getDate(), 5);                            // from a Friday: the coming Monday
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
