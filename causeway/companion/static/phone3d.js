/** The real Weebles on the phone page: companion.js on its own canvas, lit as in the companion scene, nothing around him. */

import * as THREE from 'three';
import { Companion } from './companion.js';

const SIZE = 320;

/** Draw him on `canvas`. Rejects when WebGL or the model fails; the page then keeps its still images. */
export async function mountWeebles(canvas) {
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setSize(SIZE, SIZE, false);

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(30, 1, 0.02, 10);
  camera.position.set(0, 0.03, 0.68); // room for the lifted mask above him and the droop after a Deny below
  camera.lookAt(0, 0.03, 0);

  scene.add(new THREE.HemisphereLight('#d5dbff', '#2a1d2c', 2.1));
  const key = new THREE.DirectionalLight('#fff0dc', 2.6);
  key.position.set(0.5, 0.9, 1.3);
  const rim = new THREE.DirectionalLight('#9a86ff', 3.2);
  rim.position.set(-0.35, 0.5, -1.2);
  scene.add(key, rim);

  const companion = new Companion();
  scene.add(companion.group);
  if (!(await companion.loadModel('static/companion.glb'))) throw new Error('companion.glb did not load');

  let last = 0;
  renderer.setAnimationLoop((time) => {
    const t = time / 1000;
    companion.update(Math.min(t - last, 0.1), t);
    last = t;
    renderer.render(scene, camera);
  });
  return companion;
}
