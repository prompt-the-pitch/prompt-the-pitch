// Prompt the Pitch: broadcast overlay for OBS or any graphics engine.
// Shows only the TV graphics (scorebug, win probability, lower thirds) on a transparent page.
//
//   broadcast.html?room=ABC123&lang=fr&viewer=home-fan&player=exl-9-okada   follow a watch room
//   broadcast.html?match=exl-prg-42&source=cached&speed=1&lang=en           play a match on its own
//   add &preview=1 to see the graphics over a pitch picture in a normal browser.

import { setLanguage, t } from "./i18n.js";

const $ = (id) => document.getElementById(id);
const params = new URLSearchParams(location.search);
const lang = params.get("lang") || "en";
const viewer = params.get("viewer") || "analyst";
const player = params.get("player");
const preview = params.get("preview") === "1";
const STALE_MATCH_S = 150;
const STALE_LIVE_WAIT_MS = 20000;

const S = { ws: null, source: "cached", speed: Number(params.get("speed") || 1), teams: {}, players: {}, match: null,
  lastEvent: null, queue: [], current: null, timer: null };

const esc = (text) => String(text ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pct = (v) => `${Math.round((v ?? 0) * 100)}%`;
const matchSeconds = (x) => x.period * 10000 + x.clock_s;
const teamColor = (id) => S.teams[id]?.colors?.[0] ?? "#7d828c";

async function start() {
  setLanguage(lang);
  if (preview) {
    document.body.classList.add("preview");
    $("note").hidden = false;
    $("note").textContent = "Preview. In OBS, add this page as a Browser source (1920 × 1080) without “&preview=1”: the background becomes transparent.";
  }
  const code = params.get("room");
  let matchId = params.get("match");
  const query = new URLSearchParams({ source: params.get("source") || "cached", speed: S.speed, languages: lang });
  if (code) {
    const res = await fetch(`/api/rooms/${code}`);
    if (!res.ok) { showNote("This watch room has closed."); return; }
    const room = await res.json();
    matchId = room.match_id;
    query.set("room", code);
    query.set("observer", "1"); // Follow the room without becoming a fan or the host.
  }
  if (!matchId) {
    const matches = await fetch("/api/matches").then((r) => r.json());
    matchId = (matches.find((m) => m.cached_run) ?? matches[0])?.match_id;
  }
  const proto = location.protocol === "https:" ? "wss" : "ws";
  S.ws = new WebSocket(`${proto}://${location.host}/ws/matches/${matchId}?${query}`);
  S.ws.onmessage = (e) => handle(JSON.parse(e.data));
}

function showNote(text) {
  $("note").hidden = false;
  $("note").textContent = text;
}

function send(message) {
  if (S.ws?.readyState === WebSocket.OPEN) S.ws.send(JSON.stringify(message));
}

function handle(msg) {
  switch (msg.type) {
    case "match": return onMatch(msg);
    case "status": if (msg.speed) S.speed = msg.speed; return;
    case "event":
      S.lastEvent = { period: msg.event.period, clock_s: msg.event.clock_s };
      $("clock").textContent = msg.event.clock;
      return;
    case "snapshot":
      $("score-home").textContent = msg.snapshot.score.home;
      $("score-away").textContent = msg.snapshot.score.away;
      return;
    case "forecast": return onForecast(msg.forecast);
    case "moment": return onMoment(msg.moment);
    case "player_report": return onReport(msg.report);
    case "recap": return onRecap(msg.recap);
  }
}

function onMatch(msg) {
  S.match = msg.match;
  S.source = msg.source;
  S.teams = Object.fromEntries(msg.match.teams.map((team) => [team.id, team]));
  S.players = Object.fromEntries(msg.match.teams.flatMap((team) => team.players.map((p) => [p.id, { ...p, team_id: team.id }])));
  const home = S.teams[msg.match.home_team_id], away = S.teams[msg.match.away_team_id];
  $("home-name").textContent = home.name;
  $("away-name").textContent = away.name;
  $("home-sw").style.background = home.colors[0];
  $("away-sw").style.background = away.colors[0];
  $("win").querySelector(".home").style.background = home.colors[0];
  $("win").querySelector(".away").style.background = away.colors[0];
  $("bug").hidden = false;
  // Live sessions only write the languages and players someone asked for.
  if (msg.source === "live") {
    if (!msg.profiles.some((p) => p.language === lang)) send({ type: "languages", value: [lang] });
    if (player) send({ type: "follow", player_id: player });
  }
}

function onForecast(f) {
  const win = $("win");
  win.querySelector(".home").style.width = pct(f.home);
  win.querySelector(".draw").style.width = pct(f.draw);
  win.querySelector(".away").style.width = pct(f.away);
  const home = S.teams[S.match.home_team_id], away = S.teams[S.match.away_team_id];
  $("win-label").innerHTML = `${t("winProb")} · ${esc(home.short_name)} <b>${pct(f.home)}</b> · ${t("draw")} <b>${pct(f.draw)}</b> · ${esc(away.short_name)} <b>${pct(f.away)}</b>`;
}

function figures(chips) {
  if (!chips?.length) return "";
  return `<div class="figures">${chips.map((c) => `${esc(c.label)} <b>${esc(c.value)}</b>`).join('<span class="sep">·</span>')}</div>`;
}

function onMoment(moment) {
  const card = moment.overlays[`${viewer}.${lang}`];
  if (!card) return;
  queue({ at: moment.signal, kicker: moment.signal.display_clock, title: card.title, body: card.body, chips: card.stat_chips,
    playerId: card.highlight_player_id, teamId: card.highlight_team_id || moment.signal.team_id,
    priority: card.priority, seconds: moment.display_seconds });
}

function onReport(report) {
  const r = report.reports[lang];
  if (!r || report.player_id !== player) return;
  queue({ at: report, kicker: `${report.clock} · ${t("checkin")}`, title: r.headline, body: r.summary, chips: r.key_stats,
    playerId: report.player_id, teamId: S.players[report.player_id]?.team_id, priority: "normal", seconds: 8 });
}

function onRecap(recap) {
  const r = recap.recaps[`${viewer}.${lang}`];
  if (!r) return;
  queue({ at: S.lastEvent ?? { period: 2, clock_s: 0 }, kicker: recap.kind === "full_time" ? t("fullTime") : t("halfTime"),
    title: r.headline, body: r.summary, chips: [], priority: "high", seconds: 14, fixed: true });
}

function isStale(item) {
  if (S.source === "live" || item.fixed) return performance.now() - item.arrivedAt > STALE_LIVE_WAIT_MS;
  return !!S.lastEvent && matchSeconds(S.lastEvent) - matchSeconds(item.at) > STALE_MATCH_S;
}

function queue(item) {
  item.arrivedAt = performance.now();
  if (item.priority === "high") S.queue.unshift(item); else S.queue.push(item);
  S.queue = S.queue.slice(0, 4);
  if (!S.current || (item.priority === "high" && S.current.priority !== "high")) next();
}

function next() {
  clearTimeout(S.timer);
  const box = $("overlay");
  const old = box.firstElementChild;
  if (old) { old.classList.add("leaving"); setTimeout(() => old.remove(), 300); }
  while (S.queue.length && isStale(S.queue[0])) S.queue.shift();
  const item = S.queue.shift();
  S.current = item ?? null;
  if (!item) return;
  const el = document.createElement("div");
  el.className = "l3";
  const p = S.players[item.playerId];
  el.innerHTML = `<div style="background:${item.teamId ? teamColor(item.teamId) : "#0d5c46"}"></div>
    <div class="content"><div class="kicker">${esc(item.kicker)}${p ? ` · ${p.number} ${esc(p.short_name)}` : ""}</div>
    <div class="title">${esc(item.title)}</div><div class="body">${esc(item.body)}</div>${figures(item.chips)}</div>`;
  setTimeout(() => box.appendChild(el), old ? 300 : 0);
  const seconds = item.fixed ? item.seconds : Math.max(3, item.seconds * Math.min(1, 30 / S.speed));
  S.timer = setTimeout(next, seconds * 1000);
}

start();
