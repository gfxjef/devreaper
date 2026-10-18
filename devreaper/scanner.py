"""Collect dev processes, group them into process trees and attach Claude sessions."""
import os
import re
import time

import psutil

from .infra import INFRA_NAMES
from .worktrees import worktree_of

IGNORE_FRAGMENTS = ("\\adobe\\",)
GENERIC_DIRS = {"frontend", "backend", "web", "app", "client", "server", "api", "src"}
WORKTREE_RE = re.compile(r"([^\\/\"]+)[\\/]\.worktrees[\\/]([^\\/\"\s]+)", re.I)
NODE_MODULES_RE = re.compile(r"([^\\/\"]+)[\\/]([^\\/\"]+)[\\/]node_modules[\\/]", re.I)
PY_SCRIPT_RE = re.compile(r"([^\\/\"\s]+)[\\/]([^\\/\"\s]+\.py)\b", re.I)
PY_MODULE_RE = re.compile(r"\s-m\s+([\w.]+)")


def is_dev(name: str, exe: str, cmdline: str, dev_names) -> bool:
    name = (name or "").lower()
    stem = name[:-4] if name.endswith(".exe") else name
    exe_l = (exe or "").lower()
    haystack = exe_l + " " + (cmdline or "").lower()
    if any(f in haystack for f in IGNORE_FRAGMENTS):
        return False
    if stem in dev_names:
        return True
    return stem == "chrome" and "ms-playwright" in exe_l


def kind_of(cmdline: str, exe: str = "") -> str:
    c = (cmdline or "").lower()
    if "@playwright" in c and "mcp" in c:
        return "playwright-mcp"
    if "ms-playwright" in (exe or "").lower():
        return "chromium"
    if "next" in c and (" dev" in c or "next-server" in c or "start-server" in c):
        return "next dev"
    if "vite" in c:
        return "vite"
    if "uvicorn" in c:
        return "uvicorn"
    if "python" in c:
        return "python"
    return "node"


def make_label(cmdline: str, cwd: str = "", exe: str = "") -> str:
    """Human label for a process: '<repo> › <worktree>', '<project>/<subdir>', or a fallback."""
    k = kind_of(cmdline, exe)
    if k == "playwright-mcp":
        return "Playwright MCP"
    if k == "chromium":
        return "Chromium (Playwright)"
    for text in (cmdline or "", cwd or ""):
        m = WORKTREE_RE.search(text)
        if m:
            return f"{m.group(1)} › {m.group(2)}"
    for text in (cmdline or "", cwd or ""):
        m = NODE_MODULES_RE.search(text)
        if m:
            parent, folder = m.group(1), m.group(2)
            return f"{parent}/{folder}" if folder.lower() in GENERIC_DIRS else folder
    m = PY_SCRIPT_RE.search(cmdline or "")
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    m = PY_MODULE_RE.search(cmdline or "")
    if m:
        base = os.path.basename((cwd or "").rstrip("\\/"))
        return f"{m.group(1)} en {base}" if base else m.group(1)
    if cwd:
        return os.path.basename(cwd.rstrip("\\/")) or cwd
    return ""


def _ppid_map():
    # On Windows Process.ppid() rebuilds the whole process table on every call (~10 ms each);
    # one table per scan keeps a full scan well under 100 ms.
    try:
        from psutil._pswindows import ppid_map
        return ppid_map()
    except (ImportError, AttributeError):
        return None


def snapshot():
    """All processes as {pid: rec}. Cheap attributes only."""
    ppids = _ppid_map()
    procs = {}
    for p in psutil.process_iter(["pid", "name", "create_time"] + ([] if ppids is not None else ["ppid"])):
        info = p.info
        if info["create_time"] is None:
            continue
        ppid = ppids.get(info["pid"]) if ppids is not None else info["ppid"]
        procs[info["pid"]] = {
            "pid": info["pid"], "ppid": ppid, "name": info["name"] or "",
            "create_time": info["create_time"], "proc": p,
        }
    return procs


def parent_of(rec, procs):
    """Live parent record, or None if the parent died (PID absent or reused by a younger process)."""
    parent = procs.get(rec["ppid"])
    if parent is None or parent["pid"] == rec["pid"] or parent["create_time"] > rec["create_time"] + 1:
        return None
    return parent


LOCAL_IPS = {"127.0.0.1", "::1"}
_owner_cache = {}


def listening_sockets():
    """[(pid, port, ip)] for every TCP socket in LISTEN state."""
    try:
        return [(c.pid, c.laddr.port, c.laddr.ip) for c in psutil.net_connections(kind="tcp")
                if c.status == psutil.CONN_LISTEN and c.pid]
    except psutil.Error:
        return []


def owned_by(rec, user) -> bool:
    key = (rec["pid"], rec["create_time"])
    if key not in _owner_cache:
        try:
            _owner_cache[key] = bool(user) and rec["proc"].username().lower() == user
        except psutil.Error:
            _owner_cache[key] = False
        if len(_owner_cache) > 5000:
            _owner_cache.clear()
    return _owner_cache[key]


def build_groups(dev, procs):
    """dev: {pid: rec} of dev processes. Returns {root_pid: [member pids]} (root first)."""
    groups = {}
    for pid, rec in dev.items():
        root = rec
        cur = rec
        while True:
            parent = parent_of(cur, procs)
            if parent is None:
                break
            if parent["pid"] in dev:
                root = parent
            cur = parent
        groups.setdefault(root["pid"], []).append(pid)
    for root, members in groups.items():
        members.sort(key=lambda p: (p != root, dev[p]["create_time"]))
    return groups


def find_session(rec, procs, sessions):
    cur = rec
    while True:
        parent = parent_of(cur, procs)
        if parent is None:
            return None
        if parent["pid"] in sessions:
            return sessions[parent["pid"]]
        cur = parent


def find_session_by_path(texts, sessions, home=None):
    """Live session whose cwd contains the process's path (longest match). The home dir is too broad to count."""
    home = os.path.normcase(os.path.normpath(home or os.path.expanduser("~")))
    best = None
    for s in sessions.values():
        cwd = os.path.normcase(os.path.normpath(s["cwd"])) if s["cwd"] else ""
        if not cwd or home.startswith(cwd):
            continue
        if any(cwd + os.sep in os.path.normcase(t) + os.sep for t in texts if t):
            if best is None or len(cwd) > len(best["cwd"]):
                best = s
    return best


def scan(settings, sessions, db, excluded_ports, self_pid=None):
    now = time.time()
    self_pid = self_pid or os.getpid()
    dev_names = {n.lower() for n in settings["dev_names"]}
    procs = snapshot()

    own = {self_pid}
    cur = procs.get(self_pid)
    while cur is not None:  # the py/python launcher stub that started this app
        cur = parent_of(cur, procs)
        if cur is None or not cur["name"].lower().startswith("python"):
            break
        own.add(cur["pid"])
    for rec in procs.values():  # exclude this app and anything it spawned
        cur, chain = rec, []
        while cur is not None and cur["pid"] not in own:
            chain.append(cur["pid"])
            cur = parent_of(cur, procs)
        if cur is not None:
            own.update(chain)

    try:
        user = psutil.Process(os.getpid()).username().lower()
    except psutil.Error:
        user = None
    dev = {}
    for pid, rec in procs.items():
        if pid in own or pid in sessions:
            continue
        stem = rec["name"].lower().removesuffix(".exe")
        if stem not in dev_names and stem != "chrome":
            continue
        p = rec["proc"]
        try:
            exe = p.exe()
        except psutil.Error:
            exe = ""
        try:
            cmd = " ".join(p.cmdline())
        except psutil.Error:
            cmd = ""
        if not is_dev(rec["name"], exe, cmd, dev_names):
            continue
        try:
            if user and p.username().lower() != user:
                continue  # never list (or close) processes of SYSTEM, services or other accounts
        except psutil.Error:
            continue
        try:
            mb = p.memory_info().rss / 2**20
        except psutil.Error:
            mb = 0.0
        rec.update(exe=exe, cmd=cmd, mb=mb)
        dev[pid] = rec

    sockets = listening_sockets()
    ports_by_pid, exposed_by_pid = {}, {}
    for pid, port, ip in sockets:
        ports_by_pid.setdefault(pid, set()).add(port)
        if ip not in LOCAL_IPS:
            exposed_by_pid.setdefault(pid, set()).add(port)
    out = []
    for root_pid, members in build_groups(dev, procs).items():
        root = dev[root_pid]
        try:
            cwd = root["proc"].cwd()
        except psutil.Error:
            cwd = ""
        label = ""
        for pid in members:  # first member with a meaningful label wins
            label = make_label(dev[pid]["cmd"], cwd if pid == root_pid else "", dev[pid]["exe"])
            if label:
                break
        kinds = [kind_of(dev[p]["cmd"], dev[p]["exe"]) for p in members]
        kind = next((k for k in kinds if k not in ("node", "python")), kinds[0])
        ports = sorted({port for p in members for port in ports_by_pid.get(p, ())})
        exposed = sorted({port for p in members for port in exposed_by_pid.get(p, ())})
        wt = worktree_of(cwd) or next((w for w in (worktree_of(dev[p]["cmd"]) for p in members) if w), None)

        session = find_session(root, procs, sessions)
        if session is None and parent_of(root, procs) is None and db.recall(root_pid, root["create_time"]) is None:
            session = find_session_by_path([cwd] + [dev[p]["cmd"] for p in members], sessions)
        if session:
            db.remember(root_pid, root["create_time"], session)
            state = session["state"]
        else:
            known = db.recall(root_pid, root["create_time"])
            if known and known["pid"] in sessions:
                session, state = sessions[known["pid"]], sessions[known["pid"]]["state"]
            elif known:
                session, state = known, "orphan"
            else:
                state = "none"

        age_h = (now - root["create_time"]) / 3600
        mb = sum(dev[p]["mb"] for p in members)
        out.append({
            "id": f"{root_pid}-{root['create_time']:.3f}",
            "root_pid": root_pid,
            "create_time": root["create_time"],
            "pids": members,
            "label": label or root["name"],
            "kind": kind,
            "cmd": root["cmd"][:300],
            "ports": ports,
            "exposed": exposed,
            "worktree": wt,
            "cwd": cwd,
            "excluded": bool(set(ports) & excluded_ports),
            "mb": round(mb, 1),
            "age_hours": round(age_h, 2),
            "state": state,
            "session": {"name": session["name"], "id": session["id"], "pid": session["pid"]} if session else None,
            "visible": mb >= settings["show_min_mb"] or age_h >= settings["show_min_age_hours"] or state == "orphan",
        })
    out.sort(key=lambda g: g["mb"], reverse=True)

    group_of = {pid: g["id"] for g in out for pid in g["pids"]}
    listeners = []
    for pid, port, ip in sockets:
        rec = procs.get(pid)
        name = rec["name"] if rec else ""
        listeners.append({
            "port": port, "pid": pid, "ip": ip, "name": name,
            "group_id": group_of.get(pid),
            "mine": owned_by(rec, user) if rec else False,
            "infra": name.lower() in INFRA_NAMES,
            "exposed": ip not in LOCAL_IPS,
        })
    return out, listeners
