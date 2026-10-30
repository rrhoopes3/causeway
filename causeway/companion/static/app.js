/** The companion scene (SPEC 11.4): records as cards, the masked companion, gates and why, live from /events (SPEC 11.2). */

import * as THREE from 'three';
import { Board } from './board.js';
import { Companion } from './companion.js';
import { Input } from './input.js';
import { follow, post } from './net.js';
import { Button, Caption, GatePanel, Speech, WhyBoard } from './panels.js';
import { applyEvent, canAsk, clearState, createState, openGate, showValue, taintLines, whyPresets } from './state.js';
import { APPROVE, DENY, FAINT, GOLD, INK, MUTED, canvas } from './text.js';

const BACKGROUND = new THREE.Color('#0b0d11');
const DESKTOP_EYE = new THREE.Vector3(0, 0.03, 0.45);
const DESKTOP_PITCH = -0.1;
const ORIGIN = new THREE.Vector3();
const UP = new THREE.Vector3(0, 1, 0);
const PLACE = {
  companion: new THREE.Vector3(0, -0.13, -0.95),
  speech: new THREE.Vector3(0.275, 0.035, -0.92),
  why: new THREE.Vector3(0, 0.05, -1.0),
  gate: new THREE.Vector3(0, -0.255, -0.86),
  start: new THREE.Vector3(0, -0.33, -0.9),
  caption: new THREE.Vector3(0, -0.385, -0.9),
  asks: new THREE.Vector3(-0.268, 0.02, -0.92),
};

const state = createState();
const events = [];
const ui = { conn: 'connecting', deciding: null, said: '', glanceUntil: 0, xrMode: null, recenter: false };

const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.setSize(window.innerWidth, window.innerHeight);
renderer.xr.enabled = true;
renderer.domElement.id = 'scene';
renderer.domElement.setAttribute('aria-label', 'Companion scene');
document.body.prepend(renderer.domElement);

const scene = new THREE.Scene();
const desktopBackground = backgroundTexture();
scene.background = desktopBackground;
const camera = new THREE.PerspectiveCamera(58, window.innerWidth / window.innerHeight, 0.02, 40);
camera.position.copy(DESKTOP_EYE);
scene.add(camera);

const root = new THREE.Group();
scene.add(root);
root.add(new THREE.HemisphereLight('#d5dbff', '#2a1d2c', 2.1));
const key = new THREE.DirectionalLight('#fff0dc', 2.6);
key.position.set(0.5, 0.9, 0.4);
key.target.position.copy(PLACE.companion);
const rim = new THREE.DirectionalLight('#9a86ff', 3.2);
rim.position.set(-0.35, 0.5, -2.2);
rim.target.position.copy(PLACE.companion);
root.add(key, key.target, rim, rim.target);

const companion = new Companion();
companion.group.position.copy(PLACE.companion);
root.add(companion.group);
companion.loadModel('companion.glb');

const board = new Board();
board.spawn.copy(PLACE.companion).add(new THREE.Vector3(0, 0.03, 0.05));
root.add(board.group);

const gatePanel = new GatePanel({ onDecide: decide });
place(gatePanel.group, PLACE.gate);
const whyBoard = new WhyBoard();
const speech = new Speech();
place(speech.group, PLACE.speech);
const caption = new Caption(0.36);
place(caption.group, PLACE.caption);
root.add(gatePanel.group, whyBoard.group, speech.group, caption.group);

const startButton = new Button({ label: 'Start', width: 0.14, height: 0.052, color: GOLD, solid: true, onSelect: start });
const resetButton = new Button({ label: 'Reset', width: 0.11, height: 0.052, color: MUTED, onSelect: reset });
const startRow = new THREE.Group();
place(startRow, PLACE.start);
startButton.mesh.position.x = -0.065;
resetButton.mesh.position.x = 0.075;
startRow.add(startButton.mesh, resetButton.mesh);
root.add(startRow);

const askButtons = [0, 1, 2].map((i) => new Button({ label: '', width: 0.235, height: 0.05, color: GOLD, onSelect: () => ask(i) }));
const askColumn = new THREE.Group();
place(askColumn, PLACE.asks);
askButtons.forEach((b, i) => {
  b.mesh.position.y = -i * 0.062;
  askColumn.add(b.mesh);
});
root.add(askColumn);

const input = new Input({
  renderer,
  scene,
  camera,
  targets: () => [
    ...gatePanel.targets(),
    startButton.mesh,
    resetButton.mesh,
    ...askButtons.map((b) => b.mesh),
    ...board.targets(),
  ],
  onMiss: () => board.clearFocus(),
});
input.setLook(0, DESKTOP_PITCH);

const dom = {
  conn: document.getElementById('conn'),
  run: document.getElementById('run'),
  meta: document.getElementById('meta'),
  task: document.getElementById('task'),
  start: document.getElementById('start'),
  approve: document.getElementById('approve'),
  deny: document.getElementById('deny'),
  gateControls: document.getElementById('gate-controls'),
  asks: document.getElementById('asks'),
  reset: document.getElementById('reset'),
  xr: document.getElementById('xr'),
  toast: document.getElementById('toast'),
  announce: document.getElementById('announce'),
};
dom.start.addEventListener('click', start);
dom.approve.addEventListener('click', () => decide('approve'));
dom.deny.addEventListener('click', () => decide('deny'));
dom.reset.addEventListener('click', reset);
dom.xr.addEventListener('click', toggleXR);

window.__causeway = {
  state,
  events,
  snapshot() {
    renderer.render(scene, camera);
    return renderer.domElement.toDataURL('image/png');
  },
};

/** Place an object in root space so that it faces the viewer's eye at the root's origin. */
function place(object, at) {
  object.position.copy(at);
  const m = new THREE.Matrix4().lookAt(ORIGIN, at, UP);
  object.quaternion.setFromRotationMatrix(m);
}

function backgroundTexture() {
  const el = canvas(512, 512);
  const ctx = el.getContext('2d');
  const g = ctx.createRadialGradient(256, 220, 20, 256, 256, 380);
  g.addColorStop(0, '#191b26');
  g.addColorStop(0.55, '#0f1117');
  g.addColorStop(1, '#07080b');
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, 512, 512);
  const tex = new THREE.CanvasTexture(el);
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

function resetScene() {
  clearState(state);
  events.length = 0;
  board.reset();
  companion.reset();
  gatePanel.hide(true);
  whyBoard.show(null, null, true);
  speech.hide(true);
  ui.deciding = null;
  ui.said = '';
}

function handle(event, fresh, burst) {
  if (fresh || event.type === 'hello') resetScene();
  else if (typeof event.seq === 'number' && event.seq <= state.lastSeq) return;
  events.push(event);
  if (!applyEvent(state, event)) return;
  const instant = burst;
  switch (event.type) {
    case 'record': {
      const record = event.record;
      board.add(record, instant);
      if (record.kind === 'decision' && record.text && !record.final) say(record.text, instant);
      if (!instant && !state.openGate) glance(board.cards.get(record.id));
      break;
    }
    case 'gate': {
      const gate = openGate(state);
      board.setGate(gate);
      companion.setLifted(true, instant);
      companion.lookAt(board.cards.get(gate.decision)?.target.pos || null);
      gatePanel.show(gate, state.rivals[gate.decision], instant);
      speech.hide(instant);
      announce(gateText(gate));
      break;
    }
    case 'rivals':
      if (state.openGate && state.gates[state.openGate].decision === event.decision) gatePanel.setRivals(event.rivals);
      break;
    case 'resolved': {
      if (ui.deciding === event.gate_id) ui.deciding = null;
      const next = openGate(state);
      board.setGate(next);
      if (next) {
        gatePanel.show(next, state.rivals[next.decision], instant);
      } else {
        gatePanel.hide(instant);
        companion.setLifted(false, instant);
        companion.lookAt(null);
        if (!instant) companion.react(event.verdict);
        if (ui.said) speech.say(ui.said, instant);
      }
      break;
    }
    case 'final':
      say(event.text, instant);
      break;
    case 'why': {
      const why = state.whys[state.whys.length - 1];
      board.setCited(citedIds(why.result));
      announce(`Why: ${why.question} ${why.result?.answer || ''}`);
      break;
    }
    case 'error':
      toast(event.message || 'error');
      break;
    default:
      break;
  }
  if (['hello', 'why', 'gate', 'resolved', 'final', 'status', 'error'].includes(event.type)) showWhy(instant);
  board.sync(state, state.origins);
  refresh();
}

function say(text, instant) {
  ui.said = text;
  if (!state.openGate) speech.say(text, instant);
  if (!instant) companion.react('speak');
}

function glance(card) {
  if (!card) return;
  companion.lookAt(card.target.pos);
  ui.glanceUntil = performance.now() + 1300;
}

function showWhy(instant) {
  const why = state.whys[state.whys.length - 1] || null;
  whyBoard.show(why, state.asking, instant);
}

function citedIds(result) {
  return (result?.supported || []).flatMap((claim) => (claim.cites || []).map((c) => c.id));
}

function gateText(gate) {
  const lines = taintLines(gate).map((t) => `${t.path} = ${showValue(t.value)} from ${t.label || 'nowhere in the run'} [${t.tag}]`);
  return `Gate ${gate.gate_id}: ${gate.decision} wants to call ${gate.call.name}. ${lines.join('. ')}.`;
}

async function start() {
  if (state.status === 'done' || state.status === 'error') return reset();
  const res = await post('/start', {});
  if (res.status === 409) toast('A run is already going.');
  else if (!res.ok) toast('Could not start the run.');
}

async function reset() {
  const res = await post('/reset', {});
  if (!res.ok) toast('Could not reset.');
}

async function decide(verdict) {
  const gate = openGate(state);
  if (!gate || ui.deciding) return;
  ui.deciding = gate.gate_id;
  gatePanel.setBusy(true);
  refresh();
  const res = await post('/decide', { gate_id: gate.gate_id, verdict });
  if (!res.ok) {
    ui.deciding = null;
    gatePanel.setBusy(false);
    toast(res.status === 404 ? 'That gate was already resolved.' : 'Could not reach the server.');
    refresh();
  }
}

async function ask(index) {
  const preset = whyPresets(state)[index];
  if (!preset || state.asking) return;
  state.asking = preset.question;
  showWhy(false);
  refresh();
  const res = await post('/why', { question: preset.question });
  if (!res.ok) {
    state.asking = null;
    showWhy(false);
    refresh();
    toast('Could not ask why.');
  }
}

function toast(message) {
  dom.toast.textContent = message;
  dom.toast.classList.add('show');
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => dom.toast.classList.remove('show'), 4200);
}

function announce(text) {
  dom.announce.textContent = text;
}

/** Bring every control, in the scene and in the page, in line with the state. */
function refresh() {
  const live = ui.conn === 'live';
  const status = state.status;
  const gate = openGate(state);
  const finished = status === 'done' || status === 'error';
  const startLabel = finished ? 'New run' : status === 'idle' ? 'Start' : 'Running';
  const canStart = live && (status === 'idle' || finished);
  startButton.set({ label: startLabel, enabled: canStart });
  resetButton.set({ enabled: live });
  startRow.visible = !gate;
  dom.start.textContent = startLabel;
  dom.start.disabled = !canStart;
  dom.reset.disabled = !live;

  dom.gateControls.hidden = !gate;
  dom.approve.disabled = dom.deny.disabled = !gate || Boolean(ui.deciding);

  const asking = canAsk(state);
  const presets = whyPresets(state);
  askColumn.visible = asking;
  askButtons.forEach((b, i) => b.set({ label: presets[i].label, enabled: !state.asking }));
  dom.asks.hidden = !asking;
  const labels = presets.map((p) => p.label).join('|');
  if (dom.asks.dataset.labels !== labels) {
    dom.asks.dataset.labels = labels;
    dom.asks.replaceChildren(
      ...presets.map((p, i) => {
        const b = document.createElement('button');
        b.className = 'ask';
        b.textContent = p.label;
        b.title = p.question;
        b.addEventListener('click', () => ask(i));
        return b;
      }),
    );
  }
  for (const b of dom.asks.children) b.disabled = Boolean(state.asking);

  const hello = state.hello;
  dom.conn.textContent = ui.conn;
  document.body.dataset.conn = ui.conn;
  dom.run.textContent = status;
  dom.meta.textContent = hello ? [hello.scenario, hello.model, hello.replay ? 'replay' : null].filter(Boolean).join(' · ') : '';
  dom.task.textContent = hello?.task || '';
  const dot = live ? APPROVE : ui.conn === 'connecting' ? GOLD : DENY;
  caption.set([
    ['● ', dot],
    [ui.conn, MUTED],
    ['  ·  ', FAINT],
    [status, status === 'waiting' ? GOLD : INK],
    ...(hello ? [['  ·  ', FAINT], [[hello.scenario, hello.model].filter(Boolean).join(' · '), FAINT]] : []),
  ]);
}

function onConnection(conn) {
  ui.conn = conn;
  refresh();
}

function onResize() {
  if (renderer.xr.isPresenting) return;
  camera.aspect = window.innerWidth / window.innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(window.innerWidth, window.innerHeight);
}

async function setupXR() {
  if (!navigator.xr) {
    dom.xr.textContent = 'XR not available';
    return;
  }
  const supported = (mode) => navigator.xr.isSessionSupported(mode).catch(() => false);
  if (await supported('immersive-ar')) ui.xrMode = 'immersive-ar';
  else if (await supported('immersive-vr')) ui.xrMode = 'immersive-vr';
  if (!ui.xrMode) {
    dom.xr.textContent = 'XR not available';
    return;
  }
  dom.xr.disabled = false;
  dom.xr.textContent = ui.xrMode === 'immersive-ar' ? 'Enter XR · passthrough' : 'Enter XR';
}

async function toggleXR() {
  const current = renderer.xr.getSession();
  if (current) {
    await current.end();
    return;
  }
  try {
    const session = await navigator.xr.requestSession(ui.xrMode, { optionalFeatures: ['local-floor', 'hand-tracking'] });
    const floor = !session.enabledFeatures || session.enabledFeatures.includes('local-floor');
    renderer.xr.setReferenceSpaceType(floor ? 'local-floor' : 'local');
    await renderer.xr.setSession(session);
  } catch (err) {
    toast(`Could not enter XR: ${err.message || err}`);
  }
}

renderer.xr.addEventListener('sessionstart', () => {
  const ar = ui.xrMode === 'immersive-ar';
  scene.background = ar ? null : BACKGROUND;
  renderer.setClearColor(0x000000, ar ? 0 : 1);
  ui.recenter = true;
  renderer.xr.getReferenceSpace()?.addEventListener?.('reset', () => {
    ui.recenter = true;
  });
  dom.xr.textContent = 'Exit XR';
});

renderer.xr.addEventListener('sessionend', () => {
  scene.background = desktopBackground;
  renderer.setClearColor(0x000000, 1);
  root.position.set(0, 0, 0);
  root.rotation.set(0, 0, 0);
  camera.position.copy(DESKTOP_EYE);
  camera.scale.set(1, 1, 1);
  camera.updateProjectionMatrix();
  dom.xr.textContent = ui.xrMode === 'immersive-ar' ? 'Enter XR · passthrough' : 'Enter XR';
  onResize();
});

/** Put the content about a metre in front of the viewer, level, facing them. */
function recenter(frame) {
  const pose = frame.getViewerPose(renderer.xr.getReferenceSpace());
  if (!pose) return;
  const { position, orientation } = pose.transform;
  const q = new THREE.Quaternion(orientation.x, orientation.y, orientation.z, orientation.w);
  const forward = new THREE.Vector3(0, 0, -1).applyQuaternion(q);
  root.position.set(position.x, position.y, position.z);
  root.rotation.set(0, Math.atan2(-forward.x, -forward.z), 0);
  ui.recenter = false;
}

let last = 0;
renderer.setAnimationLoop((time, frame) => {
  const t = time / 1000;
  const dt = last ? Math.min(0.05, (time - last) / 1000) : 1 / 60;
  last = time;
  if (ui.recenter && frame) recenter(frame);
  if (!state.openGate && ui.glanceUntil && performance.now() > ui.glanceUntil) {
    ui.glanceUntil = 0;
    companion.lookAt(null);
  }
  input.update();
  companion.update(dt, t);
  board.update(dt, t);
  gatePanel.update(dt);
  whyBoard.update(dt, t);
  place(whyBoard.group, new THREE.Vector3(PLACE.why.x, PLACE.why.y + whyBoard.sheet.height, PLACE.why.z));
  speech.update(dt);
  for (const b of [startButton, resetButton, ...askButtons]) b.update(dt);
  renderer.render(scene, camera);
});

window.addEventListener('resize', onResize);
setupXR();
refresh();
follow(handle, onConnection);
