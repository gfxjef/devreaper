"""Agents found in the process tree, their activity, discovered projects, and the new close options."""
import json
import os
import shutil
import subprocess
import threading
import urllib.request

import pytest

from devreaper import agents, projects, reaper, scanner, sessions, worktrees
from devreaper import app as app_mod
from devreaper.app import App
from devreaper.db import DB
from devreaper.server import make_server


def rec(pid, ppid, name, ct, cmd="", exe=""):
    return {"pid": pid, "ppid": ppid, "name": name, "create_time": ct, "cmd": cmd, "exe": exe, "proc": None}


def group(**kw):
    base = {"id": "g1", "root_pid": 1, "create_time": 0, "pids": [1], "label": "srv", "kind": "next dev",
            "cmd": "node next dev", "ports": [], "exposed": [], "worktree": None, "cwd": "", "mb": 100.0,
            "age_hours": 99.0, "state": "none", "session": None, "excluded": False, "visible": True}
    base.update(kw)
    return base


@pytest.fixture
def fake_app(monkeypatch):
    monkeypatch.setattr(app_mod.infra, "snapshot", lambda: dict(app_mod.EMPTY_INFRA))
    a = App(DB(":memory:"))
    monkeypatch.setattr(a, "rescan", lambda: a.groups())
    return a


# ---------- classification ----------
@pytest.mark.parametrize("name,exe,cmd,key,kind", [
    ("claude.exe", r"C:\Users\j\.local\bin\claude.exe", "claude", "claude", "cli"),
    ("claude.exe", r"C:\Users\j\AppData\Local\AnthropicClaude\app-1.2\claude.exe", "", "claude-desktop", "app"),
    ("node.exe", r"C:\nodejs\node.exe", r"node C:\npm\node_modules\@anthropic-ai\claude-code\cli.js", "claude", "cli"),
    ("node.exe", r"C:\nodejs\node.exe", r"node C:\npm\node_modules\@google\gemini-cli\dist\index.js", "gemini", "cli"),
    ("codex-x86_64-pc-windows-msvc.exe", r"C:\npm\node_modules\@openai\codex\bin\codex.exe", "", "codex", "cli"),
    ("Cursor.exe", r"C:\Users\j\AppData\Local\Programs\cursor\Cursor.exe", "", "cursor", "ide"),
    ("Code.exe", r"C:\Users\j\AppData\Local\Programs\Microsoft VS Code\Code.exe", "", "code", "ide"),
    ("pycharm64.exe", r"C:\Program Files\JetBrains\PyCharm\bin\pycharm64.exe", "", "pycharm64", "ide"),
    ("python.exe", r"C:\py\python.exe", r"python C:\py\Scripts\aider.exe --model x", "aider", "cli"),
])
def test_classify_agents(name, exe, cmd, key, kind):
    info = agents.classify(name, exe, cmd)
    assert info and info[0] == key and info[2] == kind


@pytest.mark.parametrize("name,exe,cmd", [
    ("node.exe", r"C:\nodejs\node.exe", r"node C:\w\app\node_modules\next\dist\bin\next dev"),
    ("python.exe", r"C:\py\python.exe", "python -m uvicorn main:app"),
    ("pycharm64.exe", r"C:\elsewhere\pycharm64.exe", ""),  # not a JetBrains install
    ("explorer.exe", r"C:\Windows\explorer.exe", ""),
])
def test_classify_rejects_non_agents(name, exe, cmd):
    assert agents.classify(name, exe, cmd) is None


def test_find_agents_maps_helpers_to_the_editor_root():
    agents._class_cache.clear()
    procs = {
        1: rec(1, 0, "Cursor.exe", 10, "Cursor.exe", r"C:\p\cursor\Cursor.exe"),
        2: rec(2, 1, "Cursor.exe", 11, "Cursor.exe --type=utility ptyHost", r"C:\p\cursor\Cursor.exe"),
        3: rec(3, 2, "powershell.exe", 12),
        4: rec(4, 3, "node.exe", 13, "node next dev", r"C:\nodejs\node.exe"),
        5: rec(5, 0, "claude.exe", 20, "claude", r"C:\u\.local\bin\claude.exe"),
    }
    found, member_of = agents.find_agents(procs)
    assert set(found) == {1, 5}
    assert member_of == {1: 1, 2: 1, 5: 5}
    assert found[1]["agent"] == "Cursor" and found[5]["kind"] == "cli"
    assert scanner.find_session(procs[4], procs, found, member_of)["agent"] == "Cursor"


def test_build_groups_stops_at_agents():
    procs = {
        1: rec(1, 0, "node.exe", 10, "node watcher"),
        2: rec(2, 1, "claude.exe", 11),
        3: rec(3, 2, "node.exe", 12, "next dev"),
    }
    dev = {1: procs[1], 3: procs[3]}
    assert scanner.build_groups(dev, procs) == {1: [1, 3]}
    assert scanner.build_groups(dev, procs, stop={2}) == {1: [1], 3: [3]}


def test_descendants_skip_agents():
    children = {1: [2, 3], 2: [4], 3: [5]}
    assert sorted(scanner.descendants(1, children, skip={3})) == [2, 4]


# ---------- activity ----------
def test_activity_marks_cpu_bursts():
    act = agents.Activity(now=0)
    act.observe("k", 0.0, 0.03, now=0)
    act.observe("k", 1.0, 0.03, now=10)       # a short burst inside the window is not judged yet
    act.observe("k", 1.2, 0.03, now=60)       # 2 % of a core over the minute: idle
    assert act.last_active("k") is None
    act.observe("k", 5.2, 0.03, now=120)      # 6.7 %: working
    assert act.last_active("k") == 120
    act.forget(set())
    assert act.last_active("k") is None


def test_agent_state_is_conservative_without_evidence():
    assert agents.agent_state(None, now=100, inactive_after_hours=2, observing_since=0) == "active"
    assert agents.agent_state(None, now=3 * 3600, inactive_after_hours=2, observing_since=0) == "inactive"
    assert agents.agent_state(10, now=3 * 3600, inactive_after_hours=2, observing_since=0) == "inactive"
    assert agents.agent_state(3 * 3600 - 60, now=3 * 3600, inactive_after_hours=2, observing_since=0) == "active"
    assert agents.agent_state(10, now=3 * 3600, inactive_after_hours=2, observing_since=0, busy=True) == "active"


def test_last_turn_ignores_bookkeeping_lines(tmp_path):
    f = tmp_path / "s.jsonl"
    lines = [{"type": "user", "timestamp": "2026-10-01T10:00:00Z"},
             {"type": "assistant", "timestamp": "2026-10-01T10:05:00.000Z"},
             {"type": "bridge-session", "sessionId": "x"},
             {"type": "last-prompt", "lastPrompt": "hola"}]
    f.write_text("\n".join(json.dumps(x) for x in lines) + "\n{broken", encoding="utf-8")
    assert agents.last_turn(f) == 1790849100.0
    assert agents.last_turn(tmp_path / "missing.jsonl") is None


def test_claude_project_dir_encoding():
    assert agents.claude_project_dir(r"C:\Users\j\My Proj\x_y").name == "C--Users-j-My-Proj-x-y"


def test_parse_session_accepts_newer_keys():
    s = sessions.parse_session({"pid": 5, "procStartFt": "116444736000000000", "statusUpdatedAt": 9000,
                                "updatedAt": 4000, "startedAt": 1000, "cwd": r"C:\x\proj"})
    assert s["proc_start"] == 0 and s["updated"] == 9 and s["started"] == 1 and s["name"] == "proj"


# ---------- projects ----------
def test_git_repo_of_folder_and_linked_worktree(tmp_path, monkeypatch):
    monkeypatch.setenv("TEMP", r"C:\no-such-temp")  # tmp_path lives under %TEMP%, which is never a project
    repo = tmp_path / "repo"
    (repo / ".git" / "worktrees" / "wt").mkdir(parents=True)
    (repo / "src" / "deep").mkdir(parents=True)
    wt = tmp_path / "elsewhere" / "wt"
    wt.mkdir(parents=True)
    (wt / ".git").write_text(f"gitdir: {repo}/.git/worktrees/wt\n", encoding="utf-8")
    assert projects.git_repo_of(str(repo / "src" / "deep")) == str(repo)
    assert projects.git_repo_of(str(wt)) == os.path.normpath(str(repo))
    assert projects.git_repo_of(os.path.expanduser("~")) is None


@pytest.mark.skipif(not shutil.which("git"), reason="git not installed")
def test_scan_worktrees_includes_discovered_plain_repo(tmp_path):
    repo = tmp_path / "plain"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    rows = worktrees.scan_worktrees([], [], [str(repo)])["rows"]
    assert len(rows) == 1 and rows[0]["status"] == "main" and rows[0]["branch"] == "main"


# ---------- policy ----------
def test_keep_reason_order():
    assert reaper.keep_reason(group(ports=[5050]), 48, {5050}) == "protegido"
    assert reaper.keep_reason(group(keep_until=2e9), 48, set(), now=1e9) == "pospuesto"
    assert reaper.keep_reason(group(state="active"), 48, set()) == "agente activo"
    assert reaper.keep_reason(group(state="inactive", in_use=True), 48, set()) == "en uso"
    assert reaper.keep_reason(group(age_hours=3), 48, set()) == "reciente"
    assert reaper.keep_reason(group(state="orphan"), 48, set()) is None


def test_due_for_auto_skips_active_agents():
    groups = [group(id="a", state="active"), group(id="b", state="inactive"), group(id="c", state="orphan")]
    assert [g["id"] for g in reaper.due_for_auto(groups, 48, set())] == ["b", "c"]


# ---------- actions ----------
def test_kill_selected_skips_protected(fake_app, monkeypatch):
    killed = []
    monkeypatch.setattr(app_mod.reaper, "kill_group", lambda g: killed.append(g["id"]) or 1)
    fake_app.db.exclude_port(3990, True)
    fake_app._groups = [group(id="a"), group(id="b", ports=[3990]), group(id="c")]
    r = fake_app.action("kill-selected", {"ids": ["a", "b", "zzz"]})
    assert killed == ["a"] and r["killed"] == 1 and r["kept"] == 1


def test_keep_alive_postpones_auto_close(fake_app):
    fake_app._groups = [group(id="a")]
    fake_app.action("keep-alive", {"id": "a", "hours": 24})
    keep = fake_app.db.kept_alive()
    assert "a" in keep
    g = dict(group(id="a"), keep_until=keep["a"])
    assert reaper.due_for_auto([g], 48, set()) == []
    fake_app.action("keep-alive", {"id": "a", "hours": 0})
    assert fake_app.db.kept_alive() == {}
    with pytest.raises(ValueError):
        fake_app.action("keep-alive", {"id": "a", "hours": 1000})


def test_kill_agent_procs_leaves_the_agent(fake_app, monkeypatch):
    killed = []
    monkeypatch.setattr(app_mod.reaper, "kill_group", lambda g: killed.append(g["id"]) or 1)
    fake_app._sessions = {7: {"pid": 7, "name": "web", "agent": "Cursor", "kind": "ide", "mb": 900, "cwd": ""}}
    fake_app._groups = [group(id="a", session={"pid": 7}), group(id="b", session={"pid": 8})]
    r = fake_app.action("kill-agent-procs", {"pid": 7})
    assert killed == ["a"] and r["killed"] == 1
    with pytest.raises(ValueError, match="Cursor"):
        fake_app.action("kill-session", {"pid": 7})  # an editor is never closed from DevReaper


def test_dev_names_migration_adds_new_runtimes(tmp_path):
    path = tmp_path / "x.db"
    db = DB(path)
    db.set_setting("dev_names", ["node", "python"])
    db._conn.execute("DELETE FROM settings WHERE key = '_dev_names_v2'")
    db._conn.commit()
    assert DB(path).settings()["dev_names"] == ["node", "python", "java", "go", "dotnet", "ruby"]
    db2 = DB(path)
    db2.set_setting("dev_names", ["node"])  # a user removing names later is respected
    assert DB(path).settings()["dev_names"] == ["node"]


def test_live_endpoint(monkeypatch):
    monkeypatch.setattr(app_mod.infra, "snapshot", lambda: dict(app_mod.EMPTY_INFRA))
    a = App(DB(":memory:"))
    a.rescan()
    a.rescan()
    srv = make_server(a, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        live = json.load(urllib.request.urlopen(f"http://127.0.0.1:{srv.server_address[1]}/api/live"))
        assert len(live["points"]) == 2 and {"used_mb", "dev_mb", "agents_mb", "cpu"} <= set(live["points"][0])
        state = json.load(urllib.request.urlopen(f"http://127.0.0.1:{srv.server_address[1]}/api/state"))
        assert state["live"] and all("keep_reason" in g for g in state["groups"])
    finally:
        srv.shutdown()


def test_idle_agents_are_quiet_terminal_agents_only(fake_app):
    import time
    now = time.time()
    fake_app._sessions = {1: {"pid": 1, "kind": "cli", "state": "inactive", "updated": now - 30 * 3600},
                          2: {"pid": 2, "kind": "ide", "state": "inactive", "updated": now - 30 * 3600},
                          3: {"pid": 3, "kind": "cli", "state": "inactive", "updated": now - 3 * 3600},
                          4: {"pid": 4, "kind": "cli", "state": "active", "updated": now - 30 * 3600},
                          5: {"pid": 5, "kind": "cli", "state": "inactive", "updated": None}}
    assert [s["pid"] for s in fake_app.idle_agents(24)] == [1]
    assert fake_app.action("kill-idle-agents", {"hours": 24}) == {"killed": 0, "freed_mb": 0.0, "kept": 0}  # pid 1 is not real
