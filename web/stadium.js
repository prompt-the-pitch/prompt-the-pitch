// Prompt the Pitch: the match in a 3D stadium (three.js).
//
//   stadium.html?match=exl-prg-42&speed=5&lang=en&viewer=analyst   play a match on its own
//   stadium.html?room=ABC123&lang=en&viewer=analyst                 follow a watch room live
//
// Positions come from /api/matches/{id}/tracking (rebuilt from the event stream); the lower
// thirds come from the recorded agent run (/api/matches/{id}/overlays). Pitch axes: x (0-105)
// becomes three.js X, y (0-68) becomes Z, both centred on the pitch.

import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

const params = new URLSearchParams(location.search);
const roomCode = params.get("room");
let matchId = params.get("match") || "exl-prg-42";
if (roomCode) {
  const res = await fetch(`/api/rooms/${roomCode}`);
  if (res.ok) matchId = (await res.json()).match_id;
}
const lang = params.get("lang") || "en";
const viewer = params.get("viewer") || "analyst";
const $ = (id) => document.getElementById(id);
const esc = (text) => String(text ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
const X = (x) => x - 52.5;
const Z = (y) => y - 34;

const data = await fetch(`/api/matches/${matchId}/tracking`).then((r) => r.json());
const cueSheet = await fetch(`/api/matches/${matchId}/overlays?feed=${viewer}.${lang}`).then((r) => (r.ok ? r.json() : { cues: [] })).catch(() => ({ cues: [] }));
const teams = Object.fromEntries(data.teams.map((t) => [t.id, t]));
const homeId = data.home_team_id;
const awayId = data.teams.find((t) => t.id !== homeId).id;
const offsets = data.period_offsets;
const frames = data.frames;
const T_END = frames[frames.length - 1][0];
const goalEvents = data.events.filter((e) => e.type === "goal");
// In a watch room the lower thirds arrive with the room's moments; alone, they come from the recorded run.
const cues = roomCode ? [] : cueSheet.cues.map((c) => ({ ...c, t: c.match_clock_s + offsets[c.period] })).sort((a, b) => a.t - b.t);

// ------------------------------------------------------------------ renderer

const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.setSize(innerWidth, innerHeight);
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFSoftShadowMap;
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.outputColorSpace = THREE.SRGBColorSpace;
document.body.prepend(renderer.domElement);

const scene = new THREE.Scene();
scene.background = new THREE.Color("#0b1220");
scene.fog = new THREE.Fog("#0b1220", 170, 360);
const camera = new THREE.PerspectiveCamera(28, innerWidth / innerHeight, 0.5, 900);
const controls = new OrbitControls(camera, renderer.domElement);
controls.enabled = false;
controls.maxPolarAngle = Math.PI * 0.48;
addEventListener("resize", () => {
  camera.aspect = innerWidth / innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(innerWidth, innerHeight);
});

scene.add(new THREE.HemisphereLight("#cfe0ff", "#20301f", 0.85));
const sun = new THREE.DirectionalLight("#ffffff", 2.4);
sun.position.set(25, 90, 45);
sun.castShadow = true;
sun.shadow.mapSize.set(2048, 2048);
Object.assign(sun.shadow.camera, { left: -70, right: 70, top: 50, bottom: -50, near: 10, far: 220 });
sun.shadow.bias = -0.0005;
scene.add(sun);

// --------------------------------------------------------------------- pitch

const pitchTexture = new THREE.TextureLoader().load("pitch.png");
pitchTexture.colorSpace = THREE.SRGBColorSpace;
pitchTexture.anisotropy = renderer.capabilities.getMaxAnisotropy();
// Same mapping as the 2D pitch: the picture spans x -6.17..111.37 and y -2.74..70.93 in metres.
const pitch = new THREE.Mesh(new THREE.PlaneGeometry(117.54, 73.67), new THREE.MeshStandardMaterial({ map: pitchTexture, roughness: 0.95 }));
pitch.rotation.x = -Math.PI / 2;
pitch.position.set(X(-6.17 + 117.54 / 2), 0, Z(-2.74 + 73.67 / 2));
pitch.receiveShadow = true;
scene.add(pitch);
const apron = new THREE.Mesh(new THREE.PlaneGeometry(240, 180), new THREE.MeshStandardMaterial({ color: "#1d3d22", roughness: 1 }));
apron.rotation.x = -Math.PI / 2;
apron.position.y = -0.03;
apron.receiveShadow = true;
scene.add(apron);

const white = new THREE.MeshStandardMaterial({ color: "#f4f4f4", roughness: 0.4 });
for (const side of [-1, 1]) {
  const goal = new THREE.Group();
  const post = new THREE.CylinderGeometry(0.07, 0.07, 2.44, 12);
  for (const z of [-3.66, 3.66]) {
    const m = new THREE.Mesh(post, white);
    m.position.set(0, 1.22, z);
    m.castShadow = true;
    goal.add(m);
  }
  const bar = new THREE.Mesh(new THREE.CylinderGeometry(0.07, 0.07, 7.32, 12), white);
  bar.rotation.x = Math.PI / 2;
  bar.position.y = 2.44;
  bar.castShadow = true;
  goal.add(bar);
  const net = new THREE.Mesh(new THREE.BoxGeometry(2, 2.44, 7.32, 4, 6, 18),
    new THREE.MeshBasicMaterial({ color: "#ffffff", wireframe: true, transparent: true, opacity: 0.28 }));
  net.position.set(side * 1, 1.22, 0);
  goal.add(net);
  goal.position.x = side * 52.5;
  scene.add(goal);
}

// ------------------------------------------------------------------- stadium

function textTexture(width, height, draw) {
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  draw(canvas.getContext("2d"), width, height);
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  texture.anisotropy = 8;
  return texture;
}

const boardTexture = textTexture(2048, 64, (g, w, h) => {
  const words = ["PROMPT THE PITCH", "AI INSIGHTS", "MICROSOFT FOUNDRY", "AGENT FRAMEWORK"];
  const n = words.length;
  for (let i = 0; i < n; i++) {
    g.fillStyle = i % 2 ? "#0d5c46" : "#15171c";
    g.fillRect(i * w / n, 0, w / n, h);
    g.fillStyle = i % 2 ? "#ffffff" : "#9fd8b8";
    g.font = "bold 34px Arial";
    g.textAlign = "center";
    g.textBaseline = "middle";
    g.fillText(words[i], i * w / n + w / (2 * n), h / 2 + 1);
  }
});
boardTexture.wrapS = THREE.RepeatWrapping;
function board(length, x, z, rotY) {
  const texture = boardTexture.clone();
  texture.repeat.set(length / 110, 1);
  texture.needsUpdate = true;
  const m = new THREE.Mesh(new THREE.BoxGeometry(length, 0.9, 0.12), [
    new THREE.MeshBasicMaterial({ color: "#15171c" }), new THREE.MeshBasicMaterial({ color: "#15171c" }),
    new THREE.MeshBasicMaterial({ color: "#15171c" }), new THREE.MeshBasicMaterial({ color: "#15171c" }),
    new THREE.MeshBasicMaterial({ map: texture }), new THREE.MeshBasicMaterial({ color: "#15171c" }),
  ]);
  m.position.set(x, 0.45, z);
  m.rotation.y = rotY;
  scene.add(m);
}
board(112, 0, -38.5, 0);
board(112, 0, 38.5, Math.PI);
board(72, -57.5, 0, Math.PI / 2);
board(72, 57.5, 0, -Math.PI / 2);

const concrete = new THREE.MeshStandardMaterial({ color: "#3a3f48", roughness: 0.9 });
const seats = new THREE.MeshStandardMaterial({ color: "#232833", roughness: 0.8 });
const metal = new THREE.MeshStandardMaterial({ color: "#1a1d23", roughness: 0.6, metalness: 0.4 });
const ROWS = 18, ROW_DEPTH = 1.0, ROW_RISE = 0.62, FRONT = 5;
const spectators = []; // [x, y, z, colour, group]

function stand({ length, x, z, rotY, roof = true, fans }) {
  const g = new THREE.Group();
  for (let r = 0; r < ROWS; r++) {
    const step = new THREE.Mesh(new THREE.BoxGeometry(length, ROW_RISE * (r + 1) + 1, ROW_DEPTH), r % 2 ? seats : concrete);
    step.position.set(0, (ROW_RISE * (r + 1) + 1) / 2, FRONT + r * ROW_DEPTH);
    step.receiveShadow = true;
    g.add(step);
  }
  const back = new THREE.Mesh(new THREE.BoxGeometry(length, ROWS * ROW_RISE + 6, 0.6), concrete);
  back.position.set(0, (ROWS * ROW_RISE + 6) / 2, FRONT + ROWS * ROW_DEPTH);
  g.add(back);
  if (roof) {
    const top = new THREE.Mesh(new THREE.BoxGeometry(length + 2, 0.5, ROWS * ROW_DEPTH + 6), metal);
    top.position.set(0, ROWS * ROW_RISE + 9, FRONT + ROWS * ROW_DEPTH / 2 - 1);
    g.add(top);
    for (let i = -1; i <= 1; i += 2 / Math.max(1, Math.round(length / 40))) {
      const pillar = new THREE.Mesh(new THREE.CylinderGeometry(0.3, 0.3, ROWS * ROW_RISE + 9, 8), metal);
      pillar.position.set(i * length / 2, (ROWS * ROW_RISE + 9) / 2, FRONT + ROWS * ROW_DEPTH);
      g.add(pillar);
    }
  }
  g.position.set(x, 0, z);
  g.rotation.y = rotY;
  scene.add(g);
  g.updateMatrixWorld();
  const v = new THREE.Vector3();
  for (let r = 0; r < ROWS; r++) {
    for (let s = -length / 2 + 0.5; s < length / 2 - 0.4; s += 0.78) {
      if (Math.random() > 0.9) continue;
      v.set(s + (Math.random() - 0.5) * 0.2, ROW_RISE * (r + 1) + 1 + 0.45, FRONT + r * ROW_DEPTH + 0.25).applyMatrix4(g.matrixWorld);
      const [colour, group] = fans();
      spectators.push([v.x, v.y, v.z, colour, group]);
    }
  }
}

const NEUTRAL = ["#2b2f36", "#3d434d", "#c9c2b4", "#5a6170", "#8c8f96"];
const pick = (list) => list[Math.floor(Math.random() * list.length)];
function fansOf(homeShare) {
  return () => {
    const r = Math.random();
    if (r < 0.18) return [pick(NEUTRAL), 2];
    const team = Math.random() < homeShare ? teams[homeId] : teams[awayId];
    const colour = Math.random() < 0.72 ? team.colors[0] : team.colors[1];
    return [colour, team.id === homeId ? 0 : 1];
  };
}
// Home fans behind the goal the home side defends first, away fans at the other end.
stand({ length: 118, x: 0, z: -40, rotY: Math.PI, fans: fansOf(0.6) });
stand({ length: 118, x: 0, z: 40, rotY: 0, roof: false, fans: fansOf(0.6) });
stand({ length: 76, x: -59, z: 0, rotY: -Math.PI / 2, fans: fansOf(0.95) });
stand({ length: 76, x: 59, z: 0, rotY: Math.PI / 2, fans: fansOf(0.08) });

// The crowd: one instanced mesh, animated in the vertex shader (cheering waves, goal jumps).
const crowdUniforms = { uTime: { value: 0 }, uHome: { value: 0 }, uAway: { value: 0 }, uBase: { value: 0.04 } };
const crowdGeometry = new THREE.CapsuleGeometry(0.2, 0.5, 2, 6);
const phases = new Float32Array(spectators.length);
const groups = new Float32Array(spectators.length);
spectators.forEach((s, i) => { phases[i] = Math.random() * 10; groups[i] = s[4]; });
crowdGeometry.setAttribute("aPhase", new THREE.InstancedBufferAttribute(phases, 1));
crowdGeometry.setAttribute("aGroup", new THREE.InstancedBufferAttribute(groups, 1));
const crowdMaterial = new THREE.MeshLambertMaterial();
crowdMaterial.onBeforeCompile = (shader) => {
  Object.assign(shader.uniforms, crowdUniforms);
  shader.vertexShader = `attribute float aPhase;\nattribute float aGroup;\nuniform float uTime, uHome, uAway, uBase;\n${shader.vertexShader}`
    .replace("#include <begin_vertex>", `#include <begin_vertex>
      float excitement = aGroup < 0.5 ? uHome : (aGroup < 1.5 ? uAway : 0.35 * max(uHome, uAway));
      float speed = 4.0 + fract(aPhase) * 3.0;
      transformed.y += (uBase + excitement * 0.55) * max(0.0, sin(uTime * speed + aPhase * 6.2831));`);
};
const crowd = new THREE.InstancedMesh(crowdGeometry, crowdMaterial, spectators.length);
const m4 = new THREE.Matrix4();
const colour = new THREE.Color();
spectators.forEach(([x, y, z, c], i) => {
  crowd.setMatrixAt(i, m4.makeTranslation(x, y, z));
  crowd.setColorAt(i, colour.set(c));
});
scene.add(crowd);

// Floodlights.
for (const [x, z] of [[-66, -50], [66, -50], [-66, 50], [66, 50]]) {
  const mast = new THREE.Mesh(new THREE.CylinderGeometry(0.5, 0.8, 42, 8), metal);
  mast.position.set(x, 21, z);
  scene.add(mast);
  const lamp = new THREE.Mesh(new THREE.BoxGeometry(7, 4, 0.6), new THREE.MeshBasicMaterial({ color: "#fffbe8" }));
  lamp.position.set(x, 43, z);
  lamp.lookAt(0, 0, 0);
  scene.add(lamp);
}

// ------------------------------------------------------------------- players

const SKIN = ["#f1c7a5", "#d9a37e", "#b07a52", "#8a5a3c", "#5e3b26"];
const GK_COLORS = { [homeId]: "#9be15d", [awayId]: "#ff9f1c" };
const standard = (c) => new THREE.MeshStandardMaterial({ color: c, roughness: 0.7 });

function numberTexture(n, fg) {
  return textTexture(64, 64, (g, w, h) => {
    g.fillStyle = fg;
    g.font = "bold 46px Arial";
    g.textAlign = "center";
    g.textBaseline = "middle";
    g.fillText(String(n), w / 2, h / 2 + 2);
  });
}

function makePlayer(shirt, shorts, number, numberColour, skin) {
  const g = new THREE.Group();
  const shirtMat = standard(shirt), shortsMat = standard(shorts), skinMat = standard(skin), sockMat = standard(shorts);
  const parts = [];
  const add = (mesh, parent = g) => { mesh.castShadow = true; parent.add(mesh); parts.push(mesh); return mesh; };
  add(new THREE.Mesh(new THREE.CapsuleGeometry(0.21, 0.42, 4, 10), shirtMat)).position.y = 1.22;
  add(new THREE.Mesh(new THREE.CylinderGeometry(0.22, 0.2, 0.26, 10), shortsMat)).position.y = 0.86;
  add(new THREE.Mesh(new THREE.SphereGeometry(0.13, 14, 10), skinMat)).position.y = 1.73;
  const legs = [], arms = [];
  for (const side of [-1, 1]) {
    const hip = new THREE.Group();
    hip.position.set(side * 0.1, 0.8, 0);
    add(new THREE.Mesh(new THREE.BoxGeometry(0.13, 0.42, 0.14), skinMat), hip).position.y = -0.21;
    add(new THREE.Mesh(new THREE.BoxGeometry(0.12, 0.38, 0.13), sockMat), hip).position.y = -0.6;
    g.add(hip);
    legs.push(hip);
    const shoulder = new THREE.Group();
    shoulder.position.set(side * 0.29, 1.43, 0);
    add(new THREE.Mesh(new THREE.BoxGeometry(0.09, 0.52, 0.09), shirtMat), shoulder).position.y = -0.26;
    g.add(shoulder);
    arms.push(shoulder);
  }
  const back = new THREE.Mesh(new THREE.PlaneGeometry(0.3, 0.3), new THREE.MeshBasicMaterial({ map: numberTexture(number, numberColour), transparent: true }));
  back.position.set(0, 1.27, -0.215);
  back.rotation.y = Math.PI;
  g.add(back);
  g.scale.setScalar(1.15);
  scene.add(g);
  return { group: g, legs, arms, phase: Math.random() * 6, yaw: 0 };
}

const players = data.ids.map((id, i) => {
  const team = teams[data.team_of[i]];
  const gk = data.goalkeepers.includes(id);
  const shirt = gk ? GK_COLORS[team.id] : team.colors[0];
  const info = data.players[id];
  const light = new THREE.Color(shirt).getHSL({}).l > 0.7;
  const p = makePlayer(shirt, team.colors[1], info.number, light ? "#15171c" : "#ffffff", SKIN[(info.number * 7 + i) % SKIN.length]);
  p.group.visible = false;
  return { ...p, id, team: team.id, info, x: 52.5, y: 34 };
});
const referee = makePlayer("#111111", "#111111", "", "#111111", SKIN[1]);
const refPos = { x: 40, y: 30 };

const ball = new THREE.Mesh(new THREE.SphereGeometry(0.2, 20, 14), new THREE.MeshStandardMaterial({ color: "#ffffff", roughness: 0.35 }));
ball.castShadow = true;
scene.add(ball);

// ------------------------------------------------------------------ playback

const state = { t: 0, playing: true, speed: Number(params.get("speed") || 5), frame: 0, cam: "auto", cut: null, sound: true, target: null };
$("speed").value = String(state.speed);

function frameAt(t) {
  let i = state.frame;
  if (frames[i][0] > t) i = 0;
  while (i < frames.length - 2 && frames[i + 1][0] <= t) i++;
  state.frame = i;
  return i;
}

function sample(t) {
  const i = frameAt(t);
  const a = frames[i], b = frames[Math.min(i + 1, frames.length - 1)];
  const span = b[0] - a[0];
  const s = span > 0 ? clamp((t - a[0]) / span, 0, 1) : 0;
  const pos = [];
  for (let k = 0; k < data.ids.length; k++) {
    const ax = a[4][2 * k], bx = b[4][2 * k];
    if (ax == null && bx == null) { pos.push(null); continue; }
    if (ax == null) { pos.push([bx, b[4][2 * k + 1]]); continue; }
    if (bx == null || s === 0) { pos.push([ax, a[4][2 * k + 1]]); continue; }
    pos.push([ax + (bx - ax) * s, a[4][2 * k + 1] + (b[4][2 * k + 1] - a[4][2 * k + 1]) * s]);
  }
  const ballPos = [a[1] + (b[1] - a[1]) * s, a[2] + (b[2] - a[2]) * s, 0.2 + a[3] * 4 * s * (1 - s)];
  return { pos, ball: ballPos };
}

const period2Start = 45 * 60 + offsets["2"];
const period1End = period2Start - 30;
function clockAt(t) {
  if (t >= period1End && t < period2Start) return "HT";
  const period = t >= period2Start ? 2 : 1;
  const c = period === 2 ? t - offsets["2"] : t;
  const limit = period === 2 ? 90 * 60 : 45 * 60;
  if (c >= limit) return `${limit / 60}+${Math.floor((c - limit) / 60) + 1}'`;
  return `${Math.floor(c / 60) + 1}'`;
}
const periodAt = (t) => (t >= period2Start ? 2 : 1);
const attacksRight = (teamId, t) => (teamId === homeId) === (periodAt(t) === 1);

// ------------------------------------------------------------------- cameras

const CAMS = { auto: "Auto director", tv: "TV", wide: "Tactical", goal: "Behind the goal", follow: "Follow", free: "Free" };
for (const [key, name] of Object.entries(CAMS)) {
  const b = document.createElement("button");
  b.textContent = name;
  b.dataset.cam = key;
  b.onclick = () => setCam(key);
  $("cams").appendChild(b);
}
function setCam(key) {
  state.cam = key;
  state.cut = null;
  controls.enabled = key === "free";
  if (key === "free") controls.target.set(0, 0, 0);
  for (const b of $("cams").children) b.classList.toggle("on", b.dataset.cam === key);
  snapNext = true;
}
let snapNext = true;
const camTarget = new THREE.Vector3(), camLook = new THREE.Vector3(), lookNow = new THREE.Vector3();

function rig(kind, ball, carrier, now) {
  const [bx, bz] = [X(ball[0]), Z(ball[1])];
  switch (kind) {
    case "wide":
      return { pos: [bx * 0.3, 105, 34], look: [bx * 0.3, 0, 2], fov: 38 };
    case "goal": {
      const side = state.cut?.side ?? (bx >= 0 ? 1 : -1);
      return { pos: [side * 71, 9, bz * 0.25], look: [side * 38, 0, bz * 0.5], fov: 34 };
    }
    case "follow": {
      const dir = carrier && attacksRight(carrier.team, state.t) ? 1 : -1;
      return { pos: [bx - dir * 13, 6, bz + 7], look: [bx + dir * 8, 0.5, bz], fov: 46 };
    }
    case "celebrate": {
      const focus = state.cut?.player;
      const fx = focus ? X(focus.x) : bx, fz = focus ? Z(focus.y) : bz;
      const a = now * 0.45;
      return { pos: [fx + Math.cos(a) * 8, 3.2, fz + Math.sin(a) * 8], look: [fx, 1.3, fz], fov: 42 };
    }
    default:
      return { pos: [clamp(bx * 0.72, -36, 36), 31, 74], look: [clamp(bx * 0.9, -46, 46), 0, bz * 0.35 - 3], fov: 25 };
  }
}

function currentCam() {
  if (state.cam !== "auto") return state.cam;
  if (state.cut && performance.now() < state.cut.until) return state.cut.kind;
  state.cut = null;
  return "tv";
}

function direct(event, sampled) {
  if (state.cam !== "auto") return;
  const now = performance.now();
  const hold = (seconds) => now + seconds * 1000 * Math.min(1, 10 / state.speed) + 600;
  const goalSide = (teamId) => (attacksRight(teamId, event.t) ? 1 : -1);
  if (event.type === "goal") {
    const scorer = players.find((p) => p.id === event.player_id);
    state.cut = { kind: "celebrate", until: hold(7), player: scorer };
  } else if (event.type === "shot" && event.result !== "goal") {
    state.cut = { kind: "goal", until: hold(4), side: goalSide(event.team_id) };
  } else if (event.type === "corner") {
    state.cut = { kind: "goal", until: hold(5), side: goalSide(event.team_id) };
  } else if (event.type === "card" || event.type === "penalty_awarded") {
    state.cut = { kind: "follow", until: hold(4) };
  } else if (event.type === "kickoff" && event.t > 1) {
    state.cut = { kind: "wide", until: hold(3) };
  } else {
    return;
  }
  snapNext = true;
}

// ---------------------------------------------------------- lower thirds

let overlayTimer = null;
function lowerThird({ kicker, title, body, chips = [], teamId }) {
  const box = $("overlay");
  const colour = teamId ? teams[teamId].colors[0] : "#0d5c46";
  const figures = chips.length ? `<div class="figures">${chips.map((c) => `${esc(c.label)} <b>${esc(c.value)}</b>`).join('<span class="sep">·</span>')}</div>` : "";
  box.innerHTML = `<div class="l3"><div style="background:${colour}"></div><div class="content">
    <div class="kicker">${esc(kicker)}</div><div class="title">${esc(title)}</div>${body ? `<div class="body">${esc(body)}</div>` : ""}${figures}</div></div>`;
  clearTimeout(overlayTimer);
  overlayTimer = setTimeout(() => { box.innerHTML = ""; }, 9000 * Math.min(1, 10 / state.speed) + 2500);
}

function caption(e) {
  const p = data.players[e.player_id];
  const name = p ? `${p.number} ${p.name}` : "";
  const team = teams[e.team_id];
  if (e.type === "goal") {
    const score = scoreAt(e.t);
    return { kicker: `${e.clock} · Goal`, title: `${p?.name ?? ""} scores!`, body: `${teams[homeId].name} ${score.home}-${score.away} ${teams[awayId].name}`, teamId: e.team_id };
  }
  if (e.type === "card") return { kicker: `${e.clock} · ${e.card === "yellow" ? "Yellow card" : "Red card"}`, title: name, teamId: e.team_id };
  if (e.type === "substitution") {
    const on = data.players[e.player_in_id];
    return { kicker: `${e.clock} · Substitution · ${team?.short_name ?? ""}`, title: `${on?.name ?? ""} on`, body: `${p?.name ?? ""} off`, teamId: e.team_id };
  }
  return null;
}

function scoreAt(t) {
  const goals = goalEvents.filter((g) => g.t <= t);
  return { home: goals.filter((g) => g.team_id === homeId).length, away: goals.filter((g) => g.team_id === awayId).length };
}

// --------------------------------------------------------------------- sound

// Everything is synthesised with Web Audio: a crowd bed that swells near the goals, roars on
// goals, an "ooh" on chances, and the referee's whistle.
let audio = null;
function brownNoise(ctx, seconds) {
  const buffer = ctx.createBuffer(1, ctx.sampleRate * seconds, ctx.sampleRate);
  const out = buffer.getChannelData(0);
  let last = 0;
  for (let i = 0; i < out.length; i++) { last = (last + 0.02 * (Math.random() * 2 - 1)) / 1.02; out[i] = last * 3.5; }
  const source = ctx.createBufferSource();
  source.buffer = buffer;
  source.loop = true;
  return source;
}
function startSound() {
  const ctx = new AudioContext();
  const master = ctx.createGain();
  master.gain.value = 0.9;
  master.connect(ctx.destination);
  const crowd = ctx.createGain();
  crowd.gain.value = 0.2;
  crowd.connect(master);
  // Two layers of voices (low murmur, brighter shouts), slowly breathing.
  for (const [freq, q, level, rate] of [[420, 0.7, 1, 0.07], [1300, 1.2, 0.45, 0.13]]) {
    const noise = brownNoise(ctx, 4);
    const filter = ctx.createBiquadFilter();
    filter.type = "bandpass";
    filter.frequency.value = freq;
    filter.Q.value = q;
    const layer = ctx.createGain();
    layer.gain.value = level;
    const lfo = ctx.createOscillator();
    lfo.frequency.value = rate;
    const depth = ctx.createGain();
    depth.gain.value = level * 0.3;
    lfo.connect(depth).connect(layer.gain);
    noise.connect(filter).connect(layer).connect(crowd);
    noise.start();
    lfo.start();
  }
  const shoutFilter = ctx.createBiquadFilter();
  shoutFilter.type = "bandpass";
  shoutFilter.frequency.value = 900;
  shoutFilter.Q.value = 1.5;
  const shout = ctx.createGain();
  shout.gain.value = 0;
  const shoutNoise = brownNoise(ctx, 3);
  shoutNoise.connect(shoutFilter).connect(shout).connect(master);
  shoutNoise.start();
  audio = { ctx, master, crowd, shout, shoutFilter };
}
function roar(level, seconds, rise = false) {
  if (!audio) return;
  const now = audio.ctx.currentTime, g = audio.shout.gain, f = audio.shoutFilter.frequency;
  g.cancelScheduledValues(now);
  g.setTargetAtTime(level, now, 0.12);
  g.setTargetAtTime(0, now + seconds, 1.0);
  f.cancelScheduledValues(now);
  f.setValueAtTime(rise ? 650 : 900, now);
  if (rise) f.linearRampToValueAtTime(1150, now + 0.6); // The "ooh" of a near miss.
}
function crowdLevel(level) {
  if (audio) audio.crowd.gain.setTargetAtTime(level, audio.ctx.currentTime, 0.8);
}
function whistle(blasts) {
  if (!audio || !state.sound) return;
  const ctx = audio.ctx;
  let t0 = ctx.currentTime + 0.02;
  for (const d of blasts) {
    const tone = ctx.createOscillator();
    tone.frequency.value = 3050;
    const trill = ctx.createOscillator(); // The pea in the whistle.
    trill.frequency.value = 27;
    const depth = ctx.createGain();
    depth.gain.value = 160;
    trill.connect(depth).connect(tone.frequency);
    const g = ctx.createGain();
    g.gain.setValueAtTime(0, t0);
    g.gain.linearRampToValueAtTime(0.16, t0 + 0.02);
    g.gain.setValueAtTime(0.16, t0 + d - 0.05);
    g.gain.linearRampToValueAtTime(0, t0 + d);
    tone.connect(g).connect(audio.master);
    for (const o of [tone, trill]) { o.start(t0); o.stop(t0 + d + 0.05); }
    t0 += d + 0.14;
  }
}
const WHISTLES = { foul: [0.32], offside: [0.32], penalty_awarded: [0.55], kickoff: [0.5] };
function refereeWhistle(e) {
  if (e.type === "period_end") whistle(e.period === 1 ? [0.3, 0.9] : [0.3, 0.3, 1.1]);
  else if (WHISTLES[e.type]) whistle(WHISTLES[e.type]);
}

// --------------------------------------------------------------------- loop

let eventIndex = 0, cueIndex = 0, lastReal = performance.now(), lastNearGoal = false;
const tmp = new THREE.Vector3();

function seek(t) {
  state.t = clamp(t, 0, T_END);
  state.frame = 0;
  eventIndex = data.events.findIndex((e) => e.t > state.t);
  if (eventIndex < 0) eventIndex = data.events.length;
  cueIndex = cues.findIndex((c) => c.t > state.t);
  if (cueIndex < 0) cueIndex = cues.length;
  snapNext = true;
}

function animate(now) {
  const dtReal = Math.min(0.1, (now - lastReal) / 1000);
  lastReal = now;
  const previous = state.t;
  if (state.playing && (!roomCode || state.target != null)) {
    // In a room the clock runs at the room's speed but never far ahead of the last event received.
    const limit = roomCode ? Math.min(T_END, state.target + 2.5) : T_END;
    state.t = Math.min(limit, state.t + dtReal * state.speed);
  }
  const dtGame = state.t - previous;
  const sampled = sample(state.t);

  // Players.
  let carrier = null, carrierDist = 2.5;
  players.forEach((p, k) => {
    const pos = sampled.pos[k];
    p.group.visible = !!pos;
    if (!pos) return;
    const [x, y] = pos;
    const vx = dtGame > 0 ? (x - p.x) / dtGame : 0, vy = dtGame > 0 ? (y - p.y) / dtGame : 0;
    const speed = Math.hypot(vx, vy);
    p.x = x; p.y = y;
    p.group.position.set(X(x), 0, Z(y));
    const d = Math.hypot(x - sampled.ball[0], y - sampled.ball[1]);
    if (d < carrierDist) { carrier = p; carrierDist = d; }
    const yawTarget = speed > 0.8 ? Math.atan2(vx, vy) : Math.atan2(sampled.ball[0] - x, sampled.ball[1] - y);
    let diff = yawTarget - p.yaw;
    diff = Math.atan2(Math.sin(diff), Math.cos(diff));
    p.yaw += diff * Math.min(1, dtReal * 8);
    p.group.rotation.y = p.yaw;
    const stride = Math.min(0.85, speed * 0.11);
    p.phase += Math.min(0.5, speed * dtGame * 1.6);
    p.legs[0].rotation.x = Math.sin(p.phase) * stride;
    p.legs[1].rotation.x = -Math.sin(p.phase) * stride;
    p.arms[0].rotation.x = -Math.sin(p.phase) * stride * 0.8;
    p.arms[1].rotation.x = Math.sin(p.phase) * stride * 0.8;
  });

  // The referee trails the play.
  const rx = sampled.ball[0] - 9, ry = clamp(sampled.ball[1] + 8, 4, 64);
  const rStep = Math.min(1, Math.max(dtGame, 0) * 0.8);
  refPos.x += (rx - refPos.x) * rStep;
  refPos.y += (ry - refPos.y) * rStep;
  referee.group.position.set(X(refPos.x), 0, Z(refPos.y));
  referee.group.rotation.y = Math.atan2(sampled.ball[0] - refPos.x, sampled.ball[1] - refPos.y);

  ball.position.set(X(sampled.ball[0]), sampled.ball[2], Z(sampled.ball[1]));

  // Events crossed since the last frame: score, captions, director, crowd.
  while (eventIndex < data.events.length && data.events[eventIndex].t <= state.t) {
    const e = data.events[eventIndex++];
    if (state.t - e.t > 20) continue; // Skipped over (seek or high speed).
    direct(e, sampled);
    const cap = caption(e);
    if (cap && !(e.type === "goal" && (roomCode || cues.some((c) => Math.abs(c.t - e.t) < 30 && c.moment_type === "goal")))) lowerThird(cap);
    refereeWhistle(e);
    if (e.type === "goal") {
      const key = e.team_id === homeId ? "uHome" : "uAway";
      crowdUniforms[key].value = 1;
      setTimeout(() => { crowdUniforms[key].value = 0; }, 9000);
      roar(1.1, 6);
    } else if (e.type === "shot" && e.result !== "goal") {
      roar(0.55, 1.4, true);
    } else if (e.type === "save") {
      roar(0.4, 1.2, true);
    }
  }
  while (cueIndex < cues.length && cues[cueIndex].t <= state.t) {
    const c = cues[cueIndex++];
    if (state.t - c.t > 40) continue;
    lowerThird({ kicker: `${c.display_clock}${c.highlight?.player_id && data.players[c.highlight.player_id] ? ` · ${data.players[c.highlight.player_id].number} ${data.players[c.highlight.player_id].name}` : ""}`,
      title: c.title, body: c.body, chips: c.stat_chips, teamId: c.highlight?.team_id });
  }
  crowdUniforms.uTime.value = now / 1000;
  const nearGoal = Math.abs(X(sampled.ball[0])) > 35;
  crowdUniforms.uBase.value = nearGoal ? 0.12 : 0.04; // Livelier near the goals.
  if (nearGoal !== lastNearGoal) { crowdLevel(nearGoal ? 0.32 : 0.2); lastNearGoal = nearGoal; }

  // Camera.
  const kind = currentCam();
  if (kind !== "free") {
    const r = rig(kind, sampled.ball, carrier, now / 1000);
    camTarget.set(...r.pos);
    camLook.set(...r.look);
    const k = snapNext ? 1 : Math.min(1, dtReal * (kind === "celebrate" ? 6 : 2.5));
    camera.position.lerp(camTarget, k);
    lookNow.lerp(camLook, k);
    camera.lookAt(lookNow);
    if (camera.fov !== r.fov) { camera.fov = r.fov; camera.updateProjectionMatrix(); }
    snapNext = false;
  } else {
    controls.update();
  }
  $("cam-name").textContent = kind === "celebrate" ? "Replay cam" : CAMS[kind] ?? "";

  // Name of the player on the ball.
  const label = $("label");
  if (carrier && kind !== "wide") {
    tmp.set(X(carrier.x), 2.6, Z(carrier.y)).project(camera);
    label.hidden = tmp.z > 1;
    label.style.left = `${(tmp.x * 0.5 + 0.5) * innerWidth}px`;
    label.style.top = `${(-tmp.y * 0.5 + 0.5) * innerHeight}px`;
    label.style.borderLeftColor = teams[carrier.team].colors[0];
    label.textContent = `${carrier.info.number} ${carrier.info.name}`;
  } else {
    label.hidden = true;
  }

  // Scorebug and timeline.
  const score = scoreAt(state.t);
  $("score-home").textContent = score.home;
  $("score-away").textContent = score.away;
  $("clock").textContent = clockAt(state.t);
  if (!seeking) $("seek").value = String(Math.round(state.t));

  renderer.render(scene, camera);
  requestAnimationFrame(animate);
}

// ------------------------------------------------------------------ controls

const home = teams[homeId], away = teams[awayId];
$("home-name").textContent = home.name;
$("away-name").textContent = away.name;
$("home-sw").style.background = home.colors[0];
$("away-sw").style.background = away.colors[0];
$("play").onclick = () => { state.playing = !state.playing; $("play").textContent = state.playing ? "Pause" : "Play"; };
$("speed").onchange = (e) => { state.speed = Number(e.target.value); };
let seeking = false;
$("seek").max = String(Math.ceil(T_END));
$("seek").oninput = (e) => { seeking = true; seek(Number(e.target.value)); };
$("seek").onchange = () => { seeking = false; };
for (const g of goalEvents) {
  const b = document.createElement("button");
  b.textContent = `⚽ ${g.clock} ${data.players[g.player_id]?.name ?? ""}`;
  b.onclick = () => seek(g.t - 12);
  $("goals").appendChild(b);
}
// Browsers only allow sound after a click: the stadium wakes up on the first interaction.
$("sound").textContent = "Click for sound";
$("sound").onclick = (event) => {
  event.stopPropagation();
  if (!audio) { startSound(); state.sound = true; }
  else state.sound = !state.sound;
  if (state.sound) audio.ctx.resume(); else audio.ctx.suspend();
  $("sound").textContent = state.sound ? "Sound on" : "Sound off";
};
const wake = (event) => {
  if (event.target === $("sound") || audio || !state.sound) return; // The button handles its own click.
  startSound();
  $("sound").textContent = "Sound on";
};
addEventListener("pointerdown", wake);
addEventListener("keydown", wake);

if (roomCode) {
  for (const id of ["play", "speed", "seek", "goals"]) ($(id).closest(".group") ?? $(id)).hidden = true;
  const note = document.createElement("span");
  note.className = "label-txt";
  note.textContent = `Following watch room ${roomCode.toUpperCase()}`;
  $("bar").prepend(note);
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const query = new URLSearchParams({ room: roomCode, observer: "1", languages: lang, source: "cached" });
  const ws = new WebSocket(`${proto}://${location.host}/ws/matches/${matchId}?${query}`);
  ws.onmessage = (m) => {
    const msg = JSON.parse(m.data);
    if (msg.type === "event") {
      const t = msg.event.clock_s + offsets[msg.event.period];
      state.target = Math.max(state.target ?? 0, t);
      if (Math.abs(state.t - t) > 8) seek(t);
    } else if (msg.type === "status") {
      state.speed = msg.speed;
      state.playing = msg.state === "playing";
    } else if (msg.type === "moment") {
      const card = msg.moment.overlays[`${viewer}.${lang}`];
      if (!card) return;
      const p = data.players[card.highlight_player_id];
      lowerThird({ kicker: `${msg.moment.signal.display_clock}${p ? ` · ${p.number} ${p.name}` : ""}`, title: card.title,
        body: card.body, chips: card.stat_chips, teamId: card.highlight_team_id || msg.moment.signal.team_id });
    }
  };
}
addEventListener("keydown", (e) => { if (e.code === "Space") $("play").click(); });

setCam("auto");
seek(Number(params.get("t") || 0));
$("loading").remove();
$("bug").hidden = false;
$("bar").hidden = false;
requestAnimationFrame(animate);
