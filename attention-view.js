/* attention-view.js — what a *reader* sees, shared verbatim by every surface that
   renders the snapshot.

   The producer owns what is true (states, tiers, links, change stamps). This
   owns the layer on top of it: change flags since the reader's last look, ghosts
   of what left, what the reader parked (snooze / acknowledge), how the Waiting
   tier groups by what its cards are, which of them have a live agent session
   open on them, and which specs the board already carries. It is dependency-free
   and side-effect-free; load it as a classic script and use window.AttentionView.

   Everything a surface needs to decide *where a card renders* and *what mark it
   carries* lives here, so the surfaces cannot drift apart again. */

(function (global) {
  'use strict';

  /** Which rows a change flag applies to, and what it says. */
  function flagOf(row, since) {
    if (row.change && row.change.kind === 'gone') return { kind: 'gone' };
    if (!since) return null;
    // Recently closed is exempt from "new": a card that closes while you are
    // away is first seen in that window, but it closed — it is not a new card.
    if (row.section !== 'closed' && row.firstSeenAt && row.firstSeenAt > since) return { kind: 'new' };
    if (row.lastChangedAt && row.lastChangedAt > since) return row.lastChange || { kind: 'moved', label: 'changed' };
    return null;
  }

  /** What a Waiting card *is*, so six near-identical ones cost one line: the
      repository it belongs to, else the Jira project its key names (all a Jira
      card has to group by), else only "other". */
  function groupOf(row) {
    if (row.container) return row.container.split('/').pop();
    const m = /^([A-Z]+)-/.exec(row.ref || '');
    return m ? m[1] : 'other';
  }

  /** What a group is hiding: the title opening its rows share, counted — or
      nothing, when they share none. The note is what earns the collapse: a
      bucket whose rows are one-offs (a Jira project's unrelated tickets, one
      repo's distinct issues) is plain rows, not a line saying "one off each". */
  function groupNoteOf(rows) {
    const openings = new Map();
    for (const row of rows) {
      const opening = ((row.title || '').split(/[:(]/)[0] || '').trim().slice(0, 28);
      openings.set(opening, (openings.get(opening) || 0) + 1);
    }
    const top = [...openings.entries()].sort((a, b) => b[1] - a[1])[0];
    return top[1] > 1 ? top[1] + ' \u00d7 ' + top[0] : '';
  }

  /** The Waiting tier as groups: every row says which group it landed in, the
      rows keep the tier's own order, and groups keep the order their key first
      appears in. A lone card is a group of one. */
  function waitingGroups(rows) {
    const byKey = new Map();
    const keyed = rows.map(row => {
      const key = groupOf(row);
      if (!byKey.has(key)) byKey.set(key, []);
      const copy = { ...row, groupKey: key };
      byKey.get(key).push(copy);
      return copy;
    });
    return {
      rows: keyed,
      groups: [...byKey.entries()].map(([key, groupRows]) =>
        ({ key, note: groupNoteOf(groupRows), rows: groupRows })),
    };
  }

  function overlay(data, options) {
    const opts = options || {};
    const seenAt = opts.seenAt || null;
    const snoozes = opts.snoozes || {};
    const acks = opts.acks || {};
    const now = opts.now || new Date().toISOString();
    const since = seenAt || (data.changes || {}).previousAt || null;
    const sessions = opts.sessions || {};
    const rows = [].concat(...(data.tiers || []).map(t => t.items), data.drafts || [], data.closed || [])
      .map(withSessions);
    const gone = (data.changes || {}).gone || [];

    /** The repo a card is about, and the only thing a session can join on: a
        Jira card carries none, so nothing joins to it. Copy, never mutate — the
        snapshot is handed to every surface as it arrived. */
    function withSessions(row) {
      const live = sessions[row.container || ''];
      return live && live.length ? Object.assign({}, row, {sessions: live}) : row;
    }

    const isMail = row => row.chip === 'MAIL';
    // A Jira card in Support Investigating is waiting in the snapshot, and
    // folds out of the rendered queue at read time — like mail, not a tier.
    const isSupport = row => (((row.states || [])[0] || {}).key) === 'with-support';
    const snoozeOf = row => (snoozes[row.key] && snoozes[row.key] > now) ? snoozes[row.key] : null;
    // An ack holds only while the card has not moved since you acknowledged it.
    const ackOf = row => {
      const at = acks[row.key];
      return (at && (!row.lastChangedAt || row.lastChangedAt <= at)) ? at : null;
    };
    const parked = row => !!(snoozeOf(row) || ackOf(row));
    /** Renders in a tier: work that is neither mail (its own fold), support
        (its own fold) nor parked. */
    const work = row => !isMail(row) && !isSupport(row) && !parked(row);

    // Every key a surface draws live, children included: a parent that vanished
    // while its children survived is the same work twice.
    const liveKeys = new Set(rows.concat(...rows.map(r => r.children || [])).map(r => r.key));
    const allKidsLive = r => (r.children || []).length > 0
      && (r.children || []).every(c => liveKeys.has(c.key));

    function ghosts(section) {
      // The Needs queue is for things you can act on; a struck-through card
      // there reads as "do I still owe this?". And a ghost whose every linked
      // child is still live duplicates them: the live card already names what
      // the parent was, so striking it again is noise.
      if (section === 'needs') return [];
      return gone.filter(r => r.section === section && !isMail(r) && !isSupport(r)
        && !parked(r) && since && r.goneAt > since && !allKidsLive(r));
    }

    const tiers = (data.tiers || []).map(t => {
      const list = t.items.filter(work).concat(ghosts(t.key)).map(withSessions);
      // Waiting is where the noise piles up, so the rules hand it back grouped
      // rather than let a surface invent the groups. The rows are the same rows
      // in the same order; the other tiers carry no groups at all.
      if (t.key === 'waiting') {
        const g = waitingGroups(list);
        return { key: t.key, title: t.title, rows: g.rows, groups: g.groups, count: list.length };
      }
      return { key: t.key, title: t.title, rows: list, count: list.length };
    });

    // Parking is one concept with two kinds — a snooze wakes at a time, an ack
    // holds until the card moves — so both land in one fold. Snoozed rows come
    // first, then the acks: the order the two separate folds used to render in.
    const parkedRows = rows.filter(r => snoozeOf(r))
      .concat(rows.filter(r => ackOf(r) && !snoozeOf(r)));
    const folds = {
      parked: parkedRows,
      mail: rows.filter(r => isMail(r) && !parked(r)),
      support: rows.filter(r => isSupport(r) && !parked(r)),
      drafts: (data.drafts || []).filter(work).concat(ghosts('drafts')),
      closed: (data.closed || []).filter(work).concat(ghosts('closed')),
    };

    /** The specs a surface renders: the same issues, each marked when the board
        already carries it — a spec you are looking at is not a suggestion. */
    const specs = (opts.specs || []).map(s => Object.assign({}, s, {onBoard: liveKeys.has(s.ref)}));

    /** Fold summaries are built from the ages the producer already put on the
        rows — never a second calculation. No updated time, no oldest clause. */
    function oldestClause(rows) {
      const timed = rows.filter(r => (r.times || {}).updated);
      if (!timed.length) return '';
      const oldest = timed.reduce((a, b) => (a.times.updated <= b.times.updated ? a : b));
      return oldest.age ? 'oldest ' + oldest.age : '';
    }

    /** Why a collapsed fold is worth opening, one line per fold. */
    const notes = {
      mail: () => [oldestClause(folds.mail),
        folds.mail.filter(r => (r.facts || []).some(f => f.label === 'unread')).length + ' unread'
      ].filter(Boolean).join(' · '),
      drafts: () => [folds.drafts.some(r => (r.states || []).some(s => s.key === 'conflicts')) ? 'conflicts' : '',
        oldestClause(folds.drafts)
      ].filter(Boolean).join(' · '),
      support: () => oldestClause(folds.support),
      // What the one fold hides: two kinds of parking, and how each wakes.
      parked: () => folds.parked.length ? 'snoozed until a time, or until the card changes' : '',
    };

    /** When nothing needs you, name what you could pick up instead: only Ready,
        and only a row — never a waiting card, never a fold. */
    function nextCard() {
      const inTier = key => (tiers.find(t => t.key === key) || { rows: [] }).rows;
      if (inTier('needs').length) return null;
      const next = inTier('ready')[0];
      if (!next) return 'Nothing needs you.';
      const state = (next.states || [])[0];
      return 'Nothing needs you. Next: ' + (next.ref || next.title) + (state ? ' · ' + state.label : '');
    }

    /** Counts describe what happened, not what is on screen: a parked card
        still counts as new/changed/dropped in the header line. */
    function counts() {
      let fresh = 0, moved = 0;
      for (const row of rows) {
        const f = flagOf(row, since);
        if (!f) continue;
        if (f.kind === 'new') fresh += 1;
        else if (f.kind === 'moved') moved += 1;
      }
      return { new: fresh, changed: moved, gone: gone.filter(r => since && r.goneAt > since).length };
    }

    function summaryText() {
      if (!since) return 'first look · nothing to compare';
      const c = counts();
      const when = seenAt
        ? `since your last look (${new Date(seenAt).toLocaleTimeString()})`
        : 'since the previous snapshot';
      return `${c.new} new · ${c.changed} changed · ${c.gone} dropped ${when}`;
    }

    const formatWhen = iso => 'until ' + new Date(iso).toLocaleString([], { weekday: 'short', hour: '2-digit', minute: '2-digit' });
    const changeLabel = f => f.kind === 'new' ? 'new' : f.kind === 'gone' ? 'dropped' : (f.label || 'changed');
    // a move out of the needs tier reads green; into it, amber.
    const changeTone = f => !f ? ''
      : (f.kind === 'moved' ? (f.to_tier === 'needs' ? 'moved' : 'moved out') : f.kind);
    const changeClass = f => !f ? '' : ' chg-' + changeTone(f);

    return {
      snoozeOf, ackOf, parked, specs,
      flag: row => flagOf(row, since),
      tiers, folds, notes, nextCard, counts, summaryText, formatWhen, changeLabel, changeTone, changeClass,      /** The five choices one control offers: hours, or "ack" (until it changes). */
      choices: [
        { value: '4', label: '4 hours' },
        { value: '24', label: '1 day' },
        { value: '72', label: '3 days' },
        { value: '168', label: '1 week' },
        { value: 'ack', label: 'until it changes' },
      ],
    };
  }

  /** Is the snapshot this process serves written by the code it is running?
      A long-lived server that keeps rewriting the file with older rules is the
      failure this catches. Unknown status (static hosting) is not a warning. */
  function stale(data, status) {
    if (!status) return { stale: false, running: '', snapshot: '' };
    const running = (status.producer && status.producer.code) || '';
    const snapshot = (data && data.producer && data.producer.code) || '';
    return { stale: !running || snapshot !== running, running, snapshot };
  }

  /** The whole warning line, so every surface says the same thing. */
  function staleText(data, status) {
    const s = stale(data, status);
    if (!s.stale) return '';
    return '\u26a0 snapshot and running code differ (snapshot ' + (s.snapshot || 'no stamp') +
      ' vs running ' + (s.running || 'unknown') + ') \u2014 refresh to rewrite it, or restart the ' +
      'collector if the collector is the old one';
  }

  global.AttentionView = { overlay, stale, staleText };
})(typeof window !== 'undefined' ? window : globalThis);
