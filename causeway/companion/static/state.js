/** Scene state folded from the companion's event stream (SPEC 11.2), plus the labels drawn from it. No three.js, no DOM. */

const PRIORITY = { UNSOURCED: 0, IN: 1, CARRY: 2, GOAL: 3, SELF: 4, PROSE: 5, LIT: 6 };

export function createState() {
  return {
    hello: null,
    status: 'idle',
    records: [],
    byId: {},
    origins: {},
    gates: {},
    openGate: null,
    rivals: {},
    final: null,
    whys: [],
    asking: null,
    errors: [],
    lastSeq: -1,
  };
}

/** Empty `state` in place, so references held elsewhere stay valid. */
export function clearState(state) {
  for (const key of Object.keys(state)) delete state[key];
  Object.assign(state, createState());
}

/** Fold one event into `state`. Returns false when the event changed nothing (a repeated record). */
export function applyEvent(state, ev) {
  if (typeof ev.seq === 'number') state.lastSeq = ev.seq;
  switch (ev.type) {
    case 'hello':
      state.hello = ev;
      return true;
    case 'status':
      state.status = ev.state;
      return true;
    case 'record': {
      const record = ev.record;
      if (!record || !record.id || state.byId[record.id]) return false;
      state.records.push(record);
      state.byId[record.id] = record;
      if (ev.origin) state.origins[record.id] = ev.origin;
      return true;
    }
    case 'gate':
      state.gates[ev.gate_id] = { ...ev, verdict: null };
      state.openGate = ev.gate_id;
      return true;
    case 'rivals':
      state.rivals[ev.decision] = ev.rivals;
      return true;
    case 'resolved': {
      const gate = state.gates[ev.gate_id];
      if (gate) gate.verdict = ev.verdict;
      if (state.openGate === ev.gate_id) {
        const open = Object.values(state.gates).filter((g) => !g.verdict);
        state.openGate = open.length ? open[open.length - 1].gate_id : null;
      }
      return true;
    }
    case 'final':
      state.final = { decision: ev.decision, text: ev.text };
      return true;
    case 'why':
      state.whys.push({ question: ev.question, result: ev.result });
      if (state.asking === ev.question) state.asking = null;
      return true;
    case 'error':
      state.errors.push(ev.message);
      if (ev.operation === 'why' && state.asking === ev.question) state.asking = null;
      return true;
    default:
      return false;
  }
}

export function openGate(state) {
  return state.openGate ? state.gates[state.openGate] : null;
}

/** 'approved', 'denied' or null for a decision, from the gates on its actions. */
export function verdictOf(state, decisionId) {
  const gates = Object.values(state.gates).filter((g) => g.decision === decisionId && g.verdict);
  if (!gates.length) return null;
  return gates.some((g) => g.verdict === 'deny') ? 'denied' : 'approved';
}

export function tagRank(tag) {
  const [family, k] = String(tag || '').split('·');
  return (PRIORITY[family] ?? 9) + (k ? Number(k) / 100 : 0);
}

export function isGoal(record) {
  return record.kind === 'event' && record.step === 1 && ['system', 'developer', 'user'].includes(record.role);
}

/** Leaves of an args object as [path, value] pairs, nulls skipped (SPEC 5.1). */
export function flatten(value, prefix = '') {
  if (value === null || value === undefined) return [];
  if (Array.isArray(value)) return value.flatMap((v, i) => flatten(v, prefix ? `${prefix}.${i}` : String(i)));
  if (typeof value === 'object') {
    return Object.entries(value).flatMap(([k, v]) => flatten(v, prefix ? `${prefix}.${k}` : k));
  }
  return [[prefix, value]];
}

/** Values that name what an action targets: no whitespace, not trivial (SPEC 6.1). */
function keyValues(args) {
  return flatten(args)
    .map(([, v]) => v)
    .filter((v) => (typeof v === 'number' ? Math.abs(v) >= 10 : typeof v === 'string' && v.trim().length >= 3 && !/\s/.test(v)));
}

export function actionLabel(action) {
  return `${action.name}(${keyValues(action.args).join(', ')})`;
}

export function recordLabel(record, origin) {
  if (record.kind === 'event') {
    if (origin) return origin;
    if (record.role === 'tool') return record.name ? `${record.name} result` : 'tool result';
    return `${record.role} message`;
  }
  if (record.final) return 'final answer';
  if (!record.actions.length) return 'no action';
  return record.actions.map(actionLabel).join(' + ');
}

function collapse(text) {
  return String(text || '')
    .split('\n')
    .map((line) => line.replace(/^[#>\-*\s]+/, '').trim())
    .filter(Boolean)
    .join(' · ');
}

/** A short second line for a card: what an event said, or what a decision said or drew on. */
export function recordSnippet(record) {
  if (record.kind === 'event') {
    const role = record.role === 'system' ? 'system: ' : record.role === 'user' ? 'user: ' : '';
    return role + collapse(record.text);
  }
  if (record.text) return collapse(record.text);
  const sources = [...new Set(record.causes.filter((c) => c.source && c.tag !== 'PROSE').map((c) => c.source))];
  return sources.length ? `← ${sources.join(' · ')}` : '';
}

/**
 * Threads from a decision to the records its causes came from: one per (source, tag), PROSE
 * sources included. A cause with no source and tag UNSOURCED gives a dangling thread (source null).
 */
export function causeThreads(record) {
  const seen = new Map();
  for (const cause of record.causes || []) {
    if (cause.tag === 'PROSE') {
      for (const source of cause.sources || (cause.source ? [cause.source] : [])) {
        const key = `${source}|PROSE`;
        if (!seen.has(key)) seen.set(key, { key, source, tag: 'PROSE', paths: [] });
        seen.get(key).paths.push(cause.path);
      }
      continue;
    }
    if (cause.tag === 'LIT') continue;
    if (!cause.source && cause.tag !== 'UNSOURCED') continue;
    const key = cause.source ? `${cause.source}|${cause.tag}` : `null|${cause.action}|${cause.path}`;
    if (!seen.has(key)) seen.set(key, { key, source: cause.source || null, tag: cause.tag, paths: [] });
    seen.get(key).paths.push(cause.path);
  }
  return [...seen.values()].sort((a, b) => tagRank(a.tag) - tagRank(b.tag));
}

/** The gate's taints, plain arguments before atoms, then by tag priority. */
export function taintLines(gate) {
  return [...(gate.taints || [])].sort((a, b) => {
    const atomA = a.path.includes('~') ? 1 : 0;
    const atomB = b.path.includes('~') ? 1 : 0;
    if (atomA !== atomB) return atomA - atomB;
    const rank = tagRank(a.tag) - tagRank(b.tag);
    if (rank) return rank;
    return a.path.localeCompare(b.path, undefined, { numeric: true });
  });
}

export function showValue(value) {
  if (value === null || value === undefined) return 'nothing';
  return String(value).replace(/\s+/g, ' ').trim();
}

/** Split lines for a rivals record: [{path, action, parts: [{value, count}], n, near_miss, upset}]. */
export function rivalSplits(rivals) {
  if (!rivals) return [];
  const n = (rivals.k ?? 0) + 1;
  return (rivals.splits || []).map((split) => ({
    action: split.action,
    path: split.path,
    n,
    parts: split.votes.map((v) => ({ value: showValue(v.value), count: v.count })),
    chosen: showValue(split.chosen),
    near_miss: split.near_miss,
    upset: split.upset,
  }));
}

/** Preset why questions: one about the latest watched argument, then two general ones. */
export function whyPresets(state) {
  const presets = [];
  const gate = openGate(state) || Object.values(state.gates).slice(-1)[0];
  const first = gate ? taintLines(gate)[0] : null;
  if (first) {
    const base = first.path.split('~')[0].split('.')[0];
    const named = {
      to: ['Why this recipient?', 'Where did the recipient come from?'],
      path: ['Why this file?', 'Where did the file name come from?'],
      url: ['Why this link?', 'Where did the link come from?'],
      content: ['Why this content?', 'Where did the written content come from?'],
    }[base];
    presets.push(
      named
        ? { label: named[0], question: named[1] }
        : { label: `Why this ${base}?`, question: `Where did the ${base} of ${gate.call.name} come from?` },
    );
  } else {
    presets.push({ label: 'Why that last step?', question: 'Why did the agent take its last action?' });
  }
  presets.push({ label: 'Did it follow the task?', question: 'Did it do what the task asked?' });
  presets.push({ label: 'What did it read?', question: 'What did it read before acting, and what did it use?' });
  return presets;
}

/** Why questions are offered after the final answer or while a gate is open (SPEC 11.4). */
export function canAsk(state) {
  return Boolean(state.final || state.openGate);
}
