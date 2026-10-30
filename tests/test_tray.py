"""The tray menu is built as plain data, so it is tested here without a real tray icon."""
from devreaper import tray

URL = "http://127.0.0.1:7777/"


def state(**kw):
    g = {"id": "g1", "label": "bimos-web", "mb": 1300.0, "ports": [3000, 3001], "state": "orphan", "kind": "next dev",
         "age_hours": 50, "cpu": 0.4, "idle_hours": 3, "idle_known": True, "keep_reason": None, "excluded": False,
         "keep_until": 0, "session": {"agent": "Claude Code", "name": "siek", "pid": 7}}
    s = {"pid": 7, "agent": "Claude Code", "name": "siek", "state": "inactive", "mb": 300, "total_mb": 1600,
         "groups": [{"id": "g1"}], "groups_mb": 1300, "protected": 0, "closable": True, "idle_hours": 5}
    base = {
        "system": {"percent": 72, "used_mb": 11500, "total_mb": 16000},
        "summary": {"cpu": 12, "dev_mb": 1300, "sessions_mb": 300, "infra_mb": 0, "orphans": 1, "orphans_mb": 1300,
                    "due": 1, "due_mb": 1300, "ram_percent": 72},
        "groups": [g], "sessions": [s],
        "ports": [{"port": 3000, "holder": {"label": "bimos-web", "killable": True}, "dev": True, "protected": False}],
        "settings": {"auto_enabled": True, "auto_interval_hours": 24},
    }
    base.update(kw)
    return base


def find(entries, prefix):
    return next(e for e in entries if e is not tray.SEP and e["text"].startswith(prefix))


def test_menu_shows_ram_and_processes_without_the_panel():
    m = tray.menu_model(state(), URL)
    assert m[0]["text"].startswith("RAM 72 %") and not m[0]["enabled"]
    procs = find(m, "Procesos (1)")
    g = procs["children"][0]
    assert g["text"].startswith("⚠ bimos-web — 1.3 GB · :3000, :3001")
    actions = [e["action"] for e in g["children"] if e is not tray.SEP and e["action"]]
    assert ("kill", {"id": "g1"}) in actions
    assert ("open", "http://localhost:3000") in actions
    assert ("open", URL + "#g=g1") in actions
    assert ("keep-alive", {"id": "g1", "hours": 24}) in actions


def test_menu_agents_and_bulk_actions():
    m = tray.menu_model(state(), URL)
    agent = find(m, "Agentes (1)")["children"][0]
    acts = [e["action"][0] for e in agent["children"] if e is not tray.SEP and e["action"]]
    assert acts == ["kill-agent-procs", "kill-session", "open"]
    assert find(m, "Cerrar huérfanos (1")["enabled"]
    auto = find(m, "Cierre automático")
    assert auto["checked"] is True and auto["action"] == ("settings", {"auto_enabled": False})
    assert find(m, "Abrir panel")["default"]


def test_menu_protected_group_and_editor_agent():
    st = state()
    st["groups"][0].update(excluded=True, keep_until=1)
    st["sessions"][0].update(closable=False)
    m = tray.menu_model(st, URL)
    g = find(m, "Procesos")["children"][0]
    texts = [e["text"] for e in g["children"] if e is not tray.SEP]
    assert "Protegido: tiene un puerto con candado" in texts
    assert "Volver a permitir el cierre automático" in texts
    agent = find(m, "Agentes")["children"][0]
    assert all(e["action"] is None or e["action"][0] != "kill-session" for e in agent["children"] if e is not tray.SEP)


def test_menu_disables_empty_sections():
    m = tray.menu_model(state(groups=[], sessions=[], ports=[]), URL)
    assert not find(m, "Procesos (0)")["enabled"] and not find(m, "Agentes (0)")["enabled"]


def test_icon_ring_and_tooltip():
    img = tray.icon_image("warn", 72)
    assert img.size == (64, 64)
    assert img.getpixel((32, 32))[:3] == tray.COLORS["warn"]
    summary = state()["summary"]
    assert len(tray.tooltip(summary)) <= 127 and "huérfanos" in tray.tooltip(summary)
    assert tray.level_of(summary) == "bad"  # 1.3 GB of orphans


KERNEL = {"level": "warn", "leak_mb": 12.6 * 1024, "growth_mb_day": 1024, "eta_hours": 30, "uptime_hours": 384,
          "commit_level": "warn", "commit_pct": 86, "commit_mb": 65 * 1024, "commit_limit_mb": 75 * 1024}


def test_menu_kernel_leak_and_commit_lines():
    m = tray.menu_model(state(kernel=KERNEL, live={"browsers_mb": 6800}), URL)
    assert "Navegadores 6.6 GB" in m[1]["text"]
    leak = find(m, "⚠ Kernel retenido 12.6 GB")
    texts = [e["text"] for e in leak["children"] if e is not tray.SEP]
    assert "Crece 1.0 GB al día" in texts and "Cerrar programas no lo libera: solo reiniciar" in texts
    assert any(t.startswith("A este ritmo llegas al 90 % el ") for t in texts)
    assert ("kernel-snooze", {"days": 7}) in [e["action"] for e in leak["children"] if e is not tray.SEP]
    assert find(m, "Memoria comprometida 86 %")


def test_menu_hides_small_kernel_and_ok_commit():
    m = tray.menu_model(state(kernel={"leak_mb": 300, "commit_level": "ok"}), URL)
    assert not any(e is not tray.SEP and e["text"].startswith(("⚠ Kernel", "Memoria comprometida")) for e in m)


def test_idle_agent_that_keeps_launching_processes():
    st = state()
    st["sessions"][0]["spawn_rate"] = 42.0
    m = tray.menu_model(st, URL)
    agent = find(m, "Agentes (1)")["children"][0]
    assert agent["text"].endswith("· lanza 42 proc/min")
    bulk = find(m, "Cerrar agentes inactivos que siguen lanzando procesos (1")
    assert bulk["action"] == ("kill-agents", {"pids": [7]})
    st["sessions"][0]["state"] = "active"
    assert not any(e is not tray.SEP and e["text"].startswith("Cerrar agentes inactivos")
                   for e in tray.menu_model(st, URL))


def test_kernel_and_commit_raise_the_icon_level():
    calm = dict(state()["summary"], orphans=0, orphans_mb=0, due=0, dev_mb=100)
    assert tray.level_of(calm) == "ok"
    assert tray.level_of(dict(calm, kernel_level="warn")) == "warn"
    assert tray.level_of(dict(calm, commit_level="crit")) == "bad"
    assert "Kernel 12.6 GB" in tray.tooltip(dict(calm, kernel_level="warn", leak_mb=12.6 * 1024))


def test_result_text():
    assert tray.result_text({"killed": 2, "freed_mb": 2048, "kept": 1}) == "2 cerrados · 2.0 GB liberados · 1 protegidos siguen abiertos"
    assert tray.result_text({"ok": True}) == "Listo"
