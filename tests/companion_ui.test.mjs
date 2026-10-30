// Exercise the real panel drawing code with a recording canvas, without a GPU or browser.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import { applyEvent, createState } from '../causeway/companion/static/state.js';

const staticURL = new URL('../causeway/companion/static/', import.meta.url);
const panelSource = (await readFile(new URL('panels.js', staticURL), 'utf8'))
  .replace("'three'", JSON.stringify(new URL('vendor/three/three.module.min.js', staticURL).href))
  .replace("'./text.js'", JSON.stringify(new URL('text.js', staticURL).href))
  .replace("'./state.js'", JSON.stringify(new URL('state.js', staticURL).href));
const { GatePanel, WhyBoard } = await import(`data:text/javascript;base64,${Buffer.from(panelSource).toString('base64')}`);

globalThis.document = {
  createElement(name) {
    assert.equal(name, 'canvas');
    const canvas = { width: 0, height: 0, text: [] };
    const context = new Proxy({
      canvas,
      measureText: (text) => ({ width: String(text).length * 12 }),
      fillText: (text) => canvas.text.push(String(text)),
      clearRect: () => { canvas.text.length = 0; },
      createLinearGradient: () => ({ addColorStop() {} }),
    }, { get: (obj, key) => key in obj ? obj[key] : () => {} });
    canvas.getContext = () => context;
    return canvas;
  },
};

test('a failed why request clears its pending state so another question can be asked', () => {
  const state = createState();
  state.asking = 'Why this recipient?';
  applyEvent(state, { type: 'error', operation: 'why', question: state.asking, message: 'why failed: timeout' });
  assert.equal(state.asking, null);
  assert.deepEqual(state.errors, ['why failed: timeout']);
  const panel = new WhyBoard();
  panel.show(null, state.asking, true);
  assert.equal(panel.fader.target, 0);
});

test('unrelated failures do not cancel an in-flight why request', () => {
  const state = createState();
  state.asking = 'Why this recipient?';
  applyEvent(state, { type: 'error', message: 'rivals failed' });
  applyEvent(state, { type: 'error', operation: 'why', question: 'An older question', message: 'timeout' });
  assert.equal(state.asking, 'Why this recipient?');
});

test('the why panel renders absence warnings and includes them in the total', () => {
  const panel = new WhyBoard();
  panel.show({ question: 'What happened?', result: {
    answer: 'The agent read a file. It never sent an email.',
    supported: [{ text: 'The agent read a file.', cites: [{ id: 'd1' }] }],
    unproven: [{ claim: { text: 'It never sent an email.', cites: [{ id: 'd1' }] }, reasons: ['claims an absence; a quote is not proof'] }],
    struck: [{ claim: { text: 'An invented claim.', cites: [] }, reasons: ['no citations'] }],
  } }, null, true);
  const text = panel.sheet.canvas.text;
  assert.ok(text.includes('QUOTE FOUND · 1 of 3'));
  assert.ok(text.includes('UNPROVEN · quote found, not proof'));
  assert.ok(text.includes('claims an absence; a quote is not proof'));
  assert.ok(text.includes('It never sent an email.'));
  assert.ok(text.includes('?'));
  assert.ok(text.includes('✕'));
});

function gateText(rivals) {
  const panel = new GatePanel({ onDecide() {} });
  panel.show({ decision: 'd1', gate_id: 'session-g1', call: { name: 'send_email', args: { to: 'dana@lab.example' } }, taints: [] }, rivals, true);
  return panel.sheet.canvas.text.join(' ');
}

test('added rival arguments or calls do not appear unanimous when splits are empty', () => {
  const chosen = 'send_email({"to":"dana@lab.example"})';
  for (const other of [
    'send_email({"cc":"outsider@elsewhere.example","to":"dana@lab.example"})',
    `${chosen} + write_file({"path":"another.txt"})`,
  ]) {
    const text = gateText({ k: 2, chosen, votes: { [chosen]: 1, [other]: 2 }, splits: [], upset: true });
    assert.match(text, /1 of 3 samples chose this action/);
    assert.match(text, /other arguments or calls differ/);
    assert.doesNotMatch(text, /all 3 samples agree/);
  }
});

test('unanimous action votes still display agreement', () => {
  assert.match(gateText({ k: 2, chosen: 'email', votes: { email: 3 }, splits: [] }), /all 3 samples agree/);
});
