/** Canvas-texture panels in front of the companion: the gate, the why board, its speech and the start row (SPEC 11.4). */

import * as THREE from 'three';
import { APPROVE, DENY, FAINT, GOLD, INK, MONO, MUTED, SANS, TAG_COLORS, canvas, fit, font, panel, rgba, roundRect, runs, tagColor, wrap } from './text.js';
import { flatten, rivalSplits, showValue, taintLines } from './state.js';

const TEXT_M = 0.0168;

function makeTexture(el) {
  const tex = new THREE.CanvasTexture(el);
  tex.colorSpace = THREE.SRGBColorSpace;
  tex.anisotropy = 4;
  return tex;
}

/**
 * A plane that shows a canvas of fixed width and a height that follows its content. The group's
 * origin is the plane's top centre, so the top edge stays put as the height changes.
 */
class Sheet {
  constructor(width, canvasWidth) {
    this.width = width;
    this.cw = canvasWidth;
    this.canvas = canvas(canvasWidth, 64);
    this.texture = makeTexture(this.canvas);
    this.material = new THREE.MeshBasicMaterial({ map: this.texture, transparent: true, alphaTest: 0.02, opacity: 1 });
    this.mesh = new THREE.Mesh(new THREE.PlaneGeometry(width, (width * 64) / canvasWidth), this.material);
    this.group = new THREE.Group();
    this.group.add(this.mesh);
    this.height = 0;
  }

  get pxPerM() {
    return this.cw / this.width;
  }

  /** Resize to `px` canvas pixels tall, returning the 2D context to draw into. */
  begin(px) {
    px = Math.max(32, Math.ceil(px));
    if (px !== this.canvas.height) {
      this.canvas.height = px;
      this.texture.dispose();
      this.texture = makeTexture(this.canvas);
      this.material.map = this.texture;
      this.mesh.geometry.dispose();
      this.mesh.geometry = new THREE.PlaneGeometry(this.width, (this.width * px) / this.cw);
    }
    this.height = (this.width * px) / this.cw;
    this.mesh.position.y = -this.height / 2;
    const ctx = this.canvas.getContext('2d');
    ctx.clearRect(0, 0, this.cw, px);
    return ctx;
  }

  end() {
    this.texture.needsUpdate = true;
  }

  dispose() {
    this.texture.dispose();
    this.material.dispose();
    this.mesh.geometry.dispose();
  }
}

/** Measure-then-draw: run `paint(ctx, measuring)` once on a scratch canvas to learn the height. */
function layout(sheet, paint) {
  const scratch = canvas(sheet.cw, 8).getContext('2d');
  const height = paint(scratch, true);
  const ctx = sheet.begin(height);
  paint(ctx, false);
  sheet.end();
}

export class Button {
  constructor({ label, width, height = 0.054, color = GOLD, solid = false, onSelect }) {
    this.width = width;
    this.height = height;
    this.cw = 512;
    this.ch = Math.round((512 * height) / width);
    this.canvas = canvas(this.cw, this.ch);
    this.texture = makeTexture(this.canvas);
    this.material = new THREE.MeshBasicMaterial({ map: this.texture, transparent: true, alphaTest: 0.02 });
    this.mesh = new THREE.Mesh(new THREE.PlaneGeometry(width, height), this.material);
    this.label = label;
    this.color = color;
    this.solid = solid;
    this.hover = false;
    this.enabled = true;
    this.press = 0;
    this.key = '';
    this.mesh.userData = {
      onSelect: () => {
        if (!this.enabled) return;
        this.press = 1;
        onSelect?.();
      },
      setHover: (on) => {
        this.hover = on;
        this.draw();
      },
      enabled: () => this.enabled,
    };
    this.draw();
  }

  set({ label = this.label, enabled = this.enabled, color = this.color, solid = this.solid } = {}) {
    Object.assign(this, { label, enabled, color, solid });
    this.draw();
  }

  draw() {
    const key = [this.label, this.enabled, this.hover, this.color, this.solid].join('|');
    if (key === this.key) return;
    this.key = key;
    const { cw, ch } = this;
    const ctx = this.canvas.getContext('2d');
    ctx.clearRect(0, 0, cw, ch);
    ctx.globalAlpha = this.enabled ? 1 : 0.38;
    roundRect(ctx, 3, 3, cw - 6, ch - 6, (ch - 6) / 2);
    if (this.solid) ctx.fillStyle = this.hover ? '#ffffff' : this.color;
    else ctx.fillStyle = this.hover ? rgba(this.color, 0.34) : 'rgba(20, 22, 28, 0.92)';
    ctx.fill();
    ctx.lineWidth = this.hover ? 6 : 4;
    ctx.strokeStyle = this.solid ? this.color : rgba(this.color, this.hover ? 1 : 0.75);
    ctx.stroke();
    const size = Math.min(Math.round(TEXT_M * (cw / this.width) * 1.05), Math.round(ch * 0.5));
    ctx.font = font(size, 700, SANS);
    ctx.fillStyle = this.solid ? '#15171c' : this.hover ? '#ffffff' : this.color;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(fit(ctx, this.label, cw - ch * 0.6), cw / 2, ch / 2 + 2);
    ctx.textAlign = 'left';
    ctx.globalAlpha = 1;
    this.texture.needsUpdate = true;
  }

  update(dt) {
    this.press = Math.max(0, this.press - dt * 5);
    this.mesh.scale.setScalar(1 - Math.sin(this.press * Math.PI) * 0.08);
  }

  dispose() {
    this.texture.dispose();
    this.material.dispose();
    this.mesh.geometry.dispose();
  }
}

/** Fade a group in and out by scaling a shared opacity over its materials. */
class Fader {
  constructor(group, materials) {
    this.group = group;
    this.materials = materials;
    this.value = 0;
    this.target = 0;
    group.visible = false;
  }

  set(on, instant = false) {
    this.target = on ? 1 : 0;
    if (instant) this.value = this.target;
  }

  update(dt) {
    this.value += (this.target - this.value) * (1 - Math.exp(-dt * 9));
    if (Math.abs(this.target - this.value) < 0.004) this.value = this.target;
    this.group.visible = this.value > 0.01;
    for (const m of this.materials()) m.opacity = this.value;
    return this.value;
  }
}

/** The gate panel: the call, one line per taint, rivals splits when they arrive, Approve and Deny. */
export class GatePanel {
  constructor({ onDecide }) {
    this.sheet = new Sheet(0.54, 1024);
    this.group = new THREE.Group();
    this.inner = new THREE.Group();
    this.group.add(this.inner);
    this.inner.add(this.sheet.group);
    this.approve = new Button({ label: 'Approve', width: 0.13, height: 0.052, color: APPROVE, solid: true, onSelect: () => onDecide('approve') });
    this.deny = new Button({ label: 'Deny', width: 0.11, height: 0.052, color: DENY, onSelect: () => onDecide('deny') });
    this.inner.add(this.approve.mesh, this.deny.mesh);
    this.gate = null;
    this.rivals = null;
    this.busy = false;
    this.fader = new Fader(this.group, () => [this.sheet.material, this.approve.material, this.deny.material]);
  }

  targets() {
    return [this.approve.mesh, this.deny.mesh];
  }

  show(gate, rivals, instant) {
    this.gate = gate;
    this.rivals = rivals || null;
    this.busy = false;
    this.draw();
    this.fader.set(true, instant);
  }

  hide(instant) {
    this.gate = null;
    this.fader.set(false, instant);
  }

  setRivals(rivals) {
    this.rivals = rivals;
    if (this.gate) this.draw();
  }

  setBusy(busy) {
    this.busy = busy;
    this.approve.set({ enabled: !busy });
    this.deny.set({ enabled: !busy });
  }

  draw() {
    const gate = this.gate;
    const W = this.sheet.cw;
    const pad = 40;
    const px = this.sheet.pxPerM;
    const buttonsLeft = W - pad - (0.13 + 0.11 + 0.016) * px;
    layout(this.sheet, (ctx, measuring) => {
      let y = 0;
      const H = ctx.canvas.height;
      if (!measuring) panel(ctx, W, H, { radius: 34, edge: TAG_COLORS.IN });
      y = 62;
      ctx.font = font(30, 700, SANS);
      ctx.fillStyle = TAG_COLORS.IN;
      ctx.textBaseline = 'alphabetic';
      if (!measuring) ctx.fillText(`⚠  ASKING FIRST · ${gate.decision}`, pad, y);
      y += 58;
      ctx.font = font(46, 700, MONO);
      ctx.fillStyle = INK;
      if (!measuring) ctx.fillText(fit(ctx, gate.call.name, buttonsLeft - pad - 16), pad, y);
      y += 22;
      const leaves = flatten(gate.call.args);
      const keyW = Math.min(170, Math.max(...leaves.map(([k]) => (ctx.font = font(30, 400, MONO), ctx.measureText(k).width)), 60) + 24);
      for (const [path, value] of leaves.slice(0, 5)) {
        y += 44;
        if (measuring) continue;
        ctx.font = font(30, 400, MONO);
        ctx.fillStyle = MUTED;
        ctx.fillText(fit(ctx, path, keyW - 16), pad, y);
        ctx.font = font(32, 400, SANS);
        ctx.fillStyle = INK;
        ctx.fillText(fit(ctx, showValue(value), W - pad * 2 - keyW), pad + keyW, y);
      }
      y += 30;
      if (!measuring) this.rule(ctx, pad, y, W);
      y += 54;
      const taints = taintLines(gate);
      if (!measuring) {
        ctx.font = font(28, 700, SANS);
        ctx.fillStyle = MUTED;
        ctx.fillText('WHERE THE VALUES CAME FROM', pad, y);
        ctx.font = font(28, 400, SANS);
        ctx.fillStyle = FAINT;
        ctx.textAlign = 'right';
        ctx.fillText(`${taints.length} not from the task`, W - pad, y);
        ctx.textAlign = 'left';
      }
      for (const taint of taints) {
        y += 48;
        if (!measuring) this.taintLine(ctx, taint, pad, y, W - pad * 2);
      }
      const splits = rivalSplits(this.rivals);
      if (this.rivals) {
        y += 34;
        if (!measuring) this.rule(ctx, pad, y, W);
        y += 54;
        if (!measuring) {
          ctx.font = font(28, 700, SANS);
          ctx.fillStyle = MUTED;
          ctx.fillText(`RIVALS · ${this.rivals.k + 1} SAMPLES`, pad, y);
          const flag = this.rivals.upset ? ['upset', TAG_COLORS.UNSOURCED] : this.rivals.near_miss ? ['near miss', TAG_COLORS.CARRY] : null;
          if (flag) {
            ctx.font = font(28, 700, SANS);
            ctx.fillStyle = flag[1];
            ctx.textAlign = 'right';
            ctx.fillText(flag[0], W - pad, y);
            ctx.textAlign = 'left';
          }
        }
        if (!splits.length) {
          y += 48;
          const total = this.rivals.k + 1;
          const agree = this.rivals.votes?.[this.rivals.chosen] || 0;
          const summary = agree === total
            ? `all ${total} samples agree`
            : `${agree} of ${total} samples chose this action; other arguments or calls differ`;
          ctx.font = font(32, 400, SANS);
          for (const [i, line] of wrap(ctx, summary, W - pad * 2, 3).entries()) {
            if (i) y += 42;
            if (!measuring) runs(ctx, pad, y, [[line, INK, font(32, 400, SANS)]]);
          }
        }
        for (const split of splits) {
          y += 48;
          if (!measuring) this.splitLine(ctx, split, pad, y, W - pad * 2);
        }
      }
      return y + 40;
    });
    const top = -(pad + 6) / px;
    this.approve.mesh.position.set(this.sheet.width / 2 - pad / px - 0.13 / 2, top - 0.052 / 2 - 0.004, 0.003);
    this.deny.mesh.position.set(this.approve.mesh.position.x - 0.13 / 2 - 0.016 - 0.11 / 2, this.approve.mesh.position.y, 0.003);
    this.setBusy(this.busy);
  }

  rule(ctx, pad, y, W) {
    ctx.fillStyle = 'rgba(255, 255, 255, 0.09)';
    ctx.fillRect(pad, y, W - pad * 2, 2);
  }

  taintLine(ctx, taint, x, y, width) {
    const color = tagColor(taint.tag);
    const tag = `[${taint.tag}]`;
    ctx.font = font(30, 700, MONO);
    const tagW = ctx.measureText(tag).width;
    ctx.fillStyle = color;
    ctx.textAlign = 'right';
    ctx.fillText(tag, x + width, y);
    ctx.textAlign = 'left';
    const room = width - tagW - 20;
    ctx.font = font(30, 400, MONO);
    const path = taint.path;
    const pathW = ctx.measureText(`${path} = `).width;
    ctx.font = font(30, 400, MONO);
    const label = fit(ctx, taint.label || 'nowhere in the run', room * 0.42);
    const labelW = ctx.measureText(label).width;
    ctx.font = font(32, 400, SANS);
    const arrowW = ctx.measureText('  ←  ').width;
    const value = fit(ctx, showValue(taint.value), Math.max(80, room - pathW - labelW - arrowW));
    runs(ctx, x, y, [
      [path, INK, font(30, 700, MONO)],
      [' = ', FAINT, font(30, 400, MONO)],
      [value, '#ffffff', font(32, 400, SANS)],
      ['  ←  ', color, font(32, 700, SANS)],
      [label, taint.source ? MUTED : TAG_COLORS.UNSOURCED, font(30, 400, MONO)],
    ]);
  }

  splitLine(ctx, split, x, y, width) {
    const head = `${split.path}: `;
    ctx.font = font(30, 700, MONO);
    const headW = ctx.measureText(head).width;
    const parts = split.parts;
    const lead = (p, i) => (i === 0 ? `${p.count} of ${split.n} samples: ` : `${p.count} of ${split.n}: `);
    ctx.font = font(32, 400, SANS);
    const fixed = parts.reduce((sum, p, i) => sum + ctx.measureText(lead(p, i)).width, 0) + ctx.measureText(' · ').width * (parts.length - 1);
    const each = Math.max(90, (width - headW - fixed) / parts.length);
    const out = [[head, INK, font(30, 700, MONO)]];
    parts.forEach((p, i) => {
      if (i) out.push([' · ', FAINT, font(32, 400, SANS)]);
      out.push([lead(p, i), MUTED, font(32, 400, SANS)]);
      ctx.font = font(32, 600, SANS);
      out.push([fit(ctx, p.value, each), p.value === split.chosen ? '#ffffff' : TAG_COLORS.CARRY, font(32, 600, SANS)]);
    });
    runs(ctx, x, y, out);
  }

  update(dt) {
    const v = this.fader.update(dt);
    this.inner.position.y = (1 - v) * -0.03;
    this.approve.update(dt);
    this.deny.update(dt);
  }
}

/** The why board: the latest answer, supported claims with cited ids, struck claims struck through. */
export class WhyBoard {
  constructor() {
    this.sheet = new Sheet(0.62, 1024);
    this.group = new THREE.Group();
    this.group.add(this.sheet.group);
    this.why = null;
    this.asking = null;
    this.fader = new Fader(this.group, () => [this.sheet.material]);
    this.pulse = 0;
  }

  show(why, asking, instant) {
    this.why = why;
    this.asking = asking;
    if (!why && !asking) {
      this.fader.set(false, instant);
      return;
    }
    this.draw();
    this.fader.set(true, instant);
  }

  draw() {
    const W = this.sheet.cw;
    const pad = 40;
    const body = font(30, 400, SANS);
    const why = this.asking ? null : this.why;
    const question = this.asking || this.why?.question || '';
    layout(this.sheet, (ctx, measuring) => {
      const H = ctx.canvas.height;
      if (!measuring) panel(ctx, W, H, { radius: 34, edge: GOLD });
      let y = 60;
      ctx.textBaseline = 'alphabetic';
      if (!measuring) {
        ctx.font = font(28, 700, SANS);
        ctx.fillStyle = GOLD;
        ctx.fillText('WHY', pad, y);
      }
      ctx.font = font(32, 600, SANS);
      const qLines = wrap(ctx, question, W - pad * 2 - 90, 2);
      qLines.forEach((line, i) => {
        if (!measuring) {
          ctx.font = font(32, 600, SANS);
          ctx.fillStyle = INK;
          ctx.fillText(line, pad + 90, y + i * 42);
        }
      });
      y += (qLines.length - 1) * 42;
      if (!why) {
        y += 56;
        if (!measuring) {
          ctx.font = font(30, 400, SANS);
          ctx.fillStyle = MUTED;
          ctx.fillText('Asking the model. Every claim it makes will be checked against the ledger…', pad, y);
        }
        return y + 40;
      }
      const result = why.result || {};
      y += 22;
      ctx.font = body;
      for (const line of wrap(ctx, result.answer || '', W - pad * 2, 4)) {
        y += 42;
        if (!measuring) {
          ctx.font = body;
          ctx.fillStyle = INK;
          ctx.fillText(line, pad, y);
        }
      }
      const supported = result.supported || [];
      const unproven = result.unproven || [];
      const struck = result.struck || [];
      if (supported.length) {
        y += 56;
        if (!measuring) this.section(ctx, `QUOTE FOUND · ${supported.length} of ${supported.length + unproven.length + struck.length}`, APPROVE, pad, y);
        for (const claim of supported) y = this.claim(ctx, measuring, claim, null, pad, y, W);
      }
      if (unproven.length) {
        y += 56;
        if (!measuring) this.section(ctx, 'UNPROVEN · quote found, not proof', GOLD, pad, y);
        for (const item of unproven) y = this.claim(ctx, measuring, item.claim || {}, item.reasons || [], pad, y, W, true);
      }
      if (struck.length) {
        y += 56;
        if (!measuring) this.section(ctx, `STRUCK · the verifier could not back these`, DENY, pad, y);
        for (const item of struck) y = this.claim(ctx, measuring, item.claim || {}, item.reasons || [], pad, y, W);
      }
      y += 54;
      if (!measuring) {
        ctx.font = font(28, 400, SANS);
        ctx.fillStyle = FAINT;
        ctx.fillText(`answered by ${result.model || 'the model'}`, pad, y);
      }
      return y + 34;
    });
  }

  section(ctx, text, color, x, y) {
    ctx.font = font(28, 700, SANS);
    ctx.fillStyle = color;
    ctx.fillText(text, x, y);
  }

  claim(ctx, measuring, claim, reasons, pad, y, W, unproven = false) {
    const struck = Boolean(reasons) && !unproven;
    const mark = unproven ? '?' : struck ? '✕' : '✓';
    const color = unproven ? GOLD : struck ? DENY : APPROVE;
    const ids = (claim.cites || []).map((c) => c.id);
    const indent = 44;
    ctx.font = font(30, 400, SANS);
    const lines = wrap(ctx, claim.text || '', W - pad * 2 - indent, 3);
    ctx.font = font(28, 700, MONO);
    const idsText = ids.join(' · ');
    const idsW = ctx.measureText(idsText).width + 20;
    ctx.font = font(30, 400, SANS);
    const lastFits = lines.length && ctx.measureText(lines[lines.length - 1]).width + idsW < W - pad * 2 - indent;
    lines.forEach((line, i) => {
      y += 42;
      if (measuring) return;
      if (i === 0) {
        ctx.font = font(30, 700, SANS);
        ctx.fillStyle = color;
        ctx.fillText(mark, pad + 4, y);
      }
      ctx.font = font(30, 400, SANS);
      ctx.fillStyle = struck ? MUTED : INK;
      ctx.fillText(line, pad + indent, y);
      if (struck) {
        const w = ctx.measureText(line).width;
        ctx.fillStyle = rgba(DENY, 0.85);
        ctx.fillRect(pad + indent - 2, y - 11, w + 4, 3);
      }
    });
    if (ids.length) {
      let x = pad + indent;
      if (lastFits) {
        ctx.font = font(30, 400, SANS);
        x += ctx.measureText(lines[lines.length - 1]).width + 18;
      } else {
        y += 40;
      }
      if (!measuring) {
        ctx.font = font(28, 700, MONO);
        ctx.fillStyle = struck ? rgba(DENY, 0.8) : GOLD;
        ctx.fillText(idsText, x, y);
      }
    }
    for (const reason of reasons || []) {
      y += 38;
      if (!measuring) {
        ctx.font = font(28, 400, SANS);
        ctx.fillStyle = rgba(color, 0.9);
        ctx.fillText(fit(ctx, reason, W - pad * 2 - indent), pad + indent, y);
      }
    }
    return y;
  }

  update(dt, t) {
    const v = this.fader.update(dt);
    if (this.asking && v > 0) this.sheet.material.opacity = v * (0.75 + 0.25 * Math.sin(t * 4));
  }
}

/** A speech bubble beside the mask: what the agent said at its latest step. */
export class Speech {
  constructor() {
    this.sheet = new Sheet(0.24, 512);
    this.group = new THREE.Group();
    this.group.add(this.sheet.group);
    this.fader = new Fader(this.group, () => [this.sheet.material]);
    this.text = '';
  }

  say(text, instant) {
    const clean = String(text || '').replace(/\s+/g, ' ').trim();
    if (!clean) {
      this.fader.set(false, instant);
      return;
    }
    if (clean !== this.text) {
      this.text = clean;
      const W = this.sheet.cw;
      layout(this.sheet, (ctx, measuring) => {
        ctx.font = font(36, 400, SANS);
        const lines = wrap(ctx, clean, W - 60 - 28, 4);
        const H = 34 + lines.length * 46 + 26;
        if (!measuring) {
          ctx.clearRect(0, 0, W, ctx.canvas.height);
          roundRect(ctx, 30, 3, W - 33, H - 6, 30);
          ctx.fillStyle = 'rgba(246, 241, 233, 0.97)';
          ctx.fill();
          ctx.beginPath();
          ctx.moveTo(32, 30);
          ctx.lineTo(4, 50);
          ctx.lineTo(32, 64);
          ctx.closePath();
          ctx.fill();
          ctx.font = font(36, 400, SANS);
          ctx.fillStyle = '#2a2131';
          ctx.textBaseline = 'alphabetic';
          lines.forEach((line, i) => ctx.fillText(line, 58, 34 + 34 + i * 46));
        }
        return H;
      });
    }
    this.fader.set(true, instant);
  }

  hide(instant) {
    this.fader.set(false, instant);
  }

  update(dt) {
    this.fader.update(dt);
  }
}

/** A line of small text in the scene, for the connection and run state where no HTML is shown. */
export class Caption {
  constructor(width = 0.34) {
    this.sheet = new Sheet(width, 640);
    this.group = this.sheet.group;
    this.text = null;
  }

  set(parts) {
    const key = JSON.stringify(parts);
    if (key === this.text) return;
    this.text = key;
    layout(this.sheet, (ctx, measuring) => {
      if (measuring) return 48;
      ctx.font = font(28, 400, SANS);
      const total = parts.reduce((w, [text]) => w + ctx.measureText(text).width, 0);
      runs(ctx, (this.sheet.cw - total) / 2, 36, parts.map(([text, color]) => [text, color, font(28, 400, SANS)]));
      return 48;
    });
  }
}

export { MUTED, FAINT, INK };
