/** Records as cards in two arcs, and threads from each decision to the cards its causes came from (SPEC 11.4). */

import * as THREE from 'three';
import { APPROVE, DENY, FAINT, INK, MONO, MUTED, SANS, TAG_COLORS, canvas, chip, fit, font, glowImage, panel, rgba, tagColor, wrap } from './text.js';
import { causeThreads, isGoal, recordLabel, recordSnippet, verdictOf } from './state.js';

const CANVAS_W = 512;
const CANVAS_H = 232;
export const CARD_W = 0.3;
export const CARD_H = (CARD_W * CANVAS_H) / CANVAS_W;
const ROWS = 4;
const ROW_Y = [0.13, -0.025, -0.18, -0.335];
const BASE_ANGLE = (31 * Math.PI) / 180;
const COL_STEP = (19.5 * Math.PI) / 180;
const RADIUS = 0.98;
const COL_DEPTH = 0.05;
const TOOL_COLOR = '#7d8794';
const ECHO_COLOR = '#b9c1cd';
const HOT_RADIUS = 0.0062;
const RADIUS_THREAD = 0.0017;

function frameImage() {
  const el = canvas(320, 168);
  const ctx = el.getContext('2d');
  ctx.shadowColor = '#ffffff';
  ctx.shadowBlur = 16;
  ctx.strokeStyle = 'rgba(255,255,255,0.9)';
  ctx.lineWidth = 5;
  const r = 26;
  const x = 18;
  const y = 18;
  const w = 320 - 36;
  const h = 168 - 36;
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
  ctx.stroke();
  ctx.stroke();
  return el;
}

class Card {
  constructor(board, record, side) {
    this.board = board;
    this.record = record;
    this.side = side;
    this.canvas = canvas(CANVAS_W, CANVAS_H);
    this.texture = new THREE.CanvasTexture(this.canvas);
    this.texture.colorSpace = THREE.SRGBColorSpace;
    this.texture.anisotropy = 4;
    this.material = new THREE.MeshBasicMaterial({ map: this.texture, transparent: true, alphaTest: 0.02, opacity: 0 });
    this.mesh = new THREE.Mesh(board.cardGeo, this.material);
    this.glowMat = new THREE.MeshBasicMaterial({
      map: board.frameTex,
      transparent: true,
      opacity: 0,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
    });
    this.glow = new THREE.Mesh(board.frameGeo, this.glowMat);
    this.glow.position.z = -0.004;
    this.group = new THREE.Group();
    this.group.add(this.glow, this.mesh);
    this.target = { pos: new THREE.Vector3(), quat: new THREE.Quaternion(), scale: 1 };
    this.opacity = 0;
    this.hover = false;
    this.key = '';
    this.accent = TOOL_COLOR;
    this.mesh.userData = {
      onSelect: () => board.toggleFocus(this),
      setHover: (on) => {
        this.hover = on;
      },
    };
  }

  /** Draw the card from `view`; skipped when nothing it shows has changed. */
  draw(view) {
    this.dim = Boolean(view.dim);
    const key = JSON.stringify(view);
    if (key === this.key) return;
    this.key = key;
    this.accent = view.color;
    const ctx = this.canvas.getContext('2d');
    panel(ctx, CANVAS_W, CANVAS_H, { radius: 24, alpha: 0.95 });
    ctx.save();
    ctx.beginPath();
    ctx.rect(2, 2, 10, CANVAS_H - 4);
    ctx.clip();
    ctx.fillStyle = view.color;
    ctx.fillRect(0, 0, 12, CANVAS_H);
    ctx.restore();

    ctx.textBaseline = 'alphabetic';
    ctx.font = font(32, 700, MONO);
    ctx.fillStyle = INK;
    ctx.fillText(view.id, 30, 56);
    let x = 30 + ctx.measureText(view.id).width + 14;
    ctx.font = font(28, 400, SANS);
    ctx.fillStyle = FAINT;
    ctx.fillText(`step ${view.step}`, x, 55);

    let right = CANVAS_W - 22;
    for (const [text, color, solid] of view.chips) {
      right -= chip(ctx, right, 44, text, color, { size: 28, align: 'right', solid }) + 8;
    }

    ctx.font = font(32, 600, SANS);
    ctx.fillStyle = view.dim ? MUTED : INK;
    const label = fit(ctx, view.label, CANVAS_W - 56);
    ctx.fillText(label, 30, 112);
    if (view.struck) {
      const w = ctx.measureText(label).width;
      ctx.strokeStyle = rgba(DENY, 0.9);
      ctx.lineWidth = 3;
      ctx.beginPath();
      ctx.moveTo(28, 101);
      ctx.lineTo(32 + w, 101);
      ctx.stroke();
    }
    ctx.font = font(28, 400, SANS);
    ctx.fillStyle = MUTED;
    wrap(ctx, view.snippet, CANVAS_W - 56, 2).forEach((line, i) => ctx.fillText(line, 30, 160 + i * 38));
    this.texture.needsUpdate = true;
  }

  /** The middle of the card's edge that faces the centre, in board space. */
  anchor(out) {
    out.set((-this.side * CARD_W) / 2, 0, 0).multiplyScalar(this.group.scale.x);
    out.applyQuaternion(this.group.quaternion).add(this.group.position);
    return out;
  }

  dispose() {
    this.texture.dispose();
    this.material.dispose();
    this.glowMat.dispose();
  }
}

class Thread {
  constructor(board, from, to, spec, index) {
    this.board = board;
    this.from = from;
    this.to = to;
    this.tag = spec.tag;
    this.key = spec.key;
    this.index = index;
    this.color = new THREE.Color(tagColor(spec.tag));
    this.material = new THREE.MeshBasicMaterial({ color: this.color, transparent: true, opacity: 0, depthWrite: false });
    this.mesh = new THREE.Mesh(new THREE.BufferGeometry(), this.material);
    this.haloMat = new THREE.MeshBasicMaterial({
      color: this.color,
      transparent: true,
      opacity: 0,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
    });
    this.halo = new THREE.Mesh(new THREE.BufferGeometry(), this.haloMat);
    this.halo.visible = false;
    this.spark = new THREE.Sprite(
      new THREE.SpriteMaterial({ map: board.glowTex, color: this.color, transparent: true, opacity: 0, blending: THREE.AdditiveBlending, depthWrite: false }),
    );
    this.spark.scale.setScalar(0.03);
    this.spark.visible = false;
    this.end = null;
    if (!to) {
      this.end = new THREE.Sprite(
        new THREE.SpriteMaterial({ map: board.glowTex, color: TAG_COLORS.UNSOURCED, transparent: true, opacity: 0.8, blending: THREE.AdditiveBlending, depthWrite: false }),
      );
      this.end.scale.setScalar(0.05);
      board.group.add(this.end);
    }
    this.a = new THREE.Vector3(Infinity, 0, 0);
    this.b = new THREE.Vector3();
    this.c = new THREE.Vector3();
    this.curve = new THREE.QuadraticBezierCurve3(new THREE.Vector3(), new THREE.Vector3(), new THREE.Vector3());
    this.hot = false;
    this.haloBuilt = false;
    this.opacity = 0;
    this.phase = Math.random() * 10;
    board.group.add(this.mesh, this.halo, this.spark);
  }

  ends(a, b) {
    this.from.anchor(a);
    if (this.to) {
      this.to.anchor(b);
      return;
    }
    const lift = 0.16 + 0.05 * (this.index % 3);
    b.copy(a).add(new THREE.Vector3(-this.from.side * 0.1, lift, -0.28 - 0.03 * this.index));
  }

  rebuild() {
    const { a, b, c } = this;
    if (this.to) {
      c.addVectors(a, b).multiplyScalar(0.5);
      c.y += 0.05 + 0.03 * (this.index % 4);
      c.z = Math.min(a.z, b.z) - 0.4 - 0.035 * (this.index % 4);
    } else {
      c.copy(a).add(new THREE.Vector3(-this.from.side * 0.02, 0.01, -0.16));
    }
    this.curve.v0.copy(a);
    this.curve.v1.copy(c);
    this.curve.v2.copy(b);
    this.mesh.geometry.dispose();
    this.mesh.geometry = new THREE.TubeGeometry(this.curve, 40, this.tag === 'PROSE' ? RADIUS_THREAD * 0.7 : RADIUS_THREAD, 5, false);
    this.haloBuilt = false;
    if (this.end) this.end.position.copy(b);
  }

  buildHalo() {
    this.halo.geometry.dispose();
    this.halo.geometry = new THREE.TubeGeometry(this.curve, 40, HOT_RADIUS, 6, false);
    this.haloBuilt = true;
  }

  update(dt, t, hot, emphasis) {
    const a = this.board.tmpA;
    const b = this.board.tmpB;
    this.ends(a, b);
    if (a.distanceToSquared(this.a) > 2.5e-7 || b.distanceToSquared(this.b) > 2.5e-7) {
      this.a.copy(a);
      this.b.copy(b);
      this.rebuild();
    }
    if (hot && !this.haloBuilt) this.buildHalo();
    this.hot = hot;
    const base = this.tag === 'PROSE' ? 0.28 : 0.55;
    let target = base;
    if (hot) target = 0.8 + 0.2 * Math.sin(t * 5 + this.phase);
    else if (emphasis > 0) target = 0.95;
    else if (emphasis < 0) target = 0.06;
    const appear = Math.min(this.from.opacity, this.to ? this.to.opacity : 1);
    this.opacity += (target - this.opacity) * (1 - Math.exp(-dt * 8));
    this.material.opacity = this.opacity * appear;
    this.halo.visible = hot;
    this.haloMat.opacity = hot ? (0.22 + 0.18 * Math.sin(t * 5 + this.phase)) * appear : 0;
    this.spark.visible = hot;
    if (hot) {
      const p = ((t * 0.55 + this.phase) % 1 + 1) % 1;
      this.curve.getPoint(p, this.spark.position);
      this.spark.material.opacity = Math.sin(p * Math.PI) * appear;
    }
    if (this.end) {
      const flicker = 0.65 + 0.25 * Math.sin(t * 9 + this.phase) + 0.1 * Math.sin(t * 23);
      this.end.material.opacity = flicker * appear * (emphasis < 0 ? 0.2 : 1) * (hot ? 1.2 : 0.8);
      this.end.scale.setScalar((hot ? 0.07 : 0.05) * (0.9 + 0.1 * Math.sin(t * 6)));
    }
  }

  dispose() {
    this.mesh.geometry.dispose();
    this.halo.geometry.dispose();
    this.material.dispose();
    this.haloMat.dispose();
    this.spark.material.dispose();
    this.mesh.removeFromParent();
    this.halo.removeFromParent();
    this.spark.removeFromParent();
    if (this.end) {
      this.end.material.dispose();
      this.end.removeFromParent();
    }
  }
}

export class Board {
  constructor() {
    this.group = new THREE.Group();
    this.cardGeo = new THREE.PlaneGeometry(CARD_W, CARD_H);
    this.frameGeo = new THREE.PlaneGeometry(CARD_W + 0.038, CARD_H + 0.038);
    this.frameTex = new THREE.CanvasTexture(frameImage());
    this.glowTex = new THREE.CanvasTexture(glowImage());
    this.cards = new Map();
    this.events = [];
    this.decisions = [];
    this.threads = [];
    this.focus = null;
    this.gate = null;
    this.hotKeys = new Set();
    this.cited = new Set();
    this.spawn = new THREE.Vector3(0, -0.1, -0.95);
    this.tmpA = new THREE.Vector3();
    this.tmpB = new THREE.Vector3();
  }

  reset() {
    for (const thread of this.threads) thread.dispose();
    for (const card of this.cards.values()) {
      card.group.removeFromParent();
      card.dispose();
    }
    this.cards.clear();
    this.events = [];
    this.decisions = [];
    this.threads = [];
    this.focus = null;
    this.gate = null;
    this.hotKeys.clear();
    this.cited.clear();
  }

  targets() {
    return [...this.cards.values()].map((c) => c.mesh);
  }

  /** Add a card for a new record, with threads to its sources when it is a decision. */
  add(record, instant) {
    if (this.cards.has(record.id)) return;
    const side = record.kind === 'event' ? -1 : 1;
    const card = new Card(this, record, side);
    this.cards.set(record.id, card);
    (side < 0 ? this.events : this.decisions).unshift(card);
    this.group.add(card.group);
    this.layout();
    if (instant) {
      card.group.position.copy(card.target.pos);
      card.group.quaternion.copy(card.target.quat);
      card.group.scale.setScalar(card.target.scale);
      card.opacity = 1;
    } else if (side > 0) {
      card.group.position.copy(this.spawn);
      card.group.quaternion.copy(card.target.quat);
      card.group.scale.setScalar(0.25);
    } else {
      card.group.position.copy(card.target.pos).add(new THREE.Vector3(-0.22, 0.04, 0.12));
      card.group.quaternion.copy(card.target.quat);
      card.group.scale.setScalar(0.9);
    }
    if (side > 0) {
      causeThreads(record).forEach((spec, i) => {
        const to = spec.source ? this.cards.get(spec.source) : null;
        if (spec.source && !to) return;
        this.threads.push(new Thread(this, card, to, spec, i));
      });
    }
  }

  slot(side, index, out) {
    const col = Math.floor(index / ROWS);
    const row = index % ROWS;
    const angle = side * (BASE_ANGLE + col * COL_STEP);
    const r = RADIUS + col * COL_DEPTH;
    out.pos.set(r * Math.sin(angle), ROW_Y[row], -r * Math.cos(angle));
    out.quat.setFromEuler(new THREE.Euler(0, -angle, 0));
    out.scale = col === 0 ? 1 : 0.97;
  }

  layout() {
    this.events.forEach((card, i) => this.slot(-1, i, card.target));
    this.decisions.forEach((card, i) => this.slot(1, i, card.target));
    const gateCard = this.gate && this.cards.get(this.gate.decision);
    if (gateCard) {
      const toward = gateCard.target.pos.clone().normalize().multiplyScalar(-0.09);
      gateCard.target.pos.add(toward);
      gateCard.target.scale = 1.08;
    }
  }

  /** Mark the open gate (or none): its card steps forward and its taint threads run hot. */
  setGate(gate) {
    this.gate = gate;
    this.hotKeys = new Set(
      (gate?.taints || []).map((w) => (w.source ? `${w.source}|${w.tag}` : `null|${w.action}|${w.path}`)),
    );
    this.layout();
  }

  setCited(ids) {
    this.cited = new Set(ids);
  }

  toggleFocus(card) {
    this.focus = this.focus === card ? null : card;
  }

  clearFocus() {
    this.focus = null;
  }

  gateCard() {
    return this.gate ? this.cards.get(this.gate.decision) : null;
  }

  /** Redraw every card whose content changed (verdicts, rivals, gate). */
  sync(state, origins) {
    for (const card of this.cards.values()) card.draw(this.view(card.record, state, origins));
  }

  view(record, state, origins) {
    const origin = origins[record.id];
    if (record.kind === 'event') {
      const goal = isGoal(record);
      const color = goal ? TAG_COLORS.GOAL : TOOL_COLOR;
      return {
        id: record.id,
        step: record.step,
        label: recordLabel(record, origin),
        snippet: recordSnippet(record),
        color,
        chips: [[goal ? 'GOAL' : 'TOOL', color, false]],
      };
    }
    const verdict = verdictOf(state, record.id);
    const rivals = state.rivals[record.id];
    const color = tagColor(record.tag);
    const chips = [[record.tag, color, false]];
    if (record.echo_of) chips.push(['ECHO', ECHO_COLOR, false]);
    if (rivals?.upset) chips.push(['upset', TAG_COLORS.UNSOURCED, false]);
    else if (rivals?.near_miss || rivals?.splits?.some((s) => s.near_miss)) chips.push(['~near', TAG_COLORS.CARRY, false]);
    if (verdict) chips.push([verdict, verdict === 'approved' ? APPROVE : DENY, true]);
    else if (record.watch?.length) chips.push(['⚠', TAG_COLORS.IN, false]);
    return {
      id: record.id,
      step: record.step,
      label: recordLabel(record, origin),
      snippet: recordSnippet(record),
      color,
      chips,
      struck: verdict === 'denied',
      dim: verdict === 'denied',
    };
  }

  update(dt, t) {
    const k = 1 - Math.exp(-dt * 7);
    const gateCard = this.gateCard();
    const linked = new Set();
    const hotSources = new Set();
    for (const thread of this.threads) {
      if (this.focus && (thread.from === this.focus || thread.to === this.focus)) {
        linked.add(thread.from);
        if (thread.to) linked.add(thread.to);
      }
      if (gateCard && thread.from === gateCard && this.hotKeys.has(thread.key) && thread.to) hotSources.add(thread.to);
    }
    if (this.focus) linked.add(this.focus);
    for (const card of this.cards.values()) {
      const g = card.group;
      g.position.lerp(card.target.pos, k);
      g.quaternion.slerp(card.target.quat, k);
      const s = g.scale.x + (card.target.scale - g.scale.x) * k;
      g.scale.setScalar(s);
      const dimmed = (this.focus && !linked.has(card)) || card.dim;
      const want = dimmed ? 0.4 : 1;
      card.opacity += (want - card.opacity) * (1 - Math.exp(-dt * 6));
      card.material.opacity = card.opacity;
      let glow = 0;
      let color = card.accent;
      if (card === gateCard) glow = 0.75 + 0.25 * Math.sin(t * 4);
      else if (hotSources.has(card)) glow = 0.5 + 0.3 * Math.sin(t * 4 + 1.3);
      else if (card === this.focus) glow = 0.7;
      else if (this.cited.has(card.record.id)) {
        glow = 0.45 + 0.15 * Math.sin(t * 2.4);
        color = TAG_COLORS.IN;
      } else if (card.hover) glow = 0.35;
      card.glowMat.color.set(color);
      card.glowMat.opacity += (glow - card.glowMat.opacity) * (1 - Math.exp(-dt * 10));
    }
    for (const thread of this.threads) {
      const hot = Boolean(gateCard && thread.from === gateCard && this.hotKeys.has(thread.key));
      let emphasis = 0;
      if (this.focus) emphasis = thread.from === this.focus || thread.to === this.focus ? 1 : -1;
      else if (gateCard && !hot) emphasis = -1;
      thread.update(dt, t, hot, emphasis);
    }
  }
}
