const $ = (id) => document.getElementById(id);
let state = null, view = null, phaseTotal = null;
let histMetric = "hum", lastHist = null, allPrograms = [];
let runMetric = "hum", runShowSensors = false, runData = null, runNames = {}, runsLoaded = false;
// klar unterscheidbare Sensorfarben (rot, orange, gelb, grün, blau, violett)
const RUNCOLS = ["#ff6b6b", "#ffa94d", "#ffd43b", "#69db7c", "#4dabf7", "#da77f2"];
let runGeom = null;

function progPhases(name) {
  const p = allPrograms.find((x) => x.name === name);
  return p ? p.phases : null;
}
function idealHumidityAt(t, started, phases) {
  let el = t - started;
  if (el < 0 || !phases) return null;
  for (const ph of phases) {
    const dur = (ph.duration_h || 0) * 3600;
    if (ph.humidity_start == null) { el -= dur; continue; }
    const end = ph.humidity_end != null ? ph.humidity_end : ph.humidity_start;
    if (dur <= 0) return ph.humidity_start;
    if (el <= dur) return ph.humidity_start + (end - ph.humidity_start) * (el / dur);
    el -= dur;
  }
  const last = phases[phases.length - 1];
  return last ? (last.humidity_end != null ? last.humidity_end : last.humidity_start) : null;
}
const PALETTE = ["#009353", "#56b6e0", "#ff7a59", "#36d399", "#c678dd", "#e0b04c", "#7bd0c8", "#9aa0ff"];

async function api(path, body, method) {
  const opts = { method: method || (body ? "POST" : "GET") };
  if (body) { opts.headers = { "Content-Type": "application/json" }; opts.body = JSON.stringify(body); }
  return (await fetch(path, opts)).json();
}
const fmt = (v, d = 1) => (v == null ? "–" : Number(v).toFixed(d));
function dur(sec) {
  if (sec == null) return "";
  const m = Math.floor(sec / 60), h = Math.floor(m / 60);
  return h > 0 ? `${h} h ${m % 60} min` : `${m} min`;
}

/* ---------- Render Status ---------- */
function applyView() {
  document.querySelectorAll(".mode").forEach((b) => b.classList.toggle("active", b.dataset.mode === view));
  $("manual-card").classList.toggle("hidden", view !== "manual");
  $("program-card").classList.toggle("hidden", view !== "program");
}
const chOn = (s, aid, iid) => [...s.heaters, ...s.fans].find((c) => c.aid === aid && c.iid === iid)?.on;

function buildActuators(s) {
  const box = $("actuators");
  if (box.childElementCount === s.heaters.length + s.fans.length) return;
  box.innerHTML = "";
  [...s.heaters.map((h) => ["🔥", h]), ...s.fans.map((f) => ["🌀", f])].forEach(([icon, ch]) => {
    const d = document.createElement("div");
    d.className = "pill"; d.id = `pill-${ch.aid}-${ch.iid}`;
    d.innerHTML = `<span class="dot"></span>${icon} ${ch.name}`;
    box.appendChild(d);
  });
}
function buildManual(s) {
  const box = $("manual-toggles");
  if (box.childElementCount === s.heaters.length + s.fans.length) return;
  box.innerHTML = "";
  [...s.heaters, ...s.fans].forEach((ch) => {
    const b = document.createElement("button");
    b.className = "toggle"; b.id = `t-${ch.aid}-${ch.iid}`; b.textContent = ch.name;
    b.onclick = async () => render(await api("/api/manual", { aid: ch.aid, iid: ch.iid, on: !chOn(state, ch.aid, ch.iid) }));
    box.appendChild(b);
  });
}
function renderSensors(s) {
  const box = $("sensors"); if (!box) return; box.innerHTML = "";
  s.sensors.forEach((se) => {
    const d = document.createElement("div"); d.className = "sensor";
    const batt = se.batt != null ? `<span class="sbatt">🔋${se.batt}%</span>` : "";
    d.innerHTML = `<div class="sname"><span>${se.name}</span><span class="edit">✎</span></div>
      <div class="svals"><span class="t">${fmt(se.temp)}°</span><span class="h">${fmt(se.hum, 0)}%</span>${batt}</div>`;
    d.querySelector(".edit").onclick = async () => {
      const name = prompt(`Name für Sensor (aid ${se.aid}):`, se.name);
      if (name != null) render(await api("/api/sensor/name", { aid: se.aid, name }));
    };
    box.appendChild(d);
  });
  $("agg-note").textContent = `· Regelwert = ${s.aggregate}`;
}

/* ---------- Trockner-Schema ---------- */
// Sensor-Nummer -> Position [x,y] (Seitenansicht). Spalten links/mitte/rechts.
const DRYER_POS = { 2: [240, 248], 1: [390, 248], 3: [540, 248], 6: [240, 332], 4: [390, 332], 5: [540, 332] };
const sNum = (name) => { const m = String(name).match(/(\d+)/); return m ? +m[1] : null; };

function renderDryer(s) {
  const box = $("dryer"); if (!box) return;
  const leftBlow = !!((s.heaters[0] && s.heaters[0].on) || (s.fans[0] && s.fans[0].on));
  const rightBlow = !!((s.heaters[1] && s.heaters[1].on) || (s.fans[1] && s.fans[1].on));
  const windOn = leftBlow || rightBlow;
  const windLeft = leftBlow && !rightBlow ? true : (rightBlow && !leftBlow ? false : s.active_side === 0);
  const sideHum = (s.hum_left != null && s.hum_right != null) ? `  ·  Feuchte L ${fmt(s.hum_left, 0)}% / R ${fmt(s.hum_right, 0)}%` : "";
  $("dryer-side").textContent = (windOn ? `Wind ${windLeft ? "◀ nach LINKS" : "nach RECHTS ▶"}` : "kein Wind") + sideHum;
  const val = {}; s.sensors.forEach((se) => { const n = sNum(se.name); if (n) val[n] = se; });

  let telai = "";
  [192, 228, 264, 300, 336, 372].forEach((y) => {
    telai += `<rect x="160" y="${y - 2}" width="460" height="4" rx="2" fill="#2c2c34"/>`;
  });

  // Sensor-Kärtchen: Nummer, Temp, Feuchte + farbige Batterie
  let sensors = "";
  for (const n in DRYER_POS) {
    const [x, y] = DRYER_POS[n], se = val[n];
    const t = se && se.temp != null ? `${se.temp.toFixed(1)}°` : "–";
    const h = se && se.hum != null ? `${Math.round(se.hum)}%` : "";
    let battFill = "#5a5a66", lvl = 0, battPct = "–";
    if (se && se.batt != null) {
      lvl = Math.max(0, Math.min(100, se.batt)); battPct = `${se.batt}%`;
      battFill = se.batt >= 50 ? "#009353" : se.batt >= 20 ? "#e0a020" : "#e2433f";
    }
    sensors += `<g>
      <rect x="${x - 61}" y="${y - 23}" width="122" height="46" rx="12" fill="#17171c" stroke="#34343e"/>
      <circle cx="${x - 50}" cy="${y - 9}" r="4" fill="#009353"/>
      <text x="${x - 41}" y="${y - 5}" class="dl-s" text-anchor="start">S${n}</text>
      <text x="${x + 25}" y="${y - 5}" class="dl-b" text-anchor="end" fill="${battFill}">${battPct}</text>
      <rect x="${x + 30}" y="${y - 15}" width="20" height="11" rx="2.5" fill="none" stroke="#6a6a76" stroke-width="1"/>
      <rect x="${x + 31}" y="${y - 14}" width="${(18 * lvl / 100).toFixed(1)}" height="9" rx="1.5" fill="${battFill}"/>
      <rect x="${x + 50}" y="${y - 12.5}" width="2.5" height="6" rx="1" fill="#6a6a76"/>
      <text x="${x - 50}" y="${y + 15}" class="dl-v" text-anchor="start">${t}   ${h}</text></g>`;
  }

  const mod = (cx, isLeft) => {
    const hi = isLeft ? 0 : 1;
    const heater = s.heaters[hi] || {}, fan = s.fans[hi] || {};
    const hOn = heater.on, fOn = fan.on;
    const ring = ((isLeft && leftBlow) || (!isLeft && rightBlow))
      ? `<rect x="${cx - 54}" y="55" width="108" height="50" rx="14" fill="none" stroke="#009353" stroke-width="2.5"/>` : "";
    const side = isLeft ? "L" : "R";
    return `${ring}
      <g class="ctl" data-aid="${heater.aid}" data-iid="${heater.iid}" style="cursor:pointer">
        <rect x="${cx - 48}" y="63" width="58" height="34" rx="9" fill="${hOn ? "#ff7a59" : "#382722"}"${hOn ? ' filter="url(#dglow)"' : ""}/>
        <text x="${cx - 19}" y="84" text-anchor="middle" class="dl-m" fill="${hOn ? "#2a0f06" : "#8a7068"}" style="pointer-events:none">HEIZ ${side}</text></g>
      <g class="ctl" data-aid="${fan.aid}" data-iid="${fan.iid}" style="cursor:pointer">
        <circle cx="${cx + 30}" cy="80" r="17" fill="${fOn ? "#56b6e0" : "#22303a"}"${fOn ? ' filter="url(#dglow)"' : ""}/>
        <text x="${cx + 30}" y="84" text-anchor="middle" class="dl-m" fill="${fOn ? "#04121a" : "#5a6e78"}" style="pointer-events:none">FAN ${side}</text></g>`;
  };

  const duct = "M 150 240 V 104 Q 150 80 174 80 H 606 Q 630 80 630 104 V 240";
  const loop = "M 174 96 H 606 Q 630 96 630 118 V 366 Q 630 388 606 388 H 174 Q 150 388 150 366 V 118 Q 150 96 174 96 Z";

  box.innerHTML = `<svg viewBox="0 0 780 425" class="dryer-svg" xmlns="http://www.w3.org/2000/svg">
    <defs><filter id="dglow" x="-60%" y="-60%" width="220%" height="220%">
      <feGaussianBlur stdDeviation="3.5" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter></defs>
    <rect x="90" y="150" width="600" height="258" rx="16" fill="#101014" stroke="#2a2a30" stroke-width="1.5"/>
    ${telai}
    <path d="${duct}" fill="none" stroke="#34343c" stroke-width="26" stroke-linecap="round"/>
    <path d="${duct}" fill="none" stroke="#101014" stroke-width="15" stroke-linecap="round"/>
    ${windOn ? `<path d="${loop}" class="dryer-flow ${windLeft ? "rev" : ""}"/>` : ""}
    ${mod(210, true)}
    ${mod(570, false)}
    ${sensors}
  </svg>`;
}

function render(s) {
  state = s;
  if (view === null) view = s.mode;
  if (s.mode === "program" && s.program) view = "program";
  applyView();

  $("temp").textContent = fmt(s.agg_temp);
  $("hum").textContent = fmt(s.agg_hum, 0);
  $("temp-sub").textContent = `Band ${s.temp_low}–${s.temp_high} °C`;
  $("hum-sub").textContent = s.phase && s.phase.humidity_target != null
    ? `Ideallinie ${s.phase.humidity_target}%` : (s.preheating ? "Vorheizen…" : "");
  const badge = $("mode-badge");
  if (s.resting) { badge.textContent = "💤 Ruhephase"; badge.className = "badge rest"; }
  else {
    badge.textContent = s.mode === "off" ? "Aus" : s.mode === "manual" ? "Manuell" : "Programm";
    badge.className = "badge " + (s.mode === "program" ? "program" : s.mode === "manual" ? "manual" : "");
  }

  let bandTxt = `Feuchte folgt Ideallinie · Heizung ${s.temp_low}–${s.temp_high}°C · Lüfter = Notnagel`;
  if (s.drop_rate != null) bandTxt += ` · Abfall ${s.drop_rate}%/h`;
  if (s.resting) bandTxt += ` · 💤 Ruhe bis ≥${s.rest_recover_to}%`;
  if (s.safety_tripped) bandTxt += " · ⚠️ Sicherheit aktiv";
  $("band").textContent = bandTxt;

  buildManual(s);
  [...s.heaters, ...s.fans].forEach((ch) => {
    const b = $(`t-${ch.aid}-${ch.iid}`); if (b) b.classList.toggle("on", s.mode === "manual" && ch.on);
  });

  const sel = $("program-select");
  if (sel.options.length !== s.programs.length || [...sel.options].some((o, i) => o.value !== s.programs[i])) {
    const keep = sel.value; sel.innerHTML = "";
    s.programs.forEach((p) => { const o = document.createElement("option"); o.value = o.textContent = p; sel.appendChild(o); });
    if (s.programs.includes(keep)) sel.value = keep;
  }
  const running = s.mode === "program" && (s.preheating || s.phase);
  $("program-start").classList.toggle("hidden", running);
  $("program-stop").classList.toggle("hidden", !running);
  $("program-status").classList.toggle("hidden", !running);
  $("prog-running").classList.toggle("hidden", !running);
  $("prog-empty").classList.toggle("hidden", running);
  if (running && s.preheating) {
    $("prog-name").textContent = `${s.program} — Vorheizen`;
    let l = `🔥 Beide Heizungen volle Leistung (leerer Kasten)`;
    if (s.preheat.remaining != null) l += ` · noch ${dur(s.preheat.remaining)}`;
    l += ` · dann Pasta einstellen`;
    $("prog-phase").textContent = l;
    const tot = s.preheat.total_min ? s.preheat.total_min * 60 : null;
    const pct = (tot && s.preheat.remaining != null) ? Math.min(100, Math.max(0, (1 - s.preheat.remaining / tot) * 100)) : 0;
    $("prog-bar-fill").style.width = `${pct}%`;
    phaseTotal = null;
  } else if (running) {
    $("prog-name").textContent = s.program;
    const ph = s.phase;
    let line = `Phase ${ph.index + 1}/${ph.count}: ${ph.name}`;
    if (ph.humidity_target != null) line += ` · Min. ${ph.humidity_target}% rF`;
    line += ` · ${ph.temp_low}–${ph.temp_high}°C`;
    if (s.phase_remaining != null) line += ` · noch ${dur(s.phase_remaining)}`;
    if (s.resting) line += ` · 💤 Ruhe bis Feuchte ≥ ${s.rest_recover_to}%`;
    $("prog-phase").textContent = line;
    if (s.phase_remaining != null) {
      if (phaseTotal === null || s.phase_remaining > phaseTotal) phaseTotal = s.phase_remaining;
      $("prog-bar-fill").style.width = `${Math.min(100, Math.max(0, phaseTotal ? (1 - s.phase_remaining / phaseTotal) * 100 : 0))}%`;
    }
  } else phaseTotal = null;

  const tr = s.humidity_trim || 0;
  $("nudge-val").textContent = tr === 0 ? "normal" : (tr < 0 ? `${tr} % · schneller` : `+${tr} % · sanfter`);

  const hr = s.hum_ref || "all";
  const guideNums = (s.humidity_guide || []).map((n) => (String(n).match(/\d+/) || [])[0]).filter(Boolean).join("·");
  $("href-all").classList.toggle("active", hr === "all");
  $("href-guide").classList.toggle("active", hr === "guide");
  $("href-guide").textContent = guideNums ? `Untere ${guideNums}` : "Untere";
  $("href-val").textContent = hr === "guide" ? "Bezug: untere Reihe" : "Bezug: Schnitt aller";

  const ov = s.overrides || [];
  $("overrides-clear").classList.toggle("hidden", ov.length === 0);
  if (ov.length && s.mode === "program") {
    const rem = Math.min(...ov.map((o) => (o.remaining != null ? o.remaining : 0)));
    const cur = $("prog-phase").textContent;
    if (!cur.includes("Eingriff")) $("prog-phase").textContent = cur + ` · ✋ Eingriff aktiv · auto-aus in ${dur(rem)} (Programm läuft weiter)`;
  }

  renderSensors(s);
  renderDryer(s);

  const fault = $("fault");
  if (s.fault) {
    $("fault-text").textContent = `🚨 NOT-AUS verriegelt: ${s.fault_reason || "Heizung lief zu lange"}. Programm gestoppt – schaltet nicht von selbst wieder ein.`;
    fault.classList.remove("hidden");
  } else fault.classList.add("hidden");

  const banner = $("banner");
  if (s.safety_tripped) { banner.textContent = `⚠️ Sicherheitsabschaltung: ≥ ${s.max_temp}°C – Heizungen aus`; banner.classList.remove("hidden"); }
  else if (s.error) { banner.textContent = `⚠️ ${s.error}`; banner.classList.remove("hidden"); }
  else banner.classList.add("hidden");

  const conn = $("conn"), age = s.reading_age;
  const humanAge = (a) => a == null ? "?" : a < 90 ? `${Math.round(a)}s` : a < 5400 ? `${Math.round(a / 60)} min` : `${(a / 3600).toFixed(1)} h`;
  if (s.mqtt_connected === false) { conn.textContent = "⚠ keine Verbindung"; conn.classList.add("stale"); }
  else if (!s.reading_ok) { conn.textContent = "warte auf Werte…"; conn.classList.remove("stale"); }
  // Push-basiert: Sensoren senden nur bei Änderung. „Lange still" erst nach >75 Min (Heartbeat).
  else if (age != null && age > 4500) { conn.textContent = `⚠ Sensoren lange still (${humanAge(age)})`; conn.classList.add("stale"); }
  else { conn.textContent = `Push · letzter Wert vor ${humanAge(age)}`; conn.classList.remove("stale"); }
  $("foot").textContent = "Pasta-Trockner · Zigbee (zigbee2mqtt) · Push, lokal";
  drawChart();
}

/* ---------- Bedienung ---------- */
document.querySelectorAll(".mode").forEach((b) =>
  b.addEventListener("click", async () => {
    view = b.dataset.mode; applyView();
    if (view === "off") render(await api("/api/off", null, "POST"));
    else if (view === "manual") render(await api("/api/manual/enter", null, "POST"));
  })
);
$("program-start").onclick = async () => { phaseTotal = null; render(await api("/api/program/start", { name: $("program-select").value })); };
$("program-stop").onclick = async () => render(await api("/api/program/stop", null, "POST"));
$("program-skip").onclick = async () => { phaseTotal = null; render(await api("/api/program/skip", null, "POST")); };
$("nudge-faster").onclick = async () => render(await api("/api/program/nudge", { delta: -1 }));
$("nudge-slower").onclick = async () => render(await api("/api/program/nudge", { delta: 1 }));
$("href-all").onclick = async () => render(await api("/api/humref", { mode: "all" }));
$("href-guide").onclick = async () => render(await api("/api/humref", { mode: "guide" }));
$("overrides-clear").onclick = async () => render(await api("/api/overrides/clear", null, "POST"));
$("program-select").onchange = () => drawChart();
$("fault-reset").onclick = async () => render(await api("/api/fault/clear", null, "POST"));
$("sensors-read").onclick = async () => {
  const b = $("sensors-read"); b.textContent = "…"; b.disabled = true;
  try { render(await api("/api/sensors/read", null, "POST")); } finally { b.textContent = "↻ Werte holen"; b.disabled = false; }
};

// Im Trockner-Schema Heizung/Lüfter direkt antippen (manuell schalten)
$("dryer").addEventListener("click", async (e) => {
  const g = e.target.closest("[data-aid]");
  if (!g || !g.dataset.aid || g.dataset.aid === "undefined") return;
  const aid = g.dataset.aid, iid = g.dataset.iid;
  render(await api("/api/manual", { aid, iid, on: !chOn(state, aid, iid) }));
});

// Tabs umschalten
document.querySelectorAll(".ptab").forEach((b) =>
  b.addEventListener("click", () => {
    const t = b.dataset.ptab;
    document.querySelectorAll(".ptab").forEach((x) => x.classList.toggle("active", x === b));
    document.querySelectorAll(".panel").forEach((p) => p.classList.toggle("hidden", p.dataset.panel !== t));
    drawChart();
    if (t === "analyse") loadRuns();
  })
);

/* ---------- Chart: Ideallinie des gewählten Programms (keine Sensorwerte) ---------- */
function idealAtHour(hour, phases) {
  let el = hour;
  for (const ph of phases) {
    const d = +ph.duration_h || 0;
    if (ph.humidity_start == null) { el -= d; continue; }
    const end = ph.humidity_end != null ? ph.humidity_end : ph.humidity_start;
    if (d <= 0) return ph.humidity_start;
    if (el <= d) return ph.humidity_start + (end - ph.humidity_start) * (el / d);
    el -= d;
  }
  const last = phases[phases.length - 1];
  return last ? (last.humidity_end != null ? last.humidity_end : last.humidity_start) : null;
}
function selectedProgramName() {
  if (state && state.program) return state.program;
  const sel = $("program-select");
  if (sel && sel.value) return sel.value;
  return allPrograms[0] && allPrograms[0].name;
}
function drawChart() {
  const cv = $("hist-canvas"); if (!cv || !cv.clientWidth) return;
  const dpr = window.devicePixelRatio || 1, W = cv.clientWidth, H = cv.clientHeight;
  cv.width = W * dpr; cv.height = H * dpr;
  const ctx = cv.getContext("2d"); ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, W, H);
  const padL = 36, padR = 12, padT = 12, padB = 22;
  ctx.font = "11px system-ui"; ctx.fillStyle = "#8b8b93";
  const name = selectedProgramName(), phases = progPhases(name);
  if (!phases || !phases.length) { ctx.fillText("kein Programm gewählt", padL, H / 2); $("hist-legend").innerHTML = ""; $("hist-stat").textContent = ""; return; }
  const totalH = phases.reduce((a, ph) => a + (+ph.duration_h || 0), 0) || 1;
  let vmin = Infinity, vmax = -Infinity;
  phases.forEach((ph) => [ph.humidity_start, ph.humidity_end].forEach((v) => { if (v != null) { if (v < vmin) vmin = v; if (v > vmax) vmax = v; } }));
  if (!isFinite(vmin)) { vmin = 40; vmax = 90; }
  vmin = Math.floor(vmin - 5); vmax = Math.ceil(vmax + 5);
  const X = (h) => padL + (h / totalH) * (W - padL - padR);
  const Y = (v) => H - padB - (v - vmin) / (vmax - vmin) * (H - padT - padB);
  ctx.strokeStyle = "#26262b"; ctx.lineWidth = 1;
  for (let g = 0; g <= 4; g++) { const v = vmin + (vmax - vmin) * g / 4, y = Y(v); ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(W - padR, y); ctx.stroke(); ctx.fillStyle = "#8b8b93"; ctx.fillText(v.toFixed(0) + "%", 2, y + 4); }
  for (let g = 0; g <= 4; g++) { const h = totalH * g / 4; ctx.fillStyle = "#8b8b93"; ctx.fillText(Math.round(h) + "h", X(h) - 6, H - 6); }
  ctx.strokeStyle = "#fff"; ctx.lineWidth = 2; ctx.setLineDash([6, 4]); ctx.beginPath();
  let started = false;
  for (let k = 0; k <= 160; k++) { const h = totalH * k / 160, v = idealAtHour(h, phases); if (v == null) continue; const x = X(h), y = Y(v); started ? ctx.lineTo(x, y) : ctx.moveTo(x, y); started = true; }
  ctx.stroke(); ctx.setLineDash([]);
  let nowLeg = "";
  if (state && state.program === name && state.program_started) {
    const eh = (Date.now() / 1000 - state.program_started) / 3600;
    if (eh >= 0 && eh <= totalH) {
      const x = X(eh); ctx.strokeStyle = "#009353"; ctx.lineWidth = 2; ctx.beginPath(); ctx.moveTo(x, padT); ctx.lineTo(x, H - padB); ctx.stroke();
      const v = idealAtHour(eh, phases); if (v != null) { ctx.fillStyle = "#009353"; ctx.beginPath(); ctx.arc(x, Y(v), 4, 0, 6.283); ctx.fill(); }
      nowLeg = `<span><i style="background:#009353"></i>Jetzt</span>`;
    }
  }
  $("hist-stat").textContent = `· ${name} · ${Math.round(totalH)} h`;
  $("hist-legend").innerHTML = `<span><i style="background:#fff"></i>Ideallinie</span>${nowLeg}`;
}
$("hist-refresh").onclick = () => drawChart();
$("hist-csv").href = "/api/history.csv?hours=100000";
$("hist-clear").onclick = async () => { if (confirm("Aufgezeichnete Sensordaten (CSV) löschen?")) await api("/api/history/clear", {}); };
window.addEventListener("resize", () => drawChart());

/* ---------- Analyse: vergangene Durchgänge (echte aufgezeichnete Kurven) ---------- */
const fmtRunLabel = (r) => {
  const d = new Date(r.start * 1000);
  const dur = (r.end - r.start) / 3600;
  const date = d.toLocaleString("de-CH", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  return `${date} · ${dur < 1 ? Math.round(dur * 60) + " min" : dur.toFixed(1) + " h"}${r.prog ? " · " + r.prog : ""}`;
};
async function loadRuns() {
  const data = await api("/api/runs");
  runNames = data.names || {};
  const runs = data.runs || [];
  const sel = $("run-select");
  $("run-empty").classList.toggle("hidden", runs.length > 0);
  $("run-canvas").classList.toggle("hidden", runs.length === 0);
  if (!runs.length) { sel.innerHTML = ""; $("run-stat").textContent = ""; $("run-legend").innerHTML = ""; return; }
  const prevIdx = (sel._runs && sel.value && sel._runs[+sel.value]) ? +sel.value : 0;
  sel.innerHTML = runs.map((r, i) => `<option value="${i}">${fmtRunLabel(r)}</option>`).join("");
  sel._runs = runs;
  sel.value = String(Math.min(prevIdx, runs.length - 1));
  runsLoaded = true;
  await loadRun(runs[+sel.value]);
}
async function loadRun(r) {
  if (!r) return;
  runData = await api(`/api/run?start=${r.start}&end=${r.end}`);
  $("run-csv").href = `/api/history.csv?start=${r.start}&end=${r.end}`;
  drawRunChart();
}
$("run-select").onchange = () => { const sel = $("run-select"); loadRun(sel._runs[+sel.value]); };
$("run-metric").onclick = () => { runMetric = runMetric === "hum" ? "temp" : "hum"; $("run-metric").textContent = runMetric === "hum" ? "Temp zeigen" : "Feuchte zeigen"; drawRunChart(); };
$("run-lines").onclick = () => { runShowSensors = !runShowSensors; $("run-lines").textContent = runShowSensors ? "Sensoren aus" : "Sensoren zeigen"; drawRunChart(); };
window.addEventListener("resize", () => { if (runData) drawRunChart(); });

function drawRunChart() {
  const cv = $("run-canvas"); if (!cv || !cv.clientWidth || !runData) return;
  const dpr = window.devicePixelRatio || 1, W = cv.clientWidth, H = cv.clientHeight;
  cv.width = W * dpr; cv.height = H * dpr;
  const ctx = cv.getContext("2d"); ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, W, H);
  const padL = 38, padR = 12, padT = 12, padB = 24;
  ctx.font = "11px system-ui";
  const agg = runData.agg || [];
  if (!agg.length) { ctx.fillStyle = "#8b8b93"; ctx.fillText("keine Daten", padL, H / 2); $("run-legend").innerHTML = ""; return; }
  const t0 = runData.start, span = Math.max(1 / 60, (runData.end - t0) / 3600);
  const isHum = runMetric === "hum", vi = isHum ? 2 : 1, unit = isHum ? "%" : "°";
  let vmin = Infinity, vmax = -Infinity;
  const consider = (v) => { if (v != null) { if (v < vmin) vmin = v; if (v > vmax) vmax = v; } };
  agg.forEach((p) => { consider(p[vi]); if (isHum) consider(p[3]); });
  if (runShowSensors) for (const a in runData.sensors) runData.sensors[a].forEach((p) => consider(p[vi]));
  if (!isFinite(vmin)) { vmin = isHum ? 40 : 20; vmax = isHum ? 90 : 40; }
  vmin = Math.floor(vmin - 2); vmax = Math.ceil(vmax + 2);
  const X = (ts) => padL + ((ts - t0) / 3600 / span) * (W - padL - padR);
  const Y = (v) => H - padB - (v - vmin) / (vmax - vmin) * (H - padT - padB);
  ctx.strokeStyle = "#26262b"; ctx.lineWidth = 1;
  for (let g = 0; g <= 4; g++) { const v = vmin + (vmax - vmin) * g / 4, y = Y(v); ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(W - padR, y); ctx.stroke(); ctx.fillStyle = "#8b8b93"; ctx.fillText(v.toFixed(0) + unit, 2, y + 4); }
  for (let g = 0; g <= 4; g++) { const h = span * g / 4; ctx.fillStyle = "#8b8b93"; ctx.fillText(h.toFixed(h < 4 ? 1 : 0) + "h", X(t0 + h * 3600) - 6, H - 6); }
  if (!isHum && state) { const yb = Y(state.temp_high), yb2 = Y(state.temp_low); ctx.fillStyle = "rgba(0,147,83,0.10)"; ctx.fillRect(padL, yb, W - padL - padR, yb2 - yb); }
  let legend = "";
  const drawLine = (pts, idx, color, width, alpha, dash) => {
    ctx.strokeStyle = color; ctx.lineWidth = width; ctx.globalAlpha = alpha; ctx.setLineDash(dash || []); ctx.beginPath();
    let st = false;
    pts.forEach((p) => { if (p[idx] == null) return; const x = X(p[0]), y = Y(p[idx]); st ? ctx.lineTo(x, y) : ctx.moveTo(x, y); st = true; });
    ctx.stroke(); ctx.globalAlpha = 1; ctx.setLineDash([]);
  };
  if (runShowSensors) {
    let i = 0;
    for (const a in runData.sensors) { const col = RUNCOLS[i % RUNCOLS.length]; i++; drawLine(runData.sensors[a], vi, col, 1.3, 0.85); legend += `<span><i style="background:${col}"></i>${runNames[a] || a}</span>`; }
  }
  if (isHum && agg.some((p) => p[3] != null)) { drawLine(agg, 3, "#fff", 1.5, 1, [6, 4]); legend = `<span><i style="background:#fff"></i>Ideallinie</span>` + legend; }
  drawLine(agg, vi, isHum ? "#009353" : "#ff7a59", 2.4, 1);
  legend = `<span><i style="background:${isHum ? "#009353" : "#ff7a59"}"></i>${isHum ? "Feuchte ⌀" : "Temp ⌀"}</span>` + legend;
  $("run-legend").innerHTML = legend;
  const first = agg.find((p) => p[vi] != null), last = [...agg].reverse().find((p) => p[vi] != null);
  const sub = first && last ? `${first[vi].toFixed(isHum ? 0 : 1)}${unit} → ${last[vi].toFixed(isHum ? 0 : 1)}${unit}` : "";
  $("run-stat").textContent = `· ${span < 1 ? Math.round(span * 60) + " min" : span.toFixed(1) + " h"} · ${sub}`;
  runGeom = { t0, span, padL, padR, W, H };
}

/* ---- Analyse-Tooltip: Sensorname + Temp + Feuchte am Mauspunkt ---- */
function nearestAt(series, t) {
  if (!series || !series.length) return null;
  let best = series[0], bd = Math.abs(series[0][0] - t);
  for (const p of series) { const d = Math.abs(p[0] - t); if (d < bd) { bd = d; best = p; } }
  return best;
}
function runHover(e) {
  const cv = $("run-canvas"); if (!runData || !runGeom || !cv) return;
  const rect = cv.getBoundingClientRect();
  const pt = e.touches ? e.touches[0] : e;
  const x = pt.clientX - rect.left;
  const g = runGeom, plotW = g.W - g.padL - g.padR;
  const frac = (x - g.padL) / plotW;
  if (frac < 0 || frac > 1) { runHideTip(); return; }
  const t = g.t0 + frac * g.span * 3600;
  const cross = $("run-cross");
  cross.style.left = x + "px"; cross.style.height = cv.clientHeight + "px"; cross.classList.remove("hidden");
  const a0 = nearestAt(runData.agg, t);
  const when = new Date(a0[0] * 1000).toLocaleString("de-CH", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  let html = `<b>${when}</b><div class="tip-agg">⌀ ${fmt(a0[1], 1)}° · ${fmt(a0[2], 0)}%${a0[3] != null ? ` · Soll ${fmt(a0[3], 0)}%` : ""}</div>`;
  Object.keys(runData.sensors).forEach((a, i) => {
    const p = nearestAt(runData.sensors[a], t); if (!p) return;
    html += `<div><i style="background:${RUNCOLS[i % RUNCOLS.length]}"></i>${runNames[a] || a}: ${fmt(p[1], 1)}° · ${fmt(p[2], 0)}%</div>`;
  });
  const tip = $("run-tip"); tip.innerHTML = html; tip.classList.remove("hidden");
  let tx = x + 14; if (tx + tip.offsetWidth > g.W) tx = x - tip.offsetWidth - 14;
  tip.style.left = Math.max(0, tx) + "px"; tip.style.top = "6px";
}
function runHideTip() { const c = $("run-cross"), t = $("run-tip"); if (c) c.classList.add("hidden"); if (t) t.classList.add("hidden"); }
(() => {
  const cv = $("run-canvas"); if (!cv) return;
  cv.addEventListener("mousemove", runHover);
  cv.addEventListener("mouseleave", runHideTip);
  cv.addEventListener("click", runHover);
  cv.addEventListener("touchstart", runHover, { passive: true });
  cv.addEventListener("touchmove", runHover, { passive: true });
})();

/* ---------- Programm-Editor ---------- */
function phaseRow(ph = {}) {
  const row = document.createElement("div"); row.className = "phase-row";
  row.innerHTML = `
    <input class="p-name" value="${ph.name || "Phase"}" />
    <input class="p-dur" type="number" step="0.5" min="0" value="${ph.duration_h ?? 1}" />
    <input class="p-hs" type="number" min="0" max="100" value="${ph.humidity_start ?? 70}" />
    <input class="p-he" type="number" min="0" max="100" value="${ph.humidity_end ?? ph.humidity_start ?? 70}" />
    <button class="icon-btn p-del" title="Phase entfernen">✕</button>`;
  row.querySelector(".p-del").onclick = () => row.remove();
  return row;
}
function programBlock(p) {
  const wrap = document.createElement("div"); wrap.className = "prog"; wrap.dataset.orig = p.name;
  const head = document.createElement("input"); head.className = "pname"; head.value = p.name;
  const cols = document.createElement("div"); cols.className = "phase-row phase-head";
  cols.innerHTML = `<span>Phase</span><span>Std.</span><span>Feuchte von %</span><span>bis %</span><span></span>`;
  const phases = document.createElement("div"); phases.className = "phases";
  (p.phases || []).forEach((ph) => phases.appendChild(phaseRow(ph)));
  const actions = document.createElement("div"); actions.className = "prog-actions";
  const add = mkBtn("+ Phase", "ghost sm", () => phases.appendChild(phaseRow()));
  const save = mkBtn("Speichern", "primary sm", async () => {
    const body = { name: head.value.trim() || "Programm", old_name: wrap.dataset.orig, phases: gather(phases) };
    await api("/api/programs", body); await loadPrograms();
  });
  const del = mkBtn("Löschen", "ghost sm danger", async () => {
    if (confirm(`Programm "${p.name}" löschen?`)) { await api(`/api/programs/${encodeURIComponent(p.name)}`, null, "DELETE"); await loadPrograms(); }
  });
  actions.append(add, save, del);
  wrap.append(head, cols, phases, actions);
  return wrap;
}
function gather(phasesEl) {
  return [...phasesEl.querySelectorAll(".phase-row")].map((r) => ({
    name: r.querySelector(".p-name").value || "Phase",
    duration_h: parseFloat(r.querySelector(".p-dur").value) || 0,
    humidity_start: parseFloat(r.querySelector(".p-hs").value),
    humidity_end: parseFloat(r.querySelector(".p-he").value),
  }));
}
function mkBtn(txt, cls, fn) { const b = document.createElement("button"); b.className = "btn " + cls; b.textContent = txt; b.onclick = fn; return b; }
async function loadPrograms() {
  allPrograms = await api("/api/programs");
  const box = $("prog-editor"); box.innerHTML = "";
  allPrograms.forEach((p) => box.appendChild(programBlock(p)));
  drawChart();
}
$("prog-new").onclick = () => {
  $("prog-editor").appendChild(programBlock({ name: "Neues Programm", phases: [{ name: "Phase 1", duration_h: 10, humidity_start: 80, humidity_end: 70 }] }));
};

/* ---------- Loop ---------- */
async function poll() {
  try { render(await api("/api/state")); }
  catch (e) { $("conn").textContent = "⚠ keine Verbindung"; $("conn").classList.add("stale"); }
}
poll(); setInterval(poll, 3000);
loadPrograms();
