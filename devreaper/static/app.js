"use strict";
const TOKEN = document.querySelector('meta[name="devreaper-token"]').content;
const $ = (id) => document.getElementById(id);

// ---------- theme: auto (follows Windows) → light → dark ----------
const THEMES = {
  auto: ["Tema: automático (sigue a Windows)", '<svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M8 2a6 6 0 0 1 0 12z" fill="currentColor"/></svg>'],
  light: ["Tema: claro", '<svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><circle cx="8" cy="8" r="3"/><path d="M8 1v1.5M8 13.5V15M1 8h1.5M13.5 8H15M3 3l1 1M12 12l1 1M3 13l1-1M12 4l1-1"/></svg>'],
  dark: ["Tema: oscuro", '<svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path d="M13.5 10.2A6 6 0 0 1 5.8 2.5a6 6 0 1 0 7.7 7.7z" fill="currentColor"/></svg>'],
};
const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch {} },
};
let theme = THEMES[store.get("theme")] ? store.get("theme") : "auto";
function applyTheme() {
  if (theme === "auto") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", theme);
  const b = $("theme");
  b.innerHTML = THEMES[theme][1];
  b.title = THEMES[theme][0] + ". Clic para cambiar.";
  b.setAttribute("aria-label", THEMES[theme][0]);
}
applyTheme();
$("theme").onclick = () => {
  theme = { auto: "light", light: "dark", dark: "auto" }[theme];
  store.set("theme", theme);
  applyTheme();
  renderLive(); if (view === "ram") renderRam();
};

// ---------- vocabulary ----------
const STATE_NAMES = { active: "Activa", inactive: "Inactiva", orphan: "Huérfano", none: "Sin agente" };
const STATE_FILTERS = [["all", "Todos"], ["active", "Activos"], ["inactive", "Inactivos"], ["orphan", "Huérfanos"], ["none", "Sin agente"]];
const KIND_NAMES = { cli: "Terminal", ide: "Editor", app: "App de escritorio" };
const WT_STATUS = {
  merged: ["Ya está en main", "ok"], gone: ["Rama remota borrada", "ok"], missing: ["Carpeta borrada", "bad"],
  open: ["Con cambios propios", ""], main: ["Checkout principal", ""], detached: ["Sin rama", ""],
};
const ORIGIN = { panel: "Panel", bandeja: "Bandeja", "automático": "Ciclo automático" };
const VIEWS = ["proc", "sess", "ports", "wt", "infra", "ram", "log", "settings"];
const LIVE_S = 15 * 60;
const LAYERS = [["dev", "dev_mb", "Desarrollo"], ["agents", "agents_mb", "Agentes"],
  ["infra", "infra_mb", "Docker y WSL"], ["rest", "rest", "Resto del sistema"]];

// ---------- state ----------
let S = null, view = "proc", preview = false, armed = null, armTimer = null;
let focus = null;            // {type: "group", id} | {type: "agent", pid}
let stateFilter = "all", lastSelIdx = null;
const sel = new Set();       // selected group ids
const pending = new Set();
const live = { points: [], groups: new Map() };
const ram = { hours: 168, series: null, fetched: 0 };
const painted = new Map();   // element id -> last HTML, so unchanged regions keep selection and hover

// ---------- formatting ----------
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const mb = (v) => v == null ? "—" : v >= 1024 ? (v / 1024).toFixed(1) + " GB" : Math.round(v) + " MB";
const pct = (v) => v == null ? "—" : (v >= 10 ? Math.round(v) : v.toFixed(1)) + " %";
function dur(h) {
  if (h == null) return "—";
  const m = Math.round(h * 60);
  if (m < 1) return "ahora";
  if (m < 60) return m + " min";
  if (m < 1440) return Math.floor(m / 60) + " h " + (m % 60) + " min";
  const d = Math.floor(m / 1440), rh = Math.floor((m % 1440) / 60);
  return rh ? `${d} d ${rh} h` : `${d} ${d === 1 ? "día" : "días"}`;
}
const ago = (ts) => ts ? dur((Date.now() / 1000 - ts) / 3600) : "—";
const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
function shortPath(p) {
  const parts = String(p || "").split(/[\\/]/).filter(Boolean);
  return parts.length > 3 ? "…\\" + parts.slice(-3).join("\\") : parts.join("\\");
}
function paint(id, html) {
  if (painted.get(id) === html) return false;
  painted.set(id, html); $(id).innerHTML = html;
  return true;
}
const LOCK = (on) => `<svg width="12" height="12" viewBox="0 0 16 16" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.6">
  <rect x="3" y="7" width="10" height="7" rx="1.5" fill="${on ? "currentColor" : "none"}"/>
  <path d="${on ? "M5 7V5a3 3 0 0 1 6 0v2" : "M5 7V5a3 3 0 0 1 5.6-1.5"}"/></svg>`;
const WARN = `<svg width="12" height="12" viewBox="0 0 16 16" aria-hidden="true"><path d="M8 1.5 15 14H1z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/><path d="M8 6v4M8 11.6v.4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>`;
const STATE_DOT = (state) => `<span class="state s-${state}"><span class="dot"></span>${STATE_NAMES[state]}</span>`;

function portChip(p, lockable = true) {
  const on = S.excluded_ports.includes(p);
  return `<span class="port ${on ? "locked" : ""}"><a href="http://localhost:${p}" target="_blank" rel="noopener">${p}</a>${lockable || on ? `
    <button data-lock="${p}" title="${on ? "Quitar protección" : "Proteger este puerto"}"
      aria-label="${on ? "Quitar protección al puerto" : "Proteger el puerto"} ${p}">${LOCK(on)}</button>` : ""}</span>`;
}
// explicit arrow: map() would pass the index as `lockable` and hide the lock on the first port
const portChips = (ports) => ports.length ? `<div class="ports">${ports.map((p) => portChip(p)).join("")}</div>` : '<span class="sub">—</span>';

function idleText(g) {
  const h = g.idle_hours || 0;
  if (g.idle_known) return h < 0.05 ? "En uso ahora" : `Último uso hace ${dur(h)}`;
  return h < 0.1 ? "Midiendo uso…" : `Sin uso en ${dur(h)}`;
}
function agentIdle(s) {
  if (s.idle_hours == null) return "Midiendo actividad…";
  return s.idle_hours < 0.05 ? "Activo ahora" : `Última actividad hace ${dur(s.idle_hours)}`;
}
const agentName = (who) => who ? `${who.agent} · ${who.name}` : "";

function toast(msg, err) {
  const t = $("toast"); t.textContent = msg; t.className = err ? "show err" : "show";
  clearTimeout(t._h); t._h = setTimeout(() => (t.className = ""), 3400);
}

// ---------- API ----------
async function post(action, body = {}) {
  const r = await fetch("/api/" + action, {
    method: "POST", headers: { "Content-Type": "application/json", "X-DevReaper-Token": TOKEN },
    body: JSON.stringify(body),
  });
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || r.statusText);
  S = data.state; pushLive(); render();
  return data.result;
}
function resultMsg(r) {
  if (r.ok) return "Listo";
  let m = r.killed ? `${plural(r.killed, "cerrado", "cerrados")} · ${mb(r.freed_mb)} liberados` : "No había nada que cerrar";
  if (r.kept) m += ` · ${plural(r.kept, "protegido sigue abierto", "protegidos siguen abiertos")}`;
  return m.charAt(0).toUpperCase() + m.slice(1);
}
async function run(action, body, okMsg) {
  try {
    const r = await post(action, body);
    toast(okMsg ? (typeof okMsg === "function" ? okMsg(r) : okMsg) : resultMsg(r));
    return r;
  } catch (e) { toast(e.message, true); }
}

// A button that closes something: the first click arms it, a second click within 3 s fires it.
function confirmBtn({ act, arg = {}, key, label, ask = "¿Seguro?", disabled = false, title = "", small = true, primary = false }) {
  const isArmed = armed === key, busy = pending.has(key);
  return `<button class="btn danger ${small ? "small" : ""} ${primary ? "" : ""} ${isArmed ? "armed" : ""}" data-act="${esc(act)}"
    data-arg="${esc(JSON.stringify(arg))}" data-key="${esc(key)}" ${disabled || busy ? "disabled" : ""}
    title="${esc(title)}" type="button">${busy ? "Cerrando…" : esc(isArmed ? ask : label)}</button>`;
}

// ---------- live data ----------
async function loadLive() {
  try {
    const d = await (await fetch("/api/live", { cache: "no-store" })).json();
    live.points = d.points;
    live.groups = new Map(Object.entries(d.groups));
  } catch { /* the offline note covers it */ }
}
function pushLive() {
  const p = S.live;
  if (!p) return;
  const last = live.points[live.points.length - 1];
  if (!last || p.ts > last.ts) live.points.push(p);
  const cut = p.ts - LIVE_S;
  while (live.points.length && live.points[0].ts < cut) live.points.shift();
  const alive = new Set();
  for (const g of S.groups) {
    alive.add(g.id);
    const arr = live.groups.get(g.id) || [];
    if (!arr.length || arr[arr.length - 1][0] < p.ts) arr.push([p.ts, g.mb, g.cpu]);
    while (arr.length && arr[0][0] < cut) arr.shift();
    live.groups.set(g.id, arr);
  }
  for (const id of [...live.groups.keys()]) if (!alive.has(id)) live.groups.delete(id);
}

// ---------- selectors ----------
const query = () => $("filter").value.trim().toLowerCase();
const matches = (text) => { const q = query(); return !q || String(text).toLowerCase().includes(q); };
const showAll = () => $("show-all").checked;
const byAgent = () => $("group-agent").checked;
const groupById = (id) => S.groups.find((g) => g.id === id);
const agentByPid = (pid) => S.sessions.find((s) => s.pid === pid);
const dueGroups = () => S.groups.filter((g) => !g.keep_reason);
function baseGroups() {
  return S.groups.filter((g) => (showAll() || g.visible || sel.has(g.id)) &&
    matches(`${g.label} ${g.cmd} ${g.kind} ${g.ports.join(" ")} ${agentName(g.session)}`));
}
function procList() {
  if (preview) return dueGroups();
  const base = baseGroups();
  return stateFilter === "all" ? base : base.filter((g) => g.state === stateFilter);
}
function nextAutoText() {
  if (!S.settings.auto_enabled) return "cuando lo actives";
  const left = (S.next_auto - Date.now() / 1000) / 3600;
  return left <= 0 ? "en breve" : "en " + dur(left);
}

// ---------- render ----------
function render() {
  if (!S) return;
  renderPulse();
  renderFacts();
  renderRail();
  renderToolbar();
  if (view === "proc") renderProc();
  if (view === "sess") renderSess();
  if (view === "ports") renderPorts();
  if (view === "wt") renderWt();
  if (view === "infra") renderInfra();
  if (view === "log") renderLog();
  if (view === "settings") renderSettings();
  renderInspector();
}

function renderPulse() {
  const sys = S.system, alert = S.settings.ram_alert_percent;
  $("ram").textContent = Math.round(sys.percent) + " %";
  $("ram-sub").textContent = `${mb(sys.used_mb)} de ${mb(sys.total_mb)} en uso`;
  const m = $("ram-meter");
  m.style.width = sys.percent + "%";
  m.className = sys.percent >= alert ? "bad" : sys.percent >= alert - 10 ? "warn" : "";
  $("ram-limit").style.left = alert + "%";
  $("ram-limit").title = `Aviso al ${alert} %`;
  $("cpu").textContent = pct(S.summary.cpu);
  $("cpus").textContent = plural(sys.cpus, "núcleo", "núcleos");
  renderLive();
}

function renderLive() {
  const box = $("live");
  if (!S || !box) return;
  const pts = live.points.map((p) => ({ ...p, rest: Math.max(0, p.used_mb - p.dev_mb - p.agents_mb - p.infra_mb) }));
  const cur = pts[pts.length - 1];
  $("live-legend").innerHTML = [...LAYERS].map(([cls, key, name]) =>
    `<span><i class="sw ${cls}"></i>${name} <b>${cur ? mb(cur[key]) : "—"}</b></span>`).join("") +
    '<span>últimos 15 min</span>';
  const W = Math.max(280, box.clientWidth), H = Math.max(104, box.clientHeight || 118);
  const mg = { l: 44, r: 10, t: 6, b: 18 }, iw = W - mg.l - mg.r, ih = H - mg.t - mg.b;
  const total = S.system.total_mb;
  const step = total > 24 * 1024 ? 8 * 1024 : total > 12 * 1024 ? 4 * 1024 : 2 * 1024;
  const yMax = Math.ceil(total / step) * step;
  const t1 = cur ? cur.ts : Date.now() / 1000, t0 = t1 - LIVE_S;
  const x = (t) => mg.l + (t - t0) / LIVE_S * iw;
  const y = (v) => mg.t + ih - Math.min(v, yMax) / yMax * ih;
  let grid = "";
  for (let v = 0; v <= yMax; v += step)
    grid += `<line class="gridline" x1="${mg.l}" x2="${W - mg.r}" y1="${y(v)}" y2="${y(v)}"/><text class="axis" x="${mg.l - 6}" y="${Math.round(y(v) + 4)}" text-anchor="end">${Math.round(v / 1024)} GB</text>`;
  let xt = "";
  for (const mins of [15, 10, 5, 0])
    xt += `<text class="axis" x="${x(t1 - mins * 60)}" y="${H - 4}" text-anchor="${mins === 15 ? "start" : mins === 0 ? "end" : "middle"}">${mins ? "−" + mins + " min" : "ahora"}</text>`;
  let areas = "";
  if (pts.length > 1) {
    let base = pts.map(() => 0);
    for (const [cls, key] of LAYERS) {
      const top = pts.map((p, i) => base[i] + p[key]);
      areas += `<path class="area a-${cls}" d="M${pts.map((p, i) => `${x(p.ts).toFixed(1)},${y(top[i]).toFixed(1)}`).join("L")}L${pts.map((p, i) => `${x(p.ts).toFixed(1)},${y(base[i]).toFixed(1)}`).reverse().join("L")}Z"/>`;
      base = top;
    }
  }
  const ly = y(total * S.settings.ram_alert_percent / 100);
  box.innerHTML = `<svg viewBox="0 0 ${W} ${H}" height="${H}" role="img" aria-label="RAM en uso durante los últimos 15 minutos, por tipo">
    ${grid}${areas}${pts.length > 1 ? "" : `<text class="axis" x="${mg.l + iw / 2}" y="${mg.t + ih / 2}" text-anchor="middle">Juntando datos en vivo…</text>`}
    <line class="limit" x1="${mg.l}" x2="${W - mg.r}" y1="${ly}" y2="${ly}"/>
    <text class="limit-label" x="${W - mg.r - 2}" y="${ly - 4}" text-anchor="end">Aviso ${S.settings.ram_alert_percent} %</text>
    ${xt}<line class="xhair" id="live-x" x1="0" x2="0" y1="${mg.t}" y2="${mg.t + ih}" visibility="hidden"/>
    <rect x="${mg.l}" y="${mg.t}" width="${iw}" height="${ih}" fill="transparent"/></svg>`;
  hover(box.querySelector("svg"), $("live-tip"), $("live-x"), pts, x, W, mg, iw, t0, LIVE_S, (p) =>
    `<b>${new Date(p.ts * 1000).toLocaleTimeString("es-PE", { hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" })} · ${mb(p.used_mb)} usados · CPU ${pct(p.cpu)}</b>` +
    [...LAYERS].reverse().map(([cls, key, name]) => `<div class="row"><span><i class="sw ${cls}"></i>${name}</span><span class="num">${mb(p[key])}</span></div>`).join(""));
}

// Crosshair + tooltip on the nearest point; shared by the live chart and the 7-day chart.
function hover(svg, tip, xh, pts, x, W, mg, iw, t0, span, html, maxGap = 30) {
  if (!svg || !pts.length) return;
  svg.addEventListener("pointermove", (ev) => {
    const r = svg.getBoundingClientRect(), px = (ev.clientX - r.left) * (W / r.width);
    const t = t0 + (px - mg.l) / iw * span;
    let best = pts[0];
    for (const p of pts) if (Math.abs(p.ts - t) < Math.abs(best.ts - t)) best = p;
    if (Math.abs(best.ts - t) > maxGap) { xh.setAttribute("visibility", "hidden"); tip.hidden = true; return; }
    xh.setAttribute("x1", x(best.ts)); xh.setAttribute("x2", x(best.ts)); xh.setAttribute("visibility", "visible");
    tip.innerHTML = html(best); tip.hidden = false;
    const wrap = tip.parentElement.getBoundingClientRect(), left = ev.clientX - wrap.left;
    tip.style.left = `${Math.min(Math.max(left + 14, 8), wrap.width - tip.offsetWidth - 8)}px`;
    tip.style.top = `${Math.min(ev.clientY - wrap.top + 14, wrap.height - 8)}px`;
  });
  svg.addEventListener("pointerleave", () => { xh.setAttribute("visibility", "hidden"); tip.hidden = true; });
}

function renderFacts() {
  const sum = S.summary, I = S.infra;
  const facts = [
    ["proc", mb(sum.dev_mb), `${plural(S.groups.length, "grupo", "grupos")} de desarrollo`, false],
    ["sess", mb(sum.sessions_mb), `${plural(S.sessions.length, "agente", "agentes")}, ${sum.agents_active} ${sum.agents_active === 1 ? "activo" : "activos"}`, false],
    ["infra", I.vm_running || I.docker_running ? mb(sum.infra_mb) : "Apagado", "Docker y WSL", false],
    ["orphans", String(sum.orphans), sum.orphans ? `huérfanos · ${mb(sum.orphans_mb)}` : "huérfanos", sum.orphans > 0],
    ["preview", S.settings.auto_enabled ? nextAutoText() : "Desactivado",
      S.settings.auto_enabled ? `próximo ciclo: ${sum.due} por cerrar${sum.due ? " · " + mb(sum.due_mb) : ""}` : "cierre automático", false],
    ["log", mb(sum.freed_7d_mb), "liberados en 7 días", false],
  ];
  paint("facts", facts.map(([k, v, l, al]) =>
    `<button class="fact ${al ? "alert" : ""} ${k === "preview" && preview ? "on" : ""}" data-fact="${k}" type="button" ${k === "preview" ? 'title="Ver, sin cerrar nada, lo que cerraría el próximo ciclo"' : ""}><b>${esc(v)}</b><span>${esc(l)}</span></button>`).join(""));
}

function renderRail() {
  for (const b of document.querySelectorAll("[data-view]")) b.setAttribute("aria-selected", b.dataset.view === view);
  for (const v of VIEWS) $("view-" + v).hidden = v !== view;
  $("n-proc").textContent = S.groups.length;
  $("n-proc").classList.toggle("alert", S.summary.orphans > 0);
  $("n-sess").textContent = S.sessions.length || "";
  $("n-ports").textContent = S.ports.filter((r) => r.holder && r.dev).length || "";
  const cand = S.worktrees.rows.filter((r) => r.candidate).length;
  $("n-wt").textContent = cand || "";
  $("n-wt").classList.toggle("alert", cand > 0);
  $("n-infra").textContent = S.infra.containers.length || "";
  $("n-log").textContent = S.log.length || "";
}

function renderToolbar() {
  const procView = view === "proc";
  const listView = ["proc", "sess", "ports", "wt", "infra", "log"].includes(view);
  $("toolbar").hidden = !listView;
  $("state-filter").hidden = !procView;
  $("group-wrap").hidden = !procView;
  $("show-all").parentElement.hidden = !["proc", "ports", "wt"].includes(view);
  $("bulk").hidden = !procView && view !== "sess";
  if (view === "sess") {
    const idle = S.sessions.filter((s) => s.closable && s.state === "inactive" && s.idle_hours != null && s.idle_hours >= 24);
    const freed = idle.reduce((t, s) => t + s.total_mb, 0);
    $("bulk").innerHTML = confirmBtn({ act: "kill-idle-agents", arg: { hours: 24 }, key: "idle-agents", small: false,
      label: `Cerrar agentes sin uso en más de 24 h (${idle.length})`, ask: `¿Cerrar ${idle.length} y liberar hasta ${mb(freed)}?`,
      disabled: !idle.length, title: "Solo agentes de terminal. Sus procesos con puerto protegido siguen abiertos. Las sesiones de Claude Code se retoman con --resume." });
  }
  if (procView) {
    const base = baseGroups(), count = (st) => st === "all" ? base.length : base.filter((g) => g.state === st).length;
    paint("state-filter", STATE_FILTERS.map(([k, name]) =>
      `<button type="button" data-state="${k}" aria-pressed="${stateFilter === k}">${name}<span class="c">${count(k)}</span></button>`).join(""));
    const sum = S.summary, st = S.settings;
    $("bulk").innerHTML =
      confirmBtn({ act: "kill-orphans", key: "orphans", label: `Cerrar huérfanos (${sum.orphans})`, small: false,
        ask: `¿Cerrar ${sum.orphans} · ${mb(sum.orphans_mb)}?`, disabled: !sum.orphans }) +
      confirmBtn({ act: "kill-old", key: "old", label: `Cerrar lo que cerraría el ciclo (${sum.due})`, small: false,
        ask: `¿Cerrar ${sum.due} · ${mb(sum.due_mb)}?`, disabled: !sum.due,
        title: `Más de ${dur(st.auto_max_age_hours)} abierto, sin agente activo, sin uso reciente y sin puerto protegido` });
  }
  // selection bar
  const chosen = [...sel].map(groupById).filter(Boolean);
  for (const id of [...sel]) if (!groupById(id)) sel.delete(id);
  $("selbar").hidden = !procView || !chosen.length;
  if (procView && chosen.length) {
    const total = chosen.reduce((t, g) => t + g.mb, 0);
    $("selbar").innerHTML = `<b>${plural(chosen.length, "seleccionado", "seleccionados")} · ${mb(total)}</b>` +
      confirmBtn({ act: "kill-selected", arg: { ids: chosen.map((g) => g.id) }, key: "sel", small: false,
        label: "Cerrar seleccionados", ask: `¿Cerrar ${chosen.length} y liberar ${mb(total)}?` }) +
      `<button class="btn" type="button" data-keep-sel="24">No cerrar automáticamente en 24 h</button>
       <button class="btn ghost" type="button" data-clear-sel>Quitar selección</button>`;
  }
  $("preview-banner").hidden = !preview || !procView;
  if (preview) {
    const due = dueGroups(), when = nextAutoText();
    $("preview-text").textContent = due.length
      ? `Vista previa, no se cierra nada: el próximo ciclo (${when}) cerraría ${due.length === 1 ? "este grupo" : `estos ${due.length} grupos`} (${mb(due.reduce((t, g) => t + g.mb, 0))}). Protege un puerto o pospón un grupo para salvarlo.`
      : `Vista previa: el próximo ciclo (${when}) no cerraría nada.`;
  }
}

function spark(id) {
  const arr = live.groups.get(id) || [];
  if (arr.length < 2) return '<svg class="spark" viewBox="0 0 72 20" aria-hidden="true"><line class="base" x1="0" x2="72" y1="19" y2="19"/></svg>';
  const t1 = arr[arr.length - 1][0], t0 = t1 - LIVE_S, max = Math.max(...arr.map((a) => a[1])) * 1.15 || 1;
  const d = arr.map((a, i) => `${i ? "L" : "M"}${((a[0] - t0) / LIVE_S * 72).toFixed(1)},${(19 - a[1] / max * 17).toFixed(1)}`).join("");
  return `<svg class="spark" viewBox="0 0 72 20" aria-hidden="true"><line class="base" x1="0" x2="72" y1="19" y2="19"/><path d="${d}"/></svg>`;
}

function keepCell(g) {
  const st = S.settings, now = Date.now() / 1000;
  switch (g.keep_reason) {
    case "protegido": return '<span class="badge info">Protegido</span>';
    case "pospuesto": return `<span class="badge info" title="Pospuesto por ti">Pospuesto ${dur((g.keep_until - now) / 3600)}</span>`;
    case "agente activo": return '<span class="badge ok">Agente activo</span>';
    case "en uso": return '<span class="badge ok">En uso</span>';
    case "reciente": return `<span class="sub" title="Lo cierra el ciclo cuando cumpla ${dur(st.auto_max_age_hours)} abierto, si sigue sin uso">En ${dur(st.auto_max_age_hours - g.age_hours)}</span>`;
    default: return st.auto_enabled ? `<span class="due" title="Se cierra en el próximo ciclo">Próximo ciclo</span>` : '<span class="sub">Desactivado</span>';
  }
}

function procRow(g, i) {
  const exposed = g.exposed.length ? `<div class="warn">${WARN} Abierto a la red: ${g.exposed.join(", ")}</div>` : "";
  const who = g.session ? esc(agentName(g.session)) : g.state === "none" ? "sin agente" : "";
  const isSel = focus && focus.type === "group" && focus.id === g.id;
  return `<tr data-row="g" data-id="${esc(g.id)}" data-i="${i}" id="row-${esc(g.id)}" class="${isSel ? "sel" : ""}" tabindex="-1">
    <td class="cb"><input type="checkbox" data-sel="${esc(g.id)}" data-i="${i}" ${sel.has(g.id) ? "checked" : ""} aria-label="Seleccionar ${esc(g.label)}"></td>
    <td>${STATE_DOT(g.state)}</td>
    <td><div class="label">${esc(g.label)}</div><div class="sub" title="${esc(g.cmd)}">${esc(g.kind)}${who ? " · " + who : ""}</div>${exposed}</td>
    <td class="r"><span class="ramcell">${spark(g.id)}<span class="num ${g.mb >= S.settings.show_min_mb ? "heavy" : ""}">${mb(g.mb)}</span></span></td>
    <td class="r cpucell"><b class="num">${pct(g.cpu)}</b><div class="sub">${idleText(g)}</div></td>
    <td>${portChips(g.ports)}</td>
    <td class="r num hide-md hide-insp">${dur(g.age_hours)}</td>
    <td class="hide-md">${keepCell(g)}</td>
    <td class="r">${confirmBtn({ act: "kill", arg: { id: g.id }, key: "g:" + g.id, label: "Cerrar", ask: `¿Cerrar · ${mb(g.mb)}?` })}</td>
  </tr>`;
}

let procOrder = [];
function renderProc() {
  const list = procList();
  const total = S.system.total_mb;
  paint("strip", list.filter((g) => g.mb / total > 0.003).map((g) =>
    `<div class="seg-b s-${g.state} ${focus && focus.id === g.id ? "on" : ""}" data-strip="${esc(g.id)}" style="flex:0 0 ${(g.mb / total * 100).toFixed(2)}%"
      title="${esc(g.label)} · ${mb(g.mb)} · ${STATE_NAMES[g.state]}">${g.mb / total > 0.05 ? esc(g.label) : ""}</div>`).join("") + '<div class="free"></div>');
  let html = "";
  procOrder = [];
  if (byAgent() && !preview) {
    const buckets = new Map();
    for (const g of list) {
      const key = g.state === "orphan" ? "orphan" : g.session ? "a" + g.session.pid : "none";
      if (!buckets.has(key)) buckets.set(key, []);
      buckets.get(key).push(g);
    }
    const sorted = [...buckets.entries()].sort((a, b) => b[1].reduce((t, g) => t + g.mb, 0) - a[1].reduce((t, g) => t + g.mb, 0));
    for (const [key, gs] of sorted) {
      const sum = gs.reduce((t, g) => t + g.mb, 0), first = gs[0];
      const title = key === "orphan" ? "Huérfanos: su agente ya se cerró" : key === "none" ? "Sin agente: lanzados a mano o antes de DevReaper" : agentName(first.session);
      const ag = key.startsWith("a") ? agentByPid(first.session.pid) : null;
      const action = key === "orphan" ? confirmBtn({ act: "kill-orphans", key: "orphans-h", label: "Cerrar huérfanos", ask: `¿Cerrar ${gs.length}?` })
        : ag ? confirmBtn({ act: "kill-agent-procs", arg: { pid: ag.pid }, key: "ap:" + ag.pid, label: "Cerrar sus procesos", ask: `¿Cerrar ${gs.length} · ${mb(sum)}?` }) : "";
      html += `<tr class="grouphead"><td colspan="9"><div class="actions" style="float:right">${action}</div><b>${esc(title)}</b>${ag ? STATE_NAMES[ag.state] + " · " : ""}${plural(gs.length, "proceso", "procesos")} · ${mb(sum)}</td></tr>`;
      for (const g of gs) { html += procRow(g, procOrder.length); procOrder.push(g.id); }
    }
  } else {
    for (const g of list) { html += procRow(g, procOrder.length); procOrder.push(g.id); }
  }
  const st = S.settings;
  paint("rows", html || `<tr><td colspan="9" class="empty">${preview ? "El próximo ciclo no cerraría nada."
    : stateFilter !== "all" ? `Nada en “${STATE_FILTERS.find((f) => f[0] === stateFilter)[1]}”.`
    : `Nada pesado ni antiguo corriendo. Activa “Ver todos” para listar cada proceso de desarrollo (más de ${st.show_min_mb} MB o ${dur(st.show_min_age_hours)} se muestran solos).`}</td></tr>`);
  const all = list.length && list.every((g) => sel.has(g.id));
  $("sel-all").checked = !!all;
  $("sel-all").indeterminate = !all && list.some((g) => sel.has(g.id));
}

let sessOrder = [];
function renderSess() {
  const list = S.sessions.filter((s) => matches(`${s.agent} ${s.name} ${s.cwd} ${s.groups.map((g) => g.label).join(" ")}`));
  sessOrder = list.map((s) => s.pid);
  paint("sessions", list.map((s, i) => {
    const n = s.groups.length, k = n - s.protected;
    const share = s.total_mb ? s.mb / s.total_mb * 100 : 100;
    const chips = s.groups.map((g) => `<button class="pchip" type="button" data-goto="${esc(g.id)}" title="${esc(g.kind)}">${esc(g.label)}${g.ports.length ? " :" + g.ports.join(" :") : ""}<span>${mb(g.mb)}</span></button>`).join("");
    const isSel = focus && focus.type === "agent" && focus.pid === s.pid;
    return `<tr data-row="a" data-pid="${s.pid}" data-i="${i}" class="${isSel ? "sel" : ""}" tabindex="-1">
      <td>${STATE_DOT(s.state)}</td>
      <td><div class="label">${esc(s.agent)} · ${esc(s.name)}</div><div class="sub" title="${esc(s.cwd)}">${esc(KIND_NAMES[s.kind] || "")} · ${esc(shortPath(s.cwd))}</div></td>
      <td><div>${agentIdle(s)}</div><div class="sub">Abierto hace ${dur(s.age_hours)} · CPU ${pct(s.cpu)}</div></td>
      <td class="r"><b class="num">${mb(s.total_mb)}</b><div class="sub">${esc(s.agent)} ${mb(s.mb)}${n ? ` + procesos ${mb(s.groups_mb)}` : ""}</div>
        <div class="stack" title="Agente / procesos que lanzó"><i class="s1" style="width:${share}%"></i>${n ? `<i class="s2" style="width:${100 - share}%"></i>` : ""}</div></td>
      <td>${n ? `<div class="chiplist">${chips}</div>` : '<span class="sub">Ninguno</span>'}
        ${s.protected ? `<div class="kept">${plural(s.protected, "protegido sigue abierto", "protegidos siguen abiertos")} si cierras</div>` : ""}</td>
      <td class="r"><div class="actions">
        ${k ? confirmBtn({ act: "kill-agent-procs", arg: { pid: s.pid }, key: "ap:" + s.pid, label: `Cerrar sus procesos (${k})`, ask: `¿Cerrar ${k} · ${mb(s.groups_mb)}?` }) : ""}
        ${s.closable ? confirmBtn({ act: "kill-session", arg: { pid: s.pid }, key: "s:" + s.pid, label: "Cerrar agente",
          ask: k ? `¿Cerrar agente y ${plural(k, "proceso", "procesos")}?` : "¿Cerrar agente?" }) : ""}
      </div></td>
    </tr>`;
  }).join("") || `<tr><td colspan="6" class="empty">No hay agentes abiertos. DevReaper reconoce Claude Code, Codex, Gemini CLI, Cursor, VS Code, Windsurf, JetBrains y otros.</td></tr>`);
}

function holderCell(h) {
  if (!h) return '<span class="sub">Libre</span>';
  if (h.type === "group") return `<div class="label">${esc(h.label)}</div><div class="sub">${STATE_NAMES[h.state]} · ${esc(h.kind)} · abierto hace ${dur(h.age_hours)}</div>`;
  if (h.type === "docker") return `<div class="label">${esc(h.label)}</div><div class="sub">Contenedor Docker · ${esc(h.image)}</div>`;
  const why = h.infra ? "Docker/WSL lo publica" : h.killable ? "Proceso tuyo" : "Sistema u otra cuenta";
  return `<div class="label">${esc(h.label)}</div><div class="sub">PID ${h.pid} · ${why}</div>`;
}

function renderPorts() {
  const rows = S.ports.filter((r) => (showAll() || r.dev) &&
    matches(`${r.port} ${r.holder?.label || ""} ${r.history.map((h) => h.label).join(" ")}`));
  const busy = S.ports.filter((r) => r.holder && r.dev), exposed = busy.filter((r) => r.exposed);
  paint("ports-head", `${plural(busy.length, "puerto en uso", "puertos en uso")} por desarrollo y contenedores` +
    (exposed.length ? ` · <span class="warn">${WARN} ${plural(exposed.length, "abierto", "abiertos")} a la red: ${exposed.map((r) => r.port).join(", ")}</span>` : "") +
    ` · ${plural(S.excluded_ports.length, "protegido", "protegidos")}. “Ver todos” muestra también los puertos de otros programas.`);
  const now = Date.now() / 1000;
  paint("ports", rows.map((r) => {
    const h = r.holder;
    // Only dev servers and containers get the warning: Windows services listening on the LAN are expected.
    const access = !h ? '<span class="sub">—</span>' : !r.exposed ? '<span class="sub">Solo este equipo</span>'
      : r.dev ? `<span class="warn" title="Escucha en todas las interfaces: otros equipos de tu red pueden conectarse">${WARN} Abierto a la red</span>`
      : '<span class="sub">Escucha en la red</span>';
    const hist = r.history.slice(0, 3).map((x) => `<div>${esc(x.label)} <span class="sub" style="display:inline">${now - x.last_seen < 120 ? "ahora" : "hace " + ago(x.last_seen)}</span></div>`).join("");
    const users = new Set(r.history.map((x) => x.worktree || x.label)).size;
    let action = "";
    if (h && r.protected) action = '<span class="kept">Protegido</span>';
    else if (h && !h.killable) action = `<span class="sub">${h.infra ? "Usa Docker y WSL" : "No es tuyo"}</span>`;
    else if (h) action = confirmBtn({ act: "free-port", arg: { port: r.port }, key: "p:" + r.port,
      label: h.type === "docker" ? "Detener" : "Liberar", ask: `¿Liberar ${r.port}?` });
    const goto = h && h.type === "group" ? ` data-goto="${esc(h.id)}" style="cursor:pointer"` : "";
    return `<tr>
      <td>${portChip(r.port, r.dev || (h && h.killable))}</td>
      <td${goto}>${holderCell(h)}</td>
      <td>${access}</td>
      <td class="r num">${h ? mb(h.mb) : "—"}</td>
      <td class="hide-md"><div class="glist">${hist || '<span class="sub">—</span>'}</div>
        ${r.conflict ? `<div class="warn">${WARN} Lo usaron ${users} proyectos distintos</div>` : ""}</td>
      <td class="r">${action}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="6" class="empty">Ningún puerto de desarrollo en uso.</td></tr>`);
}

function renderWt() {
  const W = S.worktrees;
  if (W.error) { paint("wt-head", esc(W.error)); paint("worktrees", ""); return; }
  const busy = (r) => r.groups.length || r.containers.length || r.sessions.length;
  const running = W.rows.filter(busy), cand = W.rows.filter((r) => r.candidate);
  const repos = new Set(W.rows.map((r) => r.repo)).size;
  paint("wt-head", W.updated
    ? `${plural(repos, "proyecto", "proyectos")} y ${plural(W.rows.length, "checkout", "checkouts")} encontrados donde trabajan tus agentes · ${running.length} con algo corriendo · ${plural(cand.length, "candidato", "candidatos")} a cerrar (su rama ya está en main y sigue consumiendo). Actualizado hace ${ago(W.updated)}.`
    : "Buscando proyectos…");
  const shown = W.rows.filter((r) => (showAll() || busy(r) || r.candidate) &&
    matches(`${r.repo_name} ${r.name} ${r.branch || ""} ${r.ports.join(" ")}`));
  // one block per repo (rows arrive sorted by candidate and RAM): repos with candidates, then the heaviest
  const byRepo = new Map();
  for (const r of shown) { if (!byRepo.has(r.repo)) byRepo.set(r.repo, []); byRepo.get(r.repo).push(r); }
  const weight = (rs) => [rs.some((r) => r.candidate) ? 1 : 0, rs.reduce((t, r) => t + r.mb, 0)];
  const list = [...byRepo.values()].sort((a, b) => { const [ca, ma] = weight(a), [cb, mb2] = weight(b); return cb - ca || mb2 - ma; }).flat();
  let lastRepo = null, html = "";
  for (const r of list) {
    if (r.repo !== lastRepo) {
      const all = W.rows.filter((x) => x.repo === r.repo);
      html += `<tr class="grouphead"><td colspan="7"><b>${esc(r.repo_name)}</b>${esc(shortPath(r.repo))} · ${plural(all.length, "checkout", "checkouts")}</td></tr>`;
      lastRepo = r.repo;
    }
    const [stLabel, stCls] = WT_STATUS[r.status] || [r.status, ""];
    const things = r.groups.map((g) => `<div>${esc(g.label)} ${g.ports.map((p) => `<span class="p">:${p}</span>`).join(" ")} <span class="sub" style="display:inline">${mb(g.mb)}</span></div>`)
      .concat(r.containers.map((c) => `<div>Contenedor ${esc(c.name)} ${c.ports.map((p) => `<span class="p">:${p}</span>`).join(" ")} <span class="sub" style="display:inline">${mb(c.mb)}</span></div>`)).join("");
    const n = r.groups.length + r.containers.length, k = n - r.protected;
    html += `<tr class="${r.candidate ? "cand" : ""}">
      <td><div class="label">${esc(r.name)}</div><div class="sub" title="${esc(r.path)}">${esc(r.branch || "—")}</div></td>
      <td><span class="badge ${stCls}">${stLabel}</span>${r.candidate ? '<div class="due">Candidato a cerrar</div>' : ""}</td>
      <td class="r num">${r.last_commit ? "hace " + ago(r.last_commit) : "—"}</td>
      <td>${things ? `<div class="glist">${things}</div>` : '<span class="sub">Nada</span>'}
        ${r.protected ? `<div class="kept">${plural(r.protected, "protegido", "protegidos")}</div>` : ""}</td>
      <td class="hide-md">${r.sessions.map((s) => `<div>${STATE_DOT(s.state)} ${esc(s.agent || "")} · ${esc(s.name)}</div>`).join("") || '<span class="sub">—</span>'}</td>
      <td class="r num">${r.mb ? mb(r.mb) : "—"}</td>
      <td class="r"><div class="actions"><button class="btn small ghost" type="button" data-reveal="${esc(r.path)}">Abrir carpeta</button>
        ${n ? confirmBtn({ act: "kill-worktree", arg: { path: r.path }, key: "w:" + r.path, label: "Cerrar todo",
        ask: k ? `¿Cerrar ${k} y liberar ${mb(r.mb)}?` : "Todo está protegido", disabled: !k,
        title: "Cierra los procesos y contenedores de este checkout. No cierra los agentes." }) : ""}</div></td>
    </tr>`;
  }
  paint("worktrees", html || `<tr><td colspan="7" class="empty">${W.updated ? `No hay nada corriendo en tus proyectos. “Ver todos” lista los ${W.rows.length} checkouts.` : "Buscando proyectos…"}</td></tr>`);
}

function renderInfra() {
  const I = S.infra;
  const prot = I.containers.filter((c) => c.protected).map((c) => c.name);
  paint("wsl-card", I.vm_running
    ? `<div class="grow"><h3>WSL · ${mb(I.vm_mb)}</h3>
        <p>Máquina virtual de Linux${I.distros.length ? ` con ${esc(I.distros.join(", "))}` : ""}. Apagarla libera esa RAM,
        pero detiene también Docker Desktop y todos sus contenedores.${prot.length ? ` No se puede ahora: ${esc(prot.join(", "))} usa un puerto protegido.` : ""}</p></div>
       ${confirmBtn({ act: "wsl-shutdown", key: "wsl", label: "Apagar WSL", ask: "¿Apagar WSL y Docker?", disabled: prot.length > 0, small: false,
          title: prot.length ? "Hay contenedores con puertos protegidos" : "" })}`
    : `<div class="grow"><h3>WSL</h3><p>No está corriendo.</p></div>`);
  paint("docker-card", I.docker_running
    ? `<div class="grow"><h3>Docker Desktop · ${mb(I.docker_host_mb)}</h3>
        <p>RAM de los procesos de Docker Desktop en Windows. Los contenedores corren dentro de WSL y su RAM está incluida en la de WSL.</p></div>`
    : `<div class="grow"><h3>Docker Desktop</h3><p>${I.docker_cli ? "No está corriendo." : "No está instalado o no está en el PATH."}</p></div>`);
  paint("containers", I.containers.filter((c) => matches(`${c.name} ${c.image} ${c.compose_project}`)).map((c) => `<tr>
      <td><div class="label">${esc(c.name)}</div><div class="sub">${esc(c.image)}</div></td>
      <td class="hide-md"><div class="sub" title="${esc(c.workdir)}">${esc(c.worktree ? shortPath(c.worktree) : c.compose_project || "—")}</div></td>
      <td>${portChips(c.ports.map((p) => p.host_port))}</td>
      <td class="r num">${mb(c.mb)}</td>
      <td><span class="sub">${esc(c.status)}</span></td>
      <td class="r">${c.protected ? '<span class="kept">Protegido</span>'
        : confirmBtn({ act: "docker-stop", arg: { id: c.id }, key: "c:" + c.id, label: "Detener", ask: "¿Detener?" })}</td>
    </tr>`).join("") || `<tr><td colspan="6" class="empty">${I.docker_running ? "No hay contenedores corriendo." : "Docker no está corriendo."}</td></tr>`);
}

function renderLog() {
  paint("log-head", `${plural(S.log.length, "cierre reciente", "cierres recientes")} · ${mb(S.summary.freed_7d_mb)} liberados en 7 días.`);
  paint("log", S.log.filter((l) => matches(`${l.label} ${l.reason} ${l.cmd} ${l.ports.join(" ")}`)).map((l) => `<tr>
    <td class="num">${new Date(l.ts * 1000).toLocaleString("es-PE", { dateStyle: "short", timeStyle: "short" })}</td>
    <td title="${esc(l.cmd)}"><div class="label">${esc(l.label)}</div><div class="sub">${esc(l.cmd)}</div></td><td class="num">${l.ports.join(", ") || "—"}</td>
    <td class="r num">${mb(l.mb)}</td><td>${esc(l.reason)}</td><td>${esc(ORIGIN[l.origin] || l.origin || "—")}</td></tr>`).join("") ||
    `<tr><td colspan="6" class="empty">Todavía no se ha cerrado nada.</td></tr>`);
}

function renderSettings() {
  const st = S.settings;
  paint("excluded", S.excluded_ports.map((p) =>
    `<span class="chip">${p}<button data-unlock="${p}" type="button" aria-label="Quitar protección al puerto ${p}">×</button></span>`).join(""));
  for (const el of document.querySelectorAll("[data-setting]")) {
    if (el === document.activeElement) continue;
    const v = st[el.dataset.setting];
    if (el.type === "checkbox") el.checked = !!v;
    else el.value = Array.isArray(v) ? v.join(el.dataset.setting === "project_roots" ? "; " : ", ") : v;
  }
  const repos = new Set(S.worktrees.rows.map((r) => r.repo)).size;
  $("projects-help").textContent = `DevReaper encuentra tus proyectos solo: mira las carpetas donde trabajan tus agentes (Claude Code, Codex, Gemini, Cursor, VS Code…) y los proyectos que recuerdan. Ahora conoce ${plural(repos, "proyecto", "proyectos")}. Agrega carpetas aquí solo si falta alguno.`;
  $("startup-state").textContent = S.startup ? "· configurado" : "· no configurado";
  $("startup-state").style.color = S.startup ? "var(--active)" : "var(--inactive)";
  $("startup-help").hidden = S.startup;
}

// ---------- inspector ----------
function whyText(g) {
  const st = S.settings, now = Date.now() / 1000;
  const orphan = g.state === "orphan" ? " Su agente ya se cerró: es un huérfano." : "";
  switch (g.keep_reason) {
    case "protegido": return ["", "El cierre automático no lo toca: usa un puerto protegido." + orphan];
    case "pospuesto": return ["", `Lo pospusiste: el ciclo no lo cierra durante ${dur((g.keep_until - now) / 3600)} más.` + orphan];
    case "agente activo": return ["", `Su agente (${agentName(g.session)}) sigue activo, así que el ciclo no lo cierra.`];
    case "en uso": return ["", `Usó CPU hace ${dur(g.idle_hours)}: está en uso y el ciclo no lo cierra.` + orphan];
    case "reciente": return ["", `Si sigue sin uso, el ciclo lo cerrará al cumplir ${dur(st.auto_max_age_hours)} abierto (en ${dur(st.auto_max_age_hours - g.age_hours)}).` + orphan];
    default: return st.auto_enabled ? ["due", `Se cerrará en el próximo ciclo (${nextAutoText()}).` + orphan] : ["", "El cierre automático está desactivado." + orphan];
  }
}

function miniChart(arr, idx, cls, fmt, label) {
  const W = 346, H = 64, mg = { l: 2, r: 2, t: 4, b: 4 };
  const cur = arr.length ? arr[arr.length - 1][idx] : null;
  if (arr.length < 2) return `<div class="mini"><div class="cap"><span>${label}</span><b>${fmt(cur)}</b></div><svg viewBox="0 0 ${W} ${H}" height="${H}"><text class="axis" x="${W / 2}" y="${H / 2 + 4}" text-anchor="middle">Juntando datos…</text></svg></div>`;
  const t1 = arr[arr.length - 1][0], t0 = t1 - LIVE_S;
  const max = Math.max(...arr.map((a) => a[idx]), cls === "cpu" ? 1 : 1) * 1.15;
  const x = (t) => mg.l + (t - t0) / LIVE_S * (W - mg.l - mg.r), y = (v) => H - mg.b - v / max * (H - mg.t - mg.b);
  const line = arr.map((a, i) => `${i ? "L" : "M"}${x(a[0]).toFixed(1)},${y(a[idx]).toFixed(1)}`).join("");
  const peak = Math.max(...arr.map((a) => a[idx]));
  return `<div class="mini"><div class="cap"><span>${label} · pico ${fmt(peak)}</span><b>${fmt(cur)}</b></div>
    <svg viewBox="0 0 ${W} ${H}" height="${H}" role="img" aria-label="${label}: ahora ${fmt(cur)}, pico ${fmt(peak)} en 15 minutos">
      <line class="gridline" x1="0" x2="${W}" y1="${H - mg.b}" y2="${H - mg.b}"/>
      <path class="fill ${cls}" d="${line}L${x(arr[arr.length - 1][0]).toFixed(1)},${H - mg.b}L${x(arr[0][0]).toFixed(1)},${H - mg.b}Z"/>
      <path class="line ${cls}" d="${line}"/></svg></div>`;
}

function renderInspector() {
  const box = $("inspector");
  document.querySelector(".work").classList.remove("has-insp");
  let head = "", charts = "", body = "";
  if (focus && focus.type === "group") {
    const g = groupById(focus.id);
    if (!g) { focus = null; box.hidden = true; return; }
    const [whyCls, why] = whyText(g);
    const arr = live.groups.get(g.id) || [];
    head = `<div class="ttl"><h2>${esc(g.label)}</h2>${STATE_DOT(g.state)} <span class="sub" style="display:inline">${esc(g.kind)}${g.session ? " · " + esc(agentName(g.session)) : ""}</span></div>
      <button class="close-x" type="button" data-close-insp aria-label="Cerrar el detalle">✕</button>`;
    charts = miniChart(arr, 1, "ram", mb, "RAM, 15 min") + miniChart(arr, 2, "cpu", pct, "CPU, 15 min");
    const folder = g.worktree || g.cwd;
    body = `<div class="why ${whyCls}">${esc(why)}</div>
      <div class="insp-actions">
        ${g.excluded ? '<span class="kept">Protegido: tiene un puerto con candado</span>' : confirmBtn({ act: "kill", arg: { id: g.id }, key: "i:" + g.id, label: `Cerrar y liberar ${mb(g.mb)}`, small: false, ask: "¿Seguro? Clic de nuevo" })}
        ${g.keep_until ? `<button class="btn" type="button" data-keep="${esc(g.id)}" data-hours="0">Volver a permitir el cierre automático</button>`
          : `<button class="btn" type="button" data-keep="${esc(g.id)}" data-hours="24">No cerrar en 24 h</button><button class="btn" type="button" data-keep="${esc(g.id)}" data-hours="168">No cerrar en 7 días</button>`}
      </div>
      <h3>Datos</h3>
      <dl class="facts-dl">
        <dt>Agente</dt><dd>${g.session ? esc(agentName(g.session)) + (g.state === "orphan" ? " (cerrado)" : "") : "Ninguno"}</dd>
        <dt>Abierto hace</dt><dd>${dur(g.age_hours)}</dd>
        <dt>Uso</dt><dd>${idleText(g)} · CPU ahora ${pct(g.cpu)}</dd>
        <dt>Procesos</dt><dd>${plural(g.pids.length, "proceso", "procesos")} · PID raíz ${g.root_pid}</dd>
        ${folder ? `<dt>Carpeta</dt><dd title="${esc(folder)}">${esc(shortPath(folder))} <button class="btn small ghost" type="button" data-reveal="${esc(folder)}">Abrir</button></dd>` : ""}
      </dl>
      <h3>Puertos</h3>${portChips(g.ports)}
      ${g.exposed.length ? `<div class="warn" style="margin-top:6px">${WARN} Abierto a la red: ${g.exposed.join(", ")}</div>` : ""}
      <h3>Comando</h3><div class="cmd">${esc(g.cmd)}</div>
      <div class="insp-actions"><button class="btn small" type="button" data-copy="${esc(g.cmd)}">Copiar comando</button></div>`;
  } else if (focus && focus.type === "agent") {
    const s = agentByPid(focus.pid);
    if (!s) { focus = null; box.hidden = true; return; }
    const n = s.groups.length, k = n - s.protected;
    head = `<div class="ttl"><h2>${esc(s.agent)} · ${esc(s.name)}</h2>${STATE_DOT(s.state)} <span class="sub" style="display:inline">${esc(KIND_NAMES[s.kind] || "")}</span></div>
      <button class="close-x" type="button" data-close-insp aria-label="Cerrar el detalle">✕</button>`;
    body = `<div class="why">${s.state === "active" ? "Activo: el ciclo automático no cierra lo que lanzó." : `Inactivo: ${agentIdle(s).toLowerCase()}. El ciclo puede cerrar lo que lanzó si supera la edad límite.`}</div>
      <div class="insp-actions">
        ${k ? confirmBtn({ act: "kill-agent-procs", arg: { pid: s.pid }, key: "iap:" + s.pid, small: false, label: `Cerrar sus procesos (${k} · ${mb(s.groups_mb)})`, ask: "¿Seguro? Clic de nuevo" }) : ""}
        ${s.closable ? confirmBtn({ act: "kill-session", arg: { pid: s.pid }, key: "is:" + s.pid, small: false, label: "Cerrar agente y procesos", ask: "¿Seguro? Clic de nuevo" })
          : `<span class="sub">${esc(s.agent)} se cierra desde su ventana.</span>`}
      </div>
      <h3>Datos</h3>
      <dl class="facts-dl">
        <dt>Actividad</dt><dd>${agentIdle(s)}</dd>
        <dt>Abierto hace</dt><dd>${dur(s.age_hours)}</dd>
        <dt>RAM</dt><dd>${mb(s.total_mb)} (${esc(s.agent)} ${mb(s.mb)}${n ? ` + procesos ${mb(s.groups_mb)}` : ""})</dd>
        <dt>CPU</dt><dd>${pct(s.cpu)}</dd>
        <dt>PID</dt><dd>${s.pid}</dd>
        ${s.cwd ? `<dt>Carpeta</dt><dd title="${esc(s.cwd)}">${esc(shortPath(s.cwd))} <button class="btn small ghost" type="button" data-reveal="${esc(s.cwd)}">Abrir</button></dd>` : ""}
      </dl>
      <h3>Procesos que lanzó</h3>
      ${n ? `<div class="glist">${s.groups.map((g) => `<div><button class="pchip" type="button" data-goto="${esc(g.id)}">${esc(g.label)}<span>${mb(g.mb)} · CPU ${pct(g.cpu)}</span></button> ${g.ports.map((p) => `<span class="p">:${p}</span>`).join(" ")}</div>`).join("")}</div>` : '<span class="sub">Ninguno</span>'}`;
  } else {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  document.querySelector(".work").classList.add("has-insp");
  paint("insp-head", head);
  $("insp-charts").innerHTML = charts;
  $("insp-charts").hidden = !charts;
  paint("insp-body", body);
}

function setFocus(f, scroll) {
  focus = f;
  render();
  if (scroll && f) {
    const row = f.type === "group" ? $("row-" + f.id) : document.querySelector(`tr[data-pid="${f.pid}"]`);
    if (row) { row.scrollIntoView({ block: "nearest" }); row.classList.remove("flash"); void row.offsetWidth; row.classList.add("flash"); }
  }
}

// ---------- RAM 7 days ----------
const HIST_LAYERS = [["dev", "dev_mb", "Procesos de desarrollo"], ["agents", "claude_mb", "Agentes"],
  ["infra", "infra_mb", "Docker y WSL"], ["rest", "rest", "Resto del sistema"]];

async function loadRam() {
  try {
    const r = await fetch(`/api/ram?hours=${ram.hours}`, { cache: "no-store" });
    ram.series = (await r.json()).series.map((p) => ({ ...p, rest: Math.max(0, p.used_mb - p.dev_mb - p.claude_mb - p.infra_mb) }));
    ram.fetched = Date.now();
    renderRam();
  } catch { /* offline note covers it */ }
}

function renderRam() {
  for (const b of document.querySelectorAll("[data-hours]")) if (!b.dataset.keep) b.setAttribute("aria-pressed", +b.dataset.hours === ram.hours);
  $("ram-legend").innerHTML = HIST_LAYERS.map(([cls, , name]) => `<span><i class="sw ${cls}"></i>${name}</span>`).join("");
  const box = $("ram-chart"), series = ram.series;
  if (!S || !series) { box.innerHTML = '<p class="empty">Cargando…</p>'; return; }
  if (!series.length) {
    box.innerHTML = '<p class="empty">Aún no hay muestras. DevReaper guarda una por minuto mientras está abierto: vuelve en unos minutos.</p>';
    $("ram-table").innerHTML = ""; return;
  }
  const total = S.system.total_mb, alert = S.settings.ram_alert_percent;
  const step = total > 24 * 1024 ? 8 * 1024 : total > 12 * 1024 ? 4 * 1024 : 2 * 1024;
  const yMax = Math.ceil(total / step) * step;
  const W = Math.max(320, box.clientWidth), H = Math.max(260, Math.min(420, Math.round(window.innerHeight * 0.42)));
  const mg = { l: 48, r: 14, t: 14, b: 28 }, iw = W - mg.l - mg.r, ih = H - mg.t - mg.b;
  const t1 = Date.now() / 1000, span = ram.hours * 3600, t0 = t1 - span;
  const x = (t) => mg.l + (t - t0) / span * iw;
  const y = (v) => mg.t + ih - Math.min(v, yMax) / yMax * ih;
  // split where the app was closed so gaps show as gaps, not as interpolated slopes
  const bucket = span / 480, gap = Math.max(bucket * 3, 300);
  const segs = [];
  for (const p of series) {
    const cur = segs[segs.length - 1];
    if (!cur || p.ts - cur[cur.length - 1].ts > gap) segs.push([p]); else cur.push(p);
  }
  let areas = "";
  for (const seg of segs) {
    const pts = seg.length === 1 ? [{ ...seg[0], ts: seg[0].ts - bucket / 2 }, { ...seg[0], ts: seg[0].ts + bucket / 2 }] : seg;
    let base = pts.map(() => 0);
    for (const [cls, key] of HIST_LAYERS) {
      const top = pts.map((p, i) => base[i] + p[key]);
      areas += `<path class="area a-${cls}" d="M${pts.map((p, i) => `${x(p.ts).toFixed(1)},${y(top[i]).toFixed(1)}`).join("L")}L${pts.map((p, i) => `${x(p.ts).toFixed(1)},${y(base[i]).toFixed(1)}`).reverse().join("L")}Z"/>`;
      base = top;
    }
  }
  let grid = "";
  for (let v = 0; v <= yMax; v += step)
    grid += `<line class="gridline" x1="${mg.l}" x2="${W - mg.r}" y1="${y(v)}" y2="${y(v)}"/><text class="axis" x="${mg.l - 8}" y="${Math.round(y(v) + 4)}" text-anchor="end">${Math.round(v / 1024)} GB</text>`;
  const fmtH = new Intl.DateTimeFormat("es-PE", ram.hours <= 24 ? { hour: "2-digit", minute: "2-digit", hourCycle: "h23" } : { weekday: "short", day: "numeric" });
  const tick = ram.hours <= 24 ? 4 * 3600 : 86400;
  let xt = "";
  for (let t = Math.ceil(t0 / tick) * tick; t <= t1; t += tick) {
    const d = new Date(t * 1000);
    if (ram.hours > 24) d.setHours(0, 0, 0, 0);
    const tx = ram.hours > 24 ? d.getTime() / 1000 : t;
    if (tx < t0) continue;
    xt += `<text class="axis" x="${Math.round(x(tx))}" y="${H - 8}" text-anchor="middle">${fmtH.format(d)}</text>`;
  }
  const ly = y(total * alert / 100);
  box.innerHTML = `<svg viewBox="0 0 ${W} ${H}" height="${H}" role="img" aria-label="RAM usada en las últimas ${ram.hours} horas, por tipo de proceso">
    ${grid}${areas}
    <line class="limit" x1="${mg.l}" x2="${W - mg.r}" y1="${ly}" y2="${ly}"/>
    <text class="limit-label" x="${W - mg.r}" y="${ly - 5}" text-anchor="end">Aviso ${alert} %</text>
    ${xt}<line class="xhair" id="ram-x" x1="0" x2="0" y1="${mg.t}" y2="${mg.t + ih}" visibility="hidden"/>
    <rect x="${mg.l}" y="${mg.t}" width="${iw}" height="${ih}" fill="transparent"/></svg>`;
  hover(box.querySelector("svg"), $("ram-tip"), $("ram-x"), series, x, W, mg, iw, t0, span, (best) =>
    `<b>${new Date(best.ts * 1000).toLocaleString("es-PE", { weekday: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23" })} · ${mb(best.used_mb)} usados</b>` +
    [...HIST_LAYERS].reverse().map(([cls, key, name]) => `<div class="row"><span><i class="sw ${cls}"></i>${name}</span><span class="num">${mb(best[key])}</span></div>`).join("") +
    (best.top.length ? `<div class="top">Lo más pesado: ${best.top.map((i) => `${esc(i.label)} (${mb(i.mb)})`).join(", ")}</div>` : ""), gap);
  // table view: hourly (24 h) or 6-hourly (7 days) averages
  const tspan = ram.hours <= 24 ? 3600 : 6 * 3600, rows = new Map();
  for (const p of series) {
    const k = Math.floor(p.ts / tspan) * tspan;
    if (!rows.has(k)) rows.set(k, []);
    rows.get(k).push(p);
  }
  const avg = (arr, key) => arr.reduce((t, p) => t + p[key], 0) / arr.length;
  $("ram-table").innerHTML = [...rows.entries()].reverse().map(([k, arr]) => {
    const heavy = arr.reduce((a, b) => (b.used_mb > a.used_mb ? b : a));
    return `<tr><td class="num">${new Date(k * 1000).toLocaleString("es-PE", { weekday: "short", hour: "2-digit", minute: "2-digit" })}</td>
      <td class="r num">${mb(avg(arr, "dev_mb"))}</td><td class="r num">${mb(avg(arr, "claude_mb"))}</td>
      <td class="r num">${mb(avg(arr, "infra_mb"))}</td><td class="r num">${mb(avg(arr, "rest"))}</td>
      <td class="r num">${mb(avg(arr, "used_mb"))}</td>
      <td>${heavy.top[0] ? `${esc(heavy.top[0].label)} (${mb(heavy.top[0].mb)})` : "—"}</td></tr>`;
  }).join("");
}

// ---------- polling ----------
async function poll() {
  try {
    const r = await fetch("/api/state", { cache: "no-store" });
    S = await r.json(); $("offline").hidden = true;
    pushLive();
    render();
    if (view === "ram" && Date.now() - ram.fetched > 60000) loadRam();
  } catch { $("offline").hidden = false; }
}
let pollTimer;
function schedule() { clearTimeout(pollTimer); pollTimer = setTimeout(() => poll().finally(schedule), document.hidden ? 10000 : 2000); }
document.addEventListener("visibilitychange", () => { if (!document.hidden) { poll(); schedule(); } });

// ---------- events ----------
function setView(v) {
  if (!VIEWS.includes(v)) return;
  view = v;
  if (v !== "proc") preview = false;
  if (focus && !((focus.type === "group" && v === "proc") || (focus.type === "agent" && v === "sess"))) focus = null;
  store.set("view", v);
  painted.clear();
  render();
  if (v === "ram") { renderRam(); loadRam(); }
}

function toggleSel(id, i, shift) {
  if (shift && lastSelIdx != null && view === "proc") {
    const [a, b] = [Math.min(lastSelIdx, i), Math.max(lastSelIdx, i)];
    const on = !sel.has(id);
    for (const gid of procOrder.slice(a, b + 1)) on ? sel.add(gid) : sel.delete(gid);
  } else if (sel.has(id)) sel.delete(id); else sel.add(id);
  lastSelIdx = i;
  render();
}

function fire(act) {
  if (act.disabled) return;
  const key = act.dataset.key;
  if (armed !== key) {
    armed = key; clearTimeout(armTimer);
    armTimer = setTimeout(() => { armed = null; render(); }, 3000);
    return render();
  }
  armed = null; clearTimeout(armTimer);
  pending.add(key); render();
  const name = act.dataset.act;
  run(name, JSON.parse(act.dataset.arg)).then((r) => { if (r && name === "kill-selected") sel.clear(); })
    .finally(() => { pending.delete(key); render(); });
}

async function keepAlive(ids, hours) {
  try {
    for (const id of ids) await post("keep-alive", { id, hours });
    toast(hours ? `${ids.length === 1 ? "Pospuesto" : plural(ids.length, "pospuesto", "pospuestos")}: el ciclo no lo cerrará en ${dur(hours)}` : "El ciclo puede volver a cerrarlo");
  } catch (e) { toast(e.message, true); }
}

document.addEventListener("click", (e) => {
  const t = e.target;
  const act = t.closest("[data-act]");
  if (act) return fire(act);
  const cb = t.closest("[data-sel]");
  if (cb) { e.preventDefault(); return toggleSel(cb.dataset.sel, +cb.dataset.i, e.shiftKey); }
  if (t.closest("#sel-all")) {
    const list = procList(), all = list.every((g) => sel.has(g.id));
    for (const g of list) all ? sel.delete(g.id) : sel.add(g.id);
    return render();
  }
  const lock = t.closest("[data-lock]");
  if (lock) {
    const port = +lock.dataset.lock, on = !S.excluded_ports.includes(port);
    return run("exclude", { port, excluded: on }, on ? `Puerto ${port} protegido` : `Puerto ${port} sin protección`);
  }
  const unlock = t.closest("[data-unlock]");
  if (unlock) return run("exclude", { port: +unlock.dataset.unlock, excluded: false }, "Protección quitada");
  const tab = t.closest("[data-view]");
  if (tab) return setView(tab.dataset.view);
  const keep = t.closest("[data-keep]");
  if (keep) return keepAlive([keep.dataset.keep], +keep.dataset.hours);
  if (t.closest("[data-keep-sel]")) return keepAlive([...sel], 24);
  if (t.closest("[data-clear-sel]")) { sel.clear(); return render(); }
  const hours = t.closest("[data-hours]");
  if (hours) { ram.hours = +hours.dataset.hours; ram.series = null; renderRam(); return loadRam(); }
  const stf = t.closest("[data-state]");
  if (stf) { stateFilter = stf.dataset.state; preview = false; return render(); }
  const fact = t.closest("[data-fact]");
  if (fact) {
    const k = fact.dataset.fact;
    if (k === "orphans") { stateFilter = "orphan"; preview = false; $("show-all").checked = true; return setView("proc"); }
    if (k === "preview") { preview = !preview; return setView("proc"); }
    if (k === "proc") { stateFilter = "all"; preview = false; }
    return setView(k);
  }
  const go = t.closest("[data-goto]");
  if (go) {
    const g = groupById(go.dataset.goto);
    if (!g) return;
    if (!g.visible) $("show-all").checked = true;
    stateFilter = "all"; $("filter").value = "";
    view = "proc"; store.set("view", "proc"); painted.clear();
    return setFocus({ type: "group", id: g.id }, true);
  }
  const rev = t.closest("[data-reveal]");
  if (rev) return run("reveal", { path: rev.dataset.reveal }, "Carpeta abierta en el Explorador");
  const copy = t.closest("[data-copy]");
  if (copy) {
    navigator.clipboard.writeText(copy.dataset.copy).then(() => toast("Comando copiado"), () => toast("No se pudo copiar", true));
    return;
  }
  if (t.closest("[data-close-insp]")) return setFocus(null);
  const strip = t.closest("[data-strip]");
  if (strip) return setFocus({ type: "group", id: strip.dataset.strip }, true);
  if (t.closest("a, button, input, label")) return;
  const row = t.closest("tr[data-row]");
  if (row) {
    if (row.dataset.row === "g") return setFocus(focus && focus.id === row.dataset.id ? null : { type: "group", id: row.dataset.id });
    const pid = +row.dataset.pid;
    return setFocus(focus && focus.pid === pid ? null : { type: "agent", pid });
  }
});

$("preview-exit").onclick = () => { preview = false; render(); };
$("wt-refresh").onclick = () => run("refresh-worktrees", {}, "Proyectos actualizados");
function freePort() {
  const port = parseInt($("port").value, 10);
  if (!port) return toast("Escribe un número de puerto", true);
  run("free-port", { port }, (r) => r.killed ? `Puerto ${port} liberado · ${mb(r.freed_mb)}` : `Nada que cerrar en ${port}`);
  $("port").value = "";
}
$("free-port").onclick = freePort;
$("port").onkeydown = (e) => { if (e.key === "Enter") freePort(); };
function protect() {
  const port = parseInt($("ex-port").value, 10);
  if (!port) return toast("Escribe un número de puerto", true);
  run("exclude", { port, excluded: true }, `Puerto ${port} protegido`); $("ex-port").value = "";
}
$("ex-add").onclick = protect;
$("ex-port").onkeydown = (e) => { if (e.key === "Enter") protect(); };
$("filter").oninput = render;
$("show-all").onchange = () => { store.set("show-all", $("show-all").checked ? "1" : "0"); render(); };
$("group-agent").onchange = () => { store.set("group-agent", $("group-agent").checked ? "1" : "0"); render(); };
$("open-startup").onclick = () => run("open-startup", {}, "Carpetas abiertas: arrastra el archivo con el botón derecho");
for (const el of document.querySelectorAll("[data-setting]")) {
  el.onchange = () => {
    const key = el.dataset.setting;
    const value = el.type === "checkbox" ? el.checked
      : key === "dev_names" ? el.value.split(",").map((s) => s.trim()).filter(Boolean)
      : key === "project_roots" ? el.value.split(";").map((s) => s.trim()).filter(Boolean) : +el.value;
    run("settings", { [key]: value }, "Ajuste guardado");
  };
}

// keyboard: / filter · j/k move · space select · x close · Esc back · 1-8 views
function moveCursor(delta) {
  if (view === "proc" && procOrder.length) {
    const i = focus && focus.type === "group" ? procOrder.indexOf(focus.id) : -1;
    const n = Math.max(0, Math.min(procOrder.length - 1, i + delta));
    setFocus({ type: "group", id: procOrder[n] }, true);
  } else if (view === "sess" && sessOrder.length) {
    const i = focus && focus.type === "agent" ? sessOrder.indexOf(focus.pid) : -1;
    const n = Math.max(0, Math.min(sessOrder.length - 1, i + delta));
    setFocus({ type: "agent", pid: sessOrder[n] }, true);
  }
}
document.addEventListener("keydown", (e) => {
  const typing = ["INPUT", "TEXTAREA"].includes(document.activeElement.tagName) && document.activeElement.type !== "checkbox";
  if (e.key === "Escape") {
    if (typing && document.activeElement === $("filter")) { $("filter").value = ""; $("filter").blur(); return render(); }
    if (focus) return setFocus(null);
    if (preview) { preview = false; return render(); }
    return;
  }
  if (typing || e.ctrlKey || e.metaKey || e.altKey) return;
  if (e.key === "/") { e.preventDefault(); return $("filter").focus(); }
  if (/^[1-8]$/.test(e.key)) return setView(VIEWS[+e.key - 1]);
  if (e.key === "j" || e.key === "ArrowDown") { e.preventDefault(); return moveCursor(1); }
  if (e.key === "k" || e.key === "ArrowUp") { e.preventDefault(); return moveCursor(-1); }
  if (e.key === " " && focus && focus.type === "group") { e.preventDefault(); return toggleSel(focus.id, procOrder.indexOf(focus.id), false); }
  if (e.key === "x" && focus) {
    const btn = focus.type === "group" ? document.querySelector(`#row-${CSS.escape(focus.id)} [data-act]`)
      : document.querySelector(`tr[data-pid="${focus.pid}"] [data-act]`);
    if (btn) fire(btn);
  }
});
let resizeTimer;
window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => { renderLive(); if (view === "ram") renderRam(); }, 120); });

// ---------- start ----------
$("show-all").checked = store.get("show-all") === "1";
$("group-agent").checked = store.get("group-agent") === "1";
if (VIEWS.includes(store.get("view"))) view = store.get("view");
function readHash() {
  const m = /^#(g|a)=(.+)$/.exec(location.hash);
  if (!m) return;
  if (m[1] === "g") { view = "proc"; $("show-all").checked = true; focus = { type: "group", id: decodeURIComponent(m[2]) }; }
  else { view = "sess"; focus = { type: "agent", pid: +m[2] }; }
  history.replaceState(null, "", location.pathname);
}
readHash();
window.addEventListener("hashchange", () => { readHash(); painted.clear(); render(); });
Promise.all([loadLive(), poll()]).then(() => {
  render();
  if (focus) setFocus(focus, true);
  if (view === "ram") loadRam();
  schedule();
});
