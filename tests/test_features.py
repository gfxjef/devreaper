"""Ports, worktrees, Docker/WSL, RAM history and kill origin. Docker and WSL are always faked here:
these tests must never stop a real container or shut down WSL."""
import json
import os
import shutil
import sqlite3
import subprocess
import threading
import time
import urllib.request
from types import SimpleNamespace

import pytest

from devreaper import app as app_mod
from devreaper import infra, quiet, worktrees
from devreaper.app import App, port_rows, worktree_rows
from devreaper.db import DB, bucket_samples
from devreaper.server import make_server

WT = r"C:\w\Python Proyects\siek_api\.worktrees\sec-whatsapp"


def group(**kw):
    base = {"id": "g1", "root_pid": 1, "create_time": 0, "pids": [1], "label": "srv", "kind": "next dev",
            "cmd": "node next dev", "ports": [], "exposed": [], "worktree": None, "cwd": "", "mb": 100.0,
            "age_hours": 1.0, "state": "none", "session": None, "excluded": False, "visible": True}
    base.update(kw)
    return base


def container(**kw):
    base = {"id": "5c753903674a", "name": "waha", "image": "devlikeapro/waha", "status": "Up 3 hours",
            "running_for": "", "ports": [{"host_ip": "127.0.0.1", "host_port": 3990, "container_port": 3000}],
            "compose_project": "waha", "workdir": WT + r"\deploy\waha", "worktree": WT, "mb": 365.0}
    base.update(kw)
    return base


@pytest.fixture
def no_docker(monkeypatch):
    """Fail loudly if anything tries to touch real Docker or WSL."""
    calls = []
    monkeypatch.setattr(infra, "docker_stop", lambda cid: calls.append(("stop", cid)))
    monkeypatch.setattr(infra, "wsl_shutdown", lambda: calls.append(("wsl",)))
    monkeypatch.setattr(infra, "snapshot", lambda: dict(app_mod.EMPTY_INFRA))
    return calls


@pytest.fixture
def fake_app(no_docker, monkeypatch):
    a = App(DB(":memory:"))
    monkeypatch.setattr(a, "rescan", lambda: a.groups())
    return a


# ---------- worktrees ----------
@pytest.mark.parametrize("text,expected", [
    (r'"C:\Program Files\nodejs\node.exe" "C:\w\Python Proyects\konecta\ssh_nexjs\.worktrees\sec-tableros\frontend\node_modules\next"',
     r"C:\w\Python Proyects\konecta\ssh_nexjs\.worktrees\sec-tableros"),
    (r"C:\w\repo\.worktrees\wt", r"C:\w\repo\.worktrees\wt"),
    ("C:/w/repo/.worktrees/wt/deploy/waha", os.path.normpath("C:/w/repo/.worktrees/wt")),
    (r"C:\w\repo\frontend", None),
    ("", None),
])
def test_worktree_of(text, expected):
    assert worktrees.worktree_of(text) == expected


def test_repo_of():
    assert worktrees.repo_of(r"C:\w\repo\.worktrees\wt") == r"C:\w\repo"


def test_classify(tmp_path):
    wt = {"path": str(tmp_path), "branch": "feat", "detached": False, "prunable": False}
    assert worktrees.classify(wt, "main", {"feat"}, False, False) == "merged"
    assert worktrees.classify(wt, "main", set(), True, False) == "gone"
    assert worktrees.classify(wt, "main", set(), False, False) == "open"
    assert worktrees.classify(wt, "main", set(), False, True) == "main"
    assert worktrees.classify(dict(wt, path=str(tmp_path / "nope")), "main", set(), False, False) == "missing"


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.mark.skipif(not shutil.which("git"), reason="git not installed")
def test_worktrees_read_from_git_files_match_git(tmp_path, monkeypatch):
    """Branches, worktrees, upstreams and merged state come from .git files; git runs only for --merged,
    and not again until a ref moves."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "one", cwd=repo)
    _git("branch", "merged-one", cwd=repo)
    _git("worktree", "add", "-q", str(repo / ".worktrees" / "done"), "merged-one", cwd=repo)
    _git("worktree", "add", "-q", "-b", "feat", str(repo / ".worktrees" / "feat"), cwd=repo)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "two",
         cwd=repo / ".worktrees" / "feat")
    _git("worktree", "add", "-q", "-b", "gone-one", str(repo / ".worktrees" / "gone"), cwd=repo)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "three",
         cwd=repo / ".worktrees" / "gone")
    _git("config", "branch.gone-one.remote", "origin", cwd=repo)
    _git("config", "branch.gone-one.merge", "refs/heads/gone-one", cwd=repo)
    _git("pack-refs", "--all", cwd=repo)  # tips must also be found in packed-refs

    calls = []
    real = worktrees.quiet.run
    monkeypatch.setattr(worktrees.quiet, "run", lambda args, **kw: calls.append(args[3:5]) or real(args, **kw))
    rows = {r["name"]: r for r in worktrees.scan_worktrees([], [], [str(repo)])["rows"]}
    assert rows["(principal)"]["status"] == "main" and rows["(principal)"]["branch"] == "main"
    assert rows["done"]["status"] == "merged" and rows["done"]["base"] == "main"
    assert rows["feat"]["status"] == "open"
    assert rows["gone"]["status"] == "gone"
    assert all(r["last_commit"] > 1_600_000_000 for r in rows.values())
    calls.clear()
    worktrees.scan_worktrees([], [], [str(repo)])
    assert calls == []  # nothing moved: no git process at all


def test_find_repos_only_with_linked_worktrees(tmp_path):
    (tmp_path / "a" / "repo1" / ".git" / "worktrees").mkdir(parents=True)
    (tmp_path / "a" / "repo2" / ".git").mkdir(parents=True)                       # no linked worktrees
    (tmp_path / "b" / "node_modules" / "x" / ".git" / "worktrees").mkdir(parents=True)  # skipped dir
    (tmp_path / "c" / "d" / "e" / "f" / "deep" / ".git" / "worktrees").mkdir(parents=True)  # too deep
    assert worktrees.find_repos([tmp_path]) == {os.path.normpath(str(tmp_path / "a" / "repo1"))}


# ---------- infra parsing ----------
def test_docker_api_ports_and_memory():
    ports = infra.api_ports([{"IP": "0.0.0.0", "PrivatePort": 5432, "PublicPort": 5432, "Type": "tcp"},
                             {"IP": "::", "PrivatePort": 5432, "PublicPort": 5432, "Type": "tcp"},
                             {"IP": "127.0.0.1", "PrivatePort": 3000, "PublicPort": 3990, "Type": "tcp"},
                             {"IP": "0.0.0.0", "PrivatePort": 53, "PublicPort": 53, "Type": "udp"},
                             {"PrivatePort": 9000, "Type": "tcp"}])  # exposed but not published
    assert [(p["host_ip"], p["host_port"], p["container_port"]) for p in ports] == [
        ("0.0.0.0", 5432, 5432), ("127.0.0.1", 3990, 3000)]
    assert infra.stats_mb({"memory_stats": {"usage": 300 * 2**20, "stats": {"inactive_file": 100 * 2**20}}}) == 200
    assert infra.stats_mb({"memory_stats": {"usage": 50 * 2**20, "stats": {"total_inactive_file": 10 * 2**20}}}) == 40
    assert infra.stats_mb({}) == 0


def test_parse_http_chunked_and_plain():
    raw = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\n[{\"a\"\r\n3\r\n:1}\r\n1\r\n]\r\n0\r\n\r\n"
    assert infra.parse_http(raw) == (200, b'[{"a":1}]')
    assert infra.parse_http(b"HTTP/1.1 204 No Content\r\n\r\n") == (204, b"")
    assert infra.parse_http(b"garbage") == (0, b"")


def test_docker_unreachable_is_none(monkeypatch):
    monkeypatch.setattr(infra, "docker_pipe", lambda: r"\\.\pipe\devreaper-no-such-pipe")
    assert infra.docker_containers() is None


def test_quiet_decode_utf16():
    assert quiet.decode("docker-desktop\r\n".encode("utf-16-le")).strip() == "docker-desktop"
    assert quiet.decode(b"plain") == "plain"


def test_docker_stop_rejects_bad_id():
    with pytest.raises(ValueError):
        infra.docker_stop("waha; rm -rf /")


# ---------- db ----------
def test_kill_log_migration_adds_origin(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE kill_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, label TEXT, "
                "pids TEXT, ports TEXT, mb REAL, age_hours REAL, reason TEXT)")
    con.execute("INSERT INTO kill_log (ts, label, pids, ports, mb, age_hours, reason) VALUES (1, 'old', '[]', '[]', 5, 1, 'x')")
    con.commit()
    con.close()
    db = DB(path)
    db.log_kill(dict(group(), cmd="node x"), "manual", "bandeja")
    log = db.kill_log()
    assert log[0]["origin"] == "bandeja" and log[0]["cmd"] == "node x"
    assert log[1]["label"] == "old" and log[1]["origin"] == ""
    assert db.freed_since(0) == 105


def test_port_history_upsert():
    db = DB(":memory:")
    db.touch_ports([(3000, "bimos-web", "", "next dev")], now=time.time() - 100)
    db.touch_ports([(3000, "bimos-web", "", "next dev"), (3000, "otro", "C:\\wt", "vite")])
    hist = db.port_history()[3000]
    assert {h["label"] for h in hist} == {"bimos-web", "otro"}
    first = next(h for h in hist if h["label"] == "bimos-web")
    assert first["last_seen"] - first["first_seen"] >= 99


def test_ram_series_buckets():
    rows = [(0, 100, 10, 5, 1, json.dumps([{"label": "a", "mb": 10}])),
            (30, 300, 30, 15, 3, json.dumps([{"label": "b", "mb": 30}])),
            (120, 200, 20, 10, 2, "[]")]
    out = bucket_samples(rows, 60)
    assert len(out) == 2
    assert out[0]["used_mb"] == 200 and out[0]["top"][0]["label"] == "b"


# ---------- views ----------
def test_port_rows_holders_and_conflict():
    groups = [group(ports=[3151], exposed=[3151], worktree=r"C:\r\.worktrees\a")]
    listeners = [
        {"port": 3151, "pid": 1, "ip": "0.0.0.0", "name": "node.exe", "group_id": "g1", "mine": True, "infra": False, "exposed": True},
        {"port": 3990, "pid": 9, "ip": "127.0.0.1", "name": "com.docker.backend.exe", "group_id": None, "mine": True, "infra": True, "exposed": False},
        {"port": 135, "pid": 4, "ip": "0.0.0.0", "name": "svchost.exe", "group_id": None, "mine": False, "infra": False, "exposed": True},
    ]
    history = {3151: [{"label": "a", "worktree": r"C:\r\.worktrees\a"}, {"label": "b", "worktree": r"C:\r\.worktrees\b"}],
               5050: [{"label": "flask", "worktree": ""}]}
    rows = {r["port"]: r for r in port_rows(groups, listeners, [container()], {3990}, history)}
    assert rows[3151]["holder"]["type"] == "group" and rows[3151]["exposed"] and rows[3151]["conflict"]
    assert rows[3990]["holder"]["type"] == "docker" and rows[3990]["protected"] and not rows[3990]["exposed"]
    assert rows[135]["holder"]["killable"] is False and not rows[135]["dev"]
    assert rows[5050]["holder"] is None and rows[5050]["dev"]


def test_worktree_rows_match_processes_containers_sessions():
    wt_rows = [{"repo": r"C:\w\Python Proyects\siek_api", "repo_name": "siek_api", "path": WT, "name": "sec-whatsapp",
                "branch": "sec-whatsapp", "base": "origin/main", "status": "merged", "last_commit": 1},
               {"repo": r"C:\w\Python Proyects\siek_api", "repo_name": "siek_api", "path": r"C:\w\Python Proyects\siek_api",
                "name": "(principal)", "branch": "main", "base": "origin/main", "status": "main", "last_commit": 2}]
    groups = [group(id="a", worktree=WT, ports=[8142], mb=400),
              group(id="b", worktree=None, cwd=r"C:\w\Python Proyects\siek_api\backend", mb=50)]
    sessions = [{"name": "s1", "state": "active", "cwd": WT}]
    rows = {r["name"]: r for r in worktree_rows(wt_rows, groups, [container()], sessions, {3990})}
    wa = rows["sec-whatsapp"]
    assert wa["candidate"] and wa["mb"] == 765.0 and wa["protected"] == 1
    assert [s["name"] for s in wa["sessions"]] == ["s1"]
    assert [g["mb"] for g in rows["(principal)"]["groups"]] == [50]
    assert rows["(principal)"]["sessions"] == []  # the session lives in a worktree, not in the main checkout
    only_protected = worktree_rows(wt_rows[:1], [], [container()], [], {3990})[0]
    assert not only_protected["candidate"]  # nothing closable left there


# ---------- actions (Docker/WSL faked) ----------
def test_free_port_never_kills_docker_or_wsl_infra(fake_app):
    fake_app._listeners = [{"port": 4000, "pid": 9, "ip": "127.0.0.1", "name": "com.docker.backend.exe",
                            "group_id": None, "mine": True, "infra": True, "exposed": False}]
    with pytest.raises(ValueError, match="Docker/WSL"):
        fake_app.action("free-port", {"port": 4000})


def test_free_port_on_container_port_stops_only_that_container(fake_app, no_docker):
    fake_app._infra = dict(app_mod.EMPTY_INFRA, containers=[container(id="aaaaaaaaaaaa", name="db", ports=[
        {"host_ip": "127.0.0.1", "host_port": 5432, "container_port": 5432}])])
    r = fake_app.action("free-port", {"port": 5432}, origin="panel")
    assert no_docker == [("stop", "aaaaaaaaaaaa")] and r["killed"] == 1
    assert fake_app.db.kill_log()[0]["origin"] == "panel"


def test_protected_container_is_never_stopped(fake_app, no_docker):
    fake_app.db.exclude_port(3990, True)
    fake_app._infra = dict(app_mod.EMPTY_INFRA, containers=[container()])
    with pytest.raises(ValueError):
        fake_app.action("docker-stop", {"id": "5c753903674a"})
    with pytest.raises(ValueError):
        fake_app.action("free-port", {"port": 3990})
    with pytest.raises(ValueError, match="waha"):
        fake_app.action("wsl-shutdown", {})
    assert no_docker == []


def test_kill_worktree_skips_protected(fake_app, no_docker, monkeypatch):
    fake_app.db.exclude_port(3990, True)
    other = container(id="bbbbbbbbbbbb", name="redis", ports=[{"host_ip": "127.0.0.1", "host_port": 6379, "container_port": 6379}])
    fake_app._infra = dict(app_mod.EMPTY_INFRA, containers=[container(), other])
    killed = []
    monkeypatch.setattr(app_mod.reaper, "kill_group", lambda g: killed.append(g["id"]) or 1)
    fake_app._groups = [group(id="a", worktree=WT, ports=[8142]), group(id="b", worktree=r"C:\other\.worktrees\x")]
    r = fake_app.action("kill-worktree", {"path": WT})
    assert killed == ["a"] and no_docker == [("stop", "bbbbbbbbbbbb")]
    assert r["killed"] == 2 and r["kept"] == 1


def test_kill_worktree_on_main_checkout_closes_what_the_row_shows(fake_app, no_docker, monkeypatch):
    repo = r"C:\w\Python Proyects\siek_api"
    fake_app._worktrees = {"rows": [{"repo": repo, "repo_name": "siek_api", "path": repo, "name": "(principal)",
                                     "branch": "main", "base": "origin/main", "status": "main", "last_commit": 0}],
                           "error": None, "updated": 1}
    fake_app._groups = [group(id="main-srv", worktree=None, cwd=repo + r"\backend"),
                        group(id="wt-srv", worktree=WT, cwd=WT)]
    killed = []
    monkeypatch.setattr(app_mod.reaper, "kill_group", lambda g: killed.append(g["id"]) or 1)
    row = worktree_rows(fake_app._worktrees["rows"], fake_app._groups, [], [], set())[0]
    assert [g["label"] for g in row["groups"]] == ["srv"]
    fake_app.action("kill-worktree", {"path": repo})
    assert killed == ["main-srv"]  # the linked worktree's server is not part of the main checkout


def test_ram_alert_has_cooldown(fake_app, monkeypatch):
    sent = []
    fake_app.notify = lambda title, msg: sent.append(msg)
    monkeypatch.setattr(app_mod.psutil, "virtual_memory", lambda: SimpleNamespace(percent=95, total=32 * 2**30, available=2**30))
    fake_app._groups = [group(label="ssh_nexjs › comp", mb=2600)]
    assert fake_app.check_ram_alert() and "ssh_nexjs › comp" in sent[0]
    assert not fake_app.check_ram_alert() and len(sent) == 1


def test_sample_ram_records_top(fake_app):
    fake_app._groups = [group(label="big", mb=2000), group(id="g2", label="small", mb=10)]
    fake_app.sample_ram()
    series = fake_app.ram_series(1)
    assert len(series) == 1 and series[0]["dev_mb"] == 2010 and series[0]["top"][0]["label"] == "big"


# ---------- server ----------
def test_server_assets_csp_and_ram_endpoint(no_docker):
    a = App(DB(":memory:"))
    a.rescan()
    srv = make_server(a, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        html = urllib.request.urlopen(base + "/")
        assert "script-src 'self'" in html.headers["Content-Security-Policy"]
        assert "<script>" not in html.read().decode()  # no inline scripts
        js = urllib.request.urlopen(base + "/app.js")
        assert js.headers["Content-Type"].startswith("text/javascript")
        data = json.load(urllib.request.urlopen(base + "/api/ram?hours=24"))
        assert data["hours"] == 24 and data["series"] == []
        state = json.load(urllib.request.urlopen(base + "/api/state"))
        assert {"ports", "worktrees", "infra"} <= set(state)
        ports = json.load(urllib.request.urlopen(base + "/api/ports"))
        assert ports["listening"] == sorted({l["port"] for l in a._listeners}) and ports["scanned_at"] > 0
    finally:
        srv.shutdown()
