"""SQLite storage: settings, protected ports, process lineage, kill log, postponed closes and known projects."""
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

DEFAULTS = {
    "auto_enabled": True,
    "auto_interval_hours": 24,
    "auto_max_age_hours": 48,
    "auto_last_run": 0,
    "show_min_mb": 300,
    "show_min_age_hours": 24,
    "inactive_after_hours": 2,
    "dev_names": ["node", "python", "pythonw", "uvicorn", "bun", "deno", "java", "go", "dotnet", "ruby"],
    # Extra folders to search for repos. Usually empty: projects are discovered from where agents work.
    "project_roots": [],
    "ram_alert_percent": 90,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS excluded_ports (port INTEGER PRIMARY KEY, note TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS lineage (
    pid INTEGER NOT NULL, create_time REAL NOT NULL,
    session_id TEXT, session_name TEXT, session_cwd TEXT, claude_pid INTEGER,
    PRIMARY KEY (pid, create_time));
CREATE TABLE IF NOT EXISTS kill_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, label TEXT, pids TEXT,
    ports TEXT, mb REAL, age_hours REAL, reason TEXT);
CREATE TABLE IF NOT EXISTS port_history (
    port INTEGER NOT NULL, label TEXT NOT NULL, worktree TEXT, kind TEXT,
    first_seen REAL NOT NULL, last_seen REAL NOT NULL, PRIMARY KEY (port, label));
CREATE TABLE IF NOT EXISTS ram_samples (
    ts REAL PRIMARY KEY, used_mb REAL, dev_mb REAL, claude_mb REAL, infra_mb REAL, top TEXT);
CREATE TABLE IF NOT EXISTS keep_alive (group_id TEXT PRIMARY KEY, until REAL NOT NULL, label TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS projects (path TEXT PRIMARY KEY, last_seen REAL NOT NULL);
"""
MIGRATIONS = {"kill_log": {"origin": "TEXT DEFAULT ''", "cmd": "TEXT DEFAULT ''"},
              "lineage": {"agent": "TEXT DEFAULT ''"}}
# Runtimes added to the default watch list after release, merged once into lists users already saved.
DEV_NAMES_V2 = ["java", "go", "dotnet", "ruby"]


def default_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "DevReaper" / "devreaper.db"


class DB:
    def __init__(self, path=None):
        self.path = Path(path) if path else default_path()
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._remembered = {}
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.executescript(SCHEMA)
        for table, columns in MIGRATIONS.items():
            have = {row[1] for row in self._conn.execute(f"PRAGMA table_info({table})")}
            for column, decl in columns.items():
                if column not in have:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        saved = self._conn.execute("SELECT value FROM settings WHERE key = 'dev_names'").fetchone()
        done = self._conn.execute("SELECT 1 FROM settings WHERE key = '_dev_names_v2'").fetchone()
        if saved and not done:
            names = json.loads(saved[0])
            names += [n for n in DEV_NAMES_V2 if n not in names]
            self._conn.execute("UPDATE settings SET value = ? WHERE key = 'dev_names'", (json.dumps(names),))
        self._conn.execute("INSERT OR IGNORE INTO settings VALUES ('_dev_names_v2', 'true')")
        self._conn.commit()

    def _exec(self, sql, args=()):
        with self._lock:
            cur = self._conn.execute(sql, args)
            self._conn.commit()
            return cur.fetchall()

    # settings
    def settings(self) -> dict:
        out = dict(DEFAULTS)
        for key, value in self._exec("SELECT key, value FROM settings"):
            if key in DEFAULTS:
                out[key] = json.loads(value)
        return out

    def set_setting(self, key, value):
        if key not in DEFAULTS:
            raise KeyError(key)
        self._exec("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, json.dumps(value)))

    # excluded ports
    def excluded_ports(self) -> set:
        return {r[0] for r in self._exec("SELECT port FROM excluded_ports")}

    def exclude_port(self, port: int, excluded: bool):
        if excluded:
            self._exec("INSERT OR IGNORE INTO excluded_ports (port) VALUES (?)", (int(port),))
        else:
            self._exec("DELETE FROM excluded_ports WHERE port = ?", (int(port),))

    # lineage
    def remember(self, pid, create_time, session):
        """Link a dev process to the agent that launched it (written once per process, then only on change)."""
        key = (pid, round(create_time, 3))
        row = (session["id"], session["name"], session["cwd"], session["pid"], session.get("agent", ""))
        if self._remembered.get(key) == row:
            return
        self._exec("INSERT OR REPLACE INTO lineage (pid, create_time, session_id, session_name, session_cwd,"
                   " claude_pid, agent) VALUES (?, ?, ?, ?, ?, ?, ?)", key + row)
        self._remembered[key] = row
        if len(self._remembered) > 5000:
            self._remembered.clear()

    def recall(self, pid, create_time):
        rows = self._exec(
            "SELECT session_id, session_name, session_cwd, claude_pid, agent FROM lineage"
            " WHERE pid = ? AND create_time = ?", (pid, round(create_time, 3)),
        )
        if not rows:
            return None
        sid, name, cwd, cpid, agent = rows[0]
        return {"id": sid, "name": name, "cwd": cwd, "pid": cpid, "agent": agent or "Claude Code"}

    def prune_lineage(self, older_than_days=14):
        cutoff = time.time() - older_than_days * 86400
        self._exec("DELETE FROM lineage WHERE create_time < ?", (cutoff,))

    # kill log
    def log_kill(self, group: dict, reason: str, origin: str = ""):
        self._exec(
            "INSERT INTO kill_log (ts, label, pids, ports, mb, age_hours, reason, origin, cmd)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), group["label"], json.dumps(group["pids"]), json.dumps(group["ports"]),
             group["mb"], group["age_hours"], reason, origin, group.get("cmd", "")),
        )

    def kill_log(self, limit=40):
        rows = self._exec(
            "SELECT ts, label, ports, mb, age_hours, reason, origin, cmd FROM kill_log ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [
            {"ts": ts, "label": label, "ports": json.loads(ports), "mb": mb, "age_hours": age, "reason": reason,
             "origin": origin or "", "cmd": cmd or ""}
            for ts, label, ports, mb, age, reason, origin, cmd in rows
        ]

    def freed_since(self, ts: float) -> float:
        rows = self._exec("SELECT COALESCE(SUM(mb), 0) FROM kill_log WHERE ts >= ?", (ts,))
        return round(rows[0][0], 1)

    # port history
    def touch_ports(self, entries, now=None):
        """entries: [(port, label, worktree, kind)]. Upserts first/last seen."""
        now = now or time.time()
        with self._lock:
            self._conn.executemany(
                "INSERT INTO port_history VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(port, label) DO UPDATE SET"
                " last_seen = excluded.last_seen, worktree = excluded.worktree, kind = excluded.kind",
                [(port, label, wt or "", kind, now, now) for port, label, wt, kind in entries],
            )
            self._conn.commit()

    def port_history(self, days=7):
        rows = self._exec(
            "SELECT port, label, worktree, kind, first_seen, last_seen FROM port_history"
            " WHERE last_seen >= ? ORDER BY last_seen DESC", (time.time() - days * 86400,))
        out = {}
        for port, label, wt, kind, first, last in rows:
            out.setdefault(port, []).append(
                {"label": label, "worktree": wt, "kind": kind, "first_seen": first, "last_seen": last})
        return out

    # postponed closes ("keep alive"): the auto cycle and bulk closes skip these groups until `until`
    def keep_alive(self, group_id, hours, label=""):
        if hours <= 0:
            self._exec("DELETE FROM keep_alive WHERE group_id = ?", (group_id,))
        else:
            self._exec("INSERT OR REPLACE INTO keep_alive VALUES (?, ?, ?)",
                       (group_id, time.time() + hours * 3600, label))

    def kept_alive(self) -> dict:
        return dict(self._exec("SELECT group_id, until FROM keep_alive WHERE until > ?", (time.time(),)))

    # projects discovered from where agents ran
    def touch_projects(self, paths, now=None):
        now = now or time.time()
        with self._lock:
            self._conn.executemany("INSERT OR REPLACE INTO projects VALUES (?, ?)", [(p, now) for p in paths])
            self._conn.commit()

    def projects(self, days=30) -> list:
        return [r[0] for r in self._exec("SELECT path FROM projects WHERE last_seen >= ?",
                                         (time.time() - days * 86400,))]

    # RAM samples
    def add_sample(self, ts, used_mb, dev_mb, claude_mb, infra_mb, top):
        self._exec("INSERT OR REPLACE INTO ram_samples VALUES (?, ?, ?, ?, ?, ?)",
                   (ts, used_mb, dev_mb, claude_mb, infra_mb, json.dumps(top)))

    def ram_series(self, hours=168, max_points=480):
        """Samples of the last `hours`, averaged into at most `max_points` buckets.
        Each bucket keeps the top consumers of its heaviest sample."""
        since = time.time() - hours * 3600
        rows = self._exec("SELECT ts, used_mb, dev_mb, claude_mb, infra_mb, top FROM ram_samples"
                          " WHERE ts >= ? ORDER BY ts", (since,))
        return bucket_samples(rows, hours * 3600 / max_points)

    def prune(self, sample_days=7, port_days=30, lineage_days=14):
        now = time.time()
        self._exec("DELETE FROM ram_samples WHERE ts < ?", (now - sample_days * 86400,))
        self._exec("DELETE FROM port_history WHERE last_seen < ?", (now - port_days * 86400,))
        self._exec("DELETE FROM keep_alive WHERE until < ?", (now,))
        self._exec("DELETE FROM projects WHERE last_seen < ?", (now - port_days * 86400,))
        self.prune_lineage(lineage_days)


def bucket_samples(rows, bucket_s):
    buckets = {}
    for ts, used, dev, claude, infra, top in rows:
        buckets.setdefault(int(ts // bucket_s), []).append((ts, used, dev, claude, infra, top))
    out = []
    for key in sorted(buckets):
        b = buckets[key]
        n = len(b)
        heaviest = max(b, key=lambda r: r[1])
        out.append({
            "ts": round(sum(r[0] for r in b) / n),
            "used_mb": round(sum(r[1] for r in b) / n, 1),
            "dev_mb": round(sum(r[2] for r in b) / n, 1),
            "claude_mb": round(sum(r[3] for r in b) / n, 1),
            "infra_mb": round(sum(r[4] for r in b) / n, 1),
            "top": json.loads(heaviest[5] or "[]"),
        })
    return out
