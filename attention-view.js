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

  /** The states that read as finished work — the producer's closed vocabulary
      (merged, closed, done). A row that closed is never "new" and renders
      struck, wherever it rides: first sighting of closed work is a closure, not
      a card you just picked up. */
  const CLOSED_STATES = ['merged', 'closed', 'done'];
  const isClosed = row => CLOSED_STATES.indexOf(((row.states || [])[0] || {}).key) !== -1;

  /** Which rows a change flag applies to, and what it says. */
  function flagOf(row, since) {
    if (row.change && row.change.kind === 'gone') return { kind: 'gone' };
    if (!since) return null;
    if (!isClosed(row) && row.firstSeenAt && row.firstSeenAt > since) return { kind: 'new' };
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

  /* ------------------------------------------------------------------------
     The next line: what this card is asking for, and the evidence it stands on.

     Everything a line can stand on is a *kind* of evidence. The free kinds come
     off the row we already have; the rest is read per card and passed in as
     `reads`, so a new source of truth (Jira comments, PR review threads, the
     transitions the workflow allows) is a new kind plus rules that name it —
     no rule rewritten, no card moved. Every sentence names its evidence, the
     way everything else a surface shows does. */
  const NEXT_KINDS = {
    state: { label: 'the card state', free: true },
    fact: { label: 'the source facts', free: true },
    child: { label: 'the linked items on the card', free: true },
    link: { label: 'the Jira links', free: true },
    blocker: { label: 'what Jira says holds it up', free: true },
    comment: { label: 'the Jira comments', free: false },
    'review-comment': { label: 'the PR comments', free: false },
    transition: { label: 'the transitions Jira allows', free: false },
  };
  const NEXT_CLOSED = ['merged', 'closed', 'done'];
  const isOpen = e => NEXT_CLOSED.indexOf(String(e.state || e.category || '').toLowerCase()) === -1;

  /** Row + whatever else was read -> the evidence, one item per thing we know. */
  function evidenceOf(row, reads) {
    const x = reads || {}, ev = [];
    for (const s of row.states || []) ev.push({ kind: 'state', key: s.key, label: s.label, tier: s.tier, at: (row.times || {}).updated || '' });
    for (const f of row.facts || []) ev.push({ kind: 'fact', label: f.label, tone: f.tone, at: '' });
    for (const c of row.children || []) {
      const st = (c.states || [])[0] || {};
      ev.push({ kind: 'child', ref: c.ref, title: c.title, state: st.key || '', label: st.label || '', at: (c.times || {}).updated || '' });
    }
    for (const l of row.linked || []) ev.push({ kind: 'link', ref: l.key, status: l.status, category: l.status_category, type: l.type, at: '' });
    for (const b of row.blocked_by || []) ev.push({ kind: 'blocker', ref: b.key, status: b.status, category: b.status_category, at: '' });
    for (const c of x.comment || []) ev.push({ kind: 'comment', at: c.at, who: c.who, text: c.text });
    for (const c of x['review-comment'] || []) ev.push({ kind: 'review-comment', at: c.at, who: c.who, text: c.text, ref: c.ref });
    for (const t of x.transition || []) ev.push({ kind: 'transition', name: t, at: '' });
    return ev;
  }

  /** The card as the rules read it. */
  function nextShape(row, ev) {
    const state = (row.states || [])[0] || {}, facts = (row.facts || []).map(f => f.label);
    const kids = ev.filter(e => e.kind === 'child'), links = ev.filter(e => e.kind === 'link');
    return {
      ref: row.ref, state, facts, kids, links,
      blockers: ev.filter(e => e.kind === 'blocker'),
      openKids: kids.filter(isOpen), mergedKids: kids.filter(k => !isOpen(k)),
      openLinks: links.filter(l => isOpen(l)),
      openBlockers: ev.filter(e => e.kind === 'blocker' && e.category !== 'Done'),
      humans: ev.filter(e => e.kind === 'comment' || e.kind === 'review-comment')
        .sort((a, b) => (a.at < b.at ? -1 : 1)),
      transitions: ev.filter(e => e.kind === 'transition').map(e => e.name),
    };
  }

  /* In order: first one that fires wins. Each says what to do and which evidence
     the sentence stands on. */
  const NEXT_RULES = [
    { id: 'open-link', act: false, needs: 'link',
      when: c => {
        const l = c.openLinks[0];
        if (!l || ['with-support', 'in-progress'].indexOf(c.state.key) === -1) return null;
        return { say: 'wait for ' + l.ref + ' (' + l.status + ')', why: l.ref + ' is ' + l.status + ', linked to this card' };
      } },
    { id: 'held-up', act: false, needs: 'blocker',
      when: c => {
        const b = c.openBlockers[0];
        if (!b) return null;
        return { say: 'held by ' + b.ref + ' until it is done', why: 'Jira says this is blocked by ' + b.ref + ', still ' + b.status };
      } },
    { id: 'answer-a-human', act: true, needs: 'comment',
      when: (c, ctx) => {
        const h = c.humans.slice(-1)[0];
        if (!h || !(ctx.lastLook && h.at > ctx.lastLook)) return null;
        return { say: 'answer ' + h.who + ' — they wrote after your last look', why: h.who + ' commented, nothing has moved since' };
      } },
    { id: 'merge-a-ready-pr', act: true, needs: 'child',
      when: c => {
        const r = c.kids.filter(k => k.state === 'ready')[0];
        if (!r) return null;
        return { say: 'merge ' + r.ref + ' — approved and green, nothing blocks it', why: r.ref + ' is ready: approved, with its checks green' };
      } },
    { id: 'merged-member', act: true, needs: 'child',
      when: c => {
        const m = c.mergedKids.slice(-1)[0];
        if (!m || c.state.key === 'merged') return null;
        if (c.state.key === 'to-deploy') return { say: 'ship it — the work is already merged, the ticket just waits for the deploy'
          + (c.transitions.length ? ' (' + c.transitions.slice(0, 2).join(', ') + ')' : ''),
          why: m.ref + ' is merged and the ticket reads To Deploy' };
        if (c.facts.indexOf('Ready to Test') !== -1) return { say: 'verify in staging, then close it — ' + m.ref + ' is merged', why: m.ref + ' merged and the ticket sits at Ready to Test' };
        return { say: 'close the ticket, or move it on — ' + m.ref + ' is merged', why: m.ref + ' (' + m.label + ') merged and the ticket still reads ' + c.state.label };
      } },
    { id: 'to-deploy', act: true, needs: 'state',
      when: c => c.state.key !== 'to-deploy' ? null
        : { say: 'move it to the deploy status' + (c.transitions.length ? ' — Jira offers ' + c.transitions.slice(0, 2).join(', ') : ''), why: 'the ticket is To Deploy' } },
    { id: 'verify', act: true, needs: 'fact',
      when: c => c.facts.indexOf('Ready to Test') === -1 ? null
        : { say: 'verify it in staging, then close the ticket', why: 'the Jira status is Ready to Test' } },
    { id: 'green-but-blocked', act: true, needs: 'fact',
      when: c => (c.facts.indexOf('merge blocked') === -1 || c.facts.indexOf('checks green') === -1) ? null
        : { say: 'you need an approval — checks are green and nothing else blocks it', why: 'the facts read checks green and merge blocked, which is a review' } },
    { id: 'answer-a-thread', act: true, needs: 'state',
      when: c => {
        const want = c.kids.concat([{ ref: c.ref, state: c.state.key, label: c.state.label }])
          .filter(k => ['needs-comments', 'needs-reply'].indexOf(k.state) !== -1)[0];
        if (!want) return null;
        return { say: 'answer the comments on ' + want.ref, why: want.ref + ' is ' + want.label };
      } },
    { id: 'on-them', act: false, needs: 'state',
      when: c => ['waiting-reply', 'waiting'].indexOf(c.state.key) === -1 ? null
        : { say: 'nothing to do — it is on them', why: 'the card reads ' + c.state.label } },
    { id: 'with-support', act: false, needs: 'state',
      when: c => c.state.key !== 'with-support' ? null
        : { say: 'with support — nothing for you until they answer', why: 'the Jira status is Support Investigating' } },
    { id: 'all-in', act: false, needs: 'child',
      when: c => (!c.kids.length || c.openKids.length) ? null
        : { say: 'nothing left to do on this card', why: 'every linked item is merged or closed' } },
    { id: 'jira-offers', act: true, needs: 'transition',
      when: c => !c.transitions.length ? null
        : { say: 'Jira offers: ' + c.transitions.join(', '), why: 'the transitions available from ' + c.state.label } },
  ];

  /** The line for one card, plus what it would take to have one. `reads` carries
      the evidence a per-card read added; nothing else here touches the network. */
  function nextOf(row, reads, ctx) {
    const ev = evidenceOf(row, reads);
    const c = nextShape(row, ev);
    const options = ctx || {};
    let next = null, rule = null;
    for (const r of NEXT_RULES) {
      const hit = r.when(c, options);
      if (hit) { next = { say: hit.say, why: hit.why, act: r.act !== false }; rule = r; break; }
    }
    return {
      next, rule,
      /** Rules that stand on kinds nobody has read — the price of not reading. */
      wanting: next ? [] : NEXT_RULES.filter(r => !NEXT_KINDS[r.needs].free && !(reads || {})[r.needs])
        .map(r => ({ id: r.id, needs: r.needs })),
    };
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
    const rows = [].concat(...(data.tiers || []).map(t => t.items), data.drafts || [], data.closed || []);
    const gone = (data.changes || {}).gone || [];

    const snoozeOf = row => (snoozes[row.key] && snoozes[row.key] > now) ? snoozes[row.key] : null;
    // An ack holds only while the card has not moved since you acknowledged it.
    const ackOf = row => {
      const at = acks[row.key];
      return (at && (!row.lastChangedAt || row.lastChangedAt <= at)) ? at : null;
    };
    const parked = row => !!(snoozeOf(row) || ackOf(row));
    /** Renders in a tier: work nobody has parked. */
    const work = row => !parked(row);

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
      return gone.filter(r => r.section === section
        && !parked(r) && since && r.goneAt > since && !allKidsLive(r));
    }

    const tiers = (data.tiers || []).map(t => {
      const list = t.items.filter(work).concat(ghosts(t.key));
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
      drafts: () => [folds.drafts.some(r => (r.states || []).some(s => s.key === 'conflicts')) ? 'conflicts' : '',
        oldestClause(folds.drafts)
      ].filter(Boolean).join(' · '),
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

    /** The rows that moved since your last look, in the order a surface renders
        them — the jump list under the sentence. A card that is only new is not
        here: it is already on screen where it belongs, and a jump to it would
        be a jump to nowhere. */
    function changedRows() {
      const all = tiers.flatMap(t => t.rows)
        .concat(folds.parked, folds.drafts, folds.closed);
      return all.filter(r => { const f = flagOf(r, since); return f && f.kind === 'moved'; });
    }

    const formatWhen = iso => 'until ' + new Date(iso).toLocaleString([], { weekday: 'short', hour: '2-digit', minute: '2-digit' });
    const changeLabel = f => f.kind === 'new' ? 'new' : f.kind === 'gone' ? 'dropped' : (f.label || 'changed');
    // a move out of the needs tier reads green; into it, amber.
    const changeTone = f => !f ? ''
      : (f.kind === 'moved' ? (f.to_tier === 'needs' ? 'moved' : 'moved out') : f.kind);
    const changeClass = f => !f ? '' : ' chg-' + changeTone(f);

    return {
      snoozeOf, ackOf, parked, specs,
      /** The line one card asks for, from the evidence the board already has.
          `reads` carries whatever a per-card read added; nothing here reads. */
      nextOf: (row, reads, ctx) => nextOf(row, reads,
        { lastLook: (ctx || {}).lastLook !== undefined ? ctx.lastLook : seenAt }),
      flag: row => flagOf(row, since),
      closed: isClosed,
      tiers, folds, notes, nextCard, counts, summaryText, changedRows, formatWhen, changeLabel, changeTone, changeClass,      /** The five choices one control offers: hours, or "ack" (until it changes). */
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
