"""
Zapbox WebXR test server — 3D light painting with controllers.
Open http://<your-ip>:8080 in the Zapbox browser.
"""

import asyncio
from aiohttp import web

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
<title>Zapbox Light Painter</title>
<style>
  * { margin: 0; padding: 0; }
  html, body { width: 100%; height: 100%; overflow: hidden; background: #000; }
  canvas { display: block; }

  #overlay {
    position: fixed; inset: 0; z-index: 10;
    display: flex; align-items: center; justify-content: center;
    background: rgba(0,0,0,0.85);
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    color: #fff; text-align: center;
  }
  #overlay.hidden { display: none; }

  .card {
    background: rgba(255,255,255,0.06);
    border: 1px solid #333; border-radius: 16px;
    padding: 40px 32px; max-width: 380px;
  }
  h1 { font-size: 26px; margin-bottom: 12px; }
  .sub { color: #888; font-size: 13px; line-height: 1.6; margin-bottom: 20px; }
  #start-btn {
    padding: 14px 36px; font-size: 16px; font-weight: 700;
    border: none; border-radius: 12px; cursor: pointer;
    background: #a855f7; color: #fff; transition: transform 0.1s;
  }
  #start-btn:active { transform: scale(0.95); }
  #start-btn:disabled { opacity: 0.4; cursor: default; }
  .controls {
    margin-top: 20px; font-size: 12px; color: #666; line-height: 1.8;
  }
  .controls strong { color: #aaa; }
  #status { color: #f59e0b; font-size: 13px; margin-top: 12px; }

  #debug-log {
    position: fixed; bottom: 0; left: 0; right: 0; z-index: 30;
    max-height: 30vh; overflow-y: auto;
    background: rgba(0,0,0,0.8);
    font-family: monospace; font-size: 11px; color: #0f0;
    padding: 8px; pointer-events: none;
  }
  #debug-log.hidden { display: none; }
</style>
</head>
<body>

<div id="overlay">
  <div class="card">
    <h1>Light Painter</h1>
    <div class="sub">
      Draw glowing trails in 3D space<br>using your Zapbox controllers.
    </div>
    <button id="start-btn" disabled>Checking WebXR...</button>
    <div class="controls">
      <strong>Trigger</strong> — draw<br>
      <strong>Joystick ↑↓</strong> — brush thickness<br>
      <strong>Button A</strong> — change colour<br>
      <strong>Button B</strong> — clear canvas
    </div>
    <div id="status"></div>
  </div>
</div>

<div id="debug-log" class="hidden"></div>

<script type="importmap">
{
  "imports": {
    "three": "https://cdn.jsdelivr.net/npm/three@0.164.1/build/three.module.js",
    "three/addons/": "https://cdn.jsdelivr.net/npm/three@0.164.1/examples/jsm/"
  }
}
</script>

<script type="module">
import * as THREE from "three";

const COLORS = [
  { name: "Magenta",  hex: "#ff3cac" },
  { name: "Cyan",     hex: "#00f0ff" },
  { name: "Lime",     hex: "#39ff14" },
  { name: "Gold",     hex: "#ffb300" },
  { name: "Violet",   hex: "#b94fff" },
  { name: "Hot Pink", hex: "#ff006e" },
];

const overlay  = document.getElementById("overlay");
const startBtn = document.getElementById("start-btn");
const statusEl = document.getElementById("status");
const debugLog = document.getElementById("debug-log");

function dbg(msg) {
  console.log("[XR]", msg);
  debugLog.classList.remove("hidden");
  const line = document.createElement("div");
  const ts = new Date().toLocaleTimeString("en-GB", { hour12: false });
  line.textContent = ts + "  " + msg;
  debugLog.appendChild(line);
  if (debugLog.children.length > 200) debugLog.removeChild(debugLog.firstChild);
  debugLog.scrollTop = debugLog.scrollHeight;
}

let renderer, scene, camera;
let xrSession = null;
let refSpace  = null;
let colorIdx  = 0;
let strokes   = [];
let activeStroke = null;
let strokeCount = 0;
let frameCount = 0;
let lastDrawPos = null;

const MIN_THICKNESS = 0.002;
const MAX_THICKNESS = 0.04;
const THICKNESS_SPEED = 0.0006;
let brushThickness = 0.008;

const prevButtons = { trigger: false, a: false, b: false };
let controllerMarkers = [];

// ================================================================
//  3D in-scene HUD (canvas texture on a plane near the controller)
// ================================================================
const HUD_W = 256, HUD_H = 128;
let hudCanvas, hudCtx, hudTexture, hudMesh;

function createSceneHud() {
  hudCanvas = document.createElement("canvas");
  hudCanvas.width = HUD_W;
  hudCanvas.height = HUD_H;
  hudCtx = hudCanvas.getContext("2d");

  hudTexture = new THREE.CanvasTexture(hudCanvas);
  hudTexture.minFilter = THREE.LinearFilter;

  const mat = new THREE.MeshBasicMaterial({
    map: hudTexture,
    transparent: true,
    depthTest: false,
  });
  const geo = new THREE.PlaneGeometry(0.16, 0.08);
  hudMesh = new THREE.Mesh(geo, mat);
  hudMesh.renderOrder = 9999;
  hudMesh.visible = false;
  scene.add(hudMesh);
}

function drawSceneHud() {
  const ctx = hudCtx;
  const c = COLORS[colorIdx];

  ctx.clearRect(0, 0, HUD_W, HUD_H);

  ctx.fillStyle = "rgba(0,0,0,0.55)";
  roundRect(ctx, 0, 0, HUD_W, HUD_H, 16);
  ctx.fill();

  ctx.beginPath();
  ctx.arc(40, HUD_H / 2, 22, 0, Math.PI * 2);
  ctx.fillStyle = c.hex;
  ctx.fill();
  ctx.lineWidth = 2;
  ctx.strokeStyle = "rgba(255,255,255,0.3)";
  ctx.stroke();

  ctx.fillStyle = c.hex;
  ctx.font = "bold 20px sans-serif";
  ctx.textBaseline = "top";
  ctx.fillText(c.name, 74, 18);

  const pct = (brushThickness - MIN_THICKNESS) / (MAX_THICKNESS - MIN_THICKNESS);
  const barW = 140, barH = 10, barX = 74, barY = 82;

  ctx.fillStyle = "rgba(255,255,255,0.15)";
  roundRect(ctx, barX, barY, barW, barH, 5);
  ctx.fill();

  ctx.fillStyle = c.hex;
  roundRect(ctx, barX, barY, barW * pct, barH, 5);
  ctx.fill();

  ctx.fillStyle = "#aaa";
  ctx.font = "13px sans-serif";
  ctx.fillText("Size: " + brushThickness.toFixed(3), 74, 56);

  ctx.fillStyle = "#666";
  ctx.font = "12px sans-serif";
  ctx.textAlign = "right";
  ctx.fillText(strokeCount + " strokes", HUD_W - 14, HUD_H - 14);
  ctx.textAlign = "left";

  hudTexture.needsUpdate = true;
}

function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.lineTo(x + w - r, y);
  ctx.quadraticCurveTo(x + w, y, x + w, y + r);
  ctx.lineTo(x + w, y + h - r);
  ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
  ctx.lineTo(x + r, y + h);
  ctx.quadraticCurveTo(x, y + h, x, y + h - r);
  ctx.lineTo(x, y + r);
  ctx.quadraticCurveTo(x, y, x + r, y);
  ctx.closePath();
}

function positionHud(frame, sources) {
  const xrCam = renderer.xr.getCamera();

  for (const src of sources) {
    const space = src.gripSpace || src.targetRaySpace;
    if (!space) continue;
    const pose = frame.getPose(space, refSpace);
    if (!pose) continue;

    const t = pose.transform.position;
    const q = pose.transform.orientation;
    const quat = new THREE.Quaternion(q.x, q.y, q.z, q.w);

    const offset = new THREE.Vector3(0.08, 0.1, -0.1).applyQuaternion(quat);
    hudMesh.position.set(t.x + offset.x, t.y + offset.y, t.z + offset.z);

    hudMesh.lookAt(xrCam.position);

    hudMesh.visible = true;
    return;
  }
  hudMesh.visible = false;
}

// ================================================================
//  Init
// ================================================================
function init() {
  renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(window.devicePixelRatio);
  renderer.setSize(window.innerWidth, window.innerHeight);
  renderer.xr.enabled = true;
  renderer.setClearColor(0x000000, 0);
  document.body.prepend(renderer.domElement);

  scene  = new THREE.Scene();
  camera = new THREE.PerspectiveCamera(70, window.innerWidth / window.innerHeight, 0.01, 50);

  scene.add(new THREE.AmbientLight(0xffffff, 0.5));
  const dirLight = new THREE.DirectionalLight(0xffffff, 0.8);
  dirLight.position.set(0, 2, 1);
  scene.add(dirLight);

  addFloatingCubes();
  createSceneHud();

  window.addEventListener("resize", () => {
    camera.aspect = window.innerWidth / window.innerHeight;
    camera.updateProjectionMatrix();
    renderer.setSize(window.innerWidth, window.innerHeight);
  });

  dbg("Three.js renderer initialised");
}

function addFloatingCubes() {
  const cubeGeo = new THREE.BoxGeometry(0.06, 0.06, 0.06);
  const positions = [
    [0,    1.2, -0.5],
    [0.3,  1.0, -0.6],
    [-0.3, 1.4, -0.4],
    [0.15, 1.5, -0.7],
    [-0.2, 0.9, -0.5],
  ];
  for (let i = 0; i < positions.length; i++) {
    const color = new THREE.Color(COLORS[i % COLORS.length].hex);
    const mat = new THREE.MeshStandardMaterial({
      color, emissive: color, emissiveIntensity: 0.4,
      transparent: true, opacity: 0.85,
    });
    const mesh = new THREE.Mesh(cubeGeo, mat);
    mesh.position.set(...positions[i]);
    mesh.userData.baseY = positions[i][1];
    mesh.userData.phase = Math.random() * Math.PI * 2;
    scene.add(mesh);
  }
}

// ================================================================
//  Controller markers
// ================================================================
function createControllerMarker() {
  const group = new THREE.Group();

  const sphere = new THREE.Mesh(
    new THREE.SphereGeometry(0.02, 16, 16),
    new THREE.MeshStandardMaterial({
      color: 0xffffff, emissive: 0x00f0ff, emissiveIntensity: 0.8,
      transparent: true, opacity: 0.9,
    })
  );
  group.add(sphere);

  const ray = new THREE.Mesh(
    new THREE.CylinderGeometry(0.002, 0.001, 1.0, 8),
    new THREE.MeshBasicMaterial({ color: 0x00f0ff, transparent: true, opacity: 0.25 })
  );
  ray.rotation.x = Math.PI / 2;
  ray.position.z = -0.5;
  group.add(ray);

  const tip = new THREE.Mesh(
    new THREE.SphereGeometry(0.012, 12, 12),
    new THREE.MeshStandardMaterial({
      color: 0xffffff, emissive: 0xff3cac, emissiveIntensity: 0.9,
      transparent: true, opacity: 0.85,
    })
  );
  tip.position.z = -1.0;
  group.add(tip);

  const light = new THREE.PointLight(0x00f0ff, 0.4, 0.3);
  group.add(light);

  group.visible = false;
  scene.add(group);
  return group;
}

function updateControllerMarkers(frame, sources) {
  while (controllerMarkers.length < sources.length) {
    controllerMarkers.push(createControllerMarker());
  }
  let idx = 0;
  for (const src of sources) {
    const space = src.gripSpace || src.targetRaySpace;
    if (!space) continue;
    const pose = frame.getPose(space, refSpace);
    const marker = controllerMarkers[idx];
    if (pose) {
      marker.visible = true;
      const t = pose.transform.position;
      const q = pose.transform.orientation;
      marker.position.set(t.x, t.y, t.z);
      marker.quaternion.set(q.x, q.y, q.z, q.w);
    } else {
      marker.visible = false;
    }
    idx++;
  }
  for (let i = idx; i < controllerMarkers.length; i++) {
    controllerMarkers[i].visible = false;
  }
}

// ================================================================
//  Strokes (tube geometry)
// ================================================================
function clearStrokes() {
  for (const s of strokes) {
    if (s.mesh) { scene.remove(s.mesh); s.mesh.geometry.dispose(); }
    s.mat.dispose();
    if (s.glow) { scene.remove(s.glow); s.glow.dispose(); }
  }
  strokes = [];
  activeStroke = null;
  strokeCount = 0;
  drawSceneHud();
  dbg("Strokes cleared");
}

function rebuildTube(stroke) {
  if (stroke.mesh) { scene.remove(stroke.mesh); stroke.mesh.geometry.dispose(); }
  if (stroke.points.length < 2) return;
  const curve = new THREE.CatmullRomCurve3(stroke.points, false, "centripetal", 0.3);
  const segments = Math.max(stroke.points.length * 2, 8);
  const geo = new THREE.TubeGeometry(curve, segments, stroke.radius, 8, false);
  stroke.mesh = new THREE.Mesh(geo, stroke.mat);
  scene.add(stroke.mesh);
}

function beginStroke(pos) {
  const color = new THREE.Color(COLORS[colorIdx].hex);
  const mat = new THREE.MeshStandardMaterial({
    color, emissive: color, emissiveIntensity: 0.5,
    transparent: true, opacity: 0.9, roughness: 0.3, metalness: 0.1,
  });
  const glow = new THREE.PointLight(color, 0.6, 0.5);
  glow.position.copy(pos);
  scene.add(glow);

  activeStroke = { mat, mesh: null, points: [pos.clone()], radius: brushThickness, glow };
  strokes.push(activeStroke);
  strokeCount++;
  lastDrawPos = pos.clone();
  drawSceneHud();
}

function extendStroke(pos) {
  if (!activeStroke) return;
  const pts = activeStroke.points;
  const last = pts[pts.length - 1];
  if (last.distanceTo(pos) < 0.003) return;

  pts.push(pos.clone());
  activeStroke.glow.position.copy(pos);
  lastDrawPos = pos.clone();

  if (pts.length % 3 === 0 || pts.length < 6) {
    rebuildTube(activeStroke);
  }
}

function endStroke() {
  if (!activeStroke) return;
  rebuildTube(activeStroke);
  if (activeStroke.glow) {
    scene.remove(activeStroke.glow);
    activeStroke.glow.dispose();
    activeStroke.glow = null;
  }
  activeStroke = null;
}

function splitStrokeForColorChange(pos) {
  endStroke();
  beginStroke(pos);
}

// ================================================================
//  Input reading — log ALL buttons on first press to find mapping
// ================================================================
let buttonMapLogged = false;

function readInputs(sources) {
  let trigger = false, btnA = false, btnB = false;
  let joystickY = 0;

  for (const src of sources) {
    if (!src.gamepad) continue;
    const btns = src.gamepad.buttons;
    const axes = src.gamepad.axes;

    if (frameCount === 1) {
      dbg("Gamepad: " + btns.length + " buttons, " +
          axes.length + " axes, hand=" + (src.handedness || "none"));
    }

    if (!buttonMapLogged) {
      for (let i = 0; i < btns.length; i++) {
        if (btns[i].pressed) {
          dbg("BUTTON MAP: index " + i + " pressed (hand=" + (src.handedness || "?") + ")");
          buttonMapLogged = true;
          setTimeout(() => { buttonMapLogged = false; }, 500);
        }
      }
    }

    trigger = btns.length > 0 && btns[0].pressed;

    // Zapbox maps physical A to index 5, physical B to index 4
    btnA = btns.length > 5 && btns[5].pressed;
    btnB = btns.length > 4 && btns[4].pressed;

    if (axes.length >= 4) {
      joystickY = axes[3];
    } else if (axes.length >= 2) {
      joystickY = axes[1];
    }

    break;
  }

  return { trigger, a: btnA, b: btnB, joystickY };
}

// ================================================================
//  Draw-point projection (1m forward from controller)
// ================================================================
const DRAW_OFFSET = 1.0;

function getControllerPos(frame, sources) {
  for (const src of sources) {
    const space = src.gripSpace || src.targetRaySpace;
    if (!space) continue;
    const pose = frame.getPose(space, refSpace);
    if (pose) {
      const t = pose.transform.position;
      const q = pose.transform.orientation;
      const origin = new THREE.Vector3(t.x, t.y, t.z);
      const quat = new THREE.Quaternion(q.x, q.y, q.z, q.w);
      const forward = new THREE.Vector3(0, 0, -DRAW_OFFSET).applyQuaternion(quat);
      return origin.add(forward);
    }
  }
  return null;
}

// ================================================================
//  Animation
// ================================================================
function animateCubes(time) {
  scene.traverse((obj) => {
    if (obj.isMesh && obj.userData.baseY !== undefined) {
      obj.position.y = obj.userData.baseY + Math.sin(time * 0.001 + obj.userData.phase) * 0.03;
      obj.rotation.x = time * 0.0005;
      obj.rotation.y = time * 0.0008;
    }
  });
}

// ================================================================
//  XR frame loop
// ================================================================
function onXRFrame(time, frame) {
  const session = renderer.xr.getSession();
  if (!session) return;
  frameCount++;

  const sources = session.inputSources;

  if (frameCount === 1) {
    dbg("XR frame loop running — " + sources.length + " input source(s)");
  }
  if (sources.length === 0 && frameCount === 30) {
    dbg("WARNING: No input sources detected after 30 frames");
  }

  updateControllerMarkers(frame, sources);
  positionHud(frame, sources);

  const inp = readInputs(sources);
  const pos = getControllerPos(frame, sources);

  // Joystick → thickness
  if (Math.abs(inp.joystickY) > 0.15) {
    brushThickness = Math.min(MAX_THICKNESS,
      Math.max(MIN_THICKNESS, brushThickness - inp.joystickY * THICKNESS_SPEED));
    drawSceneHud();
  }

  // Button A → cycle colour (works mid-stroke)
  if (inp.a && !prevButtons.a) {
    colorIdx = (colorIdx + 1) % COLORS.length;
    dbg("BUTTON A -> colour: " + COLORS[colorIdx].name);
    drawSceneHud();

    if (activeStroke && lastDrawPos) {
      splitStrokeForColorChange(lastDrawPos);
    }
  }

  // Button B → clear
  if (inp.b && !prevButtons.b) {
    clearStrokes();
    dbg("BUTTON B -> cleared");
  }

  // Trigger → draw
  if (inp.trigger && !prevButtons.trigger) {
    dbg("TRIGGER pressed  thickness=" + brushThickness.toFixed(3));
  }
  if (!inp.trigger && prevButtons.trigger) {
    dbg("TRIGGER released");
  }

  if (inp.trigger && pos) {
    if (!prevButtons.trigger) {
      beginStroke(pos);
    } else {
      extendStroke(pos);
    }
  } else if (!inp.trigger && prevButtons.trigger) {
    endStroke();
  }

  prevButtons.trigger = inp.trigger;
  prevButtons.a = inp.a;
  prevButtons.b = inp.b;

  animateCubes(time);
  renderer.render(scene, camera);
}

// ================================================================
//  XR session management
// ================================================================
async function startXR() {
  dbg("Requesting XR session...");

  const sessionType = await (async () => {
    if (await navigator.xr.isSessionSupported("immersive-ar")) return "immersive-ar";
    if (await navigator.xr.isSessionSupported("immersive-vr")) return "immersive-vr";
    return null;
  })();

  dbg("Session type: " + sessionType);
  if (!sessionType) {
    statusEl.textContent = "No supported XR session type found.";
    return;
  }

  try {
    xrSession = await navigator.xr.requestSession(sessionType, {
      optionalFeatures: ["local-floor", "hand-tracking"],
    });
    dbg("XR session created");
  } catch (e) {
    dbg("XR session FAILED: " + e.message);
    statusEl.textContent = "XR session failed: " + e.message;
    return;
  }

  renderer.xr.setSession(xrSession);

  try {
    refSpace = await xrSession.requestReferenceSpace("local-floor");
    dbg("Reference space: local-floor");
  } catch (e) {
    dbg("local-floor unavailable, trying local: " + e.message);
    refSpace = await xrSession.requestReferenceSpace("local");
    dbg("Reference space: local");
  }

  overlay.classList.add("hidden");
  frameCount = 0;
  drawSceneHud();

  renderer.setAnimationLoop(onXRFrame);

  xrSession.addEventListener("end", () => {
    dbg("XR session ended");
    xrSession = null;
    refSpace = null;
    renderer.setAnimationLoop(null);
    overlay.classList.remove("hidden");
    if (hudMesh) hudMesh.visible = false;
    clearStrokes();
  });

  xrSession.addEventListener("inputsourceschange", (e) => {
    dbg("Input sources changed: added=" + e.added.length +
        " removed=" + e.removed.length +
        " total=" + xrSession.inputSources.length);
    for (const src of e.added) {
      dbg("  + source: hand=" + (src.handedness || "none") +
          " grip=" + !!src.gripSpace +
          " ray=" + !!src.targetRaySpace +
          " gamepad=" + !!src.gamepad);
    }
  });
}

// ================================================================
//  Boot
// ================================================================
async function boot() {
  init();
  dbg("Boot: checking WebXR support...");

  if (!navigator.xr) {
    startBtn.textContent = "WebXR not available";
    statusEl.textContent = "Open this page in the Zapbox browser.";
    dbg("navigator.xr is undefined");
    return;
  }

  const ar = await navigator.xr.isSessionSupported("immersive-ar").catch(() => false);
  const vr = await navigator.xr.isSessionSupported("immersive-vr").catch(() => false);
  dbg("immersive-ar=" + ar + "  immersive-vr=" + vr);

  if (!ar && !vr) {
    startBtn.textContent = "No XR sessions supported";
    statusEl.textContent = "This browser doesn't support immersive sessions.";
    return;
  }

  startBtn.disabled = false;
  startBtn.textContent = "Enter XR";
  statusEl.textContent = ar ? "immersive-ar available" : "immersive-vr available";
  dbg("Ready — tap Enter XR");

  startBtn.addEventListener("click", startXR);
}

boot();
</script>
</body>
</html>
"""

PORT = 8080


async def handle(_request):
    return web.Response(text=HTML, content_type="text/html")


async def main():
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"Light Painter running on http://0.0.0.0:{PORT}")
    print("Open this URL in the Zapbox browser, then tap 'Enter XR'.")
    print("  Trigger = draw  |  Joystick = thickness  |  A = colour  |  B = clear")
    print("Press Ctrl+C to stop.")
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
