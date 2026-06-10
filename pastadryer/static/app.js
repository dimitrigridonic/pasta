const $ = (id) => document.getElementById(id);
let state = null;
let view = null;          // 'off' | 'manual' | 'program'
let phaseTotal = null;
let histMetric = "hum";   // 'hum' | 'temp'
const PALETTE = ["#e0a44c", "#4ca7e0", "#e2603b", "#7bc86c", "#c678dd", "#56b6c2", "#d19a66", "#e06c75"];

async function api(path, body) {
  const opts = body
    ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
    : {};
  return (await fetch(path, opts)).json();
}
const fmt = (v, d = 1) => (v === null || v === undefined ? "–" : Number(v).toFixed(d));
function mmss(sec) {
  if (sec == null) return "";
  const m = Math.floor(sec / 60), h = Math.floor(m / 60);
  return h > 0 ? `${h}h ${m % 60}min` : `${m} min`;
}

function applyView() {
  document.querySelectorAll(".mode").forEach((b) => b.classList.toggle("active", b.dataset.mode === view));
  $("manual-card").classList.toggle("hidden", view !== "manual");
  $("program-card").classList.toggle("hidden", view !== "program");
}

function buildActuators(s) {
  const box = $("actuators");
  if (box.childElementCount === s.heaters.length + s.fans.length) return;
  box.innerHTML = "";
  [...s.heaters.map((h) => ["🔥", h]), ...s.fans.map((f) => ["🌀", f])].forEach(([icon, ch]) => {
    const d = document.createElement("div");
    d.className = "pill";
    d.id = `pill-${ch.aid}-${ch.iid}`;
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
    b.className = "toggle";
    b.id = `t-${ch.aid}-${ch.iid}`;
    b.textContent = ch.name;
    b.onclick = async () => {
      const on = !(state && chOn(state, ch.aid, ch.iid));
      render(await api("/api/manual", { aid: ch.aid, iid: ch.iid, on }));
    };
    box.appendChild(b);
  });
}
const chOn = (s, aid, iid) =>
  [...s.heaters, ...s.fans].find((c) => c.aid === aid && c.iid === iid)?.on;

function renderSensors(s) {
  const box = $("sensors");
  box.innerHTML = "";
  s.sensors.forEach((se) => {
    const d = document.createElement("div");
    d.className = "sensor";
    d.innerHTML = `<div class="sname">${se.name}</div>
      <div class="svals"><span class="t">${fmt(se.temp)}°</span><span class="h">${fmt(se.hum, 0)}%</span></div>`;
    box.appendChild(d);
  });
  $("agg-note").textContent = `· Regelwert = ${s.aggregate}`;
}

function render(s) {
  state = s;
  if (view === null) view = s.mode;
  if (s.mode === "program" && s.program) view = "program";
  applyView();

  $("temp").textContent = fmt(s.agg_temp);
  $("hum").textContent = fmt(s.agg_hum, 0);
  $("temp-band").textContent = `Ziel ${s.temp_low}–${s.temp_high}°C`;
  $("hum-target").textContent = s.phase && s.phase.humidity_target != null ? `Ziel ${s.phase.humidity_target}%` : "";

  buildActuators(s);
  [...s.heaters, ...s.fans].forEach((ch, i) => {
    const pill = $(`pill-${ch.aid}-${ch.iid}`);
    if (!pill) return;
    pill.classList.toggle("on", !!ch.on);
    // aktive Lüfterseite markieren
    const isFan = i >= s.heaters.length;
    pill.classList.toggle("active", isFan && s.venting && (i - s.heaters.length) === s.fan_active);
  });

  buildManual(s);
  [...s.heaters, ...s.fans].forEach((ch) => {
    const b = $(`t-${ch.aid}-${ch.iid}`);
    if (b) b.classList.toggle("on", s.mode === "manual" && ch.on);
  });

  // Programmliste
  const sel = $("program-select");
  if (sel.options.length !== s.programs.length) {
    sel.innerHTML = "";
    s.programs.forEach((p) => { const o = document.createElement("option"); o.value = o.textContent = p; sel.appendChild(o); });
  }
  const running = s.mode === "program" && s.phase;
  $("program-start").classList.toggle("hidden", running);
  $("program-stop").classList.toggle("hidden", !running);
  $("program-status").classList.toggle("hidden", !running);
  if (running) {
    $("prog-name").textContent = s.program;
    const ph = s.phase;
    let line = `Phase ${ph.index + 1}/${ph.count}: ${ph.name}`;
    if (ph.humidity_target != null) line += ` · Ziel ${ph.humidity_target}% rF`;
    line += ` · ${ph.temp_low}–${ph.temp_high}°C`;
    if (s.phase_remaining != null) line += ` · noch ${mmss(s.phase_remaining)}`;
    $("prog-phase").textContent = line;
    if (s.phase_remaining != null) {
      if (phaseTotal === null || s.phase_remaining > phaseTotal) phaseTotal = s.phase_remaining;
      $("prog-bar-fill").style.width = `${Math.min(100, Math.max(0, phaseTotal ? (1 - s.phase_remaining / phaseTotal) * 100 : 0))}%`;
    }
  } else phaseTotal = null;

  renderSensors(s);

  // Banner
  const banner = $("banner");
  if (s.safety_tripped) {
    banner.textContent = `⚠️ Sicherheitsabschaltung: ≥ ${s.max_temp}°C – Heizungen aus`;
    banner.classList.remove("hidden");
  } else if (s.error) {
    banner.textContent = `⚠️ ${s.error}`; banner.classList.remove("hidden");
  } else banner.classList.add("hidden");

  const foot = $("foot"), age = s.reading_age;
  if (!s.reading_ok || (age != null && age > 90)) {
    foot.textContent = "⚠ Messwerte veraltet – Hub erreichbar?"; foot.classList.add("stale");
  } else { foot.textContent = `aktualisiert vor ${age != null ? Math.round(age) : "?"}s`; foot.classList.remove("stale"); }
}

// --- Modus / Bedienung ---
document.querySelectorAll(".mode").forEach((b) =>
  b.addEventListener("click", async () => {
    view = b.dataset.mode; applyView();
    if (view === "off") render(await api("/api/off"));
    // manual & program: nur Tab zeigen; Aktion per Knopf
  })
);
$("program-start").onclick = async () => { phaseTotal = null; render(await api("/api/program/start", { name: $("program-select").value })); };
$("program-stop").onclick = async () => render(await api("/api/program/stop"));
$("program-skip").onclick = async () => { phaseTotal = null; render(await api("/api/program/skip")); };

// --- Verlauf / Chart ---
function drawChart(names, series) {
  const cv = $("hist-canvas"), ctx = cv.getContext("2d");
  const W = cv.width, H = cv.height, pad = 28;
  ctx.clearRect(0, 0, W, H);
  const aids = Object.keys(series).filter((a) => series[a].length);
  let tmin = Infinity, tmax = -Infinity, vmin = Infinity, vmax = -Infinity;
  const idx = histMetric === "temp" ? 1 : 2;
  aids.forEach((a) => series[a].forEach((p) => {
    if (p[0] < tmin) tmin = p[0]; if (p[0] > tmax) tmax = p[0];
    if (p[idx] != null) { if (p[idx] < vmin) vmin = p[idx]; if (p[idx] > vmax) vmax = p[idx]; }
  }));
  if (!isFinite(vmin)) { ctx.fillStyle = "#a8998a"; ctx.fillText("noch keine Daten", pad, H / 2); return; }
  if (vmin === vmax) { vmin -= 1; vmax += 1; }
  const x = (t) => pad + (tmax === tmin ? 0 : (t - tmin) / (tmax - tmin)) * (W - 2 * pad);
  const y = (v) => H - pad - (v - vmin) / (vmax - vmin) * (H - 2 * pad);
  // Achsen-Beschriftung
  ctx.fillStyle = "#a8998a"; ctx.font = "11px sans-serif";
  ctx.fillText(`${vmax.toFixed(0)}${histMetric === "temp" ? "°" : "%"}`, 2, pad + 4);
  ctx.fillText(`${vmin.toFixed(0)}`, 2, H - pad + 4);
  // Linien
  const leg = $("hist-legend"); leg.innerHTML = "";
  aids.forEach((a, i) => {
    const col = PALETTE[i % PALETTE.length];
    ctx.strokeStyle = col; ctx.lineWidth = 1.6; ctx.beginPath();
    let started = false;
    series[a].forEach((p) => {
      if (p[idx] == null) return;
      const px = x(p[0]), py = y(p[idx]);
      started ? ctx.lineTo(px, py) : ctx.moveTo(px, py); started = true;
    });
    ctx.stroke();
    const sp = document.createElement("span");
    sp.innerHTML = `<i style="background:${col}"></i>${names[a] || "Sensor " + a}`;
    leg.appendChild(sp);
  });
}
async function loadHistory() {
  const h = $("hist-range").value;
  $("hist-csv").href = `/api/history.csv?hours=${h}`;
  const d = await api(`/api/history?hours=${h}`);
  drawChart(d.names || {}, d.series || {});
}
$("hist-range").onchange = loadHistory;
$("hist-refresh").onclick = loadHistory;
$("hist-metric").onclick = () => {
  histMetric = histMetric === "hum" ? "temp" : "hum";
  $("hist-metric").textContent = histMetric === "hum" ? "Feuchte" : "Temperatur";
  loadHistory();
};
$("hist-clear").onclick = async () => {
  if (confirm("Verlauf wirklich löschen?")) { await api("/api/history/clear", {}); loadHistory(); }
};

async function poll() {
  try { render(await api("/api/state")); }
  catch (e) { $("foot").textContent = "⚠ keine Verbindung zum Server"; $("foot").classList.add("stale"); }
}
poll();
setInterval(poll, 3000);
loadHistory();
setInterval(loadHistory, 60000);
