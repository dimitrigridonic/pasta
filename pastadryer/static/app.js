const $ = (id) => document.getElementById(id);
let state = null, view = null, phaseTotal = null;
let histMetric = "hum", lastHist = null, allPrograms = [];

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
  const box = $("sensors"); box.innerHTML = "";
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
// Sensor-Nummer -> Position [x,y] (Seitenansicht). Telai-Reihen y: 110..200.
// unterste Ebene (y315): S6 links · S4 mitte · S5 rechts | 3.-unterste (y265): S2 · S1 · S3
const DRYER_POS = { 6: [230, 315], 4: [320, 315], 5: [410, 315], 2: [230, 265], 1: [320, 265], 3: [410, 265] };
const sNum = (name) => { const m = String(name).match(/(\d+)/); return m ? +m[1] : null; };

function renderDryer(s) {
  const box = $("dryer"); if (!box) return;
  // Welche Seite bläst tatsächlich? (gilt für Manuell UND Programm)
  const leftBlow = !!((s.heaters[0] && s.heaters[0].on) || (s.fans[0] && s.fans[0].on));
  const rightBlow = !!((s.heaters[1] && s.heaters[1].on) || (s.fans[1] && s.fans[1].on));
  const windOn = leftBlow || rightBlow;
  const windLeft = leftBlow && !rightBlow ? true : (rightBlow && !leftBlow ? false : s.active_side === 0);
  $("dryer-side").textContent = windOn ? `Wind ${windLeft ? "◀ nach LINKS" : "nach RECHTS ▶"}` : "kein Wind";
  const val = {}; s.sensors.forEach((se) => { const n = sNum(se.name); if (n) val[n] = se; });

  // Telai als Regale (obere Hälfte)
  let telai = "";
  [190, 215, 240, 265, 290, 315].forEach((y) => {
    telai += `<rect x="175" y="${y - 2}" width="290" height="4" rx="2" fill="#2c2c34"/>`;
  });

  // Sensor-Chips
  let sensors = "";
  for (const n in DRYER_POS) {
    const [x, y] = DRYER_POS[n], se = val[n];
    const t = se && se.temp != null ? `${se.temp.toFixed(1)}°` : "–";
    const h = se && se.hum != null ? `${Math.round(se.hum)}%` : "";
    sensors += `<g>
      <rect x="${x - 37}" y="${y - 15}" width="74" height="30" rx="9" fill="#17171c" stroke="#34343e"/>
      <circle cx="${x - 25}" cy="${y}" r="4.5" fill="#009353"/>
      <text x="${x - 14}" y="${y - 2}" class="dl-s" text-anchor="start">S${n}</text>
      <text x="${x - 14}" y="${y + 10}" class="dl-v" text-anchor="start">${t} ${h}</text></g>`;
  }

  // Heizung+Lüfter-Modul (im Kanal), aktive Seite mit Ring + Glow
  const mod = (cx, isLeft) => {
    const hOn = s.heaters[isLeft ? 0 : 1] && s.heaters[isLeft ? 0 : 1].on;
    const fOn = s.fans[isLeft ? 0 : 1] && s.fans[isLeft ? 0 : 1].on;
    const ring = ((isLeft && leftBlow) || (!isLeft && rightBlow))
      ? `<rect x="${cx - 54}" y="55" width="108" height="50" rx="14" fill="none" stroke="#009353" stroke-width="2.5"/>` : "";
    const side = isLeft ? "L" : "R";
    return `${ring}
      <rect x="${cx - 48}" y="63" width="58" height="34" rx="9" fill="${hOn ? "#ff7a59" : "#382722"}"${hOn ? ' filter="url(#dglow)"' : ""}/>
      <text x="${cx - 19}" y="84" text-anchor="middle" class="dl-m" fill="${hOn ? "#2a0f06" : "#8a7068"}">HEIZ ${side}</text>
      <circle cx="${cx + 30}" cy="80" r="17" fill="${fOn ? "#56b6e0" : "#22303a"}"${fOn ? ' filter="url(#dglow)"' : ""}/>
      <text x="${cx + 30}" y="84" text-anchor="middle" class="dl-m" fill="${fOn ? "#04121a" : "#5a6e78"}">FAN</text>`;
  };

  const duct = "M 130 240 V 104 Q 130 80 154 80 H 486 Q 510 80 510 104 V 240";
  const loop = "M 152 96 H 488 Q 510 96 510 118 V 350 Q 510 372 488 372 H 152 Q 130 372 130 350 V 118 Q 130 96 152 96 Z";

  box.innerHTML = `<svg viewBox="0 0 640 430" class="dryer-svg" xmlns="http://www.w3.org/2000/svg">
    <defs><filter id="dglow" x="-60%" y="-60%" width="220%" height="220%">
      <feGaussianBlur stdDeviation="3.5" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter></defs>
    <!-- Kammer -->
    <rect x="70" y="150" width="500" height="250" rx="16" fill="#101014" stroke="#2a2a30" stroke-width="1.5"/>
    ${telai}
    <!-- Windkanal: separater Tubus mit runden 90°-Bögen runter in den Kasten -->
    <path d="${duct}" fill="none" stroke="#34343c" stroke-width="26" stroke-linecap="round"/>
    <path d="${duct}" fill="none" stroke="#101014" stroke-width="15" stroke-linecap="round"/>
    <!-- Luftzirkulation: nur wenn Wind läuft; linke Seite -> nach links (rev) -->
    ${windOn ? `<path d="${loop}" class="dryer-flow ${windLeft ? "rev" : ""}"/>` : ""}
    ${mod(200, true)}
    ${mod(440, false)}
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
    let l = `🔥 Heizt leeren Kasten auf ${s.preheat.target}°C · aktuell ${fmt(s.agg_temp)}°C`;
    if (s.preheat.remaining != null) l += ` · max noch ${dur(s.preheat.remaining)}`;
    l += ` · dann Pasta einstellen`;
    $("prog-phase").textContent = l;
    const pct = s.agg_temp != null ? Math.min(100, Math.max(0, (s.agg_temp - 20) / (s.preheat.target - 20) * 100)) : 0;
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
  if (!s.reading_ok || (age != null && age > 90)) { conn.textContent = "⚠ Messwerte alt"; conn.classList.add("stale"); }
  else { conn.textContent = `aktuell (${age != null ? Math.round(age) : "?"}s)`; conn.classList.remove("stale"); }
  $("foot").textContent = "Pasta-Trockner · Aqara M2 · lokal";
}

/* ---------- Bedienung ---------- */
document.querySelectorAll(".mode").forEach((b) =>
  b.addEventListener("click", async () => {
    view = b.dataset.mode; applyView();
    if (view === "off") render(await api("/api/off", null, "POST"));
  })
);
$("program-start").onclick = async () => { phaseTotal = null; render(await api("/api/program/start", { name: $("program-select").value })); };
$("program-stop").onclick = async () => render(await api("/api/program/stop", null, "POST"));
$("program-skip").onclick = async () => { phaseTotal = null; render(await api("/api/program/skip", null, "POST")); };
$("fault-reset").onclick = async () => render(await api("/api/fault/clear", null, "POST"));

/* ---------- Chart ---------- */
function drawChart() {
  const cv = $("hist-canvas"); if (!lastHist) return;
  const dpr = window.devicePixelRatio || 1, W = cv.clientWidth, H = cv.clientHeight;
  cv.width = W * dpr; cv.height = H * dpr;
  const ctx = cv.getContext("2d"); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);
  const { names, series } = lastHist, padL = 34, padR = 8, padT = 10, padB = 18;
  const idx = histMetric === "temp" ? 1 : 2;
  const aids = Object.keys(series).filter((a) => series[a].length);
  let tmin = Infinity, tmax = -Infinity, vmin = Infinity, vmax = -Infinity;
  aids.forEach((a) => series[a].forEach((p) => {
    if (p[0] < tmin) tmin = p[0]; if (p[0] > tmax) tmax = p[0];
    if (p[idx] != null) { if (p[idx] < vmin) vmin = p[idx]; if (p[idx] > vmax) vmax = p[idx]; }
  }));
  // Ideallinie (nur bei Feuchte + laufendem Programm) ins Wertefenster einbeziehen
  const idealPhases = histMetric === "hum" && state && state.program_started ? progPhases(state.program) : null;
  if (idealPhases && isFinite(tmin)) {
    [Math.max(tmin, state.program_started), tmax].forEach((tt) => {
      const v = idealHumidityAt(tt, state.program_started, idealPhases);
      if (v != null) { if (v < vmin) vmin = v; if (v > vmax) vmax = v; }
    });
  }
  ctx.font = "11px system-ui"; ctx.fillStyle = "#8b8b93";
  if (!isFinite(vmin)) { ctx.fillText("noch keine Daten – läuft mit, sobald geloggt wird", padL, H / 2); $("hist-legend").innerHTML = ""; return; }
  if (vmin === vmax) { vmin -= 1; vmax += 1; }
  vmin = Math.floor(vmin - 1); vmax = Math.ceil(vmax + 1);
  const X = (t) => padL + (tmax === tmin ? 0 : (t - tmin) / (tmax - tmin)) * (W - padL - padR);
  const Y = (v) => H - padB - (v - vmin) / (vmax - vmin) * (H - padT - padB);
  // Gitter
  ctx.strokeStyle = "#26262b"; ctx.lineWidth = 1;
  for (let g = 0; g <= 4; g++) {
    const v = vmin + (vmax - vmin) * g / 4, y = Y(v);
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(W - padR, y); ctx.stroke();
    ctx.fillText(v.toFixed(0) + (histMetric === "temp" ? "°" : "%"), 2, y + 4);
  }
  // Linien
  const leg = $("hist-legend"); leg.innerHTML = "";
  aids.forEach((a, i) => {
    const col = PALETTE[i % PALETTE.length];
    ctx.strokeStyle = col; ctx.lineWidth = 1.8; ctx.beginPath();
    let started = false;
    series[a].forEach((p) => {
      if (p[idx] == null) return;
      const x = X(p[0]), y = Y(p[idx]);
      started ? ctx.lineTo(x, y) : ctx.moveTo(x, y); started = true;
    });
    ctx.stroke();
    const sp = document.createElement("span");
    sp.innerHTML = `<i style="background:${col}"></i>${names[a] || "Sensor " + a}`;
    leg.appendChild(sp);
  });
  // Harte Ideallinie (weiss, gestrichelt)
  if (idealPhases && isFinite(tmin)) {
    const t0 = Math.max(tmin, state.program_started);
    ctx.strokeStyle = "#fff"; ctx.lineWidth = 2; ctx.setLineDash([6, 4]); ctx.beginPath();
    let st = false;
    for (let k = 0; k <= 80; k++) {
      const tt = t0 + (tmax - t0) * k / 80;
      const v = idealHumidityAt(tt, state.program_started, idealPhases);
      if (v == null) continue;
      const x = X(tt), y = Y(v);
      st ? ctx.lineTo(x, y) : ctx.moveTo(x, y); st = true;
    }
    ctx.stroke(); ctx.setLineDash([]);
    const sp = document.createElement("span");
    sp.innerHTML = `<i style="background:#fff"></i>Ideallinie`;
    leg.appendChild(sp);
  }
}
async function loadHistory() {
  const h = $("hist-range").value;
  $("hist-csv").href = `/api/history.csv?hours=${h}`;
  lastHist = await api(`/api/history?hours=${h}`);
  const n = Object.values(lastHist.series || {}).reduce((a, b) => a + b.length, 0);
  $("hist-stat").textContent = n ? `· ${n} Punkte` : "";
  drawChart();
}
$("hist-range").onchange = loadHistory;
$("hist-refresh").onclick = loadHistory;
$("hist-metric").onclick = () => { histMetric = histMetric === "hum" ? "temp" : "hum"; $("hist-metric").textContent = histMetric === "hum" ? "Feuchte" : "Temperatur"; drawChart(); };
$("hist-clear").onclick = async () => { if (confirm("Verlauf wirklich löschen?")) { await api("/api/history/clear", {}); loadHistory(); } };
window.addEventListener("resize", () => drawChart());

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
loadHistory(); setInterval(loadHistory, 60000);
loadPrograms();
