"""Kernel pools and the token leak, the spawn meter, and their notifications. Windows calls are faked except
in the smoke test, which only reads."""
import sys
from types import SimpleNamespace

import pytest

from devreaper import app as app_mod
from devreaper import infra, kernel, spawns, winapi
from devreaper.app import App, session_rows
from devreaper.db import DB, bucket_samples

GB = 1024
HOUR = 3600


def reading(**kw):
    base = {"commit_mb": 40 * GB, "commit_limit_mb": 75 * GB, "paged_mb": 20 * GB, "nonpaged_mb": 2 * GB,
            "paged_resident_mb": 8 * GB, "tokens": 4_500_000, "leak_mb": 12.6 * GB}
    base.update(kw)
    return base


def history(start_leak_mb, per_day_mb, hours=6, now=1_000_000.0, tokens_per_day=4_500_000):
    """[(ts, tokens, leak_mb)] one sample every 10 min, growing linearly."""
    out = []
    for i in range(hours * 6 + 1):
        t = now - hours * HOUR + i * 600
        frac = (t - now) / 86400
        out.append((t, int(4_000_000 + tokens_per_day * frac), start_leak_mb + per_day_mb * frac))
    return out


# ---------- kernel.assess ----------
def test_slope_needs_spread_and_points():
    assert kernel.slope_per_day([(0, 1), (10, 2)]) is None
    assert kernel.slope_per_day([(0, 0), (3600, 1), (7200, 2)]) == pytest.approx(24)


def test_leak_warns_with_restart_date():
    v = kernel.assess(reading(), history(12.6 * GB, 1 * GB), total_mb=32 * GB, used_mb=30 * GB,
                      boot_time=1_000_000.0 - 16 * 86400, now=1_000_000.0)
    assert v["level"] == "warn" and "reiniciando" in v["message"]
    assert v["growth_mb_day"] == pytest.approx(1 * GB, rel=0.01)
    assert v["eta_hours"] is not None and v["uptime_hours"] == pytest.approx(16 * 24)


def test_small_leak_growing_fast_still_warns_by_eta():
    v = kernel.assess(reading(leak_mb=1 * GB, tokens=400_000), history(1 * GB, 2 * GB), total_mb=32 * GB,
                      used_mb=28 * GB, boot_time=0, now=1_000_000.0)
    assert v["level"] == "warn" and v["eta_hours"] < 72


def test_many_tokens_without_growth_is_info_only():
    v = kernel.assess(reading(leak_mb=900, tokens=350_000), history(900, 0, tokens_per_day=0), total_mb=32 * GB,
                      used_mb=10 * GB, boot_time=0, now=1_000_000.0)
    assert v["level"] == "info" and v["eta_hours"] is None


def test_healthy_machine_is_ok():
    v = kernel.assess(reading(leak_mb=150, tokens=40_000), history(150, 0, tokens_per_day=0), total_mb=32 * GB,
                      used_mb=10 * GB, boot_time=0, now=1_000_000.0)
    assert v["level"] == "ok" and v["message"] == ""


@pytest.mark.parametrize("commit,level", [(60, "ok"), (65, "warn"), (72, "crit")])
def test_commit_levels(commit, level):
    v = kernel.assess(reading(commit_mb=commit * GB), [], 32 * GB, 10 * GB, 0, now=1.0)
    assert v["commit_level"] == level


def test_resident_kernel_uses_ram_not_virtual_size():
    assert kernel.resident_mb(reading()) == 10 * GB  # 8 GB resident paged + 2 GB nonpaged, not 20 GB virtual
    assert kernel.resident_mb({"paged_resident_mb": None, "nonpaged_mb": 5}) is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_windows_reads_smoke():
    perf = winapi.perf_info()
    assert perf and perf["commit_limit_mb"] > 0
    tags = winapi.pool_tags()
    assert tags and len(tags) > 100
    table = winapi.process_table()
    assert table and len(table) > 10
    r = kernel.read(winapi.ResidentPagedPool())
    assert r["tokens"] is not None and r["leak_mb"] is not None


# ---------- spawn meter ----------
def test_burst_counts_new_pids_once():
    frames = iter([{1: (0, "a")}, {1: (0, "a"), 2: (1, "git.exe")}, {1: (0, "a"), 2: (1, "git.exe"), 3: (1, "git.exe")},
                   {1: (0, "a")}])
    clock = iter([0.0, 0.1, 0.2, 0.3, 0.3, 0.3])
    r = spawns.burst(0.25, table_fn=lambda: next(frames), clock=lambda: next(clock), sleep=lambda s: None)
    assert set(r["new"]) == {2, 3} and r["seconds"] == 0.3


def test_attribute_charges_agents_launchers_and_self():
    table = {10: (1, "claude.exe"), 11: (10, "bash.exe"), 20: (11, "git.exe"), 21: (10, "git.exe"),
             30: (5, "wsl.exe"), 5: (1, "com.docker.backend.exe"), 40: (99, "conhost.exe"), 99: (1, "pythonw.exe")}
    result = {"seconds": 10.0, "new": {20: table[20], 21: table[21], 30: table[30], 40: table[40]}, "table": table}
    out = spawns.attribute(result, member_of={10: 10}, agents={10: {"agent": "Claude Code", "name": "siek"}},
                           own=frozenset({99}))
    assert out["by_agent"] == {10: 12.0}  # 2 in 10 s = 12/min, one through a bash child
    labels = {r["label"]: r for r in out["launchers"]}
    assert labels["Claude Code · siek"]["children"][0] == {"name": "git.exe", "rate": 12.0}
    assert labels["com.docker.backend.exe"]["rate"] == 6.0
    assert "DevReaper" in labels and out["rate"] == 24.0


def test_session_rows_carry_spawn_rate():
    found = {10: {"pid": 10, "name": "s", "cwd": "", "state": "inactive", "mb": 100.0, "started": 0, "updated": 0}}
    assert session_rows(found, [], set(), 1000.0, {10: 30.0})[0]["spawn_rate"] == 30.0
    assert session_rows(found, [], set(), 1000.0)[0]["spawn_rate"] == 0.0


def test_scanner_caches_survive_their_reset(monkeypatch):
    from devreaper import scanner
    monkeypatch.setattr(scanner, "_owner_cache", {(i, 0.0): True for i in range(5001)})
    monkeypatch.setattr(scanner, "_cwd_cache", {(i, 0.0): "x" for i in range(2001)})
    monkeypatch.setattr(scanner.agents_mod, "agent_cwd", lambda pid: r"C:\w")
    rec = {"pid": 99999, "create_time": 1.0, "proc": SimpleNamespace(username=lambda: "me")}
    assert scanner.owned_by(rec, "me") is True       # used to raise KeyError right after clearing
    assert scanner._cwd(rec) == r"C:\w"


# ---------- storage ----------
def test_bucket_samples_with_and_without_kernel_columns():
    rows = [(0, 100, 10, 5, 1, "[]"), (30, 300, 30, 15, 3, "[]", 2000.0, 500.0)]
    out = bucket_samples(rows, 60)
    assert out[0]["kernel_mb"] == 2000.0 and out[0]["browsers_mb"] == 500.0  # old row has none: not averaged in
    assert bucket_samples([(0, 1, 1, 1, 1, "[]")], 60)[0]["kernel_mb"] is None


def test_old_ram_samples_table_gets_new_columns(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE ram_samples (ts REAL PRIMARY KEY, used_mb REAL, dev_mb REAL, claude_mb REAL, "
                "infra_mb REAL, top TEXT)")
    con.execute("INSERT INTO ram_samples VALUES (?, 1, 1, 1, 1, '[]')", (__import__("time").time(),))
    con.commit()
    con.close()
    db = DB(path)
    db.add_sample(__import__("time").time() + 1, 2, 2, 2, 2, [], 300.0, 400.0)
    series = db.ram_series(1, max_points=1)
    assert series[0]["kernel_mb"] == 300.0


# ---------- app: notifications ----------
@pytest.fixture
def kapp(monkeypatch):
    monkeypatch.setattr(infra, "snapshot", lambda: dict(app_mod.EMPTY_INFRA))
    a = App(DB(":memory:"))
    a.sent = []
    a.notify = lambda title, msg: a.sent.append((title, msg))
    return a


def test_kernel_alert_once_a_day_and_snooze(kapp):
    kapp._kernel = {"level": "warn", "message": "Windows retiene 12.6 GB", "leak_mb": 12.6 * GB}
    assert kapp.check_kernel_alert(now=1e9) and "reinicia" in kapp.sent[0][0]
    assert not kapp.check_kernel_alert(now=1e9 + 3600)
    assert kapp.check_kernel_alert(now=1e9 + 86400 + 1)
    kapp.action("kernel-snooze", {"days": 7})
    assert not kapp.check_kernel_alert(now=__import__("time").time() + 2 * 86400)
    with pytest.raises(ValueError):
        kapp.action("kernel-snooze", {"days": 90})


def test_commit_alert_levels_have_their_own_cooldown(kapp):
    kapp._kernel = {"commit_level": "warn", "commit_pct": 86.0, "commit_mb": 65 * GB, "commit_limit_mb": 75 * GB,
                    "level": "warn", "leak_mb": 12 * GB}
    assert kapp.check_commit_alert(now=1e9) and "kernel" in kapp.sent[0][1]
    assert not kapp.check_commit_alert(now=1e9 + 4 * HOUR)
    kapp._kernel["commit_level"] = "crit"
    assert kapp.check_commit_alert(now=1e9 + 4 * HOUR)  # critical repeats every 3 h
    kapp._kernel["commit_level"] = "ok"
    assert not kapp.check_commit_alert(now=1e9 + 99 * HOUR)


def test_ram_alert_names_the_kernel_first(kapp, monkeypatch):
    monkeypatch.setattr(app_mod.psutil, "virtual_memory",
                        lambda: SimpleNamespace(percent=97, total=32 * 2**30, available=2**30))
    kapp._kernel = {"level": "warn", "leak_mb": 12 * GB}
    assert kapp.check_ram_alert() and "kernel" in kapp.sent[0][1]


def test_kill_agents_closes_only_still_inactive_terminal_agents(kapp, monkeypatch):
    kapp._sessions = {1: {"pid": 1, "kind": "cli", "state": "inactive"},
                      2: {"pid": 2, "kind": "cli", "state": "active"},     # used since the menu was built
                      3: {"pid": 3, "kind": "ide", "state": "inactive"}}   # editors are never closed from here
    closed = []
    monkeypatch.setattr(kapp, "_kill_session",
                        lambda pid, origin: closed.append(pid) or {"killed": 2, "freed_mb": 500.0, "kept": 0})
    r = kapp.action("kill-agents", {"pids": [1, 2, 3, 4]}, origin="bandeja")
    assert closed == [1] and r == {"killed": 3, "freed_mb": 500.0, "kept": 3}


def test_refresh_kernel_stores_and_assesses(kapp, monkeypatch):
    monkeypatch.setattr(kernel, "read", lambda pool=None: reading())
    for i, now in enumerate(range(1_000_000, 1_000_000 + 6 * HOUR + 1, 600)):
        monkeypatch.setattr(kernel, "read", lambda pool=None, i=i: reading(leak_mb=12 * GB + i * 30, tokens=4_000_000 + i * 3000))
        v = kapp.refresh_kernel(now=now)
    assert v["level"] == "warn" and v["growth_mb_day"] == pytest.approx(30 * 144, rel=0.01)
    assert v["implied_spawns_min"] > 0
    k = kapp.state()["kernel"]
    assert k["series"] and k["leak_mb"] is not None
