/** Canvas drawing for cards and panels, with the viewer's tag palette (SPEC 11.4). */

export const TAG_COLORS = {
  IN: '#d4a24c',
  CARRY: '#e9953a',
  GOAL: '#8fb3d9',
  SELF: '#a78bfa',
  UNSOURCED: '#ef5b5b',
  PROSE: '#9aa3af',
  LIT: '#4d5560',
  FINAL: '#cdd5e0',
  NOARGS: '#4d5560',
};

export const INK = '#e9ecf1';
export const MUTED = '#9aa2ae';
export const FAINT = '#646b77';
export const GOLD = '#d4a24c';
export const APPROVE = '#7cc79a';
export const DENY = '#ef5b5b';
export const SANS = '"Segoe UI", system-ui, Roboto, "Helvetica Neue", Arial, sans-serif';
export const MONO = 'ui-monospace, "Cascadia Mono", Consolas, "Roboto Mono", Menlo, monospace';

export function tagFamily(tag) {
  return String(tag || '').split('·')[0];
}

export function tagColor(tag) {
  return TAG_COLORS[tagFamily(tag)] || MUTED;
}

export function font(size, weight = 400, family = SANS) {
  return `${weight} ${size}px ${family}`;
}

export function rgba(hex, alpha) {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}

export function canvas(width, height) {
  const el = document.createElement('canvas');
  el.width = width;
  el.height = height;
  return el;
}

export function roundRect(ctx, x, y, w, h, r) {
  const radius = Math.min(r, w / 2, h / 2);
  ctx.beginPath();
  ctx.moveTo(x + radius, y);
  ctx.arcTo(x + w, y, x + w, y + h, radius);
  ctx.arcTo(x + w, y + h, x, y + h, radius);
  ctx.arcTo(x, y + h, x, y, radius);
  ctx.arcTo(x, y, x + w, y, radius);
  ctx.closePath();
}

/** Cut `text` to fit `max` pixels in the current font, ending in an ellipsis. */
export function fit(ctx, text, max) {
  const s = String(text ?? '');
  if (ctx.measureText(s).width <= max) return s;
  let lo = 0;
  let hi = s.length;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (ctx.measureText(s.slice(0, mid) + '…').width <= max) lo = mid;
    else hi = mid - 1;
  }
  return s.slice(0, lo).trimEnd() + '…';
}

/** Word-wrap `text` to lines of at most `max` pixels; long words break by character. */
export function wrap(ctx, text, max, maxLines = Infinity) {
  const lines = [];
  for (const para of String(text ?? '').split('\n')) {
    let line = '';
    for (const word of para.split(/\s+/).filter(Boolean)) {
      const next = line ? `${line} ${word}` : word;
      if (ctx.measureText(next).width <= max) {
        line = next;
        continue;
      }
      if (line) lines.push(line);
      line = word;
      while (ctx.measureText(line).width > max) {
        let cut = line.length - 1;
        while (cut > 1 && ctx.measureText(line.slice(0, cut)).width > max) cut--;
        lines.push(line.slice(0, cut));
        line = line.slice(cut);
      }
    }
    if (line) lines.push(line);
  }
  if (lines.length > maxLines) {
    const kept = lines.slice(0, maxLines);
    kept[maxLines - 1] = fit(ctx, `${kept[maxLines - 1]} ${lines[maxLines]}`, max);
    if (!kept[maxLines - 1].endsWith('…')) kept[maxLines - 1] = fit(ctx, kept[maxLines - 1] + ' …', max);
    return kept;
  }
  return lines;
}

/** A tag pill whose vertical centre is `cy`. Returns its width. `align: 'right'` ends it at x. */
export function chip(ctx, x, cy, text, color, { size = 28, align = 'left', solid = false } = {}) {
  ctx.font = font(size, 700, MONO);
  const padX = Math.round(size * 0.45);
  const h = Math.round(size * 1.35);
  const w = Math.ceil(ctx.measureText(text).width) + padX * 2;
  const left = align === 'right' ? x - w : x;
  roundRect(ctx, left, cy - h / 2, w, h, h / 2);
  ctx.fillStyle = solid ? color : rgba(color, 0.16);
  ctx.fill();
  ctx.lineWidth = 2;
  ctx.strokeStyle = rgba(color, solid ? 1 : 0.7);
  ctx.stroke();
  ctx.fillStyle = solid ? '#14161b' : color;
  ctx.textBaseline = 'middle';
  ctx.fillText(text, left + padX, cy + 1);
  return w;
}

/** A dark glass panel filling the canvas, with an optional coloured top edge. */
export function panel(ctx, w, h, { radius = 28, edge = null, alpha = 0.94 } = {}) {
  ctx.clearRect(0, 0, w, h);
  roundRect(ctx, 2, 2, w - 4, h - 4, radius);
  const grad = ctx.createLinearGradient(0, 0, 0, h);
  grad.addColorStop(0, `rgba(26, 28, 36, ${alpha})`);
  grad.addColorStop(1, `rgba(14, 15, 20, ${alpha})`);
  ctx.fillStyle = grad;
  ctx.fill();
  ctx.lineWidth = 2;
  ctx.strokeStyle = 'rgba(255, 255, 255, 0.12)';
  ctx.stroke();
  if (edge) {
    ctx.save();
    roundRect(ctx, 2, 2, w - 4, h - 4, radius);
    ctx.clip();
    const glow = ctx.createLinearGradient(0, 0, 0, 90);
    glow.addColorStop(0, rgba(edge, 0.22));
    glow.addColorStop(1, rgba(edge, 0));
    ctx.fillStyle = glow;
    ctx.fillRect(0, 0, w, 90);
    ctx.fillStyle = edge;
    ctx.fillRect(0, 0, w, 6);
    ctx.restore();
  }
}

/** Draw runs of [text, colour, font] left to right from x on baseline y; returns the end x. */
export function runs(ctx, x, y, parts) {
  ctx.textBaseline = 'alphabetic';
  for (const [text, color, f] of parts) {
    ctx.font = f;
    ctx.fillStyle = color;
    ctx.fillText(text, x, y);
    x += ctx.measureText(text).width;
  }
  return x;
}

let glowCanvas = null;

/** A soft white radial glow, shared by every sprite that glows. */
export function glowImage() {
  if (glowCanvas) return glowCanvas;
  glowCanvas = canvas(128, 128);
  const ctx = glowCanvas.getContext('2d');
  const g = ctx.createRadialGradient(64, 64, 0, 64, 64, 64);
  g.addColorStop(0, 'rgba(255,255,255,1)');
  g.addColorStop(0.25, 'rgba(255,255,255,0.55)');
  g.addColorStop(0.6, 'rgba(255,255,255,0.12)');
  g.addColorStop(1, 'rgba(255,255,255,0)');
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, 128, 128);
  return glowCanvas;
}
