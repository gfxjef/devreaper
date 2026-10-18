"use strict";
const TOKEN = document.querySelector('meta[name="devreaper-token"]').content;

// ---------- theme: auto (follows Windows) → light → dark ----------
const THEMES = {
  auto: ["Tema: automático (sigue a Windows)", '<svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M8 2a6 6 0 0 1 0 12z" fill="currentColor"/></svg>'],
  light: ["Tema: claro", '<svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><circle cx="8" cy="8" r="3"/><path d="M8 1v1.5M8 13.5V15M1 8h1.5M13.5 8H15M3 3l1 1M12 12l1 1M3 13l1-1M12 4l1-1"/></svg>'],
  dark: ["Tema: oscuro", '<svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path d="M13.5 10.2A6 6 0 0 1 5.8 2.5a6 6 0 1 0 7.7 7.7z" fill="currentColor"/></svg>'],
};
let theme = "auto";
try { theme = THEMES[localStorage.getItem("theme")] ? localStorage.getItem("theme") : "auto"; } catch {}
function applyTheme() {
  if (theme === "auto") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", theme);
  const b = document.getElementById("theme");
  b.innerHTML = THEMES[theme][1];
  b.title = THEMES[theme][0] + ". Clic para cambiar.";
  b.setAttribute("aria-label", THEMES[theme][0]);
}
applyTheme();
document.getElementById("theme").onclick = () => {
  theme = { auto: "light", light: "dark", dark: "auto" }[theme];
  try { localStorage.setItem("theme", theme); } catch {}
  applyTheme();
};
const STATE_NAMES = { active: "Activa", inactive: "Inactiva", orphan: "Huérfano", none: "Sin sesión" };
const WT_STATUS = {
  merged: ["Ya está en main", "ok"], gone: ["Rama remota borrada", "ok"], missing: ["Carpeta borrada", "bad"],
  open: ["Con cambios propios", ""], main: ["Checkout principal", ""], detached: ["Sin rama", ""],
};
const ORIGIN = { panel: "Panel", bandeja: "Bandeja", "automático": "Ciclo automático" };
const $ = (id) => document.getElementById(id);
let S = null, armed = null, armTimer = null, view = "proc", preview = false;
const pending = new Set();
const ram = { hours: 168, series: null, fetched: 0 };

// ---------- formatting ----------
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const mb = (v) => v == null ? "—" : v >= 1024 ? (v / 1024).toFixed(1) + " GB" : Math.round(v) + " MB";
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

function toast(msg, err) {
  const t = $("toast"); t.textContent = msg; t.className = err ? "show err" : "show";
  clearTimeout(t._h); t._h = setTimeout(() => (t.className = ""), 3200);
}

// ---------- API ----------
async function post(action, body = {}) {
  const r = await fetch("/api/" + action, {
    method: "POST", headers: { "Content-Type": "application/json", "X-DevReaper-Token": TOKEN },
    body: JSON.stringify(body),
  });
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || r.statusText);
  S = data.state; render();
  return data.result;
}

function resultMsg(r) {
  if (r.ok) return "Listo";
  let m = r.killed ? `${plural(r.killed, "cerrado", "cerrados")} · ${mb(r.freed_mb)} liberados` : "No había nada que cerrar";
  if (r.kept) m += ` · ${plural(r.kept, "protegido sigue abierto", "protegidos siguen abiertos")}`;
  return m.charAt(0).toUpperCase() + m.slice(1);
}

async function run(action, body, okMsg) {
  try { const r = await post(action, body); toast(okMsg ? (typeof okMsg === "function" ? okMsg(r) : okMsg) : resultMsg(r)); }
  catch (e) { toast(e.message, true); }
}

// A button that closes something: first click arms it, a second click within 3 s fires it.
function confirmBtn({ act, arg = {}, key, label, ask = "¿Seguro?", disabled = false, title = "", small = false }) {
  const isArmed = armed === key, busy = pending.has(key);
  return `<button class="btn danger ${small ? "small" : ""} ${isArmed ? "armed" : ""}" data-act="${esc(act)}"
    data-arg="${esc(JSON.stringify(arg))}" data-key="${esc(key)}" ${disabled || busy ? "disabled" : ""}
    title="${esc(title)}">${busy ? "Cerrando…" : esc(isArmed ? ask : label)}</button>`;
}

// ---------- filters ----------
const query = () => $("filter").value.trim().toLowerCase();
const matches = (text) => { const q = query(); return !q || String(text).toLowerCase().includes(q); };
const showAll = () => $("show-all").checked;

function dueGroups() {
  const ex = new Set(S.excluded_ports);
  return S.groups.filter((g) => g.age_hours >= S.settings.auto_max_age_hours && !g.ports.some((p) => ex.has(p)));
}

// ---------- render ----------
function render() {
  if (!S) return;
  renderHeader();
  renderTabs();
  if (view === "proc") renderProc();
  if (view === "sess") renderSess();
  if (view === "ports") renderPorts();
  if (view === "wt") renderWt();
  if (view === "infra") renderInfra();
  renderLog();
  renderSettings();
}

function renderHeader() {
  const st = S.settings, sum = S.summary;
  $("ram").textContent = Math.round(S.system.percent) + " %";
  $("ram-sub").textContent = `RAM · ${mb(S.system.used_mb)} de ${mb(S.system.total_mb)}`;
  $("dev").textContent = mb(sum.dev_mb);
  $("dev-sub").textContent = `${plural(S.groups.length, "grupo", "grupos")} de desarrollo`;
  $("claude").textContent = mb(sum.sessions_mb);
  $("claude-sub").textContent = `${plural(S.sessions.length, "sesión", "sesiones")} de Claude`;
  $("infra").textContent = S.infra.vm_running || S.infra.docker_running ? mb(sum.infra_mb) : "Apagado";
  $("orph").textContent = sum.orphans;
  $("orph-stat").classList.toggle("alert", sum.orphans > 0);
  $("freed").textContent = mb(sum.freed_7d_mb);
  if (st.auto_enabled) {
    const left = (S.next_auto - Date.now() / 1000) / 3600;
    $("next").textContent = left <= 0 ? "en breve" : "en " + dur(left);
    $("next-sub").textContent = "próximo cierre automático";
    $("next-sub").parentElement.title = `Cierra lo que lleve más de ${dur(st.auto_max_age_hours)} · ${sum.due} pendientes`;
  } else {
    $("next").textContent = "Desactivado"; $("next-sub").textContent = "cierre automático";
    $("next-sub").parentElement.title = "";
  }
  $("preview").textContent = preview ? "Viendo vista previa" : "Ver qué cerraría";

  const total = S.system.total_mb;
  $("load").innerHTML = S.groups.filter((g) => g.mb / total > 0.004).map((g) =>
    `<div class="seg s-${g.state}" data-id="${esc(g.id)}" style="flex:0 0 ${(g.mb / total * 100).toFixed(2)}%"
      title="${esc(g.label)} · ${mb(g.mb)} · ${STATE_NAMES[g.state]}">${g.mb / total > 0.05 ? esc(g.label) : ""}</div>`).join("") +
    `<div class="free"></div>`;
  $("load-caption").textContent = `Cada bloque es un grupo de procesos de desarrollo, a escala de los ${mb(total)} de RAM. ` +
    "Verde: sesión activa. Ocre: sesión inactiva. Rojo: huérfano. Gris: sin sesión.";

  $("bulk").innerHTML =
    confirmBtn({ act: "kill-orphans", key: "orphans", label: `Cerrar huérfanos (${sum.orphans})`,
      ask: `¿Cerrar ${sum.orphans}? Clic de nuevo`, disabled: !sum.orphans }) +
    confirmBtn({ act: "kill-old", key: "old", label: `Cerrar los de más de ${dur(st.auto_max_age_hours)} (${sum.due})`,
      ask: `¿Cerrar ${sum.due}? Clic de nuevo`, disabled: !sum.due });
}

function renderTabs() {
  for (const b of document.querySelectorAll("[data-view]")) b.setAttribute("aria-selected", b.dataset.view === view);
  for (const v of ["proc", "sess", "ports", "wt", "infra", "ram"]) $("view-" + v).hidden = v !== view;
  $("n-proc").textContent = S.groups.length;
  $("n-sess").textContent = `${S.sessions.length} · ${mb(S.sessions.reduce((t, s) => t + s.total_mb, 0))}`;
  $("n-ports").textContent = S.ports.filter((r) => r.holder && r.dev).length;
  $("n-wt").textContent = S.worktrees.rows.filter((r) => r.candidate).length ? `${S.worktrees.rows.filter((r) => r.candidate).length} por cerrar` : "";
  $("n-infra").textContent = S.infra.containers.length ? plural(S.infra.containers.length, "contenedor", "contenedores") : "";
  $("preview-banner").hidden = !preview || view !== "proc";
  if (preview) {
    const due = dueGroups(), left = (S.next_auto - Date.now() / 1000) / 3600;
    const when = S.settings.auto_enabled ? (left <= 0 ? "en breve" : "en " + dur(left)) : "cuando lo actives";
    $("preview-text").textContent = due.length
      ? `Vista previa, no se cierra nada: el próximo ciclo (${when}) cerraría ${due.length === 1 ? "este grupo" : `estos ${due.length} grupos`} (${mb(due.reduce((t, g) => t + g.mb, 0))}). Protege un puerto con el candado para salvarlo.`
      : `Vista previa: el próximo ciclo (${when}) no cerraría nada.`;
  }
}

function renderProc() {
  const st = S.settings;
  const list = preview ? dueGroups() : S.groups.filter((g) => (showAll() || g.visible) &&
    matches(`${g.label} ${g.cmd} ${g.ports.join(" ")} ${g.session?.name || ""}`));
  $("rows").innerHTML = list.map((g) => {
    const due = g.age_hours >= st.auto_max_age_hours;
    const note = g.excluded ? `<div class="kept">Protegido</div>` : (due && st.auto_enabled ? `<div class="due">Se cierra en el próximo ciclo</div>` : "");
    const exposed = g.exposed.length ? `<div class="warn">${WARN} Abierto a la red: ${g.exposed.join(", ")}</div>` : "";
    return `<tr id="row-${esc(g.id)}">
      <td>${STATE_DOT(g.state)}</td>
      <td><div class="label">${esc(g.label)}</div><div class="sub" title="${esc(g.cmd)}">${esc(g.kind)} · PID ${g.root_pid} · ${g.pids.length} proc.</div>${exposed}</td>
      <td>${portChips(g.ports)}</td>
      <td class="r num ${g.mb >= st.show_min_mb ? "heavy" : ""}">${mb(g.mb)}</td>
      <td class="r num">${dur(g.age_hours)}${note}</td>
      <td class="hide-sm">${g.session ? esc(g.session.name) : '<span class="sub">—</span>'}</td>
      <td class="r">${confirmBtn({ act: "kill", arg: { id: g.id }, key: "g:" + g.id, label: "Cerrar" })}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="7" class="empty">${preview ? `Nada supera ${dur(st.auto_max_age_hours)} abierto.`
    : "Nada pesado ni antiguo corriendo. Activa “Ver todos” para listar cada proceso de desarrollo."}</td></tr>`;
}

function renderSess() {
  $("sessions").innerHTML = S.sessions.filter((s) => matches(`${s.name} ${s.cwd} ${s.groups.map((g) => g.label).join(" ")}`)).map((s) => {
    const n = s.groups.length, k = n - s.protected;
    const glist = s.groups.map((g) => `<div>${esc(g.label)} ${g.ports.map((p) => `<span class="p">:${p}</span>`).join(" ")}
      <span class="sub" style="display:inline">${mb(g.mb)}</span></div>`).join("");
    return `<tr>
      <td>${STATE_DOT(s.state)}</td>
      <td><div class="label">${esc(s.name)}</div><div class="sub" title="${esc(s.cwd)}">${esc(shortPath(s.cwd))}</div></td>
      <td>${n ? `<div class="glist">${glist}</div>` : '<span class="sub">Ninguno</span>'}
        ${s.protected ? `<div class="kept">${plural(s.protected, "protegido sigue abierto", "protegidos siguen abiertos")} si cierras la sesión</div>` : ""}</td>
      <td class="r num"><b>${mb(s.total_mb)}</b><div class="sub">Claude ${mb(s.mb)}${n ? ` + procesos ${mb(s.groups_mb)}` : ""}</div></td>
      <td class="r num">${dur(s.age_hours)}</td>
      <td class="r num">${dur(s.idle_hours)}</td>
      <td class="r">${confirmBtn({ act: "kill-session", arg: { pid: s.pid }, key: "s:" + s.pid, label: "Cerrar sesión",
        ask: k ? `¿Cerrar sesión y ${plural(k, "proceso", "procesos")}?` : "¿Cerrar sesión?" })}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="7" class="empty">No hay sesiones de Claude Code abiertas.</td></tr>`;
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
  $("ports-head").innerHTML = `${plural(busy.length, "puerto en uso", "puertos en uso")} por desarrollo y contenedores` +
    (exposed.length ? ` · <span class="warn">${WARN} ${plural(exposed.length, "abierto", "abiertos")} a la red: ${exposed.map((r) => r.port).join(", ")}</span>` : "") +
    ` · ${plural(S.excluded_ports.length, "protegido", "protegidos")}. “Ver todos” muestra también los puertos de otros programas.`;
  const now = Date.now() / 1000;
  $("ports").innerHTML = rows.map((r) => {
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
    return `<tr>
      <td>${portChip(r.port, r.dev || (h && h.killable))}</td>
      <td>${holderCell(h)}</td>
      <td>${access}</td>
      <td class="r num">${h ? mb(h.mb) : "—"}</td>
      <td class="hide-sm"><div class="glist">${hist || '<span class="sub">—</span>'}</div>
        ${r.conflict ? `<div class="warn">${WARN} Lo usaron ${users} proyectos distintos</div>` : ""}</td>
      <td class="r">${action}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="6" class="empty">Ningún puerto de desarrollo en uso.</td></tr>`;
}

function renderWt() {
  const W = S.worktrees;
  if (W.error) { $("wt-head").textContent = W.error; $("worktrees").innerHTML = ""; return; }
  const running = W.rows.filter((r) => r.groups.length || r.containers.length || r.sessions.length);
  const cand = W.rows.filter((r) => r.candidate);
  $("wt-head").textContent = W.updated
    ? `${plural(W.rows.length, "worktree", "worktrees")} · ${running.length} con algo corriendo · ${plural(cand.length, "candidato", "candidatos")} a cerrar (su rama ya está en main y sigue consumiendo). Actualizado hace ${ago(W.updated)}.`
    : "Buscando worktrees…";
  const list = W.rows.filter((r) => (showAll() || r.groups.length || r.containers.length || r.sessions.length) &&
    matches(`${r.repo_name} ${r.name} ${r.branch || ""} ${r.ports.join(" ")}`));
  $("worktrees").innerHTML = list.map((r) => {
    const [stLabel, stCls] = WT_STATUS[r.status] || [r.status, ""];
    const things = r.groups.map((g) => `<div>${esc(g.label)} ${g.ports.map((p) => `<span class="p">:${p}</span>`).join(" ")} <span class="sub" style="display:inline">${mb(g.mb)}</span></div>`)
      .concat(r.containers.map((c) => `<div>Contenedor ${esc(c.name)} ${c.ports.map((p) => `<span class="p">:${p}</span>`).join(" ")} <span class="sub" style="display:inline">${mb(c.mb)}</span></div>`)).join("");
    const n = r.groups.length + r.containers.length, k = n - r.protected;
    return `<tr class="${r.candidate ? "cand" : ""}">
      <td><div class="label">${esc(r.repo_name)} › ${esc(r.name)}</div><div class="sub" title="${esc(r.path)}">${esc(r.branch || "—")}</div></td>
      <td><span class="badge ${stCls}">${stLabel}</span>${r.candidate ? '<div class="due">Candidato a cerrar</div>' : ""}</td>
      <td class="r num">${r.last_commit ? "hace " + ago(r.last_commit) : "—"}</td>
      <td>${things ? `<div class="glist">${things}</div>` : '<span class="sub">Nada</span>'}
        ${r.protected ? `<div class="kept">${plural(r.protected, "protegido", "protegidos")}</div>` : ""}</td>
      <td class="hide-sm">${r.sessions.map((s) => `<div>${STATE_DOT(s.state)} ${esc(s.name)}</div>`).join("") || '<span class="sub">—</span>'}</td>
      <td class="r num">${r.mb ? mb(r.mb) : "—"}</td>
      <td class="r">${n ? confirmBtn({ act: "kill-worktree", arg: { path: r.path }, key: "w:" + r.path, label: "Cerrar todo",
        ask: k ? `¿Cerrar ${k} y liberar ${mb(r.mb)}?` : "Todo está protegido", disabled: !k,
        title: "Cierra los procesos y contenedores de este worktree. No cierra las sesiones de Claude." }) : ""}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="7" class="empty">${W.updated ? "No hay nada corriendo en ningún worktree. “Ver todos” lista los " + W.rows.length + "." : "Buscando worktrees…"}</td></tr>`;
}

function renderInfra() {
  const I = S.infra;
  const prot = I.containers.filter((c) => c.protected).map((c) => c.name);
  $("wsl-card").innerHTML = I.vm_running
    ? `<div class="grow"><h3>WSL · ${mb(I.vm_mb)}</h3>
        <p>Máquina virtual de Linux${I.distros.length ? ` con ${esc(I.distros.join(", "))}` : ""}. Apagarla libera esa RAM,
        pero detiene también Docker Desktop y todos sus contenedores.${prot.length ? ` No se puede ahora: ${esc(prot.join(", "))} usa un puerto protegido.` : ""}</p></div>
       ${confirmBtn({ act: "wsl-shutdown", key: "wsl", label: "Apagar WSL", ask: "¿Apagar WSL y Docker?", disabled: prot.length > 0,
          title: prot.length ? "Hay contenedores con puertos protegidos" : "" })}`
    : `<div class="grow"><h3>WSL</h3><p>No está corriendo.</p></div>`;
  $("docker-card").innerHTML = I.docker_running
    ? `<div class="grow"><h3>Docker Desktop · ${mb(I.docker_host_mb)}</h3>
        <p>RAM de los procesos de Docker Desktop en Windows. Los contenedores corren dentro de WSL y su RAM está incluida en la de WSL.</p></div>`
    : `<div class="grow"><h3>Docker Desktop</h3><p>${I.docker_cli ? "No está corriendo." : "No está instalado o no está en el PATH."}</p></div>`;
  $("containers").innerHTML = I.containers.filter((c) => matches(`${c.name} ${c.image} ${c.compose_project}`)).map((c) => `<tr>
      <td><div class="label">${esc(c.name)}</div><div class="sub">${esc(c.image)}</div></td>
      <td class="hide-sm"><div class="sub" title="${esc(c.workdir)}">${esc(c.worktree ? shortPath(c.worktree) : c.compose_project || "—")}</div></td>
      <td>${portChips(c.ports.map((p) => p.host_port))}</td>
      <td class="r num">${mb(c.mb)}</td>
      <td><span class="sub">${esc(c.status)}</span></td>
      <td class="r">${c.protected ? '<span class="kept">Protegido</span>'
        : confirmBtn({ act: "docker-stop", arg: { id: c.id }, key: "c:" + c.id, label: "Detener", ask: "¿Detener?" })}</td>
    </tr>`).join("") || `<tr><td colspan="6" class="empty">${I.docker_running ? "No hay contenedores corriendo." : "Docker no está corriendo."}</td></tr>`;
}

function renderLog() {
  $("n-log").textContent = `${S.log.length} · ${mb(S.summary.freed_7d_mb)} liberados en 7 días`;
  $("log").innerHTML = S.log.map((l) => `<tr>
    <td class="num">${new Date(l.ts * 1000).toLocaleString("es-PE", { dateStyle: "short", timeStyle: "short" })}</td>
    <td title="${esc(l.cmd)}">${esc(l.label)}</td><td class="num">${l.ports.join(", ") || "—"}</td>
    <td class="r num">${mb(l.mb)}</td><td>${esc(l.reason)}</td><td>${esc(ORIGIN[l.origin] || l.origin || "—")}</td></tr>`).join("") ||
    `<tr><td colspan="6" class="empty">Todavía no se ha cerrado nada.</td></tr>`;
}

function renderSettings() {
  const st = S.settings;
  $("excluded").innerHTML = S.excluded_ports.map((p) =>
    `<span class="chip">${p}<button data-unlock="${p}" aria-label="Quitar protección al puerto ${p}">×</button></span>`).join("");
  for (const el of document.querySelectorAll("[data-setting]")) {
    if (el === document.activeElement) continue;
    const v = st[el.dataset.setting];
    if (el.type === "checkbox") el.checked = !!v;
    else el.value = Array.isArray(v) ? v.join(el.dataset.setting === "project_roots" ? "; " : ", ") : v;
  }
  $("startup-state").textContent = S.startup ? "· configurado" : "· no configurado";
  $("startup-state").style.color = S.startup ? "var(--active)" : "var(--inactive)";
  $("startup-help").hidden = S.startup;
}

// ---------- RAM chart ----------
const LAYERS = [["dev", "dev_mb", "Procesos de desarrollo"], ["claude", "claude_mb", "Sesiones de Claude"],
  ["infra", "infra_mb", "Docker y WSL"], ["rest", "rest", "Resto del sistema"]];

async function loadRam() {
  try {
    const r = await fetch(`/api/ram?hours=${ram.hours}`, { cache: "no-store" });
    ram.series = (await r.json()).series.map((p) => ({ ...p, rest: Math.max(0, p.used_mb - p.dev_mb - p.claude_mb - p.infra_mb) }));
    ram.fetched = Date.now();
    renderRam();
  } catch { /* offline banner covers it */ }
}

function renderRam() {
  for (const b of document.querySelectorAll("[data-hours]")) b.setAttribute("aria-pressed", +b.dataset.hours === ram.hours);
  const box = $("ram-chart"), series = ram.series;
  if (!S || !series) { box.innerHTML = '<p class="empty">Cargando…</p>'; return; }
  if (!series.length) {
    box.innerHTML = '<p class="empty">Aún no hay muestras. DevReaper guarda una por minuto mientras está abierto: vuelve en unos minutos.</p>';
    $("ram-table").innerHTML = ""; return;
  }
  const total = S.system.total_mb, alert = S.settings.ram_alert_percent;
  const step = total > 24 * 1024 ? 8 * 1024 : total > 12 * 1024 ? 4 * 1024 : 2 * 1024;
  const yMax = Math.ceil(total / step) * step;
  const W = Math.max(320, box.clientWidth), H = 280, m = { l: 48, r: 14, t: 14, b: 28 };
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  const t1 = Date.now() / 1000, t0 = t1 - ram.hours * 3600;
  const x = (t) => m.l + (t - t0) / (t1 - t0) * iw;
  const y = (v) => m.t + ih - Math.min(v, yMax) / yMax * ih;

  // split where the app was closed so gaps show as gaps, not as interpolated slopes
  const bucket = ram.hours * 3600 / 480, gap = Math.max(bucket * 3, 300);
  const segs = [];
  for (const p of series) {
    const cur = segs[segs.length - 1];
    if (!cur || p.ts - cur[cur.length - 1].ts > gap) segs.push([p]); else cur.push(p);
  }
  let areas = "";
  for (const seg of segs) {
    const pts = seg.length === 1 ? [{ ...seg[0], ts: seg[0].ts - bucket / 2 }, { ...seg[0], ts: seg[0].ts + bucket / 2 }] : seg;
    let base = pts.map(() => 0);
    for (const [cls, key] of LAYERS) {
      const top = pts.map((p, i) => base[i] + p[key]);
      const d = "M" + pts.map((p, i) => `${x(p.ts).toFixed(1)},${y(top[i]).toFixed(1)}`).join("L") +
        "L" + pts.map((p, i) => `${x(p.ts).toFixed(1)},${y(base[i]).toFixed(1)}`).reverse().join("L") + "Z";
      areas += `<path class="area a-${cls}" d="${d}"/>`;
      base = top;
    }
  }
  let grid = "";
  for (let v = 0; v <= yMax; v += step)
    grid += `<line class="grid" x1="${m.l}" x2="${W - m.r}" y1="${y(v)}" y2="${y(v)}"/><text class="axis" x="${m.l - 8}" y="${Math.round(y(v) + 4)}" text-anchor="end">${Math.round(v / 1024)} GB</text>`;
  const fmtH = new Intl.DateTimeFormat("es-PE", ram.hours <= 24 ? { hour: "2-digit", minute: "2-digit", hourCycle: "h23" } : { weekday: "short", day: "numeric" });
  const tick = ram.hours <= 24 ? 4 * 3600 : 86400;
  const first = Math.ceil(t0 / tick) * tick;
  let xt = "";
  for (let t = first; t <= t1; t += tick) {
    const d = new Date(t * 1000);
    if (ram.hours > 24) d.setHours(0, 0, 0, 0);
    const tx = ram.hours > 24 ? d.getTime() / 1000 : t;
    if (tx < t0) continue;
    xt += `<text class="axis" x="${Math.round(x(tx))}" y="${H - 8}" text-anchor="middle">${fmtH.format(d)}</text>`;
  }
  const ly = y(total * alert / 100);
  box.innerHTML = `<svg viewBox="0 0 ${W} ${H}" height="${H}" role="img"
      aria-label="RAM usada en las últimas ${ram.hours} horas, por tipo de proceso">
    ${grid}${areas}
    <line class="limit" x1="${m.l}" x2="${W - m.r}" y1="${ly}" y2="${ly}"/>
    <text class="limit-label" x="${W - m.r}" y="${ly - 5}" text-anchor="end">Aviso ${alert} %</text>
    ${xt}
    <line class="xhair" id="xhair" x1="0" x2="0" y1="${m.t}" y2="${m.t + ih}" visibility="hidden"/>
    <rect id="hit" x="${m.l}" y="${m.t}" width="${iw}" height="${ih}" fill="transparent"/>
  </svg>`;
  const svg = box.querySelector("svg"), tip = $("ram-tip"), xh = $("xhair");
  svg.addEventListener("pointermove", (ev) => {
    const r = svg.getBoundingClientRect(), px = (ev.clientX - r.left) * (W / r.width);
    const t = t0 + (px - m.l) / iw * (t1 - t0);
    let best = series[0];
    for (const p of series) if (Math.abs(p.ts - t) < Math.abs(best.ts - t)) best = p;
    if (Math.abs(best.ts - t) > gap) { xh.setAttribute("visibility", "hidden"); tip.hidden = true; return; }
    const bx = x(best.ts);
    xh.setAttribute("x1", bx); xh.setAttribute("x2", bx); xh.setAttribute("visibility", "visible");
    tip.innerHTML = `<b>${new Date(best.ts * 1000).toLocaleString("es-PE", { weekday: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23" })} · ${mb(best.used_mb)} usados</b>` +
      [...LAYERS].reverse().map(([cls, key, name]) => `<div class="row"><span><i class="sw ${cls}"></i>${name}</span><span class="num">${mb(best[key])}</span></div>`).join("") +
      (best.top.length ? `<div class="top">Lo más pesado: ${best.top.map((i) => `${esc(i.label)} (${mb(i.mb)})`).join(", ")}</div>` : "");
    tip.hidden = false;
    const wrap = box.parentElement.getBoundingClientRect(), left = ev.clientX - wrap.left;
    tip.style.left = `${Math.min(Math.max(left + 14, 8), wrap.width - tip.offsetWidth - 8)}px`;
    tip.style.top = `${ev.clientY - wrap.top + 14}px`;
  });
  svg.addEventListener("pointerleave", () => { xh.setAttribute("visibility", "hidden"); tip.hidden = true; });

  // table view: hourly (24 h) or 6-hourly (7 days) averages
  const span = ram.hours <= 24 ? 3600 : 6 * 3600, rows = new Map();
  for (const p of series) {
    const k = Math.floor(p.ts / span) * span;
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
    render();
    if (view === "ram" && Date.now() - ram.fetched > 60000) loadRam();
  } catch { $("offline").hidden = false; }
}

// ---------- events ----------
document.addEventListener("click", (e) => {
  const act = e.target.closest("[data-act]");
  if (act) {
    if (act.disabled) return;
    const key = act.dataset.key;
    if (armed !== key) {
      armed = key; clearTimeout(armTimer);
      armTimer = setTimeout(() => { armed = null; render(); }, 3000);
      return render();
    }
    armed = null; clearTimeout(armTimer);
    pending.add(key); render();
    run(act.dataset.act, JSON.parse(act.dataset.arg)).finally(() => { pending.delete(key); render(); });
    return;
  }
  const tab = e.target.closest("[data-view]");
  if (tab) return setView(tab.dataset.view);
  const hours = e.target.closest("[data-hours]");
  if (hours) { ram.hours = +hours.dataset.hours; ram.series = null; renderRam(); return loadRam(); }
  const lock = e.target.closest("[data-lock]");
  if (lock) {
    const port = +lock.dataset.lock, on = !S.excluded_ports.includes(port);
    return run("exclude", { port, excluded: on }, on ? `Puerto ${port} protegido` : `Puerto ${port} sin protección`);
  }
  const unlock = e.target.closest("[data-unlock]");
  if (unlock) return run("exclude", { port: +unlock.dataset.unlock, excluded: false }, "Protección quitada");
  const seg = e.target.closest(".seg");
  if (seg) {
    $("show-all").checked = true; $("filter").value = ""; preview = false; setView("proc");
    const row = document.getElementById("row-" + seg.dataset.id);
    if (row) { row.scrollIntoView({ block: "center" }); row.classList.remove("flash"); void row.offsetWidth; row.classList.add("flash"); }
  }
});

function setView(v) {
  view = v;
  if (v !== "proc") preview = false;
  try { localStorage.setItem("view", v); } catch {}
  render();
  if (v === "ram") { renderRam(); loadRam(); }
}

$("preview").onclick = () => { preview = !preview; if (preview) view = "proc"; render(); };
$("preview-exit").onclick = () => { preview = false; render(); };
$("wt-refresh").onclick = () => run("refresh-worktrees", {}, "Worktrees actualizados");
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
$("show-all").onchange = () => { try { localStorage.setItem("show-all", $("show-all").checked ? "1" : "0"); } catch {} render(); };
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
document.addEventListener("keydown", (e) => {
  if (e.key === "/" && document.activeElement.tagName !== "INPUT") { e.preventDefault(); $("filter").focus(); }
  if (e.key === "Escape" && document.activeElement === $("filter")) { $("filter").value = ""; render(); $("filter").blur(); }
});
let resizeTimer;
window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => { if (view === "ram") renderRam(); }, 150); });
for (const id of ["d-log", "d-settings", "d-ramtable"]) {
  try { $(id).open = localStorage.getItem(id) === "1"; } catch {}
  $(id).ontoggle = () => { try { localStorage.setItem(id, $(id).open ? "1" : "0"); } catch {} };
}
try {
  $("show-all").checked = localStorage.getItem("show-all") === "1";
  const v = localStorage.getItem("view");
  if (["proc", "sess", "ports", "wt", "infra", "ram"].includes(v)) view = v;
} catch {}

poll().then(() => { if (view === "ram") loadRam(); });
setInterval(poll, 2000);
