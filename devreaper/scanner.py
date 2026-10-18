"""Collect dev processes, group them into process trees and attach the coding agent that launched them."""
import os
import re
import time

import psutil

from . import agents as agents_mod
from .infra import INFRA_NAMES
from .worktrees import worktree_of

NCPU = psutil.cpu_count() or 1
GROUP_ACTIVE = 0.015  # CPU share of one core above which a dev server counts as being used

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
    exe_l = (exe or "").lower()
    name = os.path.basename(exe_l).removesuffix(".exe") or c.split(" ", 1)[0].strip('"').lower()
    if "gradle" in c:
        return "gradle"
    if "plexus-classworlds" in c or "maven" in c:
        return "maven"
    if "spring-boot" in c or "springframework.boot" in c:
        return "spring boot"
    if name in ("java", "javaw") or "java.exe" in c:
        return "java"
    if "dotnet" in name or "dotnet.exe" in c:
        if "watch" in c:
            return "dotnet watch"
        if "msbuild" in c or "vbcscompiler" in c or "build-server" in c:
            return "msbuild"
        return "dotnet"
    if name == "go" or c.startswith("go ") or "go.exe" in c:
        return "go run" if " run" in c else "go"
    if "rails" in c or "puma" in c:
        return "rails"
    if name == "ruby" or "ruby.exe" in c:
        return "ruby"
    if "python" in c:
        return "python"
    if name in ("bun", "deno"):
        return name
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
    attrs = ["pid", "name", "create_time"] + ([] if ppids is not None else ["ppid"])
    for p in psutil.process_iter():
        try:
            info = p.as_dict(attrs)  # not p.info: process_iter shares Process objects across threads
        except psutil.Error:
            continue
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


def build_groups(dev, procs, stop=frozenset()):
    """dev: {pid: rec} of dev processes. Returns {root_pid: [member pids]} (root first).
    Climbing stops at PIDs in `stop` (agents), so servers an agent launched never merge with what launched it."""
    groups = {}
    for pid, rec in dev.items():
        root = rec
        cur = rec
        while True:
            parent = parent_of(cur, procs)
            if parent is None or parent["pid"] in stop:
                break
            if parent["pid"] in dev:
                root = parent
            cur = parent
        groups.setdefault(root["pid"], []).append(pid)
    for root, members in groups.items():
        members.sort(key=lambda p: (p != root, dev[p]["create_time"]))
    return groups


def find_session(rec, procs, sessions, member_of=None):
    """First agent among the ancestors. `member_of` maps agent helper PIDs to their agent's root PID."""
    cur = rec
    while True:
        parent = parent_of(cur, procs)
        if parent is None:
            return None
        root = member_of.get(parent["pid"], parent["pid"]) if member_of else parent["pid"]
        if root in sessions:
            return sessions[root]
        cur = parent


def descendants(root_pid, children, skip):
    """Every live descendant of root_pid, not descending into PIDs in `skip`."""
    out, stack = [], list(children.get(root_pid, ()))
    while stack:
        pid = stack.pop()
        if pid in skip:
            continue
        out.append(pid)
        stack.extend(children.get(pid, ()))
    return out


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


_cwd_cache = {}
_disk = {"codex": {}, "codex_at": 0.0, "claude": {}}
DISK_EVERY_S = 30


def _cwd(rec):
    key = (rec["pid"], rec["create_time"])
    if key not in _cwd_cache:
        _cwd_cache[key] = agents_mod.agent_cwd(rec["pid"])
        if len(_cwd_cache) > 2000:
            _cwd_cache.clear()
    return _cwd_cache[key]


def _disk_activity(a, cwd, session_id, now):
    """Last activity an agent left on disk (Claude transcripts, Codex rollouts), refreshed every 30 s."""
    if a["agent_key"] == "claude":
        key = (a["pid"], a["create_time"])
        hit = _disk["claude"].get(key)
        if hit is None or now - hit[0] >= DISK_EVERY_S:
            hit = (now, agents_mod.claude_last_turn(cwd, a["cmd"], session_id))
            _disk["claude"][key] = hit
        return hit[1]
    if a["agent_key"] == "codex":
        if now - _disk["codex_at"] >= DISK_EVERY_S:
            _disk["codex"], _disk["codex_at"] = agents_mod.codex_activity(), now
        return _disk["codex"].get(os.path.normcase(os.path.normpath(cwd))) if cwd else None
    return None


def build_agents(procs, found, user, activity, settings, now):
    """{root pid: agent} with cwd, RAM, CPU and state. Claude session files, when present, add name and status."""
    agents, member_of = agents_mod.find_agents(procs, user)
    helpers = {}
    for pid, root in member_of.items():
        helpers.setdefault(root, []).append(pid)
    out = {}
    for pid, a in agents.items():
        rec = procs[pid]
        s = found.get(pid) or {}
        cwd = s.get("cwd") or _cwd(rec)
        key = ("agent", pid, rec["create_time"])
        share = activity.observe(key, agents_mod.tree_cpu(helpers[pid], procs),
                                 agents_mod.CPU_ACTIVE[a["kind"]], now)
        # What the agent wrote down (session file, last turn in its transcript) is exact; CPU is only the fallback
        # for agents that leave nothing readable (editors), since idle CLIs have occasional background bursts.
        written = [t for t in (s.get("updated"), _disk_activity(a, cwd, s.get("id", ""), now)) if t]
        last = max(written) if written else activity.last_active(key)
        out[pid] = {
            **a,
            "id": f"{a['agent_key']}:{pid}:{int(rec['create_time'])}",
            "session_id": s.get("id", ""),
            "name": s.get("name") or os.path.basename(cwd.rstrip("\\/")) or a["agent"],
            "cwd": cwd,
            "status": s.get("status", ""),
            "started": s.get("started") or rec["create_time"],
            "updated": last,
            "proc_start": s.get("proc_start"),
            "state": agents_mod.agent_state(last, now, settings["inactive_after_hours"], activity.started,
                                            s.get("status") == "busy"),
            "mb": agents_mod.tree_mb(helpers[pid], procs),
            "cpu": round(share / NCPU * 100, 1),
            "closable": a["kind"] == "cli",
        }
    return out, member_of


def scan(settings, sessions, db, excluded_ports, self_pid=None, activity=None):
    """Returns (groups, listeners, agents). `sessions`: Claude session files keyed by claude.exe PID."""
    now = time.time()
    activity = activity or agents_mod.Activity(now)
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
    agents, member_of = build_agents(procs, sessions, user, activity, settings, now)
    dev = {}
    for pid, rec in procs.items():
        if pid in own or pid in member_of or pid in sessions:
            continue
        stem = rec["name"].lower().removesuffix(".exe")
        if stem not in dev_names and stem != "chrome":
            continue
        p = rec["proc"]
        exe = rec.get("exe")
        if exe is None:
            try:
                exe = p.exe()
            except psutil.Error:
                exe = ""
        cmd = rec.get("cmd")
        if cmd is None:
            try:
                cmd = " ".join(p.cmdline())
            except psutil.Error:
                cmd = ""
        if not is_dev(rec["name"], exe, cmd, dev_names):
            continue
        if not owned_by(rec, user):
            continue  # never list (or close) processes of SYSTEM, services or other accounts
        try:
            mb = p.memory_info().rss / 2**20
        except psutil.Error:
            mb = 0.0
        rec.update(exe=exe, cmd=cmd, mb=mb)
        dev[pid] = rec

    children = {}
    for pid, rec in procs.items():
        parent = parent_of(rec, procs)
        if parent is not None:
            children.setdefault(parent["pid"], []).append(pid)
    sockets = listening_sockets()
    ports_by_pid, exposed_by_pid = {}, {}
    for pid, port, ip in sockets:
        ports_by_pid.setdefault(pid, set()).add(port)
        if ip not in LOCAL_IPS:
            exposed_by_pid.setdefault(pid, set()).add(port)
    cli_agents = {pid: a for pid, a in agents.items() if a["kind"] == "cli"}
    out, live = [], set()
    for root_pid, members in build_groups(dev, procs, stop=set(member_of)).items():
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
        # helpers that are not dev processes themselves (esbuild, the binary `go run` builds, conhost…)
        extra = [p for p in descendants(root_pid, children, set(member_of) | own) if p not in dev]
        pids = members + extra
        ports = sorted({port for p in pids for port in ports_by_pid.get(p, ())})
        exposed = sorted({port for p in pids for port in exposed_by_pid.get(p, ())})
        wt = worktree_of(cwd) or next((w for w in (worktree_of(dev[p]["cmd"]) for p in members) if w), None)

        session = find_session(root, procs, agents, member_of)
        if session is None and parent_of(root, procs) is None and db.recall(root_pid, root["create_time"]) is None:
            session = find_session_by_path([cwd] + [dev[p]["cmd"] for p in members], cli_agents)
        if session:
            db.remember(root_pid, root["create_time"], session)
            state = session["state"]
        else:
            known = db.recall(root_pid, root["create_time"])
            alive = agents.get(known["pid"]) if known else None
            # legacy rows stored Claude's sessionId instead of the agent's process identity
            if alive and (known["id"] == alive["id"] or ":" not in (known["id"] or "")):
                session, state = alive, alive["state"]
            elif known:
                session, state = known, "orphan"
            else:
                state = "none"

        gid = f"{root_pid}-{root['create_time']:.3f}"
        live.add(("group", gid))
        mb = sum(dev[p]["mb"] for p in members) + agents_mod.tree_mb(extra, procs)
        share = activity.observe(("group", gid), agents_mod.tree_cpu(pids, procs), GROUP_ACTIVE, now)
        used = activity.last_active(("group", gid))
        idle_from = used or max(activity.started, root["create_time"])
        age_h = (now - root["create_time"]) / 3600
        out.append({
            "id": gid,
            "root_pid": root_pid,
            "create_time": root["create_time"],
            "pids": pids,
            "label": label or root["name"],
            "kind": kind,
            "cmd": root["cmd"][:300],
            "ports": ports,
            "exposed": exposed,
            "worktree": wt,
            "cwd": cwd,
            "excluded": bool(set(ports) & excluded_ports),
            "mb": round(mb, 1),
            "cpu": round(share / NCPU * 100, 1),
            "idle_hours": round((now - idle_from) / 3600, 2),
            "idle_known": used is not None,
            "in_use": used is not None and now - used < settings["inactive_after_hours"] * 3600,
            "age_hours": round(age_h, 2),
            "state": state,
            "session": {"name": session["name"], "id": session["id"], "pid": session["pid"],
                        "agent": session.get("agent") or "Claude Code", "kind": session.get("kind") or "cli"}
            if session else None,
            "visible": mb >= settings["show_min_mb"] or age_h >= settings["show_min_age_hours"] or state == "orphan",
        })
    out.sort(key=lambda g: g["mb"], reverse=True)
    live |= {("agent", pid, a["create_time"]) for pid, a in agents.items()}
    activity.forget(live)

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
    return out, listeners, agents
