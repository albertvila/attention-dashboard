#!/usr/bin/env node
/* The shared consumer rules are the one piece of JS every surface runs, so they
   get a runnable check of their own:  node test_attention_view.mjs

   Covers what used to be copied per surface: change flags, the closed/new rule,
   ghosts (hidden in Needs, never for mail or support), the With support and Mail
   folds, the one Parked fold both kinds of parking land in, an ack that only
   holds while the card has not moved, the Waiting tier's groups, the fold
   summaries, and the next-card line. */

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
const ghostMail = row({ chip: 'MAIL', key: 'G3', section: 'waiting', goneAt: '2026-10-02T11:30:00.000Z', change: { kind: 'gone' } });
view = overlay(data({ changes: { previousAt: '2026-10-02T11:00:00.000Z', summary: {}, items: {}, gone: [ghost, ghostWaiting, ghostMail] } }),
               { now: NOW });
assert.deepEqual(view.tiers[0].rows.map(r => r.key), []);          // needs: hidden
assert.deepEqual(view.tiers[2].rows.map(r => r.key), ['FIRE-1', 'G2']);  // waiting keeps its ghost
assert.equal(view.tiers[2].rows[1].change.kind, 'gone');
assert.equal(view.counts().gone, 3);                                // but the header still counts all three

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

// --- mail and parking -------------------------------------------------------
const mail = row({ chip: 'MAIL', key: 'mail/1', section: 'needs' });
view = overlay(data({ tiers: [{ key: 'needs', title: 'n', items: [mail] }, { key: 'ready', title: 'r', items: [] },
                              { key: 'waiting', title: 'w', items: [row()] }] }), { now: NOW });
assert.deepEqual(view.tiers[0].rows, []);                        // mail never sits in a tier
assert.deepEqual(view.folds.mail.map(r => r.key), ['mail/1']);
assert.deepEqual(view.folds.parked, []);
assert.equal('snoozed' in view.folds || 'acked' in view.folds, false);   // one fold, not two

view = overlay(data({ tiers: [{ key: 'needs', title: 'n', items: [mail] }, { key: 'ready', title: 'r', items: [] },
                              { key: 'waiting', title: 'w', items: [row()] }] }),
               { snoozes: { 'mail/1': '2026-10-03T12:00:00.000Z', 'FIRE-1': '2026-10-03T12:00:00.000Z' }, now: NOW });
assert.deepEqual(view.folds.parked.map(r => r.key).sort(), ['FIRE-1', 'mail/1']);
assert.deepEqual(view.folds.mail, []);                            // parking beats the mail fold
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
assert.deepEqual(view.folds.parked.map(r => r.key), ['SUP-1']);
assert.deepEqual(view.folds.support, []);
assert.equal(view.tiers[2].count, 0);
view = overlay(supData([sup()]), { snoozes: { 'SUP-1': '2026-10-01T00:00:00.000Z' }, now: NOW });
assert.deepEqual(view.folds.support.map(r => r.key), ['SUP-1']);     // expired: back in the fold
assert.deepEqual(view.tiers[2].rows, []);
view = overlay(supData([sup()]), { acks: { 'SUP-1': '2026-10-02T11:00:00.000Z' }, now: NOW });
assert.deepEqual(view.folds.parked.map(r => r.key), ['SUP-1']);
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
const job = (over = {}) => row({ container: 'Launchmetrics/BIT-databricks',
  title: '[DATABRICKS] Job Failed : one', ...over });
const twins = wData([job({ key: 'D1', ref: 'BIT-databricks#1', title: '[DATABRICKS] Job Failed : one' }),
                     job({ key: 'D2', ref: 'BIT-databricks#2', title: '[DATABRICKS] Job Failed : two' }),
                     job({ key: 'D3', ref: 'BIT-databricks#3', title: '[DATABRICKS] Job Failed : three' }),
                     row({ key: 'F1', ref: 'FIRE-83399', title: 'a lone Jira card' })]);
view = overlay(twins, { now: NOW });
assert.deepEqual(view.tiers[2].groups.map(g => [g.key, g.rows.length, g.note]),
  [['BIT-databricks', 3, '3 × [DATABRICKS] Job Failed'],    // the opening they share, counted
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
view = overlay(wData([job({ key: 'D1', ref: 'BIT-databricks#1', title: '[DATABRICKS] Job Failed : one' }),
                      job({ key: 'D2', ref: 'BIT-databricks#2', title: 'Update cluster policy' })]),
               { now: NOW });
assert.deepEqual(view.tiers[2].groups.map(g => [g.key, g.rows.length, g.note]),
  [['BIT-databricks', 2, '']]);
// nothing lost, nothing duplicated: the groups are the tier's rows, once each
assert.deepEqual(groupRows(view).map(r => r.key), view.tiers[2].rows.map(r => r.key));
assert.equal(new Set(groupRows(view).map(r => r.key)).size, groupRows(view).length);
// the snapshot itself is not written to: the key rides on the rules' own rows
assert.equal('groupKey' in twins.tiers[2].items[0], false);
// groups appear in the order their key first appears — not by size — while a
// tier that interleaves them keeps its own row order
view = overlay(wData([row({ key: 'F1', ref: 'FIRE-83399', title: 'a lone Jira card' }),
                      job({ key: 'D1', ref: 'BIT-databricks#1', title: '[DATABRICKS] Job Failed : one' }),
                      job({ key: 'D2', ref: 'BIT-databricks#2', title: '[DATABRICKS] Job Failed : two' })]),
               { now: NOW });
assert.deepEqual(view.tiers[2].rows.map(r => r.key), ['F1', 'D1', 'D2']);
assert.deepEqual(view.tiers[2].groups.map(g => g.key), ['FIRE', 'BIT-databricks']);
assert.deepEqual(view.tiers[2].groups.map(g => g.rows.map(r => r.key)), [['F1'], ['D1', 'D2']]);
assert.deepEqual(groupRows(view).map(r => r.key), ['F1', 'D1', 'D2']);
// a key's rows merge wherever they sit in the tier, not only where they are
// adjacent: two BIT-databricks rows with a FIRE row between them are one group
const scattered = wData([job({ key: 'D1', ref: 'BIT-databricks#1', title: '[DATABRICKS] Job Failed : one' }),
                         row({ key: 'F1', ref: 'FIRE-83399', title: 'a lone Jira card' }),
                         job({ key: 'D2', ref: 'BIT-databricks#2', title: '[DATABRICKS] Job Failed : two' })]);
view = overlay(scattered, { now: NOW });
assert.equal(view.tiers[2].groups.length, 2);                          // run-length reading would say three
assert.deepEqual(view.tiers[2].groups.map(g => g.key), ['BIT-databricks', 'FIRE']);
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
  { key: 'waiting', title: 'w', items: [row({ key: 'Launchmetrics/LM-shared#412', ref: 'LM-shared#412',
                                                container: 'Launchmetrics/LM-shared' })] }] });
view = overlay(sessData, { now: NOW,
                           specs: [{ ref: 'Launchmetrics/LM-shared#412' }, { ref: 'Launchmetrics/PLS-rubn#9' }] });
assert.deepEqual(view.specs.map(s => [s.ref, s.onBoard]),
  [['Launchmetrics/LM-shared#412', true], ['Launchmetrics/PLS-rubn#9', false]]);
// with nothing read, every spec reads as a suggestion and no card grows anything
view = overlay(data(), { now: NOW });
assert.deepEqual(view.specs, []);
assert.equal(view.tiers[2].rows.some(r => 'sessions' in r), false);

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
assert.equal(view.notes.parked(), '');                             // nothing parked, nothing to say
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
