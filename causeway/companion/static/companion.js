/** The companion (SPEC 11.4): a small demon behind a cute mask. The mask is what the agent says; it lifts on a gate. */

import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { canvas, glowImage } from './text.js';

const BODY_R = 0.085;
const BODY_SCALE = new THREE.Vector3(1, 1.1, 0.92);
const FACE_TILT = 0.3;
const MASK_R = 0.054;
const MASK_DOME = 0.07;
const LIFT = -2.05;
const LIFT_TIME = 0.6;
const LIFT_DELAY = 0.12;
const SETTLE_TIME = 0.55;

const COLORS = {
  body: '#2d2438',
  horn: '#9a3653',
  claw: '#f1e7da',
  maskInner: '#cbbfb2',
  maskRim: '#e9dfd2',
  aura: '#6a4fe0',
  ember: '#ff7a2e',
};

function easeOutBack(t) {
  const c1 = 1.25;
  const c3 = c1 + 1;
  return 1 + c3 * (t - 1) ** 3 + c1 * (t - 1) ** 2;
}

/** Mask openness while settling: falls shut with a quick ease-in, then one small bounce. */
function settleCurve(p) {
  const land = 0.72;
  if (p < land) return 1 - (p / land) ** 3;
  return 0.07 * Math.sin((Math.PI * (p - land)) / (1 - land));
}

function texture(el) {
  const tex = new THREE.CanvasTexture(el);
  tex.colorSpace = THREE.SRGBColorSpace;
  tex.anisotropy = 4;
  return tex;
}

/** A spherical cap pointing along +Z, with planar UVs across its footprint. */
function capGeometry(radius, angle) {
  const geo = new THREE.SphereGeometry(radius, 48, 20, 0, Math.PI * 2, 0, angle);
  geo.rotateX(Math.PI / 2);
  const span = radius * Math.sin(angle) * 2;
  const pos = geo.attributes.position;
  const uv = geo.attributes.uv;
  for (let i = 0; i < pos.count; i++) uv.setXY(i, pos.getX(i) / span + 0.5, pos.getY(i) / span + 0.5);
  return geo;
}

/** A tube along `points` that narrows from `radius` to a point. */
function taperedTube(points, radius, segments = 18) {
  const curve = new THREE.CatmullRomCurve3(points);
  const geo = new THREE.TubeGeometry(curve, segments, radius, 10, false);
  const pos = geo.attributes.position;
  const ring = 11;
  const centre = new THREE.Vector3();
  const v = new THREE.Vector3();
  for (let i = 0; i <= segments; i++) {
    curve.getPointAt(i / segments, centre);
    const taper = Math.max(0.04, (1 - i / segments) ** 0.85);
    for (let j = 0; j < ring; j++) {
      const k = i * ring + j;
      v.fromBufferAttribute(pos, k).sub(centre).multiplyScalar(taper).add(centre);
      pos.setXYZ(k, v.x, v.y, v.z);
    }
  }
  geo.computeVertexNormals();
  return geo;
}

function maskFace() {
  const el = canvas(512, 512);
  const ctx = el.getContext('2d');
  const g = ctx.createRadialGradient(230, 190, 20, 256, 256, 256);
  g.addColorStop(0, '#fffbf5');
  g.addColorStop(0.6, '#f4ece1');
  g.addColorStop(0.92, '#e3d7c8');
  g.addColorStop(1, '#d6c8b7');
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, 512, 512);
  ctx.save();
  ctx.filter = 'blur(10px)';
  ctx.fillStyle = 'rgba(244, 130, 150, 0.5)';
  for (const x of [148, 364]) {
    ctx.beginPath();
    ctx.ellipse(x, 318, 46, 24, 0, 0, Math.PI * 2);
    ctx.fill();
  }
  ctx.restore();
  for (const x of [182, 330]) {
    ctx.fillStyle = '#2a2131';
    ctx.beginPath();
    ctx.ellipse(x, 252, 36, 46, 0, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = '#ffffff';
    ctx.beginPath();
    ctx.arc(x + 11, 234, 13, 0, Math.PI * 2);
    ctx.fill();
    ctx.beginPath();
    ctx.arc(x - 12, 272, 6, 0, Math.PI * 2);
    ctx.fill();
  }
  ctx.strokeStyle = '#2a2131';
  ctx.lineWidth = 8;
  ctx.lineCap = 'round';
  ctx.beginPath();
  ctx.arc(242, 318, 14, 0.15 * Math.PI, 0.95 * Math.PI);
  ctx.moveTo(284, 318);
  ctx.arc(270, 318, 14, 0.05 * Math.PI, 0.85 * Math.PI);
  ctx.stroke();
  ctx.strokeStyle = 'rgba(42, 33, 49, 0.55)';
  ctx.lineWidth = 6;
  for (const [x, dir] of [[182, -1], [330, 1]]) {
    ctx.beginPath();
    ctx.arc(x - dir * 4, 196, 30, 1.2 * Math.PI + (dir > 0 ? 0.1 : -0.05), 1.75 * Math.PI + (dir > 0 ? 0.1 : -0.05));
    ctx.stroke();
  }
  return texture(el);
}

function demonFace() {
  const el = canvas(256, 256);
  const ctx = el.getContext('2d');
  const g = ctx.createRadialGradient(128, 128, 10, 128, 128, 128);
  g.addColorStop(0, '#0d0710');
  g.addColorStop(0.7, '#150c1a');
  g.addColorStop(1, COLORS.body);
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, 256, 256);
  ctx.save();
  ctx.shadowColor = '#ff4a1a';
  ctx.shadowBlur = 16;
  const grin = ctx.createLinearGradient(0, 160, 0, 205);
  grin.addColorStop(0, '#ffb35c');
  grin.addColorStop(1, '#d2341c');
  ctx.fillStyle = grin;
  ctx.beginPath();
  ctx.moveTo(72, 160);
  ctx.quadraticCurveTo(128, 176, 184, 160);
  ctx.quadraticCurveTo(170, 214, 128, 214);
  ctx.quadraticCurveTo(86, 214, 72, 160);
  ctx.fill();
  ctx.restore();
  ctx.fillStyle = '#fff1e2';
  for (const [x, w, h] of [[88, 12, 18], [106, 9, 11], [121, 9, 10], [136, 9, 10], [151, 9, 11], [168, 12, 18]]) {
    const top = 160 + 16 * (1 - ((x - 128) / 56) ** 2) - 3;
    ctx.beginPath();
    ctx.moveTo(x - w / 2, top);
    ctx.lineTo(x + w / 2, top);
    ctx.lineTo(x, top + h);
    ctx.closePath();
    ctx.fill();
  }
  return texture(el);
}

function demonEye() {
  const el = canvas(128, 96);
  const ctx = el.getContext('2d');
  ctx.save();
  ctx.shadowColor = '#ff5a1a';
  ctx.shadowBlur = 18;
  const g = ctx.createRadialGradient(62, 50, 2, 64, 48, 46);
  g.addColorStop(0, '#fffbe6');
  g.addColorStop(0.35, '#ffd36b');
  g.addColorStop(0.75, '#ff8a2a');
  g.addColorStop(1, '#e2401a');
  ctx.fillStyle = g;
  ctx.beginPath();
  ctx.moveTo(18, 58);
  ctx.quadraticCurveTo(52, 6, 112, 30);
  ctx.quadraticCurveTo(84, 82, 18, 58);
  ctx.fill();
  ctx.restore();
  ctx.fillStyle = '#3b0c05';
  ctx.beginPath();
  ctx.ellipse(66, 46, 5, 17, 0.35, 0, Math.PI * 2);
  ctx.fill();
  return texture(el);
}

export class Companion {
  constructor() {
    this.group = new THREE.Group();
    this.rig = new THREE.Group();
    this.group.add(this.rig);
    this.glowTex = new THREE.CanvasTexture(glowImage());
    this.lifted = false;
    this.amount = 0;
    this.anim = null;
    this.glow = 0;
    this.mood = 'idle';
    this.moodUntil = 0;
    this.nod = 0;
    this.squash = 0;
    this.blinkAt = 2;
    this.blink = 0;
    this.lookYaw = 0;
    this.lookTarget = null;
    this.now = 0;
    this.buildProcedural();
  }

  buildProcedural() {
    const body = new THREE.MeshStandardMaterial({ color: COLORS.body, roughness: 0.55, metalness: 0.0 });
    const horn = new THREE.MeshStandardMaterial({ color: COLORS.horn, roughness: 0.42 });
    const claw = new THREE.MeshStandardMaterial({ color: COLORS.claw, roughness: 0.4 });
    this.procedural = new THREE.Group();
    this.rig.add(this.procedural);

    this.body = new THREE.Mesh(new THREE.SphereGeometry(BODY_R, 48, 32), body);
    this.body.scale.copy(BODY_SCALE);
    this.procedural.add(this.body);

    const faceGeo = capGeometry(BODY_R * 1.012, 0.62);
    faceGeo.rotateX(-FACE_TILT);
    const face = new THREE.Mesh(faceGeo, new THREE.MeshBasicMaterial({ map: demonFace() }));
    face.scale.copy(BODY_SCALE);
    this.procedural.add(face);

    const dir = new THREE.Vector3(0, Math.sin(FACE_TILT), Math.cos(FACE_TILT)).multiplyScalar(BODY_R * 1.012).multiply(BODY_SCALE);
    const normalTilt = Math.atan2(dir.y / BODY_SCALE.y ** 2, dir.z / BODY_SCALE.z ** 2);
    this.face = new THREE.Group();
    this.face.position.copy(dir);
    this.face.rotation.x = -normalTilt;
    this.procedural.add(this.face);

    const eyeTex = demonEye();
    const eyeMat = new THREE.MeshBasicMaterial({ map: eyeTex, transparent: true, depthWrite: false, side: THREE.DoubleSide });
    this.eyeMat = eyeMat;
    this.eyes = [];
    for (const side of [-1, 1]) {
      const eye = new THREE.Mesh(new THREE.PlaneGeometry(0.028, 0.021), eyeMat);
      eye.position.set(side * 0.021, 0.006, 0.0012);
      eye.rotation.set(0, side * 0.28, side * -0.12);
      eye.scale.x = -side;
      this.face.add(eye);
      const halo = this.sprite(COLORS.ember, 0.05);
      halo.position.set(side * 0.021, 0.006, 0.004);
      this.face.add(halo);
      this.eyes.push({ eye, halo });
    }
    this.leak = this.sprite(COLORS.ember, 0.17);
    this.leak.position.set(0, 0, -0.003);
    this.face.add(this.leak);

    this.hinge = new THREE.Group();
    this.hinge.position.set(0, MASK_R, 0.003);
    this.face.add(this.hinge);
    const angle = Math.asin(MASK_R / MASK_DOME);
    const domeGeo = capGeometry(MASK_DOME, angle);
    domeGeo.translate(0, -MASK_R, -MASK_DOME * Math.cos(angle));
    const maskFront = new THREE.Mesh(domeGeo, new THREE.MeshStandardMaterial({ map: maskFace(), roughness: 0.36 }));
    const maskBack = new THREE.Mesh(domeGeo, new THREE.MeshStandardMaterial({ color: COLORS.maskInner, roughness: 0.75, side: THREE.BackSide }));
    const rim = new THREE.Mesh(new THREE.TorusGeometry(MASK_R, 0.0026, 8, 56), new THREE.MeshStandardMaterial({ color: COLORS.maskRim, roughness: 0.5 }));
    rim.position.y = -MASK_R;
    this.hinge.add(maskFront, maskBack, rim);
    this.maskNode = this.hinge;
    this.maskBase = this.hinge.quaternion.clone();

    const hornGeo = taperedTube(
      [new THREE.Vector3(0, -0.006, 0), new THREE.Vector3(0.008, 0.016, 0), new THREE.Vector3(0.02, 0.034, 0), new THREE.Vector3(0.036, 0.044, 0), new THREE.Vector3(0.05, 0.043, 0)],
      0.0125,
    );
    for (const side of [-1, 1]) {
      const mount = new THREE.Group();
      mount.position.set(side * 0.046, 0.074, -0.008);
      mount.rotation.set(-0.35, 0, side * -0.1);
      const mesh = new THREE.Mesh(hornGeo, horn);
      if (side < 0) mesh.rotation.y = Math.PI;
      mount.add(mesh);
      this.procedural.add(mount);
    }

    const armGeo = new THREE.CapsuleGeometry(0.0125, 0.03, 4, 12);
    const clawGeo = new THREE.ConeGeometry(0.0042, 0.016, 8);
    this.arms = [];
    for (const side of [-1, 1]) {
      const shoulder = new THREE.Group();
      shoulder.position.set(side * 0.074, -0.018, 0.018);
      const arm = new THREE.Mesh(armGeo, body);
      arm.position.y = -0.026;
      shoulder.add(arm);
      for (const [dx, dz, tilt] of [[-0.006, 0.004, 0.25], [0, 0.007, 0], [0.006, 0.004, -0.25]]) {
        const c = new THREE.Mesh(clawGeo, claw);
        c.position.set(dx, -0.054, dz);
        c.rotation.set(Math.PI + 0.35, 0, tilt);
        shoulder.add(c);
      }
      this.procedural.add(shoulder);
      this.arms.push({ shoulder, side });
    }

    const tailGeo = taperedTube(
      [new THREE.Vector3(0, 0, 0), new THREE.Vector3(0.03, -0.02, -0.012), new THREE.Vector3(0.065, -0.012, -0.01), new THREE.Vector3(0.085, 0.018, -0.004)],
      0.007,
    );
    this.tail = new THREE.Group();
    this.tail.position.set(0.02, -0.07, -0.05);
    this.tail.add(new THREE.Mesh(tailGeo, body));
    const spade = new THREE.Mesh(new THREE.OctahedronGeometry(0.012, 0), horn);
    spade.scale.set(0.9, 1.3, 0.35);
    spade.position.set(0.087, 0.028, -0.004);
    spade.rotation.z = -0.5;
    this.tail.add(spade);
    this.procedural.add(this.tail);

    this.aura = this.sprite(COLORS.aura, 0.42);
    this.aura.material.opacity = 0.2;
    this.aura.position.set(0, 0.005, -0.06);
    this.rig.add(this.aura);

    this.ember = new THREE.PointLight(COLORS.ember, 0, 0.45, 2);
    this.ember.position.set(0, 0.02, 0.05);
    this.face.add(this.ember);
  }

  sprite(color, size) {
    const mat = new THREE.SpriteMaterial({
      map: this.glowTex,
      color,
      transparent: true,
      opacity: 0,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
    });
    const s = new THREE.Sprite(mat);
    s.scale.setScalar(size);
    return s;
  }

  /** Swap in static/companion.glb when the server has one. Any 404 or failure keeps the stand-in. */
  async loadModel(url) {
    let res;
    try {
      res = await fetch(url, { cache: 'no-cache' });
    } catch {
      return false;
    }
    if (!res.ok) return false;
    try {
      const gltf = await new GLTFLoader().parseAsync(await res.arrayBuffer(), '');
      const model = gltf.scene;
      const bodyNode = model.getObjectByName('Body');
      const maskNode = model.getObjectByName('Mask');
      if (!bodyNode || !maskNode) {
        console.warn('companion.glb has no Body and Mask nodes; keeping the stand-in');
        return false;
      }
      const box = new THREE.Box3().setFromObject(model);
      const size = box.getSize(new THREE.Vector3());
      if (size.y < 0.05 || size.y > 1) model.scale.setScalar(0.24 / size.y);
      box.setFromObject(model);
      model.position.sub(box.getCenter(new THREE.Vector3()));
      this.procedural.visible = false;
      this.rig.add(model);
      this.maskNode = maskNode;
      this.maskBase = maskNode.quaternion.clone();
      this.eyes = [];
      this.leak = null;
      this.arms = [];
      this.tail = null;
      model.updateMatrixWorld(true);
      const at = maskNode.getWorldPosition(new THREE.Vector3());
      this.rig.worldToLocal(at);
      this.ember.removeFromParent();
      this.ember.position.copy(at).add(new THREE.Vector3(0, -0.02, 0.05));
      this.rig.add(this.ember);
      return true;
    } catch (err) {
      console.warn('companion.glb could not be loaded; keeping the stand-in', err);
      return false;
    }
  }

  /** Lift the mask (a gate is open) or let it settle. `instant` skips the animation. */
  setLifted(on, instant = false) {
    if (on === this.lifted && !instant) return;
    this.lifted = on;
    if (instant) {
      this.anim = null;
      this.amount = on ? 1 : 0;
      this.glow = on ? 1 : 0;
      return;
    }
    this.anim = { from: this.amount, to: on ? 1 : 0, start: this.now, landed: false };
  }

  /** A short reaction: 'speak' nods, 'approve' hops, 'deny' droops. */
  react(kind) {
    if (kind === 'speak') this.nod = 1;
    if (kind === 'approve' || kind === 'deny') {
      this.mood = kind;
      this.moodUntil = this.now + (kind === 'approve' ? 1.6 : 2.6);
    }
  }

  /** Turn a little toward a point given in the companion's parent space, or face forward with null. */
  lookAt(point) {
    this.lookTarget = point ? point.clone() : null;
  }

  reset() {
    this.setLifted(false, true);
    this.mood = 'idle';
    this.lookTarget = null;
  }

  update(dt, t) {
    this.now = t;
    if (this.anim) {
      const a = this.anim;
      const elapsed = t - a.start;
      if (a.to > a.from) {
        const p = Math.min(1, Math.max(0, (elapsed - LIFT_DELAY) / LIFT_TIME));
        this.amount = a.from + (a.to - a.from) * easeOutBack(p);
        if (p >= 1) this.anim = null;
      } else {
        const p = Math.min(1, elapsed / SETTLE_TIME);
        this.amount = a.from * settleCurve(p);
        if (p >= 0.72 && !a.landed) {
          a.landed = true;
          this.squash = 1;
        }
        if (p >= 1) {
          this.amount = 0;
          this.anim = null;
        }
      }
    }
    const lifting = this.anim && this.anim.to > this.anim.from;
    const shiver = lifting && t - this.anim.start < LIFT_DELAY + 0.1 ? Math.sin(t * 95) * 0.0035 : 0;

    const glowTarget = this.lifted ? 1 : 0;
    this.glow += (glowTarget - this.glow) * (1 - Math.exp(-dt * (this.lifted ? 7 : 4)));
    if (this.mood !== 'idle' && t > this.moodUntil) this.mood = 'idle';

    const hop = this.mood === 'approve' ? Math.max(0, Math.sin((this.moodUntil - t) * 7)) * 0.018 * Math.min(1, this.moodUntil - t) : 0;
    const droop = this.mood === 'deny' ? 1 : 0;
    this.nod = Math.max(0, this.nod - dt * 2.5);
    this.squash = Math.max(0, this.squash - dt * 4);

    this.rig.position.set(shiver, Math.sin(t * 1.7) * 0.007 + hop - droop * 0.008, 0);
    let yaw = Math.sin(t * 0.6) * 0.07;
    if (this.lookTarget) {
      const local = this.lookTarget.clone().sub(this.group.position);
      yaw += Math.max(-0.4, Math.min(0.4, Math.atan2(local.x, local.z + 0.6)));
    }
    this.lookYaw += (yaw - this.lookYaw) * (1 - Math.exp(-dt * 3));
    this.rig.rotation.set(Math.sin(this.nod * Math.PI) * 0.12 + droop * 0.1, this.lookYaw, Math.sin(t * 1.1) * 0.03);
    const s = 1 - Math.sin(this.squash * Math.PI) * 0.05;
    this.rig.scale.set(1 + (1 - s) * 0.6, s, 1 + (1 - s) * 0.6);

    if (this.maskNode) {
      const q = new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(1, 0, 0), LIFT * this.amount);
      this.maskNode.quaternion.copy(this.maskBase).multiply(q);
    }

    if (t > this.blinkAt) {
      this.blink = 1;
      this.blinkAt = t + 2.5 + Math.random() * 3.5;
    }
    this.blink = Math.max(0, this.blink - dt * 7);
    const open = 1 - Math.sin(this.blink * Math.PI) * 0.9;
    const g = this.glow;
    for (const { eye, halo } of this.eyes) {
      eye.scale.y = open;
      halo.material.opacity = (0.15 + 0.85 * g) * (0.85 + 0.15 * Math.sin(t * 5.3)) * open;
      halo.scale.setScalar(0.04 + 0.03 * g);
    }
    if (this.eyeMat) this.eyeMat.color.setScalar(0.35 + 0.65 * g);
    if (this.leak) {
      this.leak.material.opacity = (0.14 + 0.06 * Math.sin(t * 2.2)) * (1 - g) + 0.32 * g;
    }
    this.ember.intensity = 0.02 + 0.09 * g * (0.85 + 0.15 * Math.sin(t * 7));
    this.aura.material.opacity = 0.16 + 0.05 * Math.sin(t * 1.3) + 0.08 * g;
    this.aura.material.color.set(g > 0.5 ? '#8a3a6a' : COLORS.aura);

    for (const { shoulder, side } of this.arms) {
      let lift = 0.42 + Math.sin(t * 2 + side) * 0.06;
      if (this.lifted) lift = 1.25 + Math.sin(t * 13 + side) * 0.05;
      if (this.mood === 'approve') lift = 2.1 + Math.sin(t * 16 + side * 1.5) * 0.25;
      if (this.mood === 'deny') lift = 0.18;
      shoulder.rotation.z += (side * lift - shoulder.rotation.z) * (1 - Math.exp(-dt * 9));
      shoulder.rotation.x = this.lifted ? -0.5 : -0.15;
    }
    if (this.tail) this.tail.rotation.y = Math.sin(t * 2.1) * 0.35;
  }
}
