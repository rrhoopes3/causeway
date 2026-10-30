/** Pointing at things: XR controller and hand rays on `select`, mouse look and click on the desktop (SPEC 11.4). */

import * as THREE from 'three';

const RAY_LENGTH = 1.6;

function shown(object) {
  for (let o = object; o; o = o.parent) if (!o.visible) return false;
  const enabled = object.userData.enabled;
  return typeof enabled === 'function' ? enabled() : enabled !== false;
}

export class Input {
  /**
   * `targets()` lists the meshes that can be selected; each has userData.onSelect and may have
   * userData.setHover. `onMiss()` runs when a select hits nothing.
   */
  constructor({ renderer, scene, camera, targets, onMiss }) {
    this.renderer = renderer;
    this.camera = camera;
    this.targets = targets;
    this.onMiss = onMiss;
    this.raycaster = new THREE.Raycaster();
    this.hovered = new Map();
    this.yaw = 0;
    this.pitch = 0;
    this.drag = null;
    this.pointer = null;
    this.controllers = [];

    const lineGeo = new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(0, 0, 0), new THREE.Vector3(0, 0, -1)]);
    const lineMat = new THREE.LineBasicMaterial({ color: 0xf3e3c3, transparent: true, opacity: 0.75 });
    const dotGeo = new THREE.SphereGeometry(0.006, 12, 8);
    const dotMat = new THREE.MeshBasicMaterial({ color: 0xffffff });
    for (let i = 0; i < 2; i++) {
      const controller = renderer.xr.getController(i);
      const line = new THREE.Line(lineGeo, lineMat);
      line.scale.z = RAY_LENGTH;
      const dot = new THREE.Mesh(dotGeo, dotMat);
      dot.visible = false;
      controller.add(line, dot);
      controller.visible = false;
      controller.addEventListener('connected', (e) => {
        controller.userData.source = e.data;
        controller.visible = e.data.targetRayMode !== 'gaze';
      });
      controller.addEventListener('disconnected', () => {
        controller.visible = false;
        this.setHover(controller, null);
      });
      controller.addEventListener('select', () => this.select(this.hitFromController(controller)));
      scene.add(controller);
      this.controllers.push({ controller, line, dot });
    }

    const el = renderer.domElement;
    el.addEventListener('pointerdown', (e) => {
      if (renderer.xr.isPresenting) return;
      this.drag = { x: e.clientX, y: e.clientY, yaw: this.yaw, pitch: this.pitch, moved: false };
      el.setPointerCapture(e.pointerId);
    });
    el.addEventListener('pointermove', (e) => {
      this.pointer = this.ndc(e);
      if (!this.drag) return;
      const dx = e.clientX - this.drag.x;
      const dy = e.clientY - this.drag.y;
      if (Math.hypot(dx, dy) > 4) this.drag.moved = true;
      if (!this.drag.moved) return;
      const perPx = THREE.MathUtils.degToRad(camera.fov) / el.clientHeight;
      this.yaw = this.drag.yaw + dx * perPx;
      this.pitch = THREE.MathUtils.clamp(this.drag.pitch + dy * perPx, -1.2, 1.2);
    });
    el.addEventListener('pointerup', (e) => {
      const drag = this.drag;
      this.drag = null;
      if (el.hasPointerCapture(e.pointerId)) el.releasePointerCapture(e.pointerId);
      if (drag && !drag.moved) this.select(this.hitFromPointer(this.ndc(e)));
    });
    el.addEventListener('pointerleave', () => {
      this.pointer = null;
    });
    el.addEventListener('wheel', (e) => {
      if (renderer.xr.isPresenting) return;
      e.preventDefault();
      camera.fov = THREE.MathUtils.clamp(camera.fov + Math.sign(e.deltaY) * 3, 35, 85);
      camera.updateProjectionMatrix();
    }, { passive: false });
  }

  ndc(e) {
    const rect = this.renderer.domElement.getBoundingClientRect();
    return new THREE.Vector2(((e.clientX - rect.left) / rect.width) * 2 - 1, -((e.clientY - rect.top) / rect.height) * 2 + 1);
  }

  /** Look direction for the desktop camera, from mouse drag. */
  setLook(yaw, pitch) {
    this.yaw = yaw;
    this.pitch = pitch;
  }

  hit() {
    const meshes = this.targets().filter(shown);
    const hits = this.raycaster.intersectObjects(meshes, false);
    return hits.find((h) => h.object.material.opacity === undefined || h.object.material.opacity > 0.2) || null;
  }

  hitFromController(controller) {
    controller.updateMatrixWorld(true);
    this.raycaster.setFromXRController(controller);
    return this.hit();
  }

  hitFromPointer(ndc) {
    this.raycaster.setFromCamera(ndc, this.camera);
    return this.hit();
  }

  select(hit) {
    if (hit) hit.object.userData.onSelect?.();
    else this.onMiss?.();
  }

  setHover(source, object) {
    const before = this.hovered.get(source) || null;
    if (before === object) return;
    this.hovered.set(source, object);
    const still = (o) => [...this.hovered.values()].includes(o);
    if (before && !still(before)) before.userData.setHover?.(false);
    if (object) object.userData.setHover?.(true);
  }

  update() {
    const xr = this.renderer.xr.isPresenting;
    if (!xr) {
      this.camera.rotation.set(this.pitch, this.yaw, 0, 'YXZ');
      const hit = this.pointer && !this.drag ? this.hitFromPointer(this.pointer) : null;
      this.setHover('mouse', hit ? hit.object : null);
      this.renderer.domElement.style.cursor = this.drag?.moved ? 'grabbing' : hit ? 'pointer' : 'grab';
      return;
    }
    this.setHover('mouse', null);
    for (const { controller, line, dot } of this.controllers) {
      if (!controller.visible) continue;
      const hit = this.hitFromController(controller);
      this.setHover(controller, hit ? hit.object : null);
      line.scale.z = hit ? hit.distance : RAY_LENGTH;
      dot.visible = Boolean(hit);
      if (hit) dot.position.set(0, 0, -hit.distance);
    }
  }
}
