"""Tray icon: a RAM ring that changes color with the alert level, and a menu rebuilt every time it opens, so
what is running (and how much RAM it takes) can be seen and closed without opening the panel."""
import os
import shutil
import subprocess
import threading
import webbrowser

from PIL import Image, ImageDraw

COLORS = {"ok": (34, 197, 94), "warn": (234, 179, 8), "bad": (239, 68, 68)}
STATE_WORDS = {"active": "activa", "inactive": "inactiva", "orphan": "huérfano", "none": "sin agente"}
MAX_GROUPS = 12
MAX_AGENTS = 12
MAX_PORTS = 12


def gb(mb):
    if mb is None:
        return "—"
    return f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{mb:.0f} MB"


def dur(hours):
    if hours is None:
        return "—"
    m = round(hours * 60)
    if m < 1:
        return "ahora"
    if m < 60:
        return f"{m} min"
    if m < 1440:
        return f"{m // 60} h {m % 60} min"
    d, h = divmod(m // 60, 24)
    return f"{d} d {h} h" if h else f"{d} d"


def level_of(summary) -> str:
    if summary["ram_percent"] >= 90 or summary["orphans_mb"] >= 1024:
        return "bad"
    if summary["due"] or summary["orphans"] or summary["dev_mb"] >= 4096:
        return "warn"
    return "ok"


def icon_image(level: str, ram_percent: float = 0) -> Image.Image:
    """Dark tile with a ring filled to the RAM percentage, in the level's color. Drawn at 4x and downsampled
    so the arc stays smooth at tray size."""
    s = 256
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((8, 8, s - 8, s - 8), radius=56, fill=(24, 24, 27))
    box, width = (44, 44, s - 44, s - 44), 34
    d.arc(box, 0, 360, fill=(63, 63, 70), width=width)
    pct = max(0.0, min(100.0, ram_percent))
    if pct > 0:
        d.arc(box, -90, -90 + 360 * pct / 100, fill=COLORS[level], width=width)
    d.ellipse((104, 104, s - 104, s - 104), fill=COLORS[level])
    return img.resize((64, 64), Image.LANCZOS)


def tooltip(summary) -> str:
    """Windows truncates tray tooltips at 127 characters."""
    text = (f"DevReaper · RAM {summary['ram_percent']:.0f} % · Dev {gb(summary['dev_mb'])} · "
            f"Agentes {gb(summary['sessions_mb'])}")
    if summary["orphans"]:
        text += f" · {summary['orphans']} huérfanos"
    return text[:127]


def item(text, action=None, enabled=True, children=None, checked=None, default=False):
    return {"text": text, "action": action, "enabled": enabled, "children": children, "checked": checked,
            "default": default}


SEP = None


def idle_text(g):
    """CPU-based last use. Before any burst is seen, say how long DevReaper has been watching instead."""
    hours = g.get("idle_hours") or 0
    if g.get("idle_known"):
        return "En uso ahora" if hours < 0.05 else f"Último uso hace {dur(hours)}"
    return "Midiendo uso…" if hours < 0.1 else f"Sin uso en las últimas {dur(hours)}"


def group_entry(g, url):
    ports = " · " + ", ".join(f":{p}" for p in g["ports"][:3]) if g["ports"] else ""
    flag = "⚠ " if g["state"] == "orphan" else ""
    head = f"{flag}{g['label']} — {gb(g['mb'])}{ports}"
    who = g["session"]
    sub = [item(f"{who['agent']} · {who['name']} ({STATE_WORDS[g['state']]})" if who
                else STATE_WORDS[g["state"]].capitalize(), enabled=False),
           item(f"{g['kind']} · abierto hace {dur(g['age_hours'])} · CPU {g['cpu']:.1f} %", enabled=False),
           item(idle_text(g), enabled=False)]
    if g.get("keep_reason") and g["keep_reason"] != "reciente":
        sub.append(item(f"El cierre automático lo salta: {g['keep_reason']}", enabled=False))
    sub.append(SEP)
    sub += [item(f"Abrir localhost:{p}", ("open", f"http://localhost:{p}")) for p in g["ports"][:3]]
    if g.get("excluded"):
        sub.append(item("Protegido: tiene un puerto con candado", enabled=False))
    else:
        sub.append(item(f"Cerrar (libera {gb(g['mb'])})", ("kill", {"id": g["id"]})))
    if g.get("keep_until"):
        sub.append(item("Volver a permitir el cierre automático", ("keep-alive", {"id": g["id"], "hours": 0})))
    else:
        sub.append(item("No cerrarlo automáticamente en 24 h", ("keep-alive", {"id": g["id"], "hours": 24})))
    sub.append(item("Ver en el panel", ("open", f"{url}#g={g['id']}")))
    return item(head, children=sub)


def agent_entry(s, url):
    n = len(s["groups"])
    head = f"{s['agent']} · {s['name']} — {gb(s['total_mb'])} ({STATE_WORDS[s['state']]})"
    sub = [item(f"{s['agent']} {gb(s['mb'])}" + (f" + {n} procesos {gb(s['groups_mb'])}" if n else " · sin procesos"),
                enabled=False),
           item(f"Sin actividad hace {dur(s['idle_hours'])}" if s["idle_hours"] is not None
                else "Actividad: midiendo…", enabled=False),
           SEP]
    closable = n - s["protected"]
    if closable:
        sub.append(item(f"Cerrar sus procesos ({closable} · {gb(s['groups_mb'])})", ("kill-agent-procs", {"pid": s["pid"]})))
    if s["closable"]:
        sub.append(item("Cerrar el agente y sus procesos", ("kill-session", {"pid": s["pid"]})))
    sub.append(item("Ver en el panel", ("open", f"{url}#a={s['pid']}")))
    return item(head, children=sub)


def menu_model(state, url):
    """The tray menu as plain data (tested without pystray). Actions: ('open', url) or (api action, body)."""
    S, summ = state["system"], state["summary"]
    groups = sorted(state["groups"], key=lambda g: g["mb"], reverse=True)
    agents = state["sessions"]
    ports = [r for r in state["ports"] if r["holder"] and r["dev"]]
    idle = [s for s in agents if s["closable"] and s["state"] == "inactive" and (s["idle_hours"] or 0) >= 24]
    model = [
        item(f"RAM {S['percent']:.0f} % · {gb(S['used_mb'])} de {gb(S['total_mb'])} · CPU {summ['cpu']:.0f} %",
             enabled=False),
        item(f"Desarrollo {gb(summ['dev_mb'])} · Agentes {gb(summ['sessions_mb'])} · Docker/WSL {gb(summ['infra_mb'])}",
             enabled=False),
        SEP,
        item(f"Procesos ({len(groups)})", children=[group_entry(g, url) for g in groups[:MAX_GROUPS]]
             + ([SEP, item(f"… y {len(groups) - MAX_GROUPS} más en el panel", ("open", url))]
                if len(groups) > MAX_GROUPS else []),
             enabled=bool(groups)),
        item(f"Agentes ({len(agents)})", children=[agent_entry(s, url) for s in agents[:MAX_AGENTS]],
             enabled=bool(agents)),
        item(f"Puertos ({len(ports)})", children=[
            item(f":{r['port']} · {r['holder']['label']}" + (" (protegido)" if r["protected"] else ""), children=[
                item(f"Abrir localhost:{r['port']}", ("open", f"http://localhost:{r['port']}")),
                item("Liberar", ("free-port", {"port": r["port"]}),
                     enabled=not r["protected"] and bool(r["holder"].get("killable"))),
            ]) for r in ports[:MAX_PORTS]], enabled=bool(ports)),
        SEP,
        item(f"Cerrar huérfanos ({summ['orphans']} · {gb(summ['orphans_mb'])})", ("kill-orphans", {}),
             enabled=bool(summ["orphans"])),
        item(f"Ejecutar el ciclo de cierre ahora ({summ['due']} · {gb(summ['due_mb'])})", ("run-auto", {}),
             enabled=bool(summ["due"])),
        item(f"Cerrar agentes sin uso en más de 24 h ({len(idle)} · {gb(sum(s['total_mb'] for s in idle))})",
             ("kill-idle-agents", {"hours": 24}), enabled=bool(idle)),
        item(f"Cierre automático cada {state['settings']['auto_interval_hours']:g} h",
             ("settings", {"auto_enabled": not state["settings"]["auto_enabled"]}),
             checked=bool(state["settings"]["auto_enabled"])),
        SEP,
        item("Abrir panel", ("open", url), default=True),
        item("Salir", ("quit", None)),
    ]
    return model


def result_text(r):
    if not r or r.get("ok"):
        return "Listo"
    msg = f"{r['killed']} cerrados · {gb(r['freed_mb'])} liberados" if r.get("killed") else "No había nada que cerrar"
    if r.get("kept"):
        msg += f" · {r['kept']} protegidos siguen abiertos"
    return msg


def find_edge():
    for path in (shutil.which("msedge"),
                 os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
                 os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe")):
        if path and os.path.isfile(path):
            return path
    return None


def open_url(url, as_app=False):
    """The panel opens as an app window (Edge --app: no tabs or address bar, all the space for the panel);
    anything else, or no Edge, goes to the default browser."""
    edge = find_edge() if as_app else None
    if edge:
        try:
            subprocess.Popen([edge, f"--app={url}", "--window-size=1440,900"], close_fds=True)
            return
        except OSError:
            pass
    webbrowser.open(url)


def build_pystray_menu(pystray, model_fn, run):
    """pystray Menu whose items are regenerated from model_fn() each time the native menu is rebuilt."""

    def handler(action):  # pystray inspects the arity: exactly (icon, item), no extra defaulted args
        return lambda icon, menu_item: run(action)

    def convert(entries):
        out = []
        for e in entries:
            if e is SEP:
                out.append(pystray.Menu.SEPARATOR)
            elif e["children"] is not None:
                out.append(pystray.MenuItem(e["text"], pystray.Menu(*convert(e["children"])), enabled=e["enabled"]))
            else:
                action = e["action"]
                kwargs = {"enabled": e["enabled"], "default": e["default"]}
                if e["checked"] is not None:
                    kwargs["checked"] = (lambda _, c=e["checked"]: c)
                out.append(pystray.MenuItem(e["text"], handler(action) if action else None, **kwargs))
        return out

    return pystray.Menu(lambda: convert(model_fn()))


def make_icon_class(pystray):
    """pystray's Win32 icon builds its native menu once; this subclass rebuilds it on the UI thread right before
    it is shown, so the menu always reflects the latest scan and is never destroyed while open."""
    base = pystray.Icon

    class FreshMenuIcon(base):
        def _on_notify(self, wparam, lparam):
            if lparam == 0x0205 and hasattr(self, "_update_menu"):  # WM_RBUTTONUP
                try:
                    self._update_menu()
                except Exception:  # never let a menu error kill the tray loop
                    pass
            return super()._on_notify(wparam, lparam)

    return FreshMenuIcon


def run_in_thread(fn):
    threading.Thread(target=fn, daemon=True).start()
