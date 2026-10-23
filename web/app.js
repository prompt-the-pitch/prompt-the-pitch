// Prompt the Pitch: broadcast companion.
// Receives the match stream of a watch room over a WebSocket and renders the pitch, the agents'
// overlays and analyst graphics, key moments and player check-ins for the selected viewer, player
// and language, win probabilities, live stats, the stands (support, predictions, proposals,
// ratings), spoken commentary and the analyst chat.

import { LANGUAGE_NAMES, setLanguage, t } from "./i18n.js";

const $ = (id) => document.getElementById(id);
const SVG_NS = "http://www.w3.org/2000/svg";
// An overlay waiting too long in the queue is old news and is dropped. Recorded runs arrive exactly
// on time, so lateness is measured in match time there. Live agents need several seconds per moment,
// so a live overlay is judged on how long it has waited since it arrived.
const STALE_MATCH_S = 150;
const STALE_LIVE_WAIT_MS = 20000;
const LIVE_SPEED = 10;
const REPORT_SECONDS = 8;
const GRAPHIC_SECONDS = 15;
const FORECAST_RUNS = 120;
const CATCH_UP_AFTER_S = 180; // Match seconds missed before offering a catch-up.
const TACTICS = ["high_press", "low_block", "go_direct", "possession_control"];

const S = {
  matches: [], clubs: [], health: null,
  ws: null, state: "idle", source: "cached", speed: 30,
  room: null, social: null, my: { support: null, prediction: null, rating: null, votes: {} },
  match: null, teams: {}, players: {}, profiles: [], role: null, lang: "en", focusPlayer: null,
  snapshot: null, moments: [], recaps: [], reports: [], forecasts: [], trail: [], lastEvent: null,
  overlayQueue: [], overlayCurrent: null, overlayTimer: null, graphicTimer: null,
  networkTeam: null, listen: false, audioQueue: [], audioBusy: false, hiddenAt: null,
};

// ---------------------------------------------------------------- helpers

function esc(text) {
  return String(text ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function svg(tag, attrs = {}, parent) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  if (parent) parent.appendChild(el);
  return el;
}

const pct = (v) => `${Math.round((v ?? 0) * 100)}%`;
const teamColor = (teamId) => S.teams[teamId]?.colors?.[0] ?? "#7d828c";
const profileId = () => (S.role ? `${S.role}.${S.lang}` : null);
const profile = () => S.profiles.find((p) => p.id === profileId());
const roles = () => [...new Map(S.profiles.map((p) => [p.role, p])).values()];
const languages = () => [...new Set(S.profiles.map((p) => p.language))];
const homeId = () => S.match?.home_team_id;
const awayId = () => S.match?.away_team_id;
const matchSeconds = (x) => x.period * 10000 + x.clock_s;
const isHost = () => !S.room || S.room.host;

function figures(chips) {
  if (!chips?.length) return "";
  return `<div class="figures">${chips.map((c) => `${esc(c.label)} <b>${esc(c.value)}</b>`).join('<span class="sep">·</span>')}</div>`;
}

function roleLabel(r) {
  if (r.mode === "analyst" && !r.favorite_team_id) return t("roleAnalyst");
  if (r.player_focus && S.players[r.favorite_player_id]) return t("roleFocus", { player: S.players[r.favorite_player_id].short_name });
  if (r.favorite_team_id && S.teams[r.favorite_team_id]) return t("roleFan", { team: S.teams[r.favorite_team_id].name });
  return r.label;
}

function impactLine(signal) {
  const impact = signal.data?.impact;
  if (!impact) return "";
  const scenario = { "without this goal": "sc_without_goal", "had the chance been scored": "sc_if_scored",
    "with eleven players": "sc_eleven" }[impact.scenario];
  return t("impactLine", { team: impact.team, now: pct(impact.win_chance), alt: pct(impact.win_chance_if), scenario: scenario ? t(scenario) : impact.scenario });
}

// ------------------------------------------------------------------- init

async function init() {
  setLanguage(S.lang);
  drawPitch();
  drawMiniPitch($("shots-map"));
  drawMiniPitch($("network-map"));
  const [health, matches, clubs] = await Promise.all([
    fetch("/api/health").then((r) => r.json()).catch(() => null),
    fetch("/api/matches").then((r) => r.json()),
    fetch("/api/clubs").then((r) => r.json()),
  ]);
  Object.assign(S, { health, matches, clubs });
  renderMatchSelect();
  for (const id of ["nm-home", "nm-away"]) $(id).innerHTML = clubs.map((c) => `<option value="${c.id}">${esc(c.name)}</option>`).join("");
  $("nm-away").selectedIndex = 1;

  $("play-btn").onclick = onPlay;
  $("speed-select").onchange = (e) => { S.speed = Number(e.target.value); send({ type: "speed", value: S.speed }); updateModeNote(); };
  $("source-select").onchange = onSourceChange;
  $("match-select").onchange = updateSourceOptions;
  $("new-match-btn").onclick = () => $("match-dialog").showModal();
  $("match-form").onsubmit = onNewMatch;
  $("follow-select").onchange = (e) => follow(e.target.value);
  $("chat-form").onsubmit = onAsk;
  $("listen-btn").onclick = toggleListen;
  $("invite-btn").onclick = invite;
  $("propose-form").onsubmit = onPropose;
  $("pr-team").onchange = fillProposeForm;
  $("pr-kind").onchange = fillProposeForm;
  document.addEventListener("visibilitychange", onVisibility);
  renderTexts();

  const code = new URLSearchParams(location.search).get("room");
  if (code) joinRoom(code.toUpperCase());
}

// Everything that depends on the interface language.
function renderTexts() {
  setLanguage(S.lang);
  $("footer").textContent = S.health?.foundry ? t("footerOn", { model: S.health.model }) : t("footerOff");
  $("listen-btn").textContent = S.listen ? t("mute") : t("listen");
  setState(S.state);
  updateSourceOptions();
  renderViewer();
  renderFollowSelect();
  if (S.snapshot) onSnapshot(S.snapshot);
  renderWinbar();
  renderWinChart();
  renderSocial();
  fillProposeForm();
  renderAgents();
  renderFeed();
}

function renderMatchSelect(selected) {
  selected ??= S.matches.find((m) => m.cached_run)?.match_id;
  $("match-select").innerHTML = S.matches.map((m) =>
    `<option value="${m.match_id}" ${m.match_id === selected ? "selected" : ""}>${esc(m.home)} v ${esc(m.away)}</option>`).join("");
  updateSourceOptions();
}

function updateSourceOptions() {
  const m = S.matches.find((x) => x.match_id === $("match-select").value);
  const recorded = $("source-select").querySelector('option[value="cached"]');
  recorded.disabled = !!m && !m.cached_run;
  recorded.textContent = m?.cached_run || !m ? t("recorded") : t("recordedNone");
  if (m && !m.cached_run && $("source-select").value !== "live") { $("source-select").value = "live"; onSourceChange(); }
  $("cue-link").hidden = !m?.cached_run;
  if (m) $("cue-link").href = `/api/matches/${m.match_id}/overlays`;
  updateModeNote();
}

function onSourceChange() {
  // Live agents take a few seconds per moment: at 10x that is about a match minute, so overlays keep up.
  if ($("source-select").value === "live" && Number($("speed-select").value) > LIVE_SPEED) {
    $("speed-select").value = String(LIVE_SPEED);
  }
  updateModeNote();
}

function updateModeNote() {
  const live = $("source-select").value === "live";
  const speed = Number($("speed-select").value);
  $("mode-note").textContent = !live ? "" : speed > LIVE_SPEED ? t("liveSlowNote", { speed }) : t("liveNote");
}

async function onNewMatch(e) {
  if (e.submitter?.value !== "ok") return;
  const form = new FormData(e.target);
  const body = { home: form.get("home"), away: form.get("away"), seed: Number(form.get("seed")) };
  const res = await fetch("/api/matches", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!res.ok) { alert((await res.json()).detail); return; }
  const { match_id } = await res.json();
  S.matches = await fetch("/api/matches").then((r) => r.json());
  renderMatchSelect(match_id);
}

// ------------------------------------------------------------- connection

function send(message) {
  if (S.ws?.readyState === WebSocket.OPEN) S.ws.send(JSON.stringify(message));
}

function onPlay() {
  if (S.state === "playing") return send({ type: "pause" });
  if (S.state === "paused") return send({ type: "resume" });
  connect();
}

async function joinRoom(code) {
  const res = await fetch(`/api/rooms/${code}`);
  if (!res.ok) { $("mode-note").textContent = t("roomClosed"); return; }
  const info = await res.json();
  $("match-select").value = info.match_id;
  $("source-select").value = info.source;
  $("mode-note").textContent = t("joining", { code });
  connect(code);
}

function connect(roomCode = null) {
  S.ws?.close();
  Object.assign(S, { moments: [], recaps: [], reports: [], forecasts: [], trail: [], snapshot: null, lastEvent: null,
    overlayQueue: [], overlayCurrent: null, focusPlayer: null, room: null, social: null,
    my: { support: null, prediction: null, rating: null, votes: {} }, audioQueue: [] });
  clearTimeout(S.overlayTimer);
  hideGraphic();
  $("overlay").innerHTML = ""; $("chat").innerHTML = ""; $("agents").innerHTML = ""; $("agent-summary").textContent = "";
  $("catchup").hidden = true;
  $("feed").innerHTML = `<p class="empty">${t("emptyKickoff")}</p>`;
  drawPitch();
  S.source = $("source-select").value;
  S.speed = Number($("speed-select").value);
  const id = $("match-select").value;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const params = new URLSearchParams({ source: S.source, speed: S.speed, languages: S.lang });
  if (roomCode) params.set("room", roomCode);
  const ws = new WebSocket(`${proto}://${location.host}/ws/matches/${id}?${params}`);
  S.ws = ws;
  ws.onmessage = (e) => handle(JSON.parse(e.data));
  ws.onclose = () => { if (S.ws === ws && S.state !== "ended") setState("idle"); };
}

function setState(state) {
  S.state = state;
  $("play-btn").textContent = t({ idle: "kickoff", playing: "pause", paused: "resume", ended: "replay" }[state]);
  const guest = S.room && !S.room.host && state !== "ended" && state !== "idle";
  $("play-btn").disabled = !!guest;
  $("speed-select").disabled = !!guest;
  $("play-btn").title = guest ? t("hostOnly") : "";
}

function handle(msg) {
  switch (msg.type) {
    case "room": return onRoom(msg);
    case "match": return onMatch(msg);
    case "status": return setState(msg.state);
    case "event": return onEvent(msg.event);
    case "snapshot": return onSnapshot(msg.snapshot);
    case "signal": $("live-line").textContent = S.lang === "en" ? `${msg.signal.display_clock}  ${msg.signal.headline}` : ""; return;
    case "moment": return onMoment(msg.moment);
    case "recap": return onRecap(msg.recap);
    case "player_report": return onReport(msg.report);
    case "forecast": return onForecast(msg.forecast);
    case "social": S.social = msg.social; return renderSocial();
    case "catch_up": return onCatchUp(msg);
    case "answer": return onAnswer(msg);
    case "profiles": S.profiles = msg.profiles; return renderViewer();
    case "notice": $("mode-note").textContent = msg.message; S.source = "cached"; return;
    case "error": return alert(msg.message);
  }
}

// Broadcast overlay (OBS browser source) for this room, viewer, language and followed player.
function updateObsLink() {
  const link = $("obs-link");
  link.hidden = !S.room;
  $("stadium-link").hidden = !S.room;
  if (!S.room) return;
  const query = new URLSearchParams({ room: S.room.code, lang: S.lang, viewer: S.role ?? "analyst", preview: "1" });
  if (S.focusPlayer) query.set("player", S.focusPlayer);
  link.href = `broadcast.html?${query}`;
  const stadium = $("stadium-link");
  stadium.hidden = false;
  stadium.href = `stadium.html?${new URLSearchParams({ room: S.room.code, lang: S.lang, viewer: S.role ?? "analyst" })}`;
}

function onRoom(msg) {
  const firstJoin = !S.room;
  S.room = { code: msg.code, you: msg.you, nickname: msg.nickname, host: msg.host };
  // Someone joining a room that is already playing is offered a catch-up once the match is loaded.
  if (firstJoin && !msg.host) S.joinedLate = true;
  updateModeNote();
  history.replaceState(null, "", `?room=${msg.code}`);
  $("invite-btn").disabled = false;
  setState(S.state);
  renderSocial();
  updateObsLink();
}

function onMatch(msg) {
  const lateJoin = !!S.match && S.match.match_id === msg.match.match_id;
  S.match = msg.match;
  S.source = msg.source;
  S.teams = Object.fromEntries(msg.match.teams.map((team) => [team.id, team]));
  S.players = Object.fromEntries(msg.match.teams.flatMap((team) => team.players.map((p) => [p.id, { ...p, team_id: team.id }])));
  S.profiles = msg.profiles;
  if (!roles().some((r) => r.role === S.role)) S.role = roles()[0]?.role;
  if (!languages().includes(S.lang)) { S.lang = languages()[0] ?? "en"; setLanguage(S.lang); }
  if (!lateJoin) S.focusPlayer = roles().find((r) => r.player_focus)?.favorite_player_id ?? null;
  S.networkTeam = S.networkTeam && S.teams[S.networkTeam] ? S.networkTeam : homeId();
  const home = S.teams[homeId()], away = S.teams[awayId()];
  const bug = $("scorebug");
  for (const [side, team] of [["home", home], ["away", away]]) {
    const name = bug.querySelector(`.${side} .name`);
    name.removeAttribute("data-i18n"); // The team name replaces the translated placeholder.
    name.innerHTML = `<span class="full">${esc(team.name)}</span><span class="short">${esc(team.short_name)}</span>`;
    bug.querySelector(`.${side} .swatch`).style.background = team.colors[0];
  }
  $("winbar").querySelector(".home").style.background = home.colors[0];
  $("winbar").querySelector(".away").style.background = away.colors[0];
  $("score-home").textContent = "0"; $("score-away").textContent = "0";
  setState(S.state);
  renderViewer();
  renderFollowSelect();
  renderNetworkTabs();
  fillProposeForm();
}

// ------------------------------------------------------------------ pitch

// The pitch picture (600 x 390 px) has its touchlines at x 31.5-567.5 px and y 14.5-374.5 px.
// Placed at these metre coordinates, its lines match the 105 x 68 m frame of the match data.
const PITCH_IMAGE = { href: "pitch.png", x: -6.17, y: -2.74, width: 117.54, height: 73.67 };

function drawPitchImage(p) {
  svg("image", { ...PITCH_IMAGE, preserveAspectRatio: "none" }, p);
}

function drawPitch() {
  const p = $("pitch");
  p.innerHTML = "";
  drawPitchImage(p);
  svg("g", { id: "layer-marks" }, p);
  svg("polyline", { id: "trail", fill: "none", stroke: "rgba(255,255,255,0.4)", "stroke-width": 0.3, "stroke-linejoin": "round" }, p);
  svg("g", { id: "layer-graphic" }, p);
  svg("circle", { id: "focus-ring", r: 2.4, fill: "none", stroke: "#fff", "stroke-width": 0.45, "stroke-dasharray": "1 0.8", opacity: 0 }, p);
  svg("circle", { id: "ball", cx: 52.5, cy: 34, r: 1, fill: "#fff" }, p);
  const label = svg("g", { id: "carrier", opacity: 0 }, p);
  svg("rect", { id: "carrier-bg", x: 0, y: 0, width: 1, height: 3.4, fill: "#fff" }, label);
  svg("rect", { id: "carrier-team", x: 0, y: 0, width: 0.8, height: 3.4, fill: "#000" }, label);
  svg("text", { id: "carrier-text", x: 1.6, y: 2.5, "font-size": 2.2, fill: "#15171c", "font-family": "Inter, sans-serif", "font-weight": 600 }, label);
}

function drawMiniPitch(el) {
  el.innerHTML = "";
  drawPitchImage(el);
  svg("g", { class: "content" }, el);
}

function onEvent(ev) {
  S.lastEvent = { period: ev.period, clock_s: ev.clock_s };
  $("clock").textContent = ev.clock;
  if (ev.x == null) return;
  const x = ev.end_x ?? ev.x, y = ev.end_y ?? ev.y;
  const marks = $("layer-marks");
  if (ev.type === "pass" && ev.end_x != null) {
    fade(svg("line", { x1: ev.x, y1: ev.y, x2: ev.end_x, y2: ev.end_y, stroke: "#fff", "stroke-width": 0.25,
      opacity: 0.55, "stroke-dasharray": ev.outcome === "success" ? "" : "0.8 0.8" }, marks), 1500);
  }
  if (ev.type === "shot") {
    fade(svg("line", { x1: ev.x, y1: ev.y, x2: ev.end_x, y2: ev.end_y, stroke: "#fff", "stroke-width": 0.6 }, marks), 5000);
  }
  if (ev.type === "goal") {
    fade(svg("circle", { cx: ev.x > 52.5 ? 105 : 0, cy: 34, r: 2, fill: "none", stroke: "#fff", "stroke-width": 0.6, class: "goal-ring" }, marks), 4000);
  }
  S.trail = [...S.trail, `${x},${y}`].slice(-10);
  $("trail").setAttribute("points", S.trail.join(" "));
  $("ball").setAttribute("cx", x); $("ball").setAttribute("cy", y);

  const player = S.players[ev.type === "pass" && ev.outcome === "success" ? ev.details.recipient_id : ev.player_id];
  if (player && ["pass", "carry", "dribble", "shot", "kickoff", "possession_change", "ball_recovery", "interception"].includes(ev.type)) {
    const text = $("carrier-text");
    text.textContent = `${player.number} ${player.short_name}`;
    const width = text.getComputedTextLength() + 2.4;
    $("carrier-bg").setAttribute("width", width);
    $("carrier-team").setAttribute("fill", teamColor(player.team_id));
    const lx = Math.min(105 - width, Math.max(0, x - width / 2));
    const ly = y > 8 ? y - 6 : y + 2.5;
    $("carrier").setAttribute("transform", `translate(${lx} ${ly})`);
    $("carrier").setAttribute("opacity", 1);
  }
  const ring = $("focus-ring");
  ring.setAttribute("cx", x); ring.setAttribute("cy", y);
  ring.setAttribute("opacity", player && player.id === S.focusPlayer ? 1 : 0);
}

function fade(el, ms) {
  el.style.transition = `opacity ${ms}ms linear`;
  requestAnimationFrame(() => requestAnimationFrame(() => { el.style.opacity = 0; }));
  setTimeout(() => el.remove(), ms + 50);
}

// ------------------------------------------------------- analyst graphics

function hideGraphic() {
  clearTimeout(S.graphicTimer);
  const layer = $("layer-graphic");
  if (layer) layer.innerHTML = "";
  $("graphic-panel").hidden = true;
}

function showGraphic(graphic, answer) {
  hideGraphic();
  const layer = $("layer-graphic");
  const panel = $("graphic-panel");
  const color = graphic.team_id ? teamColor(graphic.team_id) : "#fff";
  if (graphic.kind === "what_if") {
    panel.innerHTML = `<div class="gp-title">${esc(t("whatIfTitle"))} ${esc(graphic.title.replace(/^What if\? /, ""))}</div>
      ${graphic.labels.map((label, i) => `<div class="gp-row"><span>${esc(label === "draw" ? t("draw") : label)}</span>
        <span class="gp-bars"><span class="gp-bar before" style="width:${graphic.before[i] * 100}%"></span>
        <span class="gp-bar after" style="width:${graphic.after[i] * 100}%;background:${graphic.colors[i]}"></span></span>
        <b>${pct(graphic.before[i])} → ${pct(graphic.after[i])}</b></div>`).join("")}
      <div class="gp-legend"><span class="sw before"></span>${t("before")} <span class="sw after"></span>${t("after")}</div>`;
  } else {
    panel.innerHTML = `<div class="gp-title">${esc(graphic.title)}</div><div class="note">${t("attacking")}</div>`;
    const maxW = Math.max(1, ...(graphic.lines ?? []).map((l) => l.w));
    for (const l of graphic.lines ?? []) {
      svg("line", { x1: l.x1, y1: l.y1, x2: l.x2, y2: l.y2, stroke: "#fff", "stroke-width": 0.2 + 1.2 * (l.w / maxW), opacity: 0.85 }, layer);
    }
    for (const pt of graphic.points ?? []) {
      const fill = pt.style === "goal" ? "#fff" : pt.style === "node" ? color : pt.style === "press" ? "#ffcf4a" : "none";
      const c = svg("circle", { cx: pt.x, cy: pt.y, r: pt.r, fill, stroke: pt.style === "node" ? "#fff" : "#fff",
        "stroke-width": 0.3, opacity: pt.style === "dot" ? 0.8 : 1 }, layer);
      if (pt.style === "dot") c.setAttribute("fill", "#fff");
      if (pt.label && pt.style === "node") {
        const tx = svg("text", { x: pt.x, y: pt.y + 0.7, "font-size": 1.9, "text-anchor": "middle", fill: "#fff", "font-weight": 700, "font-family": "Inter, sans-serif" }, layer);
        tx.textContent = pt.label;
      }
    }
  }
  panel.hidden = false;
  if (answer) queueOverlay({ at: S.lastEvent ?? { period: 1, clock_s: 0 }, clock: t("graphicCaption"), title: graphic.title,
    body: answer.length > 220 ? `${answer.slice(0, 217)}…` : answer, chips: [], priority: "high", seconds: GRAPHIC_SECONDS,
    speak: false, fixed: true });
  S.graphicTimer = setTimeout(hideGraphic, GRAPHIC_SECONDS * 1000);
}

// ------------------------------------------------------------- live stats

function onSnapshot(snap) {
  S.snapshot = snap;
  if (S.joinedLate) {
    S.joinedLate = false;
    if (snap.clock_s > CATCH_UP_AFTER_S) offerCatchUp(0);
  }
  $("score-home").textContent = snap.score.home;
  $("score-away").textContent = snap.score.away;
  const phase = snap.phase;
  if (phase) {
    $("phase").textContent = phase.phase === "control" ? t("control", { team: S.teams[phase.team_id].name })
      : phase.phase === "chaos" ? t("chaos", { n: phase.turnovers_per_min }) : t("balanced");
  }
  $("tempo").textContent = t("tempo", { n: snap.tempo_passes_per_min });
  $("mom-home").textContent = t("onTop", { team: S.teams[homeId()]?.short_name });
  $("mom-away").textContent = t("onTop", { team: S.teams[awayId()]?.short_name });
  $("mom-home").style.color = teamColor(homeId());
  $("mom-away").style.color = teamColor(awayId());
  renderStats(snap);
  renderMomentum(snap.momentum_series);
  renderFocus();
  renderShots(snap);
  renderNetwork(snap);
  if (S.social) renderPredictions();
}

function renderStats(snap) {
  const h = snap.teams[homeId()], a = snap.teams[awayId()];
  const rows = [
    ["st_possession", "possession_pct"],
    ["st_xg", "xg", (v) => v.toFixed(2)],
    ["st_shots", "shots"],
    ["st_on_target", "shots_on_target"],
    ["st_passes", "passes"],
    ["st_pass_acc", "pass_accuracy", (v) => `${Math.round(v * 100)}%`],
    ["st_high", "high_turnovers"],
    ["st_ppda", "ppda", (v) => (v == null ? "–" : v.toFixed(1)), true],
    ["st_corners", "corners"],
    ["st_distance", "distance_km", (v) => v.toFixed(1)],
  ];
  $("stats").innerHTML = rows.map(([label, key, fmt = String, inverse]) => {
    let hv = h[key] ?? 0, av = a[key] ?? 0, ht, at;
    if (key === "possession_pct") { hv = Math.round(hv); av = 100 - hv; ht = `${hv}%`; at = `${av}%`; }
    else { ht = fmt(h[key]); at = fmt(a[key]); }
    let share = hv + av > 0 ? hv / (hv + av) : 0.5;
    if (inverse && hv && av) share = av / (hv + av);
    return `<div class="stat"><span class="v">${ht}</span><span class="label">${t(label)}</span><span class="v r">${at}</span>
      <div class="bar"><span style="width:${share * 100}%;background:${teamColor(homeId())}"></span><span style="width:${(1 - share) * 100}%;background:${teamColor(awayId())}"></span></div></div>`;
  }).join("");
}

function renderMomentum(series) {
  const el = $("momentum");
  el.innerHTML = "";
  svg("line", { x1: 0, y1: 40, x2: 300, y2: 40, stroke: "var(--line)", "stroke-width": 1 }, el);
  const w = 300 / Math.max(96, series.length);
  series.forEach((pt, i) => {
    const h = (Math.abs(pt.value) / 100) * 38;
    svg("rect", { x: i * w, y: pt.value >= 0 ? 40 - h : 40, width: Math.max(1, w - 0.8), height: h,
      fill: teamColor(pt.value >= 0 ? homeId() : awayId()), opacity: 0.85 }, el);
  });
}

function renderShots(snap) {
  const layer = $("shots-map").querySelector(".content");
  layer.innerHTML = "";
  for (const s of snap.shots ?? []) {
    // Home shots attack to the right, away shots to the left.
    const away = s.team_id === awayId();
    const x = away ? 105 - s.x : s.x, y = away ? 68 - s.y : s.y;
    const c = svg("circle", { cx: x, cy: y, r: 0.8 + 4 * s.xg, fill: s.result === "goal" ? teamColor(s.team_id) : "none",
      stroke: teamColor(s.team_id), "stroke-width": 0.45, opacity: 0.9 }, layer);
    const title = svg("title", {}, c);
    title.textContent = `${s.clock} ${S.players[s.player_id]?.short_name ?? ""} · xG ${s.xg} · ${s.result}`;
  }
}

function renderNetworkTabs() {
  $("network-team").innerHTML = Object.values(S.teams).map((team) =>
    `<button class="seg ${team.id === S.networkTeam ? "on" : ""}" data-team="${team.id}">${esc(team.short_name)}</button>`).join("");
  $("network-team").querySelectorAll("button").forEach((b) => (b.onclick = () => {
    S.networkTeam = b.dataset.team; renderNetworkTabs(); if (S.snapshot) renderNetwork(S.snapshot);
  }));
}

function renderNetwork(snap) {
  const layer = $("network-map").querySelector(".content");
  layer.innerHTML = "";
  const net = snap.pass_networks?.[S.networkTeam];
  if (!net) return;
  const color = teamColor(S.networkTeam);
  const pos = Object.fromEntries(net.nodes.map((n) => [n.player_id, n]));
  const maxPasses = Math.max(1, ...net.edges.map((e) => e.passes));
  for (const e of net.edges) {
    const a = pos[e.source], b = pos[e.target];
    svg("line", { x1: a.x, y1: a.y, x2: b.x, y2: b.y, stroke: color, "stroke-width": 0.2 + 1.4 * (e.passes / maxPasses), opacity: 0.55 }, layer);
  }
  for (const n of net.nodes) {
    svg("circle", { cx: n.x, cy: n.y, r: 2, fill: color, stroke: "#fff", "stroke-width": 0.3 }, layer);
    const label = svg("text", { x: n.x, y: n.y + 0.75, "font-size": 2, "text-anchor": "middle", fill: "#fff", "font-weight": 700, "font-family": "Inter, sans-serif" }, layer);
    label.textContent = S.players[n.player_id]?.number ?? "";
  }
}

// ------------------------------------------------------- win probability

function onForecast(f) {
  S.forecasts.push(f);
  S.forecasts.sort((a, b) => matchSeconds(a) - matchSeconds(b));
  renderWinbar();
  renderWinChart();
}

function renderWinbar() {
  const f = S.forecasts.at(-1);
  $("winbar").hidden = !f;
  if (!f || !S.match) { $("winbar-legend").textContent = ""; return; }
  const bar = $("winbar");
  bar.querySelector(".home").style.width = pct(f.home);
  bar.querySelector(".draw").style.width = pct(f.draw);
  bar.querySelector(".away").style.width = pct(f.away);
  $("winbar-legend").innerHTML = `${esc(S.teams[homeId()].short_name)} <b>${pct(f.home)}</b> · ${t("draw")} <b>${pct(f.draw)}</b> · ${esc(S.teams[awayId()].short_name)} <b>${pct(f.away)}</b>`;
}

function renderWinChart() {
  const el = $("winprob-chart");
  el.innerHTML = "";
  const series = S.forecasts;
  $("winprob-note").textContent = t("winProbNote", { n: FORECAST_RUNS });
  if (series.length < 2 || !S.match) return;
  const w = 300 / Math.max(96, series.length - 1);
  const path = (fn) => series.map((f, i) => `${i ? "L" : "M"}${(i * w).toFixed(1)},${(80 - fn(f) * 80).toFixed(1)}`).join(" ");
  const close = `L${((series.length - 1) * w).toFixed(1)},80 L0,80 Z`;
  // Stacked areas: home at the bottom, then the draw, the away share on top.
  svg("rect", { x: 0, y: 0, width: (series.length - 1) * w, height: 80, fill: teamColor(awayId()), opacity: 0.85 }, el);
  svg("path", { d: `${path((f) => f.home + f.draw)} ${close}`, fill: "#c9ccd1" }, el);
  svg("path", { d: `${path((f) => f.home)} ${close}`, fill: teamColor(homeId()), opacity: 0.9 }, el);
  for (const m of S.moments.filter((x) => x.signal.type === "goal")) {
    const i = series.findIndex((f) => matchSeconds(f) >= matchSeconds(m.signal));
    if (i >= 0) svg("line", { x1: i * w, y1: 0, x2: i * w, y2: 80, stroke: "#15171c", "stroke-width": 1, "stroke-dasharray": "2 2" }, el);
  }
}

// ---------------------------------------------------------- player focus

function renderFollowSelect() {
  const select = $("follow-select");
  select.disabled = !S.match;
  if (!S.match) { select.innerHTML = ""; return; }
  select.innerHTML = Object.values(S.teams).map((team) => `<optgroup label="${esc(team.name)}">${
    team.players.map((p) => `<option value="${p.id}" ${p.id === S.focusPlayer ? "selected" : ""}>${p.number}. ${esc(p.name)} (${p.position})</option>`).join("")
  }</optgroup>`).join("");
}

function follow(playerId) {
  S.focusPlayer = playerId;
  updateObsLink();
  if (S.source === "live") {
    send({ type: "follow", player_id: playerId });
    $("lang-note").textContent = t("liveFollowNote");
  }
  renderFocus();
  renderFeed();
}

function renderFocus() {
  const player = S.focusPlayer && S.snapshot?.players[S.focusPlayer];
  if (!player) { $("focus-card").innerHTML = ""; return; }
  const cells = [["fp_rating", player.rating.toFixed(1)], ["fp_minutes", player.minutes], ["fp_goals", player.goals],
    ["fp_xg", player.xg.toFixed(2)], ["fp_passes", `${player.passes_completed}/${player.passes}`], ["fp_key", player.key_passes],
    ["fp_distance", player.distance_km.toFixed(1)], ["fp_speed", player.top_speed_kmh.toFixed(1)], ["fp_sprints", player.sprints]];
  $("focus-card").innerHTML = `<div class="focus-name">${player.number}. ${esc(player.name)}</div>
    <div class="note">${esc(S.teams[player.team_id].name)}, ${player.position}${player.on_pitch ? "" : `, ${t("substituted")}`}</div>
    <div class="focus-grid">${cells.map(([k, v]) => `<div><b>${esc(v)}</b><span>${t(k)}</span></div>`).join("")}</div>`;
}

// -------------------------------------------------------------- the stands

function renderSocial() {
  const social = S.social;
  const room = S.room;
  if (!room) {
    $("room-note").textContent = "";
  } else {
    $("room-note").textContent = `${t("roomCode", { code: room.code })} · ${t("watchers", { n: social?.members ?? 1 })} · ${room.host ? t("youHost") : t("hostOnly")}`;
  }
  if (!social || !S.match) {
    for (const id of ["stands-support", "predictions", "proposals", "rating"]) $(id).innerHTML = "";
    return;
  }
  renderStands();
  renderPredictions();
  renderProposals();
  renderRating();
}

function renderStands() {
  const stands = S.social.stands;
  const totalFans = Object.values(stands).reduce((a, s) => a + s.fans, 0) || 1;
  const totalCheers = Object.values(stands).reduce((a, s) => a + s.cheers, 0) || 1;
  const ended = S.state === "ended";
  const leader = Object.entries(stands).sort((a, b) => b[1].fans - a[1].fans || b[1].cheers - a[1].cheers)[0];
  $("stands-support").innerHTML = `<div class="stands">${Object.values(S.teams).map((team) => {
    const s = stands[team.id];
    const mine = S.my.support === team.id;
    return `<div class="stand">
      <button class="support ${mine ? "on" : ""}" data-team="${team.id}" style="--team:${team.colors[0]}">${esc(team.short_name)}</button>
      <div class="stand-bars"><span style="width:${(s.fans / totalFans) * 100}%;background:${team.colors[0]}"></span></div>
      <div class="note">${t("fansCount", { n: s.fans })} · ${t("cheersCount", { n: s.cheers })}</div>
      ${mine ? `<button class="cheer" data-team="${team.id}">${t("cheer")}</button>` : ""}
    </div>`;
  }).join("")}</div>
  ${ended && leader && leader[1].fans > 0 ? `<p class="verdict">${esc(t("standWinner", { team: S.teams[leader[0]].name }))}</p>` : `<p class="note">${t("support")}</p>`}`;
  $("stands-support").querySelectorAll(".support").forEach((b) => (b.onclick = () => {
    S.my.support = b.dataset.team; send({ type: "support", team_id: b.dataset.team }); renderStands();
  }));
  $("stands-support").querySelectorAll(".cheer").forEach((b) => (b.onclick = () => {
    send({ type: "cheer" }); b.classList.add("pop"); setTimeout(() => b.classList.remove("pop"), 300);
  }));
}

function renderPredictions() {
  const social = S.social;
  if (!social || !S.match) return;
  const p = social.predictions;
  const total = p.home + p.draw + p.away;
  const ai = social.ai;
  const labels = { home: S.teams[homeId()].short_name, draw: t("draw"), away: S.teams[awayId()].short_name };
  const rows = ["home", "draw", "away"].map((k) => {
    const crowd = total ? p[k] / total : 0;
    return `<div class="pred-row">
      <button class="pred ${S.my.prediction === k ? "on" : ""}" data-k="${k}" ${social.predictions_open ? "" : "disabled"}>${esc(labels[k])}</button>
      <div class="pred-bars"><span class="crowd" style="width:${crowd * 100}%"></span><span class="ai" style="width:${(ai?.[k] ?? 0) * 100}%"></span></div>
      <span class="pred-val">${pct(crowd)} · <b>${ai ? pct(ai[k]) : "–"}</b></span></div>`;
  }).join("");
  let verdict = "";
  if (S.state === "ended" && total && social.ai_kickoff && S.snapshot) {
    const { home, away } = S.snapshot.score;
    const result = home > away ? "home" : home < away ? "away" : "draw";
    verdict = `<p class="verdict">${esc(t("crowdVerdict", { crowd: pct(p[result] / total), ai: pct(social.ai_kickoff[result]) }))}</p>`;
  }
  $("predictions").innerHTML = `<div class="pred-legend note"><span class="sw crowd"></span>${t("crowd")} <span class="sw ai"></span>${t("ai")}</div>${rows}
    ${social.predictions_open ? `<p class="note">${t("predict")}</p>` : `<p class="note">${t("predictionsClosed")}</p>`}${verdict}`;
  $("predictions").querySelectorAll(".pred").forEach((b) => (b.onclick = () => {
    S.my.prediction = b.dataset.k; send({ type: "predict", outcome: b.dataset.k }); renderPredictions();
  }));
}

function proposalText(p) {
  if (p.kind === "tactic") return `${S.teams[p.team_id]?.short_name}: ${t(`tac_${p.tactic}`)}`;
  return `${S.teams[p.team_id]?.short_name}: ${S.players[p.player_on]?.short_name ?? "?"} ↔ ${S.players[p.player_off]?.short_name ?? "?"}`;
}

function renderProposals() {
  const list = S.social.proposals;
  if (!list.length) { $("proposals").innerHTML = `<p class="note">${t("noProposals")}</p>`; return; }
  $("proposals").innerHTML = list.slice(0, 6).map((p) => {
    const mine = S.my.votes[p.id] ?? 0;
    const impact = p.status === "ready" ? t("proposalImpact", { before: pct(p.win_before), after: pct(p.win_after) })
      : p.status === "simulating" ? t("simulating") : "";
    const gain = p.status === "ready" ? (p.win_after - p.win_before) : 0;
    return `<div class="proposal" style="--team:${teamColor(p.team_id)}">
      <div class="votes"><button class="vote ${mine === 1 ? "on" : ""}" data-id="${p.id}" data-v="1" aria-label="Up">▲</button>
        <b>${p.score}</b><button class="vote ${mine === -1 ? "on" : ""}" data-id="${p.id}" data-v="-1" aria-label="Down">▼</button></div>
      <div><div class="title">${esc(proposalText(p))}</div>
        <div class="note">${esc(t("proposedBy", { who: p.author, clock: p.clock }))}</div>
        <div class="impact ${gain > 0.02 ? "up" : gain < -0.02 ? "down" : ""}">${esc(impact)}</div></div></div>`;
  }).join("");
  $("proposals").querySelectorAll(".vote").forEach((b) => (b.onclick = () => {
    const id = b.dataset.id, v = Number(b.dataset.v);
    const value = S.my.votes[id] === v ? 0 : v;
    S.my.votes[id] = value;
    send({ type: "vote", proposal_id: id, value });
  }));
}

function fillProposeForm() {
  if (!S.match) return;
  const teamSel = $("pr-team");
  const current = teamSel.value || homeId();
  teamSel.innerHTML = Object.values(S.teams).map((team) => `<option value="${team.id}" ${team.id === current ? "selected" : ""}>${esc(team.name)}</option>`).join("");
  const team = S.teams[teamSel.value];
  const kind = $("pr-kind").value;
  const onPitch = (pid) => S.snapshot?.players[pid]?.on_pitch ?? false;
  const option = (p) => `<option value="${p.id}">${p.number}. ${esc(p.short_name)} (${p.position})</option>`;
  $("pr-off").innerHTML = team.players.filter((p) => onPitch(p.id)).map(option).join("");
  $("pr-on").innerHTML = team.players.filter((p) => !onPitch(p.id) && !S.snapshot?.players[p.id]?.minutes).map(option).join("");
  $("pr-tactic").innerHTML = TACTICS.map((x) => `<option value="${x}">${t(`tac_${x}`)}</option>`).join("");
  for (const id of ["pr-off", "pr-on"]) $(id).hidden = kind !== "substitution";
  $("pr-tactic").hidden = kind !== "tactic";
}

function onPropose(e) {
  e.preventDefault();
  const kind = $("pr-kind").value;
  const message = { type: "propose", team_id: $("pr-team").value, kind };
  if (kind === "substitution") Object.assign(message, { player_off: $("pr-off").value, player_on: $("pr-on").value });
  else message.tactic = $("pr-tactic").value;
  send(message);
  $("propose").open = false;
}

function renderRating() {
  const social = S.social;
  if (!social.rating_open) { $("rating").innerHTML = `<p class="note">${t("rateLater")}</p>`; return; }
  const avg = social.ratings.average;
  $("rating").innerHTML = `<div class="stars">${[1, 2, 3, 4, 5].map((n) =>
    `<button class="star ${S.my.rating && n <= S.my.rating ? "on" : ""}" data-n="${n}" aria-label="${n}">★</button>`).join("")}</div>
    ${avg ? `<p class="note">${t("ratingAvg", { avg: avg.toFixed(1), n: social.ratings.count })}</p>` : ""}`;
  $("rating").querySelectorAll(".star").forEach((b) => (b.onclick = () => {
    S.my.rating = Number(b.dataset.n); send({ type: "rate", stars: S.my.rating }); renderRating();
  }));
}

async function invite() {
  if (!S.room) return;
  const link = `${location.origin}${location.pathname}?room=${S.room.code}`;
  try { await navigator.clipboard.writeText(link); $("invite-btn").textContent = t("copied"); }
  catch { prompt(t("invite"), link); }
  setTimeout(() => { $("invite-btn").textContent = t("invite"); }, 2000);
}

// ----------------------------------------------------------------- agents

function onMoment(moment) {
  S.moments.push(moment);
  renderAgents();
  const card = moment.overlays[profileId()];
  if (!card) return;
  queueOverlay(overlayFromMoment(moment, card));
  prepend(momentNode(moment));
  if (moment.signal.type === "goal") renderWinChart();
}

function onRecap(recap) {
  S.recaps.push(recap);
  renderAgents();
  prepend(recapNode(recap));
  const r = recap.recaps[profileId()];
  if (r) speak(`${r.headline}. ${r.summary}`, S.lang, false);
}

function onReport(report) {
  S.reports.push(report);
  renderAgents();
  if (report.player_id !== S.focusPlayer || !report.reports[S.lang]) return;
  queueOverlay(overlayFromReport(report));
  prepend(reportNode(report));
}

function prepend(node) {
  if (!node) return;
  $("feed").querySelector(".empty")?.remove();
  $("feed").prepend(node);
}

function momentNode(moment) {
  const p = profile();
  const card = moment.overlays[p?.id];
  if (!card) return null;
  const el = document.createElement("article");
  el.className = "moment";
  if (moment.signal.team_id) el.style.setProperty("--team", teamColor(moment.signal.team_id));
  const extra = [];
  const why = card.why || (p.language === "en" ? moment.insight.why_it_matters : "");
  if (why) extra.push(`<p><span>${t("whyLabel")}</span> ${esc(why)}</p>`);
  if (p.mode === "analyst") {
    const note = card.analyst_note || (p.language === "en" ? moment.insight.analyst_note : "");
    if (note) extra.push(`<p><span>${t("noteLabel")}</span> ${esc(note)}</p>`);
    if (p.language === "en") extra.push(`<p><span>${t("commentaryLabel")}</span> ${esc(moment.commentary.line)}</p>`);
  }
  const impact = impactLine(moment.signal);
  el.innerHTML = `<div class="time">${esc(moment.signal.display_clock)}</div>
    <div><div class="title">${esc(card.title)}</div><div class="body">${esc(card.body)}</div>${figures(card.stat_chips)}
    ${impact ? `<div class="impact-line">${esc(impact)}</div>` : ""}
    ${extra.length ? `<div class="extra">${extra.join("")}</div>` : ""}</div>`;
  return el;
}

function reportNode(report) {
  const r = report.reports[S.lang];
  const player = S.players[report.player_id];
  if (!r || !player) return null;
  const el = document.createElement("article");
  el.className = "moment report";
  el.style.setProperty("--team", teamColor(player.team_id));
  el.innerHTML = `<div class="time">${esc(report.clock)}</div>
    <div><div class="kicker">${t("checkin")} · ${player.number} ${esc(player.short_name)}</div>
    <div class="title">${esc(r.headline)}</div><div class="body">${esc(r.summary)}</div>${figures(r.key_stats)}</div>`;
  return el;
}

function recapNode(recap) {
  const r = recap.recaps[profileId()];
  if (!r) return null;
  const el = document.createElement("article");
  el.className = "recap";
  el.innerHTML = `<div class="kicker">${recap.kind === "full_time" ? t("fullTime") : t("halfTime")} · ${esc(recap.clock)}</div>
    <h3>${esc(r.headline)}</h3><p>${esc(r.summary)}</p>
    <ul>${r.key_moments.map((k) => `<li>${esc(k)}</li>`).join("")}</ul>
    <p>${esc(r.tactical_story)}</p><p><b>${esc(r.player_of_the_match)}</b></p>`;
  return el;
}

function renderFeed() {
  const feed = $("feed");
  feed.innerHTML = "";
  const items = [
    ...S.moments.map((m) => ({ at: matchSeconds(m.signal), node: () => momentNode(m) })),
    ...S.reports.filter((r) => r.player_id === S.focusPlayer).map((r) => ({ at: matchSeconds(r) + 0.5, node: () => reportNode(r) })),
    ...S.recaps.map((r) => ({ at: (r.kind === "half_time" ? 1 : 2) * 10000 + 9999, node: () => recapNode(r) })),
  ];
  items.sort((a, b) => b.at - a.at);
  for (const item of items) { const n = item.node(); if (n) feed.appendChild(n); }
  if (!feed.children.length) feed.innerHTML = `<p class="empty">${S.match ? t("emptyViewer") : t("emptyStart")}</p>`;
}

function renderAgents() {
  const traces = [...S.moments, ...S.recaps, ...S.reports].flatMap((x) => x.trace);
  if (!traces.length) return;
  const count = (s) => traces.filter((x) => x.status === s).length;
  const lat = traces.map((x) => x.latency_ms).sort((a, b) => a - b);
  $("agent-summary").textContent = t("agentSummary", { n: traces.length, s: (lat[lat.length >> 1] / 1000).toFixed(1) });
  const statusOf = (ts) => ts.some((x) => x.status === "fallback") ? "fallback" : ts.some((x) => x.status === "retried") ? "retried" : "ok";
  const label = { ok: t("accepted"), retried: t("corrected"), fallback: t("template") };
  const recent = [...S.moments].sort((a, b) => matchSeconds(b.signal) - matchSeconds(a.signal)).slice(0, 15);
  $("agents").innerHTML = `<div class="note">${t("agentDetail", { ok: count("ok"), retried: count("retried"), fallback: count("fallback") })}</div>`
    + recent.map((m) => {
      const status = statusOf(m.trace);
      return `<div class="agent-row"><span>${esc(m.signal.display_clock)}</span><span>${esc(m.signal.type.replace(/_/g, " "))}, ${t("agentsCount", { n: m.trace.length })}</span><span class="${status}">${label[status]}</span></div>`;
    }).join("");
}

// ---------------------------------------------------------------- overlay

function overlayFromMoment(moment, card) {
  return { at: moment.signal, clock: moment.signal.display_clock, title: card.title, body: card.body,
    chips: card.stat_chips, playerId: card.highlight_player_id, teamId: card.highlight_team_id || moment.signal.team_id,
    priority: card.priority, seconds: moment.display_seconds, excited: card.priority === "high" };
}

function overlayFromReport(report) {
  const r = report.reports[S.lang];
  return { at: report, clock: `${report.clock} · ${t("checkin")}`, title: r.headline, body: r.summary, chips: r.key_stats,
    playerId: report.player_id, teamId: S.players[report.player_id]?.team_id, priority: "normal", seconds: REPORT_SECONDS };
}

function isStale(item) {
  if (S.source === "live" || item.speak === false) return performance.now() - item.arrivedAt > STALE_LIVE_WAIT_MS;
  return !!S.lastEvent && matchSeconds(S.lastEvent) - matchSeconds(item.at) > STALE_MATCH_S;
}

function queueOverlay(item) {
  item.arrivedAt = performance.now();
  if (item.priority === "high") S.overlayQueue.unshift(item); else S.overlayQueue.push(item);
  S.overlayQueue = S.overlayQueue.slice(0, 4);
  if (!S.overlayCurrent || (item.priority === "high" && S.overlayCurrent.priority !== "high")) showNextOverlay();
}

function showNextOverlay() {
  clearTimeout(S.overlayTimer);
  const box = $("overlay");
  const old = box.firstElementChild;
  if (old) { old.classList.add("leaving"); setTimeout(() => old.remove(), 250); }
  while (S.overlayQueue.length && isStale(S.overlayQueue[0])) S.overlayQueue.shift();
  const item = S.overlayQueue.shift();
  S.overlayCurrent = item ?? null;
  if (!item) return;
  const el = document.createElement("div");
  el.className = "l3";
  const player = S.players[item.playerId];
  el.innerHTML = `<div class="bar" style="background:${item.teamId ? teamColor(item.teamId) : "var(--accent)"}"></div>
    <div class="content"><div class="kicker">${esc(item.clock)}${player ? ` · ${player.number} ${esc(player.short_name)}` : ""}</div>
    <div class="title">${esc(item.title)}</div><div class="body">${esc(item.body)}</div>${figures(item.chips)}</div>`;
  setTimeout(() => box.appendChild(el), old ? 250 : 0);
  if (item.speak !== false) speak(`${item.title}. ${item.body}`, S.lang, item.excited);
  // Match overlays shorten at high playback speeds; an analyst graphic stays readable.
  const seconds = item.fixed ? item.seconds : Math.max(3, item.seconds * Math.min(1, 30 / S.speed));
  S.overlayTimer = setTimeout(showNextOverlay, seconds * 1000);
}

// ------------------------------------------------------------------ audio

function toggleListen() {
  S.listen = !S.listen;
  $("listen-btn").textContent = S.listen ? t("mute") : t("listen");
  $("listen-btn").setAttribute("aria-pressed", String(S.listen));
  if (!S.listen) { S.audioQueue = []; speechSynthesis?.cancel(); document.querySelectorAll("audio.voice").forEach((a) => a.pause()); }
}

function speak(text, lang, excited) {
  if (!S.listen || !text) return;
  S.audioQueue.push({ text, lang, excited });
  S.audioQueue = S.audioQueue.slice(-2); // Keep the commentary close to the action.
  if (!S.audioBusy) playNext();
}

async function playNext() {
  const item = S.audioQueue.shift();
  if (!item) { S.audioBusy = false; return; }
  S.audioBusy = true;
  try {
    const url = `/api/speech?${new URLSearchParams({ text: item.text.slice(0, 400), lang: item.lang, excited: item.excited ? "true" : "false" })}`;
    const res = await fetch(url);
    if (!res.ok) throw new Error("speech unavailable");
    const audio = new Audio(URL.createObjectURL(await res.blob()));
    audio.className = "voice";
    audio.onended = playNext;
    audio.onerror = playNext;
    await audio.play();
  } catch {
    // Browser voice as a fallback (offline mode or no Azure Speech).
    const utterance = new SpeechSynthesisUtterance(item.text);
    utterance.lang = { en: "en-GB", fr: "fr-FR", es: "es-ES", pt: "pt-PT" }[item.lang] ?? "en-GB";
    utterance.onend = playNext;
    speechSynthesis.speak(utterance);
  }
}

// --------------------------------------------------------------- catch-up

function onVisibility() {
  if (document.hidden) { S.hiddenAt = S.lastEvent ? { ...S.lastEvent } : null; return; }
  if (S.hiddenAt && S.lastEvent && S.state === "playing" && matchSeconds(S.lastEvent) - matchSeconds(S.hiddenAt) > CATCH_UP_AFTER_S) {
    offerCatchUp(matchSeconds(S.hiddenAt));
  }
  S.hiddenAt = null;
}

function offerCatchUp(since) {
  const box = $("catchup");
  box.hidden = false;
  box.innerHTML = `<button class="primary small">${t("catchUpButton")}</button>`;
  box.querySelector("button").onclick = () => {
    box.innerHTML = `<p class="note">${t("catchUpLoading")}</p>`;
    send({ type: "catch_up", profile_id: profileId(), since });
  };
}

function onCatchUp(msg) {
  const c = msg.catch_up;
  const box = $("catchup");
  box.hidden = false;
  box.innerHTML = `<div class="kicker">${t("catchUpTitle")} · ${esc(msg.clock)}</div><h3>${esc(c.headline)}</h3>
    <p>${esc(c.summary)}</p><ul>${c.key_points.map((k) => `<li>${esc(k)}</li>`).join("")}</ul>`;
  speak(`${c.headline}. ${c.summary}`, S.lang, false);
}

// ----------------------------------------------------------------- viewer

function renderViewer() {
  const roleBox = $("roles");
  if (!S.profiles.length) {
    roleBox.innerHTML = `<p class="note">${t("viewersAtKickoff")}</p>`;
  } else {
    roleBox.innerHTML = roles().map((r) => `<button class="role" role="radio" aria-checked="${r.role === S.role}" data-role="${r.role}">
        <span>${esc(roleLabel(r))}</span><span class="tag">${r.mode === "analyst" ? t("tagAnalyst") : r.player_focus ? t("tagFocus") : t("tagFan")}</span></button>`).join("");
    roleBox.querySelectorAll(".role").forEach((b) => (b.onclick = () => select(b.dataset.role, S.lang)));
  }
  // Recorded runs offer their languages; live sessions can add any language.
  const available = !S.profiles.length || S.source === "live" ? Object.keys(LANGUAGE_NAMES) : languages();
  $("langs").innerHTML = available.map((l) => `<button class="lang" role="radio" aria-checked="${l === S.lang}" data-lang="${l}" title="${LANGUAGE_NAMES[l] ?? l}">${l.toUpperCase()}</button>`).join("");
  $("langs").querySelectorAll(".lang").forEach((b) => (b.onclick = () => select(S.role, b.dataset.lang)));
  const pending = S.source === "live" && S.match && !languages().includes(S.lang);
  $("lang-note").textContent = pending ? t("liveLangNote", { lang: LANGUAGE_NAMES[S.lang] })
    : S.source === "live" && S.match ? t("liveLangsInUse") : "";
}

function select(role, lang) {
  const languageChanged = lang !== S.lang;
  S.role = role;
  S.lang = lang;
  if (S.source === "live" && S.match && !languages().includes(lang)) send({ type: "languages", value: [lang] });
  S.overlayQueue = [];
  S.audioQueue = [];
  showNextOverlay();
  updateObsLink();
  if (languageChanged) renderTexts(); else { renderViewer(); renderFeed(); }
}

// ------------------------------------------------------------------- chat

function onAsk(e) {
  e.preventDefault();
  const q = $("chat-input").value.trim();
  if (!q) return;
  if (S.ws?.readyState !== WebSocket.OPEN) { alert(t("kickoffFirst")); return; }
  send({ type: "ask", question: q });
  $("chat-input").value = "";
  const el = document.createElement("div");
  el.className = "qa";
  el.dataset.q = q;
  el.innerHTML = `<div class="q">${esc(q)}</div><div class="a pending">${t("checking")}</div>`;
  $("chat").prepend(el);
}

function onAnswer(msg) {
  const el = [...$("chat").children].find((d) => d.dataset.q === msg.question && d.querySelector(".a.pending"));
  const a = el?.querySelector(".a");
  if (a) { a.textContent = msg.answer; a.classList.remove("pending"); }
  if (msg.graphic) showGraphic(msg.graphic, msg.answer);
  else speak(msg.answer, S.lang, false);
}

init();
