import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { FBXLoader } from 'three/addons/loaders/FBXLoader.js';

/**
 * MotionViewer – shared 3D viewer for GLB models and FBX source animations.
 *
 * Public contract:
 *   new MotionViewer(hostElement, { onChange?, onError? })
 *   async load(url, { label? })          – load a GLB model (skinned or static)
 *   async loadAnimation(url, { label? }) – load a pure FBX source motion
 *   setMode('model' | 'skeleton' | 'joints')
 *   setPlaying(boolean)
 *   setSpeed(number)
 *   seek(seconds)
 *   resetView()
 *   dispose()
 *
 * onChange(state) where state = {
 *   time, duration, playing, animationsCount, bonesCount,
 *   verticesCount, hasModel, label, mode
 * }
 */
export class MotionViewer {
  constructor(hostElement, { onChange, onError } = {}) {
    this._host = hostElement;
    this._onChange = onChange || (() => {});
    this._onError = onError || (() => {});

    this._mode = 'model';
    this._playing = false;
    this._speed = 1.0;
    this._label = '';
    this._loadEpoch = 0;
    this._disposed = false;

    this._mixer = null;
    this._clipAction = null;
    this._duration = 0;
    this._animationsCount = 0;
    this._bonesCount = 0;
    this._verticesCount = 0;
    this._hasModel = false;

    this._root = null;           // loaded model group
    this._skeletonHelper = null; // THREE.SkeletonHelper for bone lines
    this._jointMarkers = null;   // Points for joint markers
    this._jointPositions = null; // Float32Array reused each frame
    this._jointScratch = new THREE.Vector3();

    this._lastChangeEmit = 0;
    this._changeThrottleMs = 33; // ~30 fps

    this._visible = true;
    this._rafId = null;
    this._clock = new THREE.Clock();

    this._init();
  }

  // ------------------------------------------------------------------ setup

  _init() {
    if (!this._host) {
      throw new Error('MotionViewer: host element required');
    }


    // Scene
    this._scene = new THREE.Scene();

    // Camera
    const aspect = this._host.clientWidth / Math.max(1, this._host.clientHeight);
    this._camera = new THREE.PerspectiveCamera(45, aspect, 0.01, 1000);
    this._camera.position.set(2, 1.5, 3);

    // Renderer – transparent background so CSS frame shows through
    this._renderer = new THREE.WebGLRenderer({
      antialias: true,
      alpha: true,
      powerPreference: 'high-performance',
    });
    this._renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this._renderer.setSize(this._host.clientWidth, this._host.clientHeight, false);
    this._renderer.outputColorSpace = THREE.SRGBColorSpace;
    this._renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this._renderer.toneMappingExposure = 1.05;
    this._renderer.shadowMap.enabled = true;
    this._renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    this._host.appendChild(this._renderer.domElement);
    this._renderer.domElement.style.display = 'block';
    this._renderer.domElement.style.width = '100%';
    this._renderer.domElement.style.height = '100%';

    // Controls
    this._controls = new OrbitControls(this._camera, this._renderer.domElement);
    this._controls.enableDamping = true;
    this._controls.dampingFactor = 0.08;
    this._controls.minDistance = 0.05;
    this._controls.maxDistance = 200;
    this._controls.target.set(0, 0.5, 0);

    // Lights
    const hemi = new THREE.HemisphereLight(0xe8eef7, 0x2a2f3a, 0.55);
    this._scene.add(hemi);

    const key = new THREE.DirectionalLight(0xffffff, 1.1);
    key.position.set(3, 5, 4);
    key.castShadow = true;
    key.shadow.mapSize.set(1024, 1024);
    key.shadow.camera.near = 0.5;
    key.shadow.camera.far = 30;
    key.shadow.camera.left = -5;
    key.shadow.camera.right = 5;
    key.shadow.camera.top = 5;
    key.shadow.camera.bottom = -5;
    key.shadow.bias = -0.0005;
    this._scene.add(key);
    this._keyLight = key;

    const fill = new THREE.DirectionalLight(0x9fb4d9, 0.35);
    fill.position.set(-4, 2, -2);
    this._scene.add(fill);

    const rim = new THREE.DirectionalLight(0xffe79a, 0.25);
    rim.position.set(0, 3, -5);
    this._scene.add(rim);

    // Ground / plinth grid
    const grid = new THREE.GridHelper(10, 40, 0x4a5569, 0x323949);
    grid.position.y = 0;
    grid.material.transparent = true;
    grid.material.opacity = 0.35;
    this._scene.add(grid);
    this._grid = grid;

    // Subtle ground plane for shadow reception
    const groundGeo = new THREE.PlaneGeometry(20, 20);
    const groundMat = new THREE.ShadowMaterial({ opacity: 0.18 });
    const ground = new THREE.Mesh(groundGeo, groundMat);
    ground.rotation.x = -Math.PI / 2;
    ground.position.y = -0.001;
    ground.receiveShadow = true;
    this._scene.add(ground);
    this._ground = ground;

    // Resize observer
    this._ro = new ResizeObserver(() => this._onResize());
    this._ro.observe(this._host);

    // Visibility observer (pause rendering when off-screen)
    this._vio = new IntersectionObserver((entries) => {
      for (const e of entries) {
        this._visible = e.isIntersecting;
      }
    }, { threshold: 0 });
    this._vio.observe(this._host);

    // Start loop
    this._animate = this._animate.bind(this);
    this._rafId = requestAnimationFrame(this._animate);
  }


  // ------------------------------------------------------------- public API

  /**
   * Load a GLB model (skinned or static). Replaces current content.
   */
  async load(url, { label = '' } = {}) {
    const epoch = ++this._loadEpoch;
    this._label = label || '';

    try {
      const loader = new GLTFLoader();
      const gltf = await loader.loadAsync(url);

      if (epoch !== this._loadEpoch) {
        this._disposeObject(gltf.scene);
        return;
      }

      this._setupModel(gltf.scene, gltf.animations || [], false);
      this._emitChange(true);
    } catch (err) {
      if (epoch === this._loadEpoch) {
        this._clearScene();
        this._onError(err);
      }
      throw err;
    }
  }

  /**
   * Load a pure FBX source animation. Bone lines + joint markers always visible.
   */
  async loadAnimation(url, { label = '' } = {}) {
    const epoch = ++this._loadEpoch;
    this._label = label || '';

    try {
      const loader = new FBXLoader();
      const obj = await loader.loadAsync(url);

      if (epoch !== this._loadEpoch) {
        this._disposeObject(obj);
        return;
      }

      // FBX is often authored in centimeters (3ds Max / Maya default).
      // If the overall bounding box is very large, assume cm-scale and
      // downscale to roughly meter units so camera fitting stays sane.
      const box = new THREE.Box3().setFromObject(obj);
      if (box.isEmpty()) {
        obj.updateMatrixWorld(true);
        obj.traverse((node) => {
          if (node.isBone) box.expandByPoint(this._jointScratch.setFromMatrixPosition(node.matrixWorld));
        });
      }
      const size = new THREE.Vector3();
      box.getSize(size);
      const maxDim = Math.max(size.x, size.y, size.z);
      if (maxDim > 50) {
        // Likely centimeter scale → meter scale
        obj.scale.multiplyScalar(0.01);
      } else if (maxDim < 0.1 && maxDim > 0) {
        // Very tiny → scale up to ~1 unit
        obj.scale.multiplyScalar(1.0 / maxDim);
      }

      this._setupModel(obj, obj.animations || [], true);
      // Source FBX: default to skeleton mode so bones are visible even without mesh
      this.setMode('skeleton');
      this._emitChange(true);
    } catch (err) {
      if (epoch === this._loadEpoch) {
        this._clearScene();
        this._onError(err);
      }
      throw err;
    }
  }

  setMode(mode) {
    if (!['model', 'skeleton', 'joints'].includes(mode)) return;
    this._mode = mode;
    this._applyMode();
    this._emitChange(true);
  }

  setPlaying(playing) {
    const shouldPlay = !!playing;
    if (shouldPlay === this._playing) return;
    this._playing = shouldPlay;
    if (this._clipAction) {
      this._clipAction.paused = !shouldPlay;
      if (shouldPlay) {
        // Don't reset time – just ensure it's playing
        this._clipAction.play();
        this._clock.start();
      }
    }
    this._emitChange(true);
  }

  setSpeed(speed) {
    this._speed = Math.max(0, Number(speed)) || 0;
    if (this._clipAction) {
      this._clipAction.setEffectiveTimeScale(this._speed);
    }
    this._emitChange(true);
  }

  seek(time) {
    if (!this._clipAction || !this._duration) return;
    const t = Math.max(0, Math.min(time, this._duration));
    this._clipAction.time = t;
    // Update mixer so joint/skeleton positions reflect seek immediately
    if (this._mixer) {
      this._mixer.update(0);
    }
    this._updateJointMarkers();
    if (this._skeletonHelper) this._skeletonHelper.updateMatrixWorld(true);
    this._emitChange(true);
  }

  resetView() {
    this._fitCamera();
    this._controls.reset();
  }

  dispose() {
    if (this._disposed) return;
    this._disposed = true;
    this._loadEpoch++;
    if (this._rafId) {
      cancelAnimationFrame(this._rafId);
      this._rafId = null;
    }
    if (this._ro) { this._ro.disconnect(); this._ro = null; }
    if (this._vio) { this._vio.disconnect(); this._vio = null; }

    this._clearScene();
    for (const object of [this._grid, this._ground]) {
      object.geometry.dispose();
      object.material.dispose();
    }
    this._grid = null;
    this._ground = null;
    this._keyLight.shadow.dispose();

    if (this._controls) { this._controls.dispose(); this._controls = null; }
    if (this._renderer) {
      this._renderer.dispose();
      if (this._renderer.domElement.parentNode === this._host) {
        this._host.removeChild(this._renderer.domElement);
      }
      this._renderer = null;
    }
    this._camera = null;
    this._scene = null;
    this._clock = null;
  }

  // ----------------------------------------------------------- model setup

  _setupModel(root, animations, isSourceFBX) {
    // Clear previous
    this._clearSceneContent();

    this._root = root;
    this._isSourceFBX = isSourceFBX;

    // Count vertices and bones
    let verts = 0;
    const bones = new Set();
    root.traverse((obj) => {
      if (obj.isMesh) {
        obj.castShadow = true;
        obj.receiveShadow = true;
        if (obj.geometry && obj.geometry.attributes.position) {
          verts += obj.geometry.attributes.position.count;
        }
        // Ensure any skinning bones counted via skeletons
        if (obj.skeleton) {
          for (const bone of obj.skeleton.bones) bones.add(bone);
        }
      }
      if (obj.isBone) bones.add(obj);
    });
    this._verticesCount = verts;
    this._bonesCount = bones.size;
    this._boneList = [...bones];
    this._hasModel = verts > 0;

    // Scene graph
    this._scene.add(root);

    // Animation
    this._animationsCount = animations.length;
    if (animations.length > 0) {
      this._mixer = new THREE.AnimationMixer(root);
      const clip = animations[0];
      this._clipAction = this._mixer.clipAction(clip);
      this._clipAction.setLoop(THREE.LoopRepeat);
      this._clipAction.setEffectiveTimeScale(this._speed);
      this._duration = clip.duration;
      this._clipAction.play();
      this._playing = true;
      this._clock.start();
    } else {
      this._mixer = null;
      this._clipAction = null;
      this._duration = 0;
      this._playing = false;
    }

    // Skeleton helper (bone lines)
    this._createSkeletonHelper(root, isSourceFBX);

    // Joint markers (points)
    this._createJointMarkers(root, isSourceFBX);

    // Fit camera
    this._fitCamera();

    // Apply current mode
    this._applyMode();
  }

  _createSkeletonHelper(root, isSourceFBX) {
    if (!this._bonesCount) return;
    const helper = new THREE.SkeletonHelper(root);
    helper.material.vertexColors = false;
    helper.material.color.set(isSourceFBX ? 0x7eb0ff : 0xffd479);
    helper.material.transparent = true;
    helper.material.opacity = 0.9;
    helper.material.depthTest = false;
    helper.material.depthWrite = false;
    helper.renderOrder = 2;
    this._scene.add(helper);
    helper.updateMatrixWorld(true);
    this._skeletonHelper = helper;
  }

  _createJointMarkers(root, isSourceFBX) {
    // Dispose old
    if (this._jointMarkers) {
      this._scene.remove(this._jointMarkers);
      this._jointMarkers.geometry.dispose();
      this._jointMarkers.material.dispose();
      this._jointMarkers = null;
      this._jointPositions = null;
    }

    const bones = this._boneList;
    if (!bones?.length) return;

    // Use Points for joint markers – lightweight, updated each frame
    const positions = new Float32Array(bones.length * 3);
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));

    const color = isSourceFBX ? 0x7eb0ff : 0xffd479;
    const material = new THREE.PointsMaterial({
      color,
      size: 0.025,
      sizeAttenuation: true,
      transparent: true,
      opacity: 0.95,
      depthTest: true,
      depthWrite: false,
    });

    const points = new THREE.Points(geometry, material);
    points.renderOrder = 3;
    points.frustumCulled = false;
    this._scene.add(points);
    this._jointMarkers = points;
    this._jointPositions = positions;

    this._updateJointMarkers();
  }

  _updateJointMarkers() {
    if (!this._jointMarkers || !this._boneList) return;
    const pos = this._jointPositions;
    const bones = this._boneList;
    this._root.updateMatrixWorld(true);
    const v = this._jointScratch;
    for (let i = 0; i < bones.length; i++) {
      v.setFromMatrixPosition(bones[i].matrixWorld);
      pos[i * 3] = v.x;
      pos[i * 3 + 1] = v.y;
      pos[i * 3 + 2] = v.z;
    }
    this._jointMarkers.geometry.attributes.position.needsUpdate = true;
  }

  _applyMode() {
    if (!this._root) return;

    const mode = this._mode;
    const showMesh = mode === 'model' || mode === 'skeleton';
    const showSkeleton = mode === 'skeleton' || mode === 'joints';
    const showJoints = mode === 'joints';

    // Toggle mesh visibility
    this._root.traverse((obj) => {
      if (obj.isMesh) {
        obj.visible = showMesh;
      }
    });

    if (this._skeletonHelper) {
      this._skeletonHelper.visible = showSkeleton;
    }

    if (this._jointMarkers) {
      this._jointMarkers.visible = showJoints;
    }
  }

  // --------------------------------------------------------- camera fitting

  _fitCamera() {
    if (!this._root || !this._camera || !this._controls) return;

    const box = new THREE.Box3();

    // Try geometry first
    let hasGeometry = false;
    this._root.traverse((obj) => {
      if (obj.isMesh && obj.geometry) {
        obj.updateMatrixWorld(true);
        box.expandByObject(obj);
        hasGeometry = true;
      }
    });

    // If no geometry (pure skeleton), compute from bone positions
    if (!hasGeometry) {
      const bones = [];
      this._root.traverse((obj) => {
        if (obj.isBone) bones.push(obj);
      });
      if (bones.length > 0) {
        const v = new THREE.Vector3();
        for (const b of bones) {
          b.getWorldPosition(v);
          box.expandByPoint(v);
        }
      }
    }

    if (box.isEmpty()) {
      // Fallback: unit size
      box.setFromCenterAndSize(new THREE.Vector3(0, 0.5, 0), new THREE.Vector3(1, 1, 1));
    }

    const size = new THREE.Vector3();
    const center = new THREE.Vector3();
    box.getSize(size);
    box.getCenter(center);

    // Normalize near/far based on actual scene size
    const maxDim = Math.max(size.x, size.y, size.z, 0.01);
    this._camera.near = maxDim * 0.01;
    this._camera.far = maxDim * 100;
    this._camera.updateProjectionMatrix();

    // Position camera to frame the object nicely
    const fov = this._camera.fov * (Math.PI / 180);
    const distance = (maxDim / 2) / Math.tan(fov / 2) * 1.8;

    const dir = new THREE.Vector3(0.5, 0.35, 1).normalize();
    this._camera.position.copy(center).add(dir.multiplyScalar(distance));
    this._controls.target.copy(center);
    this._controls.update();
    this._controls.saveState();

    // Adjust light shadow camera
    if (this._keyLight) {
      this._keyLight.shadow.camera.left = -maxDim * 1.2;
      this._keyLight.shadow.camera.right = maxDim * 1.2;
      this._keyLight.shadow.camera.top = maxDim * 1.2;
      this._keyLight.shadow.camera.bottom = -maxDim * 1.2;
      this._keyLight.shadow.camera.near = maxDim * 0.1;
      this._keyLight.shadow.camera.far = maxDim * 10;
      this._keyLight.shadow.camera.updateProjectionMatrix();
    }

    // Scale grid to match scene
    if (this._grid) {
      const gridSize = Math.max(maxDim * 2, 2);
      this._grid.scale.set(gridSize / 10, 1, gridSize / 10);
      this._grid.position.y = box.min.y;
    }
    if (this._ground) {
      const groundSize = Math.max(maxDim * 4, 4);
      this._ground.scale.set(groundSize / 20, 1, groundSize / 20);
      this._ground.position.y = box.min.y - 0.001;
    }
  }

  // -------------------------------------------------------- resize / loop

  _onResize() {
    if (!this._renderer || !this._camera) return;
    const w = this._host.clientWidth;
    const h = this._host.clientHeight;
    if (w === 0 || h === 0) return;
    this._camera.aspect = w / h;
    this._camera.updateProjectionMatrix();
    this._renderer.setSize(w, h, false);
  }

  _animate() {
    this._rafId = requestAnimationFrame(this._animate);

    if (!this._renderer) return;
    if (!this._visible) {
      this._clock.getDelta();
      return;
    }

    // Only get clock delta when animation is actually running,
    // otherwise the clock accumulates time during pause and causes a jump.
    let delta = 0;
    if (this._mixer && this._playing && this._clipAction) {
      delta = this._clock.getDelta();
      this._mixer.update(delta);
    } else {
      // Still consume delta to keep the clock synchronized
      this._clock.getDelta();
    }

    // Update skeleton helper & joint markers to follow animation
    if (this._skeletonHelper && this._skeletonHelper.visible) {
      this._skeletonHelper.updateMatrixWorld(true);
    }
    if (this._jointMarkers && this._jointMarkers.visible) {
      this._updateJointMarkers();
    }

    // Controls damping
    if (this._controls) {
      this._controls.update();
    }

    // Render
    this._renderer.render(this._scene, this._camera);

    // Throttled change emission
    this._maybeEmitChange();
  }

  _maybeEmitChange() {
    this._emitChange(false);
  }

  _emitChange(immediate) {
    const now = performance.now();
    if (!immediate && now - this._lastChangeEmit < this._changeThrottleMs) return;
    this._lastChangeEmit = now;
    const time = this._clipAction ? this._clipAction.time : 0;
    this._onChange({
      time,
      duration: this._duration,
      playing: this._playing,
      animationsCount: this._animationsCount,
      bonesCount: this._bonesCount,
      verticesCount: this._verticesCount,
      hasModel: this._hasModel,
      label: this._label,
      mode: this._mode,
    });
  }

  // ------------------------------------------------------- resource cleanup

  _clearScene() {
    this._clearSceneContent();
    this._hasModel = false;
    this._bonesCount = 0;
    this._verticesCount = 0;
    this._animationsCount = 0;
    this._duration = 0;
    this._playing = false;
    this._label = '';
  }

  _clearSceneContent() {
    if (this._mixer) {
      this._mixer.stopAllAction();
      if (this._root) this._mixer.uncacheRoot(this._root);
      this._mixer = null;
    }
    this._clipAction = null;
    this._boneList = null;

    if (this._skeletonHelper) {
      this._scene.remove(this._skeletonHelper);
      this._skeletonHelper.geometry.dispose();
      this._skeletonHelper.material.dispose();
      this._skeletonHelper = null;
    }

    if (this._jointMarkers) {
      this._scene.remove(this._jointMarkers);
      this._jointMarkers.geometry.dispose();
      this._jointMarkers.material.dispose();
      this._jointMarkers = null;
      this._jointPositions = null;
    }

    if (this._root) {
      this._scene.remove(this._root);
      this._disposeObject(this._root);
      this._root = null;
    }
  }

  _disposeObject(obj) {
    if (!obj) return;
    const geometries = new Set();
    const materials = new Set();
    const textures = new Set();
    const skeletons = new Set();
    obj.traverse((child) => {
      if (child.geometry) geometries.add(child.geometry);
      if (child.material) {
        const entries = Array.isArray(child.material) ? child.material : [child.material];
        for (const material of entries) materials.add(material);
      }
      if (child.skeleton) skeletons.add(child.skeleton);
    });
    for (const geometry of geometries) geometry.dispose();
    for (const material of materials) {
      for (const value of Object.values(material)) {
        if (value?.isTexture) textures.add(value);
      }
      material.dispose();
    }
    for (const texture of textures) texture.dispose();
    for (const skeleton of skeletons) skeleton.dispose();
  }
}

export default MotionViewer;
