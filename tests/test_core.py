import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import psutil
import pytest

from devreaper import reaper, scanner, sessions
from devreaper.app import App, _validate
from devreaper.db import DB
from devreaper.server import make_server


def rec(pid, ppid, name, ct, cmd=""):
    return {"pid": pid, "ppid": ppid, "name": name, "create_time": ct, "cmd": cmd, "exe": "", "mb": 100.0}


# sessions
def test_filetime_to_epoch():
    assert sessions.filetime_to_epoch(116444736000000000) == 0  # 1970-01-01
    assert sessions.filetime_to_epoch(116444736000000000 + 86400 * 10_000_000) == 86400


def test_session_status():
    now = 10_000
    assert sessions.session_status({"status": "busy", "updated": 0}, now, 2) == "active"
    assert sessions.session_status({"status": "idle", "updated": now - 3600}, now, 2) == "active"
    assert sessions.session_status({"status": "idle", "updated": now - 3 * 3600}, now, 2) == "inactive"


def test_load_sessions_reads_live_pid(tmp_path):
    me = psutil.Process()
    ft = int((me.create_time() + sessions.FILETIME_EPOCH_OFFSET) * 10_000_000)
    (tmp_path / f"{me.pid}.json").write_text(json.dumps({
        "pid": me.pid, "sessionId": "abc", "cwd": "C:\\x\\proj", "name": "proj-1", "status": "busy",
        "startedAt": 1000, "updatedAt": 2000, "procStart": str(ft)}))
    (tmp_path / "999999.json").write_text(json.dumps({"pid": 999999, "procStart": "1"}))  # dead pid
    (tmp_path / "bad.json").write_text("{not json")
    found = sessions.load_sessions(2, tmp_path)
    assert list(found) == [me.pid]
    assert found[me.pid]["name"] == "proj-1" and found[me.pid]["state"] == "active"


def test_load_sessions_rejects_reused_pid(tmp_path):
    me = psutil.Process()
    ft = int((me.create_time() - 3600 + sessions.FILETIME_EPOCH_OFFSET) * 10_000_000)
    (tmp_path / f"{me.pid}.json").write_text(json.dumps({"pid": me.pid, "procStart": str(ft)}))
    assert sessions.load_sessions(2, tmp_path) == {}


# scanner
@pytest.mark.parametrize("cmd,expected", [
    (r'"C:\nodejs\node.exe" "C:\w\konecta\ssh_nexjs\.worktrees\sec-tableros\frontend\node_modules\next\dist\bin\next" dev',
     "ssh_nexjs › sec-tableros"),
    (r'"node" "C:\w\bimos\bimos-web\node_modules\.bin\..\next\dist\bin\next" dev -p 3000', "bimos-web"),
    (r'"node" "C:\w\shop\frontend\node_modules\vite\bin\vite.js"', "shop/frontend"),
    (r'"node" "C:\npm-cache\_npx\98\node_modules\.bin\..\@playwright\mcp\cli.js"', "Playwright MCP"),
    (r'python C:\w\tools\bot\main.py', "bot/main.py"),
])
def test_make_label(cmd, expected):
    assert scanner.make_label(cmd) == expected


def test_make_label_python_module_uses_cwd():
    assert scanner.make_label("python -m http.server 9555", r"C:\w\site") == "http.server en site"
    assert scanner.make_label("python -m http.server 9555") == "http.server"


def test_kind_of():
    assert scanner.kind_of("npx next dev --webpack -p 3151") == "next dev"
    assert scanner.kind_of("node cli.js @playwright/mcp@latest") == "playwright-mcp"
    assert scanner.kind_of("python -m uvicorn app:app") == "uvicorn"


def test_is_dev():
    names = {"node", "python"}
    assert scanner.is_dev("node.exe", r"C:\Program Files\nodejs\node.exe", "", names)
    assert not scanner.is_dev("node.exe", r"C:\Program Files\Adobe\x\node.exe", "", names)
    assert scanner.is_dev("chrome.exe", r"C:\Users\j\AppData\Local\ms-playwright\chromium\chrome.exe", "", names)
    assert not scanner.is_dev("chrome.exe", r"C:\Program Files\Google\Chrome\chrome.exe", "", names)
    assert not scanner.is_dev("explorer.exe", "", "", names)


def test_build_groups_and_dead_parent():
    procs = {
        1: rec(1, 0, "claude.exe", 10),
        2: rec(2, 1, "cmd.exe", 11),
        3: rec(3, 2, "node.exe", 12, "npx next dev"),
        4: rec(4, 3, "node.exe", 13, "next"),
        5: rec(5, 4, "node.exe", 14, "next-server"),
        6: rec(6, 77, "node.exe", 20),          # parent 77 does not exist
        7: rec(7, 1, "node.exe", 5),            # parent younger than child -> PID reuse -> dead parent
    }
    dev = {p: procs[p] for p in (3, 4, 5, 6, 7)}
    groups = scanner.build_groups(dev, procs)
    assert groups[3] == [3, 4, 5]
    assert groups[6] == [6] and groups[7] == [7]
    assert scanner.find_session(procs[3], procs, {1: {"pid": 1}}) == {"pid": 1}
    assert scanner.find_session(procs[7], procs, {1: {"pid": 1}}) is None


def test_find_session_by_path_prefers_longest_and_skips_home():
    home = r"C:\Users\j"
    ses = {
        1: {"cwd": home, "name": "home"},
        2: {"cwd": r"C:\Users\j\work\repo", "name": "repo"},
        3: {"cwd": r"C:\Users\j\work\repo\.worktrees\wt", "name": "wt"},
    }
    cmd = r'"node" "C:\Users\j\work\repo\.worktrees\wt\frontend\node_modules\next"'
    assert scanner.find_session_by_path([cmd], ses, home)["name"] == "wt"
    assert scanner.find_session_by_path([r"C:\Users\j\other\x.py"], ses, home) is None
    assert scanner.find_session_by_path([r"C:\Users\j\work\repo-two\x"], ses, home) is None


def test_scan_finds_a_real_child_process_with_its_port():
    db = DB(":memory:")
    child = subprocess.Popen([sys.executable, "-m", "http.server", "0", "--bind", "127.0.0.1"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + 10
        groups = []
        while time.time() < deadline:
            groups, listeners, _ = scanner.scan(db.settings(), {}, db, set(), self_pid=-1)
            mine = [g for g in groups if child.pid in g["pids"]]
            if mine and mine[0]["ports"]:
                break
            time.sleep(0.2)
        assert mine and mine[0]["ports"], "child http.server not found with a listening port"
    finally:
        child.kill()


# reaper / policy
def G(**kw):
    base = {"id": "x", "root_pid": 1, "create_time": 0, "pids": [1], "label": "x", "ports": [], "mb": 10,
            "age_hours": 1, "state": "none"}
    base.update(kw)
    return base


def test_due_for_auto_respects_age_and_excluded_ports():
    groups = [G(id="old", age_hours=50), G(id="young", age_hours=10), G(id="kept", age_hours=99, ports=[5050])]
    assert [g["id"] for g in reaper.due_for_auto(groups, 48, {5050})] == ["old"]


def test_orphans_filter():
    groups = [G(id="a", state="orphan"), G(id="b", state="active"), G(id="c", state="orphan", ports=[1])]
    assert [g["id"] for g in reaper.orphans(groups, {1})] == ["a"]


def test_kill_group_kills_tree_and_ignores_reused_pid():
    parent = subprocess.Popen([sys.executable, "-c",
        "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); time.sleep(60)"])
    try:
        p = psutil.Process(parent.pid)
        deadline = time.time() + 10
        while not p.children() and time.time() < deadline:
            time.sleep(0.1)
        kid = p.children()[0]
        assert reaper.kill_group(G(root_pid=p.pid, create_time=p.create_time() + 100, pids=[p.pid])) == 0
        assert reaper.kill_group(G(root_pid=p.pid, create_time=p.create_time(), pids=[p.pid])) == 2
        assert not psutil.pid_exists(kid.pid) or kid.status() == psutil.STATUS_ZOMBIE
    finally:
        parent.kill()


def test_kill_tree_spares_skipped_children():
    parent = subprocess.Popen([sys.executable, "-c",
        "import subprocess,sys,time\n"
        "for _ in range(2): subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
        "time.sleep(60)"])
    p = psutil.Process(parent.pid)
    deadline = time.time() + 10
    while len(p.children()) < 2 and time.time() < deadline:
        time.sleep(0.1)
    keep, doomed = p.children()[:2]
    try:
        reaper.kill_tree(p, skip={keep.pid})
        assert not psutil.pid_exists(parent.pid)
        assert not psutil.pid_exists(doomed.pid)
        assert keep.is_running()
    finally:
        for q in (keep, doomed):
            try:
                q.kill()
            except psutil.Error:
                pass


def test_session_rows_totals_and_protected():
    found = {7: {"pid": 7, "name": "s1", "cwd": "c", "state": "inactive", "started": 0, "updated": 0, "mb": 300.0},
             8: {"pid": 8, "name": "s2", "cwd": "d", "state": "active", "started": 0, "updated": 0, "mb": 100.0}}
    groups = [G(id="a", mb=2000, ports=[3151], session={"pid": 7, "name": "s1", "id": "x"}),
              G(id="b", mb=50, ports=[5050], session={"pid": 7, "name": "s1", "id": "x"}),
              G(id="c", mb=10, session=None)]
    from devreaper.app import session_rows
    rows = session_rows(found, groups, {5050}, now=3600)
    assert [r["name"] for r in rows] == ["s1", "s2"]
    assert rows[0]["total_mb"] == 2350 and rows[0]["groups_mb"] == 2050 and rows[0]["protected"] == 1
    assert rows[1]["groups"] == [] and rows[1]["total_mb"] == 100


def test_kill_session_refuses_unknown_or_non_claude_pid():
    app = App(DB(":memory:"))
    with pytest.raises(KeyError):
        app.action("kill-session", {"pid": 999999})
    me = psutil.Process()
    app._sessions = {me.pid: {"pid": me.pid, "name": "fake", "proc_start": None, "mb": 0, "started": 0}}
    with pytest.raises(ValueError):
        app.action("kill-session", {"pid": me.pid})  # this is python.exe, not claude.exe
    assert me.is_running()


def test_ensure_user_process():
    reaper.ensure_user_process(psutil.Process())  # own process: allowed
    system = next((p for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower() == "lsass.exe"), None)
    if system:
        with pytest.raises(ValueError):
            reaper.ensure_user_process(system)


def test_startup_is_enabled_detects_manual_shortcut(tmp_path):
    from devreaper import startup
    assert not startup.is_enabled(tmp_path)
    (tmp_path / "DevReaper (inicio).cmd - Acceso directo.lnk").write_text("")
    assert startup.is_enabled(tmp_path)
    assert not startup.is_enabled(tmp_path / "missing")


# db
def test_db_roundtrip():
    db = DB(":memory:")
    db.set_setting("auto_max_age_hours", 72)
    assert db.settings()["auto_max_age_hours"] == 72
    db.exclude_port(5050, True)
    db.exclude_port(3000, True)
    db.exclude_port(3000, False)
    assert db.excluded_ports() == {5050}
    db.remember(10, 123.4567, {"id": "s", "name": "n", "cwd": "c", "pid": 9})
    assert db.recall(10, 123.4567)["name"] == "n"
    assert db.recall(10, 999) is None
    db.log_kill(G(label="srv", ports=[3000], mb=1500, age_hours=50), "auto")
    assert db.kill_log()[0]["label"] == "srv"
    with pytest.raises(KeyError):
        db.set_setting("nope", 1)


def test_validate_settings():
    assert _validate("dev_names", ["Node.exe", " python ", ""]) == ["node", "python"]
    with pytest.raises(ValueError):
        _validate("auto_max_age_hours", 0)
    with pytest.raises(KeyError):
        _validate("auto_last_run", 0)


def test_first_launch_defers_auto_cycle():
    db = DB(":memory:")
    App(db)
    assert time.time() - db.settings()["auto_last_run"] < 5


# server
@pytest.fixture
def server():
    app = App(DB(":memory:"))
    app.rescan()
    srv = make_server(app, port=0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv, port
    srv.shutdown()


def test_server_state_and_token(server):
    srv, port = server
    base = f"http://127.0.0.1:{port}"
    html = urllib.request.urlopen(base + "/").read().decode()
    token = html.split('name="devreaper-token" content="')[1].split('"')[0]
    assert len(token) > 20
    state = json.load(urllib.request.urlopen(base + "/api/state"))
    assert {"groups", "sessions", "summary", "settings"} <= set(state)

    req = urllib.request.Request(base + "/api/exclude", data=b'{"port": 4321, "excluded": true}', method="POST")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 403

    req.add_header("X-DevReaper-Token", token)
    out = json.load(urllib.request.urlopen(req))
    assert 4321 in out["state"]["excluded_ports"]


def test_server_security_headers_and_foreign_origin(server):
    srv, port = server
    base = f"http://127.0.0.1:{port}"
    resp = urllib.request.urlopen(base + "/")
    assert "frame-ancestors 'none'" in resp.headers["Content-Security-Policy"]
    assert resp.headers["X-Frame-Options"] == "DENY"
    token = resp.read().decode().split('name="devreaper-token" content="')[1].split('"')[0]
    req = urllib.request.Request(base + "/api/exclude", data=b'{"port": 1, "excluded": true}', method="POST",
                                 headers={"X-DevReaper-Token": token, "Origin": "http://evil.example"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 403


def test_server_rejects_foreign_host(server):
    srv, port = server
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/state", headers={"Host": "evil.example"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 403
