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


def test_result_text():
    assert tray.result_text({"killed": 2, "freed_mb": 2048, "kept": 1}) == "2 cerrados · 2.0 GB liberados · 1 protegidos siguen abiertos"
    assert tray.result_text({"ok": True}) == "Listo"
