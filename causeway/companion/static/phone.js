/**
 * The owner's phone page (SPEC 12.3). It polls, because Cloudflare quick tunnels do not carry SSE. Everything on it
 * came through the agent, so it goes in with textContent and DOM properties, never as markup.
 */

import { maybeLookalike, showValue, taintLines, visible, whyPresets } from './state.js';
import { tagColor } from './text.js';

const POLL_MS = 1500;
const FETCH_MS = 8000;
const ARM_MS = 800; // a new gate's buttons wake after this, so a tap meant for the old gate cannot land on the new one
const NOTE_MS = 12000;

const HEADLINES = {
  standby: 'Waiting for Muse to check something.',
  pending: 'Checking where the values came from.',
  waiting: 'Check this before Muse acts.',
  approve: 'Approved',
  deny: 'Denied',
  clear: 'Clear',
  superseded: 'This check was replaced.',
  error: 'Causeway could not check this action.',
};
const DETAILS = {
  standby: 'Keep this page open. The mask lifts when a value in an action comes from anywhere but your request.',
  waiting: 'A value in this action came from somewhere other than your request, or could not be traced.',
  approve: 'Muse is told to go ahead the next time it checks.',
  deny: 'Muse is told not to do it the next time it checks.',
  clear: 'Every traced value came from your request, so Muse was told to go ahead. This checks where values came from, not whether they are right.',
  superseded: 'It was not approved or denied. Muse was told to check again.',
  error: 'Muse was not told to go ahead.',
};
const REPLACED = 'The check you were looking at was replaced by a newer one. It was not approved or denied.';
const CLEARED = 'The check you were looking at was cleared. It was not approved or denied.';
const CLOSED = 'That check is no longer open, so your tap changed nothing.';
const DIM_TAGS = ['LIT', 'NOARGS']; // the scene's colours for these are too dark for text on this page

const $ = (id) => document.getElementById(id);
const dom = {
  conn: $('conn'),
  headline: $('headline'),
  detail: $('detail'),
  note: $('note'),
  card: $('card'),
  action: $('action'),
  request: $('request'),
  findingsBlock: $('findings-block'),
  findings: $('findings'),
  more: $('more'),
  why: $('why'),
  whyAsk: $('why-ask'),
  whyStatus: $('why-status'),
  whyAnswer: $('why-answer'),
  whyQuestion: $('why-question'),
  whyText: $('why-text'),
  whyClaims: $('why-claims'),
  whyModel: $('why-model'),
  bar: $('bar'),
  approve: $('approve'),
  deny: $('deny'),
  setup: $('setup'),
  setupStatus: $('setup-status'),
  setupSteps: $('setup-steps'),
  setupPrompt: $('setup-prompt'),
  setupKey: $('setup-key'),
  setupRule: $('setup-rule'),
  scene: $('scene'),
};

let shown = null;
let shownText = '';
let ticket = 0;
let timer = null;
let deciding = false;
let armedGate = null;
let armedAt = 0;
let asking = null; // the why seq seen when the question was asked; a new one means it has been answered
let noteTimer = null;
let setupLoaded = false;
let weebles = null;

function el(tag, className, text) {
  const node = document.createElement(tag);
  node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function note(text) {
  clearTimeout(noteTimer);
  dom.note.textContent = text;
  dom.note.hidden = false;
  noteTimer = setTimeout(() => {
    dom.note.hidden = true;
  }, NOTE_MS);
}

/** POST JSON. Resolves to the status, or 0 when the server could not be reached in time, and never throws. */
async function post(path, body) {
  try {
    const res = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(FETCH_MS),
    });
    await res.text();
    return res.status;
  } catch {
    return 0;
  }
}

function modeOf(verdict) {
  if (verdict === 'waiting') return 'gate';
  return verdict === 'standby' || verdict === 'pending' ? 'idle' : 'verdict';
}

function findingRow(taint) {
  const row = el('li', 'finding');
  const line = el('div', 'finding-line');
  line.append(el('span', 'path', showValue(taint.path)), el('span', 'eq', ' = '), el('span', 'value', showValue(taint.value)));
  const from = el('div', 'finding-from');
  const tag = el('span', 'tag', taint.tag);
  if (!DIM_TAGS.includes(taint.tag)) tag.style.color = tagColor(taint.tag);
  from.append(el('span', 'from', taint.label === 'none' ? 'nothing was traced' : `from ${showValue(taint.label)}`), tag);
  row.append(line);
  if (maybeLookalike(taint.value)) row.append(el('div', 'lookalike', 'Not plain ASCII: a look-alike letter could be in here.'));
  row.append(from);
  return row;
}

function claimRow(status, mark, claim, reasons) {
  const row = el('li', `claim claim-${status}`);
  const body = el('div', 'claim-body');
  body.append(el('div', 'claim-text', visible(claim?.text || 'A malformed claim')));
  for (const reason of reasons) body.append(el('div', 'claim-reason', visible(reason)));
  row.append(el('div', 'claim-mark', mark), body);
  return row;
}

/** The existing why question for this gate: about its first finding, which is a tainted target when there is one. */
function whyPreset(view) {
  const gate = { call: { name: view.action }, taints: view.findings };
  return whyPresets({ openGate: 'open', gates: { open: gate } })[0];
}

function drawWhy(view) {
  const why = view.why;
  if (asking !== null && (why?.seq ?? -1) !== asking) asking = null;
  const result = why?.result;
  const open = view.verdict === 'waiting';
  dom.why.hidden = !(open || result);
  dom.whyAsk.hidden = !open || Boolean(result);
  dom.whyAsk.disabled = asking !== null;
  if (open) dom.whyAsk.textContent = why?.error ? 'Ask again' : visible(whyPreset(view).label);
  dom.whyStatus.hidden = !open || (asking === null && !why?.error);
  dom.whyStatus.classList.toggle('failed', asking === null && Boolean(why?.error));
  dom.whyStatus.textContent = asking !== null ? "Asking Meta's model. Every claim it makes is checked against what Muse reported." : why?.error || '';
  dom.whyAnswer.hidden = !result;
  if (!result) return;
  dom.whyQuestion.textContent = visible(why.question);
  dom.whyText.textContent = visible(result.answer || '');
  dom.whyClaims.replaceChildren(
    ...(result.supported || []).map((claim) => claimRow('found', 'Quote found', claim, [])),
    ...(result.unproven || []).map((item) => claimRow('unproven', 'Unproven', item.claim, item.reasons || [])),
    ...(result.struck || []).map((item) => claimRow('struck', 'Struck', item.claim, item.reasons || [])),
  );
  dom.whyModel.textContent = `Answered by ${result.model || 'the model'}. A found quote does not prove the claim.`;
}

function drawButtons() {
  const open = shown?.verdict === 'waiting';
  dom.bar.hidden = !open;
  const asleep = deciding || performance.now() < armedAt;
  dom.approve.disabled = asleep;
  dom.deny.disabled = asleep;
}

function draw(view) {
  const mode = modeOf(view.verdict);
  document.body.dataset.mode = mode;
  document.body.dataset.verdict = view.verdict;
  dom.headline.textContent = HEADLINES[view.verdict] || HEADLINES.standby;
  dom.detail.textContent = DETAILS[view.verdict] || '';
  dom.detail.hidden = !DETAILS[view.verdict];
  dom.card.hidden = view.verdict === 'standby';
  if (view.verdict !== 'standby') {
    dom.action.textContent = showValue(view.action);
    dom.request.textContent = showValue(view.request);
    dom.findingsBlock.hidden = !view.findings.length;
    dom.findings.replaceChildren(...taintLines({ taints: view.findings }).map(findingRow));
    dom.more.hidden = !view.more;
    dom.more.textContent = `And ${view.more} more not shown here. Deny if you cannot tell what this action does.`;
    drawWhy(view);
  }
  drawButtons();
  weebles?.setLifted(mode === 'gate');
}

function apply(view) {
  const before = shown;
  shown = view;
  if (before?.verdict === 'waiting' && view.gate_id !== before.gate_id) note(view.verdict === 'standby' ? CLEARED : REPLACED);
  if (view.gate_id !== before?.gate_id) asking = null;
  const answered = before && view.why && view.why.seq !== before.why?.seq;
  if (view.verdict === 'waiting' && view.gate_id !== armedGate) {
    armedGate = view.gate_id;
    armedAt = performance.now() + ARM_MS;
    setTimeout(drawButtons, ARM_MS + 20);
  }
  if (before?.verdict === 'waiting' && view.gate_id === before.gate_id && (view.verdict === 'approve' || view.verdict === 'deny')) {
    weebles?.react(view.verdict);
  }
  draw(view);
  if (view.verdict !== before?.verdict) window.scrollTo(0, 0); // the headline says what happened, and the page may have shrunk
  if (answered) (dom.whyAnswer.hidden ? dom.whyStatus : dom.whyAnswer).scrollIntoView({ block: 'nearest' });
}

function setConn(state, label) {
  document.body.dataset.conn = state;
  dom.conn.textContent = label;
}

/** One poll. A response that a tap has overtaken is dropped, so it cannot bring back buttons for a decided gate. */
async function poll() {
  clearTimeout(timer);
  const mine = ++ticket;
  try {
    if (!document.hidden) {
      const res = await fetch('state', { cache: 'no-store', signal: AbortSignal.timeout(FETCH_MS) });
      if (res.status === 401) {
        setConn('lost', 'link expired');
        note('This link no longer works. Causeway restarted, so use the new link or QR code on the PC.');
      } else if (!res.ok) {
        setConn('lost', 'server error');
      } else {
        const text = await res.text();
        if (mine === ticket) {
          setConn('live', 'live');
          if (text !== shownText) {
            shownText = text;
            apply(JSON.parse(text));
          }
        }
      }
    }
  } catch {
    if (mine === ticket) setConn('lost', 'reconnecting');
  } finally {
    clearTimeout(timer);
    timer = setTimeout(poll, POLL_MS);
  }
}

async function decide(verdict) {
  if (shown?.verdict !== 'waiting' || deciding || performance.now() < armedAt) return;
  const gate = shown.gate_id;
  deciding = true;
  drawButtons();
  ticket++;
  const status = await post('decide', { gate_id: gate, verdict });
  await poll();
  deciding = false;
  drawButtons();
  if (status === 200) return;
  if (status === 0) note('Could not reach Causeway, so your decision may not have landed. Try again.');
  else if (status !== 404) note(`Causeway did not take that (HTTP ${status}). Try again.`);
  else if (shown?.gate_id === gate && shown.verdict === 'waiting') note(CLOSED); // otherwise the state already says what happened
}

async function askWhy() {
  if (shown?.verdict !== 'waiting' || asking !== null) return;
  asking = shown.why?.seq ?? -1;
  drawWhy(shown);
  dom.whyStatus.scrollIntoView({ block: 'nearest' });
  const status = await post('why', { gate_id: shown.gate_id, question: whyPreset(shown).question });
  if (status === 202) return;
  asking = null;
  note(status === 404 ? CLOSED : 'Could not ask the model. Approve and Deny still work.');
  drawWhy(shown);
}

async function loadSetup() {
  if (setupLoaded || !dom.setup.open) return;
  try {
    const res = await fetch('setup', { cache: 'no-store', signal: AbortSignal.timeout(FETCH_MS) });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const setup = await res.json();
    dom.setupPrompt.value = setup.prompt || 'No public address yet. Restart the server with --tunnel, then reload this page.';
    dom.setupKey.value = setup.key;
    dom.setupRule.value = setup.rule;
    document.querySelector('[data-copy="setup-prompt"]').disabled = !setup.prompt;
    dom.setupStatus.hidden = true;
    dom.setupSteps.hidden = false;
    for (const box of [dom.setupPrompt, dom.setupKey, dom.setupRule]) box.style.height = `${box.scrollHeight + 2}px`;
    setupLoaded = true;
  } catch {
    dom.setupStatus.textContent = 'Could not load this. Close the section and open it again.';
  }
}

/** Runs inside the tap: iOS Safari only allows a clipboard write in a user gesture, so nothing is awaited first. */
async function copy(button) {
  const box = $(button.dataset.copy);
  const label = button.textContent;
  let copied = false;
  try {
    await navigator.clipboard.writeText(box.value);
    copied = true;
  } catch {
    box.focus();
    box.setSelectionRange(0, box.value.length);
    try {
      copied = document.execCommand('copy');
    } catch {
      copied = false;
    }
  }
  button.textContent = copied ? 'Copied' : 'Text selected. Copy it by hand.';
  setTimeout(() => {
    button.textContent = label;
  }, 2500);
}

async function loadWeebles() {
  try {
    const { mountWeebles } = await import('./phone3d.js');
    weebles = await mountWeebles(dom.scene);
    weebles.setLifted(document.body.dataset.mode === 'gate', true);
    document.body.dataset.scene = 'on';
    dom.scene.addEventListener('webglcontextlost', () => delete document.body.dataset.scene);
    dom.scene.addEventListener('webglcontextrestored', () => {
      document.body.dataset.scene = 'on';
    });
  } catch (err) {
    console.warn('keeping the still images of Weebles', err);
  }
}

dom.approve.addEventListener('click', () => decide('approve'));
dom.deny.addEventListener('click', () => decide('deny'));
dom.whyAsk.addEventListener('click', askWhy);
dom.setup.addEventListener('toggle', loadSetup);
for (const button of document.querySelectorAll('[data-copy]')) button.addEventListener('click', () => copy(button));
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) poll();
});

poll();
loadWeebles();
