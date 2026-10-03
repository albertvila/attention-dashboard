/* attention-view.js — what a *reader* sees, shared verbatim by the dashboard and
   the dsh panel.

   The producer owns what is true (states, tiers, links, change stamps). This
   owns the layer on top of it: change flags since the reader's last look, ghosts
   of what left, and what the reader parked (snooze / acknowledge). It is
   dependency-free and side-effect-free; load it as a classic script and use
   window.AttentionView.

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

  function overlay(data, options) {
    const opts = options || {};
    const seenAt = opts.seenAt || null;
    const snoozes = opts.snoozes || {};
    const acks = opts.acks || {};
    const now = opts.now || new Date().toISOString();
    const since = seenAt || (data.changes || {}).previousAt || null;
    const rows = [].concat(...(data.tiers || []).map(t => t.items), data.drafts || [], data.closed || []);
    const gone = (data.changes || {}).gone || [];

    const isMail = row => row.chip === 'MAIL';
    const snoozeOf = row => (snoozes[row.key] && snoozes[row.key] > now) ? snoozes[row.key] : null;
    // An ack holds only while the card has not moved since you acknowledged it.
    const ackOf = row => {
      const at = acks[row.key];
      return (at && (!row.lastChangedAt || row.lastChangedAt <= at)) ? at : null;
    };
    const parked = row => !!(snoozeOf(row) || ackOf(row));
    /** Renders in a tier: work that is neither mail (its own fold) nor parked. */
    const work = row => !isMail(row) && !parked(row);

    function ghosts(section) {
      // The Needs queue is for things you can act on; a struck-through card
      // there reads as "do I still owe this?".
      if (section === 'needs') return [];
      return gone.filter(r => r.section === section && !isMail(r) && !parked(r) && since && r.goneAt > since);
    }

    const tiers = (data.tiers || []).map(t => {
      const list = t.items.filter(work).concat(ghosts(t.key));
      return { key: t.key, title: t.title, rows: list, count: list.length };
    });

    const folds = {
      snoozed: rows.filter(r => snoozeOf(r)),
      acked: rows.filter(r => ackOf(r)),
      mail: rows.filter(r => isMail(r) && !parked(r)),
      drafts: (data.drafts || []).filter(work).concat(ghosts('drafts')),
      closed: (data.closed || []).filter(work).concat(ghosts('closed')),
    };

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
      snoozeOf, ackOf, parked,
      flag: row => flagOf(row, since),
      tiers, folds, counts, summaryText, formatWhen, changeLabel, changeTone, changeClass,
      /** The five choices one control offers: hours, or "ack" (until it changes). */
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
